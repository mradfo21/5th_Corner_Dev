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
import threading
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

    def test_a_shotlist_replaces_the_subjects_and_keeps_the_roles(self):
        """The compositions were good and the ideas were not.

        Four fixed generic briefs — widest view, macro, architectural wide,
        leftovers — make handsome inert frames, because nothing in them is about
        this story. The world bible is 9,000 words with a real mystery in it and
        the opening had never read a line. Subjects now come from the lore; the
        roles stay, because the roles are what made the frames good.
        """
        subjects = [
            "A hand-painted evacuation notice bolted over a company sign.",
            "A dosimeter badge clipped to a nail, its window gone black.",
            "A row of shower stalls open to the sky, tiles still numbered.",
            "Boots and coveralls left in a heap where they were stepped out of.",
        ]
        text = cutscene.build_cutscene_prompt(
            "approach", plate_role="destination", shotlist=subjects)

        for i, subject in enumerate(subjects, start=1):
            self.assertIn(subject, text, f"subject {i} missing")
            cell = cutscene._cell_name(i)
            # Subject and its cell have to be in the same instruction.
            panel = text[text.index(f"Panel {i} — "):]
            panel = panel[:panel.index("NO PEOPLE") + 9]
            self.assertIn(cell, panel, f"panel {i} lost its cell")
            self.assertIn(subject, panel, f"panel {i} lost its subject")

        # The framing roles survive, so four lore subjects do not all come back
        # as the same wide.
        for phrase in ("widest view the place affords", "tight, patient macro",
                       "static, symmetrical, frontal wide", "low and close on the ground"):
            self.assertIn(phrase, text, phrase)

        # And no shotlist means the static briefs still run.
        plain = cutscene.build_cutscene_prompt("approach", plate_role="destination")
        self.assertIn("The widest, emptiest view this place affords", plain)
        self.assertNotIn(subjects[0], plain)

    def test_the_shotlist_asks_for_evidence_not_atmosphere(self):
        """What makes the difference is that each shot is a physical thing."""
        text = cutscene.SHOTLIST_INSTRUCTIONS
        self.assertIn("NOBODY is in any of these photographs", text)
        self.assertIn("no glowing anomalies", text)
        self.assertIn("DIFFERENT subject at a DIFFERENT scale", text)
        # Four roles, matching the four panels.
        for role in ("THE WIDEST VIEW", "A MACRO DETAIL", "A BUILT THING",
                     "WHAT WAS LEFT BEHIND"):
            self.assertIn(role, text, role)

    def test_the_widest_shot_has_to_show_what_we_came_for(self):
        """The goal was excluded twice over — "do not name the goal outright"
        here, and "it does not have to appear in every panel" in the grid
        prompt — so it appeared in none of them. A run told it was walking
        toward an extraction spire was shown a fence, a padlock, a trailer and
        some badges. The montage exists to make the player want to walk
        somewhere; it was never showing them where.
        """
        text = cutscene.SHOTLIST_INSTRUCTIONS
        self.assertIn("WITH THE GOAL ON THE SKYLINE", text)
        self.assertNotIn("Do not name the goal outright", text)
        # Still unreached: showing up close is the other way to kill the hook.
        self.assertIn("Do not show the goal REACHED", text)

    def test_the_grid_prompt_requires_it_in_the_widest_panel(self):
        text = cutscene.build_cutscene_prompt(
            "approach", plate_role="destination", has_reference=False,
            goal="The Extraction Spire over the basin")
        self.assertIn("The Extraction Spire over the basin", text)
        self.assertIn("IT MUST BE VISIBLE, ON THE HORIZON, IN THE WIDEST PANEL", text)
        self.assertNotIn("It does not have to appear in every panel", text)
        self.assertIn("NOT REACHED", text)

    def test_the_first_playable_frame_keeps_it_on_the_horizon(self):
        """That frame is img2img'd from the montage's widest panel, so the goal
        arrives for free — as long as nothing drops it. The block asks for
        something unreadable approaching in the far distance too, and given two
        things for one horizon the model kept the one it was told about."""
        for block in (engine._flipbook_establishing_block(4),
                      engine._flipbook_establishing_block(single=True)):
            self.assertIn("WHAT IS ON THE HORIZON STAYS ON THE HORIZON", block)
            self.assertIn("do not replace it with weather", block.lower())

    def test_a_world_with_no_bible_falls_back_instead_of_guessing(self):
        """An empty lore slot must not produce four invented shots."""
        with patch.object(engine, "_load_state", return_value={"world_prompt": ""}), \
             patch("prompts_store.PROMPTS", {"world_initial_state": "too short"}):
            self.assertEqual(cutscene.mystery_shotlist("t"), [])

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

    def test_the_montage_is_four_beats_with_no_plate_flashed_on_the_end(self):
        """The opening used to append a fifth beat: a separately rendered plate.

        That plate was its own image render whose only other job was to seed
        this montage, and being a from-scratch text-to-image guess it was free
        to disagree with the four panels about where the level was. It did — an
        indoor storeroom on the end of four photographs of an open-pit mine.
        The beat the player arrives on is the IDLE now, drawn FROM these panels.
        """
        state = {"world_prompt": "The yard.", "tape_frames": [], "feed_log": []}
        items = [{"type": "narrative_event", "content": "Ahead of you: the yard."},
                 {"type": "player_choice_prompt", "id": 3, "choices": [{"text": "Go"}]}]
        with patch.object(engine, "INTRO_CUTSCENE", True), \
             patch.object(engine, "IMAGE_ENABLED", True):
            staged = engine._stage_opening_montage("t", state, items)

        out_dir = Path(self._tmpdir.name) / "shots_url"
        out_dir.mkdir(parents=True, exist_ok=True)
        grid = out_dir / "grid.png"
        Image.new("RGB", (480, 360), (70, 60, 45)).save(grid)
        with patch.object(engine, "get_state", return_value=state), \
             patch.object(engine, "_load_state", return_value=state), \
             patch.object(engine, "_save_state"), \
             patch.object(engine, "_get_image_dir", return_value=str(out_dir)), \
             patch.object(engine, "IMAGE_ENABLED", True), \
             patch("gemini_image_utils.GEMINI_API_KEY", "k"), \
             patch("gemini_image_utils.generate_with_gemini",
                   return_value=str(grid)), \
             patch.object(engine, "_to_web_image_url",
                          side_effect=lambda p, s: "/images/" + Path(p).name):
            payload = cutscene.play_for_session("t", {
                "cutscene_id": staged[-1]["metadata"]["cutscene_id"],
                "mood": "approach", "source_url": "",
            })

        self.assertEqual(len(payload["shots"]), 4, "four panels, nothing after")
        self.assertNotIn("threshold", [s["camera"] for s in payload["shots"]])

    def test_the_opening_renders_without_a_plate_at_all(self):
        """It IS the run's first render. There is nothing in front of it."""
        out_dir = Path(self._tmpdir.name) / "shots_none"
        out_dir.mkdir(parents=True, exist_ok=True)
        grid = out_dir / "grid.png"
        Image.new("RGB", (480, 360), (80, 40, 20)).save(grid)
        seen = {}

        def fake_t2i(**kwargs):
            seen.update(kwargs)
            return str(grid)

        with patch.object(engine, "_get_image_dir", return_value=str(out_dir)), \
             patch.object(engine, "IMAGE_ENABLED", True), \
             patch("gemini_image_utils.GEMINI_API_KEY", "k"), \
             patch("gemini_image_utils.generate_with_gemini",
                   side_effect=fake_t2i), \
             patch("gemini_image_utils.generate_gemini_img2img") as img2img, \
             patch.object(engine, "_to_web_image_url",
                          side_effect=lambda p, s: "/images/" + Path(p).name):
            result = cutscene.generate_shots(
                None, session_id="t", mood="approach",
                plate_role="destination")
        img2img.assert_not_called()
        self.assertEqual(len(result["shots"]), 4)
        self.assertEqual(result["source"], "gemini")
        self.assertEqual(result["source_plate"], "")
        self.assertEqual(seen.get("image_size"), cutscene.GRID_RENDER_SIZE,
                         "the grid is sliced into four; it needs the pixels")

    def test_the_opening_prompt_never_describes_the_cast(self):
        """Every panel of the opening is required to be empty of people, and
        `world_anchor` with the character in it opened the prompt by describing
        exactly what the panels must not contain. While the montage was img2img
        off a place-locked plate that contradiction mostly lost; as
        text-to-image it wins — a live run came back with a figure at the fence
        and two front-facing portraits of a man holding a camera."""
        gi.save_spec({"player_character": dict(
            gi.default_spec()["player_character"], enabled=True,
            name="Jason Fleece", appearance="adult man, short dark hair",
            wardrobe="olive field jacket",
            signature_gear="a battered 35mm stills camera on a neck strap")})
        text = cutscene.build_cutscene_prompt(
            "approach", plate_role="destination", has_reference=False).lower()
        for leak in ("jason", "olive field jacket", "35mm stills camera",
                     "adult man"):
            self.assertNotIn(leak, text, f"the cast leaked into the opening: {leak}")
        self.assertIn("completely empty of people", text)

    def test_a_restage_still_carries_the_cast(self):
        """Only the opening bans people. An anchor montage restages a beat the
        player is in, and the cast has to be held across its four angles."""
        gi.save_spec({"player_character": dict(
            gi.default_spec()["player_character"], enabled=True,
            name="Jason Fleece", wardrobe="olive field jacket")})
        text = cutscene.build_cutscene_prompt(
            "encounter", plate_role="anchor").lower()
        self.assertIn("olive field jacket", text)

    def test_a_montage_with_no_reference_does_not_talk_about_one(self):
        """Every PLACE LOCK clause describes an attached photograph. Emitting
        one with nothing attached sends the model looking for an image it
        cannot see."""
        text = cutscene.build_cutscene_prompt(
            "approach", plate_role="destination", has_reference=False)
        self.assertIn("THIS IS THE FIRST PHOTOGRAPH OF THIS WORLD", text)
        self.assertNotIn("PLACE LOCK", text)
        self.assertNotIn("Preserve the people and wardrobe", text)

    def test_a_restage_still_locks_to_its_plate(self):
        """Only the opening draws from nothing. Every other mood restages a
        beat the player can see."""
        text = cutscene.build_cutscene_prompt("encounter", plate_role="anchor")
        self.assertIn("PLACE LOCK", text)
        self.assertIn("the current photograph of this exact place", text)

    def test_generate_shots_returns_only_what_it_drew(self):
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
    """Reset opens on the montage, and the montage is the run's FIRST render."""

    def test_a_run_opens_on_a_montage(self):
        with patch.object(engine, "INTRO_CUTSCENE", True), \
             patch.object(engine, "IMAGE_ENABLED", True):
            self.assertTrue(engine._open_on_montage({}))

    def test_a_cold_world_still_gets_its_montage(self):
        """It used to need a cached World frame to establish toward, and that
        requirement is what silently deleted the opening whenever the cache was
        unusable — the 2026-09-17 report. The montage establishes the place
        itself now, so there is nothing left to be missing."""
        with patch.object(engine, "INTRO_CUTSCENE", True), \
             patch.object(engine, "IMAGE_ENABLED", True):
            self.assertTrue(engine._open_on_montage({}))

    def test_an_authored_opening_cutscene_wins(self):
        """A Cutscene node on the start of the graph is somebody's decision."""
        with patch.object(engine, "INTRO_CUTSCENE", True), \
             patch.object(engine, "IMAGE_ENABLED", True):
            self.assertFalse(
                engine._open_on_montage({"experience_cutscene_id": "c1"}))

    def test_text_only_runs_never_open_on_a_montage(self):
        with patch.object(engine, "INTRO_CUTSCENE", True), \
             patch.object(engine, "IMAGE_ENABLED", False):
            self.assertFalse(engine._open_on_montage({}))

    def test_staging_parks_the_slate_and_renders_nothing(self):
        state = {}
        items = [{"type": "narrative_event", "content": "You are almost there."},
                 {"type": "player_choice_prompt", "choices": [{"text": "Go in"}]}]
        out = engine._stage_opening_montage("t", state, items)
        kinds = [it["type"] for it in out]
        # Verbs under a montage ask the player to act during a beat they are
        # meant to watch.
        self.assertNotIn("player_choice_prompt", kinds)
        self.assertIn("cutscene", kinds)
        self.assertEqual(state["pending_opening_choices"]["type"],
                         "player_choice_prompt")
        self.assertNotIn("scene_image", kinds)
        pending = state["pending_cutscene"]
        self.assertTrue(pending["opening"])
        self.assertEqual(pending["mood"], "approach")

    def test_staging_claims_no_plate(self):
        """Staging is pure bookkeeping now — no image has been drawn yet, and
        the montage is about to be the first. A stale source_path here would
        send play_for_session looking for a reference that is not this run's."""
        state = {}
        out = engine._stage_opening_montage("t", state, [])
        self.assertEqual(state["pending_cutscene"]["source_path"], "")
        self.assertNotIn("current_image_url", state)
        self.assertNotIn("source_url", out[-1]["metadata"])

    def test_the_feed_item_carries_the_card_copy(self):
        state = {}
        out = engine._stage_opening_montage("t", state, [])
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

    def test_the_opening_stamp_survives_the_staged_copy_being_lost(self):
        """The stamp is DERIVED, not just inherited.

        bugs/20260917_155759: `pending_cutscene` was gone from the persisted
        state by the time play_for_session ran — the stale module-global mirror,
        written back by one of the ~50 status/feed polls between the reset and
        the play. So `prev` was empty, both keep branches missed, and `opening`
        was lost even though the client had echoed the staged `open-…` id. The
        hand-off then never rendered the first playable frame, never released the
        boot gate, and the run sat on the World's cached frame — reported as "it
        just defaulted to the default image".

        The server stages the opening itself, so it is identifiable without any
        stored state: mood "approach", no graph node.
        """
        saved = {}
        shots = [{"url": "/images/a.png", "path": ""}]
        with patch.object(engine, "get_state", return_value={}), \
             patch.object(engine, "_load_state", return_value={}), \
             patch.object(engine, "_save_state",
                          side_effect=lambda st, sid: saved.update(st)), \
             patch.object(engine, "_sync_ambient_state"), \
             patch.object(cutscene, "resolve_source_path", return_value=None), \
             patch.object(cutscene, "generate_shots", return_value={
                 "shots": shots, "duration_ms": 4000, "source": "incoming"}):
            out = cutscene.play_for_session(
                "t", {"cutscene_id": "open-d99401e3", "mood": "approach"})
        self.assertTrue(out["ok"], "an opening montage renders with no plate")
        self.assertTrue(
            saved["pending_cutscene"]["opening"],
            "the hand-off cannot tell this is the opening, so the run will lose "
            "its first playable frame and sit on the World's cached image")

    def test_a_graph_cutscene_is_not_mistaken_for_the_opening(self):
        """Only the server-staged montage is the opening. A graph node with the
        same mood interrupts a run already on screen — prefetching an
        establishing beat for it would render a frame nobody asked for."""
        saved = {}
        with patch.object(engine, "get_state", return_value={}), \
             patch.object(engine, "_load_state", return_value={}), \
             patch.object(engine, "_save_state",
                          side_effect=lambda st, sid: saved.update(st)), \
             patch.object(engine, "_sync_ambient_state"), \
             patch.object(cutscene, "resolve_source_path", return_value=__file__), \
             patch.object(cutscene, "_outgoing_world_id", return_value=""), \
             patch.object(cutscene, "generate_shots", return_value={
                 "shots": [{"url": "/images/a.png", "path": ""}],
                 "duration_ms": 4000, "source": "incoming"}):
            import experience_store as xs
            with patch.object(xs, "cutscene_by_id",
                              return_value={"id": "c1", "mood": "approach",
                                            "name": "A Door"}):
                cutscene.play_for_session("t", {"cutscene_id": "c1"})
        self.assertFalse(saved["pending_cutscene"]["opening"])

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

    What this pins is the ORDER the player experiences: something to read, a
    montage with no verbs under it, then verbs over the frame the run lands on.
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
        self.out_dir = Path(self._tmpdir.name) / "shots"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.grid = self.out_dir / "grid.png"
        Image.new("RGB", (480, 360), (70, 60, 45)).save(self.grid)

    def test_the_player_reads_then_watches_then_acts(self):
        state = {"world_prompt": "The yard.", "tape_frames": [], "feed_log": []}
        items = [
            {"type": "narrative_event", "content": "Ahead of you: The Kettle Yard."},
            {"type": "player_choice_prompt", "id": 7,
             "choices": [{"text": "Cross the yard"}]},
        ]
        with patch.object(engine, "INTRO_CUTSCENE", True), \
             patch.object(engine, "IMAGE_ENABLED", True):
            self.assertTrue(engine._open_on_montage(state))
            staged = engine._stage_opening_montage("t", state, items)

        self.assertEqual([it["type"] for it in staged],
                         ["narrative_event", "cutscene"])
        meta = staged[-1]["metadata"]
        self.assertIn("pump house", meta["goal"])
        self.assertEqual(meta["name"], "The Kettle Yard")

        # The client's own call. It sends no plate, and there is none to find:
        # this montage is the first thing the run draws.
        with patch.object(engine, "get_state", return_value=state), \
             patch.object(engine, "_load_state", return_value=state), \
             patch.object(engine, "_save_state"), \
             patch.object(engine, "_get_image_dir", return_value=str(self.out_dir)), \
             patch.object(engine, "IMAGE_ENABLED", True), \
             patch("gemini_image_utils.GEMINI_API_KEY", "k"), \
             patch("gemini_image_utils.generate_with_gemini",
                   return_value=str(self.grid)), \
             patch.object(engine, "_to_web_image_url",
                          side_effect=lambda p, s: "/images/" + Path(p).name):
            payload = cutscene.play_for_session("t", {
                "cutscene_id": meta["cutscene_id"],
                "mood": meta["mood"],
                "name": meta["name"],
                "source_url": "",
            })
        self.assertTrue(payload["ok"])
        # Four panels and nothing after them. The frame the player arrives on
        # is the idle beat, drawn FROM these.
        self.assertEqual(len(payload["shots"]), 4)
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


