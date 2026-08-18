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

    def test_injuries_are_carried(self):
        self.seed(current_observed_vision=ON_SCREEN, injuries=["gashed left hand"])
        self.assertIn("gashed left hand", self.narrate())


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
