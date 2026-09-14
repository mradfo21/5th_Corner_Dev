"""Encounter resolve must hard-cut: unique files, no enter-plate img2img.

The session exhibit (default history, 2026-09-01) is four different verbs
writing the same ``encounter_resolve_Wearing_a_tattered_high-visibi.png``
because the caption was only the clothing-clause label and the generator
img2img'd the enter standoff every time.

Pure functions + one temp dir — no network, no API key.

Run with:
    python -m unittest test_encounter -v
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")

import encounter


PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00"
    b"\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx"
    b"\x9cc\xf8\xcf\xc0P\x0f\x00\x04\x85\x01\x80\xa1\xa9\x8c!\x00"
    b"\x00\x00\x00IEND\xaeB`\x82"
)


class TestResolvePlateCaption(unittest.TestCase):
    def test_different_verbs_get_different_stems(self):
        a = encounter.resolve_plate_caption("A stranger", "Shove them", "confront",
                                            "survive", nonce="1")
        b = encounter.resolve_plate_caption("A stranger", "Break away", "evade",
                                            "escape", nonce="1")
        self.assertNotEqual(a, b)
        self.assertIn("Shove", a)
        self.assertIn("Break", b)

    def test_caption_is_not_the_clothing_label_alone(self):
        cap = encounter.resolve_plate_caption(
            "Wearing a tattered high-visibility blue",
            "Strike the hostile man", "confront", "survive", nonce="9",
        )
        self.assertTrue(cap.startswith("encounter_resolve_"))
        self.assertIn("Strike", cap)
        self.assertNotEqual(cap, "encounter_resolve_Wearing a tattered high-visibility blue")


class TestClothingClauseLabels(unittest.TestCase):
    def test_wearing_clause_is_rejected(self):
        self.assertTrue(encounter._is_clothing_clause_label(
            "Wearing a tattered high-visibility blue vest"))
        self.assertEqual(
            encounter._grounded_label_from_look(
                "wearing a tattered high-visibility blue vest over a stained shirt",
            ),
            "A stranger",
        )

    def test_person_noun_plus_clothes_is_a_name(self):
        label = encounter._grounded_label_from_look(
            "a wary man wearing a tattered high-visibility blue vest",
        )
        self.assertTrue(label.lower().startswith("a man"))
        self.assertFalse(encounter._is_clothing_clause_label(label))

    def test_normalize_rewrites_a_clothing_label(self):
        brief = encounter.normalize_encounter_brief({
            "character": {
                "label": "Wearing a tattered high-visibility blue vest",
                "look": "a wary man in a knit cap",
                "stance": "hostile",
            },
            "danger": "they are already close",
            "stakes": "If you hesitate you will not walk away clean.",
        })
        self.assertFalse(encounter._is_clothing_clause_label(brief["character"]["label"]))
        self.assertIn("man", brief["character"]["label"].lower())

    def test_fallback_choices_do_not_name_a_vest(self):
        choices = encounter.fallback_encounter_choices({
            "character": {"label": "Wearing a tattered"},
        })
        blob = " ".join(c["text"] for c in choices).lower()
        self.assertNotIn("wearing", blob)
        self.assertIn("them", blob)


class TestStakesAfterVerb(unittest.TestCase):
    def test_stakes_name_the_verb_not_the_sludge(self):
        brief = {
            "character": {"label": "A stranger"},
            "stakes": "If you hesitate the caustic sludge will surge over your boots.",
        }
        out = encounter.stakes_after_verb(brief, "Strike the hostile man",
                                          "confront", "survive")
        self.assertIn("Strike", out)
        self.assertNotIn("sludge", out)

    def test_escape_stakes_say_you_broke_clear(self):
        out = encounter.stakes_after_verb(
            {"character": {"label": "A stranger"}},
            "Break away", "evade", "escape",
        )
        self.assertIn("clear", out.lower())


class TestResolvePromptIsAHardCut(unittest.TestCase):
    def test_prompt_does_not_ask_to_copy_the_still(self):
        prompt = encounter.build_encounter_resolve_prompt(
            {
                "character": {
                    "label": "A stranger",
                    "look": "a wary man in a knit cap",
                    "stance": "hostile",
                },
                "danger": "they are already close",
                "stakes": "Hesitation costs you.",
            },
            "Strike the hostile man", "confront", "survive",
        )
        low = prompt.lower()
        self.assertNotIn("only change pose", low)
        self.assertNotIn("copy them from the still", low)
        self.assertIn("hard cut", low)

    def test_escape_turn_text_asks_for_the_place_after(self):
        text = encounter.encounter_action_for_turn(
            "Break away",
            {"character": {"label": "A stranger"}},
            lane="evade", outcome="escape",
        )
        self.assertIn("AFTER they got clear", text)


class TestEncounterSkipImage(unittest.TestCase):
    def test_escape_generates_an_aftermath_frame(self):
        self.assertFalse(encounter.encounter_turn_skip_image("escape"))

    def test_survive_and_die_keep_the_verb_still(self):
        self.assertTrue(encounter.encounter_turn_skip_image("survive"))
        self.assertTrue(encounter.encounter_turn_skip_image("wounded"))
        self.assertTrue(encounter.encounter_turn_skip_image("die"))


class TestResolveRestagesTheEnterPlate(unittest.TestCase):
    """The enter plate IS the reference, and the caption is per-verb.

    This class used to assert the opposite: the plate was reduced to a colour
    swatch so the resolve could not be a pose-edit of the standoff. That did
    stop the pose-edit, but it left the punch with no visual memory at all —
    generated from text, it came back with two people who were not the two in
    the fight, standing apart, in a different room. The duplicate-file half of
    the original bug was never the reference anyway; it was the caption, which
    is still asserted below.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.enter = Path(self._tmpdir.name) / "enter_plate.png"
        self.enter.write_bytes(PNG)
        self.enter_path = str(self.enter.resolve())
        self.swatch = Path(self._tmpdir.name) / "enter_plate_styleswatch.png"
        self.swatch.write_bytes(PNG)
        self.calls = []

    def tearDown(self):
        self._tmpdir.cleanup()

    def _record(self, label):
        def stub(*args, **kwargs):
            refs = kwargs.get("reference_image_path")
            if refs is None:
                refs = args[2] if len(args) > 2 else []
            if isinstance(refs, str):
                refs = [refs]
            caption = kwargs.get("caption") or (args[1] if len(args) > 1 else "")
            self.calls.append({
                "label": label,
                "refs": [str(Path(r).resolve()) for r in (refs or [])],
                "caption": caption,
                "style_only": bool(kwargs.get("style_only_swatch")),
            })
            return str(Path(self._tmpdir.name) / f"{label}_{len(self.calls)}.png")
        return stub

    def test_enter_plate_is_the_img2img_init(self):
        import gemini_image_utils as giu
        orig = (giu.generate_gemini_img2img, giu.generate_with_gemini,
                giu.make_style_swatch)
        giu.generate_gemini_img2img = self._record("img2img")
        giu.generate_with_gemini = self._record("t2i")
        giu.make_style_swatch = lambda *a, **k: str(self.swatch)
        brief = {
            "character": {"label": "A stranger"},
            "place_hold": "industrial yard",
        }
        import engine
        try:
            with mock.patch.object(engine, "IMAGE_ENABLED", True):
                with mock.patch.object(engine, "_load_state", return_value={"time_of_day": ""}):
                    with mock.patch.object(engine, "_get_image_dir",
                                           return_value=self._tmpdir.name):
                        with mock.patch.object(encounter, "encounter_identity_paths",
                                               return_value=[]):
                            path, mode = encounter._generate_resolve_plate(
                                "test", brief, "Strike them now.",
                                self.enter_path,
                                verb="Strike the hostile man",
                                lane="confront",
                                outcome="survive",
                            )
        finally:
            (giu.generate_gemini_img2img, giu.generate_with_gemini,
             giu.make_style_swatch) = orig
        self.assertTrue(self.calls, "no image call was recorded")
        img2img = [c for c in self.calls if c["label"] == "img2img"]
        self.assertTrue(img2img, self.calls)
        self.assertIn(self.enter_path, img2img[0]["refs"],
                      "the resolve lost the only record of who is in this fight")
        self.assertFalse(img2img[0]["style_only"],
                         "a style swatch keeps the palette and drops the cast")
        # The duplicate-file bug: one caption for every verb.
        self.assertIn("Strike", img2img[0]["caption"])
        self.assertTrue(path)
        self.assertIn("hard_cut", mode)

    def test_two_verbs_request_two_captions(self):
        import gemini_image_utils as giu
        orig = (giu.generate_gemini_img2img, giu.generate_with_gemini,
                giu.make_style_swatch)
        giu.generate_gemini_img2img = self._record("img2img")
        giu.generate_with_gemini = self._record("t2i")
        giu.make_style_swatch = lambda *a, **k: str(self.swatch)
        brief = {"character": {"label": "A stranger"}, "place_hold": ""}
        import engine
        try:
            with mock.patch.object(engine, "IMAGE_ENABLED", True):
                with mock.patch.object(engine, "_load_state", return_value={}):
                    with mock.patch.object(engine, "_get_image_dir",
                                           return_value=self._tmpdir.name):
                        with mock.patch.object(encounter, "encounter_identity_paths",
                                               return_value=[]):
                            encounter._generate_resolve_plate(
                                "test", brief, "Shove.", self.enter_path,
                                verb="Shove them", lane="confront",
                                outcome="survive",
                            )
                            encounter._generate_resolve_plate(
                                "test", brief, "Break.", self.enter_path,
                                verb="Break away", lane="evade",
                                outcome="escape",
                            )
        finally:
            (giu.generate_gemini_img2img, giu.generate_with_gemini,
             giu.make_style_swatch) = orig
        captions = [c["caption"] for c in self.calls if c["label"] == "img2img"]
        self.assertEqual(len(captions), 2)
        self.assertNotEqual(captions[0], captions[1])


if __name__ == "__main__":
    unittest.main()
