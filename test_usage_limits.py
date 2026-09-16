"""Optional monthly spend cap — unlimited by default, Cursor-style."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import usage_limits


class UsageLimitsCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "usage_limits.json"
        usage_limits.set_limits_path(self.path)
        self.addCleanup(usage_limits.set_limits_path, None)

    def test_default_is_unlimited(self):
        self.assertIsNone(usage_limits.monthly_cap_usd())
        with patch.object(usage_limits, "month_spend_usd", return_value=99.0):
            self.assertFalse(usage_limits.over_cap())

    def test_zero_and_empty_clear_the_cap(self):
        usage_limits.set_monthly_cap_usd(10)
        self.assertEqual(usage_limits.monthly_cap_usd(), 10.0)
        usage_limits.set_monthly_cap_usd(0)
        self.assertIsNone(usage_limits.monthly_cap_usd())
        usage_limits.set_monthly_cap_usd(25)
        usage_limits.set_monthly_cap_usd("")
        self.assertIsNone(usage_limits.monthly_cap_usd())
        usage_limits.set_monthly_cap_usd(25)
        usage_limits.set_monthly_cap_usd(None)
        self.assertIsNone(usage_limits.monthly_cap_usd())

    def test_over_cap_when_spend_meets_limit(self):
        usage_limits.set_monthly_cap_usd(5)
        with patch.object(usage_limits, "month_spend_usd", return_value=5.0):
            self.assertTrue(usage_limits.over_cap())
        with patch.object(usage_limits, "month_spend_usd", return_value=4.99):
            self.assertFalse(usage_limits.over_cap())

    def test_public_status_remaining(self):
        usage_limits.set_monthly_cap_usd(10)
        summary = {
            "total_cost_usd": 2.5,
            "spend_today_usd": 0.4,
            "event_count": 3,
            "cost_by_service": [{"service_type": "image", "cost_usd": 2.5, "event_count": 3}],
            "cost_by_provider": [],
        }
        with patch("cost_tracker.get_summary", return_value=summary):
            status = usage_limits.public_status()
        self.assertEqual(status["monthly_cap_usd"], 10.0)
        self.assertEqual(status["spend_usd"], 2.5)
        self.assertAlmostEqual(status["remaining_usd"], 7.5)
        self.assertFalse(status["over_cap"])
        self.assertTrue(status["byok"])


if __name__ == "__main__":
    unittest.main()
