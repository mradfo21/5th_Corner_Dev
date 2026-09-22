"""The run's goal is one thing you can see (goal.py).

Pins the parts that are pure: the record on the run, the steering line, the
establishing idle pointing at the goal, the route, and the client wiring.
The model calls themselves were proven on the real providers before this was
wired in (four runs, three Worlds: the goal was drawn into the first frame and
found in 4/4, and correctly missing once the player stepped indoors).

    python -m unittest test_goal_sight -v
"""
import os
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")

import goal
import engine

ROOT = Path(__file__).resolve().parent


class TheRecord(unittest.TestCase):

    def test_a_generated_goal_reads_as_a_name_and_a_look(self):
        st = {}
        line = goal.install(st, {"name": "Horizon Plant", "why": "It started there.",
                                 "look": "a rusted refinery under the mesa"})
        self.assertEqual(line, "Horizon Plant — a rusted refinery under the mesa")
        self.assertEqual(st["level_goal"], line)
        self.assertEqual(goal.record(st), {"name": "Horizon Plant",
                                           "why": "It started there.",
                                           "look": "a rusted refinery under the mesa"})

    def test_an_authored_line_is_kept_verbatim(self):
        st = {}
        goal.install(st, {"name": "Blast Door", "look": "a steel door"},
                     line="The reinforced blast door at the end of the hall.")
        self.assertEqual(st["level_goal"], "The reinforced blast door at the end of the hall.")
        self.assertEqual(st["goal_name"], "Blast Door")

    def test_a_label_is_label_length(self):
        self.assertEqual(goal.name_from("The red pump house, past the tanks."), "The red pump house")
        self.assertLessEqual(len(goal.name_from("x " * 80)), goal.NAME_MAX)

    def test_the_names_travel_with_the_world(self):
        # A stitch into another World must not carry this one's goal along.
        for k in ("goal_name", "goal_why", "goal_look", "level_goal"):
            self.assertIn(k, engine._WORLD_SCOPED_KEYS)


class Steering(unittest.TestCase):

    def setUp(self):
        goal._SEEN.clear()
        self.st = {"goal_name": "Horizon Plant", "goal_look": "a refinery",
                   "level_goal": "Horizon Plant — a refinery", "turn_count": 1}

    def test_in_sight_it_asks_to_keep_it_in_view(self):
        goal.note(self.st, True)
        line = goal.sight_directive(self.st)
        self.assertIn("KEEP IT IN VIEW", line)
        self.assertIn("Horizon Plant", line)

    def test_out_of_sight_for_two_turns_it_must_come_back(self):
        goal.note(self.st, True)                      # seen on turn 1
        self.st["turn_count"] = 3
        goal.note(self.st, False)
        line = goal.sight_directive(self.st)
        self.assertIn("OUT OF SIGHT FOR 2 TURNS", line)
        self.assertIn("MUST show", line)
        self.assertIn("Do not move the player", line)

    def test_never_seen_counts_from_the_start(self):
        self.st["turn_count"] = 2
        self.assertIn("OUT OF SIGHT", goal.sight_directive(self.st))

    def test_reached_or_goalless_says_nothing(self):
        self.assertEqual(goal.sight_directive({}), "")
        self.assertEqual(goal.sight_directive(dict(self.st, goal_reached_turn=4)), "")

    def test_the_consequence_directive_carries_it(self):
        line = engine.goal_directive(self.st)
        self.assertIn("WHAT THE PLAYER CAME HERE FOR", line)
        self.assertIn("visual_scene", line)

    def test_a_bare_state_still_gets_no_goal_line(self):
        self.assertEqual(engine.goal_directive({}), "")


class TheFirstFrameIsAView(unittest.TestCase):

    def test_the_idle_looks_at_the_goal_not_at_nothing(self):
        for single in (False, True):
            text = engine._flipbook_establishing_block(single=single,
                                                       goal="Horizon Plant: a refinery")
            self.assertIn("WHAT THEY CAME HERE FOR: Horizon Plant: a refinery", text)
            self.assertNotIn("something is coming", text)
            self.assertIn("never hiding it", text)

    def test_without_a_goal_the_idle_is_unchanged(self):
        self.assertEqual(engine._flipbook_establishing_block(single=True),
                         engine._flipbook_establishing_block_base(single=True))

    def test_both_idle_renders_pass_the_goal(self):
        src = (ROOT / "engine.py").read_text(encoding="utf-8")
        self.assertIn("_flipbook_establishing_block(frames, goal=_goal_mod.look_line(st))", src)
        self.assertIn("_flipbook_establishing_block(single=True, goal=_goal_mod.look_line(st))", src)


class Sight(unittest.TestCase):

    def test_no_key_or_no_file_is_a_miss_not_an_error(self):
        self.assertEqual(goal.locate("", {"name": "X"}), {"found": False, "box": None})
        self.assertEqual(goal.locate(__file__, {"name": ""}), {"found": False, "box": None})

    def test_the_box_is_normalised_from_gemini_order(self):
        class R:
            def raise_for_status(self): pass
            def json(self):
                return {"candidates": [{"content": {"parts": [{"text":
                        '{"found": true, "box_2d": [100, 200, 500, 800]}'}]}}]}
        with mock.patch.object(goal, "_gemini_key", return_value="k"), \
             mock.patch("ai_provider_manager.is_mock_active", return_value=False), \
             mock.patch("requests.post", return_value=R()):
            got = goal.locate(__file__, {"name": "Horizon Plant", "look": "a refinery"})
        self.assertTrue(got["found"])
        self.assertAlmostEqual(got["box"]["x"], 0.2)
        self.assertAlmostEqual(got["box"]["y"], 0.1)
        self.assertAlmostEqual(got["box"]["w"], 0.6)
        self.assertAlmostEqual(got["box"]["h"], 0.4)

    def test_the_route_is_mounted(self):
        # Read, not imported: importing api flips process-wide render state
        # (flipbook mode) and the plate tests that run after this one see it.
        src = (ROOT / "api.py").read_text(encoding="utf-8")
        self.assertIn("app.add_url_rule('/api/goal/sight'", src)
        self.assertIn("_goal_mod.api_goal_sight", src)


