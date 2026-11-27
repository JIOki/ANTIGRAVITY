"""Local MVP for user-owned odds capture and underround alerts."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import assistant
from providers import PROVIDERS, ProviderError

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("ODDS_DB_PATH", ROOT / "data" / "odds.db"))
HOST = os.environ.get("ODDS_HOST", "127.0.0.1")
PORT = int(os.environ.get("ODDS_PORT", "8000"))
DB_LOCK = threading.RLock()

# Valores por defecto de cada cuenta; el usuario puede cambiarlos desde el panel.
DEFAULT_THRESHOLD = 0.98
DEFAULT_MAX_AGE_MINUTES = 60
THRESHOLD_RANGE = (0.50, 1.00)
MAX_AGE_RANGE = (1, 10080)  # de 1 minuto a 7 días

SESSION_TTL = timedelta(days=7)
# Detrás de un proxy inverso (Nginx), usa la IP real del cliente enviada en X-Real-IP.
TRUST_PROXY = os.environ.get("ODDS_TRUST_PROXY", "false").lower() == "true"
# Añade el atributo Secure a la cookie de sesión (actívalo cuando el sitio se sirva por HTTPS).
SECURE_COOKIE = os.environ.get("ODDS_SECURE_COOKIE", "false").lower() == "true"
COOKIE_FLAGS = "Path=/; HttpOnly; SameSite=Strict" + ("; Secure" if SECURE_COOKIE else "")
MAX_BODY_BYTES = 100_000

# Límites de intentos de autenticación (ventana deslizante).
AUTH_WINDOW = timedelta(minutes=15)
LOGIN_FAILS_PER_EMAIL = 5
LOGIN_FAILS_PER_IP = 20
REGISTER_PER_IP = 10
CHAT_PER_IP = int(os.environ.get("ASSISTANT_MESSAGES_PER_15MIN", "40"))

SCRYPT = dict(n=2**14, r=8, p=1)
DUMMY_SALT = secrets.token_bytes(16)
DUMMY_HASH = hashlib.scrypt(b"cuenta-inexistente", salt=DUMMY_SALT, **SCRYPT)

# Integraciones por API
SYNC_INTERVAL_MINUTES = max(5, int(os.environ.get("ODDS_API_INTERVAL_MINUTES", "120")))
SYNC_HORIZON_HOURS = max(1, int(os.environ.get("ODDS_API_HORIZON_HOURS", "72")))
MANUAL_SYNC_COOLDOWN = timedelta(minutes=2)
MAX_SUBSCRIPTIONS_PER_USER = 10
SYNC_LOCK = threading.Lock()
LAST_MANUAL_SYNC: dict[int, datetime] = {}

EVENT_PATH = re.compile(r"^/api/events/(\d+)$")
ODDS_PATH = re.compile(r"^/api/events/(\d+)/odds$")
ODDS_ITEM_PATH = re.compile(r"^/api/events/(\d+)/odds/(\d+)$")
SPORTS_PATH = re.compile(r"^/api/integrations/([a-z0-9-]+)/sports$")
SUBSCRIPTION_PATH = re.compile(r"^/api/integrations/subscriptions/(\d+)$")


class RequestError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


@contextmanager
def open_db():
    """Conexión con commit/rollback automático que siempre se cierra."""
    db = connect_db()
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize_db() -> None:
    with DB_LOCK, open_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash BLOB NOT NULL,
                password_salt BLOB NOT NULL,
                alert_email TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                sport TEXT NOT NULL DEFAULT '',
                market TEXT NOT NULL,
                starts_at TEXT NOT NULL DEFAULT '',
                outcomes TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS odds (
                id INTEGER PRIMARY KEY,
                event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                outcome TEXT NOT NULL,
                bookmaker TEXT NOT NULL,
                decimal_odds REAL NOT NULL CHECK(decimal_odds > 1),
                observed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS events_user_idx ON events(user_id);
            CREATE INDEX IF NOT EXISTS odds_event_idx ON odds(event_id);
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                fingerprint TEXT NOT NULL,
                probability_sum REAL NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                UNIQUE(event_id, fingerprint)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sessions_expires_idx ON sessions(expires_at);
            CREATE TABLE IF NOT EXISTS auth_attempts (
                id INTEGER PRIMARY KEY,
                key TEXT NOT NULL,
                attempted_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS auth_attempts_key_idx ON auth_attempts(key, attempted_at);
            CREATE TABLE IF NOT EXISTS api_subscriptions (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                provider TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                sport_title TEXT NOT NULL DEFAULT '',
                regions TEXT NOT NULL,
                last_sync_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                UNIQUE(user_id, provider, sport_key)
            );
            CREATE TABLE IF NOT EXISTS api_dismissed (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                provider TEXT NOT NULL,
                external_id TEXT NOT NULL,
                PRIMARY KEY(user_id, provider, external_id)
            );
            CREATE TABLE IF NOT EXISTS integration_status (
                provider TEXT PRIMARY KEY,
                last_sync_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                requests_remaining INTEGER,
                requests_used INTEGER
            );
        """)
        # Migración de bases creadas con la versión anterior.
        columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
        if "threshold" not in columns:
            db.execute(f"ALTER TABLE users ADD COLUMN threshold REAL NOT NULL DEFAULT {DEFAULT_THRESHOLD}")
        if "max_age_minutes" not in columns:
            db.execute(f"ALTER TABLE users ADD COLUMN max_age_minutes INTEGER NOT NULL DEFAULT {DEFAULT_MAX_AGE_MINUTES}")
        event_columns = {row["name"] for row in db.execute("PRAGMA table_info(events)")}
        if "source" not in event_columns:
            db.execute("ALTER TABLE events ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
        if "external_id" not in event_columns:
            db.execute("ALTER TABLE events ADD COLUMN external_id TEXT")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS events_external_idx ON events(user_id, source, external_id) WHERE external_id IS NOT NULL")
        odds_columns = {row["name"] for row in db.execute("PRAGMA table_info(odds)")}
        if "source" not in odds_columns:
            db.execute("ALTER TABLE odds ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")


# ---------------------------------------------------------------- sesiones

def create_session(db: sqlite3.Connection, user_id: int) -> str:
    now = utcnow()
    db.execute("DELETE FROM sessions WHERE expires_at <= ?", (iso(now),))
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
               (hash_token(token), user_id, iso(now), iso(now + SESSION_TTL)))
    return token


