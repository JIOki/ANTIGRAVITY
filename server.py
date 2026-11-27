"""Local MVP for user-owned odds capture and underround alerts."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import smtplib
import sqlite3
import ssl
import threading
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
THRESHOLD = 0.98
DB_LOCK = threading.RLock()
SESSIONS: dict[str, int] = {}


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
        """)


def reply(handler: BaseHTTPRequestHandler, status: int, payload: dict, headers: dict[str, str] | None = None) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)


def send_alert_email(recipient: str, subject: str, body: str) -> tuple[bool, str]:
    host = os.environ.get("SMTP_HOST", "").strip()
    sender = os.environ.get("SMTP_FROM", "").strip()
    if not host or not sender or not recipient:
        return False, "Correo pendiente: configura SMTP_HOST, SMTP_FROM y el correo de alertas."
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    try:
        if os.environ.get("SMTP_USE_SSL", "false").lower() == "true":
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=15) as client:
                if username:
                    client.login(username, password)
                client.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=15) as client:
                client.starttls(context=ssl.create_default_context())
                if username:
                    client.login(username, password)
                client.send_message(message)
        return True, "Correo enviado."
    except (OSError, smtplib.SMTPException) as exc:
        return False, f"No se pudo enviar el correo: {exc.__class__.__name__}."


def market_calculation(db: sqlite3.Connection, event: sqlite3.Row) -> dict:
    outcomes = json.loads(event["outcomes"])
    rows = db.execute("SELECT outcome, bookmaker, decimal_odds, observed_at FROM odds WHERE event_id=? ORDER BY observed_at DESC", (event["id"],)).fetchall()
    best = {}
    history = []
    for row in rows:
        history.append(dict(row))
        current = best.get(row["outcome"])
        if current is None or row["decimal_odds"] > current["decimal_odds"]:
            best[row["outcome"]] = dict(row)
    complete = all(outcome in best for outcome in outcomes)
    probability_sum = sum(1 / best[outcome]["decimal_odds"] for outcome in outcomes) if complete else None
    qualifies = complete and probability_sum <= THRESHOLD
    return {
        "outcomes": outcomes,
        "best_odds": best,
        "complete": complete,
        "probability_sum": probability_sum,
        "probability_percent": probability_sum * 100 if probability_sum is not None else None,
        "underround_percent": (1 - probability_sum) * 100 if probability_sum is not None else None,
        "qualifies": qualifies,
        "threshold_percent": THRESHOLD * 100,
        "history": history,
    }


