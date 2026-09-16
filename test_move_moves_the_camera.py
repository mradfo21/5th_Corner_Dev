#!/usr/bin/env python3
"""Pressing MOVE has to move the camera.

There are three ways a turn can travel and they need three different things
said to the image model, but for a long time only two existed. A hard cut got a
fresh composition. Everything else got the spatial anchor — including ordinary
walking, which the movement guidance further down the same prompt was
simultaneously describing as "the camera moved 5-15 ft forward".

The prompt contradicted itself, and the prohibition won: it comes first and it
says CANNOT. So MOVE TO an object produced a frame in the same position as the
one before it, and the turn read as time passing rather than as going anywhere.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import engine  # noqa: E402

#: What a previous turn leaves behind for the next one to compose against.
REFERENCE = dict(
    prev_vision_analysis="A yard of red dirt, a metal door ahead, chains to the left.",
    prev_spatial="ahead: metal door; left: hanging chains; ground: red dirt",
    prev_setting="outdoor-yard",
)

# The camera verdict, as `build_image_prompt` now states it: one sentence group
# per turn, in prose. These used to be emoji-marked headers ("🗺️ SPATIAL
# ANCHOR", "➡️ YOU HAVE CROSSED THE SPACE") that a downstream JSON template
# string-matched to pick one of its own five camera branches — three layers
# restating one decision, each claiming precedence over the others.
#
# Wording now also branches on the active camera mode's `shows_body` (a
# first-person turn "has WALKED TO IT"; a follow-cam turn says the "follow
# camera walked WITH the character" so the character stays framed instead of
# being talked about as if the shot were empty). The active mode is whatever
# is saved on disk for this repo, not something this file controls, so each
# "did the camera move" assertion has to accept either phrasing.
PINNED = "has NOT moved from the reference position"
WALKED = ("has WALKED TO IT", "walked with the character")
PORTAL_MOVED = ("has MOVED FORWARD", "moved forward WITH the character")
FRESH = "in a NEW PLACE"
PANNED = "panned or tilted slightly"
#: Every way the prompt can say the camera went somewhere.
MOVED_SOMEHOW = WALKED + PORTAL_MOVED + (FRESH,)


def _assert_any_in(test, phrases, prompt, msg=None):
    test.assertTrue(any(p in prompt for p in phrases),
                     msg or f"none of {phrases!r} found in prompt")

#: What the SCAN rail sends for MOVE TO on something you cannot go inside.
WALK_TO_OBJECT = ("Walk over to the chains and stop right in front of it, "
                  "close enough to touch.")
#: …and on something you can.
ENTER_A_DOOR = "Enter the industrial building, moving inside into the space beyond."


def prompt_for(choice: str, *, hard=False, softened=False, **over) -> str:
    kw = dict(REFERENCE)
    kw.update(over)
    return engine.build_image_prompt(
        player_choice=choice,
        dispatch="Rusted iron chains hang from a beam.",
        narrative_dispatch="You cross the gravel.",
        hard_transition=hard,
        softened_move=softened,
        **kw,
    )


class OnePromptSaysOneThing(unittest.TestCase):
    """The bug was not a missing instruction — it was two opposite ones."""

    def test_walking_is_never_told_it_cannot_leave(self):
        prompt = prompt_for(WALK_TO_OBJECT)
        self.assertNotIn(PINNED, prompt)

    def test_no_prompt_both_advances_and_forbids_the_camera(self):
        for choice in (WALK_TO_OBJECT, ENTER_A_DOOR, "Turn and scan the horizon",
                       "Examine the rusted padlock", "Sprint for the treeline"):
            with self.subTest(choice=choice[:32]):
                prompt = prompt_for(choice)
                moved = [m for m in MOVED_SOMEHOW if m in prompt]
                self.assertFalse(PINNED in prompt and moved,
                                 "the prompt tells the camera to move and to stay")


class EachKindOfTurnGetsItsOwnInstruction(unittest.TestCase):

    def test_walking_to_something_crosses_the_space(self):
        prompt = prompt_for(WALK_TO_OBJECT)
        _assert_any_in(self, WALKED, prompt)
        # …and is explicitly still the same place, which is the difference
        # between this and a location change.
        self.assertIn("Same place seen from further in", prompt)

    def test_walking_demands_a_different_composition(self):
        """Whatever else changes, the frame may not come back identical."""
        prompt = prompt_for(WALK_TO_OBJECT)
        _assert_any_in(self, ("fills far more of the frame",
                              "on screen at the new spot"), prompt)

    def test_the_delivered_payload_forbids_returning_the_reference(self):
        """The anti-copy guarantee itself, checked where it now lives.

        `build_image_prompt` used to carry its own "Do NOT return the previous
        composition unchanged" line, on top of the img2img template's own
        version of the same rule, on top of the template's five-branch marker
        parser. Stated once, in the template, is enough — but it has to actually
        still be there, because it is the single rule standing between MOVE TO
        and a redecorated copy of the previous frame.
        """
        import prompts_store
        payload = prompts_store.render_image_template(
            "gemini_image_to_image_instructions", prompt_for(WALK_TO_OBJECT))
        self.assertIn("Never return the reference with one detail swapped", payload)

    def test_a_doorway_still_gets_a_fresh_composition(self):
        prompt = prompt_for(ENTER_A_DOOR, hard=True)
        self.assertNotIn(PINNED, prompt)
        for phrase in WALKED:
            self.assertNotIn(phrase, prompt)
        self.assertIn(FRESH, prompt)

    def test_a_throttled_doorway_still_travels_forward(self):
        """The refused-portal path is its own case and must not be swallowed."""
        prompt = prompt_for(ENTER_A_DOOR, softened=True)
        _assert_any_in(self, PORTAL_MOVED, prompt)
        self.assertNotIn(PINNED, prompt)

    def test_a_still_camera_is_still_anchored(self):
        """The anchor is not the bug; applying it to walking was."""
        for choice in ("Examine the rusted padlock", "Photograph the doorway"):
            with self.subTest(choice=choice):
                prompt = prompt_for(choice)
                self.assertIn(PINNED, prompt)

    def test_looking_around_gets_its_own_verdict(self):
        """Turning on the spot moves the lens, not the body.

        This used to assert the full spatial anchor, because exploration had no
        branch of its own and fell through to it. Saying "has not moved from the
        reference position" of a turn that pans the camera is a small lie, and
        small lies in a prompt are what the model averages over.
        """
        prompt = prompt_for("Turn and scan the horizon")
        self.assertIn(PANNED, prompt)
        for phrase in WALKED:
            self.assertNotIn(phrase, prompt)
        self.assertNotIn(PINNED, prompt)


class TheObjectsNameIsNotTheVerb(unittest.TestCase):
    """MOVE TO interpolates the target into the sentence it classifies.

    With substring matching that meant the thing you walked to could answer a
    question about how you walked: panel and company contain "pan", turnstile
    contains "turn", checkpoint contains "check", scanner contains "scan". Each
    one came back as a camera that had not moved, so walking to a control panel
    produced the same frame twice.
    """

    #: Every one of these was misclassified before whole-word matching.
    COLLIDING = ["panel", "control panel", "turnstile", "checkpoint",
                 "scanner", "company sign", "crossbeam"]
    #: …and these matched nothing at all, so they cost an LLM call and took
    #: whatever it said.
    UNMATCHED = ["chains", "rusted barrel", "antenna", "generator", "ladder",
                 "crate", "lantern"]

    def test_walking_to_anything_is_movement(self):
        for obj in self.COLLIDING + self.UNMATCHED:
            with self.subTest(obj=obj):
                phrase = (f"Walk over to the {obj} and stop right in front of "
                          f"it, close enough to touch.")
                self.assertEqual(engine._detect_movement_type(phrase),
                                 "forward_movement")

    def test_the_current_move_phrase_is_also_movement(self):
        # moveActionPhrase in standalone.js dropped the "enterable"-word-list
        # branch (one universal "Move to the X." now, since is_move alone
        # forces the hard cut — the phrasing no longer decides anything).
        # _UI_MOVE_PHRASES has to recognise the phrase it actually emits.
        for obj in self.COLLIDING + self.UNMATCHED:
            with self.subTest(obj=obj):
                self.assertEqual(
                    engine._detect_movement_type(f"Move to the {obj}."),
                    "forward_movement")

    def test_the_rails_own_actions_never_need_a_model(self):
        """Classifying the commonest action in the game should not be a paid,
        non-deterministic round-trip."""
        def explode(*a, **k):
            raise AssertionError("asked a model to classify a known phrase")

        original = engine._ask
        engine._ask = explode
        try:
            for phrase in (
                "Walk over to the antenna and stop right in front of it, "
                "close enough to touch.",
                "Enter the industrial building, moving inside into the space beyond.",
            ):
                with self.subTest(phrase=phrase[:30]):
                    self.assertEqual(engine._detect_movement_type(phrase),
                                     "forward_movement")
        finally:
            engine._ask = original

    def test_standing_still_still_reads_as_standing_still(self):
        for action, expected in [
            ("Examine the rusted padlock", "stationary"),
            ("Photograph the doorway", "stationary"),
            ("Stand still and listen", "stationary"),
            ("Turn and scan the horizon", "exploration"),
            ("Back away slowly", "exploration"),
        ]:
            with self.subTest(action=action):
                self.assertEqual(engine._detect_movement_type(action), expected)


class TheFirstTurnHasNothingToCrossFrom(unittest.TestCase):
    """With no previous frame there is nothing to quote, but there is still a
    camera, and it still either moved or it didn't.

    This class used to assert the opposite: that a move with no reference data
    said NOTHING about the camera. That looked prudent and was the bug. Those
    three reference fields are backfilled by an async vision-reground pass that
    races the next turn's render and routinely loses — real runs land with all
    three empty on every single turn — so "stay quiet without them" meant
    staying quiet almost always. Silence reads to an image model holding a
    reference frame as "no opinion", and its most available completion under no
    opinion is to redraw the reference.
    """

    def test_walking_without_a_reference_frame_still_says_the_camera_moved(self):
        prompt = prompt_for(WALK_TO_OBJECT, prev_vision_analysis="",
                            prev_spatial="", prev_setting="")
        _assert_any_in(self, WALKED, prompt)
        self.assertNotIn(PINNED, prompt)

    def test_but_it_does_not_quote_a_frame_it_never_got(self):
        """Promising the model a reference it was never given is how prompts
        start inventing."""
        prompt = prompt_for(WALK_TO_OBJECT, prev_vision_analysis="",
                            prev_spatial="", prev_setting="")
        self.assertNotIn("The previous frame showed", prompt)
        self.assertNotIn("Still inside:", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
