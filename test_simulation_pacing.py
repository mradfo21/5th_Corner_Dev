"""
test_simulation_pacing.py — the dials that make time move.

A 30-turn playtest against the real backend produced vivid individual turns and
a run that went nowhere: every frame rendered at the same minute of the same
evening, chaos climbing +1 a turn forever, and a SCAN run that changed location
27 turns running without ever leaving "cramped organic interior". Each of those
is a separate mechanism that was either never driven or driven by a counter
instead of by what happened. The tests here pin the fixed versions.

Pure functions and one temp session — no network, no API key, no server.

Run with:
    python3 -m unittest test_simulation_pacing -v
"""

import os
import random
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")

import engine

ROOT = Path(__file__).resolve().parent

EVENING = "7:42pm | weather: dusty, bruised purple twilight | mood: isolating dread"


class TestClockAdvances(unittest.TestCase):
    """The helper that can step a lighting line. The turn loop no longer
    calls it — rewriting the evening on an act break relit hard cuts —
    but the function stays pinned so a deliberate 'wait until night'
    still has one correct ladder."""

    def test_the_baseline_run_was_stuck_at_one_time(self):
        # The exact string all 30 turns of the watch_20260822_203735 runs
        # rendered at, on both playtests.
        self.assertNotEqual(engine.advance_time_of_day(EVENING), EVENING)

    def test_twilight_steps_to_night(self):
        out = engine.advance_time_of_day(EVENING)
        self.assertIn("night", out)
        self.assertNotIn("twilight", out)

    def test_the_clock_moves_with_the_light(self):
        self.assertIn("8:27pm", engine.advance_time_of_day(EVENING))

    def test_weather_and_mood_survive(self):
        # These are the identity of this playthrough's evening. An act break
        # darkens the light; it does not reinvent the weather.
        out = engine.advance_time_of_day(EVENING)
        self.assertIn("dusty, bruised purple", out)
        self.assertIn("mood: isolating dread", out)

    def test_a_run_never_brightens(self):
        tod = "7:10pm | weather: dusk light | mood: dread"
        for _ in range(6):
            tod = engine.advance_time_of_day(tod)
        for brighter in ("dawn", "morning", "midday", "afternoon", "dusk"):
            self.assertNotIn(brighter, tod)
        self.assertIn("deep night", tod)

    def test_nested_aliases_read_as_the_darkest_tier(self):
        # "deep night", "midnight" and "nightfall" all contain an earlier tier's
        # word; taking the first match would read them one tier too bright.
        for phrase, expected in (
            ("dead of night", "deep night"),
            ("midnight", "deep night"),
            ("nightfall", "night"),
            ("twilight", "dusk"),
        ):
            idx, _alias = engine._current_time_tier(f"9:00pm | weather: {phrase}")
            self.assertEqual(engine._TIME_TIERS[idx][0], expected, phrase)

    def test_the_shipped_default_darkens(self):
        out = engine.advance_time_of_day(engine.INITIAL_TIME_OF_DAY)
        self.assertIn("dusk", out)
        self.assertIn("7:15pm", out)
        self.assertIn("mood: tense anticipation", out)

    def test_the_lighting_phrase_is_replaced_not_stacked(self):
        tod = engine.INITIAL_TIME_OF_DAY
        for _ in range(2):
            tod = engine.advance_time_of_day(tod)
        self.assertIn("night", tod)
        self.assertNotIn("dusk", tod)
        self.assertNotIn("golden hour", tod)
        # "night light" is the specific nonsense a naive word swap produces.
        self.assertNotIn("night light", tod)

    def test_a_seed_that_describes_its_own_light_loses_it(self):
        # A real run seeded "dusty haze with fading violet light", which names no
        # tier this ladder knows, so the new tier was appended beside it and the
        # image prompt read "fading violet light, deep night" — two times of day
        # at once. The seed's weather and mood still have to survive.
        tod = "7:14pm | weather: dusty haze with fading violet light | mood: suffocating dread"
        tod = engine.advance_time_of_day(tod)
        self.assertNotIn("violet", tod)
        self.assertNotIn("light", tod)
        self.assertIn("night", tod)
        self.assertIn("dusty haze", tod)
        self.assertIn("suffocating dread", tod)

    def test_that_seed_then_keeps_darkening_in_place(self):
        tod = "7:14pm | weather: dusty haze with fading violet light | mood: suffocating dread"
        tod = engine.advance_time_of_day(tod)
        tod = engine.advance_time_of_day(tod)
        self.assertIn("deep night", tod)
        # Not "night, deep night" — the second step rewrites rather than appends.
        self.assertEqual(tod.count("night"), 1)
        self.assertIn("dusty haze", tod)

    def test_a_seed_that_names_no_tier_reads_one_off_the_clock(self):
        # Before the clock fallback, a string with no lighting word at all could
        # only ever move its clock and would never darken.
        out = engine.advance_time_of_day("6:40pm | weather: flat grey | mood: dread")
        self.assertIn("dusk", out)

    def test_the_clock_fallback_respects_the_hour(self):
        for clock, expected in (("6:00am", "morning"), ("1:00pm", "afternoon"),
                                ("10:30pm", "deep night")):
            out = engine.advance_time_of_day(f"{clock} | weather: flat grey")
            self.assertIn(expected, out, clock)

    def test_unreadable_input_still_moves_the_clock(self):
        self.assertIn("6:45pm", engine.advance_time_of_day("6:00pm | weather: unreadable static"))

    def test_the_clock_wraps_past_midnight(self):
        self.assertIn("12:30am", engine.advance_time_of_day("11:45pm | weather: night"))

    def test_an_empty_clock_is_left_alone(self):
        self.assertEqual(engine.advance_time_of_day(""), "")


class TestChaosIsADial(unittest.TestCase):
    """chaos_level was `+= 1` in generate_and_apply_choice — a second turn
    counter, unbounded, that never fell. It is now a decaying average of recent
    intensity, which is the only shape that survives a 'critical' phase where
    nearly every turn is eventful by construction."""

    def _apply(self, state, **kw):
        kw.setdefault("fate", "NORMAL")
        kw.setdefault("escalated", False)
        kw.setdefault("interaction", False)
        return engine.apply_chaos(state, **kw)

    def test_a_quiet_turn_cools_the_room(self):
        self.assertEqual(self._apply({"chaos_level": 8}), 4)

    def test_luck_cools_it_faster(self):
        self.assertEqual(self._apply({"chaos_level": 8}, fate="LUCKY"), 2)

    def test_bad_luck_holds_it_up(self):
        self.assertEqual(self._apply({"chaos_level": 4}, fate="UNLUCKY"), 4)

    def test_an_act_break_is_the_biggest_jolt(self):
        self.assertEqual(self._apply({"chaos_level": 2}, escalated=True), 4)

    def test_events_stack(self):
        # nothing carried over + escalated 3 + unlucky 2 + meddling 1.
        # There was an "injury 2" term here as well, fed by the wound parser;
        # it went with that parser, and it was mostly double-counting UNLUCKY.
        self.assertEqual(
            self._apply({"chaos_level": 0}, fate="UNLUCKY", escalated=True,
                        interaction=True),
            6,
        )

    def test_it_cannot_run_away(self):
        state = {"chaos_level": 0}
        for _ in range(20):
            self._apply(state, fate="UNLUCKY", escalated=True, interaction=True)
        self.assertEqual(state["chaos_level"], engine.CHAOS_MAX)

    def test_it_cannot_go_negative(self):
        state = {"chaos_level": 1}
        for _ in range(5):
            self._apply(state, fate="LUCKY")
        self.assertEqual(state["chaos_level"], 0)

    def test_a_relentless_run_still_leaves_room_to_spike(self):
        """The failure the first version of this dial actually shipped with.

        A 30-turn live run pinned chaos at the ceiling from turn 6 to turn 30
        because 'critical' rolls UNLUCKY about half the time and its phase
        directive demands a hard beat every turn. A steady diet of bad luck must
        settle BELOW the ceiling, so an act break still has somewhere to go.
        """
        state = {"chaos_level": 0}
        for _ in range(12):
            self._apply(state, fate="UNLUCKY")
        settled = state["chaos_level"]
        self.assertLess(settled, engine.CHAOS_MAX)
        self.assertGreater(self._apply(state, fate="UNLUCKY", escalated=True,
                                       interaction=True),
                           settled)

    def test_one_calm_turn_is_visible(self):
        # Whatever it has been, a turn where nothing happens must read as relief.
        state = {"chaos_level": engine.CHAOS_MAX}
        self.assertLessEqual(self._apply(state), engine.CHAOS_MAX // 2)

    def test_the_dial_un_pins_the_world_summary(self):
        """summarize_world_state branches on chaos > 7 and > 5, and feeds
        situation_summary to the realtime choice regeneration (/api/observe),
        /api/regenerate_choices and the reset slate. Pinned at the top, all
        three always read the same sentence."""
        state = {"chaos_level": 8, "player_state": {"alive": True}, "world_prompt": ""}
        self.assertIn("OVERWHELMING", engine.summarize_world_state(state))
        self._apply(state)
        self.assertNotIn("OVERWHELMING", engine.summarize_world_state(state))

    def test_choice_persistence_no_longer_touches_chaos(self):
        import json
        import tempfile
        from pathlib import Path

        import choices

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "world_state.json"
            path.write_text(json.dumps({"chaos_level": 5, "last_choice": ""}), encoding="utf-8")
            choices.generate_and_apply_choice("Vault the rail", state_path=str(path))
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["last_choice"], "Vault the rail")
        self.assertEqual(saved["chaos_level"], 5, "chaos has one owner now: engine.apply_chaos")