def session_user(db: sqlite3.Connection, token: str) -> int | None:
    row = db.execute("SELECT user_id FROM sessions WHERE token_hash=? AND expires_at > ?",
                     (hash_token(token), iso(utcnow()))).fetchone()
    return row["user_id"] if row else None


# ---------------------------------------------------- límite de intentos

def count_attempts(db: sqlite3.Connection, key: str) -> int:
    since = iso(utcnow() - AUTH_WINDOW)
    return db.execute("SELECT COUNT(*) FROM auth_attempts WHERE key=? AND attempted_at > ?", (key, since)).fetchone()[0]


def record_attempt(db: sqlite3.Connection, *keys: str) -> None:
    now = utcnow()
    db.execute("DELETE FROM auth_attempts WHERE attempted_at <= ?", (iso(now - AUTH_WINDOW),))
    db.executemany("INSERT INTO auth_attempts(key,attempted_at) VALUES(?,?)", [(key, iso(now)) for key in keys])


# ------------------------------------------------------------------ correo

def send_alert_email(recipient: str, subject: str, body: str) -> tuple[bool, str]:
    host = os.environ.get("SMTP_HOST", "").strip()
    sender = os.environ.get("SMTP_FROM", "").strip()
    if not host or not sender or not recipient:
        return False, "Correo pendiente: configura SMTP_HOST, SMTP_FROM y el correo de alertas."
    security = os.environ.get("SMTP_SECURITY", "").strip().lower()
    if not security:  # compatibilidad con la variable anterior
        security = "ssl" if os.environ.get("SMTP_USE_SSL", "false").lower() == "true" else "starttls"
    if security not in ("starttls", "ssl", "none"):
        return False, "SMTP_SECURITY debe ser starttls, ssl o none."
    port = int(os.environ.get("SMTP_PORT", "465" if security == "ssl" else "587"))
    username = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    try:
        if security == "ssl":
            client = smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=15)
        else:
            client = smtplib.SMTP(host, port, timeout=15)
        with client:
            client.ehlo()
            if security == "starttls":
                if not client.has_extn("starttls"):
                    return False, "El servidor SMTP no ofrece STARTTLS; usa SMTP_SECURITY=ssl o, solo en un relay local, none."
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if username:
                if security == "none":
                    return False, "No se envían credenciales SMTP sin cifrado; usa starttls o ssl."
                client.login(username, password)
            client.send_message(message)
        return True, "Correo enviado."
    except (OSError, smtplib.SMTPException) as exc:
        return False, f"No se pudo enviar el correo: {exc.__class__.__name__}."


# ------------------------------------------------------------------ cálculo

def market_calculation(db: sqlite3.Connection, event: sqlite3.Row, threshold: float, max_age_minutes: int,
                       now: datetime | None = None) -> dict:
    """Usa solo la observación más reciente de cada casa por resultado y descarta las vencidas."""
    now = now or utcnow()
    max_age = timedelta(minutes=max_age_minutes)
    outcomes = json.loads(event["outcomes"])
    rows = db.execute("SELECT id, outcome, bookmaker, decimal_odds, observed_at, source FROM odds WHERE event_id=? "
                      "ORDER BY observed_at DESC, id DESC", (event["id"],)).fetchall()
    history = []
    latest: dict[tuple[str, str], dict] = {}
    for row in rows:
        item = dict(row)
        key = (item["outcome"], item["bookmaker"].strip().casefold())
        item["age_minutes"] = max(0.0, (now - parse_iso(item["observed_at"])).total_seconds() / 60)
        item["fresh"] = item["age_minutes"] <= max_age_minutes
        item["current"] = key not in latest  # la más reciente de esa casa para ese resultado
        if item["current"]:
            latest[key] = item
        history.append(item)

    best: dict[str, dict] = {}
    stale_outcomes = []
    for outcome in outcomes:
        candidates = [q for (o, _), q in latest.items() if o == outcome]
        fresh = [q for q in candidates if q["fresh"]]
        if fresh:
            best[outcome] = max(fresh, key=lambda q: q["decimal_odds"])
        elif candidates:
            stale_outcomes.append(outcome)

    complete = all(outcome in best for outcome in outcomes)
    probability_sum = sum(1 / best[outcome]["decimal_odds"] for outcome in outcomes) if complete else None
    qualifies = complete and probability_sum <= threshold + 1e-12
    oldest = min((parse_iso(q["observed_at"]) for q in best.values()), default=None)
    return {
        "outcomes": outcomes,
        "best_odds": best,
        "complete": complete,
        "probability_sum": probability_sum,
        "probability_percent": probability_sum * 100 if probability_sum is not None else None,
        "underround_percent": (1 - probability_sum) * 100 if probability_sum is not None else None,
        "qualifies": qualifies,
        "threshold_percent": threshold * 100,
        "max_age_minutes": max_age_minutes,
        "stale_outcomes": stale_outcomes,
        "ignored_count": sum(1 for q in history if q["current"] and not q["fresh"]),
        "valid_until": iso(oldest + max_age) if oldest and complete else None,
        "history": history,
    }


