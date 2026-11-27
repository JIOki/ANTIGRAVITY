"""Pruebas del reparto de apuestas (surebet)."""
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402
import stakes  # noqa: E402
from test_server import ServerBase  # noqa: E402


def odds(**values):
    return {name: {"decimal_odds": value, "bookmaker": f"Casa {name}"} for name, value in values.items()}


class StakeMathTest(unittest.TestCase):
    def test_two_way_example(self):
        plan = stakes.calculate_stakes(odds(A=2.10, B=2.05), ["A", "B"], 1000, 1)
        self.assertEqual([r["stake"] for r in plan["rows"]], [494, 506])
        self.assertEqual(plan["total_staked"], 1000)
        self.assertAlmostEqual(plan["min_profit"], 37.30)
        self.assertTrue(plan["guaranteed"])
        self.assertIn("anula", plan["warning"])

    def test_every_outcome_is_profitable_and_never_over_budget(self):
        cases = [(odds(A=2.10, B=2.05), 1000), (odds(A=3.2, B=3.6, C=3.4), 750), (odds(A=1.55, B=3.1), 333),
                 (odds(A=4.5, B=4.2, C=2.6), 2500), (odds(A=2.02, B=2.02), 5000)]
        for market, total in cases:
            names = list(market)
            for step in range(1, 11):
                plan = stakes.calculate_stakes(market, names, total, step)
                self.assertLessEqual(plan["total_staked"], total, (names, step))
                self.assertTrue(all(r["stake"] % step == 0 for r in plan["rows"]))
                if plan["guaranteed"]:
                    self.assertTrue(all(r["payout"] > plan["total_staked"] for r in plan["rows"]), (names, total, step))

    def test_rounding_choice_maximizes_guaranteed_profit(self):
        plan = stakes.calculate_stakes(odds(A=2.10, B=2.05), ["A", "B"], 1000, 10)
        # 490/500 garantiza $35; 490/510 solo $29.
        self.assertEqual([r["stake"] for r in plan["rows"]], [490, 500])
        self.assertAlmostEqual(plan["min_profit"], 35.0)

    def test_no_plan_without_surebet_or_incomplete(self):
        self.assertIsNone(stakes.calculate_stakes(odds(A=1.9, B=1.9), ["A", "B"], 1000, 1))
        self.assertIsNone(stakes.calculate_stakes(odds(A=2.5), ["A", "B"], 1000, 1))

    def test_small_amount_may_not_guarantee(self):
        plan = stakes.calculate_stakes(odds(A=1.5, B=3.2), ["A", "B"], 20, 10)
        self.assertTrue(plan["feasible"])
        self.assertFalse(plan["guaranteed"])
        self.assertIn("NO se garantiza", stakes.format_stakes_text(plan))

    def test_amount_too_small_for_rounding_is_never_exceeded(self):
        plan = stakes.calculate_stakes(odds(A=2.02, B=2.02), ["A", "B"], 10, 10)
        self.assertFalse(plan["feasible"])
        self.assertEqual((plan["rows"], plan["total_staked"]), ([], 0))
        self.assertIn("demasiado bajo", stakes.format_stakes_text(plan))

