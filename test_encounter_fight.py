"""An encounter has to read as a fight, not a slot machine with pictures.

Each class here pins one thing a play session got wrong:

* the nameplate, the prose, and the image were three different people,
  because the cast lock used the look the brief INVENTED rather than the one
  the plate actually drew;
* the player got mistaken for the enemy whenever the protagonist was not the
  one hardcoded PRESS-vest character;
* committing to a verb could not change the situation, so a fight either
  looped or killed you, and there was no way to win one;
* the action beat was rendered with the standoff's "locked-off two-shot"
  framing, so a landed punch came back as two people standing apart;
* a first-person world still demanded a two-shot, which left the player out
  of frame and handed the composition to the stranger.

Pure functions only - no network, no API key.

Run with:
    python -m unittest test_encounter_fight -v
"""

import os
import random
import re
import unittest
from unittest import mock

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")

import encounter


def _brief(**over):
    base = {
        "character": {
            "label": "A hooded scavenger",
            "kind": "person",
            "look": "an oversized grease-stained yellow raincoat, hood pulled low",
            "stance": "hostile",
        },
        "danger": "swinging a length of rebar at your head",
        "stakes": "You do not walk away clean.",
        "place_hold": "rusted pier deck, fog",
    }
    base.update(over)
    return encounter.normalize_encounter_brief(base)


