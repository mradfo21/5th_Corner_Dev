"""
test_detect_filter.py — unit tests for the "underwhelming label" filter that
keeps things like the player's own gloved hands, the steering wheel/dashboard,
and the camcorder out of the SCAN hotspot overlay and the "photograph the X"
objectives derived from it (see engine._is_underwhelming_label,
engine._detect_objects, and the mirrored filter in gemini_live_vision.py).

Run with:
    python3 -m pytest test_detect_filter.py -v
"""

import os
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
        for label in ["camcorder", "handheld camera", "viewfinder", "camera lens",
                      "microphone", "mic", "headset"]:
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

    def test_evidence_on_the_ground_is_not_filtered_as_the_players_own_body(self):
        """The filter used to run the whole body down to the footwear, so a boot
        on the ground or a leg sticking out from under something — the most
        interesting thing in a horror frame — was thrown away before it could be
        tagged. Only what is genuinely in EVERY frame (the hands holding the
        camera, and the arms attached) stays filtered."""
        for label in ["boot", "boots", "shoe", "single shoe", "leg", "legs",
                      "bare foot", "knee", "shoulder", "torn sleeve"]:
            self.assertFalse(engine._is_underwhelming_label(label),
                             f"{label!r} is evidence, not the player's own body")
        for label in ["hand", "gloved hands", "forearm", "elbow", "jacket cuff"]:
            self.assertTrue(engine._is_underwhelming_label(label),
                            f"{label!r} is the camera operator and should stay filtered")


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


class TestPeopleSurviveTheFilters(unittest.TestCase):
    """SCAN's whole point is naming what's out there, and the thing it was worst
    at naming was a person. Two rules were eating them."""

    def test_a_person_standing_in_front_of_you_is_not_mistaken_for_your_own_arm(self):
        """The geometry backstop rejected anything touching the bottom edge that
        was half the frame tall and taller than wide — which is the shape of the
        camera operator's arm AND the shape of somebody standing a few metres
        away. It was silently dropping the most interesting thing in the frame.

        First-person only: the same box in third person IS the follow-cam
        subject and must not become a MOVE TO yourself tag.
        """
        person = {"label": "figure", "box_2d": [180, 340, 1000, 660],
                  "kind": "person", "speaks": True}
        with patch.object(engine.game_identity, "shows_character", return_value=False):
            objects = engine._normalize_detections([person])
        self.assertEqual([o["label"] for o in objects], ["figure"])
        self.assertTrue(objects[0]["speaks"])

    def test_the_operators_own_camera_column_is_still_rejected(self):
        """The backstop still has a job: a narrow column hugging the very bottom
        of the frame, that the detector did NOT call a living thing."""
        rig = {"label": "device", "box_2d": [350, 430, 1000, 560],
               "kind": "object", "speaks": False}
        self.assertEqual(engine._normalize_detections([rig]), [])

    def test_a_crowd_does_not_collapse_into_one_tag(self):
        """Dedupe keyed on the label alone, so three people — all of them
        labelled 'person' — came back as one."""
        crowd = [
            {"label": "person", "box_2d": [300, 100, 700, 220],
             "kind": "person", "speaks": True},
            {"label": "person", "box_2d": [300, 450, 700, 570],
             "kind": "person", "speaks": True},
            {"label": "person", "box_2d": [300, 780, 700, 900],
             "kind": "person", "speaks": True},
        ]
        self.assertEqual(len(engine._normalize_detections(crowd)), 3)

    def test_the_same_thing_twice_in_one_place_still_dedupes(self):
        """...but two boxes on the same label in the same spot are one thing
        seen twice, which is what dedupe is for."""
        double = [
            {"label": "valve", "box_2d": [300, 300, 400, 400],
             "kind": "object", "speaks": False},
            {"label": "valve", "box_2d": [305, 302, 405, 402],
             "kind": "object", "speaks": False},
        ]
        self.assertEqual(len(engine._normalize_detections(double)), 1)

    def test_the_cap_leaves_room_for_people_and_props(self):
        """Eight was spent on props before the people were reached."""
        self.assertGreaterEqual(engine.DETECT_MAX_ITEMS, 12)


