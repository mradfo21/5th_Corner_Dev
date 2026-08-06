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
from unittest import mock
from pathlib import Path

import requests

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
        #
        # The negative is built per-call now (_negative_prompt) rather than
        # being one frozen _SDXL_NEGATIVE_PROMPT constant, because "no people"
        # is only correct while the camera is nobody's eyes — see
        # game_identity. This test was still reaching for the old constant.
        p = fal._build_text2img_prompt("a rusted watchtower on the horizon")
        self.assertNotIn("NO TEXT", p.upper())
        neg = fal._negative_prompt().lower()
        for term in ("text", "border", "person"):
            self.assertIn(term, neg)

    def test_negative_prompt_stops_banning_people_in_third_person(self):
        """The anti-person tags are the one part of the negative that has to
        follow the camera: keep them in a third-person mode and SDXL is being
        told to omit the character the player asked to see."""
        with mock.patch.object(fal.game_identity, "shows_character", return_value=True):
            neg = fal._negative_prompt().lower()
        self.assertIn("text", neg)          # the anti-text bans survive
        self.assertIn("border", neg)
        self.assertNotIn("person, people", neg)
        self.assertIn("empty scene with no character", neg)

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


class TestCallFalSelfHeal(unittest.TestCase):
    """If fal rejects an optional field (e.g. negative_prompt), _call_fal must
    strip it and retry so generation never fully breaks."""

    def setUp(self):
        self._old_key = fal.FAL_API_KEY
        fal.FAL_API_KEY = "test-key"

    def tearDown(self):
        fal.FAL_API_KEY = self._old_key

    def _make_422(self):
        resp = mock.Mock()
        resp.status_code = 422
        resp.text = "unknown field negative_prompt"
        err = requests.exceptions.HTTPError(response=resp)
        resp.raise_for_status.side_effect = err
        return resp

    def _make_ok(self):
        resp = mock.Mock()
        resp.status_code = 200
        resp.raise_for_status.side_effect = None
        resp.json.return_value = {"images": [{"url": "https://x/a.png"}]}
        return resp

    def test_retries_without_optional_fields_on_422(self):
        posts = [self._make_422(), self._make_ok()]
        with mock.patch.object(fal.requests, "post", side_effect=posts) as p, \
             mock.patch.object(fal, "_download_and_save", return_value="/tmp/a.png") as dl:
            out = fal._call_fal(
                fal.FAL_MODEL,
                {"prompt": "scene", "negative_prompt": "text", "num_inference_steps": 8},
                "cap",
                None,
            )
        self.assertEqual(out, "/tmp/a.png")
        self.assertEqual(p.call_count, 2)
        # Second (retry) call must have dropped negative_prompt.
        retry_body = p.call_args_list[1].kwargs["json"]
        self.assertNotIn("negative_prompt", retry_body)
        self.assertIn("prompt", retry_body)
        dl.assert_called_once()


class TestProviderManagerWiring(unittest.TestCase):
    def test_fal_preset_available(self):
        presets = apm.get_available_presets()
        self.assertIn("fal", presets)
        self.assertEqual(presets["fal"]["image_provider"], "fal")
        self.assertEqual(presets["fal"]["image_model"], "fal-ai/fast-lightning-sdxl")


if __name__ == "__main__":
    unittest.main()
