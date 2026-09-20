"""
test_object_permanence.py — coverage for the object-permanence contract: when a
turn is issued by tapping a DETECTED object, that object must survive into the
next frame.

The defect this exists for: the world model regenerates every frame from
`visual_scene`, so a noun that text does not name stops existing. A player would
tap the steel door, commit to forcing it, and land in a corridor with no door in
it — the action resolved in the prose and vanished from the picture, which reads
as the interaction never happening at all.

What matters here:

  * The requirement only fires for a tapped subject. A typed or generated choice
    names no specific thing and must pin nothing, or the scene stops evolving.
  * MOVE TO and INTERACT demand different outcomes — closer versus changed — so
    one directive cannot serve both.
  * The claim is written on EVERY turn, including turns with no subject. A turn
    that leaves a stale claim behind lets a later reground settle it against a
    frame nobody ever asked it about.
  * Matching is head-noun tolerant. A model handed "rusted steel door" writes
    "the door", and scoring that a miss would make the measurement worthless.
  * Settling is one-shot, and separates what we ASKED for (`in_text`, the
    consequence) from what we GOT (`in_pixels`, the rendered frame's vision).

No network: every assertion here is over pure helpers or source text.

Run with:
    python3 -m unittest test_object_permanence -v
"""

import os
import unittest
from pathlib import Path

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("ANTHROPIC_API_KEY", "")

import engine

ROOT = Path(__file__).parent.resolve()


class TestSubjectNormalization(unittest.TestCase):
    """Detection labels arrive as free text from a model; they have to be
    comparable before anything can be measured against them."""

    def test_lowercases_and_collapses_whitespace(self):
        self.assertEqual(engine._permanence_subject("  Steel   DOOR "), "steel door")

    def test_strips_punctuation_but_keeps_hyphens(self):
        self.assertEqual(engine._permanence_subject("half-buried crate!"), "half-buried crate")

    def test_empty_and_none_are_empty(self):
        self.assertEqual(engine._permanence_subject(None), "")
        self.assertEqual(engine._permanence_subject("   "), "")

    def test_is_bounded(self):
        self.assertLessEqual(len(engine._permanence_subject("x" * 200)), 40)


class TestSubjectMatching(unittest.TestCase):
    """The whole measurement rests on this: too strict and every compliant
    scene scores as a miss, too loose and the number means nothing."""

    def test_matches_the_whole_phrase(self):
        self.assertTrue(engine._subject_in_text(
            "steel door", "The steel door groans open onto a stairwell."))

    def test_matches_the_head_noun_alone(self):
        # The model was handed "rusted steel door" and wrote "the door".
        self.assertTrue(engine._subject_in_text(
            "rusted steel door", "The door swings wide, hinges shrieking."))

    def test_matches_a_plural_head_noun(self):
        self.assertTrue(engine._subject_in_text(
            "cargo crate", "Crates spill across the loading floor."))

    def test_absent_subject_is_a_miss(self):
        self.assertFalse(engine._subject_in_text(
            "steel door", "A corridor of humming pipes stretches ahead."))

    def test_head_noun_must_be_substantial(self):
        # A two-letter head noun would match half the English language.
        self.assertFalse(engine._subject_in_text("the ax", "A taxi idles in the rain."))

    def test_empty_inputs_never_match(self):
        self.assertFalse(engine._subject_in_text("", "anything at all"))
        self.assertFalse(engine._subject_in_text("steel door", ""))

    def test_does_not_match_inside_a_longer_word(self):
        self.assertFalse(engine._subject_in_text("door", "The doorman is gone."))


