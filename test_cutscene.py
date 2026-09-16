"""
Tests for Cutscene nodes: 4-shot montage from a plate.

Covers schema (experience_store), 2×2 panel extract, optical montage,
prompt language, and the engine hop World → Cutscene → World.

Never touches the network. Image work uses PIL fixtures.

Run: python -m unittest test_cutscene -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import cutscene
import experience_store as xs
import worlds_store as ws
import prompts_store as ps
import game_identity as gi
import engine


class _Isolated(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self._tmpdir.name)
        self._orig = {
            "exp": xs.EXPERIENCES_DIR,
            "sessions": xs.SESSIONS_DIR,
            "worlds": ws.WORLDS_DIR,
            "prompts": ps.PROMPTS_PATH,
            "defaults": ps.DEFAULTS_PATH,
            "refs": gi.REFERENCES_DIR,
            "gi_sessions": gi.SESSIONS_DIR,
        }
        xs.EXPERIENCES_DIR = tmp / "experiences"
        xs.SESSIONS_DIR = tmp / "sessions"
        ws.WORLDS_DIR = tmp / "worlds"
        ps.PROMPTS_PATH = tmp / "simulation_prompts.json"
        ps.DEFAULTS_PATH = tmp / "simulation_prompts.defaults.json"
        gi.REFERENCES_DIR = tmp / "references"
        gi.SESSIONS_DIR = tmp / "gi_sessions"
        gi.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "world_initial_state": "A quiet test place.",
            "action_consequence_instructions": "Return JSON.",
        }
        payload.update(gi.default_spec())
        for path in (ps.PROMPTS_PATH, ps.DEFAULTS_PATH):
            path.write_text(json.dumps(payload), encoding="utf-8")
        ps.PROMPTS._mtime = None
        ps.PROMPTS._last_check = 0.0
        ps.PROMPTS._reload(force=True)

    def tearDown(self):
        xs.EXPERIENCES_DIR = self._orig["exp"]
        xs.SESSIONS_DIR = self._orig["sessions"]
        ws.WORLDS_DIR = self._orig["worlds"]
        ps.PROMPTS_PATH = self._orig["prompts"]
        ps.DEFAULTS_PATH = self._orig["defaults"]
        gi.REFERENCES_DIR = self._orig["refs"]
        gi.SESSIONS_DIR = self._orig["gi_sessions"]
        ps.PROMPTS._mtime = None
        ps.PROMPTS._last_check = 0.0
        ps.PROMPTS._reload(force=True)
        self._tmpdir.cleanup()


class TestCutsceneSchema(_Isolated):
    def test_immediate_is_in_the_catalog(self):
        ids = [row["id"] for row in xs.condition_catalog()]
        self.assertIn("immediate", ids)
        self.assertTrue(xs.condition_met({"type": "immediate"}, world_turn_count=0))

    def test_add_cutscene_round_trip(self):
        exp = xs.add_cutscene("Crossing", mood="threshold", x=10, y=-20)
        self.assertEqual(len(exp["cutscenes"]), 1)
        c = exp["cutscenes"][0]
        self.assertEqual(c["name"], "Crossing")
        self.assertEqual(c["mood"], "threshold")
        self.assertEqual(c["x"], 10)
        again = xs.get_experience()
        self.assertEqual(xs.cutscene_by_id(again, c["id"])["name"], "Crossing")

    def test_world_to_cutscene_to_world_link(self):
        exp = xs.add_world("World 2")
        exp = xs.add_cutscene("The Gate")
        a = exp["worlds"][0]["id"]
        b = exp["worlds"][1]["id"]
        c = exp["cutscenes"][0]["id"]
        xs.add_transition(a, c, {"type": "turn_count", "turns": 2})
        xs.add_transition(c, b)
        exp = xs.get_experience()
        self.assertEqual(len(exp["transitions"]), 2)
        out = [t for t in exp["transitions"] if t["from"] == c][0]
        self.assertEqual(out["condition"]["type"], "immediate")
        hit = xs.matched_transition(exp, a, world_turn_count=2)
        self.assertEqual(hit["to"], c)
        leave = xs.matched_transition(exp, c, world_turn_count=0)
        self.assertEqual(leave["to"], b)

    def test_remove_cutscene_strips_edges(self):
        exp = xs.add_cutscene("Gone")
        a = exp["worlds"][0]["id"]
        c = exp["cutscenes"][0]["id"]
        xs.add_transition(a, c, {"type": "turn_count", "turns": 1})
        exp = xs.remove_cutscene(c)
        self.assertEqual(exp["cutscenes"], [])
        self.assertEqual(exp["transitions"], [])

    def test_rename_keeps_mood(self):
        exp = xs.add_cutscene("Draft")
        cid = exp["cutscenes"][0]["id"]
        exp = xs.rename_cutscene(cid, "The Descent", mood="aftermath",
                                 shot_brief="hold the wound")
        c = xs.cutscene_by_id(exp, cid)
        self.assertEqual(c["name"], "The Descent")
        self.assertEqual(c["mood"], "aftermath")
        self.assertEqual(c["shot_brief"], "hold the wound")

    def test_unknown_mood_falls_back(self):
        exp = xs.save_experience({
            "worlds": [{"id": "w1", "name": "W", "slug": ""}],
            "cutscenes": [{"id": "c1", "name": "X", "mood": "nope"}],
            "start_world": "w1",
        })
        self.assertEqual(exp["cutscenes"][0]["mood"], "threshold")


class TestCutsceneImages(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _grid(self, cols=2, rows=2, cell=32):
        im = Image.new("RGB", (cell * cols, cell * rows), (0, 0, 0))
        colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
        for r in range(rows):
            for c in range(cols):
                i = r * cols + c
                tile = Image.new("RGB", (cell, cell), colors[i % 4])
                im.paste(tile, (c * cell, r * cell))
        path = self.dir / "grid.png"
        im.save(path)
        return path

    def test_extract_2x2(self):
        grid = self._grid()
        panels = cutscene.extract_grid_panels(grid, self.dir / "out", stem="shot")
        self.assertEqual(len(panels), 4)
        px = Image.open(panels[0]).getpixel((2, 2))
        self.assertEqual(px[0], 255)
        self.assertEqual(px[1], 0)

    def test_optical_montage_writes_four(self):
        plate = self.dir / "plate.png"
        Image.new("RGB", (400, 300), (80, 40, 20)).save(plate)
        shots = cutscene.optical_montage(plate, self.dir / "shots", mood="threshold")
        self.assertEqual(len(shots), 4)
        self.assertEqual(shots[0]["camera"], "wide")
        self.assertEqual(shots[3]["camera"], "reveal")
        for s in shots:
            self.assertTrue(Path(s["path"]).exists())

    def test_prompt_names_four_panels(self):
        text = cutscene.build_cutscene_prompt("threshold", name="The Gate")
        self.assertIn("2×2", text)
        self.assertIn("Panel 1", text)
        self.assertIn("Panel 4", text)
        self.assertIn("The Gate", text)
        self.assertIn("PLACE LOCK", text)

    def test_generate_shots_offline(self):
        plate = self.dir / "plate.png"
        Image.new("RGB", (240, 180), (30, 30, 30)).save(plate)
        out = cutscene.generate_shots(
            plate, session_id="t", mood="arrival", offline=True,
            output_dir=self.dir / "gen",
        )
        self.assertEqual(out["source"], "optical")
        self.assertEqual(len(out["shots"]), 4)
        self.assertEqual(out["mood"], "arrival")


class TestEngineCutsceneHop(_Isolated):
    def _graph(self):
        exp = xs.add_world("World 2")
        exp = xs.add_cutscene("Bridge")
        a, b = exp["worlds"][0], exp["worlds"][1]
        c = exp["cutscenes"][0]
        xs.add_transition(a["id"], c["id"], {"type": "turn_count", "turns": 1})
        xs.add_transition(c["id"], b["id"])
        return xs.get_experience(), a, b, c

    def test_start_can_be_a_cutscene(self):
        exp = xs.add_world("World 2")
        exp = xs.add_cutscene("Opening")
        b = exp["worlds"][1]
        c = exp["cutscenes"][0]
        xs.add_transition(c["id"], b["id"])
        exp = xs.set_start_world(c["id"])
        self.assertEqual(exp["start_world"], c["id"])
        again = xs.get_experience()
        self.assertEqual(again["start_world"], c["id"])
        land = xs.landing_world(again)
        self.assertEqual(land["id"], b["id"])

    def test_cutscene_into_start_world_is_the_opening(self):
        exp = xs.add_cutscene("Opening")
        c = exp["cutscenes"][0]
        a = exp["worlds"][0]
        self.assertEqual(exp["start_world"], a["id"])
        xs.add_transition(c["id"], a["id"])
        exp = xs.get_experience()
        self.assertEqual(exp["start_world"], c["id"])
        self.assertEqual(xs.opening_cutscene(exp)["id"], c["id"])
        state = engine.apply_experience_start({}, "t")
        self.assertEqual(state["experience_cutscene_id"], c["id"])
        self.assertEqual(state["experience_world_id"], a["id"])

    def test_world_into_cutscene_does_not_steal_start(self):
        exp = xs.add_world("World 2")
        exp = xs.add_cutscene("Bridge")
        a, b = exp["worlds"][0], exp["worlds"][1]
        c = exp["cutscenes"][0]
        xs.add_transition(a["id"], c["id"], {"type": "turn_count", "turns": 2})
        xs.add_transition(c["id"], b["id"])
        exp = xs.get_experience()
        self.assertEqual(exp["start_world"], a["id"])
        self.assertIsNone(xs.opening_cutscene(exp))

    def test_apply_start_cutscene_stamps_pending(self):
        exp = xs.add_world("World 2")
        exp = xs.add_cutscene("Opening")
        b = exp["worlds"][1]
        c = exp["cutscenes"][0]
        xs.add_transition(c["id"], b["id"])
        xs.set_start_world(c["id"])
        state = engine.apply_experience_start({}, "t")
        self.assertEqual(state["experience_cutscene_id"], c["id"])
        self.assertEqual(state["experience_world_id"], b["id"])
        self.assertEqual((state.get("pending_cutscene") or {}).get("cutscene_id"), c["id"])

    def test_tick_lands_on_cutscene_not_world(self):
        exp, a, b, c = self._graph()
        state = {
            "experience_id": exp["id"],
            "experience_world_id": a["id"],
            "world_turn_count": 0,
            "world_prompt": "old",
            "player_state": {"alive": True},
        }
        info = engine._tick_world_and_maybe_transition(state, "t", player_alive=True)
        self.assertIsNotNone(info)
        self.assertEqual(info["kind"], "cutscene")
        self.assertEqual(state["experience_cutscene_id"], c["id"])
        self.assertEqual(state["experience_world_id"], a["id"])
        self.assertEqual((state.get("pending_cutscene") or {}).get("status"), "pending")

    def test_complete_follows_immediate_to_world(self):
        exp, a, b, c = self._graph()
        state = {
            "experience_id": exp["id"],
            "experience_world_id": a["id"],
            "experience_cutscene_id": c["id"],
            "pending_cutscene": {"cutscene_id": c["id"], "graph": True},
            "world_turn_count": 0,
            "world_prompt": "old",
        }
        info = engine.complete_cutscene(state, "t")
        self.assertIsNotNone(info)
        self.assertEqual(info["kind"], "world")
        self.assertEqual(state["experience_world_id"], b["id"])
        self.assertEqual(state["experience_cutscene_id"], "")
        self.assertTrue(state.get("pending_world_transition"))

    def test_tick_while_on_cutscene_does_not_rehop(self):
        exp, a, b, c = self._graph()
        state = {
            "experience_id": exp["id"],
            "experience_world_id": a["id"],
            "experience_cutscene_id": c["id"],
            "pending_cutscene": {"cutscene_id": c["id"], "graph": True},
            "world_turn_count": 8,
            "world_prompt": "old",
        }
        info = engine._tick_world_and_maybe_transition(state, "t", player_alive=True)
        self.assertIsNone(info)
        self.assertEqual(state["experience_cutscene_id"], c["id"])
        self.assertEqual(state["experience_world_id"], a["id"])

    def test_complete_missing_dest_resumes(self):
        exp = xs.add_cutscene("Bridge")
        c = exp["cutscenes"][0]
        state = {
            "experience_cutscene_id": c["id"],
            "pending_cutscene": {"cutscene_id": c["id"], "to_world": "w-missing"},
            "experience_world_id": exp["worlds"][0]["id"],
        }
        info = engine.complete_cutscene(state, "t")
        self.assertEqual(info["kind"], "resume")
        self.assertEqual(state["experience_cutscene_id"], "")

    def test_complete_without_edge_resumes(self):
        exp = xs.add_cutscene("Loose")
        c = exp["cutscenes"][0]
        state = {
            "experience_cutscene_id": c["id"],
            "pending_cutscene": {"cutscene_id": c["id"]},
            "experience_world_id": exp["worlds"][0]["id"],
        }
        info = engine.complete_cutscene(state, "t")
        self.assertEqual(info["kind"], "resume")
        self.assertEqual(state["experience_cutscene_id"], "")


class TestApproachMood(_Isolated):
    """The level's opening: arriving, with the goal still out of reach."""

    def _level(self, **fields):
        base = {"name": "The Kettle Yard",
                "summary": "A flooded shipbreaking yard on a tidal flat.",
                "landmarks": "The listing tanker, the crane gantry."}
        base.update(fields)
        gi.save_spec({gi.SETTING_KEY: base})

    def test_goal_is_a_level_field(self):
        self._level(goal="The pump house with the red door.")
        self.assertEqual(gi.level_goal(), "The pump house with the red door.")
        self.assertEqual(gi.establishing_shot()["goal"],
                         "The pump house with the red door.")

    def test_an_unauthored_goal_falls_back_to_one_landmark(self):
        """The montage always needs something to put on the horizon.

        The landmark list describes the whole space, so only the FIRST entry is
        a destination — walking toward all three at once is not a shot.
        """
        self._level()
        self.assertEqual(gi.level_goal(), "The listing tanker")
        self.assertEqual(gi.level_goal(fallback=False), "")

    def test_establishing_shot_never_returns_none(self):
        """opening_shot() may decline; this one cannot.

        Every level opens on a montage, including one nobody has written yet.
        """
        gi.save_spec({gi.SETTING_KEY: dict(gi.SETTING_DEFAULTS)})
        shot = gi.establishing_shot()
        self.assertTrue(shot["title"])
        self.assertTrue(shot["approach"])

    def test_the_plate_is_the_destination_not_the_camera_position(self):
        """The plate is the world these photographs were taken in, not the shot.

        Previously this was expressed as "NOT ARRIVED YET / FURTHER BACK", which
        was true when all four panels were one walk-in toward the plate. The
        opening is a cold open now — one of its panels is a macro detail, which is
        nearer than the plate, not further back — so the guarantee is stated as
        "don't reproduce the reference's framing" instead. The lesson is the same
        one: the anchor wording had the model redraw the plate four times from
        where it already stood.
        """
        text = cutscene.build_cutscene_prompt(
            "approach", goal="the pump house", plate_role="destination")
        self.assertIn("do not reproduce the reference's framing", text)
        self.assertNotIn("the current photograph of this exact place", text)

    def test_the_opening_is_not_one_continuous_shot(self):
        """Four angles on one moment is right for every mood except this one.

        Said of the opening it produced the same walk-in four times with the
        figure sliding around inside it, which is what made the montage painful.
        """
        text = cutscene.build_cutscene_prompt(
            "approach", goal="the pump house", plate_role="destination")
        self.assertIn("NOT THE SAME MOMENT", text)
        self.assertNotIn("on the SAME moment", text)

    def test_no_generated_panel_contains_a_person(self):
        """The montage does not draw the character, and the reasons are layered.

        Asked for, the model first drew it badly: a front-facing portrait, then a
        side-on medium, then a stranger entirely when no character plate was
        passed. With an identity lock it finally drew the brief correctly — Kelsey
        from behind, small in a wide, the goal readable beyond her — and the shot
        STILL failed, because a small centred figure seen from the back in a
        composition unlike any the game then uses does not tell the player who they
        are. The plate is the gameplay composition, so the montage ends on that.
        """
        for _cam, label, brief in cutscene.MOODS["approach"]["shots"]:
            self.assertIn("NO PEOPLE", brief, label)
        text = cutscene.build_cutscene_prompt("approach", plate_role="destination")
        self.assertIn("ALL FOUR PANELS ARE COMPLETELY EMPTY OF PEOPLE", text)
        self.assertNotIn("THE CHARACTER", text)

    def test_the_opening_is_not_handed_a_character_plate(self):
        """Holding a portrait argues against "nobody is in frame".

        The identity lock belongs to montages that draw a person — an anchor
        restages a beat the player is already in — not to an opening whose every
        panel is empty.
        """
        plate = Path(self._tmpdir.name) / "id_plate.png"
        Image.new("RGB", (400, 300), (60, 60, 60)).save(plate)
        calls = []

        def fake_img2img(**kwargs):
            calls.append(kwargs)
            return None      # force the optical fallback; we only want the call

        def run(mood, role):
            with patch.object(engine, "_get_image_dir",
                              return_value=str(Path(self._tmpdir.name) / "idshots")), \
                 patch.object(engine, "IMAGE_ENABLED", True), \
                 patch("gemini_image_utils.generate_gemini_img2img", fake_img2img), \
                 patch("gemini_image_utils.GEMINI_API_KEY", "test-key"), \
                 patch("game_identity.identity_reference_paths",
                       return_value=[str(plate)]):
                cutscene.generate_shots(plate, session_id="t", mood=mood,
                                        plate_role=role)
            return calls[-1]

        opening = run("approach", "destination")
        self.assertFalse(opening.get("identity_paths"),
                         "an all-empty montage must not be handed a portrait")

        anchor = run("encounter", "anchor")
        self.assertEqual(anchor.get("identity_paths"), [str(plate)],
                         "a restage must hold the cast it is restaging")

    def test_the_opening_is_graded_for_dread_not_for_landscape_photography(self):
        """4K came back as lavender-dusk travel photography.

        The palette already said 1993 and rust; nothing said anything about mood,
        so the model defaulted to a postcard.
        """
        text = cutscene.build_cutscene_prompt("approach", plate_role="destination")
        self.assertIn("THIS IS NOT A POSTCARD", text)
        self.assertIn("No lavender or candy dusk", text)
        # And an ordinary restage must not be dragged into it.
        anchor = cutscene.build_cutscene_prompt("encounter", plate_role="anchor")
        self.assertNotIn("THIS IS NOT A POSTCARD", anchor)

    def test_a_cropped_body_part_counts_as_a_person(self):
        """A render answered "nobody in frame" with a pair of legs and boots.

        Naming people at all, even to say they are gone, puts one in the shot, so
        no brief mentions them and the ban names the parts that turned up.
        """
        text = cutscene.build_cutscene_prompt("approach", plate_role="destination")
        for part in ("no legs", "no feet", "no boots"):
            self.assertIn(part, text)
        self.assertIn("A cropped body part is still a person", text)
        # And no brief may describe its subject in terms of people at all.
        for _cam, label, brief in cutscene.MOODS["approach"]["shots"]:
            self.assertNotIn("people were here", brief.lower(), label)

    def test_each_shot_names_the_cell_it_belongs_in(self):
        """Ordinals were not binding, and the last cell is load-bearing.

        A render obeyed all four briefs and put them in the wrong cells: the
        character in the top-right, a signage macro in the bottom-right. The
        bottom-right panel is the one sliced out as the frame the run continues
        from, so that render would have opened the game on a close-up of a sign.
        """
        self.assertEqual(cutscene._cell_name(1), "TOP-LEFT")
        self.assertEqual(cutscene._cell_name(2), "TOP-RIGHT")
        self.assertEqual(cutscene._cell_name(3), "BOTTOM-LEFT")
        self.assertEqual(cutscene._cell_name(4), "BOTTOM-RIGHT")

        text = cutscene.build_cutscene_prompt("approach", plate_role="destination")
        for cell in ("TOP-LEFT", "TOP-RIGHT", "BOTTOM-LEFT", "BOTTOM-RIGHT"):
            self.assertIn(cell, text)
        self.assertIn("PANEL PLACEMENT IS NOT INTERCHANGEABLE", text)

    def test_the_montage_is_rendered_big_enough_to_cut_up(self):
        """Every panel is a quarter of one generation.

        At the play setting (1K) that is 672x376 a panel, and the unpeopled
        establishing shots came back soft with invented drifting detail. The last
        panel is also the img2img reference for the whole run, so its softness
        compounds into every later frame.
        """
        import ai_provider_manager as apm
        sizes = (apm.find_model("image", cutscene.GRID_RENDER_MODEL) or {}).get("sizes") or []
        self.assertIn(cutscene.GRID_RENDER_SIZE, sizes,
                      f"{cutscene.GRID_RENDER_MODEL} cannot render "
                      f"{cutscene.GRID_RENDER_SIZE}")
        self.assertNotEqual(cutscene.GRID_RENDER_SIZE, "1K")

    def test_the_goal_is_named_and_kept_unreached(self):
        text = cutscene.build_cutscene_prompt("approach", goal="the pump house")
        self.assertIn("the pump house", text)
        self.assertIn("NOT REACHED", text)

    def test_an_approach_shot_never_names_the_outdoors(self):
        """An indoor level gets an approach too — down the space, not outside it.

        The prompt adds "stay in this same room" from the environment label, so
        a shot brief saying "seen from OUTSIDE" contradicts it in the same
        payload and the model picks one.
        """
        for _cam, _label, brief in cutscene.MOODS["approach"]["shots"]:
            low = brief.lower()
            self.assertNotIn("outside", low, brief)
            self.assertNotIn("open ground", low, brief)
            self.assertNotIn("sky", low, brief)

    def test_the_plate_beat_uses_a_url_the_server_can_actually_serve(self):
        """The 404 that opened the run on black.

        A World frame lives in worlds/, so mapping its path through the session
        image helper produced /images/world.frame.png — which the /images route
        cannot resolve. That url became current_image_url and was handed to turn
        one, so the run opened black while the client retried the missing file
        four times. The staged url is the one the route serves.
        """
        plate = Path(self._tmpdir.name) / "url_plate.png"
        Image.new("RGB", (480, 360), (70, 60, 45)).save(plate)
        rec = {"url": "/api/worlds/world/frame?v=abc",
               "path": str(plate), "status": "ready"}
        state = {"world_prompt": "The yard.", "tape_frames": [], "feed_log": []}
        items = [{"type": "narrative_event", "content": "Ahead of you: the yard."},
                 {"type": "player_choice_prompt", "id": 3, "choices": [{"text": "Go"}]}]
        with patch.object(engine, "INTRO_CUTSCENE", True), \
             patch.object(engine, "IMAGE_ENABLED", True):
            staged = engine._stage_opening_montage("t", state, items, rec)

        out_dir = Path(self._tmpdir.name) / "shots_url"
        with patch.object(engine, "get_state", return_value=state), \
             patch.object(engine, "_load_state", return_value=state), \
             patch.object(engine, "_save_state"), \
             patch.object(engine, "_get_image_dir", return_value=str(out_dir)), \
             patch.object(engine, "_to_web_image_url",
                          side_effect=lambda p, s: "/images/" + Path(p).name):
            payload = cutscene.play_for_session("t", {
                "cutscene_id": staged[-1]["metadata"]["cutscene_id"],
                "mood": "approach", "source_url": "", "offline": True,
            })

        last = payload["shots"][-1]
        self.assertEqual(len(payload["shots"]), 5, "four panels plus the plate")
        self.assertEqual(last["camera"], "threshold")
        self.assertEqual(last["url"], rec["url"],
                         "the final beat must use the plate's servable url")
        self.assertNotIn("/images/", last["url"])

    def test_generate_shots_returns_only_what_it_drew(self):
        """The plate beat is added a layer up, in play_for_session.

        It has to be: a World frame lives in worlds/, not in the session's
        images/, so the path-to-url helper here maps it to /images/world.frame.png
        and that 404s. Because the beat's url becomes current_image_url, that 404
        was handed to turn one and the run opened on black with the client
        retrying the missing file. Only play_for_session knows the servable url.
        """
        plate = Path(self._tmpdir.name) / "plate_end.png"
        Image.new("RGB", (400, 300), (80, 40, 20)).save(plate)
        out_dir = Path(self._tmpdir.name) / "shots_end"
        with patch.object(engine, "_get_image_dir", return_value=str(out_dir)), \
             patch.object(engine, "_to_web_image_url",
                          side_effect=lambda p, s: "/images/" + Path(p).name):
            result = cutscene.generate_shots(
                plate, session_id="t", mood="approach",
                plate_role="destination", offline=True)
        self.assertEqual(len(result["shots"]), 4)

    def test_shots_hold_far_longer_than_flipbook_frames(self):
        """The cutscene/flipbook split, as a number.

        Four cameras on one moment are photographs to look at; a flipbook panel
        is one frame of motion. Holding these for a flipbook's frame time is
        what made the montage read as a slideshow on fast-forward.
        """
        import flipbook
        self.assertGreater(cutscene.HOLD_MS, 4 * flipbook.DEFAULT_FRAME_MS)


