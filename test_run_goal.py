"""The run's goal has to reach every system that writes the run.

`_goal_for_this_run` staged a goal at every reset and wrote it to
`state["level_goal"]`, and then nothing read it: it reached the montage's
title card and, one narration in six, the narrator — never the consequence
model, the choice slate, the objectives HUD or an encounter brief. A run had
a destination for exactly as long as the opening cinematic was on screen,
which is what "the goal system is completely non functional" meant. A
twelve-turn trace of the real app confirmed it: the door the montage was
staged toward appeared only as recurring scenery, the HUD lead never named
it, and the player dove through it on turn 12 without the run noticing.

These pin the wiring, surface by surface, so the next prompt refactor cannot
quietly cut one of them off again. Pure functions plus one temp session —
no network, no key.

Run with:
    python -m unittest test_run_goal -v
"""

import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")

import encounter
import engine

ROOT = Path(__file__).resolve().parent
GOAL = "The reinforced blast door at the end of the hall, marked by a green sigil."


class TestTheRunKnowsItsGoal(unittest.TestCase):

    def test_the_goal_is_read_off_the_run_not_the_sheet(self):
        # Both reset paths write level_goal now, so the run's copy is the
        # only source: a bare state gets no goal line, which keeps every
        # prompt surface deterministic for tests that build one.
        self.assertEqual(engine.run_goal({"level_goal": GOAL}), GOAL)
        self.assertEqual(engine.run_goal({}), "")
        self.assertEqual(engine.run_goal(None), "")

    def test_the_directive_names_the_goal_and_refuses_to_hand_it_over(self):
        line = engine.goal_directive({"level_goal": GOAL})
        self.assertIn(GOAL, line)
        self.assertIn("must not hand it over", line)
        self.assertTrue(line.endswith("\n"), "drops into the grounding block like the others")
        self.assertEqual(engine.goal_directive({}), "")

    def test_the_cached_frame_reset_path_stages_a_goal_too(self):
        # Only the montage path called _goal_for_this_run. With images off (or
        # a graph cutscene opening) the run had no goal at all.
        src = (ROOT / "engine.py").read_text(encoding="utf-8")
        body = src.split("def _perform_game_reset", 1)[1].split("def api_reset", 1)[0]
        after = body.split("if _open_on_montage(new_state):", 1)[1]
        montage, cached = after.split("else:", 1)
        self.assertIn("_stage_opening_montage", montage)
        self.assertIn("_goal_for_this_run(new_state", cached)


