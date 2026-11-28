"""Pruebas de extremo a extremo del MVP. Ejecuta: python -m unittest discover tests"""
import http.client
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402


class Client:
    def __init__(self, port):
        self.port = port
        self.cookie = ""

    def request(self, method, path, payload=None, raw=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        all_headers = {"Content-Type": "application/json", **(headers or {})}
        if self.cookie:
            all_headers["Cookie"] = self.cookie
        conn.request(method, path, body=body, headers=all_headers)
        response = conn.getresponse()
        data = response.read()
        cookie = response.getheader("Set-Cookie")
        if cookie:
            self.cookie = cookie.split(";")[0]
        conn.close()
        return response.status, json.loads(data) if data else {}


class ServerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = mock.patch.object(server, "DB_PATH", Path(self.tmp.name) / "odds.db")
        self.db_patch.start()
        self.httpd = server.make_server("127.0.0.1", 0)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]
        self.client = Client(self.port)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.db_patch.stop()
        self.tmp.cleanup()

    def register(self, client=None, email="ana@example.com"):
        client = client or self.client
        status, _ = client.request("POST", "/api/register", {"email": email, "password": "contrasena-segura"})
        self.assertEqual(status, 200)
        return client

    def create_event(self, outcomes=("1", "X", "2")):
        status, data = self.client.request("POST", "/api/events", {"name": "A vs B", "market": "1X2", "outcomes": list(outcomes)})
        self.assertEqual(status, 201)
        return data["id"]

    def add(self, event_id, outcome, bookmaker, odds):
        return self.client.request("POST", f"/api/events/{event_id}/odds",
                                   {"outcome": outcome, "bookmaker": bookmaker, "decimal_odds": odds})

    def age_odds(self, minutes):
        with sqlite3.connect(server.DB_PATH) as db:
            old = server.iso(server.utcnow() - timedelta(minutes=minutes))
            db.execute("UPDATE odds SET observed_at=?, confirmed_at=?", (old, old))

    def calc(self, index=0):
        status, data = self.client.request("GET", "/api/events")
        self.assertEqual(status, 200)
        return data["events"][index]["calculation"]