class TestTruncatedDetectionResponseIsSalvaged(unittest.TestCase):
    """A busy frame produces a long JSON array, and a long array can hit the
    output-token ceiling and end mid-object. Parsing the whole string then
    throws, and the original code discarded EVERY object in the response — so
    SCAN on a room full of monitors, floppy disks and a door returned no tags
    at all. "The response was cut short" and "there is nothing here" have to
    stop being the same empty list.

    Caught by the scan-driven playtester: four consecutive interior turns came
    back with zero detections while the frames were visibly full of props.
    """

    TRUNCATED = (
        '[\n'
        '  {\n'
        '    "label": "television",\n'
        '    "box_2d": [222, 53, 484, 196],\n'
        '    "kind": "machine",\n'
        '    "speaks": false\n'
        '  },\n'
        '  {\n'
        '    "label": "cork board",\n'
        '    "box_2d": [286, 218, 444, 385],\n'
        '    "kind": "object",\n'
        '    "speaks": false\n'
        '  },\n'
        '  {\n'
        '    "label": "floppy dis'
    )

    def test_complete_objects_survive_a_truncated_array(self):
        salvaged = engine._salvage_json_array_objects(self.TRUNCATED)
        self.assertEqual([o["label"] for o in salvaged], ["television", "cork board"])

    def test_the_partial_trailing_object_is_dropped(self):
        for obj in engine._salvage_json_array_objects(self.TRUNCATED):
            self.assertIn("box_2d", obj)

    def test_salvaged_objects_still_go_through_the_filter(self):
        objects = engine._normalize_detections(
            engine._salvage_json_array_objects(self.TRUNCATED))
        self.assertEqual([o["label"] for o in objects], ["television", "cork board"])

    def test_a_brace_inside_a_label_does_not_break_the_scan(self):
        text = '[{"label": "sign {closed}", "box_2d": [1, 2, 3, 4], "kind": "object"}, {"lab'
        salvaged = engine._salvage_json_array_objects(text)
        self.assertEqual([o["label"] for o in salvaged], ["sign {closed}"])

    def test_an_escaped_quote_inside_a_label_does_not_break_the_scan(self):
        text = r'[{"label": "the \"lab\"", "box_2d": [1, 2, 3, 4], "kind": "object"}, {"lab'
        salvaged = engine._salvage_json_array_objects(text)
        self.assertEqual([o["label"] for o in salvaged], ['the "lab"'])

    def test_nothing_recoverable_returns_empty(self):
        self.assertEqual(engine._salvage_json_array_objects('[{"lab'), [])
        self.assertEqual(engine._salvage_json_array_objects(""), [])

    def test_a_complete_array_is_unaffected(self):
        import json as _json
        objs = [{"label": "door", "box_2d": [1, 2, 3, 4], "kind": "object", "speaks": False}]
        self.assertEqual(engine._salvage_json_array_objects(_json.dumps(objs)), objs)


class TestDetectionTokenBudgetFitsTheItemCap(unittest.TestCase):
    """The ceiling has to be budgeted against DETECT_MAX_ITEMS. A flat 700
    tokens could not fit 12 pretty-printed objects, which is what made busy
    scenes truncate in the first place — raising the cap without raising the
    budget just moves the cliff."""

    def test_budget_scales_with_the_item_cap(self):
        source = _detect_gemini_source()
        self.assertIn("max(700, 64 + max_items * 64)", source,
                      "detection maxOutputTokens must scale with max_items")

    def test_budget_covers_a_full_slate_of_detections(self):
        # ~45 tokens per pretty-printed object, so a full slate needs room for
        # DETECT_MAX_ITEMS of them plus the array scaffolding.
        budget = max(700, 64 + engine.DETECT_MAX_ITEMS * 64)
        self.assertGreater(budget, engine.DETECT_MAX_ITEMS * 45)

    def test_prompt_forbids_an_empty_scan_of_a_populated_frame(self):
        source = _detect_gemini_source()
        self.assertIn("Never return an empty list", source)


def _detect_gemini_source() -> str:
    import inspect
    return inspect.getsource(engine._detect_objects_gemini)


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