class TestTheLockedCharacterIsTheOneOnScreen(unittest.TestCase):
    """The brief invents a look before any image exists. The plate is what
    the player saw, so the plate is what every later frame must lock to.

    Taken verbatim from the session that prompted this: the brief said
    "yellow raincoat", the plate drew a green combat jacket, and the resolve
    prompt then named both at once.
    """

    PLATE = (
        'A woman wearing a blue "PRESS" cap and tactical vest, and a man in a '
        "green combat jacket and beanie stand in the foreground. They occupy "
        "a metallic deck."
    )
    PLAYER = {
        "enabled": True,
        "name": "Wren Alvarez",
        "wardrobe": 'blue "PRESS" cap, tactical vest',
        "appearance": "adult woman",
        "signature_gear": "1993 VHS camcorder",
    }

    def setUp(self):
        for target, value in (
            ("game_identity.authored_character", self.PLAYER),
            ("game_identity.display_name", "Wren Alvarez"),
        ):
            p = mock.patch(target, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def test_the_lock_follows_the_plate_not_the_invented_brief(self):
        out = encounter.adopt_plate_look(_brief(), self.PLATE)
        locked = out["character"]["locked_look"].lower()
        self.assertIn("green combat jacket", locked)
        self.assertNotIn("raincoat", locked)

    def test_the_look_and_the_nameplate_describe_the_same_person(self):
        out = encounter.adopt_plate_look(_brief(), self.PLATE)
        look = out["character"]["look"].lower()
        label = out["character"]["label"].lower()
        garment = re.search(r"\b(jacket|coat|vest|overcoat|raincoat)\b", label)
        self.assertIsNotNone(garment, f"nameplate names no garment: {label!r}")
        self.assertIn(garment.group(1), look)

    def test_a_plate_showing_only_the_player_keeps_the_briefs_look(self):
        """Promoting the protagonist to hostile is the worse failure."""
        player_only = (
            'A woman wearing a blue "PRESS" cap and tactical vest stands alone '
            "on a fog-covered metallic deck."
        )
        out = encounter.adopt_plate_look(_brief(), player_only)
        locked = out["character"]["locked_look"].lower()
        self.assertIn("raincoat", locked)
        self.assertNotIn("press", locked)


class TestThePlayerIsNotTheEnemy(unittest.TestCase):
    """Player detection was a literal set of one character's costume words."""

    JASON = {
        "enabled": True,
        "name": "Jason Fleece",
        "wardrobe": "olive field jacket, dark work pants, boots",
        "appearance": "adult man, short dark hair, stubble",
        "signature_gear": "1993 VHS camcorder",
    }

    def setUp(self):
        patcher = mock.patch("game_identity.authored_character", return_value=self.JASON)
        patcher.start()
        self.addCleanup(patcher.stop)
        name = mock.patch("game_identity.display_name", return_value="Jason Fleece")
        name.start()
        self.addCleanup(name.stop)

    def test_a_protagonist_who_wears_no_vest_is_still_recognised(self):
        self.assertTrue(
            encounter.look_clones_player(
                "The man on the left wears an olive field jacket and a shoulder bag"
            )
        )

    def test_naming_the_protagonist_counts(self):
        self.assertTrue(encounter.look_clones_player("Jason Fleece, camcorder raised"))

    def test_a_real_stranger_is_left_alone(self):
        self.assertFalse(
            encounter.look_clones_player("a man in a green combat jacket and beanie")
        )

    def test_where_they_stood_is_not_part_of_who_they_are(self):
        """A hard cut moves the camera, so "on the left" describes nobody."""
        look = encounter.plate_stranger_look(
            "The man on the left wears a brown baseball cap and a tan jacket",
            fallback="x",
        )
        self.assertNotIn("on the left", look)
        self.assertIn("brown baseball cap", look)

    def test_the_stranger_survives_a_plate_that_describes_the_player_first(self):
        seen = (
            "A third-person view over the shoulder of a man in an olive field "
            "jacket, facing a gaunt figure in a tattered grey overcoat gripping "
            "a length of rebar."
        )
        look = encounter.plate_stranger_look(seen, fallback="a wary figure").lower()
        self.assertIn("grey overcoat", look)
        self.assertNotIn("olive field jacket", look)


class TestTheFightEscalates(unittest.TestCase):
    """Rounds used to be independent and identical, so a verb changed nothing."""

    def test_pressing_a_confront_moves_the_other_body(self):
        rng = random.Random(1)
        self.assertEqual(
            encounter.advance_enemy_state("confront", "survive", "ready", rng=rng),
            "staggered",
        )

    def test_a_staggered_body_can_be_put_down(self):
        rng = random.Random(1)
        self.assertEqual(
            encounter.advance_enemy_state("confront", "survive", "staggered", rng=rng),
            "down",
        )

    def test_putting_them_down_ends_the_encounter(self):
        self.assertTrue(
            encounter.encounter_releases("survive", {"enemy_state": "down"})
        )

    def test_a_fight_that_is_still_going_does_not_release(self):
        self.assertFalse(
            encounter.encounter_releases("survive", {"enemy_state": "staggered"})
        )

    def test_pressing_an_advantage_beats_opening_cold(self):
        ready = encounter.encounter_outcome_weights("confront", enemy_state="ready")
        stag = encounter.encounter_outcome_weights("confront", enemy_state="staggered")
        self.assertGreater(stag["survive"], ready["survive"])
        self.assertLess(stag["die"], ready["die"])

    def test_opening_with_a_punch_is_not_a_coin_flip_on_death(self):
        w = encounter.encounter_outcome_weights("confront", "hostile", "person")
        self.assertLess(w["die"] / sum(w.values()), 0.15)

    def test_the_exchange_survives_a_normalize_round_trip(self):
        b = _brief()
        b["enemy_state"] = "staggered"
        b["round_no"] = 3
        again = encounter.normalize_encounter_brief(b)
        self.assertEqual(again["enemy_state"], "staggered")
        self.assertEqual(again["round_no"], 3)


class TestTheActionBeatIsShotLikeAFight(unittest.TestCase):

    def _resolve(self, **over):
        b = _brief()
        b.update(over)
        return encounter.build_encounter_resolve_prompt(
            b, "Drive your shoulder into him", "confront",
            over.pop("outcome", "survive"), setting="outdoor",
        )

    def test_the_action_beat_drops_the_standoff_framing(self):
        prompt = self._resolve(round_no=1, enemy_state="ready")
        self.assertNotIn("locked-off two-shot", prompt)
        self.assertIn("NOT two people standing apart", prompt)

    def test_the_camera_pushes_in_as_the_fight_goes_on(self):
        first = self._resolve(round_no=1, enemy_state="ready")
        later = self._resolve(round_no=3, enemy_state="staggered")
        self.assertIn("Medium shot", first)
        self.assertNotIn("Medium shot", later)

    def test_the_finish_reads_as_a_finish(self):
        prompt = self._resolve(round_no=2, enemy_state="down")
        self.assertIn("THIS IS THE FINISH", prompt)

    def test_the_standoff_plate_still_holds_the_place(self):
        prompt = encounter.build_encounter_plate_prompt(
            _brief(), img2img=True, setting="outdoor")
        self.assertIn("PLACE LOCK", prompt)


class TestFirstPersonDoesNotDrawASecondPlayer(unittest.TestCase):

    def _plate(self, shows_player):
        with mock.patch("game_identity.shows_character", return_value=shows_player):
            return encounter.build_encounter_plate_prompt(
                _brief(), img2img=True, setting="outdoor")

    def test_first_person_asks_for_one_figure_down_the_lens(self):
        prompt = self._plate(False)
        self.assertIn("FIRST-PERSON POV", prompt)
        self.assertIn("EXACTLY ONE person is visible", prompt)
        self.assertNotIn("TWO DISTINCT PEOPLE", prompt)

    def test_third_person_still_asks_for_the_two_shot(self):
        prompt = self._plate(True)
        self.assertIn("TWO DISTINCT PEOPLE", prompt)
        self.assertNotIn("FIRST-PERSON POV", prompt)


class TestTheCameraStopsFollowingThemWhenItIsOver(unittest.TestCase):
    """Breaking away was followed by turns of the camera trailing the person
    you just escaped, because the enemy stayed the object-permanence subject
    past the release."""

    SRC = open("encounter.py", encoding="utf-8").read()

    def test_the_subject_is_dropped_once_the_encounter_releases(self):
        self.assertIn(
            '"subject": None if released else engine._permanence_subject(subject)',
            self.SRC,
        )

    def test_a_released_encounter_is_cleared_from_the_state(self):
        engine_src = open("engine.py", encoding="utf-8").read()
        self.assertIn("_encounter.clear_encounter(SID)", engine_src)


class TestAWorldStitchDoesNotRecastYou(unittest.TestCase):
    """Most saved World snapshots carry an empty cast sheet, and loading one
    dropped the run's protagonist, so the player became the shipped one."""

    SRC = open("engine.py", encoding="utf-8").read()

    def test_the_protagonist_is_carried_across_a_world_load(self):
        self.assertIn("prior_cast", self.SRC)
        self.assertIn("kept protagonist", self.SRC)

    def test_a_fight_does_not_survive_a_world_stitch(self):
        block = self.SRC.split("def apply_experience_world", 1)[1][:3000]
        for key in ("encounter", "encounter_outcome", "encounter_resolving"):
            self.assertIn(f'state.pop("{key}", None)', block)


class TestChoiceTextIsNotTruncatedMidPhrase(unittest.TestCase):

    def test_a_clipped_label_does_not_end_on_a_conjunction(self):
        for raw in (
            "A man in green combat jacket and beanie",
            "A gaunt figure in a tattered grey wool overcoat",
            "A woman with a rifle slung over the",
        ):
            got = encounter._clip(raw, "", 40)
            self.assertFalse(
                re.search(r"\s(and|or|with|in|on|a|an|the|of|at|to|from|for)$", got, re.I),
                f"{raw!r} clipped to a dangling word: {got!r}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