def user_settings(db: sqlite3.Connection, user_id: int) -> sqlite3.Row:
    return db.execute("SELECT email, alert_email, threshold, max_age_minutes FROM users WHERE id=?", (user_id,)).fetchone()


# ------------------------------------------------------------------ alertas

def evaluate_alert(db: sqlite3.Connection, user_id: int, event: sqlite3.Row) -> tuple | None:
    """Registra una alerta nueva si el mercado cumple el umbral; devuelve los datos para enviar el correo."""
    settings = user_settings(db, user_id)
    calc = market_calculation(db, event, settings["threshold"], settings["max_age_minutes"])
    if not calc["qualifies"]:
        return None
    fingerprint = hashlib.sha256(json.dumps(
        {key: (value["bookmaker"].strip().casefold(), value["decimal_odds"]) for key, value in sorted(calc["best_odds"].items())},
        sort_keys=True).encode()).hexdigest()
    recipient = settings["alert_email"]
    message = ("Oportunidad calculada; alerta pendiente de configuración de correo." if recipient
               else "Oportunidad calculada; agrega un correo personal para recibir alertas.")
    cursor = db.execute("INSERT OR IGNORE INTO alerts(user_id,event_id,fingerprint,probability_sum,status,message) VALUES(?,?,?,?,?,?)",
                        (user_id, event["id"], fingerprint, calc["probability_sum"], "pending", message))
    return (recipient, dict(event), calc, fingerprint) if cursor.rowcount else None


def deliver_alert(pending: tuple) -> dict:
    """Envía el correo fuera del candado de la base y actualiza el estado de la alerta."""
    recipient, event, calc, fingerprint = pending
    selections = "\n".join(
        f"- {pick}: {quote['decimal_odds']} ({quote['bookmaker']}, capturada hace {quote['age_minutes']:.0f} min"
        f"{', vía ' + quote['source'] if quote.get('source', 'manual') != 'manual' else ''})"
        for pick, quote in calc["best_odds"].items())
    body = (f"Alerta de cuotas para {event['name']}\nMercado: {event['market']}\n"
            f"Suma de probabilidades implícitas: {calc['probability_percent']:.2f}% (umbral {calc['threshold_percent']:.2f}%)\n"
            f"Underround teórico: {calc['underround_percent']:.2f}%\n\nCuotas usadas:\n{selections}\n\n"
            f"Solo se usaron cuotas con menos de {calc['max_age_minutes']} minutos de antigüedad.\n"
            "Verifica vigencia, disponibilidad, límites y costos antes de tomar cualquier decisión. El cálculo no garantiza resultados ni ganancias.")
    if recipient:
        sent, message = send_alert_email(recipient, f"Alerta de cuotas ≤ {calc['threshold_percent']:.2f}%: {event['name']}", body)
    else:
        sent, message = False, "Oportunidad calculada; agrega un correo personal para recibir alertas."
    with DB_LOCK, open_db() as db:
        db.execute("UPDATE alerts SET status=?, message=? WHERE event_id=? AND fingerprint=?",
                   ("sent" if sent else "pending", message, event["id"], fingerprint))
    return {"created": True, "status": "sent" if sent else "pending",
            "probability_percent": calc["probability_percent"], "message": message}


# ------------------------------------------------------- sincronización API

def subscription_groups(db: sqlite3.Connection, user_id: int | None = None) -> dict[tuple[str, str, tuple], list[int]]:
    """Agrupa suscripciones iguales (proveedor, deporte, regiones) para hacer una sola llamada por grupo."""
    query = "SELECT id, user_id, provider, sport_key, regions FROM api_subscriptions"
    rows = db.execute(query + (" WHERE user_id=?" if user_id else ""), (user_id,) if user_id else ()).fetchall()
    groups: dict[tuple[str, str, tuple], list[int]] = {}
    for row in rows:
        key = (row["provider"], row["sport_key"], tuple(sorted(json.loads(row["regions"]))))
        groups.setdefault(key, []).append(row["id"])
    return groups


