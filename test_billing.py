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
        key = billing.new_key()
        token = billing.cookie_value(key)
        self.assertEqual(billing.key_from_cookie(token), key)
        self.assertIsNone(billing.key_from_cookie(token + "x"))
        self.assertIsNone(billing.key_from_cookie("a@b.co|" + token.split("|")[1]))
        with self.assertRaises(ValueError):
            billing.cookie_value("a@b.co")

    def test_charge_goes_negative_rather_than_lose_a_cost(self):
        billing.link_email("p@example.com")
        key = billing.current_account()["key"]
        acct = billing.current_account()
        acct["wallet_usd"] = 0.10
        billing._save_account(acct)
        billing._tl.key = key
        try:
            with patch.object(billing, "requires_wallet", return_value=True), \
                    patch.dict("os.environ", {"BILLING_MARKUP": "2"}, clear=False):
                out = billing.charge_cost(0.25, "sess-1")
        finally:
            billing._tl.key = None
        self.assertEqual(out["wallet"], key)
        self.assertAlmostEqual(out["charged_usd"], 0.5)
        self.assertAlmostEqual(billing.current_account()["wallet_usd"], -0.4)
        self.assertEqual(billing.available_usd(), 0.0)

    def test_live_time_meter_charges_what_the_browser_left_out(self):
        from flask import Flask
        app = Flask("meter")
        key = billing.new_key()
        hdr = {"Cookie": f"{billing.COOKIE}={billing.cookie_value(key)}"}
        with patch.object(billing, "requires_wallet", return_value=True), \
                patch.object(billing, "_ensure_reaper"):
            with app.test_request_context("/api/reactor/token", headers=hdr):
                billing.meter_start("reactor", "s1", service_type="video", provider="reactor", model="m")
            billing._METERS[(key, "reactor", "s1")]["start"] -= 100
            with app.test_request_context("/api/reactor/usage", headers=hdr):
                self.assertAlmostEqual(billing.meter_stop("reactor", "s1", 40), 60, delta=1)
                self.assertEqual(billing.meter_stop("reactor", "s1", 40), 0.0)  # closed
            # A browser that vanished: closed at its last poll and charged.
            with app.test_request_context("/api/talk/session", headers=hdr):
                billing.meter_start("talk", "s1", service_type="voice", provider="elevenlabs", model="talk_agent")
            now = billing._METERS[(key, "talk", "s1")]["start"]
            billing._LAST_SEEN[key] = now + 30
            with patch("cost_tracker.record_usage") as rec:
                self.assertEqual(billing._reap_once(now + 30 + billing.REAP_AFTER_S + 1), 1)
            self.assertAlmostEqual(rec.call_args.kwargs["output_units"], 30, delta=0.5)

    def test_rate_card_is_the_price_a_charge_uses(self):
        import pricing
        with patch.object(billing, "requires_wallet", return_value=True), \
                patch.dict("os.environ", {"BILLING_MARKUP": "2"}, clear=False):
            card = billing.rate_card()
        row = next(r for r in card["rows"] if r["thing"] == "Sharp picture")
        cost = pricing.estimate_cost("gemini", "gemini-3.1-flash-image", "images", None, 1, size="2K")
        self.assertAlmostEqual(row["provider_usd"], round(cost, 4))
        self.assertAlmostEqual(row["price_usd"], round(cost * 2, 4))
        self.assertEqual(card["markup"], 2.0)
        self.assertTrue(card["checked"])

    def test_owner_wallet_plays_free(self):
        billing.link_email("owner@example.com")
        key = billing.current_account()["key"]
        billing.set_unlimited(True)
        with patch.object(billing, "requires_wallet", return_value=True):
            self.assertIsNone(billing.gate(min_usd=5.0))
            billing._tl.key = key
            try:
                out = billing.charge_cost(1.0, "s")
            finally:
                billing._tl.key = None
            self.assertEqual(out["charged_usd"], 0.0)
            self.assertTrue(billing.public_status()["unlimited"])
        self.assertAlmostEqual(float(billing.current_account()["wallet_usd"]), 0.0)
        billing.set_unlimited(False)
        with patch.object(billing, "requires_wallet", return_value=True):
            self.assertEqual(billing.gate()["reason"], "empty_wallet")

    def test_settle_session_no_longer_charges(self):
        with patch.object(billing, "requires_wallet", return_value=True):
            self.assertIsNone(billing.settle_session("s", 0.0))

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
        out = billing.apply_invoice_paid({"customer": "cus_test", "subscription": "sub_test"})
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

    def test_receipt_email_stays_on_this_wallet(self):
        resp = self.client.post("/api/billing/account", json={"email": "pay@example.com"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["account"]["linked"])
        self.assertEqual(data["account"]["email"], "pay@example.com")
        cookie = resp.headers.get("Set-Cookie", "")
        self.assertIn(billing.COOKIE + "=w_", cookie)
        self.assertIn("HttpOnly", cookie)
        gone = self.client.delete("/api/billing/account")
        self.assertEqual(gone.status_code, 200)
        # Forgetting the email never throws the wallet away.
        self.assertNotIn(billing.COOKIE + "=;", gone.headers.get("Set-Cookie", ""))
        again = self.client.get("/api/usage").get_json()
        self.assertTrue(again["account"]["linked"])
        self.assertIsNone(again["account"]["email"])

    def test_one_wallet_per_browser(self):
        with patch.object(billing, "requires_wallet", return_value=True):
            a = self.api.app.test_client()
            b = self.api.app.test_client()
            a.get("/api/usage")
            b.get("/api/usage")
            ka = billing.key_from_cookie(a.get_cookie(billing.COOKIE).value)
            kb = billing.key_from_cookie(b.get_cookie(billing.COOKIE).value)
            self.assertTrue(ka and kb and ka != kb)
            # Someone else's email is no way into their wallet any more.
            a.post("/api/billing/account", json={"email": "victim@example.com"})
            b.post("/api/billing/account", json={"email": "victim@example.com"})
            self.assertEqual(billing.key_from_cookie(b.get_cookie(billing.COOKIE).value), kb)
            # A forged cookie is not a wallet: the browser gets a fresh one.
            c = self.api.app.test_client()
            c.set_cookie(billing.COOKIE, ka + "|" + "0" * 32)
            c.get("/api/usage")
            kc = billing.key_from_cookie(c.get_cookie(billing.COOKIE).value)
            self.assertTrue(kc and kc not in (ka, kb))

    def test_no_store_wide_wallet_on_a_request(self):
        billing.link_email("desk@example.com")  # the store's active wallet
        with patch.object(billing, "requires_wallet", return_value=True):
            data = self.client.get("/api/usage").get_json()
        self.assertIsNone(data["account"]["email"])

    def test_a_render_thread_charges_the_wallet_that_asked(self):
        import threading
        seen = {}
        with patch.object(billing, "requires_wallet", return_value=True):
            a = self.api.app.test_client()
            a.get("/api/usage")
            ka = billing.key_from_cookie(a.get_cookie(billing.COOKIE).value)
            with self.api.app.test_request_context(
                    "/api/feed", headers={"Cookie": f"{billing.COOKIE}={billing.cookie_value(ka)}"}):
                t = threading.Thread(target=lambda: seen.update(out=billing.charge_cost(0.01, "sess-a")))
            t.start()
            t.join()
            # …and a thread nobody's request started charges nobody.
            u = threading.Thread(target=lambda: seen.update(orphan=billing.charge_cost(0.01, "sess-x")))
            u.start()
            u.join()
        self.assertEqual(seen["out"]["wallet"], ka)
        self.assertIsNone(seen["orphan"])

    def test_settings_land_on_this_browsers_wallet(self):
        resp = self.client.put("/api/usage", json={"on_demand": False})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()["on_demand"])
        self.assertIn(billing.COOKIE + "=w_", resp.headers.get("Set-Cookie", ""))

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