class TestHardTransitionThrottle(unittest.TestCase):
    """Cutting to a freshly composed frame every turn is not travel — it is a
    slideshow of unrelated rooms. The SCAN baseline did it 27 turns running.

    NOTE: `advance_turn_image_fast` no longer calls `throttle_hard_transition`
    for ANY action — MOVE is unconditional (TestMoveIsAlwaysAHardCut), and the
    typed/curated text path stopped throttling too, because the cap itself was
    an inconsistency: the same wording could cut or not depending on how many
    cuts had happened recently. These tests keep the function itself correct
    and available, in case a future feature wants "cap consecutive cuts" back
    without re-deriving the cap-holds-at-max-until-a-soft-turn logic below."""

    def test_the_first_cuts_are_allowed(self):
        state = {}
        for _ in range(engine.MAX_CONSECUTIVE_HARD_TRANSITIONS):
            self.assertTrue(engine.throttle_hard_transition(state, True, "You move."))

    def test_the_next_one_is_held(self):
        state = {}
        for _ in range(engine.MAX_CONSECUTIVE_HARD_TRANSITIONS):
            engine.throttle_hard_transition(state, True, "You move.")
        self.assertFalse(engine.throttle_hard_transition(state, True, "You move."))

    def test_a_stuck_run_cannot_alternate_around_the_cap(self):
        # Holding the chain AT the cap (rather than resetting it on a throttled
        # turn) is what stops cut/throttle/cut/throttle forever.
        state = {}
        for _ in range(engine.MAX_CONSECUTIVE_HARD_TRANSITIONS):
            engine.throttle_hard_transition(state, True, "You move.")
        for _ in range(5):
            self.assertFalse(engine.throttle_hard_transition(state, True, "You move."))

    def test_staying_put_clears_the_run(self):
        state = {}
        for _ in range(engine.MAX_CONSECUTIVE_HARD_TRANSITIONS):
            engine.throttle_hard_transition(state, True, "You move.")
        engine.throttle_hard_transition(state, False, "You crouch.")
        self.assertTrue(engine.throttle_hard_transition(state, True, "You move."))

    def test_prose_that_really_opens_a_space_overrides_the_cap(self):
        state = {}
        for _ in range(engine.MAX_CONSECUTIVE_HARD_TRANSITIONS):
            engine.throttle_hard_transition(state, True, "You move.")
        self.assertTrue(engine.throttle_hard_transition(
            state, True, "The crawlspace gives way to a vaulted machine hall."))

    def test_ordinary_play_never_trips_it(self):
        state = {}
        for _ in range(30):
            engine.throttle_hard_transition(state, False, "You shift your weight.")
        self.assertTrue(engine.throttle_hard_transition(state, True, "You step through."))

    def test_a_doorway_is_never_refused(self):
        """The throttle is about composition cost, not about whether the player
        got through a door they were told they could open. Refusing a threshold
        is unrenderable: the previous frame holds the near side and the prose
        says they are on the far side, so the image has to contradict one of
        them — and it contradicts the prose, putting them back outside."""
        state = {}
        for _ in range(engine.MAX_CONSECUTIVE_HARD_TRANSITIONS + 4):
            self.assertTrue(
                engine.throttle_hard_transition(state, True, "You move.", "portal"),
                "a threshold crossing must always get a fresh composition")

    def test_an_approach_is_still_refused_after_a_doorway(self):
        """Exempting portals must not exempt everything downstream of one, or
        the cap stops existing the moment a run goes through any door."""
        state = {}
        for _ in range(engine.MAX_CONSECUTIVE_HARD_TRANSITIONS):
            engine.throttle_hard_transition(state, True, "You move.", "portal")
        self.assertFalse(
            engine.throttle_hard_transition(state, True, "You move.", "approach"))


class TestMoveIsAlwaysAHardCut(unittest.TestCase):
    """`advance_turn_image_fast` used to run EVERY action — MOVE included —
    through the same portal-vs-approach text classifier and a consecutive-cut
    throttle, so whether an action actually changed the scene depended on the
    tapped/typed wording and on how many hard cuts had happened recently.
    `is_move` is a structured signal (it comes straight from the client's
    dedicated MOVE TO verb, source=="scan_move"), not inferred text, so it
    short-circuits the classifier entirely: MOVE always gets a fresh
    composition. The throttle was removed for the remaining (typed/curated)
    path too — see TestHardTransitionThrottle's module-level note — so ANY
    text the classifier reads as a relocation, MOVE or not, now gets a cut
    every time. Exercising the real function needs a full session + image
    pipeline, so this guards the branch structure instead."""

    @staticmethod
    def _turn_body() -> str:
        src = (ROOT / "engine.py").read_text(encoding="utf-8", errors="replace")
        start = src.index("def advance_turn_image_fast")
        end = src.index("def advance_turn_choices_deferred")
        return src[start:end]

    def _move_branch(self) -> str:
        body = self._turn_body()
        elif_idx = body.index("elif is_move:")
        next_idx = body.index("\n        elif interaction", elif_idx)
        return body[elif_idx:next_idx]

    def _infer_branch(self) -> str:
        body = self._turn_body()
        start = body.index("elif interaction or is_custom_action:")
        end = body.index("\n        else:", start)
        return body[start:end]

    def _choice_branch(self) -> str:
        body = self._turn_body()
        start = body.index("elif interaction or is_custom_action:")
        else_idx = body.index("\n        else:", start)
        # Stop before the image-generation try so a later `else` cannot
        # leak classifier calls into this assertion.
        stop = body.index("consequence_img_url = None", else_idx)
        return body[else_idx:stop]

    def test_is_move_branch_sets_hard_transition_unconditionally(self):
        self.assertIn("hard_transition = True", self._move_branch())

    def test_is_move_branch_never_calls_the_text_classifier(self):
        self.assertNotIn("is_hard_transition(", self._move_branch())

    def test_interact_and_typed_still_infer_from_text_but_are_not_throttled(self):
        # INTERACT and typed free-will still have no dedicated "new beat"
        # verb, so the wording classifier is the only relocation signal —
        # but nothing downstream may cap or refuse what it decided.
        infer = self._infer_branch()
        self.assertIn("is_hard_transition(", infer)
        self.assertNotIn("throttle_hard_transition(", infer)

    def test_a_curated_choice_pill_cuts_only_on_a_real_relocation(self):
        # Forcing every pill to a hard cut threw away the Reactor capture
        # the player was looking at. Refine from that frame unless the
        # wording is a portal / egress.
        choice = self._choice_branch()
        self.assertIn("is_hard_transition(", choice)
        self.assertIn("is_egress_choice(", choice)
        self.assertNotIn("throttle_hard_transition(", choice)