class StakeApiTest(ServerBase):
    def setup_surebet(self):
        self.register()
        event_id = self.create_event(("Tigres", "América"))
        self.add(event_id, "Tigres", "Casa 1", 2.10)
        status, data = self.add(event_id, "América", "Casa 2", 2.05)
        return event_id, data

    def test_events_include_stakes_with_user_settings(self):
        self.setup_surebet()
        status, _ = self.client.request("POST", "/api/profile", {"stake_amount": 2000, "rounding_step": 5})
        self.assertEqual(status, 200)
        _, data = self.client.request("GET", "/api/events")
        plan = data["events"][0]["stakes"]
        self.assertEqual((plan["total_requested"], plan["rounding_step"]), (2000, 5))
        self.assertEqual([r["bookmaker"] for r in plan["rows"]], ["Casa 1", "Casa 2"])
        self.assertEqual((data["stake_amount"], data["rounding_step"]), (2000, 5))
        self.assertIn("risk_warning", data)

    def test_custom_amount_endpoint(self):
        event_id, _ = self.setup_surebet()
        status, data = self.client.request("GET", f"/api/events/{event_id}/stakes?amount=500")
        self.assertEqual(status, 200)
        self.assertEqual(data["stakes"]["total_requested"], 500)
        self.assertEqual(self.client.request("GET", f"/api/events/{event_id}/stakes?amount=5")[0], 400)
        self.assertEqual(self.client.request("GET", f"/api/events/{event_id}/stakes?amount=abc")[0], 400)

    def test_settings_validation(self):
        self.register()
        for payload in ({"rounding_step": 0}, {"rounding_step": 11}, {"rounding_step": 50}, {"stake_amount": 5}, {"stake_amount": "x"}):
            self.assertEqual(self.client.request("POST", "/api/profile", payload)[0], 400, payload)
        _, me = self.client.request("GET", "/api/me")
        self.assertEqual((me["stake_amount"], me["rounding_step"], me["threshold_percent"]), (1000, 1, 98))

    def test_alert_email_contains_plan_and_warning(self):
        sent = {}

        def fake_send(recipient, subject, body):
            sent["body"] = body
            return True, "Correo enviado."

        with mock.patch.object(server, "send_alert_email", fake_send):
            _, data = self.setup_surebet()
        self.assertTrue(data["alert"]["created"])
        self.assertIn("Apuesta $494 a Tigres en Casa 1", sent["body"])
        self.assertIn("Ganancia mínima garantizada: $37.30", sent["body"])
        self.assertIn("anula una apuesta", sent["body"])

    def test_old_thresholds_migrated_to_max(self):
        self.register()
        with sqlite3.connect(server.DB_PATH) as db:
            db.execute("UPDATE users SET threshold=1.0")
        server.initialize_db()
        _, me = self.client.request("GET", "/api/me")
        self.assertEqual(me["threshold_percent"], 98)

    def test_assistant_context_includes_plan(self):
        self.setup_surebet()
        with sqlite3.connect(server.DB_PATH) as raw:
            user_id = raw.execute("SELECT id FROM users").fetchone()[0]
        with server.open_db() as db:
            context = server.assistant_user_context(db, user_id)
        self.assertIn("Apuesta $494 a Tigres en Casa 1", context)
        self.assertIn("Redondeo: $1", context)


class BatchCaptureTest(ServerBase):
    def test_batch_capture_reports_best(self):
        self.register()
        event_id = self.create_event(("América", "Empate", "Monterrey"))
        self.add(event_id, "América", "BetRivers", 2.28)
        status, data = self.client.request("POST", f"/api/events/{event_id}/odds", {
            "bookmaker": "Caliente",
            "quotes": [{"outcome": "América", "decimal_odds": 2.24}, {"outcome": "Empate", "decimal_odds": 3.5},
                       {"outcome": "Monterrey", "decimal_odds": 3.9}]})
        self.assertEqual(status, 201, data)
        saved = {item["outcome"]: item for item in data["saved"]}
        self.assertFalse(saved["América"]["is_best"])
        self.assertEqual((saved["América"]["best_bookmaker"], saved["América"]["best_odds"]), ("BetRivers", 2.28))
        self.assertTrue(saved["Monterrey"]["is_best"])
        self.assertAlmostEqual(data["probability_percent"], (1 / 2.28 + 1 / 3.5 + 1 / 3.9) * 100)

    def test_batch_validation(self):
        self.register()
        event_id = self.create_event(("A", "B"))
        url = f"/api/events/{event_id}/odds"
        cases = [
            {"bookmaker": "", "quotes": [{"outcome": "A", "decimal_odds": 2}]},
            {"bookmaker": "K", "quotes": []},
            {"bookmaker": "K", "quotes": [{"outcome": "A", "decimal_odds": 2}, {"outcome": "A", "decimal_odds": 3}]},
            {"bookmaker": "K", "quotes": [{"outcome": "Z", "decimal_odds": 2}]},
            {"bookmaker": "K", "quotes": [{"outcome": "A", "decimal_odds": 1}]},
            {"bookmaker": "K", "quotes": "x"},
        ]
        for payload in cases:
            self.assertEqual(self.client.request("POST", url, payload)[0], 400, payload)
        self.assertEqual(self.calc()["history"], [])  # nada se guardó a medias
