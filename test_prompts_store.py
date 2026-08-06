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
    "action_consequence_instructions": "Describe consequences using {dispatch} only if used via f-string, not format().",
    "player_choice_generation_instructions": "Choices for {dispatch} and {seen_elements} and {recent_choices} and {caption} and {image_description} and {time_of_day} and {beat_nudge} and {situation_summary} and {injury_state}.",
    "image_art_direction": "LOOK: 1993 VHS, muted palette.",
    "image_camera_rules": "CAMERA: eye level, first-person, no text overlays.",
    "gemini_text_to_image_instructions": "SCENE:\n{prompt}\n\n{art_direction}\n\n{camera_rules}",
    "gemini_image_to_image_instructions": "SHOW:\n{prompt}\n\n{art_direction}\n\nHONOUR THE REFERENCE.\n\n{camera_rules}",
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

    # ── unified image templates ─────────────────────────────────────
    #
    # The two image templates used to be near-duplicates, so redirecting the
    # world's look meant editing the same paragraphs twice. They now share
    # `image_art_direction` + `image_camera_rules` through placeholders.

    def test_render_image_template_injects_the_shared_blocks(self):
        out = ps.render_image_template("gemini_text_to_image_instructions", "A muddy yard")
        self.assertIn("A muddy yard", out)
        self.assertIn("LOOK: 1993 VHS, muted palette.", out)
        self.assertIn("CAMERA: eye level", out)

    def test_one_art_direction_edit_reaches_both_render_paths(self):
        # This is the whole point of the split.
        ps.save_prompt_field("image_art_direction", "LOOK: 1982 Antarctic station, blue-grey.")
        for key in ps.IMAGE_TEMPLATE_KEYS:
            out = ps.render_image_template(key, "scene")
            self.assertIn("1982 Antarctic station", out, f"{key} missed the shared edit")
            self.assertNotIn("1993 VHS", out, f"{key} kept the stale direction")

    def test_one_camera_rules_edit_reaches_both_render_paths(self):
        ps.save_prompt_field("image_camera_rules", "CAMERA: locked tripod, no handheld.")
        for key in ps.IMAGE_TEMPLATE_KEYS:
            self.assertIn("CAMERA: locked tripod", ps.render_image_template(key, "scene"))

    def test_each_template_leads_with_its_own_delta(self):
        # Ordering matters twice over: a mode-specific rule buried 4,000 chars
        # down gets ignored, and Krea clamps the prompt at 5,000 chars — which
        # was cutting the i2i spatial-lock rules off entirely.
        for key in ps.IMAGE_TEMPLATE_KEYS:
            tmpl = ps.PROMPTS[key]
            self.assertLess(tmpl.index("{prompt}"), tmpl.index("{art_direction}"),
                            f"{key}: the scene must come first")
            self.assertLess(tmpl.index("{art_direction}"), tmpl.index("{camera_rules}"),
                            f"{key}: art direction before the long camera rulebook")

    def test_each_template_keeps_only_its_own_delta(self):
        t2i = ps.render_image_template("gemini_text_to_image_instructions", "scene")
        i2i = ps.render_image_template("gemini_image_to_image_instructions", "scene")
        self.assertIn("HONOUR THE REFERENCE.", i2i)
        self.assertNotIn("HONOUR THE REFERENCE.", t2i)

    def test_render_leaves_no_unresolved_placeholders(self):
        for key in ps.IMAGE_TEMPLATE_KEYS:
            self.assertEqual(ps.find_placeholders(ps.render_image_template(key, "scene")), [])

    def test_template_without_placeholders_renders_untouched(self):
        # Backwards compatibility: an install customized before the split still
        # has all this material written inline, so injecting it would duplicate it.
        ps.save_prompt_field("gemini_text_to_image_instructions",
                             "OLD MONOLITHIC\nSCENE: {prompt}\nrules inline")
        out = ps.render_image_template("gemini_text_to_image_instructions", "scene")
        self.assertEqual(out, "OLD MONOLITHIC\nSCENE: scene\nrules inline")
        self.assertNotIn("LOOK: 1993 VHS", out)

    def test_partial_wiring_injects_only_what_is_referenced(self):
        ps.save_prompt_field("gemini_text_to_image_instructions", "SCENE: {prompt}\n{art_direction}")
        out = ps.render_image_template("gemini_text_to_image_instructions", "scene")
        self.assertIn("LOOK: 1993 VHS", out)
        self.assertNotIn("CAMERA: eye level", out)

    def test_missing_shared_field_renders_empty_rather_than_raising(self):
        ps.save_prompt_field("image_art_direction", None)
        # Must not raise — a half-migrated file shouldn't break a running turn.
        self.assertIn("scene", ps.render_image_template("gemini_text_to_image_instructions", "scene"))

    def test_dropping_a_shared_placeholder_warns_but_does_not_block(self):
        # Writing a fully bespoke template is legitimate, so this must not stop
        # the save — but it silently disconnects the shared direction from that
        # render path, which is invisible from the image, so it has to be said.
        ok, msgs = ps.validate_prompt_value(
            "gemini_image_to_image_instructions", "SHOW: {prompt}")
        self.assertTrue(ok)
        self.assertEqual(len(msgs), 1)
        self.assertIn("{art_direction}", msgs[0])
        self.assertIn("{camera_rules}", msgs[0])

    def test_partially_dropped_placeholder_names_only_the_missing_one(self):
        ok, msgs = ps.validate_prompt_value(
            "gemini_text_to_image_instructions", "SCENE: {prompt}\n{art_direction}")
        self.assertTrue(ok)
        self.assertIn("{camera_rules}", msgs[0])
        self.assertNotIn("{art_direction}", msgs[0])

    def test_fully_wired_template_produces_no_warning(self):
        ok, msgs = ps.validate_prompt_value(
            "gemini_text_to_image_instructions",
            "SCENE: {prompt}\n{art_direction}\n{camera_rules}")
        self.assertTrue(ok)
        self.assertEqual(msgs, [])

    def test_a_real_placeholder_error_still_blocks_the_save(self):
        # The advisory path must not have softened the blocking one.
        ok, msgs = ps.validate_prompt_value(
            "gemini_text_to_image_instructions",
            "SCENE: {prompt}\n{art_direction}\n{camera_rules}\n{nonsense}")
        self.assertFalse(ok)
        self.assertTrue(any("nonsense" in m for m in msgs))

    def test_shared_fields_themselves_are_never_warned_about(self):
        # They're substituted as values, so they have no placeholder contract.
        for key in (ps.ART_DIRECTION_KEY, ps.CAMERA_RULES_KEY):
            ok, msgs = ps.validate_prompt_value(key, "anything at all {loose")
            self.assertTrue(ok)
            self.assertEqual(msgs, [])

    def test_shared_placeholders_validate_on_the_templates(self):
        for key in ps.IMAGE_TEMPLATE_KEYS:
            ok, warnings = ps.validate_prompt_value(
                key, "SCENE: {prompt}\n{art_direction}\n{camera_rules}")
            self.assertTrue(ok, f"{key}: {warnings}")

    def test_shared_fields_are_not_format_parsed(self):
        # They're substituted as values, so braces in them are literal and must
        # not be rejected or blow up the render.
        ps.save_prompt_field("image_art_direction", "LOOK: a {curly} brace and a lone {")
        ok, _ = ps.validate_prompt_value("image_art_direction", "LOOK: a lone {")
        self.assertTrue(ok)
        out = ps.render_image_template("gemini_text_to_image_instructions", "scene")
        self.assertIn("a {curly} brace and a lone {", out)

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
        ok, warnings = ps.validate_prompt_value(
            "gemini_text_to_image_instructions",
            "SCENE:\n{prompt}\n{art_direction}\n{camera_rules}\nmore text")
        self.assertTrue(ok)
        self.assertEqual(warnings, [])

    def test_validate_prompt_value_rejects_unknown_placeholder(self):
        ok, warnings = ps.validate_prompt_value(
            "gemini_text_to_image_instructions",
            "SCENE:\n{prompt} and {bogus}\n{art_direction}\n{camera_rules}")
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


