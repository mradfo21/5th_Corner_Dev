"""
test_prompts_store.py — offline unit tests for prompts_store.py (hot-reload,
save/reset, and placeholder validation). Never touches the network; uses a
temp prompts file + temp defaults file so it never mutates the real
committed prompts/simulation_prompts.json.

Run with:
    python3 -m unittest test_prompts_store -v
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import prompts_store as ps


SAMPLE_PROMPTS = {
    "_comment_story_setup": "═══",
    "world_initial_state": "A quiet desert town at dusk.",
    "story_progression_phases": ["Establish", "Explore", "Climax"],
    "action_consequence_instructions": "Describe consequences using {dispatch} only if used via f-string, not format().",
    "player_choice_generation_instructions": "Choices for {dispatch} and {seen_elements} and {recent_choices} and {caption} and {image_description} and {time_of_day} and {beat_nudge} and {situation_summary} and {injury_state}.",
    "gemini_text_to_image_instructions": "SCENE:\n{prompt}",
    "gemini_image_to_image_instructions": "SHOW:\n{prompt}",
    "image_negative_prompt": "no CGI",
}


class PromptsStoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmpdir.name)

        self._orig_prompts_path = ps.PROMPTS_PATH
        self._orig_defaults_path = ps.DEFAULTS_PATH
        ps.PROMPTS_PATH = tmp_path / "simulation_prompts.json"
        ps.DEFAULTS_PATH = tmp_path / "simulation_prompts.defaults.json"

        with ps.PROMPTS_PATH.open("w", encoding="utf-8") as f:
            json.dump(SAMPLE_PROMPTS, f)
        with ps.DEFAULTS_PATH.open("w", encoding="utf-8") as f:
            json.dump(SAMPLE_PROMPTS, f)

        # Force a clean reload against the temp file for every test.
        ps.PROMPTS._mtime = None
        ps.PROMPTS._last_check = 0.0
        ps.PROMPTS._reload(force=True)

    def tearDown(self):
        ps.PROMPTS_PATH = self._orig_prompts_path
        ps.DEFAULTS_PATH = self._orig_defaults_path
        ps.PROMPTS._mtime = None
        ps.PROMPTS._last_check = 0.0
        ps.PROMPTS._reload(force=True)
        self._tmpdir.cleanup()

    # ── basic load ──────────────────────────────────────────────────

    def test_loads_sample_prompts(self):
        self.assertEqual(ps.PROMPTS["world_initial_state"], "A quiet desert town at dusk.")
        self.assertEqual(ps.PROMPTS.get("nonexistent", "fallback"), "fallback")

    def test_editable_keys_excludes_comments(self):
        keys = ps.editable_keys()
        self.assertIn("world_initial_state", keys)
        self.assertNotIn("_comment_story_setup", keys)

    # ── save / reset round trip ─────────────────────────────────────

    def test_save_prompt_field_persists_and_reloads_live_dict(self):
        ps.save_prompt_field("world_initial_state", "A new setting entirely.")
        self.assertEqual(ps.PROMPTS["world_initial_state"], "A new setting entirely.")
        with ps.PROMPTS_PATH.open("r", encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["world_initial_state"], "A new setting entirely.")

    def test_save_prompts_bulk(self):
        ps.save_prompts_bulk({
            "world_initial_state": "Bulk edit A",
            "image_negative_prompt": "Bulk edit B",
        })
        self.assertEqual(ps.PROMPTS["world_initial_state"], "Bulk edit A")
        self.assertEqual(ps.PROMPTS["image_negative_prompt"], "Bulk edit B")

    def test_reset_prompt_field_restores_default(self):
        ps.save_prompt_field("world_initial_state", "Something edited.")
        self.assertNotEqual(ps.PROMPTS["world_initial_state"], SAMPLE_PROMPTS["world_initial_state"])
        ps.reset_prompt_field("world_initial_state")
        self.assertEqual(ps.PROMPTS["world_initial_state"], SAMPLE_PROMPTS["world_initial_state"])

    def test_reset_prompt_field_unknown_key_raises(self):
        with self.assertRaises(KeyError):
            ps.reset_prompt_field("totally_made_up_key")

    def test_reset_all_prompts_restores_everything(self):
        ps.save_prompts_bulk({"world_initial_state": "X", "image_negative_prompt": "Y"})
        ps.reset_all_prompts()
        self.assertEqual(dict(ps.PROMPTS), SAMPLE_PROMPTS)

    # ── hot reload via mtime ────────────────────────────────────────

    def test_external_file_edit_is_picked_up_without_explicit_reload_call(self):
        # Simulate a totally separate process editing the file on disk
        # (e.g. a different gunicorn worker), bypassing prompts_store's
        # own save function entirely.
        ps.PROMPTS._last_check = 0.0  # clear throttle so the next access re-checks mtime
        edited = dict(SAMPLE_PROMPTS)
        edited["world_initial_state"] = "Edited by another process."
        time.sleep(0.02)  # ensure a distinct mtime on filesystems with coarse resolution
        with ps.PROMPTS_PATH.open("w", encoding="utf-8") as f:
            json.dump(edited, f)
        os.utime(ps.PROMPTS_PATH, None)

        self.assertEqual(ps.PROMPTS["world_initial_state"], "Edited by another process.")

    def test_recheck_is_throttled(self):
        ps.PROMPTS._reload(force=True)
        ps.PROMPTS._last_check = time.time()  # pretend we just checked
        edited = dict(SAMPLE_PROMPTS)
        edited["world_initial_state"] = "Should not be seen yet."
        with ps.PROMPTS_PATH.open("w", encoding="utf-8") as f:
            json.dump(edited, f)
        # Throttle window hasn't elapsed, so the live dict should still show the old value.
        self.assertEqual(ps.PROMPTS["world_initial_state"], SAMPLE_PROMPTS["world_initial_state"])

    # ── placeholder validation ──────────────────────────────────────

    def test_validate_prompt_value_accepts_known_placeholders(self):
        value = ps.PROMPT_SCHEMA_BY_ID["gemini_text_to_image_instructions"]
        ok, warnings = ps.validate_prompt_value("gemini_text_to_image_instructions", "SCENE:\n{prompt}\nmore text")
        self.assertTrue(ok)
        self.assertEqual(warnings, [])

    def test_validate_prompt_value_rejects_unknown_placeholder(self):
        ok, warnings = ps.validate_prompt_value("gemini_text_to_image_instructions", "SCENE:\n{prompt} and {bogus}")
        self.assertFalse(ok)
        self.assertTrue(any("bogus" in w for w in warnings))

    def test_validate_prompt_value_rejects_stray_brace(self):
        ok, warnings = ps.validate_prompt_value(
            "player_choice_generation_instructions",
            "{dispatch} {seen_elements} {recent_choices} {caption} {image_description} "
            "{time_of_day} {beat_nudge} {situation_summary} {injury_state} { stray",
        )
        self.assertFalse(ok)

    def test_validate_prompt_value_allows_literal_braces_when_not_format_required(self):
        # action_consequence_instructions isn't in the format-required set,
        # so arbitrary braces (e.g. a pasted JSON example) are always safe.
        ok, warnings = ps.validate_prompt_value(
            "action_consequence_instructions", 'Return JSON: { "dispatch": "...", "player_alive": true }'
        )
        self.assertTrue(ok)
        self.assertEqual(warnings, [])

    def test_find_placeholders(self):
        self.assertEqual(sorted(ps.find_placeholders("{a} text {b} {a}")), ["a", "a", "b"])


if __name__ == "__main__":
    unittest.main()
