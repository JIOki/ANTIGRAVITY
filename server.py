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
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

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
MAX_BODY_BYTES = 100_000

# Límites de intentos de autenticación (ventana deslizante).
AUTH_WINDOW = timedelta(minutes=15)
LOGIN_FAILS_PER_EMAIL = 5
LOGIN_FAILS_PER_IP = 20
REGISTER_PER_IP = 10

SCRYPT = dict(n=2**14, r=8, p=1)
DUMMY_SALT = secrets.token_bytes(16)
DUMMY_HASH = hashlib.scrypt(b"cuenta-inexistente", salt=DUMMY_SALT, **SCRYPT)

EVENT_PATH = re.compile(r"^/api/events/(\d+)$")
ODDS_PATH = re.compile(r"^/api/events/(\d+)/odds$")
ODDS_ITEM_PATH = re.compile(r"^/api/events/(\d+)/odds/(\d+)$")


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


def initialize_db() -> None:
    with DB_LOCK, connect_db() as db:
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
        """)
        # Migración de bases creadas con la versión anterior.
        columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
        if "threshold" not in columns:
            db.execute(f"ALTER TABLE users ADD COLUMN threshold REAL NOT NULL DEFAULT {DEFAULT_THRESHOLD}")
        if "max_age_minutes" not in columns:
            db.execute(f"ALTER TABLE users ADD COLUMN max_age_minutes INTEGER NOT NULL DEFAULT {DEFAULT_MAX_AGE_MINUTES}")


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
    rows = db.execute("SELECT id, outcome, bookmaker, decimal_odds, observed_at FROM odds WHERE event_id=? "
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
        with DB_LOCK, connect_db() as db:
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
        raise RequestError(404, "No encontrado.")

    # -- POST
    def post_api(self, path: str, data: dict) -> None:
        if path in ("/api/register", "/api/login"):
            self.authenticate(path.endswith("register"), data)
            return
        if path == "/api/logout":
            token = self.session_token()
            if token:
                with DB_LOCK, connect_db() as db:
                    db.execute("DELETE FROM sessions WHERE token_hash=?", (hash_token(token),))
            reply(self, 200, {"ok": True}, {"Set-Cookie": "odds_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})
            return
        if path == "/api/profile":
            self.update_profile(data)
            return
        if path == "/api/events":
            name, sport, market, starts_at, outcomes = parse_event(data)
            with DB_LOCK, connect_db() as db:
                user_id = self.require_user(db)
                cursor = db.execute("INSERT INTO events(user_id,name,sport,market,starts_at,outcomes) VALUES(?,?,?,?,?,?)",
                                    (user_id, name, sport, market, starts_at, json.dumps(outcomes, ensure_ascii=False)))
            reply(self, 201, {"id": cursor.lastrowid})
            return
        match = ODDS_PATH.match(path)
        if match:
            self.add_odds(int(match.group(1)), data)
            return
        raise RequestError(404, "No encontrado.")

    # -- PUT
    def put_api(self, path: str, data: dict) -> None:
        match = EVENT_PATH.match(path)
        if not match:
            raise RequestError(404, "No encontrado.")
        name, sport, market, starts_at, outcomes = parse_event(data)
        with DB_LOCK, connect_db() as db:
            user_id = self.require_user(db)
            event_id = int(match.group(1))
            if not db.execute("SELECT 1 FROM events WHERE id=? AND user_id=?", (event_id, user_id)).fetchone():
                raise RequestError(404, "Evento no encontrado.")
            db.execute("UPDATE events SET name=?, sport=?, market=?, starts_at=?, outcomes=? WHERE id=? AND user_id=?",
                       (name, sport, market, starts_at, json.dumps(outcomes, ensure_ascii=False), event_id, user_id))
            placeholders = ",".join("?" * len(outcomes))
            removed = db.execute(f"DELETE FROM odds WHERE event_id=? AND outcome NOT IN ({placeholders})",
                                 (event_id, *outcomes)).rowcount
        reply(self, 200, {"ok": True, "removed_odds": removed})

    # -- DELETE
    def delete_api(self, path: str, data: dict) -> None:
        with DB_LOCK, connect_db() as db:
            user_id = self.require_user(db)
            match = EVENT_PATH.match(path)
            if match:
                cursor = db.execute("DELETE FROM events WHERE id=? AND user_id=?", (int(match.group(1)), user_id))
                if not cursor.rowcount:
                    raise RequestError(404, "Evento no encontrado.")
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
        ip_key = f"ip:{self.client_address[0]}"
        with DB_LOCK, connect_db() as db:
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
        reply(self, 200, {"ok": True}, {"Set-Cookie": f"odds_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"})

    def update_profile(self, data: dict) -> None:
        with DB_LOCK, connect_db() as db:
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

    def add_odds(self, event_id: int, data: dict) -> None:
        outcome = str(data.get("outcome", "")).strip()
        bookmaker = str(data.get("bookmaker", "")).strip()
        try:
            decimal_odds = float(data.get("decimal_odds"))
        except (TypeError, ValueError):
            decimal_odds = 0
        if not outcome or not bookmaker or not (1 < decimal_odds <= 100000):
            raise RequestError(400, "Ingresa resultado, casa y cuota decimal mayor que 1.")
        email_data = None
        alert_response = None
        with DB_LOCK, connect_db() as db:
            user_id = self.require_user(db)
            event = db.execute("SELECT * FROM events WHERE id=? AND user_id=?", (event_id, user_id)).fetchone()
            if not event:
                raise RequestError(404, "Evento no encontrado.")
            if outcome not in json.loads(event["outcomes"]):
                raise RequestError(400, "El resultado no pertenece a este mercado.")
            db.execute("INSERT INTO odds(event_id,outcome,bookmaker,decimal_odds,observed_at) VALUES(?,?,?,?,?)",
                       (event_id, outcome, bookmaker[:120], decimal_odds, iso(utcnow())))
            settings = user_settings(db, user_id)
            calc = market_calculation(db, event, settings["threshold"], settings["max_age_minutes"])
            if calc["qualifies"]:
                fingerprint = hashlib.sha256(json.dumps(
                    {key: (value["bookmaker"].strip().casefold(), value["decimal_odds"]) for key, value in sorted(calc["best_odds"].items())},
                    sort_keys=True).encode()).hexdigest()
                recipient = settings["alert_email"]
                message = ("Oportunidad calculada; alerta pendiente de configuración de correo." if recipient
                           else "Oportunidad calculada; agrega un correo personal para recibir alertas.")
                cursor = db.execute("INSERT OR IGNORE INTO alerts(user_id,event_id,fingerprint,probability_sum,status,message) VALUES(?,?,?,?,?,?)",
                                    (user_id, event_id, fingerprint, calc["probability_sum"], "pending", message))
                if cursor.rowcount:
                    email_data = (recipient, event, calc, fingerprint)
                    alert_response = {"created": True, "status": "pending", "probability_percent": calc["probability_percent"], "message": message}
        if email_data:
            recipient, event, calc, fingerprint = email_data
            selections = "\n".join(
                f"- {pick}: {quote['decimal_odds']} ({quote['bookmaker']}, capturada hace {quote['age_minutes']:.0f} min)"
                for pick, quote in calc["best_odds"].items())
            body = (f"Alerta de cuotas para {event['name']}\nMercado: {event['market']}\n"
                    f"Suma de probabilidades implícitas: {calc['probability_percent']:.2f}% (umbral {calc['threshold_percent']:.2f}%)\n"
                    f"Underround teórico: {calc['underround_percent']:.2f}%\n\nCuotas usadas:\n{selections}\n\n"
                    f"Solo se usaron cuotas con menos de {calc['max_age_minutes']} minutos de antigüedad.\n"
                    "Verifica vigencia, disponibilidad, límites y costos antes de tomar cualquier decisión. El cálculo no garantiza resultados ni ganancias.")
            sent, message = send_alert_email(recipient, f"Alerta de cuotas ≤ {calc['threshold_percent']:.2f}%: {event['name']}", body)
            with DB_LOCK, connect_db() as db:
                db.execute("UPDATE alerts SET status=?, message=? WHERE event_id=? AND fingerprint=?",
                           ("sent" if sent else "pending", message, event["id"], fingerprint))
            alert_response = {"created": True, "status": "sent" if sent else "pending",
                              "probability_percent": calc["probability_percent"], "message": message}
        reply(self, 201, {"ok": True, "alert": alert_response})


def make_server(host: str = HOST, port: int = PORT) -> ThreadingHTTPServer:
    initialize_db()
    return ThreadingHTTPServer((host, port), App)


if __name__ == "__main__":
    server = make_server()
    print(f"Cuotas MVP disponible en http://{HOST}:{PORT}")
    server.serve_forever()