class TheClient(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")
        cls.html = (ROOT / "templates/standalone.html").read_text(encoding="utf-8")
        cls.css = (ROOT / "static/css/standalone.css").read_text(encoding="utf-8")

    def test_every_painted_scene_and_every_finished_beat_asks(self):
        self.assertIn("try { GoalTag.onScene(); } catch (_) {}", self.js)
        self.assertEqual(self.js.count("GoalTag.onScene()"), 2)

    def test_a_held_beat_counts_as_settled(self):
        # playing() stays true while a beat holds its last frame; the tag
        # waited on it forever in the first playtest.
        self.assertIn("atRest: () => !timer || idx >= frames.length - 1", self.js)
        self.assertIn("sceneSequence.atRest", self.js)

    def test_the_hud_and_the_tag_exist(self):
        for needle in ('id="goal-hud"', 'id="goal-tag"', 'class="gt-name"'):
            self.assertIn(needle, self.html)
        self.assertIn("#goal-tag", self.css)
        self.assertIn("body.has-goal #shot-tally", self.css)


class ARunFromBeforeTheGoal(unittest.TestCase):
    """Played in the app: a run begun before the merge had level_goal and no
    name, and nothing was ever tagged or named."""

    def setUp(self):
        goal._ADOPTED.clear()
        self.st = {"level_goal": "During a massive protest, the president has been kidnapped.",
                   "turn_count": 12}

    def test_it_gets_a_record_from_its_own_line(self):
        with mock.patch.object(goal, "invent", return_value={
                "name": "Administration Hub", "why": "They took him there.",
                "look": "a glass tower over the square"}):
            rec = goal.adopt(self.st)
        self.assertEqual(rec["name"], "Administration Hub")
        self.assertEqual(goal.record(self.st)["name"], "Administration Hub")
        self.assertIn("Administration Hub", goal.sight_directive(self.st))
        self.assertNotIn("goal_name", self.st)   # state.json is the turn loop's

    def test_with_no_model_the_first_clause_is_the_name(self):
        with mock.patch.object(goal, "invent", return_value={}):
            self.assertEqual(goal.adopt(self.st)["name"], "During a massive protest")

    def test_a_run_with_a_record_is_left_alone(self):
        st = dict(self.st, goal_name="Blast Door")
        with mock.patch.object(goal, "invent") as inv:
            self.assertEqual(goal.adopt(st)["name"], "Blast Door")
        inv.assert_not_called()


class YouCanWalkThereAndArrive(unittest.TestCase):
    """Played: the tag could not be clicked, and nothing ever completed."""

    def setUp(self):
        goal._APPROACH.clear(); goal._ADOPTED.clear(); goal._SEEN.clear()
        self.st = {"goal_name": "Horizon Plant", "goal_look": "a refinery",
                   "level_goal": "Horizon Plant — a refinery", "turn_count": 3}

    def test_each_click_is_a_step_and_the_beat_hears_it(self):
        self.assertEqual(goal.approach(self.st)["steps"], 1)
        self.assertEqual(goal.approach(self.st)["steps"], 1)     # same turn, one step
        self.assertIn("HEADING FOR HORIZON PLANT (step 1 of", goal.sight_directive(self.st))

    def test_the_last_step_is_the_arrival_beat_then_it_is_reached(self):
        for t in range(goal.APPROACH_STEPS):
            self.st["turn_count"] = 3 + t
            goal.approach(self.st)
        self.assertIn("THE PLAYER ARRIVES THIS BEAT", goal.sight_directive(self.st))
        self.assertIn("goal_reached is TRUE", goal.sight_directive(self.st))
        self.assertFalse(goal.arrived(self.st))
        self.st["turn_count"] += 1                                # the beat resolved
        self.assertTrue(goal.arrived(self.st))
        self.assertEqual(goal.sight_directive(self.st), "")

    def test_reach_on_a_close_goal_is_the_arrival(self):
        goal.approach(self.st, final=True)
        self.assertIn("THE PLAYER ARRIVES THIS BEAT", goal.sight_directive(self.st))
        self.st["turn_count"] += 1
        self.assertTrue(goal.arrived(self.st))

    def test_the_model_saying_so_still_counts(self):
        self.assertTrue(goal.arrived(dict(self.st, goal_reached_turn=2)))


class TheClientCanGo(unittest.TestCase):

    def test_the_tag_and_the_hud_name_walk_you_there(self):
        js = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")
        self.assertIn('postJSON("/api/goal/sight", { approach: true, final })', js)
        self.assertIn('makeChoice(final ? reachFor(r.name) : actionFor(r.name), null, { source: "typed" })', js)
        self.assertIn('card.id = "goal-begin"', js)
        # Arriving does not pop a card: the goal glows and the click finishes.
        self.assertIn('a.id = "goal-arrive"', js)
        self.assertIn('postJSON("/api/goal/sight", { complete: true })', js)
        css = (ROOT / "static/css/standalone.css").read_text(encoding="utf-8")
        self.assertIn("#goal-tag.on .gt-name, #goal-tag.on .gt-kind, #goal-tag.on .gt-hit { pointer-events: auto", css)


if __name__ == "__main__":
    unittest.main()
