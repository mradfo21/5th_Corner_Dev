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
                "gemini:gemini-3.1-flash-lite": {"unit_type": "tokens", "input_per_1m": 37.5, "output_per_1m": 150.0},
                "gemini:default": {"unit_type": "tokens", "input_per_1m": 75.0, "output_per_1m": 300.0},
                "gemini:gemini-3-pro-image": {"unit_type": "images", "per_unit": 0.134, "sizes": {"1K": 0.134, "4K": 0.24}},
                "openai:old-style": {"unit_type": "tokens", "input_per_1k": 0.15, "output_per_1k": 0.6},
                "krea:krea-2/medium": {"unit_type": "images", "per_unit": 0.02},
                "voicebox:tts": {"unit_type": "characters", "per_1k": 0.18},
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
        cost = pricing.estimate_cost("voicebox", "tts", "characters", input_units=500)
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
        self.assertEqual(rate["input_per_1m"], 37.5)

    def test_per_1k_token_fields_are_refused(self):
        # The unit mix-up that priced text ~150x high must not come back.
        self.assertIsNone(pricing.estimate_cost("openai", "old-style", "tokens", input_units=1000, output_units=1000))

    def test_no_fallback_across_unit_types(self):
        # A picture logged under a purpose name must not be priced as a token.
        self.assertIsNone(pricing.estimate_cost("gemini", "talk_portrait", "images", output_units=1))

    def test_image_priced_at_its_size(self):
        self.assertAlmostEqual(pricing.estimate_cost("gemini", "gemini-3-pro-image", "images", output_units=1, size="4K"), 0.24)
        self.assertAlmostEqual(pricing.estimate_cost("gemini", "gemini-3-pro-image", "images", output_units=1, size="1k"), 0.134)
        self.assertAlmostEqual(pricing.estimate_cost("gemini", "gemini-3-pro-image", "images", output_units=1), 0.134)

    def test_shipped_table_is_sourced_and_per_million(self):
        import json as _json
        from pathlib import Path as _P
        table = _json.loads((_P(__file__).parent / "pricing.json").read_text(encoding="utf-8"))["rates"]
        for key, rate in table.items():
            self.assertIn("source", rate, key)
            self.assertIn("checked", rate, key)
            self.assertNotIn("input_per_1k", rate, key)
            self.assertNotIn("output_per_1k", rate, key)

    def test_shipped_flash_lite_is_cheap(self):
        # 10k tokens of story text costs well under a cent at the real rate.
        import json as _json
        from pathlib import Path as _P
        table = _json.loads((_P(__file__).parent / "pricing.json").read_text(encoding="utf-8"))
        pricing.save_pricing(table)
        cost = pricing.estimate_cost("gemini", "gemini-3.1-flash-lite", "tokens", input_units=10_000, output_units=200)
        self.assertLess(cost, 0.01)

    def test_shipped_voices_are_priced_on_the_key_you_play_on(self):
        """One key (docs/plans/ONE_KEY_AUDIO_PLAN.md): a spoken line is a
        Gemini TTS call, or an OpenAI one through the bridge, and both have a
        rate. A retired provider's rows would only price what nothing logs."""
        import json as _json
        from pathlib import Path as _P
        table = _json.loads((_P(__file__).parent / "pricing.json").read_text(encoding="utf-8"))
        self.assertFalse([k for k in table["rates"] if k.startswith("elevenlabs:")])
        pricing.save_pricing(table)
        # A minute of speech is 1,500 audio tokens out: about a cent and a half.
        minute = pricing.estimate_cost("gemini", "gemini-3.8-flash-tts", "tokens",
                                       input_units=0, output_units=1500)
        self.assertAlmostEqual(minute, 0.0135)
        self.assertLess(pricing.estimate_cost("gemini", "gemini-3.8-flash-lite-tts", "tokens",
                                              input_units=0, output_units=1500), minute)
        self.assertIsNotNone(pricing.estimate_cost(
            "gemini", "gemini-3.8-flash-tts:voice_design", "tokens",
            input_units=237, output_units=655 + 947))
        self.assertAlmostEqual(pricing.estimate_cost("openai", "gpt-4o-mini-tts", "seconds",
                                                     output_units=60), 0.015)


if __name__ == "__main__":
    unittest.main()
