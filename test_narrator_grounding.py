"""
test_narrator_grounding.py — what the narrator is actually told before it speaks.

The complaint this exists for: the narrator's lines read as repetitive and
unresponsive to the scene. Both causes were in the inputs, not the model.

  * `{scene}` was the whole `world_prompt` — a 1200-1500 word document about the
    world at large, slow-moving at the top, pasted under the label "CURRENT
    SCENE" with no clip. The narrator was handed a wall of background and almost
    nothing about the frame in front of the player, and that bulk drowned the
    recent beats underneath it. Near-identical inputs produce near-identical
    lines.
  * The shipped direction commanded the CONTENT of every line: "say how you FEEL
    — afraid, uneasy, but determined" and "make it clear you have to find out
    what happened here". Every unfocused narration was ordered onto the same
    beat. That is authored repetition, not model drift.
  * Nothing ever told the narrator what it had already said, so "don't repeat
    yourself" was a hope rather than an instruction.

These tests pin the inputs, which is where the defect lived. Whether a given
line is good is a judgment no test can make; whether the line was written from
the current frame, the current state, and a list of what not to say again, is
exactly what a test can make.

No network: `engine._ask` is stubbed, so the prompt is captured rather than sent.

Run with:
    python3 -m unittest test_narrator_grounding -v
"""

import json
import os
import shutil
import unittest
from pathlib import Path

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("ANTHROPIC_API_KEY", "")

import engine

ROOT = Path(__file__).parent.resolve()
SESSION_ID = "narrator-test"

ON_SCREEN = (
    "A flooded stairwell descending into black water, handrail sheared off, "
    "emergency light strobing red on the wet concrete."
)
# The tail marker sits far past any sane clip: if it reaches the prompt, the
# whole document is being pasted in again.
WORLD_DOC = (
    "THE WORLD: a decommissioned research station in the high desert. " * 60
) + "TAIL_OF_THE_WORLD_DOCUMENT"


def _discard_test_session():
    shutil.rmtree(engine._get_session_root(SESSION_ID), ignore_errors=True)


class NarratorPromptCase(unittest.TestCase):
    """Captures the prompt `_narrator_script` builds instead of sending it."""

    def setUp(self):
        engine.LLM_ENABLED = True
        self._real_ask = engine._ask
        self.prompts = []

        def fake_ask(prompt, **kwargs):
            self.prompts.append(prompt)
            return "The water has reached the third step since I last looked."

        engine._ask = fake_ask
        _discard_test_session()

    def tearDown(self):
        engine._ask = self._real_ask
        _discard_test_session()

    def seed(self, **overrides):
        st = engine._load_state(SESSION_ID)
        st.update({
            "world_prompt": WORLD_DOC,
            "turn_count": 4,
            "current_phase": "normal",
            "player_state": {"alive": True},
            "feed_log": [],
        })
        st.update(overrides)
        engine._save_state(st, SESSION_ID)
        return st

    def narrate(self, focus="", multi=False):
        engine._narrator_script(focus, multi, SESSION_ID)
        self.assertTrue(self.prompts, "the narrator asked the model for nothing")
        return self.prompts[-1]


class TestSceneIsWhatIsOnScreen(NarratorPromptCase):

    def test_prefers_the_vision_of_the_rendered_frame(self):
        self.seed(current_observed_vision=ON_SCREEN,
                  current_image_prompt="a corridor of humming pipes")
        p = self.narrate()
        self.assertIn("flooded stairwell", p)
        self.assertNotIn("humming pipes", p)

    def test_falls_back_to_the_prompt_the_frame_was_drawn_from(self):
        self.seed(current_image_prompt=ON_SCREEN)
        self.assertIn("flooded stairwell", self.narrate())

    def test_falls_back_to_the_world_document_last(self):
        # Still better than nothing on turn one, before any frame has rendered.
        self.seed()
        self.assertIn("research station", self.narrate())

    def test_the_world_document_no_longer_floods_the_prompt(self):
        # The whole point: 1200+ words of background used to be pasted in whole.
        self.seed()
        self.assertNotIn("TAIL_OF_THE_WORLD_DOCUMENT", self.narrate(),
                         "the world document is being pasted in unclipped again")

    def test_the_scene_is_clipped_even_when_it_is_long(self):
        self.seed(current_observed_vision="x" * 5000)
        self.assertNotIn("x" * 500, self.narrate())