class TestPlayerSelfLabelsAreDroppedInThirdPerson(unittest.TestCase):
    """SCAN tagging the followed body as 'player character' is how a move
    invents a second, often differently-gendered figure. The detect prompt
    already asks not to; this is the code backstop."""

    def test_generic_self_tags_are_dropped_when_the_body_is_on_screen(self):
        with patch.object(engine.game_identity, "shows_character", return_value=True), \
             patch.object(engine.game_identity, "display_name", return_value="Jason Fleece"):
            for label in ["player character", "the player", "protagonist",
                          "Jason Fleece", "jason", "fleece"]:
                self.assertTrue(engine._is_player_self_label(label), label)

    def test_other_people_are_not_dropped(self):
        with patch.object(engine.game_identity, "shows_character", return_value=True), \
             patch.object(engine.game_identity, "display_name", return_value="Jason Fleece"):
            for label in ["injured man", "soldier", "person", "figure", "guard"]:
                self.assertFalse(engine._is_player_self_label(label), label)

    def test_self_tags_survive_in_first_person(self):
        with patch.object(engine.game_identity, "shows_character", return_value=False):
            self.assertFalse(engine._is_player_self_label("player character"))

    def test_viewfinder_self_rule_is_hands_even_in_third_person(self):
        with patch.object(engine.game_identity, "shows_character", return_value=True), \
             patch.object(engine.game_identity, "display_name", return_value="Jason Fleece"):
            rule = engine._detect_self_rule(viewfinder=True)
        self.assertIn("viewer's own hands", rule)
        self.assertNotIn("Jason Fleece", rule)

    def test_third_person_self_rule_names_the_world_not_the_followed_body(self):
        """A 3P follow-cam shot used to come back as `person` + `microphone`
        because the prompt said tag the foreground body if unsure, and tag
        their gear. Watch never hits this path, so those options looked
        right there and wrong on a world tap."""
        with patch.object(engine.game_identity, "shows_character", return_value=True), \
             patch.object(engine.game_identity, "display_name", return_value="Wren Alvarez"):
            rule = engine._detect_self_rule()
        self.assertIn("Wren Alvarez", rule)
        self.assertIn("Do NOT tag Wren Alvarez", rule)
        self.assertIn("wearing or carrying", rule.lower())
        self.assertNotIn("tag them anyway", rule.lower())
        self.assertIn("buildings, vehicles", rule.lower())

    def test_normalize_drops_the_self_tag(self):
        with patch.object(engine.game_identity, "shows_character", return_value=True), \
             patch.object(engine.game_identity, "display_name", return_value="Jason Fleece"):
            out = engine._normalize_detections([
                {"label": "player character", "box_2d": [100, 200, 800, 500],
                 "kind": "person", "speaks": False},
                {"label": "pickup truck", "box_2d": [400, 400, 700, 800],
                 "kind": "object", "speaks": False},
            ])
        labels = [o["label"] for o in out]
        self.assertEqual(labels, ["pickup truck"])