class TestChoicePickDoesNotImg2imgTheCurrentFrame(unittest.TestCase):
    """A curated choice pick is a hard visual cut: the previous still must
    not be the img2img init — including on frame 1, where a special case
    used to force the intro shot as the reference no matter the flag.

    Last night's CHOICE playtest (render_20260826_012336) is the exhibit:
    36/40 picks missed the location-phrase classifier and rendered with
    "Match the lighting... to the previous image" plus the previous frame
    as the Gemini img2img reference."""

    PNG = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00"
        b"\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx"
        b"\x9cc\xf8\xcf\xc0P\x0f\x00\x04\x85\x01\x80\xa1\xa9\x8c!\x00"
        b"\x00\x00\x00IEND\xaeB`\x82"
    )

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.prev = Path(self._tmpdir.name) / "prev_frame.png"
        self.prev.write_bytes(self.PNG)
        self.prev_path = str(self.prev.resolve())
        self.calls = []
        self._orig_summaries = (
            engine.summarize_world_state,
            engine.summarize_world_prompt_for_image,
        )
        engine.summarize_world_state = lambda *a, **k: ""
        engine.summarize_world_prompt_for_image = lambda *a, **k: ""
        self._orig_provider = engine.ai_provider_manager.get_image_provider
        engine.ai_provider_manager.get_image_provider = lambda: "gemini"
        self._orig_ask = engine._ask
        engine._ask = lambda *a, **k: "STATIONARY"

    def tearDown(self):
        engine.summarize_world_state, engine.summarize_world_prompt_for_image = (
            self._orig_summaries)
        engine.ai_provider_manager.get_image_provider = self._orig_provider
        engine._ask = self._orig_ask
        self._tmpdir.cleanup()

    def _record(self, label):
        def stub(*args, **kwargs):
            refs = kwargs.get("reference_image_path") or []
            if isinstance(refs, str):
                refs = [refs]
            self.calls.append((label, [str(Path(r).resolve()) for r in refs]))
            return self.prev_path
        return stub

    def _render(self, frame_idx, hard_transition):
        import gemini_image_utils as giu
        orig = (giu.generate_gemini_img2img, giu.generate_with_gemini,
                giu.make_style_swatch)
        giu.generate_gemini_img2img = self._record("img2img")
        giu.generate_with_gemini = self._record("t2i")
        giu.make_style_swatch = lambda *a, **k: None
        try:
            engine._gen_image_impl(
                caption="Jason Fleece crouches along the fence.",
                mode="camcorder",
                choice="Follow the perimeter fence line",
                frame_idx=frame_idx,
                dispatch="You track the rusted chain-link perimeter.",
                world_prompt="",
                hard_transition=hard_transition,
                session_id="test_choice_hardcut",
                history_ref=[{
                    "choice": "Intro",
                    "vision_dispatch": "A desert industrial yard at twilight.",
                    "image": self.prev_path,
                    "image_url": self.prev_path,
                    "hard_transition": False,
                }],
            )
        finally:
            (giu.generate_gemini_img2img, giu.generate_with_gemini,
             giu.make_style_swatch) = orig

    def test_last_nights_choice_wording_is_now_a_classifier_hit(self):
        """This used to assert the opposite, as evidence for the renderer fix.

        The classifier only knew interiors: every preposition meant entering
        an enclosure and every destination noun was a room or a passage. In
        an open world nothing matched, so travel across terrain rendered as
        an img2img edit of the previous frame, and a long outdoor stretch
        became an edit of an edit of an edit.
        """
        for phrase in (
            "Follow the perimeter fence line",
            "Climb the rusted maintenance ladder",
            "Vault over the rusted bulkhead",
            "Sprint toward the scorched basin",
            "Scale the lightning struck mesa",
        ):
            self.assertTrue(engine.is_hard_transition(phrase, ""), phrase)

    def test_crossing_a_room_to_an_object_is_still_not_travel(self):
        """The counterweight: approaching a thing in this place is a new
        vantage, not a new location. Cutting there made MOVE read as
        teleporting, so it stays a continuation."""
        for phrase in (
            "Walk over to the oil pump and stop right in front of it.",
            "Move to the industrial crane.",
            "Approach the gantry crane",
            "Advance toward the flickering bulb",
        ):
            self.assertFalse(engine.is_hard_transition(phrase, ""), phrase)

    def test_an_observation_is_never_travel(self):
        for phrase in (
            "Follow the wire with your eyes",
            "Climb onto the crate to get a better look",
            "Look around carefully",
            "Raise the camcorder and film the fence line",
        ):
            self.assertFalse(engine.is_hard_transition(phrase, ""), phrase)

    def test_a_hard_cut_does_not_pass_the_previous_frame_as_img2img_init(self):
        self._render(frame_idx=2, hard_transition=True)
        self.assertTrue(self.calls)
        for label, refs in self.calls:
            self.assertNotIn(self.prev_path, refs,
                             f"{label} was handed the previous frame: {refs}")

    def test_frame_one_hard_cut_is_not_forced_to_img2img_the_intro(self):
        self._render(frame_idx=1, hard_transition=True)
        self.assertTrue(self.calls)
        for label, refs in self.calls:
            self.assertNotIn(self.prev_path, refs,
                             f"{label} was handed the intro still: {refs}")

    def test_a_soft_continuation_still_uses_the_previous_frame(self):
        # INTERACT / look-around refine-from-current must keep working.
        self._render(frame_idx=2, hard_transition=False)
        img2img = [refs for label, refs in self.calls if label == "img2img"]
        self.assertTrue(img2img, self.calls)
        self.assertIn(self.prev_path, img2img[0])

    def test_a_live_capture_does_not_lock_a_hard_cut_to_the_current_frame(self):
        # MOVE TO must leave. Using the Reactor capture as img2img init
        # redrew the same yard every trip.
        live = Path(self._tmpdir.name) / "observed_123.png"
        live.write_bytes(self.PNG)
        live_path = str(live.resolve())
        import gemini_image_utils as giu
        orig = (giu.generate_gemini_img2img, giu.generate_with_gemini,
                giu.make_style_swatch)
        giu.generate_gemini_img2img = self._record("img2img")
        giu.generate_with_gemini = self._record("t2i")
        giu.make_style_swatch = lambda *a, **k: None
        try:
            engine._gen_image_impl(
                caption="Jason Fleece reaches the rusted truck.",
                mode="camcorder",
                choice="Move to the rusted truck",
                frame_idx=2,
                dispatch="You close on the truck.",
                world_prompt="",
                hard_transition=True,
                session_id="test_live_capture_lead",
                history_ref=[{
                    "choice": "Intro",
                    "vision_dispatch": "A desert yard.",
                    "image": live_path,
                    "image_url": live_path,
                    "guide_image": self.prev_path,
                    "live_capture": True,
                    "hard_transition": False,
                }],
            )
        finally:
            (giu.generate_gemini_img2img, giu.generate_with_gemini,
             giu.make_style_swatch) = orig
        self.assertTrue(self.calls)
        for label, refs in self.calls:
            self.assertNotIn(live_path, refs,
                             f"{label} was handed the live capture: {refs}")

    def test_a_live_capture_leads_img2img_on_a_soft_continuation(self):
        live = Path(self._tmpdir.name) / "observed_456.png"
        live.write_bytes(self.PNG)
        live_path = str(live.resolve())
        import gemini_image_utils as giu
        orig = (giu.generate_gemini_img2img, giu.generate_with_gemini,
                giu.make_style_swatch)
        giu.generate_gemini_img2img = self._record("img2img")
        giu.generate_with_gemini = self._record("t2i")
        giu.make_style_swatch = lambda *a, **k: None
        try:
            engine._gen_image_impl(
                caption="Jason Fleece inspects the vent.",
                mode="camcorder",
                choice="Look closer at the vent",
                frame_idx=2,
                dispatch="You lean in.",
                world_prompt="",
                hard_transition=False,
                session_id="test_live_capture_soft",
                history_ref=[{
                    "choice": "Intro",
                    "vision_dispatch": "A desert yard.",
                    "image": live_path,
                    "image_url": live_path,
                    "guide_image": self.prev_path,
                    "live_capture": True,
                    "hard_transition": False,
                }],
            )
        finally:
            (giu.generate_gemini_img2img, giu.generate_with_gemini,
             giu.make_style_swatch) = orig
        img2img = [refs for label, refs in self.calls if label == "img2img"]
        self.assertTrue(img2img, self.calls)
        self.assertEqual(img2img[0][0], live_path)


