"""
test_cost_tracker.py — offline unit tests for cost_tracker.py (the SQLite
usage ledger backing the admin Analytics tab). Never touches the network;
each test points cost_tracker at a fresh temp SQLite file and a fresh temp
pricing.json so it never mutates real project data.

Run with:
    python3 -m unittest test_cost_tracker -v
"""

import tempfile
import unittest
from pathlib import Path

import cost_tracker
import pricing


class CostTrackerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_root = Path(self._tmpdir.name)

        self._orig_db_path = cost_tracker.DB_PATH
        self._orig_analytics_dir = cost_tracker.ANALYTICS_DIR
        cost_tracker.ANALYTICS_DIR = tmp_root
        cost_tracker.DB_PATH = tmp_root / "usage.db"
        cost_tracker._initialized = False
        cost_tracker._wire.pending = {}  # nothing folds in from another test

        self._orig_pricing_path = pricing.PRICING_PATH
        pricing.PRICING_PATH = tmp_root / "pricing.json"
        pricing._cached_pricing = None
        pricing._cache_timestamp = 0
        pricing.save_pricing({
            "rates": {
                "gemini:gemini-3.1-flash-lite": {"unit_type": "tokens", "input_per_1m": 37.5, "output_per_1m": 150.0},
                "krea:krea-2/medium": {"unit_type": "images", "per_unit": 0.02},
                "reactor:default": {"unit_type": "seconds", "per_unit": None},
            }
        })

    def tearDown(self):
        cost_tracker.DB_PATH = self._orig_db_path
        cost_tracker.ANALYTICS_DIR = self._orig_analytics_dir
        cost_tracker._initialized = False
        pricing.PRICING_PATH = self._orig_pricing_path
        pricing._cached_pricing = None
        pricing._cache_timestamp = 0
        self._tmpdir.cleanup()

    def test_record_usage_returns_estimated_cost(self):
        cost = cost_tracker.record_usage(
            "s1", "text", "gemini", "gemini-3.1-flash-lite",
            input_units=1000, output_units=1000, unit_type="tokens", success=True,
        )
        self.assertAlmostEqual(cost, 0.0375 + 0.15)

    def test_record_usage_never_raises_on_bad_input(self):
        # None-typed session_id / garbage kwargs must degrade to a logged
        # failure, never an exception reaching the caller (gameplay must
        # never break because tracking broke).
        cost = cost_tracker.record_usage(None, "text", "gemini", "x", input_units="not-a-number")
        self.assertIsNone(cost)

    def test_failed_call_is_not_priced(self):
        cost = cost_tracker.record_usage(
            "s1", "image", "krea", "krea-2/medium", output_units=1, unit_type="images", success=False,
            error_message="timeout",
        )
        self.assertIsNone(cost)
        errors = cost_tracker.get_errors("all")["errors"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error_message"], "timeout")

    def test_unpriced_success_shows_up_as_unpriced(self):
        cost_tracker.record_usage("s1", "voice", "reactor", "default", output_units=30, unit_type="seconds", success=True)
        summary = cost_tracker.get_summary("all")
        self.assertEqual(summary["unpriced_event_count"], 1)

    def test_summary_aggregates_across_providers(self):
        cost_tracker.record_usage("s1", "text", "gemini", "gemini-3.1-flash-lite",
                                   input_units=1000, output_units=1000, unit_type="tokens", success=True)
        cost_tracker.record_usage("s1", "image", "krea", "krea-2/medium",
                                   output_units=1, unit_type="images", success=True)
        cost_tracker.record_usage("s2", "image", "krea", "krea-2/medium",
                                   output_units=1, unit_type="images", success=False, error_message="boom")

        summary = cost_tracker.get_summary("all")
        self.assertEqual(summary["event_count"], 3)
        self.assertEqual(summary["error_count"], 1)
        self.assertEqual(summary["session_count"], 2)
        self.assertAlmostEqual(summary["total_cost_usd"], 0.0375 + 0.15 + 0.02)

        by_service = {r["service_type"]: r["cost_usd"] for r in summary["cost_by_service"]}
        self.assertAlmostEqual(by_service["text"], 0.0375 + 0.15)
        self.assertAlmostEqual(by_service["image"], 0.02)

    def test_session_rollup_matches_ledger(self):
        cost_tracker.record_usage("s1", "text", "gemini", "gemini-3.1-flash-lite",
                                   input_units=2000, output_units=0, unit_type="tokens", success=True)
        cost_tracker.record_usage("s1", "image", "krea", "krea-2/medium",
                                   output_units=1, unit_type="images", success=True)

        detail = cost_tracker.get_session_detail("s1")
        self.assertEqual(detail["rollup"]["event_count"], 2)
        self.assertAlmostEqual(detail["rollup"]["total_cost_usd"], 0.075 + 0.02)
        self.assertEqual(len(detail["events"]), 2)

    def test_get_sessions_sorts_by_cost_desc_by_default(self):
        cost_tracker.record_usage("cheap", "image", "krea", "krea-2/medium", output_units=1, unit_type="images", success=True)
        cost_tracker.record_usage("expensive", "text", "gemini", "gemini-3.1-flash-lite",
                                   input_units=100000, output_units=100000, unit_type="tokens", success=True)

        sessions = cost_tracker.get_sessions(sort="cost_desc")["sessions"]
        self.assertEqual(sessions[0]["session_id"], "expensive")
        self.assertEqual(sessions[1]["session_id"], "cheap")

    def test_providers_breakdown_groups_by_provider_and_model(self):
        cost_tracker.record_usage("s1", "image", "krea", "krea-2/medium", output_units=1, unit_type="images", success=True)
        cost_tracker.record_usage("s2", "image", "krea", "krea-2/medium", output_units=1, unit_type="images", success=True)

        rows = cost_tracker.get_providers_breakdown("all")["providers"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "krea")
        self.assertEqual(rows[0]["n"], 2)
        self.assertAlmostEqual(rows[0]["cost"], 0.04)

    def test_export_iterator_yields_all_events_in_order(self):
        cost_tracker.record_usage("s1", "text", "gemini", "gemini-3.1-flash-lite",
                                   input_units=100, output_units=100, unit_type="tokens", success=True)
        cost_tracker.record_usage("s1", "image", "krea", "krea-2/medium", output_units=1, unit_type="images", success=True)

        rows = list(cost_tracker.iter_events_for_export("all"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["service_type"], "text")
        self.assertEqual(rows[1]["service_type"], "image")

    def test_track_context_manager_records_on_success(self):
        with cost_tracker.track("s1", "voice", "gemini", "gemini-3.8-flash-tts") as t:
            t["input_units"] = 60
            t["output_units"] = 350
            t["unit_type"] = "tokens"
        errors = cost_tracker.get_errors("all")["errors"]
        self.assertEqual(len(errors), 0)
        summary = cost_tracker.get_summary("all")
        self.assertEqual(summary["event_count"], 1)

    def test_track_context_manager_records_failure_and_reraises(self):
        with self.assertRaises(RuntimeError):
            with cost_tracker.track("s1", "voice", "gemini", "gemini-3.8-flash-tts"):
                raise RuntimeError("boom")
        errors = cost_tracker.get_errors("all")["errors"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error_message"], "boom")

    def test_storage_health_with_no_data_is_inconclusive(self):
        health = cost_tracker.get_storage_health()
        self.assertIsNone(health["oldest_event_at"])
        self.assertIsNone(health["survived_restart"])
        self.assertIsInstance(health["mount_detected"], bool)

    def test_storage_health_not_yet_survived_a_restart(self):
        # Data written by THIS process is necessarily newer than this
        # process's own start time — that alone proves nothing either way.
        cost_tracker.record_usage("s1", "text", "gemini", "gemini-3.1-flash-lite",
                                   input_units=10, output_units=10, unit_type="tokens", success=True)
        health = cost_tracker.get_storage_health()
        self.assertIsNotNone(health["oldest_event_at"])
        self.assertFalse(health["survived_restart"])

    def test_storage_health_confirms_survived_restart(self):
        from datetime import timedelta
        cost_tracker.record_usage("s1", "text", "gemini", "gemini-3.1-flash-lite",
                                   input_units=10, output_units=10, unit_type="tokens", success=True)
        # Simulate "a restart happened": move this process's recorded start
        # time to AFTER the event that's already in the ledger.
        orig_started_at = cost_tracker._PROCESS_STARTED_AT
        try:
            # "Now + 5s", not "import time + 5s": in a long test run the
            # event above can be written more than 5s after import.
            from datetime import datetime, timezone
            cost_tracker._PROCESS_STARTED_AT = datetime.now(timezone.utc) + timedelta(seconds=5)
            health = cost_tracker.get_storage_health()
            self.assertTrue(health["survived_restart"])
        finally:
            cost_tracker._PROCESS_STARTED_AT = orig_started_at


    # ── wire logging (A3) and charging (C1) ───────────────────────────────
    def _events(self):
        conn = cost_tracker._connect()
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM usage_events ORDER BY id")]
        finally:
            conn.close()

    class _Resp:
        status_code = 200

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def test_wire_logs_a_picture_once_under_its_real_model_and_size(self):
        pricing.save_pricing({"rates": {
            "gemini:gemini-3.1-flash-image": {"unit_type": "images", "per_unit": 0.067,
                                              "sizes": {"1K": 0.067, "2K": 0.101}},
        }})
        body = {"candidates": [{"content": {"parts": [{"inlineData": {"data": "x"}}]}}],
                "usageMetadata": {"promptTokenCount": 10}}
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-image-preview:generateContent"
        payload = {"generationConfig": {"imageConfig": {"imageSize": "2K"}}}
        cost_tracker._log_wire(cost_tracker._gemini_model(url), payload, self._Resp(body), 900)
        # The caller's own log of that picture (under a guessed model) folds in.
        folded = cost_tracker.record_usage("s1", "image", "gemini", "guess", output_units=1,
                                           unit_type="images", success=True)
        self.assertIsNone(folded)
        rows = self._events()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "gemini-3.1-flash-image")
        self.assertAlmostEqual(rows[0]["cost_usd"], 0.101)
        # A later picture nobody wire-logged is still counted.
        cost_tracker.record_usage("s1", "image", "gemini", "other", output_units=1,
                                  unit_type="images", success=True)
        self.assertEqual(len(self._events()), 2)

    def test_wire_logs_text_tokens(self):
        body = {"candidates": [{"content": {"parts": [{"text": "hi"}]}}],
                "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 800,
                                  "thoughtsTokenCount": 200}}
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key=x"
        cost_tracker._log_wire(cost_tracker._gemini_model(url), {}, self._Resp(body), 300)
        self.assertIsNone(cost_tracker.record_usage(
            "s1", "text", "gemini", "gemini-3.1-flash-lite", operation="ask",
            input_units=1000, output_units=1000, unit_type="tokens", success=True))
        rows = self._events()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["input_units"], 1000)
        self.assertEqual(rows[0]["output_units"], 1000)

    def test_a_logged_cost_is_charged_to_the_wallet(self):
        from unittest.mock import patch
        import billing
        with patch.object(billing, "charge_cost",
                          return_value={"wallet": "w_" + "b" * 32, "charged_usd": 0.375, "markup": 2.0}) as charge:
            cost = cost_tracker.record_usage("s1", "text", "gemini", "gemini-3.1-flash-lite",
                                             input_units=1000, output_units=1000,
                                             unit_type="tokens", success=True)
        charge.assert_called_once()
        row = self._events()[0]
        self.assertEqual(row["wallet"], "w_" + "b" * 32)
        self.assertAlmostEqual(row["charged_usd"], 0.375)
        self.assertAlmostEqual(row["markup"], 2.0)
        self.assertAlmostEqual(cost, 0.1875)


    def test_history_is_repriced_once_keeping_the_old_value(self):
        import sqlite3
        # A ledger from before the fix: no v1 column, a text row at the old
        # per-1k price.
        cost_tracker._initialized = False
        conn = sqlite3.connect(str(cost_tracker.DB_PATH))
        conn.executescript("""
            CREATE TABLE usage_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
              session_id TEXT NOT NULL, turn_count INTEGER, service_type TEXT NOT NULL,
              provider TEXT NOT NULL, model TEXT NOT NULL, operation TEXT, input_units REAL,
              output_units REAL, unit_type TEXT, cost_usd REAL, latency_ms INTEGER,
              success INTEGER NOT NULL DEFAULT 1, error_message TEXT, discord_guild_id TEXT,
              discord_channel_id TEXT, meta_json TEXT);
            INSERT INTO usage_events (ts, session_id, service_type, provider, model, input_units,
              output_units, unit_type, cost_usd, success)
            VALUES ('2026-09-01T00:00:00+00:00', 'old', 'text', 'gemini', 'gemini-3.1-flash-lite',
              1000, 1000, 'tokens', 187.5, 1);
        """)
        conn.commit()
        conn.close()
        cost_tracker.init_db()
        row = self._events()[0]
        self.assertAlmostEqual(row["cost_usd_v1"], 187.5)
        self.assertAlmostEqual(row["cost_usd"], 0.0375 + 0.15)
        self.assertAlmostEqual(cost_tracker.session_cost_usd("old"), 0.0375 + 0.15)


    def test_receipts_split_a_run_and_keep_wallets_apart(self):
        from unittest.mock import patch
        import billing
        mine, theirs = "w_" + "c" * 32, "w_" + "d" * 32
        with patch.object(billing, "charge_cost",
                          return_value={"wallet": mine, "charged_usd": 0.04, "markup": 2.0}):
            cost_tracker.record_usage("run1", "image", "krea", "krea-2/medium",
                                      output_units=1, unit_type="images", success=True)
            with patch.object(billing, "requires_wallet", return_value=True), \
                    patch.object(billing, "_known_key", return_value=mine):
                cost_tracker.record_usage("run1", "image", "krea", "krea-2/medium",
                                          output_units=1, unit_type="images", success=False,
                                          error_message="timeout")
        with patch.object(billing, "charge_cost",
                          return_value={"wallet": theirs, "charged_usd": 0.04, "markup": 2.0}):
            cost_tracker.record_usage("run2", "image", "krea", "krea-2/medium",
                                      output_units=1, unit_type="images", success=True)
        mine_r = cost_tracker.receipts(mine)
        self.assertEqual(len(mine_r), 1)
        part = mine_r[0]["parts"][0]
        self.assertEqual((part["service"], part["count"], part["failed"]), ("image", 1, 1))
        self.assertAlmostEqual(mine_r[0]["charged_usd"], 0.04)
        self.assertEqual(len(cost_tracker.receipts(None)), 2)


if __name__ == "__main__":
    unittest.main()
