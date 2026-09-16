"""Coin-op packs and cabinet defaults — no live Stripe calls."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import coinop


class PackTests(unittest.TestCase):
    def test_unknown_pack_falls_back_to_roll(self):
        p = coinop.pack_by_id("nope")
        self.assertEqual(p["id"], "roll")
        self.assertTrue(p["usual"])

    def test_known_packs(self):
        self.assertEqual(coinop.pack_by_id("coin")["credits"], 20)
        self.assertEqual(coinop.pack_by_id("coin")["price_cents"], 199)
        self.assertEqual(coinop.pack_by_id("bucket")["credits"], 200)
        self.assertEqual(coinop.pack_by_id("bucket")["price_cents"], 999)

    def test_credits_for_pack(self):
        self.assertEqual(coinop.credits_for_pack("roll", 12), 12)
        self.assertEqual(coinop.credits_for_pack("roll", "0"), 80)
        self.assertEqual(coinop.credits_for_pack(None), 80)
        self.assertEqual(coinop.credits_for_pack("bucket"), 200)


class ConfigTests(unittest.TestCase):
    def test_disabled_config_is_dark(self):
        env = {k: os.environ.get(k) for k in ("FEATURE_COINOP",)}
        try:
            os.environ.pop("FEATURE_COINOP", None)
            cfg = coinop.public_config()
        finally:
            if env["FEATURE_COINOP"] is None:
                os.environ.pop("FEATURE_COINOP", None)
            else:
                os.environ["FEATURE_COINOP"] = env["FEATURE_COINOP"]
        self.assertFalse(cfg["enabled"])
        self.assertTrue(cfg.get("dark"))
        self.assertIn("FEATURE_COINOP", cfg.get("needs") or [])

    def test_public_config_lists_three_packs_with_roll_usual(self):
        with patch.object(coinop, "is_enabled", return_value=True):
            cfg = coinop.public_config()
        self.assertTrue(cfg["enabled"])
        ids = [p["id"] for p in cfg["packs"]]
        self.assertEqual(ids, ["coin", "roll", "bucket"])
        usual = [p for p in cfg["packs"] if p["usual"]]
        self.assertEqual(len(usual), 1)
        self.assertEqual(usual[0]["id"], "roll")
        self.assertEqual(cfg["default_pack"], "roll")
        self.assertTrue(cfg["credit_gating"])

    def test_gating_defaults_on_when_machine_is_live(self):
        saved = os.environ.get("COINOP_CREDIT_GATING")
        try:
            os.environ.pop("COINOP_CREDIT_GATING", None)
            with patch.object(coinop, "is_enabled", return_value=True):
                self.assertTrue(coinop.is_credit_gating_enabled())
                self.assertTrue(coinop.public_config()["credit_gating"])
        finally:
            if saved is None:
                os.environ.pop("COINOP_CREDIT_GATING", None)
            else:
                os.environ["COINOP_CREDIT_GATING"] = saved

    def test_gating_can_be_turned_off(self):
        saved = os.environ.get("COINOP_CREDIT_GATING")
        try:
            os.environ["COINOP_CREDIT_GATING"] = "0"
            with patch.object(coinop, "is_enabled", return_value=True):
                self.assertFalse(coinop.is_credit_gating_enabled())
        finally:
            if saved is None:
                os.environ.pop("COINOP_CREDIT_GATING", None)
            else:
                os.environ["COINOP_CREDIT_GATING"] = saved


class CheckoutPackTests(unittest.TestCase):
    def test_comp_checkout_uses_selected_pack(self):
        with patch.object(coinop, "is_enabled", return_value=True), \
             patch.object(coinop, "_comp_available", return_value={
                 "ok": True, "reason": "test_mode", "code": None,
             }), \
             patch.object(coinop, "_mark_seen_paid") as mark:
            out = coinop.create_checkout(
                "s1",
                SimpleNamespace(host_url="http://localhost/"),
                pack_id="bucket",
                return_to="machine",
            )
        self.assertTrue(out["comp"])
        self.assertEqual(out["pack"], "bucket")
        self.assertEqual(out["credits"], 200)
        self.assertEqual(mark.call_args.kwargs["pack"], "bucket")


if __name__ == "__main__":
    unittest.main()
