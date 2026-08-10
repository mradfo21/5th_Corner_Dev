"""
test_detect_filter.py — unit tests for the "underwhelming label" filter that
keeps things like the player's own gloved hands, the steering wheel/dashboard,
and the camcorder out of the SCAN hotspot overlay and the "photograph the X"
objectives derived from it (see engine._is_underwhelming_label,
engine._detect_objects, and the mirrored filter in gemini_live_vision.py).

Run with:
    python3 -m pytest test_detect_filter.py -v
"""

import unittest
from unittest.mock import MagicMock, patch

import engine


class TestIsUnderwhelmingLabel(unittest.TestCase):
    """Direct unit tests for engine._is_underwhelming_label."""

    def test_pov_body_parts_are_filtered(self):
        for label in [
            "hand", "hands", "gloved hand", "gloved hands", "glove", "gloves",
            "finger", "fingers", "thumb", "palm", "wrist", "knuckle", "fist",
            "arm", "arms", "forearm", "elbow", "driving glove", "leather gloves",
        ]:
            self.assertTrue(engine._is_underwhelming_label(label),
                             f"{label!r} should be filtered as an underwhelming POV body part")

    def test_vehicle_interior_is_filtered(self):
        for label in [
            "steering wheel", "dashboard", "dash", "gauge", "gauges",
            "speedometer", "odometer", "rearview mirror", "side mirror",
            "windshield", "wiper", "wipers", "gear shift", "gearshift",
            "handbrake", "seatbelt", "sun visor", "glove box", "car seat",
            "headrest",
        ]:
            self.assertTrue(engine._is_underwhelming_label(label),
                             f"{label!r} should be filtered as a vehicle-interior fixture")

    def test_own_recording_gear_is_filtered(self):
        for label in ["camcorder", "handheld camera", "viewfinder", "camera lens"]:
            self.assertTrue(engine._is_underwhelming_label(label),
                             f"{label!r} should be filtered as the player's own recording gear")

    def test_generic_background_is_filtered(self):
        for label in ["shadow", "shadows", "reflection", "dust", "haze", "glare",
                      "sunbeam", "sunlight", "horizon"]:
            self.assertTrue(engine._is_underwhelming_label(label),
                             f"{label!r} should be filtered as generic background")

    def test_interesting_objects_are_kept(self):
        """The whole point: real points of interest must NEVER be filtered."""
        for label in [
            "industrial facility", "organic growth", "abandoned vehicle",
            "steel door", "wooden crate", "rusty valve", "campfire",
            "figure", "stranger", "wolf", "radio tower", "security camera",
            "billboard", "gas station", "collapsed bridge", "tent", "wreckage",
        ]:
            self.assertFalse(engine._is_underwhelming_label(label),
                              f"{label!r} is a real point of interest and must NOT be filtered")

    def test_empty_or_whitespace_label_is_filtered(self):
        self.assertTrue(engine._is_underwhelming_label(""))
        self.assertTrue(engine._is_underwhelming_label("   "))
        self.assertTrue(engine._is_underwhelming_label(None))

    def test_case_and_article_insensitive(self):
        self.assertTrue(engine._is_underwhelming_label("THE STEERING WHEEL"))
        self.assertTrue(engine._is_underwhelming_label("A Camcorder"))
        self.assertTrue(engine._is_underwhelming_label("  Gloved Hands  "))

    def test_does_not_over_filter_unrelated_words(self):
        # Regression guard: words that merely contain a filtered substring but
        # aren't actually about hands/gloves/vehicle-interior must survive.
        for label in ["handrail", "handshake stone", "wheelchair", "flashlight"]:
            self.assertFalse(engine._is_underwhelming_label(label),
                              f"{label!r} should NOT be filtered (word-boundary check)")


class TestDetectObjectsFiltersUnderwhelmingLabels(unittest.TestCase):
    """Integration-style test: a mocked Gemini response mixing underwhelming
    and interesting labels must come back from _detect_objects with ONLY the
    interesting ones — matching the reported bug (hands/camcorder crowding out
    industrial facility/organic growth).

    Pins DETECT_BACKEND to "gemini": this exercises the Gemini path specifically,
    and the default backend is now the on-device detector, which would never
    reach the mocked HTTP session. The equivalent coverage for the local backend
    lives in test_local_vision.py.
    """

    def _mock_response(self, objects):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "candidates": [{
                "content": {"parts": [{"text": __import__("json").dumps(objects)}]},
            }],
        }
        return resp

    def test_underwhelming_objects_are_dropped_from_the_result(self):
        raw = [
            {"label": "hands", "box_2d": [400, 400, 600, 600], "kind": "object", "speaks": False},
            {"label": "camcorder", "box_2d": [100, 100, 200, 200], "kind": "object", "speaks": False},
            {"label": "steering wheel", "box_2d": [500, 300, 700, 500], "kind": "object", "speaks": False},
            {"label": "industrial facility", "box_2d": [50, 50, 300, 900], "kind": "object", "speaks": False},
            {"label": "organic growth", "box_2d": [600, 700, 800, 950], "kind": "object", "speaks": False},
        ]
        with patch.object(engine, "DETECT_BACKEND", "gemini"), \
             patch.object(engine, "LLM_ENABLED", True), \
             patch.object(engine, "VISION_ENABLED", True), \
             patch.object(engine, "GEMINI_API_KEY", "test-key"), \
             patch.object(engine._GEMINI_HTTP_SESSION, "post", return_value=self._mock_response(raw)):
            objects = engine._detect_objects(image_bytes=b"fake-jpeg-bytes", mime_type="image/jpeg")

        labels = {o["label"] for o in objects}
        self.assertIn("industrial facility", labels)
        self.assertIn("organic growth", labels)
        self.assertNotIn("hands", labels,
                          "underwhelming 'hands' must be filtered out of /api/detect results")
        self.assertNotIn("camcorder", labels,
                          "underwhelming 'camcorder' must be filtered out of /api/detect results")
        self.assertNotIn("steering wheel", labels,
                          "underwhelming 'steering wheel' must be filtered out of /api/detect results")
        self.assertEqual(len(objects), 2)


class TestLiveVisionParseFiltersUnderwhelmingLabels(unittest.TestCase):
    """The experimental Gemini Live API path (gemini_live_vision.py, opt-in via
    DETECT_LIVE_API=1) mirrors the same wire contract as /api/detect and must
    apply the identical underwhelming-label filter."""

    def test_parse_detection_payload_drops_underwhelming_labels(self):
        import json as _json
        import gemini_live_vision as glv

        raw = _json.dumps([
            {"label": "hands", "box_2d": [400, 400, 600, 600], "kind": "object", "speaks": False},
            {"label": "camcorder", "box_2d": [100, 100, 200, 200], "kind": "object", "speaks": False},
            {"label": "organic growth", "box_2d": [600, 700, 800, 950], "kind": "object", "speaks": False},
        ])
        objects = glv._parse_detection_payload(raw)
        labels = {o["label"] for o in objects}
        self.assertIn("organic growth", labels)
        self.assertNotIn("hands", labels)
        self.assertNotIn("camcorder", labels)


if __name__ == "__main__":
    unittest.main()
