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
    def test_text2img_leads_with_scene_then_style_tags(self):
        p = fal._build_text2img_prompt("a rusted watchtower on the horizon")
        # Scene must lead so SDXL's CLIP encoder actually sees it.
        self.assertTrue(p.strip().startswith("a rusted watchtower"))
        self.assertIn("VHS", p)
        # Lean prompt: no giant Gemini prose blocks.
        self.assertLessEqual(len(p), 700)
        self.assertNotIn("CINEMATIC PERSPECTIVE", p)

    def test_anti_text_constraints_live_in_negative_prompt(self):
        # The "no text / no border / no people" constraints belong in the
        # negative prompt for SDXL, not buried in the positive prompt.
        p = fal._build_text2img_prompt("a rusted watchtower on the horizon")
        self.assertNotIn("NO TEXT", p.upper())
        neg = fal._SDXL_NEGATIVE_PROMPT.lower()
        for term in ("text", "border", "person"):
            self.assertIn(term, neg)

    def test_img2img_includes_continuity_language(self):
        p = fal._build_img2img_prompt("step through the doorway")
        self.assertIn("doorway", p)
        self.assertIn("continuation", p.lower())
        self.assertLessEqual(len(p), 700)

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


if __name__ == "__main__":
    unittest.main()