class StripeCheckoutCase(unittest.TestCase):
    """The top-up path against real stripe-python objects (15.x objects are
    not dicts — the reason both credit paths used to fail)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        billing.set_store_path(Path(self._tmp.name) / "billing.json")
        self.addCleanup(billing.set_store_path, None)
        billing._local_app = False

    PAYER = "w_" + "a" * 32

    def _session(self, **over):
        import stripe
        data = {
            "id": "cs_test_abc", "object": "checkout.session", "status": "complete",
            "payment_status": "paid", "amount_total": 1000, "customer": "cus_payer",
            "invoice": "in_123", "customer_details": {"email": "payer@example.com"},
            "metadata": {"purpose": "pack", "pack": "ten", "account": self.PAYER},
        }
        data.update(over)
        return stripe.checkout.Session.construct_from(data, "sk_test_x")

    def _wallet(self, email):
        return float(billing._read()["accounts"].get(email, {}).get("wallet_usd") or 0.0)

    def test_plain_reads_a_stripe_object(self):
        cs = self._session()
        with self.assertRaises(TypeError):
            dict(cs.metadata)  # the old redeem's failure, on this library
        md = billing._plain(billing._plain(cs).get("metadata"))
        self.assertEqual(md["pack"], "ten")

    def test_credits_the_account_that_paid_not_the_browser(self):
        billing.link_email("someone-else@example.com")  # this browser's account
        mine = billing.current_account()["key"]
        out = billing.fulfill_checkout(self._session())
        self.assertTrue(out["ok"])
        self.assertEqual(out["account"], self.PAYER)
        self.assertAlmostEqual(self._wallet(self.PAYER), 10.0)
        self.assertAlmostEqual(self._wallet(mine), 0.0)
        row = billing._read()["accounts"][self.PAYER]
        self.assertEqual(row["stripe_customer_id"], "cus_payer")
        self.assertEqual(row["payments"][0]["invoice"], "in_123")
        self.assertEqual(row["email"], "payer@example.com")  # receipt address

    def test_legacy_email_session_still_lands(self):
        md = {"purpose": "pack", "pack": "ten", "email": "old@example.com"}
        self.assertTrue(billing.fulfill_checkout(self._session(id="cs_test_old", metadata=md))["ok"])
        self.assertAlmostEqual(self._wallet("old@example.com"), 10.0)

    def test_webhook_and_return_credit_once(self):
        self.assertFalse(billing.fulfill_checkout(self._session())["already_redeemed"])
        self.assertTrue(billing.fulfill_checkout(self._session())["already_redeemed"])
        self.assertAlmostEqual(self._wallet(self.PAYER), 10.0)

    def test_delayed_payment_waits(self):
        out = billing.fulfill_checkout(self._session(payment_status="unpaid"))
        self.assertEqual(out["reason"], "processing")
        self.assertAlmostEqual(self._wallet(self.PAYER), 0.0)
        # …and lands when async_payment_succeeded brings it back paid.
        self.assertTrue(billing.fulfill_checkout(self._session())["ok"])
        self.assertAlmostEqual(self._wallet(self.PAYER), 10.0)

    def test_top_up_invoice_is_not_a_plan(self):
        billing.fulfill_checkout(self._session())
        out = billing.apply_invoice_paid({"customer": "cus_payer"})
        self.assertEqual(out.get("ignored"), "not_a_subscription_invoice")
        self.assertEqual(billing._read()["accounts"][self.PAYER]["plan"], "free")

    def test_return_path_stays_in_the_game(self):
        self.assertEqual(billing._safe_return_path("/play?session=abc&billing=success&cs=cs_1"), "/play?session=abc")
        for bad in ("https://evil.example/x", "//evil.example", "/play\\..\\x", "/admin", "", None):
            self.assertEqual(billing._safe_return_path(bad), "/standalone", bad)

    def test_checkout_request(self):
        seen = {}

        class _Sessions:
            @staticmethod
            def create(**kw):
                seen.update(kw)
                return type("CS", (), {"id": "cs_test_new", "url": "https://checkout.stripe.com/x"})()

        fake = type("S", (), {"checkout": type("C", (), {"Session": _Sessions})})
        req = type("R", (), {"host_url": "http://127.0.0.1:5188/"})()
        billing.link_email("payer@example.com")
        with patch.object(billing, "is_payments_enabled", return_value=True), \
                patch.object(billing, "_stripe_client", return_value=fake):
            out = billing.create_checkout("pack", req, pack_id="ten", return_to="/play?session=abc")
            with self.assertRaises(ValueError):
                billing.create_checkout("play", req)
        self.assertEqual(out["checkout_session_id"], "cs_test_new")
        self.assertNotIn("payment_method_types", seen)
        self.assertTrue(seen["invoice_creation"]["enabled"])
        self.assertEqual(seen["customer_creation"], "always")
        # Embedded in the sheet by default; a bank redirect comes back here.
        self.assertEqual(seen["ui_mode"], "embedded_page")
        self.assertNotIn("success_url", seen)
        self.assertTrue(seen["return_url"].startswith("http://127.0.0.1:5188/play?session=abc&billing=success&cs="))
        self.assertEqual(out["ui_mode"], "embedded_page")
        self.assertEqual(seen["metadata"]["account"], billing.current_account()["key"])
        self.assertEqual(seen["client_reference_id"], seen["metadata"]["account"])
        self.assertEqual(seen["customer_email"], "payer@example.com")
        # Managed Payments refuses a product without a tax code.
        self.assertEqual(seen["line_items"][0]["price_data"]["product_data"]["tax_code"], "txcd_10201003")

        seen.clear()
        with patch.dict(os.environ, {"STRIPE_CHECKOUT_UI": "hosted_page", "STRIPE_TAX_CODE": "txcd_10201001"}), \
                patch.object(billing, "is_payments_enabled", return_value=True), \
                patch.object(billing, "_stripe_client", return_value=fake):
            out = billing.create_checkout("pack", req, pack_id="ten", return_to="/play?session=abc")
        self.assertEqual(seen["ui_mode"], "hosted_page")
        self.assertNotIn("return_url", seen)
        self.assertTrue(seen["success_url"].startswith("http://127.0.0.1:5188/play?session=abc&billing=success&cs="))
        self.assertTrue(seen["cancel_url"].endswith("billing=cancel"))
        self.assertEqual(seen["line_items"][0]["price_data"]["product_data"]["tax_code"], "txcd_10201001")
        self.assertEqual(out["url"], "https://checkout.stripe.com/x")


class WebhookCase(unittest.TestCase):
    """A signed checkout.session.completed event credits the wallet."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        billing.set_store_path(Path(self._tmp.name) / "billing.json")
        self.addCleanup(billing.set_store_path, None)
        billing._local_app = False

    def _signed(self, secret, event):
        import hashlib, hmac, json, time
        payload = json.dumps(event).encode("utf-8")
        t = int(time.time())
        sig = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
        return payload, f"t={t},v1={sig}"

    def test_signed_events_credit_once_and_async_lands(self):
        import coinop
        secret = "whsec_test_local"
        cs = {"id": "cs_test_hook", "object": "checkout.session", "status": "complete",
              "payment_status": "unpaid", "amount_total": 2500,
              "metadata": {"purpose": "pack", "pack": "twentyfive", "email": "hook@example.com"}}
        with patch.dict("os.environ", {"STRIPE_WEBHOOK_SECRET": secret}, clear=False):
            ev = {"id": "evt_1", "object": "event", "type": "checkout.session.completed",
                  "data": {"object": cs}}
            out = coinop.handle_webhook(*self._signed(secret, ev))
            self.assertTrue(out["ok"])
            self.assertEqual(out["billing"]["reason"], "processing")
            ev2 = {"id": "evt_2", "object": "event", "type": "checkout.session.async_payment_succeeded",
                   "data": {"object": dict(cs, payment_status="paid")}}
            self.assertTrue(coinop.handle_webhook(*self._signed(secret, ev2))["billing"]["ok"])
            self.assertTrue(coinop.handle_webhook(*self._signed(secret, ev2))["billing"]["already_redeemed"])
            bad = coinop.handle_webhook(self._signed(secret, ev2)[0], "t=1,v1=00")
            self.assertEqual(bad["reason"], "bad_signature")
        wallet = billing._read()["accounts"]["hook@example.com"]["wallet_usd"]
        self.assertAlmostEqual(wallet, 27.5)


class CoinopHostedCase(unittest.TestCase):
    def test_cabinet_stays_off_hosted_saas(self):
        import coinop
        with patch.dict(
            "os.environ",
            {"FEATURE_BILLING": "1", "FEATURE_COINOP": "1", "COINOP_ON_HOSTED": ""},
            clear=False,
        ):
            self.assertFalse(coinop.is_enabled())

    def test_turn_meter_never_runs_on_the_wallet(self):
        import coinop
        with patch.dict("os.environ", {"FEATURE_BILLING": "1", "FEATURE_COINOP": "1",
                                        "COINOP_ON_HOSTED": "1", "COINOP_CREDIT_GATING": "1"}, clear=False), \
                patch.object(coinop, "is_enabled", return_value=True), \
                patch.object(billing, "requires_wallet", return_value=True):
            self.assertFalse(coinop.is_credit_gating_enabled())


if __name__ == "__main__":
    unittest.main()