class TestTheGradeOfALeaving(unittest.TestCase):
    """A threshold and a destination are not the same move. One of them can be
    softened without lying about where the player is standing."""

    def test_a_door_is_a_portal(self):
        for phrase in ("Enter the dark door, moving inside into the space beyond.",
                       "Step through the doorway.",
                       "Squeeze through the hatch into the space beyond."):
            self.assertEqual(engine.transition_kind(phrase), "portal", phrase)

    def test_a_landmark_is_an_approach(self):
        self.assertEqual(
            engine.transition_kind("Move to the silo, crossing into the compound."),
            "approach")

    def test_standing_still_is_neither(self):
        self.assertEqual(engine.transition_kind("Photograph the gauge."), "")
        self.assertEqual(engine.transition_kind(""), "")

    def test_the_grade_agrees_with_the_boolean(self):
        """They share a detector precisely so they cannot drift apart — a
        `transition_kind` that said "portal" where `is_hard_transition` said no
        would exempt a turn from a throttle it was never subject to."""
        for phrase in ("Enter the dark door, moving inside into the space beyond.",
                       "Move to the silo, crossing into the compound.",
                       "Walk over to the oil pump and stop right in front of it.",
                       "Photograph the gauge.",
                       "Navigate deeper into the crawlspace."):
            self.assertEqual(bool(engine.transition_kind(phrase)),
                             engine.is_hard_transition(phrase, ""), phrase)


class TestASoftenedMoveStillMoves(unittest.TestCase):
    """The bug this pins is the one that made the game walk backwards.

    A throttled turn was built exactly like a turn where the player stood
    still, so the spatial anchor told the camera it could not leave a position
    the player had already left — and the render drew where they used to be.
    Vision then read that frame back into the compass, so the NEXT turn
    anchored on the old place too, and the run reversed a frame at a time.
    """

    HERE = "Ahead: a corrugated wall. Left: the open yard you came in from."

    def _prompt(self, choice="Enter the dark door, moving inside into the space beyond.",
                **kw):
        return engine.build_image_prompt(
            player_choice=choice,
            dispatch="The dark doorway swallows the torch beam.",
            prev_vision_analysis="a loading bay",
            prev_spatial=self.HERE,
            prev_setting="outdoor-industrial",
            **kw)

    def test_standing_still_is_still_pinned(self):
        """The anchor is load-bearing for turns that genuinely do not move.

        Asked with a stationary action, because that is the claim. This used to
        pass the fixture's doorway entry and rely on the anchor being the
        catch-all default — so it went on passing while walking was being
        pinned too, which was the whole bug.
        """
        prompt = self._prompt(choice="Photograph the gauge.")
        self.assertIn("has NOT moved from the reference position", prompt)
        self.assertIn(self.HERE, prompt)

    def test_a_softened_move_is_not_pinned(self):
        prompt = self._prompt(softened_move=True)
        self.assertNotIn("has NOT moved", prompt)

    def test_a_softened_move_is_told_it_moved(self):
        prompt = self._prompt(softened_move=True)
        self.assertTrue(
            "has MOVED FORWARD" in prompt
            or "follow camera moved forward WITH the character" in prompt,
            prompt[:400],
        )

    def test_a_softened_move_is_told_not_to_go_back(self):
        """Naming the failure is worth a line: the model's most available
        completion, given a reference frame and no instruction, is the
        reference frame."""
        prompt = self._prompt(softened_move=True)
        self.assertTrue(
            "do not turn back toward anywhere already left" in prompt
            or "do not turn the lens to face them" in prompt,
            prompt[:400],
        )

    def test_a_hard_cut_is_unaffected(self):
        prompt = self._prompt(hard_transition=True)
        self.assertNotIn("has NOT moved", prompt)
        self.assertIn("in a NEW PLACE", prompt)

    def test_only_one_camera_verdict_is_ever_stated(self):
        """The point of the rewrite. Each of these used to be announced by two
        or three separate blocks — an emoji-marked prefix, a bulleted movement
        suffix, and (on the scaffold path) an unconditional "render the SAME
        camera position" clause — which between them could assert both that the
        camera had travelled and that it must not. With precedence undefined the
        model went with whichever line was most concrete, and the concrete one
        was always the scene sentence. One verdict per turn, or we are back to
        the model arbitrating between us."""
        verdicts = (
            "in a NEW PLACE",
            "has MOVED FORWARD",
            "follow camera moved forward WITH the character",
            "has WALKED TO IT",
            "follow camera walked with the character",
            "panned or tilted slightly",
            "has NOT moved from the reference",
        )
        for label, kw in (
            ("hard cut", dict(hard_transition=True)),
            ("softened", dict(softened_move=True)),
            ("walk to", dict(choice="Walk over to the pump and stop right in front of it.")),
            ("stationary", dict(choice="Photograph the gauge.")),
        ):
            with self.subTest(label):
                prompt = self._prompt(**kw)
                stated = [v for v in verdicts if v in prompt]
                self.assertEqual(len(stated), 1, f"{label} stated {stated}")

    def test_the_scaffold_path_does_not_re_pin_a_camera_that_moved(self):
        """`build_image_prompt` has three base-prompt shapes, and the middle one
        (no `visual_scene`, but a prior vision analysis to scaffold from) used to
        end with "render the SAME camera position evolved ... keep ground type,
        environment type, lighting, and visible landmarks identical". That fired
        on movement turns too, immediately below a CAMERA line saying the camera
        had gone through a door. It was the last surviving copy of the
        contradiction and the easiest to miss, because it only appears when the
        LLM omits `visual_scene`."""
        prompt = self._prompt(softened_move=True)  # fixture omits narrative_dispatch
        self.assertTrue(
            "has MOVED FORWARD" in prompt
            or "follow camera moved forward WITH the character" in prompt,
        )
        self.assertNotIn("SAME camera position", prompt)
        self.assertNotIn("landmarks identical", prompt)