class TestTheLiveStateReachesTheLine(NarratorPromptCase):
    """The signals that actually differ turn to turn. Without them the only
    thing separating two narrations is model temperature."""

    def test_time_of_day_is_included(self):
        self.seed(current_observed_vision=ON_SCREEN,
                  time_of_day="3:40am | weather: freezing fog")
        self.assertIn("freezing fog", self.narrate())

    def test_an_escalating_phase_is_named(self):
        self.seed(current_observed_vision=ON_SCREEN, current_phase="critical")
        self.assertIn("critical", self.narrate())

    def test_a_normal_phase_is_not_worth_saying(self):
        self.seed(current_observed_vision=ON_SCREEN, current_phase="normal")
        self.assertNotIn("the situation is normal", self.narrate())

    def test_being_hunted_is_carried(self):
        """This used to assert the tracked injury list reached the line. That
        list is gone with the wound parser; being hunted is the live stake now,
        and the narrator gets the wound prose from RECENT EVENTS regardless."""
        self.seed(current_observed_vision=ON_SCREEN,
                  detection={"heat": 9, "level": 3, "since_turn": 1})
        self.assertIn("hunted", self.narrate())

    def test_staying_hidden_is_not_worth_saying(self):
        self.seed(current_observed_vision=ON_SCREEN,
                  detection={"heat": 0, "level": 0, "since_turn": 0})
        self.assertNotIn("you are hidden", self.narrate())

    def test_the_action_just_taken_reaches_the_line(self):
        self.seed(current_observed_vision=ON_SCREEN,
                  last_choice="Move to the rusted truck")
        p = self.narrate()
        self.assertIn("Move to the rusted truck", p)
        self.assertIn("THE PLAYER JUST DID", p)

    def test_acted_on_the_request_beats_stale_last_choice(self):
        # MOVE TO narrates on the click, before /api/choose writes last_choice.
        self.seed(current_observed_vision=ON_SCREEN,
                  last_choice="Look at the vent")
        engine._narrator_script(
            "The player has just committed to travel to the rusted truck.",
            False, SESSION_ID, acted="Move to the rusted truck",
        )
        p = self.prompts[-1]
        self.assertIn("Move to the rusted truck", p)
        self.assertIn("WHERE THEY WERE LEAVING", p)
        self.assertNotIn("Look at the vent", p)


class TestDoNotRepeatYourself(NarratorPromptCase):

    def test_previously_spoken_lines_are_shown_to_the_model(self):
        self.seed(current_observed_vision=ON_SCREEN,
                  narrator_recent=["I have to find out what happened here."])
        p = self.narrate()
        self.assertIn("I have to find out what happened here.", p)
        self.assertIn("do not repeat", p.lower())

    def test_nothing_spoken_yet_adds_no_block(self):
        self.seed(current_observed_vision=ON_SCREEN)
        self.assertNotIn("do not repeat", self.narrate().lower())

    def test_the_avoid_list_reaches_the_focused_line_too(self):
        # The MOVE TO bridge is the most repeated line in the game — it fires on
        # every travel beat, so it is the one that most needs this.
        self.seed(current_observed_vision=ON_SCREEN,
                  narrator_recent=["The dark is thicker down here."])
        p = self.narrate(focus="Say one line about leaving for the water tower.")
        self.assertIn("The dark is thicker down here.", p)

    def test_the_avoid_list_reaches_the_multi_voice_script(self):
        self.seed(current_observed_vision=ON_SCREEN,
                  narrator_recent=["Something moved in the water."])
        p = self.narrate(multi=True)
        self.assertIn("Something moved in the water.", p)