class TestOpeningMontage(_Isolated):
    """Reset opens on the montage; the cached plate becomes its reference."""

    def _rec(self):
        return {"url": "/api/worlds/yard/frame?v=1", "path": "", "status": "ready"}

    def test_a_warm_plate_opens_on_a_montage(self):
        with patch.object(engine, "INTRO_CUTSCENE", True), \
             patch.object(engine, "IMAGE_ENABLED", True):
            self.assertTrue(engine._open_on_montage({}, self._rec()))

    def test_no_plate_means_no_montage(self):
        """With nothing to establish toward, fall back to rendering an opening.

        The montage is drawn FROM the cached frame, so a cold World has to keep
        the old behaviour rather than open on four shots of nowhere.
        """
        with patch.object(engine, "INTRO_CUTSCENE", True), \
             patch.object(engine, "IMAGE_ENABLED", True):
            self.assertFalse(engine._open_on_montage({}, {}))

    def test_an_authored_opening_cutscene_wins(self):
        """A Cutscene node on the start of the graph is somebody's decision."""
        with patch.object(engine, "INTRO_CUTSCENE", True), \
             patch.object(engine, "IMAGE_ENABLED", True):
            self.assertFalse(
                engine._open_on_montage({"experience_cutscene_id": "c1"}, self._rec()))

    def test_text_only_runs_never_open_on_a_montage(self):
        with patch.object(engine, "INTRO_CUTSCENE", True), \
             patch.object(engine, "IMAGE_ENABLED", False):
            self.assertFalse(engine._open_on_montage({}, self._rec()))

    def test_staging_parks_the_slate_and_hides_the_plate(self):
        state = {}
        items = [{"type": "narrative_event", "content": "You are almost there."},
                 {"type": "player_choice_prompt", "choices": [{"text": "Go in"}]}]
        out = engine._stage_opening_montage("t", state, items, self._rec())
        kinds = [it["type"] for it in out]
        # Verbs under a montage ask the player to act during a beat they are
        # meant to watch.
        self.assertNotIn("player_choice_prompt", kinds)
        self.assertIn("cutscene", kinds)
        self.assertEqual(state["pending_opening_choices"]["type"],
                         "player_choice_prompt")
        # The plate is a reference, never a scene the player looks at.
        self.assertNotIn("scene_image", kinds)
        self.assertEqual(state["current_image_url"], self._rec()["url"])
        pending = state["pending_cutscene"]
        self.assertTrue(pending["opening"])
        self.assertEqual(pending["mood"], "approach")

    def test_the_plate_stays_on_the_server_as_a_file(self):
        """A World frame's URL is /api/worlds/<slug>/frame, which
        _resolve_image_path cannot turn back into a path — it only knows
        /images/<name> and absolute paths. So a plate sent out to the browser and
        echoed back resolves to nothing and the montage fails to develop.
        """
        state = {}
        out = engine._stage_opening_montage("t", state, [], self._rec())
        self.assertEqual(state["pending_cutscene"]["source_path"],
                         self._rec()["path"])
        self.assertNotIn("source_url", out[-1]["metadata"])

    def test_the_feed_item_carries_the_card_copy(self):
        state = {}
        out = engine._stage_opening_montage("t", state, [], self._rec())
        meta = out[-1]["metadata"]
        self.assertTrue(meta["opening"])
        self.assertEqual(meta["mood"], "approach")
        self.assertEqual(meta["duration_ms"], int(cutscene.HOLD_MS))

    def test_generating_shots_keeps_the_opening_stamp(self):
        """play_for_session rebuilds pending_cutscene; it must not wipe this.

        Reset stamps the fields only reset knows — that this IS the opening, and
        the parked slate it owns. Losing them leaves the hand-off unable to tell
        an opening montage from any other one.
        """
        src = Path(cutscene.__file__).read_text(encoding="utf-8")
        body = src.split("def play_for_session", 1)[1]
        self.assertIn("**keep,", body)
        self.assertIn('prev.get("cutscene_id") == play_id', body)

    def test_the_handoff_serves_the_parked_slate(self):
        slate = {"type": "player_choice_prompt", "id": 9,
                 "choices": [{"text": "Cross the yard"}]}
        st = {"pending_cutscene": {"opening": True, "shots": [
                  {"url": "/images/a.png", "path": ""},
                  {"url": "/images/d.png", "path": ""}]},
              "pending_opening_choices": slate, "feed_log": [], "tape_frames": []}
        with patch.object(engine, "_load_history", return_value=[{"choice": "x"}]):
            out = engine._finish_opening_montage(st, "t")
        self.assertEqual(out["choices"], slate)
        self.assertNotIn("pending_opening_choices", st)
        self.assertIsNone(st["pending_cutscene"])

    def test_the_run_plays_from_the_last_shot_not_the_plate(self):
        """The frame the montage ENDED on is where the player is standing.

        Leaving current_image_url on the plate would make turn 1 continue from a
        composition the montage walked away from and the player never saw held.
        """
        st = {"pending_cutscene": {"opening": True, "shots": [
                  {"url": "/images/first.png", "path": ""},
                  {"url": "/images/last.png", "path": ""}]},
              "current_image_url": "/api/worlds/yard/frame?v=1",
              "feed_log": [], "tape_frames": []}
        with patch.object(engine, "_load_history", return_value=[{"choice": "x"}]):
            out = engine._finish_opening_montage(st, "t")
        self.assertEqual(st["current_image_url"], "/images/last.png")
        self.assertEqual(out["image_url"], "/images/last.png")

    def test_a_montage_that_never_developed_still_hands_over(self):
        """Esc during "developing…", or a failed generation. The run must open.

        Without a slate here the choice bar is empty and the only way out of the
        run is knowing to reload.
        """
        st = {"pending_cutscene": {"opening": True, "shots": []},
              "current_image_url": "/api/worlds/yard/frame?v=1",
              "feed_log": [], "tape_frames": []}
        with patch.object(engine, "_load_history", return_value=[{"choice": "x"}]):
            out = engine._finish_opening_montage(st, "t")
        self.assertEqual(out["choices"]["type"], "player_choice_prompt")
        self.assertTrue(out["choices"]["choices"])
        self.assertEqual(st["current_image_url"], "/api/worlds/yard/frame?v=1")


