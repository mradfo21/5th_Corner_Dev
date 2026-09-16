"""Account + hosted usage wallet — Cursor-style billing."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import billing


class BillingCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "billing.json"
        billing.set_store_path(self.path)
        self.addCleanup(billing.set_store_path, None)
        billing._local_app = False

    def test_default_is_unlinked_free(self):
        status = billing.public_status()
        self.assertFalse(status["account"]["linked"])
        self.assertEqual(status["plan"]["id"], "free")
        self.assertEqual(status["wallet_usd"], 0.0)
        self.assertTrue(status["on_demand"])

    def test_link_and_unlink_email(self):
        billing.link_email("You@Example.com")
        acct = billing.current_account()
        self.assertEqual(acct["email"], "you@example.com")
        billing.unlink()
        self.assertFalse(billing.public_status()["account"]["linked"])

    def test_bad_email_rejected(self):
        with self.assertRaises(ValueError):
            billing.link_email("not-an-email")

    def test_debit_burns_included_then_wallet(self):
        billing.link_email("p@example.com")
        acct = billing.current_account()
        acct["plan"] = "play"
        acct["included_usd"] = 10.0
        acct["wallet_usd"] = 5.0
        billing._save_account(acct)
        billing.debit(12.0)
        row = billing.current_account()
        self.assertAlmostEqual(row["included_used_usd"], 10.0)
        self.assertAlmostEqual(row["wallet_usd"], 3.0)
        self.assertAlmostEqual(row["billed_usd"], 12.0)

    def test_available_respects_on_demand(self):
        billing.link_email("p@example.com")
        acct = billing.current_account()
        acct["included_usd"] = 0.0
        acct["wallet_usd"] = 8.0
        acct["on_demand"] = False
        billing._save_account(acct)
        self.assertEqual(billing.available_usd(), 0.0)
        billing.set_on_demand(True)
        self.assertAlmostEqual(billing.available_usd(), 8.0)

    def test_account_cap_blocks(self):
        billing.link_email("p@example.com")
        billing.set_monthly_cap_usd(5)
        acct = billing.current_account()
        acct["billed_usd"] = 5.0
        billing._save_account(acct)
        self.assertTrue(billing.over_account_cap())
        with patch.object(billing, "requires_wallet", return_value=True):
            blocked = billing.gate()
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked["reason"], "spend_limit")

    def test_gate_needs_account_when_hosted(self):
        with patch.object(billing, "is_payments_enabled", return_value=True):
            with patch.object(billing, "requires_wallet", return_value=True):
                blocked = billing.gate()
        self.assertEqual(blocked["reason"], "needs_account")

    def test_gate_empty_wallet(self):
        billing.link_email("p@example.com")
        with patch.object(billing, "requires_wallet", return_value=True):
            blocked = billing.gate()
        self.assertEqual(blocked["reason"], "empty_wallet")

    def test_cookie_roundtrip(self):
        token = billing.cookie_value("a@b.co")
        self.assertEqual(billing.email_from_cookie(token), "a@b.co")
        self.assertIsNone(billing.email_from_cookie(token + "x"))

    def test_pack_usual_fallback(self):
        usual = billing.pack_by_id("nope")
        self.assertEqual(usual["id"], "twentyfive")
        self.assertEqual(billing.pack_by_id("fifty")["credit_usd"], 57.5)

    def test_local_app_never_requires_wallet(self):
        billing.mark_local_app()
        with patch.object(billing, "is_payments_enabled", return_value=True):
            with patch.dict("os.environ", {"FEATURE_BILLING": "1"}, clear=False):
                self.assertFalse(billing.requires_wallet())

    def test_feature_flag_gates_hosted_wallet(self):
        with patch.object(billing, "is_payments_enabled", return_value=True):
            self.assertFalse(billing.requires_wallet())
            with patch.dict("os.environ", {"FEATURE_BILLING": "1"}, clear=False):
                self.assertTrue(billing.requires_wallet())

    def test_host_pays_never_requires_wallet(self):
        with patch.dict("os.environ", {"BILLING_HOST_PAYS": "1"}, clear=False):
            with patch.object(billing, "is_payments_enabled", return_value=True):
                self.assertFalse(billing.requires_wallet())

    def test_gate_hold_refuses_when_estimate_exceeds_wallet(self):
        billing.link_email("p@example.com")
        acct = billing.current_account()
        acct["wallet_usd"] = 1.0
        billing._save_account(acct)
        with patch.object(billing, "requires_wallet", return_value=True):
            blocked = billing.gate(min_usd=2.0)
        self.assertEqual(blocked["reason"], "insufficient_balance")
        with patch.object(billing, "requires_wallet", return_value=True):
            self.assertIsNone(billing.gate(min_usd=0.5))

    def test_invoice_paid_refreshes_play_included(self):
        billing.link_email("p@example.com")
        acct = billing.current_account()
        acct["plan"] = "play"
        acct["stripe_customer_id"] = "cus_test"
        acct["included_usd"] = 10.0
        acct["included_used_usd"] = 9.5
        billing._save_account(acct)
        out = billing.apply_invoice_paid({"customer": "cus_test"})
        self.assertTrue(out["ok"])
        row = billing.current_account()
        self.assertAlmostEqual(row["included_used_usd"], 0.0)
        self.assertAlmostEqual(row["included_usd"], 10.0)

    def test_grant_wallet_enables_on_demand(self):
        billing.link_email("p@example.com")
        billing.set_on_demand(False)
        billing.grant_wallet(10.0, source="test", checkout_session_id="cs_test")
        acct = billing.current_account()
        self.assertTrue(acct["on_demand"])
        self.assertAlmostEqual(acct["wallet_usd"], 10.0)
        self.assertIn("cs_test", acct["redeemed"])


class BillingApiCase(unittest.TestCase):
    def setUp(self):
        import api
        self.api = api
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        billing.set_store_path(Path(self._tmp.name) / "billing.json")
        self.addCleanup(billing.set_store_path, None)
        billing._local_app = False
        self.client = api.app.test_client()

    def test_usage_includes_account_and_packs(self):
        resp = self.client.get("/api/usage")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("account", data)
        self.assertFalse(data["account"]["linked"])
        self.assertIn("plans", data)
        self.assertGreaterEqual(len(data["packs"]), 3)
        self.assertIn("on_demand", data)

    def test_link_and_unlink_round_trip(self):
        resp = self.client.post("/api/billing/account", json={"email": "pay@example.com"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["account"]["linked"])
        self.assertEqual(data["account"]["email"], "pay@example.com")
        cookie = resp.headers.get("Set-Cookie", "")
        self.assertIn(billing.COOKIE, cookie)
        gone = self.client.delete("/api/billing/account")
        self.assertEqual(gone.status_code, 200)
        self.assertFalse(gone.get_json()["account"]["linked"])

    def test_on_demand_requires_account(self):
        resp = self.client.put("/api/usage", json={"on_demand": False})
        self.assertEqual(resp.status_code, 400)

    def test_hosted_billing_refuses_open_admin(self):
        saved = os.environ.get("ADMIN_TOKEN")
        try:
            os.environ.pop("ADMIN_TOKEN", None)
            with patch.dict("os.environ", {"FEATURE_BILLING": "1"}, clear=False):
                self.assertFalse(self.api._admin_token_ok())
            os.environ.pop("FEATURE_BILLING", None)
            self.assertTrue(self.api._admin_token_ok())
        finally:
            if saved is None:
                os.environ.pop("ADMIN_TOKEN", None)
            else:
                os.environ["ADMIN_TOKEN"] = saved


class CoinopHostedCase(unittest.TestCase):
    def test_cabinet_stays_off_hosted_saas(self):
        import coinop
        with patch.dict(
            "os.environ",
            {"FEATURE_BILLING": "1", "FEATURE_COINOP": "1", "COINOP_ON_HOSTED": ""},
            clear=False,
        ):
            self.assertFalse(coinop.is_enabled())


if __name__ == "__main__":
    unittest.main()