def import_provider_events(db: sqlite3.Connection, provider_key: str, user_id: int, events, now: datetime) -> tuple[int, int, list]:
    """Crea o actualiza eventos y cuotas de un proveedor para un usuario. Devuelve (eventos, cuotas, alertas)."""
    dismissed = {row[0] for row in db.execute("SELECT external_id FROM api_dismissed WHERE user_id=? AND provider=?", (user_id, provider_key))}
    stamp = iso(now)
    imported_events = imported_quotes = 0
    alerts = []
    for item in events:
        if item.external_id in dismissed:
            continue
        try:
            if parse_iso(item.starts_at) <= now:
                continue  # solo eventos por comenzar; las cuotas en vivo cambian demasiado rápido para este intervalo
        except ValueError:
            pass
        outcomes_json = json.dumps(item.outcomes, ensure_ascii=False)
        event = db.execute("SELECT * FROM events WHERE user_id=? AND source=? AND external_id=?",
                           (user_id, provider_key, item.external_id)).fetchone()
        if event is None:
            cursor = db.execute("INSERT INTO events(user_id,name,sport,market,starts_at,outcomes,source,external_id) VALUES(?,?,?,?,?,?,?,?)",
                                (user_id, item.name[:160], item.sport[:80], item.market[:120], item.starts_at[:60], outcomes_json, provider_key, item.external_id))
            event_id = cursor.lastrowid
        else:
            event_id = event["id"]
            db.execute("UPDATE events SET name=?, sport=?, starts_at=?, outcomes=? WHERE id=?",
                       (item.name[:160], item.sport[:80], item.starts_at[:60], outcomes_json, event_id))
            placeholders = ",".join("?" * len(item.outcomes))
            db.execute(f"DELETE FROM odds WHERE event_id=? AND outcome NOT IN ({placeholders})", (event_id, *item.outcomes))
        imported_events += 1
        for quote in item.quotes:
            latest = db.execute("SELECT id, decimal_odds FROM odds WHERE event_id=? AND outcome=? AND source=? "
                                "AND lower(trim(bookmaker))=lower(trim(?)) ORDER BY observed_at DESC, id DESC LIMIT 1",
                                (event_id, quote.outcome, provider_key, quote.bookmaker)).fetchone()
            if latest and abs(latest["decimal_odds"] - quote.decimal_odds) < 1e-9:
                # Misma cuota: se confirma que sigue vigente sin duplicar el historial.
                db.execute("UPDATE odds SET observed_at=? WHERE id=?", (stamp, latest["id"]))
            else:
                db.execute("INSERT INTO odds(event_id,outcome,bookmaker,decimal_odds,observed_at,source) VALUES(?,?,?,?,?,?)",
                           (event_id, quote.outcome, quote.bookmaker[:120], quote.decimal_odds, stamp, provider_key))
            imported_quotes += 1
        event = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        pending = evaluate_alert(db, user_id, event)
        if pending:
            alerts.append(pending)
    return imported_events, imported_quotes, alerts


def run_sync(user_id: int | None = None, now: datetime | None = None) -> dict:
    """Consulta los proveedores para las suscripciones (de todos o de un usuario) e importa las cuotas."""
    if not SYNC_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "Ya hay una sincronización en curso."}
    try:
        now = now or utcnow()
        with DB_LOCK, open_db() as db:
            groups = subscription_groups(db, user_id)
            owners = {row["id"]: row["user_id"] for row in db.execute("SELECT id, user_id FROM api_subscriptions")}
        summary = {"ok": True, "calls": 0, "events": 0, "quotes": 0, "alerts": 0, "errors": []}
        pending_alerts = []
        for (provider_key, sport_key, regions), subscription_ids in groups.items():
            provider = PROVIDERS.get(provider_key)
            error = ""
            result = None
            if provider is None:
                error = "Proveedor no disponible."
            else:
                try:
                    result = provider.fetch_odds(sport_key, list(regions), SYNC_HORIZON_HOURS)
                    summary["calls"] += 1
                except ProviderError as exc:
                    error = str(exc)
            with DB_LOCK, open_db() as db:
                if result is not None:
                    for sub_id in subscription_ids:
                        events, quotes, alerts = import_provider_events(db, provider_key, owners[sub_id], result.events, now)
                        summary["events"] += events
                        summary["quotes"] += quotes
                        pending_alerts.extend(alerts)
                    db.execute("INSERT INTO integration_status(provider,last_sync_at,last_error,requests_remaining,requests_used) VALUES(?,?,?,?,?) "
                               "ON CONFLICT(provider) DO UPDATE SET last_sync_at=excluded.last_sync_at, last_error='', "
                               "requests_remaining=COALESCE(excluded.requests_remaining, requests_remaining), "
                               "requests_used=COALESCE(excluded.requests_used, requests_used)",
                               (provider_key, iso(now), "", result.requests_remaining, result.requests_used))
                else:
                    summary["errors"].append(f"{sport_key}: {error}")
                    db.execute("INSERT INTO integration_status(provider,last_error) VALUES(?,?) "
                               "ON CONFLICT(provider) DO UPDATE SET last_error=excluded.last_error", (provider_key, error))
                db.executemany("UPDATE api_subscriptions SET last_sync_at=?, last_error=? WHERE id=?",
                               [(iso(now), error, sub_id) for sub_id in subscription_ids])
        for pending in pending_alerts:
            deliver_alert(pending)
        summary["alerts"] = len(pending_alerts)
        return summary
    finally:
        SYNC_LOCK.release()


def estimated_monthly_credits(db: sqlite3.Connection) -> int:
    calls_per_month = 30 * 24 * 60 / SYNC_INTERVAL_MINUTES
    total = 0
    for (provider_key, _, regions) in subscription_groups(db):
        provider = PROVIDERS.get(provider_key)
        if provider:
            total += provider.credits_per_call(list(regions)) * calls_per_month
    return round(total)


