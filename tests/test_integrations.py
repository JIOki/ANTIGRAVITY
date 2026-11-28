"""Pruebas de la integración con proveedores de cuotas (sin llamadas reales a internet)."""
import io
import json
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import providers  # noqa: E402
import server  # noqa: E402
from test_server import Client, ServerBase  # noqa: E402


def sample_payload(start_iso, draw_price=3.6):
    """Respuesta con el formato de /v4/sports/{sport}/odds de The Odds API."""
    return [{
        "id": "evt123",
        "sport_key": "soccer_mexico_ligamx",
        "sport_title": "Liga MX",
        "commence_time": start_iso,
        "home_team": "Tigres",
        "away_team": "América",
        "bookmakers": [
            {"key": "casa1", "title": "Casa Uno", "last_update": start_iso, "markets": [
                {"key": "h2h", "outcomes": [{"name": "Tigres", "price": 3.2}, {"name": "América", "price": 2.1}, {"name": "Draw", "price": 3.3}]}]},
            {"key": "casa2", "title": "Casa Dos", "markets": [
                {"key": "h2h", "outcomes": [{"name": "Tigres", "price": 2.0}, {"name": "América", "price": 3.4}, {"name": "Draw", "price": draw_price}]}]},
            # Mercado sin empate: no es comparable con el 1X2 y debe descartarse.
            {"key": "casa3", "title": "Casa Tres", "markets": [
                {"key": "h2h", "outcomes": [{"name": "Tigres", "price": 9.0}, {"name": "América", "price": 9.0}]}]},
        ],
    }, {
        "id": "ya-empezo", "sport_title": "Liga MX", "commence_time": "2020-01-01T00:00:00Z",
        "home_team": "A", "away_team": "B", "bookmakers": [
            {"key": "x", "title": "X", "markets": [{"key": "h2h", "outcomes": [{"name": "A", "price": 5}, {"name": "B", "price": 5}]}]}],
    }]


class FakeResponse(io.BytesIO):
    def __init__(self, payload, headers):
        super().__init__(json.dumps(payload).encode())
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ParserTest(ServerBase):
    def test_parser_keeps_consistent_market(self):
        event = providers.TheOddsAPI._parse_event(sample_payload("2030-01-01T00:00:00Z")[0])
        self.assertEqual(event.outcomes, ["Tigres", "América", "Empate"])
        self.assertEqual({q.bookmaker for q in event.quotes}, {"Casa Uno", "Casa Dos"})
        self.assertEqual(len(event.quotes), 6)

    def test_fetch_odds_reads_quota_headers_and_never_leaks_key(self):
        api = providers.TheOddsAPI()
        captured = {}

        def fake_urlopen(request, timeout, context):
            captured["url"] = request.full_url
            return FakeResponse(sample_payload("2030-01-01T00:00:00Z"), {"x-requests-remaining": "480", "x-requests-used": "20", "x-requests-last": "2"})

        with mock.patch.dict("os.environ", {"ODDS_API_KEY": "secreta"}), mock.patch.object(providers, "urlopen", fake_urlopen):
            result = api.fetch_odds("soccer_mexico_ligamx", ["us", "eu"], 72)
        self.assertIn("regions=us%2Ceu", captured["url"])
        self.assertIn("oddsFormat=decimal", captured["url"])
        self.assertEqual((result.requests_remaining, result.requests_used, result.requests_last), (480, 20, 2))
        self.assertEqual(len(result.events), 2)

    def test_missing_key_is_reported(self):
        with mock.patch.dict("os.environ", {"ODDS_API_KEY": ""}):
            with self.assertRaises(providers.ProviderError):
                providers.TheOddsAPI().fetch_odds("soccer_epl", ["eu"], 72)