class TestTheMontageReachesTheFirstPlayableFrame(_Isolated):
    """The order is: level definition → montage → first playable frame.

    The montage is now the run's first render, and the idle beat is drawn from
    its panels. There is no plate between them any more — that plate was a
    third render whose only jobs were to seed these two and to flash for four
    seconds, and being a from-scratch guess it was free to disagree with the
    montage about where the level was.
    """

    def setUp(self):
        super().setUp()
        self.dir = Path(self._tmpdir.name)
        self.panels = []
        for i in range(4):
            p = self.dir / f"cutscene_ab12_0{i + 1}.png"
            Image.new("RGB", (64, 48), (10 * i, 40, 50)).save(p)
            self.panels.append(p)

    def _pending(self, shots=None):
        if shots is None:
            shots = [{"url": f"/images/{p.name}", "path": str(p)}
                     for p in self.panels]
        return {"opening": True, "source_path": "", "shots": shots}

    def test_the_widest_montage_panel_leads(self):
        """Panel one of the approach mood is "the widest, emptiest view this
        place affords" — the strongest single statement of where we are, and
        now the img2img ANCHOR rather than context behind a plate."""
        refs = engine._montage_place_refs(self._pending())
        self.assertEqual(refs[0], str(self.panels[0]))

    def test_two_panels_ride_in_now_that_there_is_no_plate(self):
        """The plate used to hold the anchor slot and one panel fitted behind
        it. The panels ARE the place now."""
        refs = engine._montage_place_refs(self._pending())
        self.assertEqual(len(refs), engine.OPENING_MONTAGE_REFS)
        self.assertEqual(refs, [str(self.panels[0]), str(self.panels[1])])

    def test_a_montage_that_never_developed_carries_nothing(self):
        self.assertEqual(engine._montage_place_refs(self._pending([])), [])

    def test_panels_that_were_swept_off_disk_are_skipped(self):
        self.panels[0].unlink()
        refs = engine._montage_place_refs(self._pending())
        self.assertEqual(refs[0], str(self.panels[1]))

    def test_the_idle_is_anchored_on_the_montage_itself(self):
        """No plate in refs[0] any more. The place the player arrives in is the
        photograph they were just shown."""
        captured = {}

        def fake_flipbook(**kwargs):
            captured.update(kwargs)
            return None

        st = {"world_prompt": "The yard.", "time_of_day": "dusk"}
        with patch.object(engine, "_load_state", return_value=st), \
             patch.object(engine, "flipbook_active", return_value=True), \
             patch.object(engine, "_get_image_dir", return_value=str(self.dir)), \
             patch.object(engine, "_opening_idle_still", return_value=None), \
             patch.object(engine, "_flipbook_generate", side_effect=fake_flipbook):
            engine._generate_opening_establishing("t", self._pending())

        self.assertEqual(captured["refs"],
                         [str(self.panels[0]), str(self.panels[1])])
        self.assertTrue(captured["ref_is_anchor"])
        self.assertTrue(captured["establishing"])

    def test_the_idle_needs_a_montage_to_stand_in(self):
        """With no panels there is nothing to put the character into, and the
        caller falls back to the ordinary intro render."""
        with patch.object(engine, "_flipbook_generate") as gen:
            self.assertIsNone(
                engine._generate_opening_establishing("t", self._pending([])))
        gen.assert_not_called()

    def test_with_flipbook_off_the_idle_is_a_single_still(self):
        """There is no plate behind this any more, so a boot that cannot draw a
        flipbook would otherwise hand turn one an unpeopled landscape."""
        made = self.dir / "idle.png"
        Image.new("RGB", (64, 48), (20, 20, 20)).save(made)
        st = {"world_prompt": "The yard.", "time_of_day": "dusk"}
        with patch.object(engine, "_load_state", return_value=st), \
             patch.object(engine, "flipbook_active", return_value=False), \
             patch.object(engine, "_get_image_dir", return_value=str(self.dir)), \
             patch.object(engine, "_flipbook_generate") as flip, \
             patch("gemini_image_utils.generate_gemini_img2img",
                   return_value=str(made)) as still, \
             patch.object(engine, "_to_web_image_url",
                          side_effect=lambda p, s: "/images/" + Path(p).name):
            out = engine._generate_opening_establishing("t", self._pending())
        flip.assert_not_called()
        self.assertIsNone(out["payload"], "nothing to animate")
        self.assertEqual(out["still"], str(made))
        self.assertEqual(still.call_args.kwargs["reference_image_path"],
                         [str(self.panels[0]), str(self.panels[1])])
        self.assertTrue(still.call_args.kwargs["include_people"])

    def test_the_idle_asks_for_the_character_to_be_added(self):
        """The montage panels are unpeopled BY INSTRUCTION, so the beat that
        follows has to put somebody into them — that was the deleted plate's
        one real job."""
        with patch.object(gi, "shows_character", return_value=True), \
             patch.object(gi, "display_name", return_value="Kelsey"):
            text = engine._flipbook_establishing_block(4)
            single = engine._flipbook_establishing_block(single=True)
        for block in (text, single):
            self.assertIn("THE REFERENCE PHOTOGRAPHS HAVE NOBODY IN THEM", block)
            self.assertIn("Put Kelsey into that place", block)
            self.assertIn("seen from BEHIND", block)

    def test_a_missing_idle_beat_is_rendered_inline_from_the_panels(self):
        """The failure that shipped: the prefetch did not fire, nothing noticed,
        and turn one drew its own first frame text-to-image from NO reference in
        a different place. The hand-off must not be able to continue without a
        frame built from the montage."""
        st = {"pending_cutscene": self._pending(),
              "feed_log": [], "tape_frames": [], "current_image_url": ""}
        made = self.dir / "inline_idle.png"
        Image.new("RGB", (64, 48), (44, 44, 44)).save(made)
        seq = {"payload": {"frame_count": 4}, "still": str(made),
               "still_url": "/images/inline_idle.png",
               "seq": {"still_path": str(made)}}
        with patch.object(engine, "_load_history", return_value=[]), \
             patch.object(engine, "_save_history"), \
             patch.object(engine, "_sync_ambient_history"), \
             patch.object(engine, "_spawn_cached_opening_vision"), \
             patch.object(engine, "_generate_opening_establishing",
                          return_value=seq) as gen:
            out = engine._finish_opening_montage(st, "t", opening_seq=None)
        gen.assert_called_once()
        self.assertEqual(gen.call_args[0][1]["shots"], self._pending()["shots"])
        self.assertEqual(out["image_url"], "/images/inline_idle.png")

    def test_the_inline_render_is_skipped_when_there_are_no_panels(self):
        st = {"pending_cutscene": self._pending([]),
              "feed_log": [], "tape_frames": [], "current_image_url": ""}
        with patch.object(engine, "_load_history", return_value=[]), \
             patch.object(engine, "_save_history"), \
             patch.object(engine, "_sync_ambient_history"), \
             patch.object(engine, "_spawn_cached_opening_vision"), \
             patch.object(engine, "_generate_opening_establishing") as gen:
            engine._finish_opening_montage(st, "t", opening_seq=None)
        gen.assert_not_called()

    def test_the_opening_stamp_survives_a_play_id_that_does_not_match(self):
        """`opening` is what both /api/cutscene/play and /api/cutscene/complete
        read. Matching on cutscene_id alone let any mismatch wipe it, and the
        run then lost its first playable frame AND its authored slate."""
        src = Path(cutscene.__file__).read_text(encoding="utf-8")
        body = src.split("def play_for_session", 1)[1]
        self.assertIn('prev.get("opening") and not (prev.get("shots") or [])', body)

    def test_when_the_idle_lands_the_panels_are_not_sent_twice_inline(self):
        """The inline fallback must not fire when the prefetch already won."""
        st = {"pending_cutscene": self._pending(),
              "feed_log": [], "tape_frames": [], "current_image_url": ""}
        still = str(self.dir / "prefetched.png")
        Image.new("RGB", (64, 48), (30, 30, 30)).save(still)
        seq = {"payload": {"frame_count": 4}, "still": still,
               "still_url": "/images/prefetched.png",
               "seq": {"still_path": still}}
        with patch.object(engine, "_load_history", return_value=[]), \
             patch.object(engine, "_save_history"), \
             patch.object(engine, "_sync_ambient_history"), \
             patch.object(engine, "_spawn_cached_opening_vision"), \
             patch.object(engine, "_generate_opening_establishing") as gen:
            engine._finish_opening_montage(st, "t", opening_seq=seq)
        gen.assert_not_called()

    def test_with_flipbook_on_the_panels_are_not_sent_twice(self):
        """The idle frame was already generated FROM those panels. Handing the
        raw panels to turn one as well puts a second composition beside the one
        that already won."""

    def test_when_the_idle_lands_the_panels_are_not_sent_twice(self):
        """The idle frame was already generated FROM those panels. Handing the
        raw panels to turn one as well puts a second composition beside the one
        that already won."""
        st = {"pending_cutscene": self._pending(),
              "feed_log": [], "tape_frames": [], "current_image_url": ""}
        still = str(self.dir / "establishing.png")
        Image.new("RGB", (64, 48), (30, 30, 30)).save(still)
        seq = {"payload": {"frame_count": 4}, "still": still,
               "still_url": "/images/establishing.png",
               "seq": {"still_path": still}}
        saved = {}
        with patch.object(engine, "_load_history", return_value=[]), \
             patch.object(engine, "_save_history",
                          side_effect=lambda h, s: saved.setdefault("hist", h)), \
             patch.object(engine, "_sync_ambient_history"), \
             patch.object(engine, "_spawn_cached_opening_vision"):
            engine._finish_opening_montage(st, "t", opening_seq=seq)
        self.assertEqual(saved["hist"][0]["montage_refs"], [])

    def test_turn_one_picks_the_panels_up_off_the_handoff_entry(self):
        """The collector has to read them back, or writing them was pointless."""
        src = Path(engine.__file__).read_text(encoding="utf-8")
        body = src.split("def _gen_image_impl", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('entry.get("montage_refs")', body)
        self.assertIn("opening_montage_refs", body)
        # and they must be appended to the references the render actually uses
        self.assertIn("ref_images_to_use.append(extra)", body)

    def test_extra_references_survive_the_flipbook_assembler(self):
        """_flipbook_generate read refs[0] and dropped the rest on the floor,
        which is what made passing the montage in a no-op."""
        captured = {}

        def fake_img2img(**kwargs):
            captured["refs"] = kwargs.get("reference_image_path")
            return None

        st = {}
        with patch("gemini_image_utils.generate_gemini_img2img",
                   side_effect=fake_img2img), \
             patch.object(engine, "flipbook_settings",
                          return_value={"frames": 4, "frame_ms": 500}):
            engine._flipbook_generate(
                prompt_str="", caption="opening", choice="", dispatch="",
                world_prompt="", time_of_day="", img_dir=str(self.dir),
                session_id="t", st=st,
                refs=[str(self.panels[0]), str(self.panels[1])],
                ref_is_anchor=True, establishing=True, write_state=False,
            )
        self.assertEqual(captured["refs"][:2],
                         [str(self.panels[0]), str(self.panels[1])])


class TestTheHandoffBelongsToThisRun(_Isolated):
    """The establishing render runs ~15s OUTSIDE the state lock, on purpose —
    holding it across an image call would stall every other session. So the
    result has to prove it still belongs to the run that is landing."""

    def _complete(self, before, after):
        """Run api_cutscene_complete with the run swapped mid-render."""
        import api
        seq = {"payload": {"frame_count": 4}, "still": __file__,
               "still_url": "/images/old.png", "seq": {"still_path": __file__}}
        seen = iter([before, after, after, after])
        with api.app.test_request_context("/api/cutscene/complete", json={}), \
             patch.object(engine, "_resolve_request_session_id", return_value="t"), \
             patch.object(engine, "_load_state", side_effect=lambda *a, **k: next(seen)), \
             patch.object(engine, "_save_state"), \
             patch.object(engine, "_sync_ambient_state"), \
             patch.object(engine, "_load_history", return_value=[{"choice": "x"}]), \
             patch.object(engine, "_generate_opening_establishing", return_value=seq):
            return engine.api_cutscene_complete().get_json()

    def _run(self, cid, url):
        return {"pending_cutscene": {"opening": True, "cutscene_id": cid,
                                     "shots": [{"url": url, "path": ""}]},
                "feed_log": [], "tape_frames": []}

    def test_the_beat_lands_when_the_run_is_still_the_same_one(self):
        out = self._complete(self._run("open-a", "/images/a.png"),
                             self._run("open-a", "/images/a.png"))
        self.assertEqual(out["image_url"], "/images/old.png")
        self.assertEqual(out["sequence"], {"frame_count": 4})

    def test_a_beat_from_a_replaced_run_is_discarded(self):
        """A reset landing inside the render stages a fresh montage. Installing
        this sequence over that one would open the NEW run on the OLD run's
        establishing frame — the stale first frame this path exists to stop."""
        out = self._complete(self._run("open-a", "/images/a.png"),
                             self._run("open-b", "/images/b.png"))
        self.assertIsNone(out["sequence"])
        self.assertEqual(out["image_url"], "/images/b.png",
                         "the new run lands on its own montage")


class TestTheMontageIsNotLiftedOntoAStalePicture(unittest.TestCase):
    """Reported as "that same stupid image appeared in between the opening
    cutscene and the first frame, as a glitch, for like 1 second".

    It is the START MENU's wallpaper. Signal.lock paints the warmed still onto
    the real scene layer on the way into a run, deliberately, so a plain start
    does not open on a black void. But `applyDest` only STARTS the swap —
    setScene waits for the new image to load before painting it — so popping the
    montage the instant applyDest returned uncovered that wallpaper for exactly
    as long as the decode took.
    """

    @classmethod
    def setUpClass(cls):
        cls.js = (Path(__file__).resolve().parent / "static" / "js" /
                  "standalone.js").read_text(encoding="utf-8", errors="replace")

    def _finish(self):
        return self.js.split("    async function finish() {", 1)[1] \
                      .split("\n    function onEsc", 1)[0]

    def test_the_overlay_waits_for_a_real_picture_before_it_pops(self):
        # Split on the CALL: the comment above it names the function too.
        fn = self._finish()
        before, after = fn.split("await whenDestPainted", 1)
        self.assertIn("applyDest(hop)", before,
                      "the destination is not even requested before the wait")
        self.assertIn("Moments.pop", after,
                      "the montage pops before the frame has painted")

    def test_the_wait_is_bounded(self):
        """A frame that never paints must not trap the player in the montage."""
        fn = self.js.split("function whenDestPainted(maxMs) {", 1)[1] \
                    .split("\n    }", 1)[0]
        self.assertIn("setTimeout(fin", fn)
        self.assertIn("onNextScenePainted", fn)

    def test_a_cutscene_with_nowhere_to_go_does_not_wait(self):
        """A graph cutscene that leads nowhere has no frame to wait for, and
        sitting out the timeout would just stall on the last shot."""
        before = self._finish().split("await whenDestPainted", 1)[0]
        self.assertIn("if (hop.destUrl)", before)


class TestTheFirstPlayableFrameRendersBehindTheMontage(_Isolated):
    """The montage is ~20s of held shots. The first playable frame used to be
    started only once the player had watched every one of them out, so the
    opening spent that whole time doing nothing and then made them wait again.
    It now starts the instant the montage begins playing, and the hand-off
    collects it."""

    def setUp(self):
        super().setUp()
        engine._OPENING_PREFETCH.clear()
        self.pending = {"opening": True, "cutscene_id": "open-a",
                        "source_path": __file__, "shots": [{"path": __file__}]}

    def tearDown(self):
        engine._OPENING_PREFETCH.clear()
        super().tearDown()

    def _beat(self, url="/images/beat.png"):
        return {"payload": {"frame_count": 4}, "still": __file__,
                "still_url": url, "seq": {"still_path": __file__}}

    def test_the_render_starts_when_the_montage_starts_playing(self):
        started = threading.Event()
        with patch.object(engine, "_generate_opening_establishing",
                          side_effect=lambda s, p: started.set() or self._beat()):
            self.assertTrue(engine._spawn_opening_establishing("t", self.pending))
            self.assertTrue(started.wait(5), "the beat never started rendering")
            seq, handled = engine._take_opening_establishing("t", "open-a")
        self.assertTrue(handled)
        self.assertEqual(seq["still_url"], "/images/beat.png")

    def test_the_handoff_holds_on_a_beat_that_is_still_rendering(self):
        """"Kick it off early" only helps if a montage the player skipped still
        waits for the frame — turn one's img2img continues from it."""
        release = threading.Event()

        def slow(_sid, _pending):
            release.wait(5)
            return self._beat("/images/slow.png")

        with patch.object(engine, "_generate_opening_establishing",
                          side_effect=slow):
            engine._spawn_opening_establishing("t", self.pending)
            threading.Timer(0.2, release.set).start()
            seq, handled = engine._take_opening_establishing("t", "open-a")
        self.assertTrue(handled)
        self.assertEqual(seq["still_url"], "/images/slow.png")

    def test_a_beat_from_another_run_is_not_collected(self):
        """A reset mid-montage stages a new opening. Its hand-off must not be
        handed the previous run's frame — it renders its own."""
        with patch.object(engine, "_generate_opening_establishing",
                          return_value=self._beat()):
            engine._spawn_opening_establishing("t", self.pending)
        seq, handled = engine._take_opening_establishing("t", "open-b")
        self.assertIsNone(seq)
        self.assertFalse(handled, "the caller must fall through to its own render")

    def test_one_render_per_run_however_often_play_is_called(self):
        calls = []
        with patch.object(engine, "_generate_opening_establishing",
                          side_effect=lambda s, p: calls.append(p) or self._beat()):
            self.assertTrue(engine._spawn_opening_establishing("t", self.pending))
            self.assertFalse(engine._spawn_opening_establishing("t", self.pending))
            engine._take_opening_establishing("t", "open-a")
        self.assertEqual(len(calls), 1)

    def test_only_the_opening_prefetches(self):
        """Every other cutscene interrupts a run already on screen; there is no
        establishing beat waiting on the other side of it."""
        graph_cut = dict(self.pending, opening=False)
        with patch.object(engine, "_generate_opening_establishing") as gen:
            self.assertFalse(engine._spawn_opening_establishing("t", graph_cut))
        gen.assert_not_called()

    def test_the_handoff_does_not_render_again_when_the_beat_is_waiting(self):
        """The whole point. With a prefetch in hand /api/cutscene/complete must
        return immediately instead of starting a second ~15s render."""
        import api

        st = {"pending_cutscene": {"opening": True, "cutscene_id": "open-a",
                                   "shots": [{"url": "/images/a.png", "path": ""}]},
              "feed_log": [], "tape_frames": []}
        with patch.object(engine, "_generate_opening_establishing",
                          return_value=self._beat()):
            engine._spawn_opening_establishing("t", self.pending)
            engine._OPENING_PREFETCH["t"]["done"].wait(5)
        with api.app.test_request_context("/api/cutscene/complete", json={}), \
             patch.object(engine, "_resolve_request_session_id", return_value="t"), \
             patch.object(engine, "_load_state", return_value=st), \
             patch.object(engine, "_save_state"), \
             patch.object(engine, "_sync_ambient_state"), \
             patch.object(engine, "_load_history", return_value=[{"choice": "x"}]), \
             patch.object(engine, "_generate_opening_establishing") as gen:
            out = engine.api_cutscene_complete().get_json()
        gen.assert_not_called()
        self.assertEqual(out["image_url"], "/images/beat.png")
        self.assertEqual(out["sequence"], {"frame_count": 4})

    def test_playing_the_montage_kicks_the_render_off(self):
        """The wiring itself: /api/cutscene/play returns the shots AND leaves
        the first playable frame rendering behind them."""
        import api

        st = {"pending_cutscene": self.pending}
        with api.app.test_request_context("/api/cutscene/play", json={}), \
             patch.object(engine, "_resolve_request_session_id", return_value="t"), \
             patch.object(engine, "_rate_limited", return_value=False), \
             patch.object(engine, "_load_state", return_value=st), \
             patch("cutscene.play_for_session",
                   return_value={"ok": True, "shots": [{"url": "/images/1.png"}]}), \
             patch.object(engine, "_spawn_opening_establishing") as spawn:
            body = engine.api_cutscene_play()
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args[0][1]["cutscene_id"], "open-a")
        self.assertTrue(body.get_json()["ok"])


