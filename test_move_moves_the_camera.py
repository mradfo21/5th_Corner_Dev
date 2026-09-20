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


class TheFirstMoveOutOfTheOpeningActuallyMoves(unittest.TestCase):
    """A hard cut normally ships a blurred swatch, so there is no framing to
    copy and nothing had to forbid copying it. Straight out of the opening
    montage the reference is a LEGIBLE frame on purpose — the establishing shot
    the player is standing in — and with nothing forbidding it the model
    reproduced that composition and called it a move. The client harness put a
    number on it: "Sprint toward the rusted factory" came back at 0.96
    continuity with the frame before it, and reported that the world had not
    taken the player anywhere.
    """

    NEEDLE = "Do not reproduce the reference framing."
    MOVE = "Sprint toward the rusted factory"
    STAY = "Examine the rusted padlock"

    def _prompt(self, choice, hard=True, holds=True):
        return engine.build_image_prompt(
            player_choice=choice, dispatch="A scene.",
            hard_transition=hard, holds_reference_frame=holds, **REFERENCE)

    def test_the_first_move_is_told_the_vantage_travelled(self):
        p = self._prompt(self.MOVE)
        self.assertIn(self.NEEDLE, p)
        self.assertIn("NEW vantage", p)

    def test_it_still_holds_the_place_it_came_out_of(self):
        """The reference is kept for a reason — blurring it to a swatch is what
        made a run visibly begin somewhere other than where the cutscene put
        you. This line moves the camera WITHIN that place, it does not license
        a new one."""
        p = self._prompt(self.MOVE)
        self.assertIn("same place", p.lower())
        self.assertIn("same light", p.lower())

    def test_a_first_turn_that_does_not_travel_is_left_alone(self):
        self.assertNotIn(self.NEEDLE, self._prompt(self.STAY))

    def test_an_ordinary_hard_cut_is_left_alone(self):
        """That path ships a swatch; there is no framing to forbid copying."""
        self.assertNotIn(self.NEEDLE, self._prompt(self.MOVE, holds=False))

    def test_a_soft_turn_is_left_alone(self):
        self.assertNotIn(self.NEEDLE, self._prompt(self.MOVE, hard=False))


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


class TheSceneHasToReachTheRenderer(unittest.TestCase):
    """The scene the story wrote is the only thing that says where the player
    ended up. It has to arrive.

    `build_image_prompt` decides whether to send `visual_scene` at all by
    comparing it against the narrative: `has_visual_scene` is
    `narrative != caption`. The turn loop passed the caption into BOTH slots,
    so that test was False on every turn of the live path and the "render
    exactly this scene" branch was unreachable. What shipped instead was the
    scaffold — a paraphrase of the PREVIOUS frame plus "render the result of
    that action" — with the scene generated, logged, and dropped.

    MOVE TO is where a silent prompt does the most damage, because the
    destination is named nowhere else: a click on the mesa came back as a
    corrugated shed door.
    """

    SCENE = ("He stands at the foot of the red mesa, the fence now far behind "
             "him across open dirt.")
    PROSE = "You cross the open ground, grit working into your boots."

    def _prompt(self, narrative):
        return engine.build_image_prompt(
            player_choice="Move to the mesa.",
            dispatch=self.SCENE,
            narrative_dispatch=narrative,
            hard_transition=True,
            **REFERENCE,
        )

    def test_the_scene_is_in_the_prompt(self):
        self.assertIn(self.SCENE, self._prompt(self.PROSE))

    def test_a_caption_echoed_into_the_narrative_slot_drops_the_scene(self):
        """The failure this guards, stated as the mechanism that caused it."""
        self.assertNotIn(self.SCENE, self._prompt(self.SCENE))

    def test_the_turn_loop_sends_the_prose_not_the_caption(self):
        """…which is why the call site itself is pinned.

        The two channels were split apart in the engine long after this call
        site was written, and back then `dispatch` WAS `visual_scene`, so
        passing it twice was a harmless no-op. Nothing fails loudly when it
        stops being one.
        """
        src = (ROOT / "engine.py").read_text(encoding="utf-8")
        loop = src.split("scene = _generate_and_append_scene_image(", 1)[1] \
                  .split(")", 1)[0]
        self.assertIn("dispatch=dispatch_text,", loop)
        self.assertNotIn("dispatch=vision_dispatch_text or dispatch_text,", loop)

    def test_a_travelling_hard_cut_says_where_the_camera_went(self):
        """`is_move` forces `hard_transition`, so the FRESH branch is the only
        one MOVE TO can reach — and "a different space" is not a destination."""
        prompt = self._prompt(self.PROSE)
        self.assertIn(FRESH, prompt)
        self.assertIn("the DESTINATION", prompt)

    def test_standing_still_is_not_told_it_arrived(self):
        prompt = engine.build_image_prompt(
            player_choice="Examine the rusted padlock",
            dispatch=self.SCENE, narrative_dispatch=self.PROSE,
            hard_transition=False, **REFERENCE,
        )
        self.assertNotIn("the DESTINATION", prompt)


class ADestinationIsNotAlwaysARoom(unittest.TestCase):
    """SCAN detects landforms, vehicles and open ground, not just doors.

    The MOVE half of the permanence requirement used to place the tapped object
    "in the room the player just crossed into ... on its wall, floor, or
    surface". True of a steel door, nonsense about a mesa — and the model
    resolved the nonsense the only way the sentence allows, by inventing an
    enclosure to put the player inside and demoting the mesa to a backdrop
    behind it.
    """

    OUTDOOR = ["mesa", "pickup truck", "rusted tank", "chain link fence",
               "radio mast", "catch pond"]

    def test_the_move_requirement_never_assumes_an_interior(self):
        for subj in self.OUTDOOR:
            with self.subTest(subj=subj):
                d = engine._permanence_directive(subj, is_move=True)
                self.assertIn(subj, d)
                self.assertNotIn("the room the player just crossed into", d)
                self.assertNotIn("on its wall, floor, or surface", d)

    def test_it_still_forbids_the_product_photo_it_was_written_for(self):
        d = engine._permanence_directive("mesa", is_move=True)
        self.assertIn("product photo", d)
        self.assertIn("ARRIVED AT", d)

    def test_interact_is_untouched(self):
        d = engine._permanence_directive("steel door", is_move=False)
        self.assertIn("CHANGED BY the action", d)
        self.assertNotIn("ARRIVED AT", d)

    def test_traversal_only_crosses_through_actual_openings(self):
        d = engine._action_directive(is_interaction=True, is_move=True,
                                     subject="mesa")
        self.assertIn("When the destination is ITSELF an opening", d)
        self.assertIn("they arrive AT it and stop there", d)
        # The unconditional version of this sentence is what sent a MOVE TO on
        # a monolith through a door that was never there.
        self.assertNotIn("Arriving means they crossed through. When", d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