def sync_loop(stop: threading.Event) -> None:
    while not stop.wait(SYNC_INTERVAL_MINUTES * 60):
        try:
            summary = run_sync()
            print(f"[sync] {summary}")
        except Exception as exc:  # el hilo no debe morir por un error inesperado
            print(f"[sync] error: {exc!r}")


def assistant_user_context(db: sqlite3.Connection, user_id: int) -> str:
    """Resumen breve de la cuenta para que el asistente pueda guiar al usuario."""
    settings = user_settings(db, user_id)
    events = db.execute("SELECT * FROM events WHERE user_id=? ORDER BY created_at DESC LIMIT 8", (user_id,)).fetchall()
    total = db.execute("SELECT COUNT(*) FROM events WHERE user_id=?", (user_id,)).fetchone()[0]
    subs = [row["sport_title"] for row in db.execute("SELECT sport_title FROM api_subscriptions WHERE user_id=?", (user_id,))]
    lines = [
        f"- Umbral: {settings['threshold'] * 100:.2f}%. Vigencia de cuotas: {settings['max_age_minutes']} min.",
        f"- Correo de alertas: {'configurado' if settings['alert_email'] else 'sin configurar'}.",
        f"- Cuotas por API: {'configurado' if any(p.configured for p in PROVIDERS.values()) else 'sin clave en el servidor'}; "
        f"deportes seguidos: {', '.join(subs) if subs else 'ninguno'}.",
        f"- Eventos en la cuenta: {total}." + (" Aún no tiene eventos." if not total else ""),
    ]
    for event in events:
        calc = market_calculation(db, event, settings["threshold"], settings["max_age_minutes"])
        if calc["complete"]:
            state = f"suma {calc['probability_percent']:.2f}%" + (" (umbral alcanzado)" if calc["qualifies"] else "")
        else:
            missing = [o for o in calc["outcomes"] if o not in calc["best_odds"]]
            state = f"incompleto, faltan cuotas vigentes de: {', '.join(missing)}"
        lines.append(f"  • {event['name']} [{event['market']}]: {state}")
    return "\n".join(lines)


def parse_event(data: dict) -> tuple[str, str, str, str, list[str]]:
    name = str(data.get("name", "")).strip()
    sport = str(data.get("sport", "")).strip()
    market = str(data.get("market", "")).strip()
    starts_at = str(data.get("starts_at", "")).strip()
    outcomes = data.get("outcomes")
    if not name or not market or not isinstance(outcomes, list):
        raise RequestError(400, "Completa evento, mercado y resultados.")
    outcomes = list(dict.fromkeys(str(x).strip()[:80] for x in outcomes if str(x).strip()))
    if len(outcomes) < 2 or len(outcomes) > 20:
        raise RequestError(400, "Agrega entre 2 y 20 resultados distintos.")
    return name[:160], sport[:80], market[:120], starts_at[:60], outcomes


# ------------------------------------------------------------------- HTTP

def reply(handler: BaseHTTPRequestHandler, status: int, payload: dict, headers: dict[str, str] | None = None) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)


STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/assistant.js": ("assistant.js", "text/javascript; charset=utf-8"),
    "/assistant.css": ("assistant.css", "text/css; charset=utf-8"),
}


