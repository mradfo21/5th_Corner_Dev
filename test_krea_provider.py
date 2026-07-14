"""
test_krea_provider.py — offline unit tests for the Krea 2 image provider
(krea_image_utils.py) and its wiring into ai_provider_manager / ai_config.json.

These tests never touch the network: they exercise pure helpers (model tier
resolution, prompt building, URL extraction, reference path selection) and the
no-API-key short-circuit. Run with:

    python3 -m unittest test_krea_provider -v
"""

import os
import tempfile
import unittest
from pathlib import Path

import contextlib

import krea_image_utils as krea
import ai_provider_manager as apm


@contextlib.contextmanager
def _patched_image_model(model_name):
    """Temporarily force ai_provider_manager.get_image_model() to a value so
    tier-resolution logic can be tested without mutating ai_config.json."""
    original = apm.get_image_model
    apm.get_image_model = lambda: model_name
    try:
        yield
    finally:
        apm.get_image_model = original


class TestTierFromName(unittest.TestCase):
    def test_large(self):
        self.assertEqual(krea._tier_from_name("krea-2/large"), krea.KREA_LARGE)

    def test_medium(self):
        self.assertEqual(krea._tier_from_name("krea-2/medium"), krea.KREA_MEDIUM)

    def test_none_for_non_tier(self):
        self.assertIsNone(krea._tier_from_name("gemini-3.1-flash-lite-image"))
        self.assertIsNone(krea._tier_from_name(None))


class TestModelResolution(unittest.TestCase):
    def test_explicit_arg_wins(self):
        self.assertEqual(krea._resolve_model("krea-2/large", False), krea.KREA_LARGE)
        self.assertEqual(krea._resolve_model("krea-2/medium", True), krea.KREA_MEDIUM)

    def test_config_model_drives_tier(self):
        # With no explicit arg, the configured image_model is authoritative
        # (decoupled from hd_mode) so the preset controls speed vs quality.
        apm._backend_override = None
        with _patched_image_model("krea-2/large"):
            self.assertEqual(krea._resolve_model(None, False), krea.KREA_LARGE)
        with _patched_image_model("krea-2/medium"):
            self.assertEqual(krea._resolve_model(None, True), krea.KREA_MEDIUM)

    def test_hd_mode_fallback_when_config_has_no_tier(self):
        with _patched_image_model("gemini-3.1-flash-lite-image"):
            self.assertEqual(krea._resolve_model(None, True), krea.KREA_LARGE)
            self.assertEqual(krea._resolve_model(None, False), krea.KREA_MEDIUM)


class TestPromptBuilding(unittest.TestCase):
    def test_text2img_includes_scene_and_anchors(self):
        p = krea._build_text2img_prompt("a rusted watchtower on the horizon")
        self.assertIn("rusted watchtower", p)
        self.assertIn("NO TEXT", p.upper())
        self.assertLessEqual(len(p), 5000)

    def test_img2img_includes_continuity_language(self):
        p = krea._build_img2img_prompt("step through the doorway")
        self.assertIn("doorway", p)
        self.assertIn("style reference", p.lower())
        self.assertLessEqual(len(p), 5000)

    def test_time_of_day_injected(self):
        p = krea._build_text2img_prompt("empty road", time_of_day="dusk, overcast")
        self.assertIn("dusk", p)


class TestExtractUrls(unittest.TestCase):
    def test_result_urls_list_of_strings(self):
        job = {"result": {"urls": ["https://x/a.png", "https://x/b.png"]}}
        self.assertEqual(krea._extract_urls(job)[0], "https://x/a.png")

    def test_result_urls_list_of_dicts(self):
        job = {"result": {"urls": [{"url": "https://x/a.png"}]}}
        self.assertEqual(krea._extract_urls(job), ["https://x/a.png"])

    def test_top_level_urls(self):
        job = {"urls": ["https://x/top.png"]}
        self.assertEqual(krea._extract_urls(job), ["https://x/top.png"])

    def test_empty_when_missing(self):
        self.assertEqual(krea._extract_urls({}), [])
        self.assertEqual(krea._extract_urls({"result": {}}), [])


class TestReferenceUploadPath(unittest.TestCase):
    def test_prefers_small_sidecar_when_present(self):
        old = krea.USE_DOWNSAMPLED_FOR_IMG2IMG
        krea.USE_DOWNSAMPLED_FOR_IMG2IMG = True
        try:
            with tempfile.TemporaryDirectory() as d:
                full = Path(d) / "frame.png"
                small = Path(d) / "frame_small.png"
                full.write_bytes(b"x")
                small.write_bytes(b"x")
                self.assertEqual(krea._reference_upload_path(str(full)), str(small))
        finally:
            krea.USE_DOWNSAMPLED_FOR_IMG2IMG = old

    def test_falls_back_to_full_when_no_sidecar(self):
        with tempfile.TemporaryDirectory() as d:
            full = Path(d) / "frame.png"
            full.write_bytes(b"x")
            self.assertEqual(krea._reference_upload_path(str(full)), str(full))


class TestNoApiKeyShortCircuit(unittest.TestCase):
    def setUp(self):
        self._old_key = krea.KREA_API_KEY

    def tearDown(self):
        krea.KREA_API_KEY = self._old_key

    def test_text2img_returns_none_without_key(self):
        krea.KREA_API_KEY = ""
        self.assertIsNone(krea.generate_with_krea("prompt", "caption"))

    def test_img2img_returns_none_without_key(self):
        krea.KREA_API_KEY = ""
        self.assertIsNone(krea.generate_krea_img2img("prompt", "caption", "ref.png"))


class TestProviderManagerWiring(unittest.TestCase):
    def test_krea_presets_available(self):
        presets = apm.get_available_presets()
        self.assertIn("krea", presets)
        self.assertIn("krea_large", presets)
        self.assertEqual(presets["krea"]["image_provider"], "krea")
        self.assertEqual(presets["krea"]["image_model"], "krea-2/medium")
        self.assertEqual(presets["krea_large"]["image_model"], "krea-2/large")


if __name__ == "__main__":
    unittest.main()