class ServerTest(ServerBase):
    # 1. vigencia de cuotas
    def test_alert_and_calculation(self):
        self.register()
        event_id = self.create_event()
        self.add(event_id, "1", "K1", 3.2)
        self.add(event_id, "X", "K2", 3.6)
        status, data = self.add(event_id, "2", "K3", 3.4)
        self.assertEqual(status, 201)
        self.assertTrue(data["alert"]["created"])
        calc = self.calc()
        self.assertAlmostEqual(calc["probability_sum"], 1 / 3.2 + 1 / 3.6 + 1 / 3.4)
        self.assertTrue(calc["qualifies"])

    def test_stale_odds_are_ignored(self):
        self.register()
        event_id = self.create_event()
        for outcome, odds in (("1", 3.2), ("X", 3.6), ("2", 3.4)):
            self.add(event_id, outcome, "K1", odds)
        self.age_odds(120)  # más que la vigencia por defecto de 60 min
        calc = self.calc()
        self.assertFalse(calc["complete"])
        self.assertFalse(calc["qualifies"])
        self.assertEqual(sorted(calc["stale_outcomes"]), ["1", "2", "X"])
        self.assertEqual(calc["ignored_count"], 3)

    def test_latest_quote_per_bookmaker_replaces_older_one(self):
        self.register()
        event_id = self.create_event(("Sí", "No"))
        self.add(event_id, "Sí", "Casa", 2.5)       # cuota alta antigua
        self.add(event_id, "Sí", "casa ", 1.8)      # misma casa (mayúsculas/espacios distintos), cuota nueva
        self.add(event_id, "No", "Otra", 2.0)
        calc = self.calc()
        self.assertEqual(calc["best_odds"]["Sí"]["decimal_odds"], 1.8)
        self.assertAlmostEqual(calc["probability_sum"], 1 / 1.8 + 1 / 2.0)
        self.assertFalse(calc["qualifies"])

    # 2. sesiones persistentes
    def test_sessions_survive_restart_and_logout(self):
        self.register()
        cookie = self.client.cookie
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd = server.make_server("127.0.0.1", 0)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        client = Client(self.httpd.server_address[1])
        client.cookie = cookie
        self.assertEqual(client.request("GET", "/api/me")[0], 200)
        self.assertEqual(client.request("POST", "/api/logout", {})[0], 200)
        client.cookie = cookie
        self.assertEqual(client.request("GET", "/api/me")[0], 401)

    def test_expired_session_rejected(self):
        self.register()
        with sqlite3.connect(server.DB_PATH) as db:
            db.execute("UPDATE sessions SET expires_at=?", (server.iso(server.utcnow() - timedelta(seconds=1)),))
        self.assertEqual(self.client.request("GET", "/api/me")[0], 401)

    def test_token_not_stored_in_plaintext(self):
        self.register()
        token = self.client.cookie.split("=", 1)[1]
        with sqlite3.connect(server.DB_PATH) as db:
            stored = [row[0] for row in db.execute("SELECT token_hash FROM sessions")]
        self.assertNotIn(token, stored)

    # 3. límite de intentos y mensajes genéricos
    def test_login_rate_limit(self):
        self.register()
        other = Client(self.port)
        for _ in range(server.LOGIN_FAILS_PER_EMAIL):
            self.assertEqual(other.request("POST", "/api/login", {"email": "ana@example.com", "password": "incorrecta-123"})[0], 401)
        status, _ = other.request("POST", "/api/login", {"email": "ana@example.com", "password": "contrasena-segura"})
        self.assertEqual(status, 429)

    def test_rate_limit_uses_real_ip_behind_proxy(self):
        self.register()
        with mock.patch.object(server, "TRUST_PROXY", True):
            attacker = Client(self.port)
            for _ in range(server.LOGIN_FAILS_PER_IP):
                attacker.request("POST", "/api/login", {"email": f"x{_}@example.com", "password": "incorrecta-123"}, headers={"X-Real-IP": "203.0.113.9"})
            blocked, _ = attacker.request("POST", "/api/login", {"email": "ana@example.com", "password": "contrasena-segura"}, headers={"X-Real-IP": "203.0.113.9"})
            ok, _ = Client(self.port).request("POST", "/api/login", {"email": "ana@example.com", "password": "contrasena-segura"}, headers={"X-Real-IP": "198.51.100.7"})
        self.assertEqual((blocked, ok), (429, 200))

    def test_duplicate_register_is_generic(self):
        self.register()
        status, data = Client(self.port).request("POST", "/api/register", {"email": "ana@example.com", "password": "otra-contrasena"})
        self.assertEqual(status, 400)
        self.assertNotIn("existe", data["error"])

    # 4. Content-Length
    def test_invalid_content_length(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", "/api/login")
        conn.putheader("Content-Length", "-5")
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 400)
        conn.close()
        status, _ = self.client.request("POST", "/api/login", raw=b"x" * (server.MAX_BODY_BYTES + 1))
        self.assertEqual(status, 413)

    # 6. SMTP
    def test_smtp_without_starttls_does_not_send(self):
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        fake.has_extn.return_value = False
        env = {"SMTP_HOST": "smtp.local", "SMTP_FROM": "a@b.com", "SMTP_SECURITY": "starttls"}
        with mock.patch.dict("os.environ", env), mock.patch("smtplib.SMTP", return_value=fake):
            sent, message = server.send_alert_email("x@y.com", "s", "b")
        self.assertFalse(sent)
        self.assertIn("STARTTLS", message)
        fake.send_message.assert_not_called()

    # 7. editar, borrar y ajustes
    def test_edit_and_delete(self):
        self.register()
        event_id = self.create_event()
        self.add(event_id, "X", "K1", 3.0)
        status, data = self.client.request("PUT", f"/api/events/{event_id}", {"name": "A vs B", "market": "Ganador", "outcomes": ["1", "2"]})
        self.assertEqual((status, data["removed_odds"]), (200, 1))
        self.add(event_id, "1", "K1", 2.0)
        odds_id = self.calc()["history"][0]["id"]
        self.assertEqual(self.client.request("DELETE", f"/api/events/{event_id}/odds/{odds_id}")[0], 200)
        self.assertEqual(self.calc()["history"], [])
        self.assertEqual(self.client.request("DELETE", f"/api/events/{event_id}")[0], 200)
        self.assertEqual(self.client.request("GET", "/api/events")[1]["events"], [])

    def test_other_user_cannot_touch_event(self):
        self.register()
        event_id = self.create_event()
        intruder = self.register(Client(self.port), "beto@example.com")
        self.assertEqual(intruder.request("DELETE", f"/api/events/{event_id}")[0], 404)
        self.assertEqual(intruder.request("PUT", f"/api/events/{event_id}", {"name": "x", "market": "y", "outcomes": ["a", "b"]})[0], 404)
        self.assertEqual(intruder.request("POST", f"/api/events/{event_id}/odds", {"outcome": "1", "bookmaker": "K", "decimal_odds": 2})[0], 404)

    def test_threshold_and_max_age_settings(self):
        self.register()
        event_id = self.create_event(("Sí", "No"))
        self.add(event_id, "Sí", "K1", 2.06)
        self.add(event_id, "No", "K2", 2.06)  # suma = 97.09%
        self.assertTrue(self.calc()["qualifies"])  # umbral por defecto 98%
        status, _ = self.client.request("POST", "/api/profile", {"threshold_percent": 95, "max_age_minutes": 5})
        self.assertEqual(status, 200)
        self.assertFalse(self.calc()["qualifies"])
        self.age_odds(10)
        self.assertFalse(self.calc()["complete"])
        # El umbral máximo es 98%: deja 2% de margen de seguridad.
        self.assertEqual(self.client.request("POST", "/api/profile", {"threshold_percent": 98})[0], 200)
        self.assertEqual(self.client.request("POST", "/api/profile", {"threshold_percent": 99})[0], 400)
        self.assertEqual(self.client.request("POST", "/api/profile", {"threshold_percent": 120})[0], 400)
        self.assertEqual(self.client.request("POST", "/api/profile", {"max_age_minutes": 0})[0], 400)

if __name__ == "__main__":
    unittest.main()