class TestDirective(unittest.TestCase):
    """The prompt block is the whole mechanism — if it doesn't name the object,
    nothing downstream can keep it."""

    def test_no_subject_yields_no_directive(self):
        self.assertEqual(engine._permanence_directive("", False), "")
        self.assertEqual(engine._permanence_directive(None, True), "")

    def test_names_the_subject_and_the_field_it_governs(self):
        d = engine._permanence_directive("steel door", False)
        self.assertIn("steel door", d)
        self.assertIn("visual_scene", d)

    def test_interact_demands_the_object_changed_and_still_present(self):
        d = engine._permanence_directive("steel door", is_move=False)
        self.assertIn("CHANGED BY", d)
        self.assertNotIn("CLOSER", d)

    def test_move_demands_the_object_be_in_place_not_centered(self):
        d = engine._permanence_directive("steel door", is_move=True)
        self.assertIn("steel door", d)
        self.assertNotIn("CHANGED BY", d)
        # A model told to put the object "central"/"CLOSER" satisfies that
        # with a centered hero shot of the object alone — which reads as the
        # OBJECT moving to the middle of frame, not the PLAYER walking up to
        # it. Both words are banned so that reading can't come back.
        self.assertNotIn("CLOSER", d)
        self.assertNotIn("central", d.lower())


class TestActionDirectiveRelocation(unittest.TestCase):
    """TRAVERSAL guidance used to only reach a SCAN "Move to X" tap
    (is_interaction=True). A curated/typed choice earning the identical hard
    cut through the egress backstop or the text classifier got nothing —
    no "this is locomotion, not confrontation" steering, no "keep the
    camera's existing relationship to the player" instruction — which is
    exactly the seam a real run's camera flip fell through. `also_relocating`
    is how a non-SCAN action opts into the same text."""

    def test_plain_choice_with_no_relocation_gets_nothing(self):
        self.assertEqual(engine._action_directive(False, False, ""), "")

    def test_scan_interact_is_unaffected_by_the_new_parameter(self):
        # A SCAN "interact" (is_interaction=True, is_move=False) must keep
        # getting the OBJECT INTERACTION text, not TRAVERSAL — also_relocating
        # only matters for non-SCAN actions.
        d = engine._action_directive(True, False, "steel door")
        self.assertIn("OBJECT INTERACTION", d)
        self.assertNotIn("TRAVERSAL", d)

    def test_scan_move_is_unaffected_by_the_new_parameter(self):
        d = engine._action_directive(True, True, "steel door")
        self.assertIn("TRAVERSAL", d)

    def test_non_scan_relocation_now_gets_traversal_text(self):
        # The whole point of also_relocating: a curated/typed choice that is
        # about to get a hard cut for a non-SCAN reason (egress, or the text
        # classifier) earns the identical TRAVERSAL guidance a SCAN MOVE gets.
        d = engine._action_directive(False, False, "", also_relocating=True)
        self.assertIn("TRAVERSAL", d)
        self.assertNotIn("OBJECT INTERACTION", d)

    def test_non_scan_relocation_tells_the_model_to_hold_the_camera(self):
        d = engine._action_directive(False, False, "", also_relocating=True)
        self.assertIn("stays BEHIND them", d)

    def test_non_scan_relocation_with_no_subject_adds_no_permanence_claim(self):
        # There is no detected object to protect when nothing was tapped —
        # OBJECT PERMANENCE must not appear just because also_relocating did.
        d = engine._action_directive(False, False, "", also_relocating=True)
        self.assertNotIn("OBJECT PERMANENCE", d)


class TestRecord(unittest.TestCase):
    """A turn's claim: what was pinned, and whether the consequence honoured it."""

    def test_no_subject_records_nothing(self):
        self.assertIsNone(engine._permanence_record("", False, "any scene text"))

    def test_records_a_kept_subject(self):
        rec = engine._permanence_record(
            "steel door", False, "The steel door hangs off one hinge.")
        self.assertEqual(rec["subject"], "steel door")
        self.assertEqual(rec["kind"], "interact")
        self.assertTrue(rec["in_text"])
        self.assertIsNone(rec["in_pixels"], "the frame has not been looked at yet")
        self.assertTrue(rec["pending"])

    def test_records_a_dropped_subject_without_failing_the_turn(self):
        # A model that ignores the requirement must be MEASURED, not raised on:
        # the turn is already live and the player is owed their consequence.
        rec = engine._permanence_record(
            "steel door", False, "A corridor of humming pipes stretches ahead.")
        self.assertFalse(rec["in_text"])
        self.assertTrue(rec["pending"])

    def test_move_is_recorded_as_its_own_kind(self):
        rec = engine._permanence_record("water tower", True, "The water tower looms closer.")
        self.assertEqual(rec["kind"], "move")