class TestTheOpeningIdle(_Isolated):
    """The first thing a player ever sees of their own character.

    "Stands still, same pose, same spot" got exactly that and it read as a
    figure rocking back and forth for eight seconds. The beat is an idle
    animation now — settle, check the gear, look up at something far off — with
    the camera still locked to the composition the montage handed over.
    """

    def _block(self):
        with patch.object(gi, "shows_character", return_value=True), \
             patch.object(gi, "display_name", return_value="Kelsey"):
            return engine._flipbook_establishing_block(4)

    def test_the_character_does_something_worth_watching(self):
        text = self._block()
        self.assertIn("SETTLES", text)
        self.assertIn("CHECKS THEIR GEAR", text)
        self.assertIn("LOOKS UP AND OUT", text)
        self.assertIn("Kelsey", text)

    def test_something_is_coming_and_never_arrives(self):
        """Tension without an event. A threat that actually turns up in the
        opening frames would be a monster the turn loop never placed."""
        text = self._block()
        self.assertIn("has NOT arrived", text)
        self.assertIn("never identified", text)
        self.assertIn("never enters the middle or the foreground", text)

    def test_the_camera_and_the_feet_are_still_pinned(self):
        """The reason this block exists: the action block walks the character
        out of the frame the montage established, and the last panel is turn
        one's img2img anchor."""
        text = self._block()
        self.assertIn("SAME framing", text)
        self.assertIn("The feet do not move", text)
        self.assertIn("does NOT walk", text)
        self.assertIn("Do NOT move the camera", text)
        self.assertIn("Do NOT introduce new locations", text)

    def test_first_person_gets_an_idle_it_can_actually_show(self):
        """With no character on screen there is nobody to watch settle, so the
        idle is the hands and the breath instead."""
        with patch.object(gi, "shows_character", return_value=False):
            text = engine._flipbook_establishing_block(4)
        self.assertIn("YOU CHECK YOUR GEAR", text)
        self.assertIn("your own hands", text)