class TestOpeningBeatEndToEnd(_Isolated):
    """Reset → montage → hand-off, through the real functions, no network.

    Generation goes down cutscene.py's optical path (four cinematic crops of the
    plate), which is the offline half of the same code the paid path uses. What
    this pins is the ORDER the player experiences: something to read, a montage
    with no verbs under it, then verbs over the frame the montage ended on.
    """

    def setUp(self):
        super().setUp()
        import world_frames as wf
        self._wf = wf
        self._wf_orig = wf.WORLDS_DIR if hasattr(wf, "WORLDS_DIR") else None
        gi.save_spec({gi.SETTING_KEY: {
            "name": "The Kettle Yard",
            "summary": "A flooded shipbreaking yard on a tidal flat.",
            "goal": "The pump house with the red door, where the manifests are.",
        }})
        self.plate = Path(self._tmpdir.name) / "plate.png"
        Image.new("RGB", (480, 360), (70, 60, 45)).save(self.plate)
        self.rec = {"url": "/api/worlds/yard/frame?v=1",
                    "path": str(self.plate), "status": "ready"}

    def test_the_player_reads_then_watches_then_acts(self):
        state = {"world_prompt": "The yard.", "tape_frames": [], "feed_log": []}
        items = [
            {"type": "narrative_event", "content": "Ahead of you: The Kettle Yard."},
            {"type": "player_choice_prompt", "id": 7,
             "choices": [{"text": "Cross the yard"}]},
        ]
        with patch.object(engine, "INTRO_CUTSCENE", True), \
             patch.object(engine, "IMAGE_ENABLED", True):
            self.assertTrue(engine._open_on_montage(state, self.rec))
            staged = engine._stage_opening_montage("t", state, items, self.rec)

        self.assertEqual([it["type"] for it in staged],
                         ["narrative_event", "cutscene"])
        meta = staged[-1]["metadata"]
        self.assertIn("pump house", meta["goal"])
        self.assertEqual(meta["name"], "The Kettle Yard")

        # The client's own call. It sends no plate — at the top of a level there
        # is nothing on screen to send — so this also proves the server finds the
        # staged one.
        out_dir = Path(self._tmpdir.name) / "shots"
        with patch.object(engine, "get_state", return_value=state), \
             patch.object(engine, "_load_state", return_value=state), \
             patch.object(engine, "_save_state"), \
             patch.object(engine, "_get_image_dir", return_value=str(out_dir)), \
             patch.object(engine, "_to_web_image_url",
                          side_effect=lambda p, s: "/images/" + Path(p).name):
            payload = cutscene.play_for_session("t", {
                "cutscene_id": meta["cutscene_id"],
                "mood": meta["mood"],
                "name": meta["name"],
                "source_url": "",
                "offline": True,
            })
        self.assertTrue(payload["ok"])
        # Four generated panels, then the plate as the fifth and final beat —
        # which the assertion further down about done["image_url"] then proves:
        # the run continues from the plate, exactly.
        self.assertEqual(len(payload["shots"]), 5)
        self.assertEqual(payload["duration_ms"], int(cutscene.HOLD_MS))
        # play_for_session merges onto what reset stamped rather than replacing
        # it, or the hand-off can't tell this is the opening.
        self.assertTrue(state["pending_cutscene"]["opening"])
        state["pending_cutscene"]["shots"] = payload["shots"]

        with patch.object(engine, "_load_history", return_value=[{"choice": "x"}]):
            done = engine._finish_opening_montage(state, "t")

        # Verbs are back — the slate written for this level, not "Look around" —
        # over the frame the montage ended on. Its id is re-stamped on append,
        # which is what it should be: the client asks the feed for id > since_id,
        # and an item created at reset but appended after the montage would be
        # filtered out forever if it kept its original id (see _feed_append).
        self.assertEqual(done["choices"]["choices"], [{"text": "Cross the yard"}])
        self.assertGreater(done["choices"]["id"], staged[-1]["id"])
        self.assertEqual(done["image_url"], payload["shots"][-1]["url"])
        self.assertEqual(state["current_image_url"], payload["shots"][-1]["url"])
        self.assertIsNone(state["pending_cutscene"])
        self.assertEqual([it["type"] for it in state["feed_log"]],
                         ["player_choice_prompt"])

    def test_the_prompt_the_montage_is_drawn_from_names_the_goal(self):
        """What the author typed into the Level sheet has to reach the render.

        The goal is the only reason the establishing shots have somewhere to
        point, so a goal that stops at the sheet is the whole feature missing.
        """
        text = cutscene.build_cutscene_prompt(
            "approach", name="The Kettle Yard", goal=gi.level_goal(),
            plate_role="destination")
        self.assertIn("pump house", text)
        self.assertIn("NOT REACHED", text)


class TestEnvironmentLock(_Isolated):
    def test_the_indoor_outdoor_lock_reads_what_vision_recorded(self):
        """This clause never fired: it was read from setting_reference.setting,
        a field the Level sheet has never had, so it was always "".
        """
        with patch.object(engine, "_load_history",
                          return_value=[{"setting_type": "indoor-corridor"}]):
            self.assertEqual(cutscene.environment_type("t"), "indoor-corridor")
        text = cutscene.build_cutscene_prompt("approach", setting="indoor-corridor")
        self.assertIn("INDOORS", text)

    def test_the_lock_falls_back_to_the_render_base(self):
        with patch.object(engine, "_load_history", return_value=[]), \
             patch.object(engine, "_load_state",
                          return_value={"current_render_base": "x"}), \
             patch.object(engine, "classify_setting", return_value="outdoor-desert"):
            self.assertEqual(cutscene.environment_type("t"), "outdoor-desert")


if __name__ == "__main__":
    unittest.main()
