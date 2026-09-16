"""
Tests for the Experience graph: Worlds stitched by transitions.

Covers the schema (experience_store), clone persistence (worlds_store),
and the engine hook that honors turn-count (and death) transitions.

Never touches the network. Redirects experiences/, worlds/, and the prompt
file into a temp dir so a run cannot rewrite the live authoring data.

Run: python -m unittest test_experience_graph -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import experience_store as xs
import worlds_store as ws
import prompts_store as ps
import game_identity as gi
import engine


class _IsolatedGraph(unittest.TestCase):
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


class TestConditionSchema(_IsolatedGraph):
    def test_the_type_enum_is_the_catalog(self):
        """New hooks are a catalog row. The dropdown and CONDITION_TYPES
        must stay the same list — not two hand-written pairs of buttons."""
        cat = xs.condition_catalog()
        self.assertEqual([row["id"] for row in cat], list(xs.CONDITION_TYPES))
        for row in cat:
            self.assertTrue(row["id"])
            self.assertTrue(row["label"])
            self.assertIn("fields", row)
        self.assertIn("turns", next(
            row["fields"] for row in cat if row["id"] == "turn_count"))

    def test_unknown_condition_type_falls_back_to_turn_count(self):
        cond = xs.normalize_condition({"type": "has_sword", "turns": 3})
        self.assertEqual(cond["type"], "turn_count")
        self.assertEqual(cond["turns"], 3)

    def test_turn_count_clamps(self):
        self.assertEqual(xs.normalize_condition({"type": "turn_count", "turns": 0})["turns"], 1)
        self.assertEqual(xs.normalize_condition({"type": "turn_count", "turns": 5000})["turns"], 999)

    def test_turn_count_met_at_threshold(self):
        cond = {"type": "turn_count", "turns": 4}
        self.assertFalse(xs.condition_met(cond, world_turn_count=3))
        self.assertTrue(xs.condition_met(cond, world_turn_count=4))
        self.assertTrue(xs.condition_met(cond, world_turn_count=9))

    def test_game_over_reads_player_alive_only(self):
        cond = {"type": "game_over"}
        self.assertFalse(xs.condition_met(cond, world_turn_count=99, player_alive=True))
        self.assertTrue(xs.condition_met(cond, world_turn_count=0, player_alive=False))

    def test_default_experience_is_one_world_no_transitions(self):
        exp = xs.get_experience()
        self.assertEqual(len(exp["worlds"]), 1)
        self.assertEqual(exp["transitions"], [])
        self.assertEqual(exp["start_world"], exp["worlds"][0]["id"])
        self.assertEqual(exp["sound"]["palette"], "tape")
        self.assertEqual(exp["sound"]["muted"], [])
        self.assertTrue(exp["lore"]["enabled"])
        # Empty Lore inherits the start World's bible so Play and the graph
        # share it. Isolated prompts seed a quiet test place.
        self.assertEqual(exp["lore"]["notes"], "A quiet test place.")
        self.assertEqual(exp["lore"].get("source"), "world")
        self.assertEqual(exp["lore"]["documents"], [])


class TestSoundSchema(_IsolatedGraph):
    def test_unknown_palette_falls_back_to_tape(self):
        sound = xs.normalize_sound({"palette": "arcade", "muted": ["clicks", "nope"]})
        self.assertEqual(sound["palette"], "tape")
        self.assertEqual(sound["muted"], ["clicks"])

    def test_volume_clamps(self):
        self.assertEqual(xs.normalize_sound({"volume": -2})["volume"], 0.0)
        self.assertEqual(xs.normalize_sound({"volume": 4})["volume"], 1.0)

    def test_set_sound_survives_add_world(self):
        xs.set_sound({"palette": "quiet", "muted": ["chrome"], "volume": 0.4})
        exp = xs.add_world("World 2")
        self.assertEqual(exp["sound"]["palette"], "quiet")
        self.assertEqual(exp["sound"]["muted"], ["chrome"])
        self.assertAlmostEqual(exp["sound"]["volume"], 0.4)

    def test_clone_copies_sound(self):
        xs.set_sound({"palette": "silent", "muted": ["turn"]})
        cloned = xs.create_experience("Night Mesa", clone_from=xs.get_active_slug())
        self.assertEqual(cloned["sound"]["palette"], "silent")
        self.assertEqual(cloned["sound"]["muted"], ["turn"])


class TestPacingSchema(_IsolatedGraph):
    def test_missing_threat_uses_the_product_clock(self):
        threat = xs._normalize_threat(None)
        self.assertEqual(threat["escalate_at"], 4)
        self.assertEqual(threat["critical_at"], 9)
        self.assertTrue(threat["beat_normal"].startswith("BEAT:"))
        self.assertTrue(threat["beat_escalating"].startswith("BEAT:"))
        self.assertTrue(threat["beat_critical"].startswith("BEAT:"))

    def test_new_experience_starts_slower_than_somewhere(self):
        exp = xs.default_experience()
        self.assertEqual(exp["threat"]["escalate_at"], 8)
        self.assertEqual(exp["threat"]["critical_at"], 20)
        saved = xs.create_experience("Slow Run")
        self.assertEqual(saved["threat"]["escalate_at"], 8)
        self.assertEqual(saved["threat"]["critical_at"], 20)

    def test_set_pacing_writes_beats_and_marks(self):
        exp = xs.set_pacing({
            "escalate_at": 3,
            "critical_at": 6,
            "beat_normal": "BEAT: a stranger watches from the ridge.",
            "beat_escalating": "BEAT: the tanks are close.",
            "beat_critical": "BEAT: last stand at the fence.",
        })
        self.assertEqual(exp["threat"]["escalate_at"], 3)
        self.assertEqual(exp["threat"]["critical_at"], 6)
        self.assertEqual(exp["threat"]["beat_normal"], "BEAT: a stranger watches from the ridge.")
        self.assertEqual(exp["threat"]["beat_escalating"], "BEAT: the tanks are close.")
        self.assertEqual(exp["threat"]["beat_critical"], "BEAT: last stand at the fence.")

    def test_critical_stays_above_escalate(self):
        threat = xs._normalize_threat({"escalate_at": 12, "critical_at": 8})
        self.assertEqual(threat["escalate_at"], 12)
        self.assertEqual(threat["critical_at"], 13)

    def test_clone_copies_pacing(self):
        xs.set_pacing({"escalate_at": 2, "critical_at": 5, "beat_normal": "BEAT: watched.", "beat_escalating": "BEAT: now."})
        cloned = xs.create_experience("Night Mesa", clone_from=xs.get_active_slug())
        self.assertEqual(cloned["threat"]["escalate_at"], 2)
        self.assertEqual(cloned["threat"]["critical_at"], 5)
        self.assertEqual(cloned["threat"]["beat_normal"], "BEAT: watched.")
        self.assertEqual(cloned["threat"]["beat_escalating"], "BEAT: now.")

    def test_engine_reads_authored_beats(self):
        xs.set_pacing({
            "escalate_at": 3,
            "critical_at": 6,
            "beat_normal": "BEAT: a stranger watches from the ridge.",
            "beat_escalating": "BEAT: the tanks are close.",
            "beat_critical": "BEAT: last stand at the fence.",
        })
        self.assertEqual(
            engine.beat_nudge_text({"threat_level": 0}),
            "BEAT: a stranger watches from the ridge.",
        )
        self.assertEqual(
            engine.beat_nudge_text({"threat_level": 3}),
            "BEAT: the tanks are close.",
        )
        self.assertEqual(
            engine.beat_nudge_text({"threat_level": 6}),
            "BEAT: last stand at the fence.",
        )
        self.assertEqual(engine._phase_for_threat(2), "normal")
        self.assertEqual(engine._phase_for_threat(3), "escalating")
        self.assertEqual(engine._phase_for_threat(6), "critical")


class TestExperiencePersistence(_IsolatedGraph):
    def test_add_world_clones_and_persists(self):
        exp = xs.add_world("World 2")
        self.assertEqual(len(exp["worlds"]), 2)
        names = {w["name"] for w in exp["worlds"]}
        self.assertIn("World 2", names)
        for w in exp["worlds"]:
            self.assertTrue(w["slug"], "every forked World needs a snapshot")
            data = ws.get_world(w["slug"])
            self.assertIn("world_initial_state", data.get("prompts") or {})
        again = xs.get_experience()
        self.assertEqual(len(again["worlds"]), 2)

    def test_persist_writes_the_world_you_are_in(self):
        """A rename used to fork a second file. REDRAW then drew the old slug."""
        exp = xs.get_experience()
        world = exp["worlds"][0]
        xs.ensure_world_snapshot(world)
        xs.save_experience(exp)
        slug = world["slug"]
        self.assertTrue(slug)
        xs.rename_world(world["id"], "World 3")
        spec = dict(gi.CHARACTER_DEFAULTS)
        spec.update({
            "enabled": True,
            "name": "Subject 7-Delta",
            "role": "Containment Breach Victim",
        })
        gi.save_spec({gi.CHARACTER_KEY: spec})
        out = xs.persist_world_snapshot(world["id"])
        stored = xs.world_by_id(out, world["id"])
        self.assertEqual(stored["slug"], slug)
        data = ws.get_world(slug)
        self.assertEqual(
            (data["prompts"].get(gi.CHARACTER_KEY) or {}).get("name"),
            "Subject 7-Delta")
        self.assertFalse((ws.WORLDS_DIR / "world-3.json").exists())

    def test_a_saved_look_survives_the_play_reset(self):
        """Create types Look. Play then resets. That used to reload the
        World snapshot and wipe the sheet, so the compiled preview was a lie."""
        exp = xs.get_experience()
        world = exp["worlds"][0]
        xs.ensure_world_snapshot(world)
        xs.save_experience(exp)
        token = "cobalt cheek scar proof-look"
        gi.save_spec({gi.CHARACTER_KEY: {
            "enabled": True,
            "name": "Wren Alvarez",
            "appearance": token,
        }})
        xs.persist_world_snapshot(world["id"])
        gi.save_spec({gi.CHARACTER_KEY: {
            "enabled": True,
            "name": "Scratch",
            "appearance": "this must not survive the reset",
        }})
        self.assertIn("Scratch", gi.protagonist_line())
        engine.apply_experience_start({}, session_id="graph-proof")
        self.assertIn(token, gi.protagonist_line())
        self.assertIn(token, gi.live_prefix())
        prompt = engine.build_image_prompt("Wren stands at the fence.")
        self.assertIn(token, prompt)

    def test_transition_round_trip(self):
        exp = xs.add_world("World 2")
        a, b = exp["worlds"][0]["id"], exp["worlds"][1]["id"]
        exp = xs.add_transition(a, b, {"type": "turn_count", "turns": 6})
        self.assertEqual(len(exp["transitions"]), 1)
        t = exp["transitions"][0]
        self.assertEqual(t["from"], a)
        self.assertEqual(t["to"], b)
        self.assertEqual(t["condition"], {"type": "turn_count", "turns": 6})
        exp = xs.update_transition(t["id"], {"condition": {"type": "game_over"}})
        self.assertEqual(exp["transitions"][0]["condition"]["type"], "game_over")
        exp = xs.remove_transition(t["id"])
        self.assertEqual(exp["transitions"], [])

    def test_duplicate_transition_is_idempotent(self):
        exp = xs.add_world("World 2")
        a, b = exp["worlds"][0]["id"], exp["worlds"][1]["id"]
        xs.add_transition(a, b, {"type": "turn_count", "turns": 4})
        again = xs.add_transition(a, b, {"type": "turn_count", "turns": 9})
        self.assertEqual(len(again["transitions"]), 1)
        self.assertEqual(again["transitions"][0]["condition"]["turns"], 4)

    def test_cannot_remove_last_world(self):
        exp = xs.get_experience()
        with self.assertRaises(ValueError):
            xs.remove_world(exp["worlds"][0]["id"])

    def test_matched_transition_is_first_true_edge(self):
        exp = xs.add_world("World 2")
        a, b = exp["worlds"][0]["id"], exp["worlds"][1]["id"]
        xs.add_transition(a, b, {"type": "turn_count", "turns": 3})
        exp = xs.get_experience()
        self.assertIsNone(xs.matched_transition(exp, a, world_turn_count=2))
        hit = xs.matched_transition(exp, a, world_turn_count=3)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["to"], b)

    def test_world_layout_round_trips(self):
        exp = xs.add_world("World 2", x=120, y=-40)
        placed = exp["worlds"][-1]
        self.assertEqual(placed["x"], 120)
        self.assertEqual(placed["y"], -40)
        exp = xs.move_world(placed["id"], -10, 80)
        moved = xs.world_by_id(exp, placed["id"])
        self.assertEqual(moved["x"], -10)
        self.assertEqual(moved["y"], 80)
        again = xs.get_experience()
        self.assertEqual(xs.world_by_id(again, placed["id"])["y"], 80)

    def test_experience_layout_round_trips(self):
        exp = xs.move_experience(80, -30)
        self.assertEqual(exp["x"], 80)
        self.assertEqual(exp["y"], -30)
        again = xs.get_experience()
        self.assertEqual(again["x"], 80)
        self.assertEqual(again["y"], -30)
        worlds = again["worlds"]
        self.assertTrue(worlds)

    def test_add_world_defaults_to_new_world(self):
        exp = xs.add_world()
        names = [w["name"] for w in exp["worlds"]]
        self.assertIn("New World", names)
        self.assertIn("World", names)

    def test_add_world_without_clone_is_not_somewhere(self):
        exp = xs.add_world("Blank Sit")
        slug = exp["worlds"][-1]["slug"]
        brief = (ws.get_world(slug).get("prompts") or {}).get("world_initial_state") or ""
        self.assertNotIn("Horizon", brief)
        self.assertNotIn("Four Corners", brief)
        self.assertIn("third person", brief.lower())

    def test_add_world_clone_from_keeps_source_brief(self):
        exp = xs.get_experience()
        src = exp["worlds"][0]
        xs.ensure_world_snapshot(src)
        xs.save_experience(exp)
        path = ws.WORLDS_DIR / f"{src['slug']}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("prompts", {})["world_initial_state"] = "Horizon fence test bible."
        path.write_text(json.dumps(data), encoding="utf-8")
        exp = xs.add_world("Forked", clone_from=src["id"])
        brief = (ws.get_world(exp["worlds"][-1]["slug"]).get("prompts") or {}).get(
            "world_initial_state") or ""
        self.assertIn("Horizon", brief)

    def test_rename_keeps_a_blurb(self):
        exp = xs.add_world("Yard")
        wid = exp["worlds"][-1]["id"]
        exp = xs.rename_world(wid, "The Yard", blurb="A chain-link lot at dusk.")
        w = xs.world_by_id(exp, wid)
        self.assertEqual(w["name"], "The Yard")
        self.assertEqual(w["blurb"], "A chain-link lot at dusk.")
        again = xs.get_experience()
        self.assertEqual(xs.world_by_id(again, wid)["blurb"], "A chain-link lot at dusk.")
        xs.rename_world(wid, "The Yard", blurb="")
        self.assertNotIn("blurb", xs.world_by_id(xs.get_experience(), wid))


class TestEngineTurnCountTransition(_IsolatedGraph):
    def _stitched(self, turns=2):
        exp = xs.add_world("World 2")
        a, b = exp["worlds"][0], exp["worlds"][1]
        xs.add_transition(a["id"], b["id"], {"type": "turn_count", "turns": turns})
        return xs.get_experience(), a, b

    def test_no_switch_before_threshold(self):
        exp, a, b = self._stitched(3)
        state = {
            "experience_id": exp["id"],
            "experience_world_id": a["id"],
            "world_turn_count": 0,
            "world_prompt": "old",
        }
        info = engine._tick_world_and_maybe_transition(state, "t", player_alive=True)
        self.assertIsNone(info)
        self.assertEqual(state["world_turn_count"], 1)
        self.assertEqual(state["experience_world_id"], a["id"])

    def test_switch_after_n_turns(self):
        exp, a, b = self._stitched(2)
        state = {
            "experience_id": exp["id"],
            "experience_world_id": a["id"],
            "world_turn_count": 1,
            "world_prompt": "old",
            "player_state": {"alive": True},
        }
        info = engine._tick_world_and_maybe_transition(state, "t", player_alive=True)
        self.assertIsNotNone(info)
        self.assertEqual(state["experience_world_id"], b["id"])
        self.assertEqual(state["world_turn_count"], 0)
        self.assertTrue(state["pending_world_transition"])
        self.assertEqual(info["to"]["id"], b["id"])

    def test_death_transition_revives_into_target(self):
        exp = xs.add_world("After")
        a, b = exp["worlds"][0], exp["worlds"][1]
        xs.add_transition(a["id"], b["id"], {"type": "game_over"})
        state = {
            "experience_world_id": a["id"],
            "world_turn_count": 0,
            "world_prompt": "old",
            "player_state": {"alive": False},
        }
        info = engine._tick_world_and_maybe_transition(state, "t", player_alive=False)
        self.assertIsNotNone(info)
        self.assertEqual(state["experience_world_id"], b["id"])

    def test_apply_experience_start_loads_start_world_when_stitched(self):
        exp, a, b = self._stitched(8)
        xs.set_start_world(b["id"])
        state = engine.apply_experience_start({}, "t")
        self.assertEqual(state["experience_world_id"], b["id"])
        self.assertEqual(state["world_turn_count"], 0)

    def test_apply_experience_start_loads_a_one_world_slug(self):
        """Play locks to a bound World even when the graph has no stitches."""
        ws.save_world("Fence", note="demo", slug="fence")
        xs.save_experience({
            "name": "SOMEWHERE",
            "worlds": [{"id": "w1", "name": "Fence", "slug": "fence"}],
            "start_world": "w1",
            "transitions": [],
        })
        with patch.object(ws, "load_world", wraps=ws.load_world) as load:
            state = engine.apply_experience_start({}, "t")
        load.assert_called_once_with("fence")
        self.assertEqual(state["experience_world_id"], "w1")
        self.assertEqual(state["world_turn_count"], 0)

    def test_status_helper_names_the_live_world(self):
        _exp, a, b = self._stitched(8)
        xs.set_start_world(b["id"])
        state = engine.apply_experience_start({}, "t")
        import api as app_api
        self.assertEqual(app_api._status_world_name(state), b["name"])
        self.assertEqual(app_api._status_world_name({"experience_world_id": a["id"]}), a["name"])
        self.assertEqual(app_api._status_world_name({}), "")


class TestExperienceCatalog(_IsolatedGraph):
    _PNG = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
    )

    def test_list_always_includes_default(self):
        items = xs.list_experiences()
        self.assertEqual([row["id"] for row in items], ["default"])
        self.assertEqual(items[0]["name"], "Untitled Experience")
        self.assertTrue(items[0]["active"])

    def test_list_finds_every_file_and_keeps_default_first(self):
        xs.save_experience({"name": "Night Mesa", "worlds": [
            {"id": "w1", "name": "Yard", "slug": ""},
        ], "start_world": "w1", "transitions": []}, slug="night-mesa")
        ids = [row["id"] for row in xs.list_experiences()]
        self.assertEqual(ids[0], "default")
        self.assertIn("night-mesa", ids)
        night = next(row for row in xs.list_experiences() if row["id"] == "night-mesa")
        self.assertEqual(night["name"], "Night Mesa")
        self.assertFalse(night["active"])

    def test_set_active_switches_get_experience(self):
        xs.save_experience({"name": "Night Mesa", "worlds": [
            {"id": "w1", "name": "Yard", "slug": ""},
        ], "start_world": "w1", "transitions": []}, slug="night-mesa")
        self.assertEqual(xs.set_active("night-mesa"), "night-mesa")
        self.assertEqual(xs.get_active_slug(), "night-mesa")
        self.assertEqual(xs.get_experience()["name"], "Night Mesa")
        self.assertEqual(xs.get_experience()["id"], "night-mesa")

    def test_set_active_unknown_slug_raises(self):
        with self.assertRaises(KeyError):
            xs.set_active("no-such-game")

    def test_rename_keeps_the_file_slug(self):
        xs.save_experience({"name": "Night Mesa", "worlds": [
            {"id": "w1", "name": "Yard", "slug": ""},
        ], "start_world": "w1", "transitions": []}, slug="night-mesa")
        out = xs.rename_experience("Red Mesa", "night-mesa")
        self.assertEqual(out["id"], "night-mesa")
        self.assertEqual(out["name"], "Red Mesa")
        self.assertTrue((xs.EXPERIENCES_DIR / "night-mesa.json").exists())
        self.assertFalse((xs.EXPERIENCES_DIR / "red-mesa.json").exists())
        self.assertEqual(xs.get_experience("night-mesa")["name"], "Red Mesa")

    def test_create_makes_a_new_file_and_activates(self):
        xs.get_experience("default")
        out = xs.create_experience("Dawn Yard")
        self.assertEqual(out["name"], "Dawn Yard")
        self.assertEqual(out["id"], "dawn-yard")
        self.assertTrue((xs.EXPERIENCES_DIR / "dawn-yard.json").exists())
        self.assertEqual(xs.get_active_slug(), "dawn-yard")
        self.assertEqual(xs.get_experience()["id"], "dawn-yard")
        self.assertEqual(len(out["worlds"]), 1)
        self.assertEqual(out["worlds"][0]["name"], "Dawn Yard")
        self.assertTrue(out["worlds"][0].get("slug"))

    def test_create_without_clone_is_a_fresh_default(self):
        xs.save_experience({"name": "Night Mesa", "worlds": [
            {"id": "w1", "name": "Yard", "slug": "yard"},
            {"id": "w2", "name": "Ridge", "slug": "ridge"},
        ], "start_world": "w1", "transitions": [
            {"from": "w1", "to": "w2", "condition": {"type": "turn_count", "turns": 3}},
        ]}, slug="night-mesa")
        xs.set_active("night-mesa")
        out = xs.create_experience("Blank Page")
        self.assertEqual(out["id"], "blank-page")
        self.assertEqual(len(out["worlds"]), 1)
        self.assertEqual(out["worlds"][0]["name"], "Blank Page")
        self.assertEqual(out["transitions"], [])
        self.assertNotEqual(out["worlds"][0]["id"], "w1")
        self.assertEqual(xs.get_active_slug(), "blank-page")

    def test_preview_prefers_the_newest_stored_still(self):
        img_dir = xs.SESSIONS_DIR / "default" / "images"
        img_dir.mkdir(parents=True)
        first = img_dir / "a.png"
        last = img_dir / "b.png"
        first.write_bytes(self._PNG)
        last.write_bytes(self._PNG)
        older = 1_700_000_000
        import os
        os.utime(first, (older, older))
        os.utime(last, (older + 10, older + 10))
        (xs.SESSIONS_DIR / "default" / "state.json").write_text(
            json.dumps({"experience_id": "default"}), encoding="utf-8")
        self.assertEqual(xs.preview_url_for("default"), "/images/b.png")


class TestExperienceLore(_IsolatedGraph):
    def test_notes_round_trip_and_brief(self):
        lore = xs.set_lore_notes("Horizon buried the gate in 1987.")
        self.assertIn("1987", lore["notes"])
        brief = xs.lore_brief()
        self.assertIn("HISTORICAL BACKGROUND", brief)
        self.assertIn("1987", brief)
        self.assertIn("1987", xs.with_lore("You are at the fence."))

    def test_disabled_lore_is_silent(self):
        xs.set_lore_notes("Secret vault under the mesa.", enabled=False)
        self.assertEqual(xs.lore_brief(), "")
        self.assertEqual(xs.with_lore("Fence."), "Fence.")

    def test_text_upload_lands_on_disk_and_in_the_brief(self):
        added = xs.add_lore_document(name="timeline.md", text="March 1987. Building C-7 sealed.")
        docs = added["lore"]["documents"]
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["kind"], "text")
        self.assertTrue((xs.lore_dir() / docs[0]["file"]).is_file())
        self.assertIn("Building C-7", xs.lore_brief())

    def test_image_upload_gets_a_url(self):
        png = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
            "/x8AAwMCAO+X2nkAAAAASUVORK5CYII="
        )
        added = xs.add_lore_document(name="map.png", image="data:image/png;base64," + png)
        doc = added["document"]
        self.assertEqual(doc["kind"], "image")
        self.assertTrue(doc["url"].startswith("/api/experience/lore/"))
        self.assertTrue(xs.lore_file_path(xs.get_active_slug(), doc["id"]).is_file())

    def test_remove_document(self):
        added = xs.add_lore_document(name="memo.txt", text="Do not enter after dark.")
        did = added["document"]["id"]
        xs.remove_lore_document(did)
        self.assertEqual(xs.get_lore()["documents"], [])
        self.assertNotIn("after dark", xs.lore_brief())

    def test_clone_copies_lore_files(self):
        xs.set_lore_notes("The red biome woke in winter.")
        xs.add_lore_document(name="cast.md", text="Fleece was never supposed to come back.")
        cloned = xs.create_experience("Night Mesa", clone_from=xs.get_active_slug())
        self.assertIn("winter", cloned["lore"]["notes"])
        self.assertEqual(len(cloned["lore"]["documents"]), 1)
        self.assertIn("Fleece", xs.lore_brief(cloned["id"]))

    def test_reset_world_prompt_includes_lore(self):
        xs.set_lore_notes("The Gate is a hole they named so it would sound like a door.")
        seed = engine._intro_world_seed("")
        self.assertIn("HISTORICAL BACKGROUND", seed)
        self.assertIn("The Gate is a hole", seed)

    def test_with_lore_does_not_duplicate_the_bible(self):
        bible = "This is a 1993 analog-horror adventure at The Gate under Horizon."
        xs.set_lore_notes(bible)
        once = xs.with_lore("You are at the fence.")
        self.assertEqual(once.count("HISTORICAL BACKGROUND"), 1)
        self.assertEqual(once.count("1993 analog-horror"), 1)
        self.assertEqual(xs.with_lore(once).count("HISTORICAL BACKGROUND"), 1)
        self.assertEqual(xs.with_lore(bible).count("1993 analog-horror"), 1)

    def test_apply_lore_to_prompt_prepends_once(self):
        xs.set_lore_notes("The Gate is a hole they named so it would sound like a door.")
        prompt = xs.apply_lore_to_prompt("Narrate the fence.")
        self.assertTrue(prompt.startswith("HISTORICAL BACKGROUND"))
        self.assertIn("The Gate is a hole", prompt)
        self.assertIn("Narrate the fence.", prompt)
        self.assertEqual(xs.apply_lore_to_prompt(prompt).count("HISTORICAL BACKGROUND"), 1)

    def test_empty_lore_inherits_the_start_world_bible(self):
        from prompts_store import PROMPTS, save_prompts_bulk
        save_prompts_bulk({
            "world_initial_state": (
                "Horizon buried The Gate in 1987. Four Corners. Jason Fleece."
            )
        })
        PROMPTS._mtime = None
        PROMPTS._reload(force=True)
        ws.save_world("Mesa", slug="mesa")
        exp = xs.get_experience()
        exp["start_world"] = exp["worlds"][0]["id"]
        exp["worlds"][0]["slug"] = "mesa"
        exp["lore"] = xs.default_lore()
        xs.save_experience(exp)
        brief = xs.lore_brief()
        self.assertIn("HISTORICAL BACKGROUND", brief)
        self.assertIn("Horizon buried The Gate", brief)
        self.assertIn("Jason Fleece", brief)
        self.assertEqual(xs.get_lore().get("source"), "world")

    def test_inline_document_feeds_the_brief(self):
        exp = xs.get_experience()
        exp["lore"] = xs.normalize_lore({
            "enabled": True,
            "notes": "",
            "documents": [{
                "id": "lhorizon01",
                "name": "horizon_bible.md",
                "kind": "text",
                "file": "",
                "chars": 0,
                "text": "The red biome woke under Building C-7.",
            }],
        })
        xs.save_experience(exp)
        self.assertIn("The red biome woke under Building C-7.", xs.lore_brief())
        self.assertIn("horizon_bible.md", xs.lore_brief())


class TestShippedSomewhereLore(unittest.TestCase):
    """The flagship Experience must ship the Horizon bible on its Lore node."""

    def test_somewhere_json_holds_the_horizon_bible(self):
        path = Path(__file__).resolve().parent / "experiences" / "somewhere.json"
        self.assertTrue(path.is_file(), "experiences/somewhere.json is the shipped Play door")
        exp = json.loads(path.read_text(encoding="utf-8"))
        lore = exp.get("lore") or {}
        blob = str(lore.get("notes") or "")
        for doc in lore.get("documents") or []:
            blob += "\n" + str(doc.get("text") or "")
        for marker in ("Horizon", "The Gate", "Four Corners", "1993", "Jason"):
            self.assertIn(marker, blob, f"shipped lore is missing {marker!r}")
        self.assertNotEqual(lore.get("enabled"), False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
