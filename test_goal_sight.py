"""The run's goal is one thing you can see (goal.py).

Pins the parts that are pure: the record on the run, the steering line, the
establishing idle pointing at the goal, the route, and the client wiring.
The model calls themselves were proven on the real providers before this was
wired in (four runs, three Worlds: the goal was drawn into the first frame and
found in 4/4, and correctly missing once the player stepped indoors).

    python -m unittest test_goal_sight -v
"""
import json
import os
import sys
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
                                           "look": "a rusted refinery under the mesa",
                                           # A draft with no spine leaves the
                                           # spine empty rather than absent.
                                           "truth": "", "claim": "", "cost": ""})

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
        for k in ("goal_name", "goal_why", "goal_look", "level_goal",
                  # ...and so do its secret, its claimant, what it costs, and
                  # the thing that is supposed to be in the room.
                  *goal.SPINE_KEYS, *goal.PRIZE_KEYS, "goal_phase"):
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
        # Walking in is not finishing it: the reward cutscene is
        # (goal.finish_reward), so the beat must not claim the goal.
        self.assertIn("goal_reached is FALSE", goal.sight_directive(self.st))
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


class NoMergeLeftovers(unittest.TestCase):
    """Played: a stray `=======` from a merge sat just above #goal-hud in the
    stylesheet, which threw the rule away, so the top-left GOAL lay flat along
    the top edge of the screen instead of standing in its corner."""

    def test_the_client_files_carry_no_conflict_markers(self):
        import re
        for rel in ("static/css/standalone.css", "static/js/standalone.js",
                    "templates/standalone.html"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            hits = re.findall(r"(?m)^(<<<<<<< |=======\r?$|>>>>>>> )", text)
            self.assertEqual(hits, [], rel)

class TheBoss(unittest.TestCase):
    """Played: reaching the goal was an overlay and then nothing. The goal now
    comes with the one who holds it; ENTER opens the fight with them, and only
    beating them completes the goal."""

    def setUp(self):
        for d in (goal._APPROACH, goal._ADOPTED, goal._SEEN, goal._BOSSES, goal._WON):
            d.clear()
        goal._DONE.clear(); goal._PAID.clear()
        self.st = {"goal_name": "Administration Spire", "goal_look": "a black tower",
                   "level_goal": "Administration Spire — a black tower", "turn_count": 4}

    def test_invent_brings_a_boss_and_install_keeps_it(self):
        st = {}
        goal.install(st, {"name": "Horizon Plant", "look": "a refinery", "why": "w",
                          "boss": {"name": "The Plant Warden", "kind": "person",
                                   "look": "a gaunt man in a gas mask", "want": "Nobody leaves."}})
        self.assertEqual(goal.boss(st)["name"], "The Plant Warden")
        for k in goal.BOSS_KEYS:
            self.assertIn(k, engine._WORLD_SCOPED_KEYS)

    def test_a_run_without_one_gets_one_drafted_once(self):
        with mock.patch("ai_provider_manager.chat", return_value=json.dumps(
                {"name": "Minister of Order", "kind": "person", "look": "grey suit", "want": "Silence."})) as chat:
            self.assertEqual(goal.boss(self.st, make=True)["name"], "Minister of Order")
            self.assertEqual(goal.boss(self.st, make=True)["name"], "Minister of Order")
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(goal.boss_subject(self.st)["source"], "boss")

    def test_the_arrival_shows_the_boss_and_does_not_finish_the_goal(self):
        goal.install_boss(self.st, {"name": "Minister of Order", "kind": "person", "look": "grey suit"})
        for t in range(goal.APPROACH_STEPS):
            self.st["turn_count"] = 4 + t
            goal.approach(self.st)
        line = goal.sight_directive(self.st)
        self.assertIn("Minister of Order", line)
        self.assertIn("goal_reached is FALSE", line)

    def test_beating_the_boss_opens_the_way_in(self):
        goal._DONE.discard(self.st["level_goal"])
        goal.boss_defeated(self.st, "Minister of Order", "down")
        # the door is reached...
        self.assertEqual(self.st["goal_reached_turn"], 5)
        self.assertTrue(goal.arrived(self.st))
        self.assertEqual(self.st["goal_boss_defeated"], "down")
        # ...and the goal is NOT done: a completed goal never lights its door,
        # and the thing in the room is what finishes it.
        self.assertNotIn(self.st["level_goal"], goal._DONE)
        src = (ROOT / "goal.py").read_text(encoding="utf-8")
        self.assertIn("            done = line_key in _DONE\n", src)

    def test_the_fight_is_the_climax_and_the_cutscene_is_its_payoff(self):
        """Played 2026-09-22: "the idea of clicking on a goal and it triggering
        an encounter is incorrect … if we reach the goal, we need a reward".
        The boss stays in the engine; ENTER no longer opens it."""
        js = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")
        # Clicking the goal pays out; it does not start a fight.
        self.assertNotIn('Encounter.start({ subject: { label: r.boss.name', js)
        self.assertIn('Cutscene.play({ mood: "reward", name: goalName, graph: true })', js)


class TheRewardForReachingIt(unittest.TestCase):
    """ENTER pays off with an in-game cutscene, and puts the player down on the
    other side of the goal — not a card, and not a fight."""

    def setUp(self):
        goal._DONE.clear(); goal._APPROACH.clear()
        self.st = {"goal_name": "Sigil Door", "goal_look": "a steel door",
                   "goal_why": "What was sealed in there is still in there.",
                   "level_goal": "Sigil Door — a steel door", "turn_count": 6,
                   "world_prompt": "A drowned service corridor."}

    def test_the_shots_are_of_this_goal(self):
        brief = goal.reward_brief(self.st)
        self.assertIn("THE PLACE IS: Sigil Door", brief)
        self.assertIn("a steel door", brief)
        self.assertIn("this one is the frame the game carries on from", brief)
        self.assertEqual(goal.reward_request(self.st)["mood"], "reward")

    def test_the_mood_exists_and_ends_on_a_playable_frame(self):
        import cutscene
        self.assertIn("reward", cutscene.MOODS)
        shots = cutscene.MOODS["reward"]["shots"]
        self.assertEqual(len(shots), 4)
        self.assertIn("THE PAYOFF FRAME", shots[3][2])   # the thing itself, playable
        # The game's own model AND size (None, None = the play settings), so
        # the goal, its cutscene and the scene after are one render in one
        # light. On the pro model at 2K it "looked VERY different, like they
        # had new lighting" (2026-09-23). The play model 400s only at 2K.
        model, size = cutscene.render_settings("reward")
        self.assertIsNone(model)
        self.assertIsNone(size)
        self.assertGreater(cutscene.render_strength("reward"),
                           cutscene.render_strength("threshold"))
        self.assertEqual(cutscene.render_settings("approach")[0], cutscene.GRID_RENDER_MODEL)

    def test_finishing_it_puts_the_player_inside_and_marks_it_reached(self):
        rows = []
        pending = {"mood": "reward", "shots": [
            {"path": str(ROOT / "goal.py"), "url": "/images/a.png"},
            {"path": str(ROOT / "cutscene.py"), "url": "/images/last.png"},
        ]}
        with mock.patch.object(engine, "_load_history", return_value=[]), \
             mock.patch.object(engine, "_save_history", side_effect=lambda h, s: rows.extend(h)):
            out = goal.finish_reward(self.st, "sid", pending)
        self.assertEqual(out["image_url"], "/images/last.png")
        self.assertEqual(self.st["current_image_url"], "/images/last.png")
        self.assertIn("inside it now", self.st["world_prompt"])
        # Landing inside does NOT finish it any more: the thing they came for
        # is in the room and taking it is the payoff (goal.take_prize).
        self.assertEqual(goal.phase(self.st), "prize")
        self.assertEqual(goal.standing(self.st)["done"], 0)
    def test_the_engine_deposits_when_the_reward_ends(self):
        src = (ROOT / "engine.py").read_text(encoding="utf-8")
        self.assertIn('if str(_pending_reward.get("mood") or "") == "reward":', src)
        self.assertIn("_goal_mod.finish_reward(st, sid, _pending_reward)", src)
        cut = (ROOT / "cutscene.py").read_text(encoding="utf-8")
        self.assertIn('"graph": bool(node) or mood == "reward"', cut)


class TheRunIsAListOfGoals(unittest.TestCase):
    """"the actual game loop here is to simply find a certain # of goals.
    1/3 goals reached … victory is surviving to get to the 3 goals"."""

    def setUp(self):
        goal._DONE.clear(); goal._APPROACH.clear(); goal._BOSSES.clear()
        self.st = {"goal_name": "Sigil Door", "goal_look": "a steel door",
                   "level_goal": "Sigil Door — a steel door", "turn_count": 9,
                   "world_prompt": "A drowned corridor.", "goal_inside": "Sigil Door"}

    def test_the_board_starts_at_one_of_three(self):
        board = goal.standing({})
        self.assertEqual((board["done"], board["of"], board["index"]), (0, 3, 1))
        self.assertFalse(board["victory"])

    def test_reaching_one_hands_the_run_the_next(self):
        with mock.patch.object(goal, "invent", return_value={
                "name": "Pump House", "why": "The water goes somewhere.",
                "look": "a low brick shed", "boss": "", "boss_look": "", "boss_want": ""}) as inv:
            out = goal.advance(self.st, "sid")
        self.assertEqual(out["next"], "Pump House")
        self.assertEqual(self.st["goals_done"], ["Sigil Door"])
        self.assertEqual(goal.record(self.st)["name"], "Pump House")
        self.assertFalse(self.st.get("goal_reached_turn"))     # the new one is ahead of them
        self.assertEqual(goal.standing(self.st)["index"], 2)
        # ...and it is drafted from where they now stand, not from the street.
        self.assertEqual(inv.call_args.kwargs.get("from_inside"), "Sigil Door")

    def test_the_draft_is_told_not_to_name_the_place_they_are_in(self):
        import inspect
        # The brief the drafter is handed lives in _known now — invent() is
        # the draft/judge/redraft loop around it.
        src = inspect.getsource(goal._known)
        self.assertIn("THE PLAYER IS STANDING INSIDE", src)
        self.assertIn("The next goal is a DIFFERENT place", src)

    def test_the_third_one_wins_the_run(self):
        st = dict(self.st, goals_done=["One", "Two"])
        with mock.patch.object(goal, "invent") as inv:
            out = goal.advance(st, "sid")
        inv.assert_not_called()                                # nothing left to walk to
        self.assertTrue(out["victory"])
        self.assertTrue(st["run_victory"])
        self.assertEqual(goal.standing(st)["done"], 3)

    def test_taking_the_thing_moves_the_board_on(self):
        rows = []
        pending = {"mood": "reward", "shots": [{"path": str(ROOT / "goal.py"), "url": "/images/x.png"}]}
        with mock.patch.object(engine, "_load_history", return_value=[]), \
             mock.patch.object(engine, "_save_history", side_effect=lambda h, s: rows.extend(h)), \
             mock.patch.object(goal, "invent", return_value={"name": "Pump House", "why": "",
                                                            "look": "a shed"}):
            goal.finish_reward(self.st, "sid", pending)
            out = goal.take_prize(self.st, "sid")
        self.assertEqual(out["next"], "Pump House")
        self.assertEqual(out["board"]["done"], 1)

    def test_the_client_counts_them_and_ends_the_run(self):
        js = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")
        self.assertIn("`Goal ${Math.min(board.index, board.of)}/${board.of}`", js)
        self.assertIn("function showVictory(board)", js)
        # ...and the end of the run is the one card that HOLDS: there is
        # nothing behind it left to play (every other card lets go).
        self.assertIn('showReached(`${board.done} of ${board.of}`, line, "Run complete", true)', js)

    def test_reaching_it_pays_out_and_never_opens_a_fight(self):
        js = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")
        enter = js[js.index("async function complete()"):]
        enter = enter[:enter.index("function watchBossFight")]
        self.assertIn("await playReward(r.name)", enter)
        self.assertNotIn("Encounter.start", enter)
        # A boss who was staged and never stepped out is dropped here rather
        # than fought here.
        self.assertIn('pendingBoss = null; bossFor = "";', enter)
        self.assertIn('Cutscene.play({ mood: "reward", name: goalName, graph: true })', js)


class TheFightAtTheGoalSettles(unittest.TestCase):
    """Played 2026-09-22: "the boss fight felt incoherent, the boss wasn't
    defeated, there was no goal cutscene". A boss fight may not break off with
    him still on his feet, and he may not change identity between rounds."""

    def test_the_rule_is_in_the_rules(self):
        # He has no morale to break, so he never runs; nothing else ends a
        # fight with him on his feet (combat.foe_block / play_round).
        import combat
        boss = combat.foe_block("person", "opportunistic", boss=True)
        self.assertEqual(boss["morale"], 0)
        self.assertGreater(boss["max"], combat.foe_block("person")["max"])
        src = (ROOT / "encounter.py").read_text(encoding="utf-8")
        # ...and the fight keeps his name and his face.
        self.assertIn("_pin = _goal_pin.boss(engine._load_state(session_id) or {}) or {}", src)
        self.assertIn('char["label"] = _pin["name"]', src)

    def test_running_from_him_is_still_allowed(self):
        import random
        import encounter
        boss = {"boss": True, "character": {"label": "The Zealot", "stance": "hostile"}}
        ends = set()
        for seed in range(200):
            got = encounter.roll_exchange({}, boss, "Run for the door", "evade",
                                          rng=random.Random(seed))
            ends.add(got["rolled"]["end"])
        self.assertIn("escaped", ends)


class NamesAreNotSentences(unittest.TestCase):
    """Played 2026-09-23: a creature was named "The scene shows a third-person
    view" — the vision pass's opening words — on the fight's title and on the
    spoil it dropped."""

    def test_a_sentence_about_the_picture_is_not_a_name(self):
        import encounter
        for s in ("The scene shows a third-person view", "The image shows a figure",
                  "A third-person view", "A figure standing in the doorway of the old mill"):
            self.assertTrue(encounter._reads_as_description(s), s)
        for s in ("The Alpha Handler", "A man", "A scavenger", "The Chief Surgeon"):
            self.assertFalse(encounter._reads_as_description(s), s)

    def test_the_fight_is_named_for_what_it_is(self):
        import encounter
        self.assertEqual(encounter._grounded_label_from_look(
            "A mutated, gaunt humanoid fused with metallic debris",
            "The scene shows a third-person view of an armored figure", kind="creature"),
            "A humanoid")
        self.assertEqual(encounter._grounded_label_from_look(
            "", "The scene shows a third-person view of a corridor", kind="creature"),
            "A creature")

    def test_the_spoil_is_off_someone(self):
        import encounter
        self.assertEqual(encounter.foe_name("sid", {"character": {"kind": "creature"}},
                                            "The scene shows a third-person view"), "a creature")
        self.assertEqual(encounter.foe_name("sid", {}, "The image shows a man"), "a stranger")
        self.assertEqual(encounter.foe_name("sid", {}, ""), "")


class NothingCoversThePicture(unittest.TestCase):
    """The loop playtest got as far as standing in the room with the thing it
    came for and then could not touch it: the REACHED card was still up over
    everything ("<div id=\"goal-reached\"> intercepts pointer events"), which
    is what the player saw as "i think its stuck, unable to progress on the
    goal when it clicks"."""

    def setUp(self):
        self.js = (ROOT / "static" / "js" / "standalone.js").read_text(encoding="utf-8")

    def test_the_boss_going_down_no_longer_puts_a_card_up(self):
        # Beating him is not the goal being done any more — the reward plays
        # and taking the thing in the room is what ends it.
        self.assertNotIn("showPayoff(res);", self.js)
        self.assertIn("function showPayoff(res)", self.js)   # kept, uncalled

    def test_the_card_gets_out_of_the_way_once_the_prize_is_there(self):
        self.assertIn('if (res.phase === "prize" || res.found) dismissReached();', self.js)

    def test_every_card_but_the_end_of_the_run_lets_go(self):
        self.assertIn("function dismissReached()", self.js)
        self.assertIn("reachedTimer = setTimeout(dismissReached, 4200);", self.js)
        # The run ending is the one card with nothing behind it to play.
        self.assertIn('showReached(`${board.done} of ${board.of}`, line, "Run complete", true);',
                      self.js)
        self.assertIn("function showReached(name, line, kind, hold)", self.js)

    def test_taking_it_is_where_the_beat_goes(self):
        take = self.js.split("async function takeIt(r) {", 1)[1].split("\n    }", 1)[0]
        self.assertIn('"Goal complete"', take)
        self.assertIn("res.of_place", take)
        # ...and the server hands the client what that beat needs to say it.
        g = (ROOT / "goal.py").read_text(encoding="utf-8")
        self.assertIn('out = {"took": pz.get("name", ""), "of_place": rec.get("name", ""),', g)

    def test_a_goal_with_nothing_in_it_still_gets_something(self):
        # The room has to have the thing in it, or the tag points at the
        # building the player is standing in and the goal cannot be finished.
        st = {"goal_name": "Pump House", "goal_look": "a brick shed",
              "level_goal": "Pump House — a brick shed"}
        pz = goal.prize(st, make=True)
        self.assertTrue(pz.get("name"))
        self.assertTrue(pz.get("verb"))
        self.assertEqual(goal.target({**st, "goal_phase": "prize"})["name"], pz["name"])


class TheBossStandsInTheWayIn(unittest.TestCase):
    """"the boss fight felt incoherent". He is the thing between the player and
    the place — so he happens BEFORE the way in, never after it. Staged on the
    walk, he steps out on the walk; still waiting when they get to the door, he
    is standing in the door; and once the reward has put them in the room he has
    missed his turn entirely."""

    def setUp(self):
        self.js = (ROOT / "static" / "js" / "standalone.js").read_text(encoding="utf-8")

    def test_he_is_never_staged_into_a_room_already_entered(self):
        stage = self.js.split("function stageBoss(who) {", 1)[1].split("\n    }", 1)[0]
        self.assertIn("reachedShownFor === mine", stage)
        self.assertIn('result.phase === "prize"', stage)
        self.assertIn("pendingBoss = null", stage)

    def test_the_door_is_past_his_last_chance(self):
        # He is the thing on the WAY IN. Once the player is at the door, ENTER
        # pays out — a fight behind that click is the shape that was called
        # wrong, so a boss who never stepped out is simply dropped.
        comp = self.js.split("async function complete() {", 1)[1].split("\n    }", 1)[0]
        self.assertNotIn("Encounter.start", comp)
        self.assertIn('pendingBoss = null; bossFor = "";', comp)
        self.assertIn("if (await playReward(r.name)) return;", comp)

    def test_beating_him_is_what_plays_the_reward(self):
        watch = self.js.split("function watchBossFight(goalName) {", 1)[1].split("\n    }", 1)[0]
        self.assertIn("await playReward(goalName)", watch)
        self.assertIn("if (!sawFight) return;", watch)


class WinningItEndsIt(unittest.TestCase):
    """"victory is surviving to get to the 3 goals". A won run has no fourth
    place to walk to, so it stops handing them one — the board and the card are
    all that is left."""

    def test_a_won_run_is_answered_before_anything_else(self):
        src = (ROOT / "goal.py").read_text(encoding="utf-8")
        head = src.split("def api_goal_sight()", 1)[1]
        won = head.index('if st.get("run_victory"):')
        # ...before adopt, which would hand the won run its last goal back.
        self.assertLess(won, head.index("rec = adopt(st)"))
        self.assertIn('return jsonify({"ok": True, "goal": False, "victory": True,', src)

    def test_the_client_still_shows_the_card_with_no_goal_on_the_answer(self):
        js = (ROOT / "static" / "js" / "standalone.js").read_text(encoding="utf-8")
        self.assertIn("if (res && res.victory && res.board && victoryShownFor !== res.board.done) {",
                      js)
        self.assertNotIn("if (!res || !res.goal) { setHud(null); hide(); return; }", js)

    def test_three_taken_wins_it(self):
        st = {"goal_name": "Third Place", "level_goal": "Third Place",
              "goals_done": ["One", "Two"], "turn_count": 20}
        out = goal.advance(st, "sid")
        self.assertTrue(out.get("victory"))
        self.assertTrue(st.get("run_victory"))
        self.assertEqual(goal.standing(st)["done"], goal.GOALS_TO_WIN)
        self.assertTrue(goal.standing(st)["victory"])


class TheWalkIsWhatGetsYouThere(unittest.TestCase):
    """A playtest lap was handed a new goal and was standing inside it without
    one step, because the model read the beat where the player walked out of
    the last place — prize in hand — as an arrival at the next one. The
    player's own steps are the distance; the model only confirms the last one."""

    def test_the_engine_will_not_take_the_models_word_on_its_own(self):
        src = (ROOT / "engine.py").read_text(encoding="utf-8")
        self.assertIn("_near = int(_p.get(\"steps\") or 0) >= _goal_steps.APPROACH_STEPS - 1", src)
        self.assertIn("if goal_reached and _near and player_alive and run_goal(state)", src)

    def test_a_fresh_goal_is_not_already_reached(self):
        goal._APPROACH.clear()
        st = {"goal_name": "Pump House", "goal_look": "a brick shed",
              "level_goal": "Pump House — a brick shed", "turn_count": 9}
        self.assertFalse(goal.arrived(st))
        self.assertEqual(goal.progress(st)["steps"], 0)
        # ...and it takes the whole walk.
        for _ in range(goal.APPROACH_STEPS):
            st["turn_count"] += 1
            goal.approach(st)
        st["turn_count"] += 1
        self.assertTrue(goal.arrived(st))


# ─────────────────────────────────────────────────────────────────────────────
# GOALS WORTH WALKING TO  (see _claude_goal_design.md)
# ─────────────────────────────────────────────────────────────────────────────

_BOOK = {
    "status": "ready",
    "frames": [
        {"row": "SETS", "title": "DROWNED OFFICE",
         "subject": "a records room half under water",
         "story": "someone kept working after the water came"},
        {"row": "SETS", "title": "THE SPILLWAY", "subject": "a concrete chute",
         "story": "it runs dry now"},
        {"row": "CONFLICT", "title": "THE TALLY", "subject": "a man counting bodies",
         "story": "he will not stop for anyone"},
    ],
    "roster_looks": [{"kind": "tally clerk",
                      "look": "a thin man in a wet coat with a ledger"}],
    "look_rules": {"motifs": ["standing water", "ledger paper", "sodium light"]},
}


def _cand(name, why, truth, claim="The Tally Clerk wants the register back",
          cost="Two hours of dark and the last of the water"):
    return {"name": name, "why": why, "look": "a low shape under sodium light",
            "truth": truth, "claim": claim, "cost": cost,
            "prize": {"name": "Wet Ledger", "look": "a swollen book", "verb": "Lift"},
            "boss": {"name": "The Tally Clerk", "kind": "person",
                     "look": "a thin man in a wet coat", "want": "the register"}}


class TheDrafterCanSeeTheWorldItIsIn(unittest.TestCase):
    """Every run builds a nine-frame look book — row 3 is three places "dressed
    with narrative evidence so each tells a story by itself", row 2 is three
    conflicts at their most dangerous instant. The consequence model has read
    it since 2026-09-21. The GOAL, the one thing the whole run is pointed at,
    was the only system still drafted blind to it — which is why three runs in
    a row produced a Hub, a Spire and a Door with a key in each."""

    def test_the_brief_carries_the_sets_the_conflicts_and_the_cast(self):
        with mock.patch.dict("sys.modules"):
            lb = mock.MagicMock()
            lb.current.return_value = _BOOK
            sys.modules["look_book"] = lb
            brief = goal.world_brief("sid")
        self.assertIn("DROWNED OFFICE", brief)          # a place, dressed
        self.assertIn("THE SPILLWAY", brief)
        self.assertIn("THE TALLY", brief)               # a set piece
        self.assertIn("tally clerk", brief)             # who lives here
        self.assertIn("standing water", brief)          # the run's motifs
        self.assertIn("someone kept working after the water came", brief)

    def test_a_book_that_is_not_ready_is_simply_not_there(self):
        # A run must never wait on the look book, or block when it failed.
        with mock.patch.dict("sys.modules"):
            lb = mock.MagicMock()
            lb.current.return_value = {"status": "building"}
            sys.modules["look_book"] = lb
            self.assertEqual(goal.world_brief("sid"), "")
            lb.current.side_effect = RuntimeError("no book")
            self.assertEqual(goal.world_brief("sid"), "")


class AGoalHasASpine(unittest.TestCase):
    """name/why/look is a waypoint. What makes a place worth walking to is what
    the player does NOT know (truth), who else wants it (claim), and what
    getting there takes (cost)."""

    def test_the_three_keys_survive_install_and_come_back_on_record(self):
        st = {}
        goal.install(st, _cand("Drowned Records Room", "Someone kept writing.",
                               "The last entry is dated after everyone left."))
        self.assertEqual(st["goal_truth"], "The last entry is dated after everyone left.")
        self.assertTrue(st["goal_claim"])
        self.assertTrue(st["goal_cost"])
        rec = goal.record(st)
        self.assertEqual(rec["truth"], st["goal_truth"])
        self.assertEqual(rec["claim"], st["goal_claim"])
        self.assertEqual(rec["cost"], st["goal_cost"])
        self.assertEqual(goal.claim_of(st), st["goal_claim"])

    def test_the_arrival_is_where_the_truth_is_paid_off(self):
        st = {}
        goal.install(st, _cand("Drowned Records Room", "Someone kept writing.",
                               "The last entry is dated after everyone left."))
        brief = goal.reward_brief(st)
        self.assertIn("WHAT IS ACTUALLY TRUE OF THIS PLACE", brief)
        self.assertIn("The last entry is dated after everyone left.", brief)
        # ...and never as words on the screen.
        self.assertIn("never write it as words on screen", brief)

    def test_the_walk_spends_the_claim_and_the_cost(self):
        goal._APPROACH.clear()
        st = {"turn_count": 3}
        goal.install(st, _cand("Drowned Records Room", "Someone kept writing.",
                               "The last entry is dated after everyone left."))
        goal.approach(st)
        d = goal.sight_directive(st)
        self.assertIn("SOMEBODY ELSE IS MOVING ON IT TOO", d)
        self.assertIn("WHAT THIS IS COSTING THEM", d)
        # Evidence in the world, never an announcement.
        self.assertIn("never an announcement", d)

    def test_a_goal_that_is_taken_leaves_its_spine_behind(self):
        st = {"goals_done": [], "turn_count": 4}
        goal.install(st, _cand("Drowned Records Room", "a", "b"))
        with mock.patch.object(goal, "invent", return_value={}):
            goal.advance(st, "sid", took="Wet Ledger")
        for k in goal.SPINE_KEYS:
            self.assertNotIn(k, st, f"{k} carried over into the next goal")


class TheGoalIsJudgedBeforeItIsKept(unittest.TestCase):
    """One draft, first result, installed — with no bar and no second opinion.
    Now: three candidates, six written criteria, and one redraft when the
    winner falls short."""

    def _chat(self, drafts, verdict, seen=None):
        def chat(msgs, **kw):
            text = msgs[0]["content"]
            if seen is not None:
                seen.append(text)
            if text.startswith("You are the game's director"):
                return json.dumps(verdict)
            return json.dumps({"goals": drafts})
        return chat

    def _run(self, chat, **kw):
        with mock.patch.dict("sys.modules"):
            aim = mock.MagicMock(); aim.chat = chat
            sys.modules["ai_provider_manager"] = aim
            gi = mock.MagicMock()
            gi.authored_setting.return_value = {"name": "Ossuary Station",
                                                "summary": "a flooded relay"}
            gi.get_spec.return_value = {"player_character": {"role": "engineer"}}
            sys.modules["game_identity"] = gi
            lb = mock.MagicMock(); lb.current.return_value = _BOOK
            sys.modules["look_book"] = lb
            return goal.invent(lore="the water came in", world_prompt="knee deep", **kw)

    def test_it_keeps_the_one_the_judge_picks_not_the_first_one(self):
        drafts = [_cand("Cooling Spire", "It is tall.", "It is tall."),
                  _cand("Drowned Records Room", "Someone kept writing.",
                        "The last entry is dated after everyone left."),
                  _cand("Data Siphon Hub", "It hums.", "It hums.")]
        verdict = {"scores": [
            {"i": 0, "worth": 2, "only_here": 1, "conflict": 3, "mystery": 1, "payoff": 3, "fresh": 2},
            {"i": 1, "worth": 5, "only_here": 5, "conflict": 4, "mystery": 5, "payoff": 4, "fresh": 4},
            {"i": 2, "worth": 3, "only_here": 2, "conflict": 3, "mystery": 1, "payoff": 3, "fresh": 3}],
            "pick": 1, "why": "the only one with a body in it", "fix": ""}
        got = self._run(self._chat(drafts, verdict), session_id="sid")
        self.assertEqual(got["name"], "Drowned Records Room")
        self.assertNotEqual(got["truth"], got["why"])

    def test_the_drafter_is_handed_the_run_and_the_world(self):
        seen = []
        drafts = [_cand("Drowned Records Room", "a", "b")]
        verdict = {"scores": [{"i": 0, "worth": 5, "only_here": 5, "conflict": 5,
                               "mystery": 5, "payoff": 5, "fresh": 5}],
                   "pick": 0, "why": "", "fix": ""}
        self._run(self._chat(drafts, verdict, seen), session_id="sid",
                  phase="escalating", done=["Reinforced Blast Door"],
                  carried=["Neural Override Key"])
        draft = next(t for t in seen if t.startswith("You are choosing the GOAL"))
        for must in ("DROWNED OFFICE", "THE TALLY", "tally clerk", "standing water",
                     "ESCALATING LEG", "Reinforced Blast Door", "Neural Override Key"):
            self.assertIn(must, draft)

    def test_a_goal_under_the_bar_is_redrafted_once_with_the_note(self):
        seen = []
        drafts = [_cand("Cooling Spire", "It is tall.", "It is tall.")]
        verdict = {"scores": [{"i": 0, "worth": 1, "only_here": 1, "conflict": 1,
                               "mystery": 1, "payoff": 1, "fresh": 1}],
                   "pick": 0, "why": "weak", "fix": "give it a body"}
        self._run(self._chat(drafts, verdict, seen), session_id="sid")
        asked = [t for t in seen if t.startswith("You are choosing the GOAL")]
        self.assertEqual(len(asked), 2, "a goal under the bar earns exactly one redraft")
        self.assertIn("FELL SHORT", asked[1])
        self.assertIn("give it a body", asked[1])

    def test_a_good_goal_is_not_redrafted(self):
        seen = []
        drafts = [_cand("Drowned Records Room", "Someone kept writing.", "It is still being written.")]
        verdict = {"scores": [{"i": 0, "worth": 5, "only_here": 5, "conflict": 4,
                               "mystery": 5, "payoff": 4, "fresh": 4}],
                   "pick": 0, "why": "good", "fix": ""}
        self._run(self._chat(drafts, verdict, seen), session_id="sid")
        self.assertEqual(len([t for t in seen if t.startswith("You are choosing the GOAL")]), 1)

    def test_the_judge_going_missing_never_blocks_a_run(self):
        def chat(msgs, **kw):
            if msgs[0]["content"].startswith("You are the game's director"):
                raise RuntimeError("judge down")
            return json.dumps({"goals": [_cand("Drowned Office", "a", "b")]})
        got = self._run(chat, session_id="sid")
        self.assertEqual(got["name"], "Drowned Office")

    def test_the_whole_thing_failing_falls_back_the_way_it_always_did(self):
        def chat(msgs, **kw):
            raise RuntimeError("no key")
        self.assertEqual(self._run(chat, session_id="sid"), {})


class TheRunEscalates(unittest.TestCase):
    """look_book's CONFLICT row is already written normal / escalating /
    critical. Goal 1, 2 and 3 are drafted against it, so the run escalates by
    design rather than by chance."""

    def test_the_leg_of_the_run_decides_the_phase(self):
        self.assertEqual(goal.PHASES, ("normal", "escalating", "critical"))
        src = (ROOT / "goal.py").read_text(encoding="utf-8")
        self.assertIn("phase=PHASES[min(len(names), len(PHASES) - 1)]", src)

    def test_each_phase_asks_for_something_different(self):
        src = (ROOT / "goal.py").read_text(encoding="utf-8")
        self.assertIn("It opens the story", src)
        self.assertIn("costs more than", src)
        self.assertIn("This is the last one", src)


class TheBarIsWrittenDown(unittest.TestCase):
    """"asking if this is a goal WORTH having in our experience" — the six
    criteria a goal is scored against, in the source, so they can be argued
    with rather than guessed at."""

    def test_all_six_criteria_are_named(self):
        for c in ("WORTH_WALKING_TO", "ONLY_HERE", "CONFLICT", "MYSTERY",
                  "PAYOFF", "FRESH"):
            self.assertIn(c, goal.JUDGE_RULES)

    def test_generic_nouns_score_one(self):
        self.assertIn("hub, spire, core, module, port", goal.JUDGE_RULES)
        self.assertIn("Score 1 for generic nouns", goal.JUDGE_RULES)

    def test_mystery_means_the_truth_differs_from_the_why(self):
        self.assertIn("is `truth` genuinely different from `why`", goal.JUDGE_RULES)
        self.assertIn("It must be different from `why`", goal.SPINE_RULES)

class WhatTheyCameForIsAlwaysTakeable(unittest.TestCase):
    """Two laps of a three-lap playtest ended with the player standing in the
    room, the thing in it, and no tag on the picture — vision had not boxed it
    in the deposited frame, so the only way to finish the goal had vanished
    ("prize_tagged: false"). The server has already put it in the room; not
    seeing it is not a reason to stop offering it."""

    def setUp(self):
        self.js = (ROOT / "static" / "js" / "standalone.js").read_text(encoding="utf-8")

    def test_an_unlocated_prize_still_gets_a_tag(self):
        self.assertIn('const inRoom = !!(r && r.phase === "prize");', self.js)
        self.assertIn("if ((!r.found || !r.box) && !inRoom) { hide(); return; }", self.js)

    def test_it_goes_where_the_payoff_shot_left_it(self):
        self.assertIn("const b = (r.found && r.box) ? r.box : "
                      "{ x: 0.42, y: 0.34, w: 0.16, h: 0.3 };", self.js)

    def test_a_goal_that_is_not_in_the_room_yet_still_needs_to_be_seen(self):
        # Outside the room, an unfound goal has no tag — that is the rule that
        # keeps a label off a picture the thing is not in.
        draw = self.js.split("function draw() {", 1)[1].split("\n    }", 1)[0]
        self.assertIn("&& !inRoom", draw)

    def test_the_prize_tag_is_not_taken_down_by_a_repaint(self):
        scene = self.js.split("function onScene() {", 1)[1].split("\n    }", 1)[0]
        self.assertIn('if (!(result && result.phase === "prize")) hide();', scene)


class TheWalkDoesNotKillTheRun(unittest.TestCase):
    """"we're dead.." — the first encounter of a run killed the player outright
    on turn 3. The engine climbs threat_level by +1 every turn on its own, and
    the approach was ADDING 2, then 3, then 4 on top of it, with nothing in the
    game that ever brings it down: three clicks put the run at 9, past critical
    (6), before the story had started. Over three goals that is 12, 24, 36."""

    def setUp(self):
        goal._APPROACH.clear(); goal._DONE.clear()

    def _walk(self, st):
        """One goal's worth of walking, with the turn loop's own +1 a turn."""
        for step in range(1, goal.APPROACH_STEPS + 1):
            st["threat_level"] = int(st.get("threat_level", 0) or 0) + 1
            goal.press(st, step)
        return int(st["threat_level"])

    def test_the_walk_is_a_floor_not_a_ladder(self):
        st = {"threat_level": 0}
        goal.press(st, 1)
        self.assertEqual(st["threat_level"], 2)
        goal.press(st, 1)                      # asking twice must not stack
        goal.press(st, 1)
        self.assertEqual(st["threat_level"], 2)

    def test_it_never_lowers_a_threat_the_world_raised(self):
        st = {"threat_level": 5}
        goal.press(st, 1)
        self.assertEqual(st["threat_level"], 5)

    def test_the_way_there_is_not_critical_the_door_is(self):
        st = {"threat_level": 0}
        for step in range(1, goal.APPROACH_STEPS):
            goal.press(st, step)
            self.assertLess(st["threat_level"], engine.STORY_CRITICAL_AT,
                            "the walk tipped the world over before the door")
        goal.press(st, goal.APPROACH_STEPS)
        self.assertEqual(st["threat_level"], engine.STORY_CRITICAL_AT)

    def test_one_goal_arrives_at_critical_and_no_further(self):
        st = {"threat_level": 0}
        self.assertEqual(self._walk(st), engine.STORY_CRITICAL_AT)

    def test_taking_a_goal_lets_the_world_breathe_out(self):
        st = {"threat_level": 0, "goals_done": [], "goal_name": "A",
              "level_goal": "A", "turn_count": 0}
        self._walk(st)
        self.assertEqual(st["threat_level"], engine.STORY_CRITICAL_AT)
        with mock.patch.object(goal, "invent", return_value={"name": "B", "why": "", "look": ""}):
            goal.advance(st, "sid", took="the thing")
        self.assertLess(st["threat_level"], engine.STORY_ESCALATE_AT)
        self.assertEqual(st.get("current_phase"), "normal")
        self.assertNotIn("goal_pressure", st)

    def test_a_whole_run_stays_survivable_and_still_escalates(self):
        st = {"threat_level": 0, "goals_done": [], "goal_name": "G1",
              "level_goal": "G1", "turn_count": 0}
        starts = []
        for lap in range(1, 4):
            starts.append(int(st.get("threat_level", 0) or 0))
            self._walk(st)
            # every lap is critical AT THE DOOR, and never worse than that
            self.assertEqual(st["threat_level"], engine.STORY_CRITICAL_AT,
                             f"lap {lap} ran away from critical")
            st["goal_name"] = st["level_goal"] = f"G{lap}"
            goal._APPROACH.clear()
            with mock.patch.object(goal, "invent",
                                   return_value={"name": f"N{lap}", "why": "", "look": ""}):
                goal.advance(st, "sid", took=f"thing{lap}")
        # ...and each lap begins a little hotter than the one before it.
        self.assertEqual(starts, sorted(starts))
        self.assertLess(starts[0], starts[-1])
        self.assertLessEqual(starts[-1], engine.STORY_CRITICAL_AT)


_GEAR = [
    {"name": "Sump Cutter", "kind": "weapon", "look": "a rusted hydraulic shear",
     "power": "Cuts a sealed door in one pass", "plate": "/b/item_01.jpg"},
    {"name": "Tidal Anchor", "kind": "armor", "look": "a barnacled iron weight",
     "power": "Holds you down when the surge comes", "plate": "/b/item_02.jpg"},
    {"name": "Second Lung", "kind": "upgrade", "look": "a scarred rebreather",
     "power": "Lets you breathe where the air is bad", "plate": "/b/item_03.jpg"},
]


def _with_gear(table=None):
    lb = mock.MagicMock()
    lb.items.return_value = [dict(g) for g in (table if table is not None else _GEAR)]
    return mock.patch.dict("sys.modules", {"look_book": lb})


class TheRunHandsOutThisWorldsGear(unittest.TestCase):
    """The look book designs six pieces of gear per world; a goal's prize is
    DRAWN from that table rather than invented for the goal. So what is waiting
    inside is scarce, belongs to this world, and already has a picture shot on
    the props sheet — instead of being a name appended to a list."""

    def setUp(self):
        goal._APPROACH.clear(); goal._DONE.clear(); goal._PRIZES.clear()

    def test_it_draws_without_replacement(self):
        st = {"gear": [], "goals_taken": []}
        got = []
        with _with_gear():
            for _ in range(4):
                g = goal.draw_gear(st, "sid")
                if not g:
                    break
                got.append(g["name"])
                st["goals_taken"].append(g["name"])
        self.assertEqual(len(got), 3)
        self.assertEqual(len(set(got)), 3, "the same gear was handed out twice")

    def test_an_empty_table_falls_back_to_inventing(self):
        with _with_gear([]):
            self.assertEqual(goal.draw_gear({}, "sid"), {})
        # ...and a look book that is not there at all must not raise.
        with mock.patch.dict("sys.modules", {"look_book": None}):
            self.assertEqual(goal.draw_gear({}, "sid"), {})

    def test_the_verb_suits_what_the_thing_is(self):
        by = {g["kind"]: goal.as_prize(g)["verb"] for g in _GEAR}
        self.assertEqual(by["weapon"], "Take")
        self.assertEqual(by["armor"], "Wear")
        self.assertEqual(by["upgrade"], "Fit")
        self.assertEqual(goal.as_prize({}), {})

    def test_the_place_is_built_around_the_thing_not_the_other_way(self):
        seen = {}

        def chat(msgs, **kw):
            t = msgs[0]["content"]
            if t.startswith("You are the game's director"):
                return json.dumps({"scores": [{"i": 0, "worth": 5, "only_here": 5,
                                               "conflict": 4, "mystery": 5,
                                               "payoff": 4, "fresh": 4}],
                                   "pick": 0, "why": "", "fix": ""})
            seen["draft"] = t
            return json.dumps({"goals": [{"name": "Pump Gallery", "why": "a",
                "look": "b", "truth": "c", "claim": "d", "cost": "e",
                "prize": {"name": "INVENTED", "look": "x", "verb": "Take"}}]})

        st = {"goals_done": [], "goal_name": "A", "level_goal": "A",
              "turn_count": 4, "threat_level": 0, "gear": [], "goals_taken": []}
        aim = mock.MagicMock(); aim.chat = chat
        gi = mock.MagicMock()
        gi.authored_setting.return_value = {"name": "Ossuary", "summary": "a relay"}
        gi.get_spec.return_value = {"player_character": {"role": "engineer"}}
        with _with_gear(), mock.patch.dict("sys.modules", {"ai_provider_manager": aim,
                                                           "game_identity": gi}):
            goal.advance(st, "sid", took="")
        draft = seen["draft"]
        self.assertIn("WHAT IS WAITING INSIDE IS ALREADY DECIDED", draft)
        self.assertIn("BUILD THE PLACE AROUND IT", draft)
        # ...and it is not asked to invent one as well.
        self.assertNotIn("Then name WHAT IS INSIDE", draft)
        # The drawn gear wins over anything the draft returned.
        self.assertIn(st["goal_prize"], [g["name"] for g in _GEAR])
        self.assertNotEqual(st["goal_prize"], "INVENTED")
        self.assertTrue(st["goal_prize_power"])
        self.assertTrue(st["goal_prize_kind"])
        self.assertTrue(st["goal_prize_plate"])

    def test_taking_it_puts_it_in_the_pack(self):
        st = {"goals_done": [], "goal_name": "Pump Gallery",
              "level_goal": "Pump Gallery", "turn_count": 6, "gear": [],
              "goal_phase": "prize", "goal_prize": "Sump Cutter",
              "goal_prize_look": "a shear", "goal_prize_verb": "Take",
              "goal_prize_kind": "weapon", "goal_prize_power": "Cuts a door",
              "goal_prize_plate": "/b/item_01.jpg"}
        with _with_gear(), mock.patch.object(goal, "invent", return_value={}):
            out = goal.take_prize(st, "sid")
        self.assertEqual(out["gear"]["name"], "Sump Cutter")
        self.assertEqual(out["gear"]["kind"], "weapon")
        self.assertEqual(out["gear"]["power"], "Cuts a door")
        self.assertEqual(out["gear"]["from"], "Pump Gallery")
        self.assertEqual([g["name"] for g in goal.held(st)], ["Sump Cutter"])
        # taking it twice does not double it up
        st["goal_phase"] = "prize"
        with _with_gear(), mock.patch.object(goal, "invent", return_value={}):
            goal.take_prize(st, "sid")
        self.assertEqual(len(goal.held(st)), 1)

    def test_the_prize_carries_its_picture_to_the_client(self):
        st = {"goal_phase": "prize", "goal_prize": "Tidal Anchor",
              "goal_prize_look": "a weight", "goal_prize_verb": "Wear",
              "goal_prize_kind": "armor", "goal_prize_power": "Holds you down",
              "goal_prize_plate": str(ROOT / "sessions" / "run-3" / "look_book"
                                      / "the-world" / "item_02.png")}
        card = goal._gear_card(st)
        self.assertEqual(card["name"], "Tidal Anchor")
        self.assertEqual(card["kind"], "armor")
        self.assertEqual(card["verb"], "Wear")
        self.assertTrue(card["plate"].startswith("/api/look_book/run-3/file/item_02.png"))
        # nothing to show while they are still walking there
        self.assertIsNone(goal._gear_card({"goal_phase": "place"}))


class TheFindIsShown(unittest.TestCase):
    """A piece of gear was designed, shot, and stood lit in the room — the
    moment it becomes theirs gets the screen, then it goes into the pack
    (static/js/pack.js), not a line of text and not a strip along the bottom."""

    def setUp(self):
        self.js = (ROOT / "static" / "js" / "standalone.js").read_text(encoding="utf-8")
        self.pack = (ROOT / "static" / "js" / "pack.js").read_text(encoding="utf-8")
        self.css = (ROOT / "static" / "css" / "pack.css").read_text(encoding="utf-8")
        self.html = (ROOT / "templates" / "standalone.html").read_text(encoding="utf-8")

    def test_taking_gear_shows_the_thing(self):
        take = self.js.split("async function takeIt(r) {", 1)[1].split("\n    }", 1)[0]
        self.assertIn("showFind(res.gear, res.board);", take)
        self.assertIn("setPack(res.pack, res.world_gear);", take)

    def test_the_last_prize_is_shown_before_the_win(self):
        # The third take used to go straight to the victory card, so the last
        # (best) find of the run was never shown at all.
        take = self.js.split("async function takeIt(r) {", 1)[1].split("\n    }", 1)[0]
        find = take.index("const shown = showFind(res.gear, res.board);")
        self.assertLess(find, take.index("await shown;"))
        self.assertLess(take.index("await shown;"), take.index("showVictory(res.board);", find))
        self.assertIn("if (won) victoryShownFor = res.board.done;", take)

    def test_the_goal_tag_hands_the_find_and_the_list_to_the_pack(self):
        fn = self.js.split("function showFind(gear, board) {", 1)[1][:400]
        self.assertIn("window.Pack.found(gear, { board: board || null })", fn)
        fn = self.js.split("function setPack(pack, world) {", 1)[1][:300]
        self.assertIn("window.Pack.sync(pack || [], world || null)", fn)

    def test_the_page_loads_the_pack(self):
        self.assertIn("filename='js/pack.js'", self.html)
        self.assertIn("filename='css/pack.css'", self.html)
        # after the game script, which hands it everything
        self.assertLess(self.html.index("filename='js/standalone.js'"),
                        self.html.index("filename='js/pack.js'"))

    def test_the_card_shows_the_thing_what_it_is_and_what_it_does(self):
        for part in ("pf-plate", "pf-kind", "pf-name", "pf-power", "pf-worth", "pf-from"):
            self.assertIn(part, self.pack)
        # ...and says whether it was a find or taken off someone
        self.assertIn('spoils ? "Spoils" : "Found"', self.pack)

    def test_a_thing_with_no_picture_shows_its_letter_not_a_broken_one(self):
        self.assertIn('im.addEventListener("error", () => { imgs.delete(key); im.replaceWith(mono(g)); });',
                      self.pack)
        self.assertIn("if (!g.plate) return mono(g);", self.pack)

    def test_the_open_pack_holds_still(self):
        # Every server answer re-syncs the pack; a rebuild made new <img>s
        # that painted blank until they decoded, and the playtest caught the
        # open pack with every cell empty right after the win.
        self.assertIn("if (sig === drawnAs && grid.childElementCount) return;", self.pack)
        self.assertIn("if (had && !had.isConnected) return had;", self.pack)
        self.assertIn("renders: S.renders,", self.pack)

    def test_the_card_hands_the_game_back_and_blocks_nothing(self):
        self.assertIn("const FIND_MS = 4600;", self.pack)
        find = self.css.split("#pack-find {", 1)[1].split("}", 1)[0]
        self.assertIn("pointer-events: none;", find)
        card = self.css.split("#pack-find .pf-card {", 1)[1].split("}", 1)[0]
        self.assertIn("pointer-events: auto;", card)

    def test_the_find_waits_for_a_fight_to_leave_the_screen(self):
        self.assertIn('"moment-active"', self.pack)
        self.assertIn("await waitUntil(() => !away(), 15000);", self.pack)

    def test_the_win_shows_what_was_carried_out(self):
        fn = self.js.split("function showVictory(board) {", 1)[1].split("\n    }", 1)[0]
        self.assertIn('window.Pack.haul($("goal-reached"))', fn)
        # no goal left to name in the corner
        self.assertIn("setHud(null);", fn)
        self.assertIn("function haul(card) {", self.pack)
        self.assertIn("#goal-reached .pk-haul-img", self.css)
        # a new run takes it off the card
        self.assertIn('document.querySelectorAll(".pk-haul").forEach((n) => n.remove());', self.pack)

    def test_the_old_strip_is_gone(self):
        self.assertIn("#goal-pack { display: none !important; }", self.css)

    def test_the_tag_says_what_kind_of_thing_it_is(self):
        self.assertIn("String(r.gear.kind).toUpperCase()", self.js)


class ThePackIsABackpackBesideTheFist(unittest.TestCase):
    """"a new icon similar to the choices icon (to the right of it though)
    that is a backpack. devise a simplified version of an RPG backpack
    interface" (Matt, 2026-09-22). Designed on the canvas, row 4."""

    def setUp(self):
        self.pack = (ROOT / "static" / "js" / "pack.js").read_text(encoding="utf-8")
        self.css = (ROOT / "static" / "css" / "pack.css").read_text(encoding="utf-8")

    def test_it_sits_right_of_the_fist_in_the_same_ring(self):
        self.assertIn('fist.insertAdjacentElement("afterend", btn)', self.pack)
        btn = self.css.split("#pack-btn {", 1)[1].split("}", 1)[0]
        self.assertIn("--pk-shift: 64px;", btn)
        self.assertIn("transform: translateX(calc(-50% + var(--pk-shift)));", btn)
        self.assertIn("width: 51px;", btn)
        self.assertIn("border-radius: 50%;", btn)

    def test_an_empty_pack_is_not_a_button(self):
        self.assertIn("#pack-btn.empty { opacity: 0; pointer-events: none; }", self.css)
        self.assertIn('btn.classList.toggle("empty", S.items.length === 0);', self.pack)

    def test_it_counts_and_glows_when_something_new_is_in_it(self):
        self.assertIn("#pack-btn.has-new {", self.css)
        self.assertIn('btn.classList.toggle("has-new", S.fresh.size > 0 && !S.open);', self.pack)
        # the count does not jump before the thing has landed
        self.assertIn("return S.items.filter((g) => !S.pending.has(keyOf(g))).length;", self.pack)

    def test_it_goes_with_the_rest_of_the_hub(self):
        for c in ("start-menu-on", "moment-active", "awaiting-first-scene", "opening-blackout"):
            self.assertIn(f"body.{c} #pack-btn", self.css)

    def test_slots_on_the_left_the_thing_on_the_right(self):
        # The character's frame (2026-09-24): three rows of four, beside the figure.
        self.assertIn("const BASE_SLOTS = 12;", self.pack)
        self.assertIn('"pk-cell" + (g ? "" : " empty")', self.pack)
        for part in ("pk-d-kind", "pk-d-name", "pk-d-power", "pk-d-worth", "pk-d-from"):
            self.assertIn(part, self.pack)
        # never loses a thing: the grid grows by rows past twelve
        self.assertIn("Math.max(BASE_SLOTS, Math.ceil(S.items.length / COLS) * COLS)", self.pack)

    def test_b_opens_it_esc_closes_it_and_i_is_left_alone(self):
        self.assertIn('if ((k === "b" || k === "B") && !e.repeat && !away())', self.pack)
        self.assertIn('if (k === "Escape" || k === "b" || k === "B")', self.pack)
        self.assertNotIn('k === "i"', self.pack)
        # typing an action never opens it
        self.assertIn('t.tagName === "INPUT" || t.tagName === "TEXTAREA"', self.pack)

    def test_a_resumed_run_is_not_all_new(self):
        self.assertIn("if (S.known === null) {\n      S.known = new Set(names);", self.pack)

    def test_the_plates_are_shown_as_cut_outs(self):
        cell = self.css.split("#pack-panel .pk-plate {", 1)[1].split("}", 1)[0]
        self.assertIn("object-fit: contain;", cell)
        self.assertIn("drop-shadow", cell)


class AWonFightPaysOut(unittest.TestCase):
    """"introduce a loot award for winning at an encounter" (Matt,
    2026-09-22). Putting someone down or talking them down hands over one
    piece of this world's gear: a spoil first, then only the treasures the
    goals will not need."""

    TABLE = [
        {"name": "Sump Cutter", "kind": "weapon", "tier": "treasure", "look": "a shear",
         "power": "Cuts a door", "plate": "/b/item_01.png", "worth": "w1"},
        {"name": "Tidal Anchor", "kind": "armor", "tier": "treasure", "look": "a weight",
         "power": "Holds you down", "plate": "/b/item_02.png"},
        {"name": "Second Lung", "kind": "upgrade", "tier": "treasure", "look": "a rebreather",
         "power": "Breathe bad air", "plate": "/b/item_03.png"},
        {"name": "Gutter Shiv", "kind": "weapon", "tier": "spoil", "look": "a shiv",
         "power": "Quiet work", "plate": "/b/item_07.png", "worth": "w7"},
    ]

    def test_a_spoil_drops_first_and_goes_in_the_pack(self):
        st = {"gear": [], "goals_done": []}
        with _with_gear(self.TABLE):
            got = goal.award_spoil(st, "sid", foe="The Archive Auditor", how="down")
        self.assertEqual(got["name"], "Gutter Shiv")
        self.assertEqual(got["source"], "encounter")
        self.assertEqual(got["from"], "The Archive Auditor")
        self.assertEqual(got["worth"], "w7")
        self.assertEqual([g["name"] for g in goal.held(st)], ["Gutter Shiv"])

    def test_the_goals_prizes_are_never_spent_on_a_fight(self):
        # Three goals ahead, three treasures left: nothing to spare once the
        # spoil is gone.
        st = {"gear": [], "goals_done": []}
        with _with_gear(self.TABLE):
            first = goal.award_spoil(st, "sid", foe="A")
            second = goal.award_spoil(st, "sid", foe="B")
        self.assertEqual(first["name"], "Gutter Shiv")
        self.assertEqual(second, {})
        # ...but a treasure the remaining goals cannot use is fair game.
        st = {"gear": [], "goals_done": ["one", "two"], "goal_prize": "Sump Cutter"}
        with _with_gear(self.TABLE[:3]):
            got = goal.award_spoil(st, "sid", foe="C")
        self.assertIn(got["name"], ("Tidal Anchor", "Second Lung"))

    def test_the_prize_being_walked_to_is_not_dropped_by_a_fight(self):
        st = {"gear": [], "goals_done": ["one", "two"], "goal_prize": "Tidal Anchor"}
        with _with_gear(self.TABLE[:3]):
            for _ in range(5):
                got = goal.award_spoil(st, "sid", foe="C")
                self.assertNotEqual((got or {}).get("name"), "Tidal Anchor")

    def test_no_table_no_loot_and_no_error(self):
        with _with_gear([]):
            self.assertEqual(goal.award_spoil({}, "sid", foe="A"), {})
        with mock.patch.dict("sys.modules", {"look_book": None}):
            self.assertEqual(goal.award_spoil({}, "sid", foe="A"), {})

    def test_the_pack_card_says_where_it_came_from(self):
        card = goal._pack_card({"name": "Gutter Shiv", "kind": "weapon", "tier": "spoil",
                                "power": "Quiet work", "worth": "w7", "from": "A",
                                "source": "encounter", "plate": ""})
        self.assertEqual(card["source"], "encounter")
        self.assertEqual(card["worth"], "w7")
        self.assertEqual(card["tier"], "spoil")

    def test_what_was_picked_up_along_the_way_is_in_the_same_pack(self):
        st = {"gear": [{"name": "Gutter Shiv", "kind": "weapon", "source": "encounter"}],
              "inventory": ["crowbar", "flashlight"]}
        cards = goal.pack_cards(st, "sid")
        self.assertEqual([c["name"] for c in cards], ["Gutter Shiv", "Crowbar", "Flashlight"])
        self.assertEqual(cards[1]["source"], "pickup")
        self.assertEqual(cards[1]["plate"], "")
        css = (ROOT / "static" / "css" / "pack.css").read_text(encoding="utf-8")
        self.assertIn("#inventory-hud { display: none !important; }", css)

    def test_how_much_of_the_world_is_found(self):
        st = {"gear": [{"name": "Gutter Shiv"}, {"name": "Invented Thing"}]}
        with _with_gear(self.TABLE):
            self.assertEqual(goal.world_gear(st, "sid"), {"found": 1, "of": 4})

    def test_the_encounter_pays_out_on_a_win_and_only_a_win(self):
        src = (ROOT / "encounter.py").read_text(encoding="utf-8")
        body = src.split("def api_resolve():", 1)[1]
        self.assertIn("won = bool(\n        released and rolled[\"outcome\"] != \"die\"", body)
        self.assertIn("in ENCOUNTER_ENEMY_SETTLED", body.split("won = bool(", 1)[1][:260])
        self.assertIn("got = _goal.award_spoil(st, session_id, foe=foe,", body)
        self.assertIn('foe = subject if boss_fight else foe_name(', body)
        self.assertIn('"loot": loot,', body)
        self.assertIn('"pack": pack,', body)

    def test_the_spoil_says_who_it_came_off_in_plain_words(self):
        import encounter
        book = {"roster_looks": [{"kind": "a scavenger hauling scrap", "terms": ["scavenger", "picker"]}]}
        with mock.patch("look_book.current", return_value=book), \
             mock.patch("look_book._entry_for", side_effect=lambda b, k: b["roster_looks"][0]):
            self.assertEqual(encounter.foe_name("sid", {"roster_kind": "a scavenger hauling scrap"},
                                                "A man"), "the scavenger")
        self.assertEqual(encounter.foe_name("sid", {}, "A man"), "a man")
        self.assertEqual(encounter.foe_name("sid", {}, "A figure in long"), "a figure")
        self.assertEqual(encounter.foe_name("sid", {}, "The Alpha Handler"), "The Alpha Handler")

    def test_the_client_shows_the_spoils(self):
        js = (ROOT / "static" / "js" / "standalone.js").read_text(encoding="utf-8")
        after = js.split('await playVerdict(outcome, res.verdict_word || "", gearLine);', 1)[1][:900]
        self.assertIn('window.Pack.found(res.loot, { source: "encounter" })', after)
        self.assertIn("window.Pack.sync(res.pack)", after)


class GearWorksInAFight(unittest.TestCase):
    """Loot that changes nothing is a display case. Two rules a player can
    read off the verdict card: a weapon carried makes a committed attack
    likelier to end it; armour carried takes one killing blow a fight."""

    class _Rng:
        def __init__(self, *vals):
            self.vals = list(vals)

        def random(self):
            return self.vals.pop(0)

    def test_the_pack_says_what_it_fights_with(self):
        st = {"gear": [{"name": "Old Knife", "kind": "weapon"},
                       {"name": "Blast Apron", "kind": "armor"},
                       {"name": "Shock Baton", "kind": "weapon"},
                       {"name": "Slit Visor", "kind": "upgrade"}]}
        self.assertEqual(goal.gear_edge(st), {"weapon": ["Shock Baton", "Old Knife"],
                                              "armor": ["Blast Apron"]})
        self.assertEqual(goal.gear_edge({}), {})

    def test_the_verdict_card_names_the_gear_that_did_it(self):
        js = (ROOT / "static" / "js" / "standalone.js").read_text(encoding="utf-8")
        self.assertIn("function playVerdict(outcome, wordOverride, gearLine) {", js)
        self.assertIn('const gearLine = res.armor_saved ? `The ${res.armor_saved} took it`', js)
        pack = (ROOT / "static" / "js" / "pack.js").read_text(encoding="utf-8")
        self.assertIn('armor: "In a fight: takes one killing blow."', pack)
        self.assertIn('weapon: "In a fight: +1 to hit, and 2d6+3 damage instead of 1d8+3."', pack)

    def test_a_goals_walk_holds_one_roadside_fight(self):
        import encounter
        st = {"level_goal": "Pump Gallery", "encounter_travel_remain": 1.0}
        fire, why, _ = encounter.apply_travel(st, 2.0)
        self.assertTrue(fire)
        self.assertEqual(st["goal_leg_fights"], 1)
        st["encounter_travel_remain"] = 1.0
        fire, why, _ = encounter.apply_travel(st, 2.0)
        self.assertFalse(fire)
        self.assertEqual(why, "leg_quota")
        # a person in the picture counts the same way
        self.assertEqual(encounter.sighting_can_fire(st), (False, "leg_quota"))
        st2 = {"level_goal": "Pump Gallery", "turn_count": 9}
        encounter.stage_sighting(st2, {"label": "A guard", "kind": "person"})
        self.assertEqual(st2["goal_leg_fights"], 1)
        # a run with no goal is not rationed
        free = {"encounter_travel_remain": 1.0, "goal_leg_fights": 5}
        self.assertTrue(encounter.apply_travel(free, 2.0)[0])
        # ...and the next goal's walk gets its own
        src = (ROOT / "goal.py").read_text(encoding="utf-8")
        adv = src.split("def advance(", 1)[1]
        self.assertIn('st.pop("goal_leg_fights", None)', adv)


class TheBossStaysTheBoss(unittest.TestCase):
    """api_begin stamps the fight at the goal as the boss, then aligns the
    brief to its plate — a rebuild that dropped the stamp. So the boss fight
    rolled as a roadside one (two rounds, not three), was renamed to what the
    picture showed ("Commander Of Edicts" became "A figure in heavy"), and its
    defeat never reached goal.boss_defeated (00:10 playtest)."""

    def test_the_stamp_survives_the_rebuild(self):
        import encounter
        brief = encounter.normalize_encounter_brief({
            "character": {"label": "Commander Of Edicts", "look": "a tall officer"},
            "boss": True, "armor_spent": "Blast Apron"})
        self.assertTrue(brief["boss"])
        self.assertEqual(brief["armor_spent"], "Blast Apron")
        plain = encounter.normalize_encounter_brief({"character": {"label": "A guard"}})
        self.assertNotIn("boss", plain)

    def test_the_plate_does_not_rename_the_boss(self):
        import encounter
        brief = {"character": {"label": "Commander Of Edicts", "look": "a tall officer"},
                 "boss": True, "max_rounds": 3}
        out = encounter.align_brief_to_plate(
            brief, {"description": "a figure in heavy armour stands in the smoke"})
        self.assertEqual(out["character"]["label"], "Commander Of Edicts")
        self.assertTrue(out["boss"])


class GoalsHoldTreasures(unittest.TestCase):
    """A goal holds a treasure; the spoils are for the fights."""

    def test_a_goal_draws_a_treasure_while_there_are_any(self):
        table = AWonFightPaysOut.TABLE
        for _ in range(12):
            with _with_gear(table):
                self.assertEqual(goal.draw_gear({"gear": []}, "sid").get("tier"), "treasure")
        with _with_gear([table[3]]):
            self.assertEqual(goal.draw_gear({"gear": []}, "sid")["name"], "Gutter Shiv")

    def test_taking_the_prize_keeps_the_whole_row(self):
        st = {"goals_done": [], "goal_name": "Pump Gallery", "level_goal": "Pump Gallery",
              "turn_count": 6, "gear": [], "goal_phase": "prize", "goal_prize": "Sump Cutter",
              "goal_prize_look": "a shear", "goal_prize_verb": "Take",
              "goal_prize_kind": "weapon", "goal_prize_power": "Cuts a door",
              "goal_prize_plate": "/b/item_01.png"}
        with _with_gear(AWonFightPaysOut.TABLE), mock.patch.object(goal, "invent", return_value={}):
            out = goal.take_prize(st, "sid")
        self.assertEqual(out["gear"]["worth"], "w1")
        self.assertEqual(out["gear"]["tier"], "treasure")
        self.assertEqual(out["gear"]["source"], "goal")

    def test_a_goal_drafted_before_the_gear_existed_gets_it_on_the_way_in(self):
        st = {"level_goal": "Pump Gallery", "goal_name": "Pump Gallery", "goal_phase": "place",
              "goal_prize": "The Cache", "goal_prize_look": "a case", "goal_prize_kind": "",
              "gear": []}
        with _with_gear(AWonFightPaysOut.TABLE):
            pz = goal.bind_gear(st, "sid")
        self.assertTrue(pz)
        self.assertEqual(st["goal_prize_kind"], pz["kind"])
        self.assertNotEqual(st["goal_prize"], "The Cache")
        # ...and a goal that already holds gear keeps it.
        before = dict(st)
        with _with_gear(AWonFightPaysOut.TABLE):
            self.assertEqual(goal.bind_gear(st, "sid"), {})
        self.assertEqual(st["goal_prize"], before["goal_prize"])

    def test_the_walk_binds_it(self):
        src = (ROOT / "goal.py").read_text(encoding="utf-8")
        walk = src.split('if body.get("approach"):', 1)[1].split("return jsonify", 1)[0]
        self.assertIn("press(live, step[\"steps\"])\n                    bind_gear(live, sid)", walk)


class ThePlateTheClientGets(unittest.TestCase):
    """The find card and the pack showed a broken picture for a whole run:
    plates went out as /api/look_book/plate?path=C:\\... — a route that never
    existed. They go out through the look book's own file route now."""

    def test_a_disk_path_becomes_the_file_route(self):
        p = ROOT / "sessions" / "run-9" / "look_book" / "the-world" / "item_04.png"
        url = goal._plate_url(str(p))
        self.assertTrue(url.startswith("/api/look_book/run-9/file/item_04.png?w=the-world"), url)
        self.assertNotIn("?path=", url)

    def test_a_url_passes_through_and_nothing_is_nothing(self):
        self.assertEqual(goal._plate_url("/api/look_book/x/file/item_01.png?w=y"),
                         "/api/look_book/x/file/item_01.png?w=y")
        self.assertEqual(goal._plate_url(""), "")
        self.assertEqual(goal._plate_url("/somewhere/else/item_01.png"), "")

    def test_every_answer_carries_the_pack(self):
        src = (ROOT / "goal.py").read_text(encoding="utf-8")
        api = src.split("def api_goal_sight():", 1)[1]
        # won, goalless, and every ordinary answer
        self.assertIn('"board": standing(st), **_pack_body(st, sid)})', api)
        self.assertIn('return jsonify({"ok": True, "goal": False, **_pack_body(st, sid)})', api)
        self.assertIn("**_pack_body(st, sid),", api)


class NamesDoNotEndMidPhrase(unittest.TestCase):

    def test_a_long_line_is_cut_at_a_word_that_ends_a_name(self):
        self.assertEqual(goal._clean_name("The reinforced blast door at the end of the hall"),
                         "The reinforced blast door")
        self.assertEqual(goal.name_from("The reinforced blast door at the end of the hall, "
                                        "marked by a faint green light"),
                         "The reinforced blast door")
        self.assertEqual(goal._clean_name("Horizon Plant"), "Horizon Plant")


class TheFirstGoalDrawsItsGearToo(unittest.TestCase):
    """Goals 2 and 3 go through goal.advance, which draws from the look book
    before it drafts the place. The FIRST goal is built in engine, and without
    the same draw the opening lap was the only one that handed out an invented
    prize. The draw once read a `session_id` that was not in scope: the
    NameError took the draw and the draft down with it, and the first goal
    fell back to the level's sentence cut at 34 characters — "The reinforced
    blast door at the" (22:00 playtest)."""

    def test_the_opening_goal_draws_from_this_runs_table(self):
        seen = {}

        def draw(state, sid):
            seen["sid"] = sid
            return {"name": "Sump Cutter", "kind": "weapon", "look": "a shear",
                    "power": "Cuts a door", "plate": "/b/item_01.png"}

        def invent(**kw):
            seen["holds"] = kw.get("holds")
            seen["invent_sid"] = kw.get("session_id")
            return {"name": "Pump Gallery", "why": "w", "look": "l",
                    "prize": {"name": "INVENTED", "look": "x", "verb": "Take"}}

        st = {}
        with mock.patch.object(engine, "LLM_ENABLED", True), \
             mock.patch.object(engine._goal_mod, "draw_gear", draw), \
             mock.patch.object(engine._goal_mod, "invent", invent), \
             mock.patch.object(engine.game_identity, "level_goal", return_value=""), \
             mock.patch.object(engine, "log_error") as err:
            engine._goal_for_this_run(st, "", "run-7")
        self.assertEqual(seen["sid"], "run-7")
        self.assertEqual(seen["invent_sid"], "run-7")
        self.assertEqual(seen["holds"]["name"], "Sump Cutter")
        self.assertEqual(st["goal_name"], "Pump Gallery")
        self.assertEqual(st["goal_prize"], "Sump Cutter")      # drawn beats invented
        self.assertEqual(st["goal_prize_kind"], "weapon")
        err.assert_not_called()

    def test_an_authored_world_keeps_its_words_and_gets_the_gear(self):
        st = {}
        with mock.patch.object(engine, "LLM_ENABLED", True), \
             mock.patch.object(engine._goal_mod, "draw_gear",
                               return_value={"name": "Tidal Anchor", "kind": "armor",
                                             "look": "a weight", "power": "p", "plate": ""}), \
             mock.patch.object(engine._goal_mod, "invent",
                               return_value={"name": "Blast Door", "why": "w", "look": "l"}), \
             mock.patch.object(engine.game_identity, "level_goal",
                               return_value="The reinforced blast door at the end of the hall."):
            engine._goal_for_this_run(st, "", "run-8")
        self.assertEqual(st["level_goal"], "The reinforced blast door at the end of the hall.")
        self.assertEqual(st["goal_name"], "Blast Door")
        self.assertEqual(st["goal_prize"], "Tidal Anchor")

    def test_with_no_model_it_still_does_not_fall_over(self):
        st = {}
        with mock.patch.object(engine, "LLM_ENABLED", False), \
             mock.patch.object(engine.game_identity, "level_goal",
                               return_value="The red pump house, past the tanks."):
            engine._goal_for_this_run(st, "", "run-9")
        self.assertEqual(st["goal_name"], "The red pump house")

    def test_every_caller_says_which_run(self):
        src = (ROOT / "engine.py").read_text(encoding="utf-8")
        self.assertIn('_goal_for_this_run(state, "", session_id)', src)
        self.assertIn('_goal_for_this_run(new_state, shot.get("goal"), session_id)', src)
        self.assertIn('_goal_for_this_run(new_state, "", SID)', src)


class TheRewardContinuesTheFrameOnScreen(unittest.TestCase):
    """Played 2026-09-23: "the cutscene with the goal reward uses a different
    character and loses continuity with the frame we were at when triggering
    the cutscene". The player was on top of the rig in a PRESS vest; panel 1
    was a low wide at its foot, the player in a blue jacket. Four causes, each
    pinned: the brief asked for four unrelated cameras; the prompt said the
    goal is "NOT REACHED in any of them"; the frame on screen went out
    unlabelled; and it went out under the encounter's restage ("copy BOTH
    faces … hands on the other body") instead of the keyframe contract."""

    def setUp(self):
        self.st = {"goal_name": "Rusted Rig Skeleton", "goal_look": "an oil derrick",
                   "goal_why": "The shear is up there.", "level_goal": "Rusted Rig Skeleton",
                   "turn_count": 11}

    def test_the_brief_is_one_take_from_the_frame_on_screen(self):
        brief = goal.reward_brief(self.st)
        self.assertIn("ONE CONTINUOUS TAKE", brief)
        self.assertIn("the next instant of that frame", brief)
        self.assertNotIn("FOUR DIFFERENT CAMERAS", brief)

    def test_panel_one_continues_the_start_keyframe(self):
        import cutscene
        first = cutscene.MOODS["reward"]["shots"][0][2]
        self.assertIn("THE NEXT INSTANT OF THE START KEYFRAME", first)
        prompt = cutscene.build_cutscene_prompt(
            "reward", shot_brief=goal.reward_brief(self.st), goal="Rusted Rig Skeleton",
            plate_role="anchor")
        self.assertIn("ONE CONTINUOUS TAKE", prompt)
        self.assertIn("the SHEET wins", prompt)          # who they are: the sheet, not a drifted frame
        self.assertNotIn("NOT REACHED", prompt)          # the reward IS reaching it
        self.assertNotIn("Do not teleport", prompt)      # the take goes inside
        # every other restage keeps its place lock and its far-off goal
        other = cutscene.build_cutscene_prompt("threshold", goal="A door", plate_role="anchor")
        self.assertIn("PLACE LOCK", other)
        self.assertIn("NOT REACHED", other)

    def test_the_frame_goes_out_first_labelled_under_the_keyframe_contract(self):
        import cutscene, tempfile
        import gemini_image_utils as giu
        from PIL import Image
        tmp = Path(tempfile.mkdtemp())
        frame = tmp / "on_screen.png"
        Image.new("RGB", (64, 36), (90, 60, 40)).save(frame)
        seen = {}

        def fake(**kw):
            seen.update(kw)
            return None   # no grid: generate_shots falls back to crops, fine here

        with mock.patch.object(giu, "GEMINI_API_KEY", "k"), \
                mock.patch.object(giu, "generate_gemini_img2img", side_effect=fake), \
                mock.patch.object(engine, "get_state", return_value=self.st), \
                mock.patch.object(cutscene, "_identity_plates", return_value=[]):
            cutscene.generate_shots(frame, session_id="t", mood="reward",
                                    shot_brief=goal.reward_brief(self.st),
                                    goal="Rusted Rig Skeleton", output_dir=tmp)
        self.assertTrue(seen, "the reward grid was never asked for")
        self.assertEqual(seen["lead_reference"], str(frame))
        self.assertIn("START KEYFRAME", seen["reference_labels"][str(frame)])
        self.assertIn("THE PLAYER", seen["reference_labels"][str(frame)])
        self.assertTrue(seen["is_flipbook"])
        self.assertEqual(seen["flipbook_grid"], (2, 2))
        self.assertFalse(seen["hold_cast"])
        self.assertFalse(seen["include_people"])

    def test_the_turn_after_it_starts_from_inside_not_from_the_frame_before(self):
        """"make sure the goal, to the scene after, is also using the right
        img2img" (2026-09-23). The reward deposit wrote its last panel into
        history as the handoff, but the flipbook — the renderer every turn
        uses — never reads history for its start keyframe: it reads
        ``flipbook_last_frame``, which still held the last panel of the turn
        BEFORE the reward, outside the place. The World stitch clears that
        key (_WORLD_SCOPED_KEYS); the reward did not. So "Take the shear"
        was drawn continuing the frame outside the rig, the frame before that
        riding in as its WIDER VIEW, and the room the reward had just walked
        the player into reached the grid nowhere."""
        import tempfile
        import gemini_image_utils as giu
        from PIL import Image
        tmp = Path(tempfile.mkdtemp())

        def png(name, rgb):
            p = tmp / name
            Image.new("RGB", (64, 36), rgb).save(p)
            return str(p)

        outside = png("turn11_last.png", (200, 120, 40))
        outside_wide = png("turn11_first.png", (180, 110, 40))
        panels = [png(f"reward_{i}.png", (20, 20 + 40 * i, 90)) for i in range(4)]
        st = dict(self.st, flipbook_last_frame=outside, flipbook_first_frame=outside_wide,
                  flipbook_last_grid=outside_wide,
                  current_sequence={"frames": ["/images/turn11_last.png"]},
                  current_observed_vision="The rusted rig from its platform, the Warden below.",
                  flipbook_mode=True, flipbook_frames=4)
        pending = {"mood": "reward", "shots": [
            {"path": p, "url": f"/images/{Path(p).name}"} for p in panels]}
        rows = []
        with mock.patch.object(engine, "_load_history", return_value=[]), \
             mock.patch.object(engine, "_save_history", side_effect=lambda h, s: rows.extend(h)):
            goal.finish_reward(st, "sid", pending)
        inside = panels[-1]
        # the history handoff was always right
        self.assertEqual(rows[-1]["image"], inside)
        self.assertTrue(rows[-1]["hard_transition"])
        # a turn that cuts out of the room is not handed the outside again
        self.assertEqual(rows[-1]["montage_refs"], [panels[2]])
        # ...and now the flipbook's own anchors say the same thing
        self.assertEqual(st.get("flipbook_last_frame"), inside)
        self.assertFalse(st.get("flipbook_first_frame"))
        self.assertFalse(st.get("flipbook_last_grid"))
        self.assertFalse(st.get("current_sequence"))
        self.assertNotIn("Warden below", str(st.get("current_observed_vision") or ""))

        # The next turn, drawn the way the engine draws it: the history
        # collector hands it the handoff frame as refs[0], no anchor flag.
        seen = {}

        def fake(**kw):
            seen.update(kw)
            return None
        with mock.patch.object(giu, "generate_gemini_img2img", side_effect=fake):
            engine._flipbook_generate(
                prompt_str="You close your hand on the shear.", caption="shear",
                choice="Take the Hydraulic Wire Shear", dispatch="You take it.",
                world_prompt="", time_of_day="dusk", img_dir=tmp, session_id="sid",
                st=st, refs=[inside], write_state=False)
        self.assertTrue(seen, "the next turn's grid was never asked for")
        self.assertEqual(seen["lead_reference"], inside)
        sent = [str(p) for p in seen["reference_image_path"]]
        self.assertNotIn(outside, sent)
        self.assertNotIn(outside_wide, sent)
        self.assertIn("START KEYFRAME", seen["reference_labels"][inside])


if __name__ == "__main__":
    unittest.main()
