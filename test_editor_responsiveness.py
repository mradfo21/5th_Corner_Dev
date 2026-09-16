#!/usr/bin/env python3
"""Proof that Character / Level edits compile, restage, and stay on the sheet.

The editor used to save and then do nothing the author could see: Level
copy died on the leftover off-switch, Character look was dropped if Jason
already appeared in the bible, and every save remounted the form and hid
the compiled prompt. These tests are the last mile — not "did PUT 200".
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import game_identity as gi
import prompts_store as ps

ROOT = Path(__file__).resolve().parent


class _TempPrompts(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self._tmpdir.name)
        self._orig_prompts = ps.PROMPTS_PATH
        self._orig_defaults = ps.DEFAULTS_PATH
        self._orig_sessions = gi.SESSIONS_DIR
        ps.PROMPTS_PATH = tmp / "simulation_prompts.json"
        gi.SESSIONS_DIR = tmp / "sessions"
        gi.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        ps.DEFAULTS_PATH = tmp / "simulation_prompts.defaults.json"
        payload = {
            "world_initial_state": "The year is 1993. You are Jason Fleece.",
            "image_art_direction": "1993 analog VHS, industrial horror.",
        }
        payload.update(gi.default_spec())
        for path in (ps.PROMPTS_PATH, ps.DEFAULTS_PATH):
            path.write_text(json.dumps(payload), encoding="utf-8")
        self._reload()

    def tearDown(self):
        ps.PROMPTS_PATH = self._orig_prompts
        ps.DEFAULTS_PATH = self._orig_defaults
        gi.SESSIONS_DIR = self._orig_sessions
        self._reload()
        self._tmpdir.cleanup()

    def _reload(self):
        ps.PROMPTS._mtime = None
        ps.PROMPTS._last_check = 0.0
        ps.PROMPTS._reload(force=True)


class AnEmptySomewhereLevelDoesNotStealTheBible(_TempPrompts):
    """SOMEWHERE ships Level-off and blank. That must stay a no-op."""

    def test_blank_level_does_not_compile_a_plate(self):
        self.assertFalse(gi.setting_authored())
        self.assertEqual(gi.setting_plate(), "")
        self.assertEqual(gi.place_line(), "")


class CharacterLookReachesEverySurface(_TempPrompts):
    def test_appearance_is_in_the_prefix_the_video_hears(self):
        gi.save_spec({
            gi.CAMERA_KEY: {"mode": "third_person"},
            gi.CHARACTER_KEY: {
                "enabled": True,
                "name": "Jason Fleece",
                "appearance": "neon pink mohawk, chrome cheek scar",
            },
        })
        look = "neon pink mohawk"
        self.assertIn(look, gi.protagonist_line())
        self.assertIn(look, gi.live_prefix())
        self.assertIn(look, gi.character_visual_sheet())
        preview = gi.preview()
        self.assertIn(look, preview["camera"]["prefix"])
        self.assertIn(look, preview["blocks"][gi.CHARACTER_KEY]["image"])

    def test_the_identity_put_returns_that_preview(self):
        import api

        gi.save_spec({
            gi.CAMERA_KEY: {"mode": "third_person"},
            gi.CHARACTER_KEY: {"enabled": True, "name": "Jason Fleece"},
        })
        client = api.app.test_client()
        resp = client.put("/api/admin/studio/identity", json={
            "player_character": {"appearance": "silver visor, burnt-orange coat"},
        })
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:400])
        body = resp.get_json()
        payload = (body or {}).get("data") or body or {}
        preview = payload.get("preview") or {}
        camera = preview.get("camera") or {}
        self.assertIn("silver visor", camera.get("prefix") or "")
        self.assertIn(
            "silver visor",
            ((preview.get("blocks") or {}).get("player_character") or {}).get("image") or "",
        )


class LevelCopyCompilesWithTheSwitchOff(_TempPrompts):
    def test_landmarks_and_palette_reach_the_live_prefix(self):
        gi.save_spec({gi.SETTING_KEY: {
            "enabled": False,
            "landmarks": "a lime-green water tower with a skull painted on it",
            "palette": "golden hour, red dust",
        }})
        self.assertTrue(gi.setting_authored())
        self.assertFalse(gi.setting_enabled())
        self.assertIn("lime-green water tower", gi.place_line())
        self.assertIn("lime-green water tower", gi.setting_plate())
        self.assertIn("golden hour", gi.setting_plate())
        self.assertIn("lime-green water tower", gi.live_prefix())
        shot = gi.opening_shot()
        self.assertIsNotNone(shot)
        self.assertIn("lime-green water tower", shot["vision"])


class TheClientKeepsTheSheetAlive(unittest.TestCase):
    def setUp(self):
        self.js = (ROOT / "static" / "js" / "standalone.js").read_text(
            encoding="utf-8", errors="replace")

    def test_saves_do_not_remount_the_open_form(self):
        self.assertIn("keepSheet: true", self.js)
        self.assertIn("function paintCompiled", self.js)
        apply = self.js.split("function applyIdentityPayload", 1)[1][:1600]
        self.assertIn("keepSheet", apply)
        self.assertIn("paintCompiled", apply)

    def test_compiled_prompt_is_on_the_sheet_not_in_a_closed_details(self):
        self.assertIn('pane.dataset.compiledFor = block.id', self.js)
        self.assertNotIn("we-more-compiled", self.js)

    def test_typing_commits_without_waiting_for_blur(self):
        self.assertIn("setTimeout(commit, 520)", self.js)
        self.assertIn('addEventListener("input"', self.js)

    def test_save_blurs_the_live_field_then_resets_the_frame(self):
        flush = self.js.split("async function flushSave()", 1)[1][:900]
        self.assertIn("ae.blur", flush)
        self.assertIn("resetFrame: true", flush)

    def test_the_live_steer_is_the_server_prefix(self):
        steer = self.js.split("function worldSteerPrompt", 1)[1].split("function sceneFromWorld", 1)[0]
        self.assertIn("Camera.prefix()", steer)
        self.assertNotIn("has(who)", steer)
        self.assertIn("opening_shot", steer)
        self.assertIn("frame_prompt", steer)
        self.assertNotIn("lastScene", steer)


class TheDeskWiringStillSeedsLingBot(unittest.TestCase):
    """The older wiring tests must keep passing after the prefix-first steer."""

    def test_steer_still_has_a_frame_fallback(self):
        js = (ROOT / "static" / "js" / "standalone.js").read_text(
            encoding="utf-8", errors="replace")
        steer = js.split("function worldSteerPrompt", 1)[1][:1800]
        self.assertIn("frame_prompt", steer)
        self.assertIn('A wide view of this place.', steer)


if __name__ == "__main__":
    unittest.main(verbosity=2)