class TestResolution(unittest.TestCase):
    """Settling the claim against the frame that actually rendered."""

    def test_records_survival_in_the_rendered_frame(self):
        st = {"permanence": engine._permanence_record(
            "steel door", False, "The steel door hangs off one hinge.")}
        engine._resolve_permanence(st, "A buckled steel door fills the left of frame.")
        self.assertTrue(st["permanence"]["in_pixels"])
        self.assertFalse(st["permanence"]["pending"])

    def test_records_a_renderer_that_dropped_the_object(self):
        # in_text True + in_pixels False is the interesting case: the LLM obeyed
        # and the image model did not. That split is the reason both are stored.
        st = {"permanence": engine._permanence_record(
            "steel door", False, "The steel door hangs off one hinge.")}
        engine._resolve_permanence(st, "An empty corridor of humming pipes.")
        self.assertTrue(st["permanence"]["in_text"])
        self.assertFalse(st["permanence"]["in_pixels"])

    def test_is_one_shot(self):
        st = {"permanence": engine._permanence_record(
            "steel door", False, "The steel door hangs open.")}
        engine._resolve_permanence(st, "The steel door fills the frame.")
        engine._resolve_permanence(st, "Nothing but sand.")
        self.assertTrue(st["permanence"]["in_pixels"], "a settled claim is not re-scored")

    def test_no_claim_is_not_an_error(self):
        for empty in ({}, {"permanence": None}, {"permanence": "junk"}):
            engine._resolve_permanence(empty, "some vision text")  # must not raise


class TestWiring(unittest.TestCase):
    """Source assertions for the seams a unit test cannot reach without a live
    LLM and a browser. Each pins a link that, if dropped, would leave the
    feature looking complete while the label never arrives."""

    @classmethod
    def setUpClass(cls):
        cls.engine_src = (ROOT / "engine.py").read_text(encoding="utf-8")
        cls.client_src = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")

    def test_the_consequence_prompt_carries_the_requirement(self):
        # The permanence clause rides on the back of whichever object directive
        # the turn earned, and that directive is what the prompt interpolates.
        self.assertIn("return directive + _permanence_directive(subject, relocating)",
                      self.engine_src)
        self.assertIn("interaction_directive = _action_directive(",
                      self.engine_src)
        self.assertIn("also_relocating=non_scan_relocation", self.engine_src)

    def test_the_claim_is_written_on_every_turn(self):
        # Unconditional: a turn with no subject writes None, which is what stops
        # a stale claim being settled against a later, unrelated frame.
        self.assertIn('st["permanence"] = _permanence_record(', self.engine_src)

    def test_the_rendered_frame_settles_the_claim(self):
        self.assertIn("_resolve_permanence(st, vision)", self.engine_src)

    def test_the_turn_receives_the_subject_from_the_request(self):
        self.assertIn("action_subject = _permanence_subject(data.get('subject'))",
                      self.engine_src)
        self.assertIn('"subject": action_subject', self.engine_src)

    def test_the_client_sends_the_detected_label(self):
        # Asserted on the argument rather than the whole call, which was
        # pinned as one literal line and broke the moment the SCAN tap grew a
        # third option to pass (see test_interact_plate_handoff).
        tap = self.client_src.split("const source = action.id === \"move\"", 1)[1]
        commit = tap[:tap.index("if (dive) dive.armTurn(")]
        self.assertIn("makeChoice(phrase, null, {", commit)
        self.assertIn("subject: obj.label", commit)
        self.assertIn("subject: actionSubject", self.client_src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