class EditableSurfaceTestCase(unittest.TestCase):
    """The size and shape of what a player is asked to reason about.

    These read the REAL committed prompt file and schema on purpose — the point
    is to keep the shipped editing surface small and fully wired, and neither
    property survives being asserted against a fixture.
    """

    def test_every_prompt_in_the_file_is_wired_and_editable(self):
        """No key you can save that changes nothing.

        Four keys had drifted into exactly that state — ~9KB of prompt text
        read by no code path, snapshotted into every saved world, and named in
        the README as something to edit. That costs an edit, a restart, and
        your trust in every other field.
        """
        self.assertEqual(ps.unwired_keys(), [])

    def test_every_schema_field_exists_in_the_prompt_file(self):
        """The inverse: no editor field pointing at a key that isn't there."""
        missing = [f["id"] for f in ps.PROMPT_SCHEMA if f["id"] not in ps.PROMPTS]
        self.assertEqual(missing, [])

    def test_the_primary_surface_stays_short(self):
        """Four prompts do the redirecting. Adding a fifth should be a decision
        somebody makes on purpose, not something that happens."""
        self.assertEqual(ps.primary_keys(), [
            "world_initial_state",
            "action_consequence_instructions",
            "player_choice_generation_instructions",
            "image_art_direction",
        ])

    def test_every_field_declares_a_tier_and_a_known_group(self):
        for field in ps.PROMPT_SCHEMA:
            self.assertIn(field.get("tier"), (ps.TIER_PRIMARY, ps.TIER_ADVANCED), field["id"])
            self.assertIn(field.get("group"), ps.GROUP_LABELS, field["id"])

    def test_every_group_has_a_label_a_blurb_and_a_primary_field(self):
        """A tab with no primary field is a tab of pure machinery — it should be
        folded into a neighbour rather than presented as a peer."""
        primary_groups = {
            f["group"] for f in ps.PROMPT_SCHEMA if f["tier"] == ps.TIER_PRIMARY
        }
        for group in ps.GROUP_LABELS:
            self.assertIn(group, ps.GROUP_BLURBS, group)
            self.assertIn(group, primary_groups, group)


if __name__ == "__main__":
    unittest.main()