class App(BaseHTTPRequestHandler):
    server_version = "CuotasMVP/0.1"

    def log_message(self, fmt: str, *args) -> None:
        # Keep request logs useful without printing authorization headers or passwords.
        super().log_message(fmt, *args)

    def body_json(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        if size > 100_000:
            raise ValueError("La solicitud excede el tamaño permitido.")
        value = json.loads(self.rfile.read(size) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("Se esperaba un objeto JSON.")
        return value

    def user_id(self) -> int | None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get("odds_session")
        return SESSIONS.get(morsel.value) if morsel else None

    def require_user(self) -> int | None:
        user_id = self.user_id()
        if user_id is None:
            reply(self, HTTPStatus.UNAUTHORIZED, {"error": "Inicia sesión para continuar."})
        return user_id

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            file_path = ROOT / "web" / "index.html"
            data = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/app.css" or path == "/app.js":
            file_path = ROOT / "web" / path.lstrip("/")
            data = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/css; charset=utf-8" if path.endswith("css") else "text/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/me":
            user_id = self.require_user()
            if user_id is None:
                return
            with DB_LOCK, connect_db() as db:
                user = db.execute("SELECT email, alert_email FROM users WHERE id=?", (user_id,)).fetchone()
            reply(self, 200, {"email": user["email"], "alert_email": user["alert_email"]})
            return
        if path == "/api/events":
            user_id = self.require_user()
            if user_id is None:
                return
            with DB_LOCK, connect_db() as db:
                events = db.execute("SELECT * FROM events WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()
                items = []
                for event in events:
                    item = dict(event)
                    item["outcomes"] = json.loads(item["outcomes"])
                    item["calculation"] = market_calculation(db, event)
                    item["alerts"] = [dict(row) for row in db.execute("SELECT status, probability_sum, message, created_at FROM alerts WHERE event_id=? ORDER BY created_at DESC LIMIT 5", (event["id"],))]
                    items.append(item)
            reply(self, 200, {"events": items, "threshold_percent": THRESHOLD * 100})
            return
        reply(self, 404, {"error": "No encontrado."})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            data = self.body_json()
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            reply(self, 400, {"error": str(exc) or "JSON inválido."})
            return
        if path in ("/api/register", "/api/login"):
            email = str(data.get("email", "")).strip().lower()
            password = str(data.get("password", ""))
            if len(email) > 254 or "@" not in email or len(password) < 10:
                reply(self, 400, {"error": "Ingresa un correo válido y una contraseña de al menos 10 caracteres."})
                return
            with DB_LOCK, connect_db() as db:
                user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
                if path.endswith("register"):
                    if user:
                        reply(self, 409, {"error": "Ya existe una cuenta con ese correo."})
                        return
                    salt = secrets.token_bytes(16)
                    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
                    try:
                        cursor = db.execute("INSERT INTO users(email,password_hash,password_salt,alert_email) VALUES(?,?,?,?)", (email, digest, salt, email))
                    except sqlite3.IntegrityError:
                        reply(self, 409, {"error": "Ya existe una cuenta con ese correo."})
                        return
                    user_id = cursor.lastrowid
                else:
                    if not user:
                        reply(self, 401, {"error": "Correo o contraseña incorrectos."})
                        return
                    candidate = hashlib.scrypt(password.encode(), salt=user["password_salt"], n=2**14, r=8, p=1)
                    if not hmac.compare_digest(candidate, user["password_hash"]):
                        reply(self, 401, {"error": "Correo o contraseña incorrectos."})
                        return
                    user_id = user["id"]
            token = secrets.token_urlsafe(32)
            SESSIONS[token] = user_id
            reply(self, 200, {"ok": True}, {"Set-Cookie": f"odds_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800"})
            return
        if path == "/api/logout":
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            morsel = cookie.get("odds_session")
            if morsel:
                SESSIONS.pop(morsel.value, None)
            reply(self, 200, {"ok": True}, {"Set-Cookie": "odds_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})
            return
        user_id = self.require_user()
        if user_id is None:
            return
        if path == "/api/profile":
            alert_email = str(data.get("alert_email", "")).strip().lower()
            if alert_email and ("@" not in alert_email or len(alert_email) > 254):
                reply(self, 400, {"error": "Correo de alertas inválido."})
                return
            with DB_LOCK, connect_db() as db:
                db.execute("UPDATE users SET alert_email=? WHERE id=?", (alert_email, user_id))
            reply(self, 200, {"ok": True, "alert_email": alert_email})
            return
        if path == "/api/events":
            name = str(data.get("name", "")).strip()
            sport = str(data.get("sport", "")).strip()
            market = str(data.get("market", "")).strip()
            outcomes = data.get("outcomes")
            starts_at = str(data.get("starts_at", "")).strip()
            if not name or not market or not isinstance(outcomes, list):
                reply(self, 400, {"error": "Completa evento, mercado y resultados."})
                return
            outcomes = list(dict.fromkeys(str(x).strip() for x in outcomes if str(x).strip()))
            if len(outcomes) < 2 or len(outcomes) > 20:
                reply(self, 400, {"error": "Agrega entre 2 y 20 resultados distintos."})
                return
            with DB_LOCK, connect_db() as db:
                cursor = db.execute("INSERT INTO events(user_id,name,sport,market,starts_at,outcomes) VALUES(?,?,?,?,?,?)", (user_id, name[:160], sport[:80], market[:120], starts_at[:60], json.dumps(outcomes, ensure_ascii=False)))
                event_id = cursor.lastrowid
            reply(self, 201, {"id": event_id})
            return
        if path.startswith("/api/events/") and path.endswith("/odds"):
            parts = path.strip("/").split("/")
            if len(parts) != 4 or not parts[2].isdigit():
                reply(self, 404, {"error": "No encontrado."})
                return
            event_id = int(parts[2])
            outcome = str(data.get("outcome", "")).strip()
            bookmaker = str(data.get("bookmaker", "")).strip()
            try:
                decimal_odds = float(data.get("decimal_odds"))
            except (TypeError, ValueError):
                decimal_odds = 0
            if not outcome or not bookmaker or not (1 < decimal_odds <= 100000):
                reply(self, 400, {"error": "Ingresa resultado, casa y cuota decimal mayor que 1."})
                return
            email_data = None
            alert_response = None
            with DB_LOCK, connect_db() as db:
                event = db.execute("SELECT * FROM events WHERE id=? AND user_id=?", (event_id, user_id)).fetchone()
                if not event:
                    reply(self, 404, {"error": "Evento no encontrado."})
                    return
                if outcome not in json.loads(event["outcomes"]):
                    reply(self, 400, {"error": "El resultado no pertenece a este mercado."})
                    return
                db.execute("INSERT INTO odds(event_id,outcome,bookmaker,decimal_odds) VALUES(?,?,?,?)", (event_id, outcome, bookmaker[:120], decimal_odds))
                event = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
                calc = market_calculation(db, event)
                if calc["qualifies"]:
                    fingerprint = hashlib.sha256(json.dumps({key: (value["bookmaker"], value["decimal_odds"]) for key, value in sorted(calc["best_odds"].items())}, sort_keys=True).encode()).hexdigest()
                    user = db.execute("SELECT alert_email FROM users WHERE id=?", (user_id,)).fetchone()
                    recipient = user["alert_email"]
                    status = "pending"
                    message = "Oportunidad calculada; alerta pendiente de configuración de correo." if recipient else "Oportunidad calculada; agrega un correo personal para recibir alertas."
                    cursor = db.execute("INSERT OR IGNORE INTO alerts(user_id,event_id,fingerprint,probability_sum,status,message) VALUES(?,?,?,?,?,?)", (user_id, event_id, fingerprint, calc["probability_sum"], status, message))
                    if cursor.rowcount:
                        email_data = (recipient, event, calc, fingerprint)
                        alert_response = {"created": True, "status": status, "probability_percent": calc["probability_percent"]}
            if email_data:
                recipient, event, calc, fingerprint = email_data
                selections = "\n".join(f"- {pick}: {quote['decimal_odds']} ({quote['bookmaker']})" for pick, quote in calc["best_odds"].items())
                body = (f"Alerta de cuotas para {event['name']}\nMercado: {event['market']}\n"
                        f"Suma de probabilidades implícitas: {calc['probability_percent']:.2f}%\n"
                        f"Underround teórico: {calc['underround_percent']:.2f}%\n\nCuotas usadas:\n{selections}\n\n"
                        "Verifica vigencia, disponibilidad, límites y costos antes de tomar cualquier decisión. El cálculo no garantiza resultados ni ganancias.")
                sent, message = send_alert_email(recipient, f"Alerta de cuotas ≤ 98%: {event['name']}", body)
                with DB_LOCK, connect_db() as db:
                    db.execute("UPDATE alerts SET status=?, message=? WHERE event_id=? AND fingerprint=?", ("sent" if sent else "pending", message, event["id"], fingerprint))
                alert_response = {"created": True, "status": "sent" if sent else "pending", "probability_percent": calc["probability_percent"], "message": message}
            reply(self, 201, {"ok": True, "alert": alert_response})
            return
        reply(self, 404, {"error": "No encontrado."})


if __name__ == "__main__":
    initialize_db()
    print(f"Cuotas MVP disponible en http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), App).serve_forever()