class SyncTest(ServerBase):
    def setUp(self):
        super().setUp()
        server.LAST_MANUAL_SYNC.clear()
        self.start = server.iso(server.utcnow() + timedelta(days=1))
        self.payload = sample_payload(self.start)
        self.fetch_calls = []

        def fake_fetch(sport_key, regions, horizon):
            self.fetch_calls.append((sport_key, tuple(regions)))
            events = [e for e in (providers.TheOddsAPI._parse_event(i) for i in self.payload) if e]
            return providers.FetchResult(events, requests_remaining=400, requests_used=100)

        self.fetch_patch = mock.patch.object(server.PROVIDERS["the-odds-api"], "fetch_odds", side_effect=fake_fetch)
        self.fetch_patch.start()

    def tearDown(self):
        self.fetch_patch.stop()
        super().tearDown()

    def subscribe(self, client=None, regions=("eu",)):
        client = client or self.client
        status, data = client.request("POST", "/api/integrations/subscriptions",
                                      {"provider": "the-odds-api", "sport_key": "soccer_mexico_ligamx", "sport_title": "Liga MX", "regions": list(regions)})
        self.assertEqual(status, 201, data)
        return data["id"]

    def test_sync_imports_events_quotes_and_alerts(self):
        self.register()
        self.subscribe()
        status, summary = self.client.request("POST", "/api/integrations/sync", {})
        self.assertEqual(status, 200, summary)
        self.assertEqual((summary["calls"], summary["events"], summary["alerts"]), (1, 1, 1))  # el evento ya iniciado se omite
        events = self.client.request("GET", "/api/events")[1]["events"]
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual((event["source"], event["name"], event["outcomes"]), ("the-odds-api", "Tigres vs América", ["Tigres", "América", "Empate"]))
        calc = event["calculation"]
        # Mejores: Tigres 3.2 (Uno), América 3.4 (Dos), Empate 3.6 (Dos)
        self.assertAlmostEqual(calc["probability_sum"], 1 / 3.2 + 1 / 3.4 + 1 / 3.6)
        self.assertTrue(calc["qualifies"])
        self.assertEqual(calc["best_odds"]["Empate"]["source"], "the-odds-api")
        info = self.client.request("GET", "/api/integrations")[1]
        self.assertEqual(info["providers"][0]["status"]["requests_remaining"], 400)
        self.assertGreater(info["estimated_monthly_credits"], 0)

    def test_resync_refreshes_without_duplicating_and_records_changes(self):
        self.register()
        self.subscribe()
        server.run_sync()
        with sqlite3.connect(server.DB_PATH) as db:
            first = db.execute("SELECT COUNT(*) FROM odds").fetchone()[0]
            old = server.iso(server.utcnow() - timedelta(minutes=50))
            db.execute("UPDATE odds SET observed_at=?, confirmed_at=?", (old, old))
        self.payload = sample_payload(self.start, draw_price=3.0)  # cambia una cuota
        server.run_sync()
        with sqlite3.connect(server.DB_PATH) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM odds").fetchone()[0], first + 1)
        calc = self.client.request("GET", "/api/events")[1]["events"][0]["calculation"]
        self.assertTrue(all(q["age_minutes"] < 1 for q in calc["best_odds"].values()))  # confirmadas de nuevo
        self.assertEqual(calc["best_odds"]["Empate"]["decimal_odds"], 3.3)

    def test_same_subscription_is_one_call_and_users_are_isolated(self):
        self.register()
        self.subscribe()
        other = self.register(Client(self.port), "beto@example.com")
        self.subscribe(other)
        summary = server.run_sync()
        self.assertEqual(len(self.fetch_calls), 1)
        self.assertEqual(summary["events"], 2)  # un evento para cada usuario
        mine = self.client.request("GET", "/api/events")[1]["events"]
        theirs = other.request("GET", "/api/events")[1]["events"]
        self.assertNotEqual(mine[0]["id"], theirs[0]["id"])
        sub_id = self.client.request("GET", "/api/integrations")[1]["subscriptions"][0]["id"]
        self.assertEqual(other.request("DELETE", f"/api/integrations/subscriptions/{sub_id}")[0], 404)

    def test_deleted_api_event_is_not_recreated(self):
        self.register()
        self.subscribe()
        server.run_sync()
        event_id = self.client.request("GET", "/api/events")[1]["events"][0]["id"]
        self.assertEqual(self.client.request("DELETE", f"/api/events/{event_id}")[0], 200)
        server.run_sync()
        self.assertEqual(self.client.request("GET", "/api/events")[1]["events"], [])

    def test_api_event_outcomes_cannot_be_edited(self):
        self.register()
        self.subscribe()
        server.run_sync()
        event_id = self.client.request("GET", "/api/events")[1]["events"][0]["id"]
        status, _ = self.client.request("PUT", f"/api/events/{event_id}", {"name": "x", "market": "y", "outcomes": ["Tigres", "América"]})
        self.assertEqual(status, 400)

    def test_manual_sync_cooldown_and_validation(self):
        self.register()
        self.assertEqual(self.client.request("POST", "/api/integrations/subscriptions",
                                             {"provider": "the-odds-api", "sport_key": "x y", "regions": ["eu"]})[0], 400)
        self.assertEqual(self.client.request("POST", "/api/integrations/subscriptions",
                                             {"provider": "the-odds-api", "sport_key": "soccer_epl", "regions": ["mx"]})[0], 400)
        self.subscribe()
        self.assertEqual(self.client.request("POST", "/api/integrations/sync", {})[0], 200)
        self.assertEqual(self.client.request("POST", "/api/integrations/sync", {})[0], 429)

    def test_provider_error_is_recorded(self):
        self.register()
        self.subscribe()
        server.PROVIDERS["the-odds-api"].fetch_odds.side_effect = providers.ProviderError("clave inválida")
        summary = server.run_sync()
        self.assertEqual(summary["errors"], ["soccer_mexico_ligamx: clave inválida"])
        sub = self.client.request("GET", "/api/integrations")[1]["subscriptions"][0]
        self.assertEqual(sub["last_error"], "clave inválida")


class SmartSyncTest(SyncTest):
    def test_two_fetches_per_game_day_and_estimate(self):
        self.register()
        self.subscribe()
        now = server.utcnow()
        first = now + timedelta(hours=11)  # primer partido dentro de 12 h
        cal = [{"id": "a", "commence_time": server.iso(first)}, {"id": "b", "commence_time": server.iso(first + timedelta(hours=2))},
               {"id": "c", "commence_time": server.iso(first + timedelta(days=2))}]
        with mock.patch.object(server.PROVIDERS["the-odds-api"], "list_events", return_value=cal, create=True):
            server.refresh_calendar(now)
        self.assertEqual(server.smart_tick(now)[0].split(":")[0], "soccer_mexico_ligamx")  # consulta temprana
        self.assertEqual(server.smart_tick(now + timedelta(minutes=5)), [])                 # no repite
        self.assertEqual(server.smart_tick(first - timedelta(minutes=70)), [])               # aún no toca la previa
        self.assertEqual(len(server.smart_tick(first - timedelta(minutes=55))), 1)           # consulta previa
        self.assertEqual(server.smart_tick(first - timedelta(minutes=50)), [])
        self.assertEqual(len(self.fetch_calls), 2)
        with server.open_db() as db:
            # 2 días con partido en el calendario de 7 días → 2*30/7 días/mes × 2 consultas × 1 crédito
            self.assertEqual(server.estimated_monthly_credits(db), round(2 * 30 / 7 * 2))
        info = self.client.request("GET", "/api/integrations")[1]
        self.assertEqual(info["mode"], "smart")
