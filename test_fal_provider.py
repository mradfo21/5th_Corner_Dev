"""
test_fal_provider.py — offline unit tests for the fal.ai SDXL Lightning image
provider (fal_image_utils.py) and its wiring into ai_provider_manager /
ai_config.json.

These tests never touch the network: they exercise pure helpers (prompt
building, image URL extraction, reference data-URI selection) and the
no-API-key short-circuit. Run with:

    python3 -m unittest test_fal_provider -v
"""

import tempfile
import unittest
from pathlib import Path

import fal_image_utils as fal
import ai_provider_manager as apm


class TestPromptBuilding(unittest.TestCase):
    def test_text2img_includes_scene_and_anchors(self):
        p = fal._build_text2img_prompt("a rusted watchtower on the horizon")
        self.assertIn("rusted watchtower", p)
        self.assertIn("NO TEXT", p.upper())
        self.assertLessEqual(len(p), 1200)

    def test_img2img_includes_continuity_language(self):
        p = fal._build_img2img_prompt("step through the doorway")
        self.assertIn("doorway", p)
        self.assertIn("previous moment", p.lower())
        self.assertLessEqual(len(p), 1200)

    def test_time_of_day_injected(self):
        p = fal._build_text2img_prompt("empty road", time_of_day="dusk, overcast")
        self.assertIn("dusk", p)


class TestExtractFirstImageUrl(unittest.TestCase):
    def test_url_field(self):
        result = {"images": [{"url": "https://x/a.png"}]}
        self.assertEqual(fal._extract_first_image_url(result), "https://x/a.png")

    def test_plain_string_entry(self):
        result = {"images": ["https://x/a.png"]}
        self.assertEqual(fal._extract_first_image_url(result), "https://x/a.png")

    def test_empty_when_missing(self):
        self.assertIsNone(fal._extract_first_image_url({}))
        self.assertIsNone(fal._extract_first_image_url({"images": []}))


class TestReferenceDataUri(unittest.TestCase):
    def test_prefers_small_sidecar_when_present(self):
        old = fal.USE_DOWNSAMPLED_FOR_IMG2IMG
        fal.USE_DOWNSAMPLED_FOR_IMG2IMG = True
        try:
            with tempfile.TemporaryDirectory() as d:
                full = Path(d) / "frame.png"
                small = Path(d) / "frame_small.png"
                full.write_bytes(b"full-bytes")
                small.write_bytes(b"small-bytes")
                uri = fal._reference_data_uri(str(full))
                self.assertTrue(uri.startswith("data:image/png;base64,"))
                # Decoded payload should match the SMALL sidecar, not the full file.
                import base64
                decoded = base64.b64decode(uri.split(",", 1)[1])
                self.assertEqual(decoded, b"small-bytes")
        finally:
            fal.USE_DOWNSAMPLED_FOR_IMG2IMG = old

    def test_falls_back_to_full_when_no_sidecar(self):
        with tempfile.TemporaryDirectory() as d:
            full = Path(d) / "frame.png"
            full.write_bytes(b"only-bytes")
            uri = fal._reference_data_uri(str(full))
            self.assertTrue(uri.startswith("data:image/png;base64,"))

    def test_none_when_missing_file(self):
        self.assertIsNone(fal._reference_data_uri("/nonexistent/path/frame.png"))


class TestNoApiKeyShortCircuit(unittest.TestCase):
    def setUp(self):
        self._old_key = fal.FAL_API_KEY

    def tearDown(self):
        fal.FAL_API_KEY = self._old_key

    def test_text2img_returns_none_without_key(self):
        fal.FAL_API_KEY = ""
        self.assertIsNone(fal.generate_with_fal("prompt", "caption"))

    def test_img2img_returns_none_without_key(self):
        fal.FAL_API_KEY = ""
        self.assertIsNone(fal.generate_fal_img2img("prompt", "caption", "ref.png"))


class TestProviderManagerWiring(unittest.TestCase):
    def test_fal_preset_available(self):
        presets = apm.get_available_presets()
        self.assertIn("fal", presets)
        self.assertEqual(presets["fal"]["image_provider"], "fal")
        self.assertEqual(presets["fal"]["image_model"], "fal-ai/fast-lightning-sdxl")

    def test_fal_is_shipped_default(self):
        self.assertEqual(apm.get_image_provider(), "fal")
        self.assertEqual(apm.get_image_model(), "fal-ai/fast-lightning-sdxl")


class TestSpeedDefaults(unittest.TestCase):
    def test_two_step_default(self):
        self.assertEqual(fal.FAL_NUM_INFERENCE_STEPS, 2)

    def test_sync_mode_on_by_default(self):
        self.assertTrue(fal._FAL_SYNC_MODE)

    def test_safety_checker_off_by_default(self):
        self.assertFalse(fal._FAL_ENABLE_SAFETY_CHECKER)

    def test_base_payload_is_speed_tuned(self):
        payload = fal._base_payload("a wet alley")
        self.assertEqual(payload["num_inference_steps"], 2)
        self.assertEqual(payload["num_images"], 1)
        self.assertTrue(payload["sync_mode"])
        self.assertFalse(payload["enable_safety_checker"])
        self.assertFalse(payload["expand_prompt"])
        self.assertEqual(payload["format"], "jpeg")


if __name__ == "__main__":
    unittest.main()
