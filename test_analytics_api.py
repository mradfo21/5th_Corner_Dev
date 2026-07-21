"""
test_analytics_api.py — Flask test-client tests for the /api/admin/analytics/*
and /api/admin/pricing routes added for the Cost & Usage Analytics tab.

Uses api.app's test client against a temp SQLite DB / pricing.json (never the
real ones), and sets ADMIN_TOKEN for the duration of the test module so the
auth-guard behavior (401 without a token, 200 with it) is exercised exactly
as it runs in production.

Run with:
    python3 -m unittest test_analytics_api -v
"""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

import api  # noqa: E402  (import after ADMIN_TOKEN is set)
import cost_tracker  # noqa: E402
import pricing  # noqa: E402


class AnalyticsApiTestCase(unittest.TestCase):
    def setUp(self):
        os.environ["ADMIN_TOKEN"] = "test-admin-token"
        self.client = api.app.test_client()
        self.auth_headers = {"X-Admin-Token": "test-admin-token"}

        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_root = Path(self._tmpdir.name)

        self._orig_db_path = cost_tracker.DB_PATH
        self._orig_analytics_dir = cost_tracker.ANALYTICS_DIR
        cost_tracker.ANALYTICS_DIR = tmp_root
        cost_tracker.DB_PATH = tmp_root / "usage.db"
        cost_tracker._initialized = False

        self._orig_pricing_path = pricing.PRICING_PATH
        pricing.PRICING_PATH = tmp_root / "pricing.json"
        pricing._cached_pricing = None
        pricing._cache_timestamp = 0
        pricing.save_pricing({
            "rates": {
                "gemini:gemini-3.1-flash-lite": {"unit_type": "tokens", "input_per_1k": 0.0375, "output_per_1k": 0.15},
                "krea:krea-2/medium": {"unit_type": "images", "per_unit": 0.02},
            }
        })

        cost_tracker.record_usage("s1", "text", "gemini", "gemini-3.1-flash-lite",
                                   input_units=1000, output_units=1000, unit_type="tokens", success=True, turn_count=1)
        cost_tracker.record_usage("s1", "image", "krea", "krea-2/medium",
                                   output_units=1, unit_type="images", success=False, error_message="timeout", turn_count=2)
        cost_tracker.record_usage("s2", "image", "krea", "krea-2/medium",
                                   output_units=1, unit_type="images", success=True)

    def tearDown(self):
        cost_tracker.DB_PATH = self._orig_db_path
        cost_tracker.ANALYTICS_DIR = self._orig_analytics_dir
        cost_tracker._initialized = False
        pricing.PRICING_PATH = self._orig_pricing_path
        pricing._cached_pricing = None
        pricing._cache_timestamp = 0
        self._tmpdir.cleanup()

    def test_summary_requires_admin_token(self):
        res = self.client.get("/api/admin/analytics/summary")
        self.assertEqual(res.status_code, 401)

    def test_summary_ok_with_token_header(self):
        res = self.client.get("/api/admin/analytics/summary?range=all", headers=self.auth_headers)
        self.assertEqual(res.status_code, 200)
        data = res.get_json()["data"]
        self.assertEqual(data["session_count"], 2)
        self.assertEqual(data["error_count"], 1)

    def test_summary_ok_with_token_query_param(self):
        res = self.client.get("/api/admin/analytics/summary?range=all&token=test-admin-token")
        self.assertEqual(res.status_code, 200)

    def test_timeseries_returns_buckets(self):
        res = self.client.get("/api/admin/analytics/timeseries?range=all&granularity=day", headers=self.auth_headers)
        self.assertEqual(res.status_code, 200)
        data = res.get_json()["data"]
        self.assertGreaterEqual(len(data["buckets"]), 1)

    def test_sessions_list_sorted_by_cost(self):
        res = self.client.get("/api/admin/analytics/sessions?sort=cost_desc", headers=self.auth_headers)
        self.assertEqual(res.status_code, 200)
        sessions = res.get_json()["data"]["sessions"]
        self.assertEqual(len(sessions), 2)

    def test_session_detail_returns_events(self):
        res = self.client.get("/api/admin/analytics/sessions/s1", headers=self.auth_headers)
        self.assertEqual(res.status_code, 200)
        data = res.get_json()["data"]
        self.assertEqual(len(data["events"]), 2)

    def test_providers_breakdown(self):
        res = self.client.get("/api/admin/analytics/providers?range=all", headers=self.auth_headers)
        self.assertEqual(res.status_code, 200)
        providers = res.get_json()["data"]["providers"]
        self.assertTrue(any(p["provider"] == "krea" for p in providers))

    def test_errors_list_contains_failed_call(self):
        res = self.client.get("/api/admin/analytics/errors?range=all", headers=self.auth_headers)
        self.assertEqual(res.status_code, 200)
        errors = res.get_json()["data"]["errors"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error_message"], "timeout")

    def test_export_csv_has_header_and_rows(self):
        res = self.client.get("/api/admin/analytics/export.csv?range=all", headers=self.auth_headers)
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/csv", res.headers["Content-Type"])
        lines = res.get_data(as_text=True).strip().splitlines()
        self.assertEqual(lines[0].split(",")[0], "id")
        self.assertEqual(len(lines), 4)  # header + 3 seeded events

    def test_pricing_get_requires_token(self):
        res = self.client.get("/api/admin/pricing")
        self.assertEqual(res.status_code, 401)

    def test_pricing_get_returns_rates(self):
        res = self.client.get("/api/admin/pricing", headers=self.auth_headers)
        self.assertEqual(res.status_code, 200)
        rates = res.get_json()["data"]["rates"]
        self.assertIn("krea:krea-2/medium", rates)

    def test_pricing_put_updates_single_rate(self):
        res = self.client.put(
            "/api/admin/pricing",
            headers=self.auth_headers,
            json={"provider": "fal", "model": "fast-lightning-sdxl", "rate": {"unit_type": "images", "per_unit": 0.0035}},
        )
        self.assertEqual(res.status_code, 200)
        rates = res.get_json()["data"]["rates"]
        self.assertEqual(rates["fal:fast-lightning-sdxl"]["per_unit"], 0.0035)

    def test_pricing_put_rejects_malformed_body(self):
        res = self.client.put("/api/admin/pricing", headers=self.auth_headers, json={"provider": "fal"})
        self.assertEqual(res.status_code, 400)

    def test_reactor_usage_records_a_video_event_no_auth_required(self):
        # This is a client-facing beacon endpoint (reactor_renderer.js), not an
        # /api/admin/* route — it must NOT require ADMIN_TOKEN.
        res = self.client.post(
            "/api/reactor/usage",
            json={"session_id": "s1", "model": "happy-oyster", "duration_seconds": 42.0},
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["ok"])
        events = cost_tracker.get_session_detail("s1")["events"]
        video_events = [e for e in events if e["service_type"] == "video"]
        self.assertEqual(len(video_events), 1)
        self.assertEqual(video_events[0]["provider"], "reactor")
        self.assertEqual(video_events[0]["model"], "happy-oyster")
        self.assertEqual(video_events[0]["output_units"], 42.0)

    def test_reactor_usage_ignores_zero_duration(self):
        res = self.client.post(
            "/api/reactor/usage",
            json={"session_id": "s1", "model": "happy-oyster", "duration_seconds": 0},
        )
        self.assertEqual(res.status_code, 200)
        events = cost_tracker.get_session_detail("s1")["events"]
        self.assertEqual(len([e for e in events if e["service_type"] == "video"]), 0)

    def test_reactor_usage_malformed_body_never_errors(self):
        res = self.client.post("/api/reactor/usage", data="not json", content_type="application/json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["ok"])

    def test_talk_end_records_talk_agent_voice_usage(self):
        # /api/talk/end is the only server-side signal ElevenLabs' TALK
        # conversational agent (a client<->agent websocket) ever gives us —
        # the client reports how long the channel was actually connected.
        res = self.client.post(
            "/api/talk/end",
            json={"voice_id": "", "session_id": "s1", "duration_seconds": 17.5},
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["ok"])
        events = cost_tracker.get_session_detail("s1")["events"]
        talk_events = [e for e in events if e["service_type"] == "voice" and e["model"] == "talk_agent"]
        self.assertEqual(len(talk_events), 1)
        self.assertEqual(talk_events[0]["provider"], "elevenlabs")
        self.assertEqual(talk_events[0]["output_units"], 17.5)

    def test_talk_end_zero_duration_records_nothing(self):
        res = self.client.post("/api/talk/end", json={"voice_id": "", "session_id": "s1"})
        self.assertEqual(res.status_code, 200)
        events = cost_tracker.get_session_detail("s1")["events"]
        self.assertEqual(len([e for e in events if e["model"] == "talk_agent"]), 0)

    def test_storage_health_requires_admin_token(self):
        res = self.client.get("/api/admin/analytics/storage_health")
        self.assertEqual(res.status_code, 401)

    def test_storage_health_returns_diagnostic_fields(self):
        res = self.client.get("/api/admin/analytics/storage_health", headers=self.auth_headers)
        self.assertEqual(res.status_code, 200)
        data = res.get_json()["data"]
        self.assertIn("mount_detected", data)
        self.assertIn("survived_restart", data)
        self.assertIn("db_path", data)


if __name__ == "__main__":
    unittest.main()