class TestMovementGuidanceSurvivesAMissingVisionReground(unittest.TestCase):
    """`prev_vision_analysis` / `prev_spatial` / `prev_setting` are backfilled
    into history by an async vision-reground worker that races the next
    turn's image generation — and a real 5-turn playtest showed it losing
    that race on every single turn (history.json landed with all three
    fields empty start to finish). `build_image_prompt` used to require
    that data before it would say ANYTHING about whether the camera moved:
    no reference text meant no 'YOU HAVE CROSSED THE SPACE', no 'FORWARD
    MOVEMENT', no 'LOCATION CHANGE' — just the bare narrative text and
    silence where the camera instruction should be. Silence reads to the
    image model as 'no opinion', and its most available completion with a
    reference frame in hand and no opinion is to redraw the reference. This
    is exactly why walking up to a backpack or a fresh pile of industrial
    machinery rendered as the same shot, one object nudged, turn after turn.
    Every movement-vs-stationary branch must work with NOTHING but
    `hard_transition` / `movement_type` / `softened_move` — vision text is
    enrichment on top, never the precondition."""

    def _prompt(self, choice, **kw):
        return engine.build_image_prompt(
            player_choice=choice,
            dispatch="The backpack sits on a heap of debris ahead.",
            narrative_dispatch="You stumble across the grease-slicked floor toward it.",
            prev_vision_analysis="",
            prev_spatial="",
            prev_setting="",
            **kw)

    def test_a_bare_forward_move_still_says_the_camera_crossed_the_space(self):
        prompt = self._prompt(
            "Walk over to the backpack and stop right in front of it, close enough to touch.")
        self.assertTrue(
            "has WALKED TO IT" in prompt
            or "follow camera walked with the character" in prompt,
        )
        self.assertNotIn("has NOT moved", prompt)

    def test_a_bare_hard_transition_still_says_location_change(self):
        prompt = self._prompt(
            "Enter the door, moving inside into the space beyond.", hard_transition=True)
        self.assertIn("in a NEW PLACE", prompt)
        self.assertNotIn("has NOT moved", prompt)
        self.assertNotIn("has WALKED TO IT", prompt)
        self.assertNotIn("follow camera walked with the character", prompt)

    def test_a_bare_softened_move_still_says_it_moved(self):
        prompt = self._prompt("Sprint toward the shadow line", softened_move=True)
        self.assertTrue(
            "has MOVED FORWARD" in prompt
            or "follow camera moved forward WITH the character" in prompt,
        )
        self.assertNotIn("has NOT moved", prompt)

    def test_a_bare_stationary_action_is_told_to_hold_rather_than_left_silent(self):
        prompt = self._prompt("Photograph the gauge.")
        self.assertIn("has NOT moved from the reference position", prompt)
        self.assertNotIn("has WALKED TO IT", prompt)
        self.assertNotIn("follow camera walked with the character", prompt)

    def test_reference_text_is_still_woven_in_when_it_does_arrive(self):
        """The fallback exists for when the data is missing — it must not
        crowd out the richer branch when the reground DID land in time."""
        prompt = engine.build_image_prompt(
            player_choice="Walk over to the backpack and stop right in front of it, close enough to touch.",
            dispatch="The backpack sits on a heap of debris ahead.",
            narrative_dispatch="You stumble across the grease-slicked floor toward it.",
            prev_vision_analysis="a loading bay strewn with crates",
            prev_spatial="Ahead: the backpack. Left: a rusted pillar.",
            prev_setting="indoor-industrial",
        )
        self.assertTrue(
            "has WALKED TO IT" in prompt
            or "follow camera walked with the character" in prompt,
        )
        self.assertIn("Still inside: indoor-industrial", prompt)
        self.assertIn("The previous frame showed: a loading bay", prompt)

    def test_the_reference_clause_is_stated_once_not_per_branch(self):
        """It used to be pasted into each of four movement branches AND opened
        the scaffold branch, so a scaffold turn said it twice. Duplication is
        how a 300-word prompt becomes a 2,000-word one, one defensible addition
        at a time."""
        prompt = engine.build_image_prompt(
            player_choice="Walk over to the backpack and stop right in front of it.",
            dispatch="The backpack sits on a heap of debris ahead.",
            narrative_dispatch="You stumble toward it.",
            prev_vision_analysis="a loading bay strewn with crates",
        )
        self.assertEqual(prompt.count("a loading bay strewn with crates"), 1)


class TestTheImagePayloadHasOneVoice(unittest.TestCase):
    """Measured before the trim: one image call rendered to 14,078 chars /
    2,139 words, of which roughly forty described the actual frame. Nine
    separate layers legislated the camera, and two of them shipped mutually
    exclusive precedence claims — the cast directive opened "THIS OVERRIDES ANY
    CONFLICTING CAMERA LANGUAGE BELOW" while the img2img template opened
    "NOTHING BELOW MAY OVERRULE IT".

    A prompt that has to declare precedence over its own other clauses already
    has a structural conflict; with several such declarations precedence is
    undefined, and the model stops arbitrating by authority and takes whichever
    line is most CONCRETE. That is always the literal scene sentence — which is
    why capitalised camera directives lost to "the player collapsed on the red
    dirt floor just outside", and why MOVE TO kept rendering as the previous
    frame with the chosen object pasted into it.

    These tests pin the properties that keep it that way: no precedence claims,
    no blanket spatial lock, no perspective contradiction, and a payload small
    enough that the scene is still legible inside it.
    """

    def _rendered(self, scene: str = "Some scene description.") -> str:
        import prompts_store
        return prompts_store.render_image_template(
            "gemini_image_to_image_instructions", scene)

    def test_no_unconditional_same_direction_lock_survives(self):
        out = self._rendered()
        self.assertNotIn("SAME HEIGHT and pointing in the SAME DIRECTION", out)
        self.assertNotIn("SAME spatial arrangement of objects", out)

    def test_nothing_in_the_payload_claims_precedence_over_anything_else(self):
        out = self._rendered()
        for claim in ("OVERRULE", "OVERRIDES", "READ THIS FIRST", "NON-NEGOTIABLE",
                      "takes precedence", "HIGHEST PRIORITY"):
            self.assertNotIn(claim, out, f"payload still asserts {claim!r}")

    def test_the_payload_stays_small_enough_for_the_scene_to_be_heard(self):
        """Not a style preference. At 2,139 words the ~40 words that describe
        this particular frame were 2% of the payload, and the model reliably
        spent its attention on the other 98%."""
        words = len(self._rendered().split())
        self.assertLess(words, 800, f"image payload back up to {words} words")

    def test_the_shared_blocks_never_contradict_the_active_perspective(self):
        """The bug that survived longest, because it was invisible: the shared
        blocks are substituted into the template DOWNSTREAM of
        `game_identity.apply`, so `image_camera_rules` reached the model exactly
        as written in JSON. In third person that meant every single frame
        carried both "the PLAYER CHARACTER is fully visible — head to feet" and,
        a thousand words later, "NEVER show your face, head, or full body" /
        "no part of you exists in frame". Asked to both show and hide the
        protagonist on every render, the model flip-flopped between framings and
        defaulted to changing as little as possible from the reference."""
        import game_identity, prompts_store
        spec = game_identity.get_spec()
        if not game_identity.shows_character(spec):
            self.skipTest("shipped default is first person; nothing to contradict")
        out = prompts_store.render_image_template(
            "gemini_image_to_image_instructions", "SCENE").lower()
        for contradiction in (
            "never show your face",
            "no part of you exists in frame",
            "camera operator does not exist",
            "first-person",
            "first person",
        ):
            self.assertNotIn(contradiction, out)


