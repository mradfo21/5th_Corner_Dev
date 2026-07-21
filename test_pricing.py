"""
test_pricing.py — offline unit tests for pricing.py (rate table + cost
estimation). Never touches the network; uses a temp pricing.json so it
never mutates the real committed file.

Run with:
    python3 -m unittest test_pricing -v
"""

import json
import tempfile
import unittest
from pathlib import Path

import pricing


class PricingTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_path = pricing.PRICING_PATH
        pricing.PRICING_PATH = Path(self._tmpdir.name) / "pricing.json"
        pricing._cached_pricing = None
        pricing._cache_timestamp = 0

        pricing.save_pricing({
            "rates": {
                "gemini:gemini-3.1-flash-lite": {"unit_type": "tokens", "input_per_1k": 0.0375, "output_per_1k": 0.15},
                "gemini:default": {"unit_type": "tokens", "input_per_1k": 0.075, "output_per_1k": 0.3},
                "krea:krea-2/medium": {"unit_type": "images", "per_unit": 0.02},
                "elevenlabs:tts": {"unit_type": "characters", "per_1k": 0.18},
                "reactor:default": {"unit_type": "seconds", "per_unit": None},
            }
        })

    def tearDown(self):
        pricing.PRICING_PATH = self._orig_path
        pricing._cached_pricing = None
        pricing._cache_timestamp = 0
        self._tmpdir.cleanup()

    def test_token_cost_uses_input_and_output_rates(self):
        cost = pricing.estimate_cost("gemini", "gemini-3.1-flash-lite", "tokens", input_units=1000, output_units=1000)
        self.assertAlmostEqual(cost, 0.0375 + 0.15)

    def test_falls_back_to_provider_default(self):
        cost = pricing.estimate_cost("gemini", "some-unknown-model", "tokens", input_units=1000, output_units=1000)
        self.assertAlmostEqual(cost, 0.075 + 0.3)

    def test_flat_per_image_rate(self):
        cost = pricing.estimate_cost("krea", "krea-2/medium", "images", output_units=1)
        self.assertAlmostEqual(cost, 0.02)

    def test_flat_rate_defaults_output_units_to_one(self):
        cost = pricing.estimate_cost("krea", "krea-2/medium", "images")
        self.assertAlmostEqual(cost, 0.02)

    def test_character_rate(self):
        cost = pricing.estimate_cost("elevenlabs", "tts", "characters", input_units=500)
        self.assertAlmostEqual(cost, 0.09)

    def test_unpriced_null_rate_returns_none(self):
        cost = pricing.estimate_cost("reactor", "default", "seconds", output_units=30)
        self.assertIsNone(cost)

    def test_unknown_provider_returns_none(self):
        cost = pricing.estimate_cost("totally_unknown", "model", "tokens", input_units=100, output_units=100)
        self.assertIsNone(cost)

    def test_set_rate_persists_and_reloads(self):
        pricing.set_rate("fal", "fast-lightning-sdxl", {"unit_type": "images", "per_unit": 0.0035})
        cost = pricing.estimate_cost("fal", "fast-lightning-sdxl", "images", output_units=1)
        self.assertAlmostEqual(cost, 0.0035)

        # Confirm it actually hit disk, not just the in-memory cache.
        with pricing.PRICING_PATH.open() as f:
            on_disk = json.load(f)
        self.assertIn("fal:fast-lightning-sdxl", on_disk["rates"])

    def test_get_rate_prefers_exact_model_over_default(self):
        rate = pricing.get_rate("gemini", "gemini-3.1-flash-lite")
        self.assertEqual(rate["input_per_1k"], 0.0375)


if __name__ == "__main__":
    unittest.main()