class TestFollowCamIsNotAnExplorableRegion(unittest.TestCase):
    """World-tap SCAN in third person was tagging the followed body as
    `person` and their radio as `microphone`. Watch looks fine because it
    paints recorded landmarks, not this live detect pass."""

    RAW = [
        {"label": "person", "box_2d": [220, 360, 980, 620],
         "kind": "person", "speaks": True},
        {"label": "microphone", "box_2d": [480, 430, 620, 540],
         "kind": "object", "speaks": False},
        {"label": "person", "box_2d": [420, 780, 520, 860],
         "kind": "person", "speaks": True},
        {"label": "salvage barn", "box_2d": [80, 50, 420, 480],
         "kind": "object", "speaks": False},
        {"label": "pickup truck", "box_2d": [380, 520, 560, 820],
         "kind": "object", "speaks": False},
    ]

    def test_follow_cam_body_and_worn_kit_are_dropped(self):
        with patch.object(engine.game_identity, "shows_character", return_value=True), \
             patch.object(engine.game_identity, "display_name", return_value="Jason Fleece"):
            labels = [o["label"] for o in engine._normalize_detections(self.RAW)]
        self.assertEqual(labels, ["salvage barn", "pickup truck"])

    def test_a_named_npc_off_to_the_side_is_kept(self):
        raw = [
            {"label": "person", "box_2d": [220, 360, 980, 620],
             "kind": "person", "speaks": True},
            {"label": "guard", "box_2d": [300, 50, 700, 180],
             "kind": "person", "speaks": True},
            {"label": "chain-link fence", "box_2d": [200, 20, 700, 280],
             "kind": "object", "speaks": False},
        ]
        with patch.object(engine.game_identity, "shows_character", return_value=True):
            labels = [o["label"] for o in engine._normalize_detections(raw)]
        self.assertIn("guard", labels)
        self.assertIn("chain-link fence", labels)
        self.assertNotIn("person", labels)

    def test_first_person_still_keeps_a_person_in_front_of_you(self):
        person = {"label": "figure", "box_2d": [180, 340, 1000, 660],
                  "kind": "person", "speaks": True}
        with patch.object(engine.game_identity, "shows_character", return_value=False):
            labels = [o["label"] for o in engine._normalize_detections([person])]
        self.assertEqual(labels, ["figure"])

    def test_viewfinder_does_not_strip_a_foreground_person(self):
        person = {"label": "figure", "box_2d": [180, 340, 1000, 660],
                  "kind": "person", "speaks": True}
        with patch.object(engine.game_identity, "shows_character", return_value=True):
            labels = [o["label"] for o in engine._normalize_detections(
                [person], viewfinder=True)]
        self.assertEqual(labels, ["figure"])

    def test_include_self_keeps_the_followed_body_for_the_leak_check(self):
        person = {"label": "person", "box_2d": [220, 360, 980, 620],
                  "kind": "person", "speaks": True}
        with patch.object(engine.game_identity, "shows_character", return_value=True):
            labels = [o["label"] for o in engine._normalize_detections(
                [person], include_self=True)]
        self.assertEqual(labels, ["person"])

    def test_a_radio_on_a_crate_is_not_worn_kit(self):
        raw = [
            {"label": "radio", "box_2d": [400, 50, 520, 160],
             "kind": "machine", "speaks": True},
            {"label": "wooden crate", "box_2d": [420, 40, 700, 220],
             "kind": "object", "speaks": False},
        ]
        with patch.object(engine.game_identity, "shows_character", return_value=True):
            labels = [o["label"] for o in engine._normalize_detections(raw)]
        self.assertIn("radio", labels)
        self.assertIn("wooden crate", labels)


class TestDetectScenePriorIsTheLiveFrame(unittest.TestCase):
    """SCAN / MOVE TO must not be primed with the last still's render recipe.

    current_image_prompt is a camera-grammar + previous-location blob. Passing
    it as "this frame was rendered from…" made Gemini (and local prompt-anchor)
    keep offering the pink sedan after the live video had moved on.
    """

    def test_observed_vision_is_the_only_prior(self):
        st = {
            "current_observed_vision": "a rusted silo beside a chain-link fence",
            "current_image_prompt": "CAMERA: THIRD-PERSON… pink sedan… warehouse…",
        }
        self.assertEqual(engine._detect_scene_prior(st),
                         "a rusted silo beside a chain-link fence")

    def test_still_recipe_is_never_a_prior(self):
        st = {"current_image_prompt": "CAMERA: THIRD-PERSON… pink sedan… warehouse…"}
        self.assertEqual(engine._detect_scene_prior(st), "")

    def test_empty_or_non_dict_is_empty(self):
        self.assertEqual(engine._detect_scene_prior({}), "")
        self.assertEqual(engine._detect_scene_prior(None), "")
        self.assertEqual(engine._detect_scene_prior("x"), "")


class TestSniffImageMime(unittest.TestCase):
    """Observed frames are JPEG bytes; older builds named them .png."""

    def test_jpeg_magic_wins_over_png_extension(self, tmp_path=None):
        import tempfile
        jpeg = (
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01"
            b"\x00\x00\xff\xd9"
        )
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(jpeg)
            path = f.name
        try:
            self.assertEqual(engine._sniff_image_mime(path), "image/jpeg")
        finally:
            os.unlink(path)

    def test_png_magic(self):
        import tempfile
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(png)
            path = f.name
        try:
            self.assertEqual(engine._sniff_image_mime(path), "image/png")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
