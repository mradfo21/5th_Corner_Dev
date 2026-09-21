"""Switching Worlds mid-run leaves the previous World behind — all of it.

Reported: "I still find issues switching worlds. It feels not all data gets
cleared for the cutscene, which then pollutes the rest."

Traced, that was three things. A world-to-world cutscene was composed for the
World being LEFT: its World was bound only when the montage finished, so the
bible, the place, the landmarks and the goal the montage drew from were the old
World's, and its plate was the frame the player was standing on. The stitch
itself mutated the live state and cleared six keys — the goal, the heat, the
witness, the story clock, the narrator memory, the encounter roster, the
flipbook keyframe (reference slot 1 of the next grid) and the lighting line all
survived by omission. And history.json was never marked, so the new World's
first frame was img2img'd off the old World's last one and every frame after
chained off that.

Pure functions with the stores patched. No network.

Run with:
    python -m unittest test_world_stitch -v
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")

from PIL import Image

import cutscene
import engine
import experience_store
import game_identity
import worlds_store

ROOT = Path(__file__).resolve().parent

WORLD_A = {"id": "wA", "name": "The Sub-Level", "slug": "sub-level"}
WORLD_B = {"id": "wB", "name": "The Shipyard", "slug": "shipyard"}
CUT = {"id": "c1", "name": "Through the hatch", "mood": "threshold",
       "source": "incoming", "shot_brief": ""}
EXP = {"id": "exp", "worlds": [WORLD_A, WORLD_B], "cutscenes": [CUT],
       "transitions": [{"from": "c1", "to": "wB", "condition": {"type": "immediate"}}]}


def _polluted_state():
    """A run seven turns into World A, with everything World A wrote."""
    return {
        "turn_count": 7, "world_turn_count": 7,
        "experience_id": "exp", "experience_world_id": "wA",
        "level_goal": "The blast door with the green sigil", "goal_reached_turn": 5,
        "detection": {"heat": 9, "level": 3, "since_turn": 2},
        "detection_witness": {"watchers": 2, "label": "a guard", "at_turn": 7},
        "threat_level": 11, "current_phase": "critical", "fate": "UNLUCKY",
        "chaos_level": 4, "in_combat": True,
        "encounter_roster": ["a System purge enforcer"], "encounter_kinds_used": ["x"],
        "encounter_last_turn": 6, "encounter_last_label": "a creature",
        "encounter_sighting": {"label": "a creature", "turn": 7},
        "encounter_travel_remain": 3.2,
        "flipbook_last_frame": "C:/old/panel_f04.png", "flipbook_first_frame": "C:/old/panel_f01.png",
        "flipbook_last_grid": "C:/old/grid.png", "current_sequence": {"frames": ["x"]},
        "narrator_recent": ["The corridor hums."], "narrator_beat": "dread",
        "recent_events": ["a pipe fell"], "environment_streak": 4,
        "environment_streak_vocab": ["corridor"],
        "seen_elements": ["blast door", "pipe rack"], "scene_objects": ["pipes"],
        "scene_objects_turn": 7, "choices": ["Sprint"], "choices_metadata": {"x": 1},
        "time_of_day": "7:14pm | weather: toxic neon green | mood: dread",
        # what the player carries, and who they are, stay
        "inventory": ["access codes"], "companions": {"medic": {"label": "medic"}},
        "player_state": {"alive": True, "condition": "wounded"},
        "feed_log": [{"id": 1, "type": "narration", "content": "..."}],
        "world_prompt": "old bible",
    }


class TestTheStitchLeavesTheOldWorldBehind(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.plate = self.tmp / "shipyard_plate.png"
        Image.new("RGB", (32, 32)).save(self.plate)
        self.saved_hist = None

        def _save_hist(hist, sid):
            self.saved_hist = list(hist)

        self.patches = [
            mock.patch.object(experience_store, "get_experience", return_value=EXP),
            mock.patch.object(experience_store, "with_lore", side_effect=lambda t: t),
            mock.patch.object(engine, "_bind_world_prompts", return_value=True),
            mock.patch.object(engine, "purge_run_caches"),
            mock.patch.object(engine, "_generate_random_starting_time",
                              return_value="6:05am | weather: fog off the water | mood: cold"),
            mock.patch.object(engine, "_goal_for_this_run",
                              side_effect=lambda st, authored="": st.__setitem__("level_goal", "The dry dock gate")),
            mock.patch.object(engine, "_load_history", return_value=[{"choice": "old", "image": "C:/old/x.png"}]),
            mock.patch.object(engine, "_save_history", side_effect=_save_hist),
            mock.patch.object(engine, "_to_web_image_url", side_effect=lambda p, s: "/images/" + os.path.basename(p)),
            mock.patch.object(game_identity, "world_brief", side_effect=lambda t: "new bible: " + t),
            mock.patch.object(game_identity, "place_summary", return_value="A flooded shipyard under fog"),
            mock.patch.dict(engine.PROMPTS, {"world_initial_state": "shipyard bible"}, clear=False),
        ]
        for p in self.patches:
            p.start()
        import world_frames
        self._wf = mock.patch.multiple(
            world_frames,
            record=mock.Mock(return_value={"path": str(self.plate), "url": "/api/worlds/shipyard/frame", "drawn": "abc"}),
            drawn_from_live=mock.Mock(return_value=True),
        )
        self._wf.start()

    def tearDown(self):
        self._wf.stop()
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_everything_about_the_old_place_goes(self):
        st = _polluted_state()
        info = engine.apply_experience_world(st, "wB", "s")
        self.assertEqual(info["to"]["id"], "wB")
        # the keys the engine reads with a default are re-established empty;
        # everything else is gone outright
        reset = {"scene_objects": [], "scene_objects_turn": -1, "choices": [], "in_combat": False,
                 "level_goal": "The dry dock gate"}     # re-drafted for THIS place (patched above)
        for key in engine._WORLD_SCOPED_KEYS:
            if key in reset:
                self.assertEqual(st[key], reset[key], key)
            else:
                self.assertNotIn(key, st, key)
        # ...and is re-established for the new one
        self.assertEqual(st["detection"], {"heat": 0, "level": engine.DETECT_HIDDEN, "since_turn": 7})
        self.assertEqual(st["threat_level"], 0)
        self.assertEqual(st["current_phase"], "normal")
        self.assertEqual(st["chaos_level"], 0)
        self.assertEqual(st["seen_elements"], [])
        self.assertEqual(st["world_turn_count"], 0)
        self.assertTrue(st["pending_world_transition"])
        self.assertEqual(st["experience_world_id"], "wB")
        self.assertEqual(st["world_prompt"], "new bible: shipyard bible")

    def test_the_run_re_rolls_its_light_and_its_goal_for_the_new_place(self):
        """The lighting line goes into every render as "Lighting:" and was
        rolled off the OLD level's palette; the goal reaches every prompt."""
        st = _polluted_state()
        engine.apply_experience_world(st, "wB", "s")
        self.assertIn("fog off the water", st["time_of_day"])
        self.assertEqual(st.get("level_goal"), "The dry dock gate")
        self.assertNotIn("goal_reached_turn", st)

    def test_what_the_player_carries_stays(self):
        st = _polluted_state()
        engine.apply_experience_world(st, "wB", "s")
        self.assertEqual(st["inventory"], ["access codes"])
        self.assertEqual(st["companions"], {"medic": {"label": "medic"}})
        self.assertEqual(st["player_state"], {"alive": True, "condition": "wounded"})
        self.assertEqual(st["turn_count"], 7)
        self.assertEqual(len(st["feed_log"]), 1)

    def test_history_gets_a_boundary_the_next_frame_continues_from(self):
        """The img2img reference walk stops at a hard transition and nothing
        wrote one for a stitch: the new World's first frame was drawn off the
        old World's last panel."""
        st = _polluted_state()
        engine.apply_experience_world(st, "wB", "s")
        row = self.saved_hist[-1]
        self.assertEqual(row["choice"], "__world_stitch__")
        self.assertTrue(row["hard_transition"])
        self.assertTrue(row["world_stitch"])
        self.assertEqual(row["image"], str(self.plate))
        self.assertIn("shipyard", row["vision_dispatch"].lower())
        self.assertEqual(st["current_image_url"], "/api/worlds/shipyard/frame")
        # the flipbook keyframe handle is gone, so the next grid's START is
        # whatever history says — this row.
        self.assertNotIn("flipbook_last_frame", st)
        # The turn after a stitch is forced to a hard cut, and a hard cut
        # blurs its reference to a swatch unless the reference is an opening
        # handoff. This row is one: the first frame keeps the anchor's pixels.
        self.assertTrue(row["cached_opening"])
        self.assertEqual(row["montage_refs"], [])
        self.assertEqual(row["guide_image"], str(self.plate))

    def test_the_arrival_montages_last_panel_beats_the_plate(self):
        shot = self.tmp / "cutscene_arrival_04.png"
        Image.new("RGB", (32, 32)).save(shot)
        st = _polluted_state()
        engine.apply_experience_world(st, "wB", "s", arrival_shot=str(shot),
                                      montage_refs=["C:/x/cutscene_arrival_01.png"])
        row = self.saved_hist[-1]
        self.assertEqual(row["image"], str(shot))
        self.assertEqual(row["montage_refs"], ["C:/x/cutscene_arrival_01.png"])
        self.assertEqual(st["current_image_url"], "/images/cutscene_arrival_04.png")

    def test_a_missing_world_is_left_alone(self):
        st = _polluted_state()
        self.assertIsNone(engine.apply_experience_world(st, "nope", "s"))
        self.assertEqual(st["experience_world_id"], "wA")
        self.assertEqual(st["threat_level"], 11)