class TestTheSoftChainDoesNotCompoundForever(unittest.TestCase):
    """A soft turn generates from the previous GENERATED frame, so an
    unbroken run of them is an edit of an edit of an edit. Nothing bounded
    that: the chain only reset at a hard cut, and before travel was
    detectable outdoors a run could be twenty frames long, by which point
    colour, contrast and texture have drifted into mush."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _frame(self, name):
        p = Path(self._tmp.name) / f"{name}.png"
        p.write_bytes(b"x")
        return str(p)

    def _history(self, soft_frames):
        hist = [{"image": self._frame("anchor"), "hard_transition": True}]
        for i in range(soft_frames):
            hist.append({"image": self._frame(f"soft{i}"), "hard_transition": False})
        return hist

    def test_a_short_run_keeps_compounding(self):
        """Below the cap continuity is worth more than the small loss."""
        for n in range(engine.IMG2IMG_SOFT_CHAIN_MAX + 1):
            self.assertIsNone(
                engine._soft_chain_anchor(self._history(n),
                                          engine.IMG2IMG_SOFT_CHAIN_MAX),
                f"{n} soft frames should not re-anchor",
            )

    def test_a_long_run_re_anchors_on_the_clean_frame(self):
        hist = self._history(engine.IMG2IMG_SOFT_CHAIN_MAX + 2)
        anchor = engine._soft_chain_anchor(hist, engine.IMG2IMG_SOFT_CHAIN_MAX)
        self.assertEqual(anchor, hist[0]["image"])

    def test_the_previous_frame_is_still_kept_for_spatial_state(self):
        src = open("engine.py", encoding="utf-8").read()
        self.assertIn("ref_images_to_use = [anchor] + ref_images_to_use", src)

    def test_no_history_and_no_prior_cut_are_survivable(self):
        self.assertIsNone(engine._soft_chain_anchor([], 3))
        self.assertIsNone(engine._soft_chain_anchor(
            [{"image": self._frame("a")} for _ in range(9)], 3))


class TestMoveOnlyCutsForEnterableThings(unittest.TestCase):
    """Walking to a rock used to compose a brand-new world, same as walking
    through a door — which is what made MOVE read as teleporting."""

    def test_entering_cuts(self):
        for phrase in ("Enter the tunnel, moving inside into the space beyond.",
                       "Enter the warehouse, moving inside into the space beyond."):
            self.assertTrue(engine.is_hard_transition(phrase, ""))

    def test_walking_across_the_room_does_not(self):
        self.assertFalse(engine.is_hard_transition(
            "Walk over to the oil pump and stop right in front of it, close enough to touch.", ""))


class TestMoveAsksForTravel(unittest.TestCase):
    """MOVE and INTERACT shared one directive that said the player was
    "handling" the object. On a MOVE turn that describes the wrong act, and the
    frame came back from an unmoved camera — the run showed things happening
    near the player instead of the player crossing the ground toward them."""

    def _move(self):
        return engine._action_directive(is_interaction=True, is_move=True,
                                        subject="oil pump")

    def _interact(self):
        return engine._action_directive(is_interaction=True, is_move=False,
                                        subject="oil pump")

    def test_a_plain_choice_gets_no_object_directive(self):
        self.assertEqual(
            engine._action_directive(is_interaction=False, is_move=False, subject=""), "")

    def test_move_demands_the_camera_travelled(self):
        d = self._move()
        self.assertIn("NEW VANTAGE POINT", d)
        self.assertIn("TRAVERSAL", d)
        self.assertIn("FAR SIDE", d)

    def test_move_is_not_described_as_handling_the_object(self):
        d = self._move()
        self.assertNotIn("OBJECT INTERACTION", d)
        self.assertIn("not handling", d)

    def test_interact_still_works_the_object_in_place(self):
        d = self._interact()
        self.assertIn("OBJECT INTERACTION", d)
        self.assertNotIn("NEW VANTAGE POINT", d)

    def test_both_verbs_still_carry_the_permanence_clause(self):
        for d in (self._move(), self._interact()):
            self.assertIn("oil pump", d)
            self.assertIn("visual_scene", d)


class TestInteractIsShelved(unittest.TestCase):
    """INTERACT pokes the live world model, which reacts too weakly today for
    the poke to show at all. While that is true it must not sit on the tag next
    to MOVE TO splitting players onto the dead path."""

    @classmethod
    def setUpClass(cls):
        cls.client_src = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")

    def test_the_button_is_switched_off(self):
        self.assertIn("const INTERACT_ENABLED = false;", self.client_src)

    def test_the_action_bar_honours_the_switch(self):
        # SCAN_ACTIONS is filtered by each action's `when` predicate, so the
        # gate has to hang off INTERACT's entry or the button renders anyway.
        self.assertIn("when: () => INTERACT_ENABLED,", self.client_src)
        self.assertIn("SCAN_ACTIONS.filter((a) => !a.when || a.when(obj))",
                      self.client_src)

    def test_the_definition_survives_for_when_it_comes_back(self):
        self.assertIn('id: "interact", label: "INTERACT"', self.client_src)


class TestEnvironmentStagnation(unittest.TestCase):
    """Vision already labelled every turn's environment and nothing read it, so
    nothing noticed a run that had been in one kind of place for 27 turns."""

    def test_the_same_kind_of_place_accumulates(self):
        state = {}
        for expected in (1, 2, 3):
            self.assertEqual(
                engine.update_environment_streak(state, "indoor-tunnel"), expected)

    def test_going_somewhere_else_resets_it(self):
        state = {}
        engine.update_environment_streak(state, "indoor-tunnel")
        engine.update_environment_streak(state, "indoor-tunnel")
        self.assertEqual(engine.update_environment_streak(state, "outdoor-desert"), 1)

    def test_a_missing_label_is_not_evidence_of_movement(self):
        # An empty vision call must not silently clear a streak.
        state = {}
        engine.update_environment_streak(state, "indoor-tunnel")
        engine.update_environment_streak(state, "indoor-tunnel")
        self.assertEqual(engine.update_environment_streak(state, ""), 2)
        self.assertEqual(engine.update_environment_streak(state, "indoor-tunnel"), 3)

    def test_an_unreadable_turn_with_no_scene_text_holds_rather_than_guesses(self):
        # No label AND no prose to compare is total absence of evidence. Nothing
        # here says "same place", so nothing should count as more time in place —
        # that used to be a blind `+= 1` on every call, which is exactly the bug
        # that turned a 45-turn chase across a desert, a truck, a drainage ditch
        # and a facility interior into "stuck" every time vision didn't answer.
        state = {}
        engine.update_environment_streak(state, "")
        for _ in range(4):
            self.assertEqual(engine.update_environment_streak(state, ""), 1)

    def test_repeating_machine_prose_with_no_room_word_still_accumulates(self):
        # A run fixated on one oil derrick describes a different PART of it each
        # turn — pump, drive rod, floor grate, oil pit — so no two turns share
        # much vocabulary with each other. They all share vocabulary with the
        # growing pool of everything said about that derrick so far, which is
        # what actually catches this stall.
        state = {}
        scenes = [
            "The rusted iron oil pump looms, its casing streaked with old oil.",
            "The oil pump's drive rod vibrates, grease coating the rusted iron housing.",
            "A jagged iron floor grate beside the rusted pump vents oily steam near the casing.",
            "The oil pit beneath the rusted drive rod churns with black sludge.",
        ]
        streaks = [engine.update_environment_streak(state, "", scene_text=s) for s in scenes]
        self.assertEqual(streaks, [1, 2, 3, 4])

    def test_genuinely_different_prose_does_not_fake_a_streak(self):
        # Crawling under a truck, then through a drainage ditch, then into a
        # facility pipe: three different real places, none of which name a
        # taxonomy room, and none of which share much vocabulary either.
        state = {}
        scenes = [
            "Underneath the transport truck, oily mud and gravel fill the frame.",
            "A narrow concrete drainage ditch stretches ahead into the dark.",
            "The cylindrical metal pipe interior extends forward, floor slick with sludge.",
        ]
        streaks = [engine.update_environment_streak(state, "", scene_text=s) for s in scenes]
        self.assertEqual(streaks, [1, 1, 1])

    def test_a_hard_cut_is_movement_even_with_no_label(self):
        state = {}
        for _ in range(5):
            engine.update_environment_streak(state, "", scene_text="the rusted oil pump")
        self.assertEqual(
            engine.update_environment_streak(state, "", hard_transition=True,
                                             scene_text="a bright open desert"), 1)

    def test_a_remembered_place_survives_an_unreadable_turn(self):
        state = {}
        engine.update_environment_streak(state, "indoor-tunnel")
        engine.update_environment_streak(state, "")
        # Going somewhere genuinely different still resets, and the blank turn
        # in the middle did not wipe the remembered place.
        self.assertEqual(engine.update_environment_streak(state, "outdoor-desert"), 1)

    def test_label_formatting_does_not_split_a_streak(self):
        state = {}
        engine.update_environment_streak(state, "Indoor Tunnel")
        self.assertEqual(engine.update_environment_streak(state, "indoor-tunnel"), 2)


class TestSettingFallback(unittest.TestCase):
    """Vision labels the environment, but on the live turn path Phase 2 runs
    before the frame has rendered, so it labels nothing at all. The guard needs
    a signal that survives that."""

    def test_it_reads_a_kind_of_space_out_of_scene_prose(self):
        self.assertEqual(
            engine.classify_setting("The narrow corridor runs ahead into the dark"),
            "indoor-corridor")
        self.assertEqual(
            engine.classify_setting("Open sand stretches to the horizon"),
            "outdoor-desert")

    def test_prose_about_a_machine_names_no_room(self):
        # An oil pump stands outdoors as happily as in; guessing a warehouse off
        # it would also tell the drift beats to stop the wind.
        self.assertEqual(
            engine.classify_setting(
                "The massive rusted iron oil pump dominates the frame, its drive "
                "rod vibrating inches from the lens"),
            "")

    def test_a_word_inside_another_word_is_not_a_room(self):
        # "vented" contains "vent"; substring counting used to break a streak
        # that had never left the spot.
        self.assertEqual(
            engine.classify_setting("the fissure now vented wide with thick steam"), "")

    def test_nothing_at_all_is_not_a_guess(self):
        self.assertEqual(engine.classify_setting(""), "")
        self.assertEqual(engine.classify_setting("   "), "")

    def test_vision_wins_when_it_ran(self):
        state = {"current_render_base": "a long corridor"}
        hist  = [{"setting_type": "outdoor-desert"}]
        self.assertEqual(engine.resolve_environment_label(state, hist), "outdoor-desert")

    def test_the_render_base_answers_when_vision_did_not(self):
        state = {"current_render_base": "a long corridor running into the dark"}
        self.assertEqual(
            engine.resolve_environment_label(state, [{"setting_type": ""}]),
            "indoor-corridor")

    def test_the_style_preamble_does_not_vote(self):
        # Every render base opens with the same style preamble. If that scored,
        # every turn would carry the same label and the streak would be noise.
        self.assertEqual(engine.classify_setting(engine.REALTIME_STYLE_ANCHOR), "")


class TestContentWordOverlap(unittest.TestCase):
    """The anti-loop's last resort when no room word is present at all: does
    this turn's vocabulary already belong to the streak's accumulated pool."""

    def test_the_style_preamble_contributes_no_words(self):
        preamble = engine.REALTIME_STYLE_ANCHOR
        self.assertEqual(engine._content_words(preamble), frozenset())

    def test_shared_nouns_score_high(self):
        a = engine._content_words("the rusted oil pump looms overhead")
        b = engine._content_words("oily rusted pipes surround the pump housing")
        self.assertGreaterEqual(engine._overlap_coefficient(a, b), 0.3)

    def test_unrelated_scenes_score_low(self):
        a = engine._content_words("the rusted oil pump looms overhead")
        b = engine._content_words("sand dunes stretch toward the shimmering horizon")
        self.assertEqual(engine._overlap_coefficient(a, b), 0.0)

    def test_either_side_empty_is_zero_not_a_crash(self):
        self.assertEqual(engine._overlap_coefficient(frozenset(), frozenset({"pump"})), 0.0)
        self.assertEqual(engine._overlap_coefficient(frozenset({"pump"}), frozenset()), 0.0)


class TestRegroundFindsTheFrame(unittest.TestCase):
    """The reground worker locates this turn's frame on disk from the state's
    image URL. That URL carries a ?session=… query string, which basename()
    happily keeps — so for a long time it built a path that could not exist,
    waited out its full timeout every turn, and never regrounded anything."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="reground_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.name = "1234_a_rusted_door.png"
        open(os.path.join(self.dir, self.name), "wb").close()

    def _resolve(self, url: str) -> str:
        # Mirrors the path the worker builds from current_image_url.
        return os.path.join(self.dir, os.path.basename(url.split("?", 1)[0]))

    def test_a_plain_url_resolves(self):
        self.assertTrue(os.path.exists(self._resolve(f"/images/{self.name}")))

    def test_the_session_query_string_does_not_hide_the_file(self):
        url = f"/images/{self.name}?session=postfix_scan6"
        self.assertTrue(os.path.exists(self._resolve(url)))

    def test_the_query_string_really_was_the_problem(self):
        # Guard the guard: the naive version genuinely fails on this input, so
        # this test would have caught the original bug.
        url = f"/images/{self.name}?session=postfix_scan6"
        naive = os.path.join(self.dir, os.path.basename(url))
        self.assertFalse(os.path.exists(naive))