class TestNarrationIsClippedToABeat(unittest.TestCase):
    """The register is short declaratives stacked into a beat. Clipping at the
    first full stop could only ever deliver the first third of a line."""

    def test_one_sentence_is_the_default(self):
        self.assertEqual(
            engine._clip_narration("The gate was open. Nobody had opened it."),
            "The gate was open.")

    def test_a_beat_keeps_its_short_declaratives(self):
        line = "The gate was open. Nobody had opened it. I went in."
        self.assertEqual(engine._clip_narration(line, 3), line)

    def test_the_ceiling_still_holds(self):
        line = "One. Two. Three. Four."
        self.assertEqual(engine._clip_narration(line, 3), "One. Two. Three.")

    def test_an_ellipsis_is_a_pause_not_an_ending(self):
        self.assertEqual(
            engine._clip_narration("The water was still... too still."),
            "The water was still... too still.")

    def test_an_unterminated_line_gets_its_full_stop(self):
        self.assertEqual(engine._clip_narration("Nobody came back"),
                         "Nobody came back.")

    def test_a_long_tail_is_dropped_rather_than_read_out(self):
        long_second = " " + ("and the dust went on for miles " * 12) + "."
        out = engine._clip_narration("The rig was dead." + long_second, 3)
        self.assertEqual(out, "The rig was dead.")

    def test_one_sentence_is_the_floor_even_when_it_runs_long(self):
        """Better a long first sentence than a line cut off mid-thought."""
        long_first = "The rig " + ("went on and on " * 30) + "."
        self.assertEqual(engine._clip_narration(long_first, 3), long_first)


class TestNarrationMemory(unittest.TestCase):

    def setUp(self):
        _discard_test_session()

    def tearDown(self):
        _discard_test_session()

    def test_records_what_was_said(self):
        engine._load_state(SESSION_ID)
        engine._remember_narration([{"character": "narrator", "text": "The water is rising."}], SESSION_ID)
        st = engine._load_state(SESSION_ID)
        self.assertEqual(st.get("narrator_recent"), ["The water is rising."])

    def test_accumulates_across_narrations(self):
        engine._remember_narration([{"text": "One."}], SESSION_ID)
        engine._remember_narration([{"text": "Two."}], SESSION_ID)
        self.assertEqual(engine._load_state(SESSION_ID).get("narrator_recent"), ["One.", "Two."])

    def test_is_capped(self):
        for i in range(engine.NARRATOR_MEMORY + 5):
            engine._remember_narration([{"text": f"line {i}."}], SESSION_ID)
        kept = engine._load_state(SESSION_ID).get("narrator_recent") or []
        self.assertEqual(len(kept), engine.NARRATOR_MEMORY)
        self.assertNotIn("line 0.", kept, "the oldest lines fall off the end")

    def test_empty_and_malformed_scripts_are_survivable(self):
        # Narration the player is waiting on must never fail because a
        # bookkeeping write did.
        for junk in ([], None, [{}], [{"text": "   "}]):
            engine._remember_narration(junk, SESSION_ID)
        self.assertFalse(engine._load_state(SESSION_ID).get("narrator_recent"))