class TestEverySurfaceReadsIt(unittest.TestCase):
    """One assertion per consumer. Source-level where the prompt is assembled
    inside a function that needs the network; functional where it is not."""

    @classmethod
    def setUpClass(cls):
        cls.engine_src = (ROOT / "engine.py").read_text(encoding="utf-8")
        cls.encounter_src = (ROOT / "encounter.py").read_text(encoding="utf-8")
        cls.cutscene_src = (ROOT / "cutscene.py").read_text(encoding="utf-8")

    def test_the_consequence_prompt(self):
        body = self.engine_src.split("def _generate_combined_dispatches", 1)[1]
        grounding = body.split("grounding_block = (", 1)[1][:1400]
        self.assertIn("goal_directive(state)", grounding)
        # Between the phase and what SCAN saw — after the rules, before the
        # frame — so the model reads it as part of the situation.
        self.assertLess(grounding.index("STORY PHASE"), grounding.index("goal_directive(state)"))
        self.assertLess(grounding.index("goal_directive(state)"), grounding.index("onscreen_directive"))

    def test_the_choice_slate(self):
        # beat_nudge_text is the one line all eight slate generators read.
        with mock.patch.object(engine, "_threat_block", return_value={}):
            plain = engine.beat_nudge_text({"threat_level": 0})
            with_goal = engine.beat_nudge_text({"threat_level": 0, "level_goal": GOAL})
        self.assertTrue(with_goal.startswith(plain), "the authored beat still leads")
        self.assertIn(GOAL, with_goal)
        self.assertIn("one option should", with_goal)
        self.assertNotIn("GOAL:", plain, "no goal, no goal line")

    def test_the_objectives_lead(self):
        body = self.engine_src.split("def generate_directive", 1)[1].split("def ", 1)[0]
        self.assertIn("run_goal(st)", body)
        self.assertIn("THE RUN'S DESTINATION", body)
        self.assertIn("{goal_line}", body)

    def test_the_narrator(self):
        self.assertEqual(engine._narrator_goal({"level_goal": GOAL}), GOAL)
        body = self.engine_src.split("def _narrator_script", 1)[1]
        self.assertIn("_narrator_goal(st)", body)

    def test_the_encounter_brief(self):
        body = self.encounter_src.split("def encounter_lore_context", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("engine.run_goal(st)", body)
        with mock.patch.object(engine, "_load_state", return_value={"level_goal": GOAL}), \
             mock.patch.object(engine, "_load_history", return_value=[]):
            text = encounter.encounter_lore_context("goal_test_session")
        self.assertIn("WHAT THE PLAYER IS TRYING TO REACH: " + GOAL, text)

    def test_the_montage_photographs_the_goal_it_announced(self):
        # play_for_session used to re-derive the goal from the sheet, so on an
        # unauthored level the title card named the drafted landmark while the
        # shotlist was composed toward the sheet's first-landmark fallback.
        body = self.cutscene_src.split("def play_for_session", 1)[1]
        self.assertIn('goal = str(staged.get("goal") or "").strip()', body)


class TestReachingItIsRecorded(unittest.TestCase):
    """The consequence model answers `goal_reached` the way it answers
    `player_alive`; the engine counts the first true once and every surface
    then talks about what comes after instead of re-offering the door."""

    def test_the_schema_asks_and_the_contract_names_it(self):
        import json
        self.assertIn("goal_reached", engine._CONSEQUENCE_RESPONSE_SCHEMA["properties"])
        self.assertNotIn("goal_reached", engine._CONSEQUENCE_RESPONSE_SCHEMA["required"],
                         "a World carrying an older prompt copy never mentions it")
        for name in ("simulation_prompts.defaults.json", "simulation_prompts.json"):
            with self.subTest(file=name):
                doc = json.loads((ROOT / "prompts" / name).read_text(encoding="utf-8"))
                text = doc["action_consequence_instructions"]
                self.assertIn("EXACTLY these five fields", text)
                self.assertIn('"goal_reached"', text)
        src = (ROOT / "engine.py").read_text(encoding="utf-8")
        self.assertIn("Return JSON with all five fields", src)
        self.assertIn('state["_turn_goal_reached"] = data.get("goal_reached") is True', src)

    def test_the_turn_loop_files_it_as_a_completed_objective(self):
        src = (ROOT / "engine.py").read_text(encoding="utf-8")
        loop = src.split("def _process_turn_background(", 1)[1]
        self.assertIn('p1.get("goal_reached")', loop)
        self.assertIn('type="objective_done"', loop)
        fast = src.split("def advance_turn_image_fast(", 1)[1]
        self.assertIn('state["goal_reached_turn"] = int(state.get("turn_count") or 0) + 1', fast)
        self.assertIn('not state.get("goal_reached_turn")', fast, "counted once")

    def test_every_surface_changes_register_once_it_is_reached(self):
        st = {"level_goal": GOAL, "goal_reached_turn": 7, "threat_level": 0}
        self.assertIn("REACHED on turn 7", engine.goal_directive(st))
        self.assertIn("Do not offer the goal again", engine.goal_directive(st))
        with mock.patch.object(engine, "_threat_block", return_value={}):
            self.assertIn("GOAL REACHED", engine.beat_nudge_text(st))
            self.assertNotIn("one option should move toward it", engine.beat_nudge_text(st))
        self.assertIn("you are there now", engine._narrator_goal(st))
        src = (ROOT / "engine.py").read_text(encoding="utf-8")
        lead = src.split("def generate_directive", 1)[1].split("def ", 1)[0]
        self.assertIn("REACHED. The lead is", lead)
        api_src = (ROOT / "api.py").read_text(encoding="utf-8")
        self.assertIn('int(s.get("goal_reached_turn") or 0)', api_src,
                      "the sticky lead cache has to turn over the moment the goal is reached")


class TestAFightRollsWithTheTurnsLuck(unittest.TestCase):
    """encounter.api_resolve rolls every exchange against state["fate"], and
    nothing ever wrote that key — so every fight in the game's history rolled
    NORMAL and the LUCKY/UNLUCKY lane odds were dead weight."""

    SID = "goaltest_fate_session"

    def setUp(self):
        self.addCleanup(shutil.rmtree, engine._get_session_root(self.SID), True)
        state = engine._load_state(self.SID)
        state.update({"threat_level": 0, "current_phase": "normal", "chaos_level": 0})
        engine._save_state(state, self.SID)
        marks = mock.patch.object(
            engine, "_threat_marks",
            return_value=(engine.STORY_ESCALATE_AT, engine.STORY_CRITICAL_AT))
        marks.start()
        self.addCleanup(marks.stop)

    def test_the_fate_the_turn_rolled_is_the_fate_on_disk(self):
        dyn = engine.advance_story_dynamics(session_id=self.SID)
        self.assertIn(dyn["fate"], ("LUCKY", "NORMAL", "UNLUCKY"))
        self.assertEqual(engine._load_state(self.SID).get("fate"), dyn["fate"])

    def test_the_resolver_reads_that_key(self):
        src = (ROOT / "encounter.py").read_text(encoding="utf-8")
        body = src.split("def api_resolve", 1)[1]
        self.assertIn('st.get("fate")', body)


class TestTheContractCannotLoseRelocated(unittest.TestCase):
    """The OUTPUT CONTRACT at the top of the consequence prompt demanded four
    fields; the OUTPUT FORMAT example lower down showed three, and the schema
    let `relocated` go missing. A model that followed the nearer example
    handed back None, and the renderer fell back to the wording classifier
    the field exists to replace."""

    def test_the_schema_requires_it(self):
        self.assertIn("relocated", engine._CONSEQUENCE_RESPONSE_SCHEMA["required"])

    def test_the_example_shows_it_and_it_must_match_the_camera(self):
        import json
        for name in ("simulation_prompts.defaults.json", "simulation_prompts.json"):
            with self.subTest(file=name):
                doc = json.loads((ROOT / "prompts" / name).read_text(encoding="utf-8"))
                text = doc["action_consequence_instructions"]
                fmt = text.split("OUTPUT FORMAT:", 1)[1].split("}", 1)[0]
                self.assertIn('"relocated"', fmt)
                self.assertIn("must agree with `visual_scene`", text)


if __name__ == "__main__":
    unittest.main()
