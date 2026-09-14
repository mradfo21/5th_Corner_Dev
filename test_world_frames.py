"""
Tests for the per-World first-frame cache.

Never touches the network. Redirects worlds/, experiences/, and prompts into
a temp dir so a run cannot rewrite live authoring data or fire a paid render.

Run: python -m unittest test_world_frames -v
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
import world_frames as wf
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
        }
        xs.EXPERIENCES_DIR = tmp / "experiences"
        xs.SESSIONS_DIR = tmp / "sessions"
        ws.WORLDS_DIR = tmp / "worlds"
        ps.PROMPTS_PATH = tmp / "simulation_prompts.json"
        ps.DEFAULTS_PATH = tmp / "simulation_prompts.defaults.json"
        gi.REFERENCES_DIR = tmp / "refs"
        xs.EXPERIENCES_DIR.mkdir()
        xs.SESSIONS_DIR.mkdir()
        ws.WORLDS_DIR.mkdir()
        gi.REFERENCES_DIR.mkdir()
        payload = {
            "world_initial_state": "You arrive.",
            "gemini_text_to_image_instructions": "SCENE: {prompt}",
            "gemini_image_to_image_instructions": "SHOW: {prompt}",
            "image_art_direction": "grain",
            "image_camera_rules": "eye level",
            "image_negative_prompt": "text overlays",
        }
        payload.update(gi.default_spec())
        for path in (ps.PROMPTS_PATH, ps.DEFAULTS_PATH):
            path.write_text(json.dumps(payload), encoding="utf-8")
        ps.PROMPTS._mtime = None
        ps.PROMPTS._last_check = 0.0
        ps.PROMPTS._reload(force=True)
        self._images_patch = patch.object(wf, "_images_enabled", return_value=False)
        self._images_patch.start()
        wf._last_all_kick = 0.0
        with wf._lock:
            wf._generating.clear()
            for t in list(wf._timers.values()):
                try:
                    t.cancel()
                except Exception:
                    pass
            wf._timers.clear()

    def _rendered_world(self, name: str = "Yard") -> dict:
        """A World whose frame is a real opening shot.

        The harness runs with images off, so ensure() can only produce a mint
        placeholder. Anything testing what a player sees on Start needs a
        frame that claims to be rendered, because Play now refuses to open a
        run on a placeholder.
        """
        info = self._world(name)
        wf.ensure(info["slug"], wait=True)
        wf.install_from_file(info["slug"], wf.frame_path(info["slug"]),
                             wf.fingerprint_for_slug(info["slug"]),
                             source="generated")
        return info

    def tearDown(self):
        self._images_patch.stop()
        with wf._lock:
            for t in list(wf._timers.values()):
                try:
                    t.cancel()
                except Exception:
                    pass
            wf._timers.clear()
            wf._generating.clear()
        xs.EXPERIENCES_DIR = self._orig["exp"]
        xs.SESSIONS_DIR = self._orig["sessions"]
        ws.WORLDS_DIR = self._orig["worlds"]
        ps.PROMPTS_PATH = self._orig["prompts"]
        ps.DEFAULTS_PATH = self._orig["defaults"]
        gi.REFERENCES_DIR = self._orig["refs"]
        ps.PROMPTS._mtime = None
        ps.PROMPTS._last_check = 0.0
        ps.PROMPTS._reload(force=True)
        self._tmpdir.cleanup()

    def _world(self, name="World", **setting):
        spec = dict(gi.SETTING_DEFAULTS)
        spec.update({"enabled": True, "name": name, "summary": "A place", "opening_shot": "Dawn."})
        spec.update(setting)
        ps.save_prompts_bulk({gi.SETTING_KEY: spec, "world_initial_state": "You arrive at " + name})
        return ws.save_world(name)


class TestSteerPrompt(_Isolated):
    def test_clip_keeps_short_text(self):
        self.assertEqual(wf.clip_steer_prompt("  Dawn at the yard.  "), "Dawn at the yard.")

    def test_clip_caps_long_text(self):
        blob = "word " * 800
        clipped = wf.clip_steer_prompt(blob)
        self.assertLessEqual(len(clipped), wf.STEER_PROMPT_MAX)
        self.assertTrue(clipped.endswith("..."))

    def test_scene_prompt_uses_opening_shot(self):
        info = self._world("Yard")
        prompts = ws.get_world(info["slug"])["prompts"]
        text = wf.scene_prompt_for(prompts)
        self.assertIn("Dawn", text)

    def test_ensure_stores_the_generation_prompt(self):
        info = self._world("Yard")
        rec = wf.ensure(info["slug"], wait=True)
        self.assertTrue(rec.get("prompt"))
        self.assertIn("Dawn", rec["prompt"])


class TestFingerprint(_Isolated):
    def test_same_prompts_hash_the_same(self):
        a = wf.fingerprint({"setting_reference": {"name": "A"}, "image_art_direction": "x"})
        b = wf.fingerprint({"setting_reference": {"name": "A"}, "image_art_direction": "x"})
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)

    def test_opening_shot_change_dirties(self):
        info = self._world("Kettle")
        first = wf.fingerprint_for_slug(info["slug"])
        data = ws.get_world(info["slug"])
        setting = dict(data["prompts"][gi.SETTING_KEY])
        setting["opening_shot"] = "Low tide, the tanker filling the right of frame"
        data["prompts"][gi.SETTING_KEY] = setting
        path = ws.WORLDS_DIR / f"{info['slug']}.json"
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        second = wf.fingerprint_for_slug(info["slug"])
        self.assertNotEqual(first, second)

    def test_choice_copy_does_not_dirty(self):
        prompts = {
            "setting_reference": {"name": "A"},
            "player_choice_generation_instructions": "old",
        }
        a = wf.fingerprint(prompts)
        prompts["player_choice_generation_instructions"] = "new"
        self.assertEqual(a, wf.fingerprint(prompts))


class TestEnsure(_Isolated):
    def test_mock_ensure_writes_a_png_and_is_ready(self):
        info = self._world("Yard")
        rec = wf.ensure(info["slug"], wait=True)
        self.assertEqual(rec["status"], "ready")
        self.assertTrue(rec["url"].startswith("/api/worlds/" + info["slug"] + "/frame"))
        self.assertTrue(Path(rec["path"]).is_file())
        self.assertGreater(Path(rec["path"]).stat().st_size, 32)
        again = wf.ensure(info["slug"], wait=True)
        self.assertEqual(again["status"], "ready")
        self.assertFalse(again["dirty"])

    def test_dirty_after_snapshot_change(self):
        info = self._world("Yard")
        wf.ensure(info["slug"], wait=True)
        data = ws.get_world(info["slug"])
        setting = dict(data["prompts"][gi.SETTING_KEY])
        setting["opening_shot"] = "A different dawn"
        data["prompts"][gi.SETTING_KEY] = setting
        (ws.WORLDS_DIR / f"{info['slug']}.json").write_text(
            json.dumps(data, indent=2), encoding="utf-8")
        rec = wf.record(info["slug"])
        self.assertTrue(rec["dirty"])
        self.assertEqual(rec["status"], "dirty")
        self.assertTrue(rec["url"], "stale still stays visible")

    def test_url_busts_when_the_file_is_rewritten(self):
        info = self._world("Yard")
        first = wf.ensure(info["slug"], wait=True)
        self.assertIn("t=", first["url"])
        data = ws.get_world(info["slug"])
        setting = dict(data["prompts"][gi.SETTING_KEY])
        setting["opening_shot"] = "A later dusk"
        data["prompts"][gi.SETTING_KEY] = setting
        (ws.WORLDS_DIR / f"{info['slug']}.json").write_text(
            json.dumps(data, indent=2), encoding="utf-8")
        again = wf.ensure(info["slug"], wait=True)
        self.assertNotEqual(first["url"], again["url"])

    def test_mock_does_not_replace_a_generated_still(self):
        """A no-key pass used to stamp a mint placeholder over a real frame
        and call that ready, so the Experience tiles went empty."""
        info = self._world("Yard")
        rec = wf.ensure(info["slug"], wait=True)
        real = Path(rec["path"])
        payload = real.read_bytes() + b"REAL"
        real.write_bytes(payload)
        wf._write_meta(info["slug"], {
            "fingerprint": wf.fingerprint_for_slug(info["slug"]),
            "updated": 1,
            "source": "generated",
        })
        again = wf.ensure(info["slug"], wait=True)
        self.assertEqual(real.read_bytes(), payload)
        self.assertEqual(again["source"], "generated")
        data = ws.get_world(info["slug"])
        setting = dict(data["prompts"][gi.SETTING_KEY])
        setting["opening_shot"] = "A later dusk"
        data["prompts"][gi.SETTING_KEY] = setting
        (ws.WORLDS_DIR / f"{info['slug']}.json").write_text(
            json.dumps(data, indent=2), encoding="utf-8")
        stale = wf.ensure(info["slug"], wait=True)
        self.assertEqual(real.read_bytes(), payload)
        self.assertEqual(stale["source"], "generated")

    def test_placeholder_regens_once_images_can_run(self):
        info = self._world("Yard")
        rec = wf.ensure(info["slug"], wait=True)
        self.assertEqual(rec["source"], "placeholder")
        called = {"n": 0}

        def fake_paid(slug, prompts, fp):
            called["n"] += 1
            return wf.install_from_file(
                slug, rec["path"], fp, source="generated")["path"]

        with patch.object(wf, "_images_enabled", return_value=True), \
             patch.object(wf, "_generate_paid", side_effect=fake_paid):
            again = wf.ensure(info["slug"], wait=True)
        self.assertEqual(called["n"], 1)
        self.assertEqual(again["source"], "generated")

    def test_new_plate_recaches_the_circle(self):
        info = self._world("Yard")
        wf.ensure(info["slug"], wait=True)
        before = Path(wf.frame_path(info["slug"])).read_bytes()
        plate = gi.save_reference(
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
            "setting",
            "new-level.png",
        )
        data = ws.get_world(info["slug"])
        setting = dict(data["prompts"][gi.SETTING_KEY])
        setting["reference_images"] = [plate["id"]]
        data["prompts"][gi.SETTING_KEY] = setting
        (ws.WORLDS_DIR / f"{info['slug']}.json").write_text(
            json.dumps(data, indent=2), encoding="utf-8")
        rec = wf.ensure(info["slug"], wait=True)
        after = Path(rec["path"]).read_bytes()
        self.assertNotEqual(before, after)
        self.assertEqual(rec["source"], "plate")
        self.assertEqual(rec["status"], "ready")

    def test_paid_dirty_keeps_the_generated_still_while_rendering(self):
        """A reference plate is input. The circle has to keep the last scene
        until a generated opening shot lands — not flash the upload."""
        info = self._world("Yard")
        wf.ensure(info["slug"], wait=True)
        before = Path(wf.frame_path(info["slug"])).read_bytes()
        plate = gi.save_reference(
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
            "setting",
            "take.png",
        )
        data = ws.get_world(info["slug"])
        setting = dict(data["prompts"][gi.SETTING_KEY])
        setting["reference_images"] = [plate["id"]]
        setting["summary"] = "A rusted radio on a kitchen table"
        data["prompts"][gi.SETTING_KEY] = setting
        (ws.WORLDS_DIR / f"{info['slug']}.json").write_text(
            json.dumps(data, indent=2), encoding="utf-8")
        with patch.object(wf, "_images_enabled", return_value=True), \
             patch.object(wf, "_generate_paid", return_value=None) as paid:
            rec = wf.ensure(info["slug"], wait=True)
        after = Path(wf.frame_path(info["slug"])).read_bytes()
        self.assertEqual(before, after)
        self.assertNotEqual(rec["source"], "plate")
        self.assertTrue(paid.called)
        self.assertTrue(rec["dirty"] or rec["status"] in ("dirty", "generating"))

    def test_paid_gen_uses_the_worlds_own_sheet(self):
        info = self._world("Yard")
        captured = {}

        def fake_gen(*args, **kwargs):
            captured["caption"] = args[0] if args else ""
            captured["kwargs"] = kwargs
            return (None, "", None)

        prompts = ws.get_world(info["slug"])["prompts"]
        with patch.object(engine, "_gen_image", side_effect=fake_gen):
            wf._generate_paid(info["slug"], prompts, "fp")
        self.assertIn("identity_spec", captured["kwargs"])
        spec = captured["kwargs"]["identity_spec"]
        self.assertEqual(spec[gi.SETTING_KEY]["name"], "Yard")
        self.assertIn("Dawn", captured["caption"])


class TestAnnotate(_Isolated):
    def test_experience_worlds_carry_frame_url(self):
        info = self._world("Yard")
        wf.ensure(info["slug"], wait=True)
        exp = xs.get_experience()
        exp["worlds"][0]["slug"] = info["slug"]
        stamped = wf.annotate_experience(exp)
        self.assertTrue(stamped["worlds"][0]["frame_url"])
        self.assertEqual(stamped["worlds"][0]["frame_status"], "ready")
        self.assertTrue(stamped["worlds"][0]["frame_prompt"])
        self.assertIn("Dawn", stamped["worlds"][0]["frame_prompt"])

    def test_preview_url_prefers_cached_frame(self):
        info = self._world("Yard")
        wf.ensure(info["slug"], wait=True)
        exp = xs.get_experience()
        exp["worlds"][0]["slug"] = info["slug"]
        xs.save_experience(exp)
        url = xs.preview_url_for(xs.ACTIVE_SLUG, xs.get_experience())
        self.assertTrue(url)
        self.assertIn("/api/worlds/" + info["slug"] + "/frame", url)

    def test_preview_url_skips_a_mint_placeholder_when_images_can_run(self):
        info = self._world("Yard")
        wf.ensure(info["slug"], wait=True)
        self.assertEqual(wf.record(info["slug"])["source"], "placeholder")
        exp = xs.get_experience()
        exp["worlds"][0]["slug"] = info["slug"]
        xs.save_experience(exp)
        img_dir = xs.SESSIONS_DIR / "default" / "images"
        img_dir.mkdir(parents=True)
        (img_dir / "last.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 40)
        (xs.SESSIONS_DIR / "default" / "state.json").write_text(
            json.dumps({"experience_id": xs.ACTIVE_SLUG}), encoding="utf-8")
        with patch.object(wf, "_images_enabled", return_value=True):
            url = xs.preview_url_for(xs.ACTIVE_SLUG, xs.get_experience())
        self.assertEqual(url, "/images/last.png")


class TestInject(_Isolated):
    def test_reset_injects_cached_scene_image_and_skips_spawn_when_clean(self):
        info = self._rendered_world("Yard")
        rec = wf.record(info["slug"])
        self.assertEqual(rec["status"], "ready")
        exp = xs.get_experience()
        exp["worlds"][0]["slug"] = info["slug"]
        xs.save_experience(exp)
        items = []
        state = {"experience_world_id": exp["worlds"][0]["id"], "tape_frames": []}
        kwargs = {
            "caption": "You arrive.",
            "dispatch": "Dawn at the yard.",
            "choice": "Initialize Simulation",
            "world_prompt": "You arrive.",
        }
        with patch.object(engine, "_load_history", return_value=[]), \
             patch.object(engine, "_save_history"), \
             patch.object(engine, "_sync_ambient_history"), \
             patch.object(engine, "_spawn_cached_opening_vision"):
            need_spawn = engine._apply_cached_opening_frame(
                "wf-test", state, items, kwargs)
        self.assertFalse(need_spawn)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["type"], "scene_image")
        self.assertEqual(items[0]["image_url"], rec["url"])
        self.assertTrue((items[0].get("metadata") or {}).get("cached_opening"))
        self.assertEqual(state["current_image_url"], rec["url"])
        self.assertTrue(state["current_image_prompt"])
        self.assertTrue((items[0].get("metadata") or {}).get("image_prompt"))
        self.assertEqual(state["current_image_prompt"],
                         (items[0].get("metadata") or {}).get("image_prompt"))

    def test_a_placeholder_is_not_an_opening_frame(self):
        """world_frames drops a mint square in whenever a frame goes missing
        and leaves it there if the render fails. Opening a run on it is the
        flat green screen players get when they press Start."""
        info = self._world("Yard")
        with patch.object(wf, "_images_enabled", return_value=False):
            rec = wf.ensure(info["slug"], wait=True)
        self.assertEqual(rec["source"], "placeholder")
        self.assertFalse(wf.is_real_still(rec))
        exp = xs.get_experience()
        exp["worlds"][0]["slug"] = info["slug"]
        xs.save_experience(exp)
        items = []
        with patch.object(engine, "_load_history", return_value=[]), \
             patch.object(engine, "_save_history"), \
             patch.object(engine, "_sync_ambient_history"), \
             patch.object(engine, "_spawn_cached_opening_vision"), \
             patch.object(wf, "ensure"):
            need_spawn = engine._apply_cached_opening_frame(
                "wf-test",
                {"experience_world_id": exp["worlds"][0]["id"], "tape_frames": []},
                items, {"caption": "x", "dispatch": "y"})
        self.assertTrue(need_spawn, "a placeholder must not stand in for the opening")
        self.assertEqual(items, [], "nothing green should reach the feed")

    def test_a_rendered_frame_is_still_good_enough_to_open_on(self):
        info = self._rendered_world("Yard")
        self.assertTrue(wf.is_real_still(wf.record(info["slug"])))

    def test_missing_cache_asks_for_spawn(self):
        items = []
        state = {"experience_world_id": "", "tape_frames": []}
        need_spawn = engine._apply_cached_opening_frame(
            "wf-test", state, items, {"caption": "x", "dispatch": "y"})
        self.assertTrue(need_spawn)
        self.assertEqual(items, [])


class TestTheOpeningFrameIsActuallyLookedAt(_Isolated):
    """A world frame lives at one fixed path and is regenerated in place.

    Everything that reads the scene does so through vision, so if vision
    answers from the name of the file rather than its contents, the game
    narrates a picture that is no longer on screen.
    """

    def _reply(self, description):
        body = {"candidates": [{"content": {"parts": [{"text":
            f"TIME: dusk\nCOLOR: grey\nSETTING: outdoor-other\n"
            f"SPATIAL: Ahead: nothing\nDESCRIPTION: {description}"}]}}]}
        resp = unittest.mock.Mock()
        resp.status_code = 200
        resp.json.return_value = body
        return resp

    def setUp(self):
        super().setUp()
        engine._vision_cache.clear()
        self.img = Path(self._tmpdir.name) / "world.frame.png"
        self.img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"first" * 20)

    def test_replacing_the_file_gets_a_fresh_look(self):
        with patch("requests.post", return_value=self._reply("a red barn")):
            first = engine._vision_analyze_all(str(self.img))
        self.assertEqual(first["description"], "a red barn")

        # Same path, different picture — exactly what a frame regen does.
        self.img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"second" * 30)
        with patch("requests.post", return_value=self._reply("a flooded pit")):
            second = engine._vision_analyze_all(str(self.img))
        self.assertEqual(second["description"], "a flooded pit")

    def test_an_unchanged_file_is_still_only_looked_at_once(self):
        with patch("requests.post", return_value=self._reply("a red barn")) as post:
            engine._vision_analyze_all(str(self.img))
            engine._vision_analyze_all(str(self.img))
        self.assertEqual(post.call_count, 1)

    def test_a_downsample_left_over_from_an_older_render_is_ignored(self):
        small = self.img.with_name("world.frame_small.png")
        small.write_bytes(b"\x89PNG\r\n\x1a\n" + b"stale" * 20)
        import os
        old = small.stat().st_mtime - 500
        os.utime(small, (old, old))
        with patch("requests.post", return_value=self._reply("the real frame")) as post:
            engine._vision_analyze_all(str(self.img))
        sent = post.call_args.kwargs["json"]
        blob = json.dumps(sent)
        import base64
        self.assertIn(base64.b64encode(self.img.read_bytes()).decode(), blob)
        self.assertNotIn(base64.b64encode(small.read_bytes()).decode(), blob)

    def test_the_cached_opening_does_not_go_into_history_unseen(self):
        info = self._rendered_world("Yard")
        rec = wf.record(info["slug"])
        self.assertEqual(rec["status"], "ready")
        exp = xs.get_experience()
        exp["worlds"][0]["slug"] = info["slug"]
        xs.save_experience(exp)
        saved = []
        with patch.object(engine, "_load_history", return_value=[]), \
             patch.object(engine, "_save_history"), \
             patch.object(engine, "_sync_ambient_history"), \
             patch.object(engine, "_spawn_cached_opening_vision") as look:
            engine._apply_cached_opening_frame(
                "wf-test", {"experience_world_id": exp["worlds"][0]["id"],
                            "tape_frames": []},
                saved, {"caption": "You arrive.", "dispatch": "Dawn."})
        look.assert_called_once()
        self.assertEqual(look.call_args[0][1], rec["path"])

    def test_the_backfill_writes_what_it_saw_onto_the_opening_entry(self):
        hist = [{"choice": "Initialize Simulation", "cached_opening": True,
                 "vision_dispatch": "Jason Fleece is in frame.",
                 "vision_analysis": ""}]
        with patch.object(engine, "_vision_analyze_all", return_value={
                 "description": "A flooded pit under a grey sky.",
                 "setting": "outdoor-other", "spatial": "Ahead: water"}), \
             patch.object(engine, "_load_history", return_value=hist), \
             patch.object(engine, "_save_history") as save, \
             patch.object(engine, "get_active_session_id", return_value="other"):
            engine._spawn_cached_opening_vision("wf-test", str(self.img))
            for t in __import__("threading").enumerate():
                if t.name == "opening-vision":
                    t.join(timeout=5)
        save.assert_called_once()
        entry = save.call_args[0][0][0]
        self.assertEqual(entry["vision_analysis"],
                         "A flooded pit under a grey sky.")
        self.assertEqual(entry["spatial_compass"], "Ahead: water")
        # The dispatch is the protagonist's sheet, not the scene; leave it be.
        self.assertEqual(entry["vision_dispatch"], "Jason Fleece is in frame.")


class TestForceReset(_Isolated):
    def test_invalidate_dirties_a_ready_frame(self):
        info = self._world("Yard")
        ready = wf.ensure(info["slug"], wait=True)
        self.assertEqual(ready["status"], "ready")
        dirty = wf.invalidate(info["slug"])
        self.assertTrue(dirty["dirty"])
        self.assertNotEqual(dirty["status"], "ready")

    def test_force_reset_without_keys_restamps(self):
        info = self._world("Yard")
        first = wf.ensure(info["slug"], wait=True)
        again = wf.force_reset(info["slug"], wait=True)
        self.assertEqual(again["status"], "ready")
        self.assertEqual(again["fingerprint"], wf.fingerprint_for_slug(info["slug"]))
        self.assertTrue(again.get("path"))
        self.assertEqual(Path(first["path"]).read_bytes(), Path(again["path"]).read_bytes())

    def test_force_reset_reruns_paid_gen(self):
        info = self._world("Yard")
        wf.ensure(info["slug"], wait=True)
        called = {"n": 0}

        def fake_paid(slug, prompts, fp):
            called["n"] += 1
            return wf.install_from_file(
                slug, wf.frame_path(slug), fp, source="generated")["path"]

        with patch.object(wf, "_images_enabled", return_value=True), \
             patch.object(wf, "_generate_paid", side_effect=fake_paid):
            again = wf.force_reset(info["slug"], wait=True)
        self.assertEqual(called["n"], 1)
        self.assertEqual(again["status"], "ready")
        self.assertEqual(again["source"], "generated")


if __name__ == "__main__":
    unittest.main()