class TestTheAuthoredDirection(unittest.TestCase):
    """The shipped `narrator_direction` is the actual brief the model reads, so
    the repetition complaint has to be answerable here or nowhere."""

    @classmethod
    def setUpClass(cls):
        cls.live = json.loads((ROOT / "prompts/simulation_prompts.json").read_text(encoding="utf-8"))
        cls.defaults = json.loads((ROOT / "prompts/simulation_prompts.defaults.json").read_text(encoding="utf-8"))

    def test_shipped_and_default_agree(self):
        self.assertEqual(self.live["narrator_direction"], self.defaults["narrator_direction"])

    def test_it_does_not_dictate_the_feeling(self):
        d = self.live["narrator_direction"].lower()
        self.assertNotIn("afraid, uneasy, but determined", d)

    def test_it_does_not_dictate_the_intent(self):
        # "Make it clear you have to find out what happened here" on every line
        # is why every line said it.
        self.assertNotIn("have to find out what happened here",
                         self.live["narrator_direction"].lower())

    def test_it_asks_the_line_to_be_specific_to_this_moment(self):
        self.assertIn("{scene}", self.live["narrator_direction"])
        self.assertIn("specific", self.live["narrator_direction"].lower())

    def test_it_lets_the_narrator_reach_into_the_lore(self):
        """`_ask(use_lore=True)` has always prepended the Experience bible to
        this call, but the direction forbade summarising the premise — so the
        one voice with the whole history in its context could never use any of
        it, and had nothing to say but a caption of the current frame."""
        d = self.live["narrator_direction"]
        self.assertIn("HISTORICAL BACKGROUND", d)
        self.assertIn("ONE buried piece", d)
        # Permission without a leash is an info-dump.
        self.assertIn("info-dump", d.lower())

    def test_the_line_is_history_rather_than_a_caption_of_the_frame(self):
        """The narrator is the only voice carrying the bible, and describing
        what is already on screen is the one thing the screen already does. So
        the line is a fact out of the past: the world gets deeper as the player
        goes further in, instead of being narrated back at them."""
        d = self.live["narrator_direction"].lower()
        self.assertIn("one line of history", d)
        self.assertIn("do not caption the frame", d)

    def test_it_asks_for_one_line(self):
        d = self.live["narrator_direction"].lower()
        self.assertIn("one short sentence", d)
        self.assertNotIn("two or three sentences", d)

    def test_it_carries_every_placeholder_the_engine_supplies(self):
        from prompts_store import PROMPT_SCHEMA
        field = next(f for f in PROMPT_SCHEMA if f["id"] == "narrator_direction")
        for var in field["format_vars"]:
            self.assertIn("{" + var + "}", self.live["narrator_direction"],
                          f"the shipped direction drops {{{var}}}")

    def test_the_declared_vars_match_what_the_engine_passes(self):
        from prompts_store import PROMPT_SCHEMA
        field = next(f for f in PROMPT_SCHEMA if f["id"] == "narrator_direction")
        # A var the engine does not pass raises KeyError at narration time and
        # silently drops the authored voice for the shipped one.
        self.assertEqual(
            set(field["format_vars"]),
            {"world", "self", "premise", "scene", "recent", "focus", "avoid"},
        )