class TestEgressEnforcement(unittest.TestCase):
    """The directive asks the model for a way out; this is what happens when it
    offers a third way deeper in instead."""

    DEEPER = ["Crawl deeper into the shaft",
              "Squeeze through the narrowing rib",
              "Push further into the dark"]

    def test_a_short_streak_is_left_alone(self):
        self.assertEqual(engine.enforce_egress_option(list(self.DEEPER), 1), self.DEEPER)

    def test_a_long_streak_gets_a_way_out(self):
        out = engine.enforce_egress_option(list(self.DEEPER), engine.ENVIRONMENT_STREAK_LIMIT)
        self.assertTrue(any(engine.is_egress_choice(c) for c in out))

    def test_the_slate_keeps_its_size(self):
        out = engine.enforce_egress_option(list(self.DEEPER), engine.ENVIRONMENT_STREAK_LIMIT)
        self.assertEqual(len(out), len(self.DEEPER))

    def test_the_model_s_two_best_options_survive(self):
        out = engine.enforce_egress_option(list(self.DEEPER), engine.ENVIRONMENT_STREAK_LIMIT)
        self.assertEqual(out[:2], self.DEEPER[:2])

    def test_an_existing_way_out_is_not_overwritten(self):
        offered = ["Crawl deeper into the shaft",
                   "Climb back out toward the pipe",
                   "Push further into the dark"]
        self.assertEqual(
            engine.enforce_egress_option(list(offered), engine.ENVIRONMENT_STREAK_LIMIT),
            offered)

    def test_the_forced_option_rotates(self):
        # A run that stays stuck must not be handed the same sentence forever.
        first = engine.enforce_egress_option(list(self.DEEPER), engine.ENVIRONMENT_STREAK_LIMIT)
        second = engine.enforce_egress_option(list(self.DEEPER), engine.ENVIRONMENT_STREAK_LIMIT + 1)
        self.assertNotEqual(first[-1], second[-1])

    def test_the_prompt_only_nags_when_stuck(self):
        self.assertEqual(engine.stagnation_directive(1), "")
        self.assertIn("next_choices",
                      engine.stagnation_directive(engine.ENVIRONMENT_STREAK_LIMIT))