class App(BaseHTTPRequestHandler):
    server_version = "CuotasMVP/0.2"

    def body_json(self) -> dict:
        raw = self.headers.get("Content-Length")
        if raw is None:
            return {}
        try:
            size = int(raw)
        except ValueError:
            raise RequestError(400, "Content-Length inválido.")
        if size < 0:
            raise RequestError(400, "Content-Length inválido.")
        if size > MAX_BODY_BYTES:
            raise RequestError(413, "La solicitud excede el tamaño permitido.")
        try:
            value = json.loads(self.rfile.read(size) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise RequestError(400, "JSON inválido.")
        if not isinstance(value, dict):
            raise RequestError(400, "Se esperaba un objeto JSON.")
        return value

    def client_ip(self) -> str:
        if TRUST_PROXY:
            real_ip = (self.headers.get("X-Real-IP") or "").strip()
            if real_ip:
                return real_ip[:64]
        return self.client_address[0]

    def session_token(self) -> str | None:
        morsel = SimpleCookie(self.headers.get("Cookie", "")).get("odds_session")
        return morsel.value if morsel else None

    def require_user(self, db: sqlite3.Connection) -> int:
        token = self.session_token()
        user_id = session_user(db, token) if token else None
        if user_id is None:
            raise RequestError(401, "Inicia sesión para continuar.")
        return user_id

    def handle_api(self, method: str) -> None:
        path = urlparse(self.path).path
        try:
            data = self.body_json() if method in ("POST", "PUT") else {}
            handler = getattr(self, f"{method.lower()}_api")
            handler(path, data)
        except RequestError as exc:
            reply(self, exc.status, {"error": exc.message})

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in STATIC:
            name, content_type = STATIC[path]
            data = (ROOT / "web" / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return
        self.handle_api("GET")

    def do_POST(self) -> None:
        self.handle_api("POST")

    def do_PUT(self) -> None:
        self.handle_api("PUT")

    def do_DELETE(self) -> None:
        self.handle_api("DELETE")

    # -- GET
    def get_api(self, path: str, data: dict) -> None:
        if path == "/api/assistant":
            with DB_LOCK, open_db() as db:
                token = self.session_token()
                logged_in = bool(token and session_user(db, token))
            reply(self, 200, assistant.greeting(logged_in))
            return
        with DB_LOCK, open_db() as db:
            user_id = self.require_user(db)
            settings = user_settings(db, user_id)
            if path == "/api/me":
                reply(self, 200, {"email": settings["email"], "alert_email": settings["alert_email"],
                                  "threshold_percent": settings["threshold"] * 100,
                                  "max_age_minutes": settings["max_age_minutes"]})
                return
            if path == "/api/events":
                events = db.execute("SELECT * FROM events WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()
                items = []
                for event in events:
                    item = dict(event)
                    item["outcomes"] = json.loads(item["outcomes"])
                    item["calculation"] = market_calculation(db, event, settings["threshold"], settings["max_age_minutes"])
                    item["alerts"] = [dict(row) for row in db.execute(
                        "SELECT status, probability_sum, message, created_at FROM alerts WHERE event_id=? "
                        "ORDER BY created_at DESC LIMIT 5", (event["id"],))]
                    items.append(item)
                reply(self, 200, {"events": items, "threshold_percent": settings["threshold"] * 100,
                                  "max_age_minutes": settings["max_age_minutes"]})
                return
            if path == "/api/integrations":
                status = {row["provider"]: dict(row) for row in db.execute("SELECT * FROM integration_status")}
                providers = [{
                    "key": provider.key, "title": provider.title, "configured": provider.configured,
                    "api_key_env": provider.api_key_env, "regions": provider.regions,
                    "status": status.get(provider.key, {}),
                } for provider in PROVIDERS.values()]
                subscriptions = [dict(row, regions=json.loads(row["regions"])) for row in db.execute(
                    "SELECT id, provider, sport_key, sport_title, regions, last_sync_at, last_error FROM api_subscriptions "
                    "WHERE user_id=? ORDER BY created_at", (user_id,))]
                reply(self, 200, {"providers": providers, "subscriptions": subscriptions,
                                  "max_age_minutes": settings["max_age_minutes"],
                                  "interval_minutes": SYNC_INTERVAL_MINUTES, "horizon_hours": SYNC_HORIZON_HOURS,
                                  "estimated_monthly_credits": estimated_monthly_credits(db)})
                return
        match = SPORTS_PATH.match(path)
        if match:
            provider = PROVIDERS.get(match.group(1))
            if provider is None:
                raise RequestError(404, "Proveedor no encontrado.")
            try:
                sports = provider.list_sports()
            except ProviderError as exc:
                raise RequestError(502, str(exc))
            reply(self, 200, {"sports": sports})
            return
        raise RequestError(404, "No encontrado.")

    # -- POST
    def post_api(self, path: str, data: dict) -> None:
        if path in ("/api/register", "/api/login"):
            self.authenticate(path.endswith("register"), data)
            return
        if path == "/api/logout":
            token = self.session_token()
            if token:
                with DB_LOCK, open_db() as db:
                    db.execute("DELETE FROM sessions WHERE token_hash=?", (hash_token(token),))
            reply(self, 200, {"ok": True}, {"Set-Cookie": f"odds_session=; {COOKIE_FLAGS}; Max-Age=0"})
            return
        if path == "/api/profile":
            self.update_profile(data)
            return
        if path == "/api/assistant":
            self.assistant_chat(data)
            return
        if path == "/api/events":
            name, sport, market, starts_at, outcomes = parse_event(data)
            with DB_LOCK, open_db() as db:
                user_id = self.require_user(db)
                cursor = db.execute("INSERT INTO events(user_id,name,sport,market,starts_at,outcomes) VALUES(?,?,?,?,?,?)",
                                    (user_id, name, sport, market, starts_at, json.dumps(outcomes, ensure_ascii=False)))
            reply(self, 201, {"id": cursor.lastrowid})
            return
        match = ODDS_PATH.match(path)
        if match:
            self.add_odds(int(match.group(1)), data)
            return
        if path == "/api/integrations/subscriptions":
            self.add_subscription(data)
            return
        if path == "/api/integrations/sync":
            with DB_LOCK, open_db() as db:
                user_id = self.require_user(db)
            last = LAST_MANUAL_SYNC.get(user_id)
            if last and utcnow() - last < MANUAL_SYNC_COOLDOWN:
                raise RequestError(429, "Espera un par de minutos entre sincronizaciones manuales para cuidar tus créditos.")
            LAST_MANUAL_SYNC[user_id] = utcnow()
            summary = run_sync(user_id)
            reply(self, 200 if summary.get("ok") else 409, summary)
            return
        raise RequestError(404, "No encontrado.")

    # -- PUT
    def put_api(self, path: str, data: dict) -> None:
        match = EVENT_PATH.match(path)
        if not match:
            raise RequestError(404, "No encontrado.")
        name, sport, market, starts_at, outcomes = parse_event(data)
        with DB_LOCK, open_db() as db:
            user_id = self.require_user(db)
            event_id = int(match.group(1))
            current = db.execute("SELECT source, outcomes FROM events WHERE id=? AND user_id=?", (event_id, user_id)).fetchone()
            if not current:
                raise RequestError(404, "Evento no encontrado.")
            if current["source"] != "manual" and outcomes != json.loads(current["outcomes"]):
                raise RequestError(400, "Los resultados de un evento importado por API los define el proveedor.")
            db.execute("UPDATE events SET name=?, sport=?, market=?, starts_at=?, outcomes=? WHERE id=? AND user_id=?",
                       (name, sport, market, starts_at, json.dumps(outcomes, ensure_ascii=False), event_id, user_id))
            placeholders = ",".join("?" * len(outcomes))
            removed = db.execute(f"DELETE FROM odds WHERE event_id=? AND outcome NOT IN ({placeholders})",
                                 (event_id, *outcomes)).rowcount
        reply(self, 200, {"ok": True, "removed_odds": removed})

    # -- DELETE
    def delete_api(self, path: str, data: dict) -> None:
        with DB_LOCK, open_db() as db:
            user_id = self.require_user(db)
            match = EVENT_PATH.match(path)
            if match:
                event = db.execute("SELECT source, external_id FROM events WHERE id=? AND user_id=?", (int(match.group(1)), user_id)).fetchone()
                if not event:
                    raise RequestError(404, "Evento no encontrado.")
                if event["external_id"]:
                    # Para que la próxima sincronización no lo vuelva a crear.
                    db.execute("INSERT OR IGNORE INTO api_dismissed(user_id,provider,external_id) VALUES(?,?,?)",
                               (user_id, event["source"], event["external_id"]))
                db.execute("DELETE FROM events WHERE id=? AND user_id=?", (int(match.group(1)), user_id))
                reply(self, 200, {"ok": True})
                return
            match = SUBSCRIPTION_PATH.match(path)
            if match:
                cursor = db.execute("DELETE FROM api_subscriptions WHERE id=? AND user_id=?", (int(match.group(1)), user_id))
                if not cursor.rowcount:
                    raise RequestError(404, "Suscripción no encontrada.")
                reply(self, 200, {"ok": True})
                return
            match = ODDS_ITEM_PATH.match(path)
            if match:
                cursor = db.execute("DELETE FROM odds WHERE id=? AND event_id IN (SELECT id FROM events WHERE id=? AND user_id=?)",
                                    (int(match.group(2)), int(match.group(1)), user_id))
                if not cursor.rowcount:
                    raise RequestError(404, "Cuota no encontrada.")
                reply(self, 200, {"ok": True})
                return
        raise RequestError(404, "No encontrado.")

    # ---------------------------------------------------------- acciones
    def authenticate(self, register: bool, data: dict) -> None:
        email = str(data.get("email", "")).strip().lower()
        password = str(data.get("password", ""))
        if len(email) > 254 or "@" not in email or len(password) < 10 or len(password) > 1024:
            raise RequestError(400, "Ingresa un correo válido y una contraseña de 10 a 1024 caracteres.")
        ip_key = f"ip:{self.client_ip()}"
        with DB_LOCK, open_db() as db:
            if register:
                if count_attempts(db, f"register:{ip_key}") >= REGISTER_PER_IP:
                    raise RequestError(429, "Demasiados registros desde esta conexión. Intenta más tarde.")
                record_attempt(db, f"register:{ip_key}")
                db.commit()  # el intento cuenta aunque el registro falle
                salt = secrets.token_bytes(16)
                digest = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
                try:
                    cursor = db.execute("INSERT INTO users(email,password_hash,password_salt,alert_email) VALUES(?,?,?,?)",
                                        (email, digest, salt, email))
                except sqlite3.IntegrityError:
                    # Mensaje genérico: no confirma directamente qué correos están registrados.
                    raise RequestError(400, "No se pudo crear la cuenta con esos datos. Si ya tienes una, inicia sesión.")
                user_id = cursor.lastrowid
            else:
                email_key = f"login:{email}"
                if count_attempts(db, email_key) >= LOGIN_FAILS_PER_EMAIL or count_attempts(db, f"login:{ip_key}") >= LOGIN_FAILS_PER_IP:
                    raise RequestError(429, "Demasiados intentos fallidos. Espera 15 minutos e intenta de nuevo.")
                user = db.execute("SELECT id, password_hash, password_salt FROM users WHERE email=?", (email,)).fetchone()
                # Se calcula scrypt también si la cuenta no existe, para no revelarlo por el tiempo de respuesta.
                salt, expected = (user["password_salt"], user["password_hash"]) if user else (DUMMY_SALT, DUMMY_HASH)
                candidate = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
                if not user or not hmac.compare_digest(candidate, expected):
                    record_attempt(db, email_key, f"login:{ip_key}")
                    db.commit()
                    raise RequestError(401, "Correo o contraseña incorrectos.")
                db.execute("DELETE FROM auth_attempts WHERE key=?", (email_key,))
                user_id = user["id"]
            token = create_session(db, user_id)
        max_age = int(SESSION_TTL.total_seconds())
        reply(self, 200, {"ok": True}, {"Set-Cookie": f"odds_session={token}; {COOKIE_FLAGS}; Max-Age={max_age}"})

    def update_profile(self, data: dict) -> None:
        with DB_LOCK, open_db() as db:
            user_id = self.require_user(db)
            settings = dict(user_settings(db, user_id))
            if "alert_email" in data:
                alert_email = str(data.get("alert_email", "")).strip().lower()
                if alert_email and ("@" not in alert_email or len(alert_email) > 254):
                    raise RequestError(400, "Correo de alertas inválido.")
                settings["alert_email"] = alert_email
            if "threshold_percent" in data:
                try:
                    threshold = float(data["threshold_percent"]) / 100
                except (TypeError, ValueError):
                    threshold = -1
                if not THRESHOLD_RANGE[0] <= threshold <= THRESHOLD_RANGE[1]:
                    raise RequestError(400, "El umbral debe estar entre 50% y 100%.")
                settings["threshold"] = round(threshold, 6)
            if "max_age_minutes" in data:
                try:
                    max_age = int(data["max_age_minutes"])
                except (TypeError, ValueError):
                    max_age = 0
                if not MAX_AGE_RANGE[0] <= max_age <= MAX_AGE_RANGE[1]:
                    raise RequestError(400, "La vigencia debe estar entre 1 y 10080 minutos (7 días).")
                settings["max_age_minutes"] = max_age
            db.execute("UPDATE users SET alert_email=?, threshold=?, max_age_minutes=? WHERE id=?",
                       (settings["alert_email"], settings["threshold"], settings["max_age_minutes"], user_id))
        reply(self, 200, {"ok": True, "alert_email": settings["alert_email"],
                          "threshold_percent": settings["threshold"] * 100, "max_age_minutes": settings["max_age_minutes"]})

    def assistant_chat(self, data: dict) -> None:
        question = str(data.get("message", "")).strip()
        if not question:
            raise RequestError(400, "Escribe una pregunta.")
        chat_key = f"chat:ip:{self.client_ip()}"
        with DB_LOCK, open_db() as db:
            if count_attempts(db, chat_key) >= CHAT_PER_IP:
                raise RequestError(429, "Hiciste muchas preguntas seguidas. Espera unos minutos e intenta de nuevo.")
            record_attempt(db, chat_key)
            token = self.session_token()
            user_id = session_user(db, token) if token else None
            context = assistant_user_context(db, user_id) if user_id else ""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")  # Nginx: entregar cada fragmento sin esperar
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.close_connection = True
        try:
            for event in assistant.answer_stream(question, data.get("history"), bool(user_id), context):
                self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # el visitante cerró el chat

    def add_subscription(self, data: dict) -> None:
        provider = PROVIDERS.get(str(data.get("provider", "")))
        sport_key = str(data.get("sport_key", "")).strip()
        sport_title = str(data.get("sport_title", "")).strip()[:120]
        regions = data.get("regions")
        if provider is None:
            raise RequestError(400, "Proveedor no válido.")
        if not re.fullmatch(r"[a-z0-9_]{2,80}", sport_key):
            raise RequestError(400, "Deporte no válido.")
        if not isinstance(regions, list) or not regions or any(r not in provider.regions for r in regions):
            raise RequestError(400, "Elige al menos una región válida.")
        regions = sorted(set(regions))
        with DB_LOCK, open_db() as db:
            user_id = self.require_user(db)
            count = db.execute("SELECT COUNT(*) FROM api_subscriptions WHERE user_id=?", (user_id,)).fetchone()[0]
            if count >= MAX_SUBSCRIPTIONS_PER_USER:
                raise RequestError(400, f"Máximo {MAX_SUBSCRIPTIONS_PER_USER} suscripciones por cuenta.")
            try:
                cursor = db.execute("INSERT INTO api_subscriptions(user_id,provider,sport_key,sport_title,regions) VALUES(?,?,?,?,?)",
                                    (user_id, provider.key, sport_key, sport_title or sport_key, json.dumps(regions)))
            except sqlite3.IntegrityError:
                raise RequestError(409, "Ya sigues ese deporte con este proveedor.")
        reply(self, 201, {"id": cursor.lastrowid})

    def add_odds(self, event_id: int, data: dict) -> None:
        outcome = str(data.get("outcome", "")).strip()
        bookmaker = str(data.get("bookmaker", "")).strip()
        try:
            decimal_odds = float(data.get("decimal_odds"))
        except (TypeError, ValueError):
            decimal_odds = 0
        if not outcome or not bookmaker or not (1 < decimal_odds <= 100000):
            raise RequestError(400, "Ingresa resultado, casa y cuota decimal mayor que 1.")
        with DB_LOCK, open_db() as db:
            user_id = self.require_user(db)
            event = db.execute("SELECT * FROM events WHERE id=? AND user_id=?", (event_id, user_id)).fetchone()
            if not event:
                raise RequestError(404, "Evento no encontrado.")
            if outcome not in json.loads(event["outcomes"]):
                raise RequestError(400, "El resultado no pertenece a este mercado.")
            db.execute("INSERT INTO odds(event_id,outcome,bookmaker,decimal_odds,observed_at,source) VALUES(?,?,?,?,?,'manual')",
                       (event_id, outcome, bookmaker[:120], decimal_odds, iso(utcnow())))
            pending = evaluate_alert(db, user_id, event)
        alert_response = deliver_alert(pending) if pending else None
        reply(self, 201, {"ok": True, "alert": alert_response})


def make_server(host: str = HOST, port: int = PORT) -> ThreadingHTTPServer:
    initialize_db()
    return ThreadingHTTPServer((host, port), App)


if __name__ == "__main__":
    server = make_server()
    if assistant.ASSISTANT_ENABLED:
        threading.Thread(target=assistant.ensure_model, daemon=True).start()
    configured = [p.title for p in PROVIDERS.values() if p.configured]
    if configured:
        threading.Thread(target=sync_loop, args=(threading.Event(),), daemon=True).start()
        print(f"Sincronización automática con {', '.join(configured)} cada {SYNC_INTERVAL_MINUTES} min.")
    print(f"Cuotas MVP disponible en http://{HOST}:{PORT}")
    server.serve_forever()