class TestTheBoundWorldsCarryTheShippedVoice(unittest.TestCase):
    """Editing the prompt file alone does nothing, and does it silently.

    `prompts/simulation_prompts.json` is a derived scratch pad: a run binds to
    its start World and `worlds_store.load_world` writes that snapshot's
    prompts over the live file. Every saved World had its own frozen copy of
    the narrator direction, so rewriting the shipped one changed the editor and
    nothing a player ever heard — the next Play restored the old voice.
    """

    RETIRED = "speaking quietly to yourself"

    @classmethod
    def setUpClass(cls):
        cls.worlds = ROOT / "worlds"
        if not cls.worlds.is_dir():
            raise unittest.SkipTest("no saved worlds in this checkout")

    def test_no_saved_world_still_carries_the_retired_direction(self):
        stale = []
        for path in sorted(self.worlds.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            prompts = (data or {}).get("prompts")
            if not isinstance(prompts, dict):
                continue
            if self.RETIRED in (prompts.get("narrator_direction") or ""):
                stale.append(path.name)
        self.assertEqual(
            stale, [],
            "these Worlds will overwrite the shipped narrator on the next "
            "Play/reset: " + ", ".join(stale))


class TestColdOpenWaitsForTheOpening(unittest.TestCase):
    """The 4.2s timed cold open talked over the title card while the opening
    cinematic was still rendering, and its focus ordered every first line onto
    'I feel uneasy / I have to find out what happened'."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")

    def test_reset_arms_the_cold_open_instead_of_firing_it_on_a_timer(self):
        self.assertIn("Narrator.armColdOpen()", self.js)
        # The old boot fired a 4.2s timer regardless of the cinematic.
        self.assertNotIn("setTimeout(() => Narrator.coldOpen()", self.js)

    def test_cold_open_defers_while_the_cinematic_is_rendering(self):
        busy = self.js.split("function openingBusy()", 1)[1][:500]
        self.assertIn("Cutscene.isGenerating", busy)
        self.assertNotIn("Cutscene.isActive", busy)
        self.assertIn("OpeningFade.isHolding", busy)
        cold = self.js.split("function coldOpen()", 1)[1].split(
            "function onOpeningReady", 1)[0]
        self.assertIn("openingBusy()", cold)

    def test_the_montage_starting_releases_the_cold_open(self):
        """Voice-over: the narrator starts when shots exist and playback
        begins, not when the montage is over."""
        enter = self.js.split("async function enterFromServer", 1)[1]
        enter = enter.split("async function play(opts)", 1)[0]
        after_play = enter.split("playing = true", 1)[1]
        self.assertIn("Narrator.onOpeningReady()",
                      after_play.split("return true", 1)[0])

    def test_cold_open_does_not_dictate_unease_or_the_mission(self):
        self.assertNotIn(
            "Say how uneasy you feel and that you need to find out what happened",
            self.js)
        self.assertNotIn("You have just woken up here", self.js)
        cold = self.js.split("function coldOpen()", 1)[1].split(
            "function onOpeningReady", 1)[0]
        self.assertIn("opening cinematic", cold)
        self.assertNotIn("uneasy", cold)
        self.assertIn("Do not state the mission", cold)

    def test_the_cold_open_is_the_beat_allowed_to_carry_backstory(self):
        """Everywhere else the narrator is pinned to the frame in front of it.
        Over the opening montage that reads as a caption, so this is the one
        focus that sends it into the history."""
        cold = self.js.split("function coldOpen()", 1)[1].split(
            "function onOpeningReady", 1)[0]
        self.assertIn("history of this place", cold)
        self.assertIn("how things came to be this way", cold)


if __name__ == "__main__":
    unittest.main(verbosity=2)

class TestTheNarratorSpeaksWhenItShould(unittest.TestCase):
    """Reported as "it plays on the opening, twice after my first choice, then
    never again". Three separate causes, all of them here."""

    @classmethod
    def setUpClass(cls):
        cls.js = (Path(__file__).resolve().parent / "static" / "js"
                  / "standalone.js").read_text(encoding="utf-8")

    def test_every_committed_choice_gets_a_line(self):
        """"Never again" was literal: the only triggers were the cold open and
        MOVE TO, so an ordinary choice never narrated at all."""
        self.assertIn("function onCommit(choiceText)", self.js)
        self.assertIn("Narrator.onCommit(choiceText)", self.js)
        commit = self.js.split("function onCommit(choiceText) {", 1)[1].split(
            "\n    }", 1)[0]
        for guard in ("state.gameOver", "Talk.isOpen()", "state.audioUnlocked",
                      "openingBusy()", "busy || playing"):
            self.assertIn(guard, commit, f"onCommit must decline on {guard}")

    def test_a_move_does_not_get_two_narrations_at_once(self):
        """MOVE TO already fires transition(), which is the deliberate TWO
        lines the player heard as "twice". Firing onCommit as well would hit
        narrate()'s busy check and cancel the transition instead of adding
        to it."""
        self.assertIn("if (!moveTarget) {", self.js)

    def test_the_caption_waits_for_the_voice(self):
        """show() ran before the SDK loaded and the websocket opened, so the
        line was on screen seconds before it was spoken."""
        seg = self.js.split("function speakSegment(seg, myGen) {", 1)[1].split(
            "\n    }", 1)[0]
        self.assertIn("const reveal = () => show(seg.character, seg.text);", seg)
        self.assertIn('if (md === "speaking") { spoke = true; reveal(); }', seg)
        self.assertNotIn("\n        show(seg.character, seg.text);", seg)
        # a connection that never speaks must still subtitle rather than vanish
        self.assertIn("if (!spoke && !done && myGen === gen) reveal();", seg)

    def test_the_bar_is_positioned_by_the_stylesheet(self):
        """An inline bottom offset pushed the caption up by the action wheel's
        height, back into the choice stack's band - the exact collision the
        stylesheet had moved it to the bottom edge to avoid."""
        self.assertNotIn('el.narratorBar.style.bottom', self.js)
        css = (Path(__file__).resolve().parent / "static" / "css"
               / "standalone.css").read_text(encoding="utf-8")
        line = css.split("#narrator-line {", 1)[1].split("}", 1)[0]
        size = float(line.split("font-size:", 1)[1].split("px", 1)[0].strip())
        self.assertLessEqual(size, 10.0, "the caption is a subtitle, not a choice")