class TestScanInteractionRisk(unittest.TestCase):
    """`risk_boost` existed and only the web SCAN path passed it. These pin that
    it actually changes the odds rather than just being threaded through."""

    @staticmethod
    def _unlucky_rate(bias, n=4000, seed=7):
        random.seed(seed)
        return sum(engine.compute_fate(bias) == "UNLUCKY" for _ in range(n)) / n

    def test_baseline_matches_the_documented_split(self):
        self.assertAlmostEqual(self._unlucky_rate(0.0), 0.25, delta=0.03)

    def test_poking_an_object_makes_fate_meaner(self):
        # A SCAN interaction in the 'normal' phase: phase_bias 0 + 0.15.
        self.assertGreater(self._unlucky_rate(0.15), self._unlucky_rate(0.0) + 0.10)

    def test_late_game_meddling_is_the_meanest(self):
        # 'critical' phase (0.22) + interaction (0.15). Asserted relatively, not
        # against an absolute rate, because FATE_UNLUCKY_CEILING now caps how
        # mean a turn can get — the ordering is the thing that matters.
        self.assertGreater(self._unlucky_rate(0.37), self._unlucky_rate(0.15))
        self.assertGreater(self._unlucky_rate(0.15), self._unlucky_rate(0.0))

    def test_misfortune_stops_short_of_becoming_the_house_rule(self):
        """However deep the run gets, a majority-but-not-overwhelming share."""
        self.assertLessEqual(self._unlucky_rate(0.5), engine.FATE_UNLUCKY_CEILING + 0.03)

    def test_luck_thins_as_risk_climbs_but_never_runs_out(self):
        """This test used to assert `lucky == 0` at high risk, which is the bug
        rather than the requirement. Luck SHOULD thin — that is what risk means —
        but scan_move adds its +0.15 on every single turn, so combined with phase
        and detection bias the old `0.25 - bias` floored at zero from roughly
        turn three of every run. A measured 13-turn run rolled LUCKY not once.
        With no relief available the world only ever punishes, and a player reads
        that as a simulation that has stopped reacting to them."""
        def lucky_rate(bias, n=4000, seed=11):
            random.seed(seed)
            return sum(engine.compute_fate(bias) == "LUCKY" for _ in range(n)) / n

        self.assertLess(lucky_rate(0.37), lucky_rate(0.0))
        for bias in (0.37, 0.5, 1.0):
            with self.subTest(bias=bias):
                self.assertGreater(lucky_rate(bias), 0.05,
                                   "a break must stay possible at any risk")


class TestStoryDynamicsDriveTheState(unittest.TestCase):
    """End-to-end over one temp session: the dials must actually persist."""

    SID = "pacingtest_session"

    def setUp(self):
        self.addCleanup(shutil.rmtree, engine._get_session_root(self.SID), True)
        state = engine._load_state(self.SID)
        state.update({"threat_level": 0, "current_phase": "normal",
                      "chaos_level": 0, "time_of_day": EVENING})
        engine._save_state(state, self.SID)

    def test_a_scan_interaction_pushes_threat_harder_than_a_button(self):
        dyn = engine.advance_story_dynamics(session_id=self.SID, risk_boost=2)
        self.assertEqual(dyn["threat_level"], 1 + engine.MAX_RISK_THREAT_BOOST)
        self.assertGreater(dyn["threat_level"], 1)

    def test_meddling_cannot_burn_through_the_whole_ladder(self):
        """At the original uncapped boost, a SCAN run that moved every turn hit
        'critical' on turn 3 and spent its last 27 turns with nowhere to go."""
        for _ in range(3):
            dyn = engine.advance_story_dynamics(session_id=self.SID, risk_boost=2)
        self.assertNotEqual(dyn["phase"], "critical")

    def test_crossing_into_a_new_phase_does_not_relight_the_evening(self):
        """Phase is the tension dial. Stepping the lighting string on an act
        break made every hard-cut frame a new time of day, because that
        string is what image generation injects. The sky stays put."""
        start = engine._load_state(self.SID)["time_of_day"]
        seen = []
        for _ in range(engine.STORY_ESCALATE_AT):
            dyn = engine.advance_story_dynamics(session_id=self.SID)
            seen.append((dyn["phase"], dyn["time_of_day"]))
        self.assertTrue(any(phase == "escalating" for phase, _ in seen),
                        "threat never reached the escalating phase")
        self.assertTrue(all(tod == start for _, tod in seen))
        self.assertEqual(engine._load_state(self.SID)["time_of_day"], start)

    def test_the_light_does_not_move_between_ordinary_turns(self):
        first = engine.advance_story_dynamics(session_id=self.SID)
        second = engine.advance_story_dynamics(session_id=self.SID)
        self.assertEqual(first["phase"], "normal")
        self.assertEqual(first["time_of_day"], second["time_of_day"])


class TestVisualToneStaysFixed(unittest.TestCase):
    """summarize_world_prompt_for_image() used to call the LLM fresh every
    turn and got a different lighting/mood gloss each time ("flickering
    sodium-vapor highlights" vs "high-contrast crimson strobe lighting" vs
    "oppressive desert twilight") for the SAME unbroken scene — a visible
    day-for-night flicker across a run whose time_of_day string never
    changed. It must now be pinned per session and only re-asked on an
    actual hard transition (or the first frame)."""

    SID = "tonetest_session"

    def setUp(self):
        engine._VISUAL_TONE_CACHE.pop(self.SID, None)
        self.addCleanup(engine._VISUAL_TONE_CACHE.pop, self.SID, None)
        self._orig_ask = engine._ask
        self.calls = []

        def fake_ask(prompt, **kwargs):
            self.calls.append(prompt)
            return f"tone #{len(self.calls)}"

        engine._ask = fake_ask
        self.addCleanup(setattr, engine, "_ask", self._orig_ask)

    def test_a_continuous_scene_reuses_the_first_tone(self):
        first = engine.summarize_world_prompt_for_image(
            "OVERWHELMING CHAOS! decisive action is paramount", session_id=self.SID,
            hard_transition=False, frame_idx=2,
        )
        second = engine.summarize_world_prompt_for_image(
            "the red biome is dangerously close", session_id=self.SID,
            hard_transition=False, frame_idx=3,
        )
        third = engine.summarize_world_prompt_for_image(
            "you are being pursued by hostile forces", session_id=self.SID,
            hard_transition=False, frame_idx=4,
        )
        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertEqual(len(self.calls), 1,
                          "later turns in the same scene re-asked the LLM instead of reusing the tone")

    def test_a_hard_transition_is_allowed_to_relight(self):
        first = engine.summarize_world_prompt_for_image(
            "a dusty industrial yard", session_id=self.SID, hard_transition=False, frame_idx=2,
        )
        second = engine.summarize_world_prompt_for_image(
            "you step through the doorway into a dark interior", session_id=self.SID,
            hard_transition=True, frame_idx=3,
        )
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.calls), 2)

    def test_the_first_frame_always_asks_fresh(self):
        engine._VISUAL_TONE_CACHE[self.SID] = "stale tone from a previous run"
        engine.summarize_world_prompt_for_image(
            "System Online.", session_id=self.SID, hard_transition=False, frame_idx=0,
        )
        self.assertEqual(len(self.calls), 1)

    def test_the_prompt_bans_camcorder_and_vhs_language(self):
        engine.summarize_world_prompt_for_image(
            "equipped with a heavy-duty 1993 VHS camcorder", session_id=self.SID,
            hard_transition=False, frame_idx=0,
        )
        self.assertIn("camcorder", self.calls[0].lower())
        self.assertTrue(
            "never mention a camcorder" in self.calls[0].lower()
            or "must not leak" in self.calls[0].lower(),
            "the instruction no longer tells the model to keep equipment out of the tone gloss",
        )

    def test_a_relight_is_anchored_to_the_previous_tone(self):
        engine.summarize_world_prompt_for_image(
            "a dusty industrial yard", session_id=self.SID, hard_transition=False, frame_idx=2,
        )
        engine.summarize_world_prompt_for_image(
            "you step through the doorway into a dark interior", session_id=self.SID,
            hard_transition=True, frame_idx=3,
        )
        self.assertIn("tone #1", self.calls[1],
                      "a hard-transition relight did not anchor on the previous tone")


if __name__ == "__main__":
    unittest.main(verbosity=2)