class TestTheSheetsAreFilledBeforeAnythingRenders(_Isolated):
    """Nothing in a boot knows where the level is except the Level sheet.

    `opening_shot`, `place_summary`, `establishing_shot` and the montage's own
    shotlist all read it, and when a field is blank they each independently
    free-associate over a nine-thousand-word bible. Three reads of one empty
    field, three different answers, and a run that does not stay in a place.
    """

    def _hollow(self):
        ps.save_prompts_bulk({gi.SETTING_KEY: dict(gi.SETTING_DEFAULTS,
                                                   enabled=True, name="Level 1")})

    def test_a_hollow_sheet_is_drafted_from_the_bible(self):
        self._hollow()
        self.assertTrue(engine.level_sheet_is_hollow())
        with patch.object(gi, "apply_text_fill") as fill:
            fill.return_value = {"filled": {"summary": "A drowned rail yard."}}
            self.assertTrue(engine._ensure_level_sheet_is_filled())
        self.assertIn(gi.SETTING_KEY, [c.args[0] for c in fill.call_args_list])

    def test_the_level_sheet_is_drafted_first(self):
        """Everything below the call site is about to read it."""
        self._hollow()
        order = []
        with patch.object(gi, "apply_text_fill",
                          side_effect=lambda b, **k: order.append(b) or {"filled": {}}):
            engine._ensure_level_sheet_is_filled()
        self.assertEqual(order[0], gi.SETTING_KEY)

    def test_one_blank_field_is_enough_to_trigger_it(self):
        """`goal` blank makes level_goal walk back to the first landmark — so a
        level whose landmarks start "chain-link fence" establishes a montage
        toward a fence the player is already standing at."""
        ps.save_prompts_bulk({gi.SETTING_KEY: dict(
            gi.SETTING_DEFAULTS, enabled=True, name="The Fence",
            summary="A quarantine perimeter.", landmarks="chain-link fence, mesa",
            goal="")})
        self.assertFalse(engine.level_sheet_is_hollow(), "not hollow — just a gap")
        self.assertEqual(gi.level_goal(), "chain-link fence", "the bug")
        with patch.object(gi, "apply_text_fill",
                          return_value={"filled": {"goal": "The pump house."}}) as fill:
            self.assertTrue(engine._ensure_level_sheet_is_filled())
        self.assertTrue(fill.called)

    def test_a_complete_set_of_sheets_costs_nothing(self):
        ps.save_prompts_bulk({
            gi.SETTING_KEY: dict(gi.SETTING_DEFAULTS, enabled=True, name="X",
                                 summary="s", era="e", palette="p",
                                 landmarks="l", opening_shot="o", goal="g"),
        })
        for block in gi.text_fillable_blocks():
            patch_fields = {f["id"]: "set" for f in gi.fillable_text_fields(block)}
            ps.save_prompts_bulk({block: dict(gi.get_spec()[block], **patch_fields)})
        with patch.object(gi, "apply_text_fill") as fill:
            self.assertFalse(engine._ensure_level_sheet_is_filled())
        fill.assert_not_called()

    def test_a_failed_draft_is_never_fatal(self):
        self._hollow()
        with patch.object(gi, "apply_text_fill", side_effect=RuntimeError("no key")):
            self.assertFalse(engine._ensure_level_sheet_is_filled())

    def test_with_no_text_model_it_says_so_and_moves_on(self):
        self._hollow()
        with patch.object(engine, "LLM_ENABLED", False), \
             patch.object(gi, "apply_text_fill") as fill:
            self.assertFalse(engine._ensure_level_sheet_is_filled())
        fill.assert_not_called()

    def test_reset_never_drafts_the_sheets_itself(self):
        """The regression this guards is the one it caused.

        Drafting on the boot path meant a live LLM call PERSISTING its guess
        over authoring data every reset. Handed the Four Corners bible it
        invented "The Kettle Yard — a flooded shipbreaking yard", wrote it into
        prompts/simulation_prompts.json, and the montage was drawn of it. The
        capability is fine; doing it silently, on boot, is not.
        """
        src = Path(engine.__file__).read_text(encoding="utf-8")
        reset = src.split("def _perform_game_reset(", 1)[1] \
                   .split("\ndef api_reset", 1)[0]
        self.assertNotIn("_ensure_level_sheet_is_filled()", reset,
                         "no LLM write over authoring data on the boot path")

    def test_every_run_walks_toward_something(self):
        """"The goal system is completely non functional ... making sure we
        always have a goal generated can really help the experience stay
        focused."

        A run with no goal has no shape: the montage established "toward '(no
        goal authored)'" and every frame was a place rather than a direction.
        """
        st = {"world_prompt": "a quarantined site"}
        with patch.object(gi, "level_goal", return_value=""), \
             patch.object(gi, "draft_level_goal",
                          return_value="The drill tower on the ridge."):
            got = engine._goal_for_this_run(st, "")
        self.assertEqual(got, "The drill tower on the ridge.")
        self.assertEqual(st["level_goal"], "The drill tower on the ridge.")

    def test_it_never_writes_over_the_level_sheet(self):
        """The distinction the whole design rests on.

        `_ensure_level_sheet_is_filled` was on the boot path for one afternoon,
        invented "The Kettle Yard" out of the Four Corners bible and PERSISTED
        it over the author's words. This drafts into the RUN instead: the
        authoring store is not touched, so there is nothing to undo.
        """
        st = {}
        with patch.object(gi, "level_goal", return_value=""), \
             patch.object(gi, "draft_level_goal", return_value="A tower."), \
             patch.object(ps, "save_prompts_bulk") as save, \
             patch.object(gi, "save_spec") as save_spec:
            engine._goal_for_this_run(st, "")
        save.assert_not_called()
        save_spec.assert_not_called()

    def test_an_authored_goal_is_never_second_guessed(self):
        st = {}
        with patch.object(gi, "level_goal", return_value="The red pump house."), \
             patch.object(gi, "draft_level_goal") as draft:
            got = engine._goal_for_this_run(st, "chain-link fence")
        self.assertEqual(got, "The red pump house.")
        draft.assert_not_called()

    def test_one_draft_per_playthrough(self):
        """Cached in state: the landmark must not change under the player
        halfway through the run, and it must not cost a call every time."""
        st = {"level_goal": "The drill tower."}
        with patch.object(gi, "level_goal", return_value=""), \
             patch.object(gi, "draft_level_goal") as draft:
            self.assertEqual(engine._goal_for_this_run(st, ""), "The drill tower.")
        draft.assert_not_called()

    def test_a_failed_draft_is_not_fatal(self):
        st = {}
        with patch.object(gi, "level_goal", return_value=""), \
             patch.object(gi, "draft_level_goal", side_effect=RuntimeError("no key")):
            self.assertEqual(engine._goal_for_this_run(st, "the mesa"), "the mesa")

    def test_the_goal_has_to_be_big_and_far_off(self):
        """"A large distant object / monolith / structure ... to draw the
        players eye". The draft used to ask only for something "visible from a
        distance", which a door in the next room technically is."""
        src = Path(gi.__file__).read_text(encoding="utf-8")
        prompt = src.split("def draft_level_goal", 1)[1].split("return _clean_text", 1)[0]
        low = prompt.lower()
        self.assertIn("big and it must be far away", low)
        self.assertIn("miles off", low)
        self.assertIn("horizon", low)
        # ...and the things that are none of those.
        for small in ("door", "room", "crate", "sign"):
            self.assertIn(small, low, f"{small!r} is not ruled out")

    def test_the_montage_is_told_where_this_run_is_going(self):
        src = Path(engine.__file__).read_text(encoding="utf-8")
        stage = src.split("def _stage_opening_montage", 1)[1] \
                   .split("\ndef ", 1)[0]
        self.assertIn("_goal_for_this_run(new_state", stage)

    def test_the_draft_is_still_reachable_on_purpose(self):
        tool = Path(engine.__file__).parent / "tools" / "draft_identity_sheets.py"
        self.assertTrue(tool.is_file(), "the capability has to stay reachable")
        self.assertIn("--apply", tool.read_text(encoding="utf-8"),
                      "and it has to be opt-in")


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