class TestTheArrivalCutsceneIsComposedForTheDestination(unittest.TestCase):

    def test_staging_a_cutscene_that_leads_elsewhere_binds_that_world_first(self):
        st = {"experience_world_id": "wA"}
        with mock.patch.object(experience_store, "get_experience", return_value=EXP), \
             mock.patch.object(engine, "_generate_random_starting_time", return_value="x"), \
             mock.patch.object(engine, "_bind_world_prompts", return_value=True) as bind:
            info = engine.apply_experience_cutscene(st, "c1", "s")
        bind.assert_called_once()
        self.assertEqual(bind.call_args[0][0]["id"], "wB")
        pending = st["pending_cutscene"]
        self.assertEqual(pending["to_world"], "wB")
        self.assertTrue(pending["arrival"])
        self.assertEqual(info["kind"], "cutscene")
        # the STATE swap waits for the arrival: the player is still in A
        self.assertEqual(st["experience_world_id"], "wA")

    def test_a_cutscene_that_returns_to_the_same_world_binds_nothing(self):
        exp = dict(EXP, transitions=[{"from": "c1", "to": "wA", "condition": {"type": "immediate"}}])
        st = {"experience_world_id": "wA"}
        with mock.patch.object(experience_store, "get_experience", return_value=exp), \
             mock.patch.object(engine, "_bind_world_prompts", return_value=True) as bind:
            engine.apply_experience_cutscene(st, "c1", "s")
        bind.assert_not_called()
        self.assertNotIn("arrival", st["pending_cutscene"])

    def test_staging_an_arrival_rolls_the_destinations_lighting_once(self):
        """The montage reads "Lighting:" from the state. Rolled after the bind
        so it is the destination's hour, and remembered so the stitch does
        not roll a second one under the first playable frame."""
        st = {"experience_world_id": "wA", "time_of_day": "7:14pm | weather: toxic neon | mood: dread"}
        with mock.patch.object(experience_store, "get_experience", return_value=EXP), \
             mock.patch.object(engine, "_bind_world_prompts", return_value=True), \
             mock.patch.object(engine, "_generate_random_starting_time",
                               return_value="6:05am | weather: fog off the water | mood: cold"):
            engine.apply_experience_cutscene(st, "c1", "s")
        self.assertIn("fog off the water", st["time_of_day"])
        self.assertEqual(st["pending_cutscene"]["arrival_lighting"], st["time_of_day"])

    def test_a_departure_draws_in_the_world_it_leaves(self):
        """"Departure" is a last look at the place being left. It keeps that
        World's prompts for the montage; the destination is bound when it
        ends, and the run lands on the destination's own plate — never on a
        panel of the old place."""
        exp = dict(EXP, cutscenes=[dict(CUT, mood="departure")])
        st = {"experience_world_id": "wA"}
        with mock.patch.object(experience_store, "get_experience", return_value=exp), \
             mock.patch.object(engine, "_bind_world_prompts", return_value=True) as bind:
            engine.apply_experience_cutscene(st, "c1", "s")
        bind.assert_not_called()
        pending = st["pending_cutscene"]
        self.assertEqual(pending["to_world"], "wB")
        self.assertNotIn("arrival", pending)
        self.assertNotIn("arrival_lighting", pending)

    def test_completing_a_departure_lands_on_the_plate_not_the_montage(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            last = tmp / "cutscene_x_04.png"
            Image.new("RGB", (8, 8)).save(last)
            st = {"experience_world_id": "wA", "experience_cutscene_id": "c1",
                  "time_of_day": "old light",
                  "pending_cutscene": {"cutscene_id": "c1", "to_world": "wB",
                                       "shots": [{"path": str(last)}]}}
            with mock.patch.object(experience_store, "get_experience", return_value=EXP), \
                 mock.patch.object(engine, "apply_experience_world",
                                   return_value={"from": WORLD_A, "to": WORLD_B, "transition": None}) as apply:
                engine.complete_cutscene(st, "s")
            kw = apply.call_args.kwargs
            self.assertEqual(kw["arrival_shot"], "")
            self.assertEqual(kw["montage_refs"], [])
            self.assertTrue(kw["relight"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_completing_an_arrival_keeps_the_lighting_it_drew_under(self):
        st = {"experience_world_id": "wA", "experience_cutscene_id": "c1",
              "time_of_day": "6:05am | weather: fog off the water | mood: cold",
              "pending_cutscene": {"cutscene_id": "c1", "to_world": "wB", "arrival": True,
                                   "arrival_lighting": "6:05am | weather: fog off the water | mood: cold",
                                   "shots": []}}
        with mock.patch.object(experience_store, "get_experience", return_value=EXP), \
             mock.patch.object(engine, "apply_experience_world",
                               return_value={"from": WORLD_A, "to": WORLD_B, "transition": None}) as apply:
            engine.complete_cutscene(st, "s")
        self.assertFalse(apply.call_args.kwargs["relight"])

    def test_completing_it_hands_the_last_panel_to_the_stitch(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            first = tmp / "cutscene_x_01.png"
            last = tmp / "cutscene_x_04.png"
            Image.new("RGB", (8, 8)).save(first)
            Image.new("RGB", (8, 8)).save(last)
            st = {"experience_world_id": "wA", "experience_cutscene_id": "c1",
                  "pending_cutscene": {"cutscene_id": "c1", "to_world": "wB", "arrival": True,
                                       "shots": [{"path": str(first)},
                                                 {"path": str(tmp / "missing.png")},
                                                 {"path": str(last)}]}}
            with mock.patch.object(experience_store, "get_experience", return_value=EXP), \
                 mock.patch.object(engine, "apply_experience_world",
                                   return_value={"from": WORLD_A, "to": WORLD_B, "transition": None}) as apply:
                info = engine.complete_cutscene(st, "s")
            self.assertEqual(info["kind"], "world")
            self.assertEqual(apply.call_args.kwargs["arrival_shot"], str(last))
            # the montage's OTHER panels ride along; the anchor is not repeated
            self.assertEqual(apply.call_args.kwargs["montage_refs"], [str(first)])
            self.assertIsNone(st["pending_cutscene"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _play(self, node_source, cur_world, pending, *, mood=None, body=None, dest="wB"):
        seen = {}

        def fake_resolve(session_id, **kw):
            seen["resolve"] = kw
            return None

        def fake_generate(plate, **kw):
            seen["generate"] = kw
            return {"shots": [], "duration_ms": 1000, "source": "x"}

        state = {"experience_world_id": cur_world, "pending_cutscene": pending}
        node = dict(CUT, source=node_source, mood=mood or CUT["mood"])
        with mock.patch.object(engine, "get_state", return_value=state), \
             mock.patch.object(engine, "_load_state", return_value=dict(state)), \
             mock.patch.object(engine, "_save_state"), \
             mock.patch.object(engine, "_sync_ambient_state"), \
             mock.patch.object(experience_store, "get_experience", return_value=EXP), \
             mock.patch.object(experience_store, "cutscene_by_id", return_value=node), \
             mock.patch.object(cutscene, "_outgoing_world_id", return_value=dest), \
             mock.patch.object(cutscene, "resolve_source_path", side_effect=fake_resolve), \
             mock.patch.object(cutscene, "generate_shots", side_effect=fake_generate), \
             mock.patch.object(cutscene, "environment_type", return_value="indoor-corridor"), \
             mock.patch.object(engine, "classify_setting", return_value="outdoor-industrial"), \
             mock.patch.object(game_identity, "place_summary", return_value="An open shipyard on the water"), \
             mock.patch.object(game_identity, "level_goal", return_value="the gate"):
            out = cutscene.play_for_session("s", {"cutscene_id": "c1", **(body or {})})
        return out, seen

    def test_an_arrival_on_the_default_source_is_the_first_photograph_of_that_world(self):
        """"Incoming plate" (the default) restaged the frame on screen — the
        place being left — under the destination's place lock. An arrival now
        draws the way the level's opening draws: no plate, toward the
        destination, from its own sheet."""
        out, seen = self._play("incoming", "wA",
                               {"cutscene_id": "c1", "to_world": "wB", "arrival": True},
                               body={"source_url": "/images/old_world_frame.png"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(seen["resolve"]["source_url"], "")
        self.assertEqual(seen["resolve"]["source"], "incoming")
        self.assertFalse(seen["resolve"]["allow_current"])
        self.assertEqual(seen["generate"]["plate_role"], "destination")
        # the destination's own place decides indoor/outdoor, not the history
        # of the World being left
        self.assertEqual(seen["generate"]["setting"], "outdoor-industrial")

    def test_an_authored_destination_frame_is_honoured_and_the_screen_is_not(self):
        """The client sends whatever is on screen as `source_url`, and
        resolve_source_path takes a source_url over everything — so even a
        cutscene authored "Destination World frame" was place-locked to the
        World being left."""
        out, seen = self._play("dest_world", "wA",
                               {"cutscene_id": "c1", "to_world": "wB", "arrival": True},
                               body={"source_url": "/images/old_world_frame.png"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(seen["resolve"]["source_url"], "")
        self.assertEqual(seen["resolve"]["source"], "dest_world")
        self.assertEqual(seen["resolve"]["dest_world_id"], "wB")

    def test_a_departure_restages_the_place_it_leaves(self):
        out, seen = self._play("incoming", "wA", {"cutscene_id": "c1", "to_world": "wB"},
                               mood="departure", body={"source_url": "/images/old_world_frame.png"})
        self.assertEqual(seen["resolve"]["source_url"], "/images/old_world_frame.png")
        self.assertEqual(seen["resolve"]["source"], "incoming")
        self.assertTrue(seen["resolve"]["allow_current"])
        self.assertFalse(out["ok"])            # no plate resolved, not an opening

    def test_an_arrival_without_a_plate_still_draws(self):
        # resolve returns None above; an arrival is an opening of that World.
        out, _ = self._play("incoming", "wA", {"cutscene_id": "c1", "to_world": "wB", "arrival": True})
        self.assertTrue(out["ok"])

    def test_a_same_world_cutscene_keeps_its_authored_plate(self):
        out, seen = self._play("incoming", "wA", {"cutscene_id": "c1"}, dest="wA")
        self.assertFalse(out["ok"])            # no plate, not an opening: refuses as before
        self.assertEqual(seen["resolve"]["source"], "incoming")
        self.assertTrue(seen["resolve"]["allow_current"])


class TestTheRunsProtagonistCrossesTheStitch(unittest.TestCase):

    def test_the_prior_cast_wins_over_the_destinations_sheet(self):
        prior = {"enabled": True, "name": "Isaac Clarke"}
        other = {"enabled": True, "name": "Jason Fleece"}
        specs = iter([{game_identity.CHARACTER_KEY: prior}, {game_identity.CHARACTER_KEY: other}])
        import prompts_store
        with mock.patch.object(game_identity, "character_enabled", return_value=True), \
             mock.patch.object(game_identity, "get_spec", side_effect=lambda: next(specs)), \
             mock.patch.object(worlds_store, "load_world", return_value={}), \
             mock.patch.object(prompts_store, "save_prompts_bulk") as save:
            self.assertTrue(engine._bind_world_prompts(WORLD_B))
        save.assert_called_once_with({game_identity.CHARACTER_KEY: prior})

    def test_a_matching_sheet_is_not_rewritten(self):
        prior = {"enabled": True, "name": "Isaac Clarke"}
        import prompts_store
        with mock.patch.object(game_identity, "character_enabled", return_value=True), \
             mock.patch.object(game_identity, "get_spec", return_value={game_identity.CHARACTER_KEY: dict(prior)}), \
             mock.patch.object(worlds_store, "load_world", return_value={}), \
             mock.patch.object(prompts_store, "save_prompts_bulk") as save:
            engine._bind_world_prompts(WORLD_B)
        save.assert_not_called()

    def test_a_world_without_a_slug_binds_nothing(self):
        with mock.patch.object(worlds_store, "load_world") as load:
            self.assertFalse(engine._bind_world_prompts({"id": "x", "slug": ""}))
        load.assert_not_called()


class TestABindReplacesTheLivePromptsItDoesNotMerge(unittest.TestCase):

    def test_keys_the_snapshot_lacks_come_from_the_factory_not_the_last_world(self):
        import prompts_store
        stored = {"world_initial_state": "shipyard bible", "player_character": {"enabled": False}}
        known = ["world_initial_state", "encounter_plate_anchor", "director_instructions",
                 "player_character"]
        with mock.patch.object(worlds_store, "_read_world", return_value={"prompts": stored, "name": "S"}), \
             mock.patch.object(prompts_store, "editable_keys", return_value=known), \
             mock.patch.object(prompts_store, "load_defaults",
                               return_value={"encounter_plate_anchor": "FACTORY ANCHOR",
                                             "director_instructions": "FACTORY DIRECTOR",
                                             "player_character": {"name": "Jason Fleece"}}), \
             mock.patch.object(worlds_store, "harness_doctrine_in", return_value=[]), \
             mock.patch.object(prompts_store, "save_prompts_bulk") as save:
            worlds_store.load_world("shipyard")
        written = save.call_args[0][0]
        self.assertEqual(written["encounter_plate_anchor"], "FACTORY ANCHOR")
        self.assertEqual(written["director_instructions"], "FACTORY DIRECTOR")
        # the cast is the run's, never filled from the factory
        self.assertEqual(written["player_character"], {"enabled": False})


class TestTheClientForgetsTheOldPlaceToo(unittest.TestCase):

    def test_a_world_transition_resets_the_case_file(self):
        js = (ROOT / "static" / "js" / "standalone.js").read_text(encoding="utf-8", errors="replace")
        body = js.split('case "world_transition":', 1)[1].split("return;", 1)[0]
        for call in ("Objectives.reset()", "Evidence.reset()", "Investigations.clear()",
                     "closeScan()", "state.scanPrewarm ="):
            self.assertIn(call, body, call)


if __name__ == "__main__":
    unittest.main(verbosity=2)
