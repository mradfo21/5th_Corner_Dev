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
        self.assertIn("finished photograph", p.lower())
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


class TestAspectRatioIsClampedToWhatKreaAccepts(unittest.TestCase):
    """The renderer's ratio used to be forwarded to Krea unchecked. The
    project ships at 21:9, which Krea rejects outright:

        HTTP 422 {"field":"aspect_ratio","message":"Invalid option:
        expected one of "1:1"|"4:3"|"3:2"|"16:9"|"2.35:1"|...}

    so every single Krea request failed validation before a pixel was
    generated, and the provider silently fell through to the Gemini safety
    net. Selecting Krea rendered nothing by Krea, ever."""

    def test_the_shipped_ratio_is_rejected_by_krea_raw(self):
        self.assertNotIn("21:9", krea.KREA_ASPECT_RATIOS)

    def test_it_lands_on_the_nearest_cinematic_ratio(self):
        self.assertEqual(krea._nearest_krea_aspect("21:9"), "2.35:1")

    def test_supported_ratios_pass_through_untouched(self):
        for r in krea.KREA_ASPECT_RATIOS:
            self.assertEqual(krea._nearest_krea_aspect(r), r)

    def test_anything_the_renderer_can_produce_is_sendable(self):
        for r in ("21:9", "16:9", "4:3", "1:1", "9:16", "3:2", "2:3",
                  "garbage", "", None):
            self.assertIn(krea._nearest_krea_aspect(r), krea.KREA_ASPECT_RATIOS,
                          f"{r!r} would 422")

    def test_the_payload_builder_never_emits_an_unsupported_ratio(self):
        self.assertIn(krea._krea_aspect("21:9"), krea.KREA_ASPECT_RATIOS)
        self.assertIn(krea._krea_aspect(), krea.KREA_ASPECT_RATIOS)


class TestKreaIsNotHandedTheBlurredSwatch(unittest.TestCase):
    """`make_style_swatch` destroys a frame's geometry on purpose — it is a
    workaround for Gemini copying the composition out of any legible photo.
    Krea has no such problem: references go into `image_style_references`,
    a real style channel. Handing THAT a blurred colour field makes the blur
    the style, and Krea returns a smear with no scene in it."""

    def setUp(self):
        self.src = Path("engine.py").read_text(encoding="utf-8")
        start = self.src.index('active_image_provider == "krea"')
        self.branch = self.src[start:start + 4000]

    def test_the_krea_hard_cut_does_not_build_a_swatch(self):
        self.assertNotIn("make_style_swatch", self.branch)

    def test_the_krea_hard_cut_sends_the_sharp_previous_frame(self):
        self.assertIn("ref_images_to_use = prev_img_paths_list[:1]", self.branch)

    def test_gemini_still_gets_its_swatch(self):
        """The workaround is still correct for the provider it was written
        for — this must not be read as 'swatches were a mistake'."""
        gem = self.src[:self.src.index('active_image_provider == "krea"')]
        self.assertIn("make_style_swatch", gem)


class TestTheSwatchIsNeverShownAsAFrame(unittest.TestCase):
    """The swatch is written next to the frame it was derived from, so the
    tape's directory scan served it as if it were a still."""

    def test_the_tape_filters_swatches_out(self):
        src = Path("api.py").read_text(encoding="utf-8")
        self.assertIn("_styleswatch.png", src)
        scan = src[src.index("img_dir.glob('*.png')"):][:600]
        self.assertIn("not p.name.endswith('_styleswatch.png')", scan)


class TestProviderManagerWiring(unittest.TestCase):
    def test_krea_presets_available(self):
        presets = apm.get_available_presets()
        self.assertIn("krea", presets)
        self.assertNotIn("krea_large", presets)
        self.assertEqual(presets["krea"]["image_provider"], "krea")
        self.assertEqual(presets["krea"]["image_model"], "krea-2/medium")


if __name__ == "__main__":
    unittest.main()
