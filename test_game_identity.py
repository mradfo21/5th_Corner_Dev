"""
test_game_identity.py — offline unit tests for game_identity.py (the Cast &
Camera spec: who you play as, the level, and where the camera sits).

Covers the four-stage prompt pipeline (compile / retune / reconcile / negate),
spec normalization, the reference-image store, and — most importantly — that
everything is a genuine no-op while the sheet sits at its shipped defaults.

Never touches the network. Redirects both the prompt file and the reference
directory into a temp dir so the committed prompts/simulation_prompts.json and
assets/references/ are never mutated.

Run with:
    python3 -m unittest test_game_identity -v
"""

import json
import tempfile
import unittest
from pathlib import Path

import prompts_store as ps
import game_identity as gi


# A trimmed stand-in for the real prompt file. The image/negative prompts carry
# the same first-person + anti-person language the shipped ones do, because
# reconciling that language is the whole point of the module under test.
SAMPLE_PROMPTS = {
    "_comment_story_setup": "═══",
    "world_initial_state": "The year is 1993. You are Jason Fleece, photojournalist.",
    "action_consequence_instructions": "Return JSON with dispatch and visual_scene.",
    "situation_summary_instructions": "Describe what is happening NOW.",
    "gemini_text_to_image_instructions": (
        "SCENE: {prompt}\n"
        "CRITICAL POV RULE: This is FIRST-PERSON perspective.\n"
        "NEVER show your face, head, or full body.\n"
        "ABSOLUTELY NO PERSON VISIBLE — pure environmental shot.\n"
        "Jason is behind the camera."
    ),
    "gemini_image_to_image_instructions": "SHOW: {prompt}\nThe camera IS your eyes.",
    "image_negative_prompt": (
        "CGI, third person perspective, over shoulder view, behind character, "
        "following someone, borders, text overlays"
    ),
}


class _IdentityFixture(unittest.TestCase):
    """Temp prompt file + temp reference dir shared by both suites below."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self._tmpdir.name)

        self._orig_prompts_path = ps.PROMPTS_PATH
        self._orig_defaults_path = ps.DEFAULTS_PATH
        self._orig_refs_dir = gi.REFERENCES_DIR
        ps.PROMPTS_PATH = tmp / "simulation_prompts.json"
        ps.DEFAULTS_PATH = tmp / "simulation_prompts.defaults.json"
        gi.REFERENCES_DIR = tmp / "references"

        payload = dict(SAMPLE_PROMPTS)
        payload.update(gi.default_spec())
        for path in (ps.PROMPTS_PATH, ps.DEFAULTS_PATH):
            with path.open("w", encoding="utf-8") as f:
                json.dump(payload, f)

        self._reload()

    def tearDown(self):
        ps.PROMPTS_PATH = self._orig_prompts_path
        ps.DEFAULTS_PATH = self._orig_defaults_path
        gi.REFERENCES_DIR = self._orig_refs_dir
        self._reload()
        self._tmpdir.cleanup()

    def _reload(self):
        ps.PROMPTS._mtime = None
        ps.PROMPTS._last_check = 0.0
        ps.PROMPTS._reload(force=True)

    # Convenience: the two setups every test builds on.
    def _set_character(self, **overrides):
        base = {
            "enabled": True,
            "name": "Wren Alvarez",
            "pronouns": "she/her",
            "role": "salvage diver",
            "appearance": "early thirties, cropped black hair",
            "wardrobe": "patched orange dive suit",
        }
        base.update(overrides)
        gi.save_spec({gi.CHARACTER_KEY: base})

    def _set_mode(self, mode):
        gi.save_spec({gi.CAMERA_KEY: {"mode": mode}})


class GameIdentityTestCase(_IdentityFixture):
    """The spec, the compiler, and the four-stage prompt pipeline."""

    # ── defaults are a genuine no-op ────────────────────────────────

    def test_defaults_are_inert(self):
        self.assertFalse(gi.is_active())
        self.assertTrue(gi.is_first_person())
        self.assertFalse(gi.shows_character())
        self.assertFalse(gi.character_enabled())
        self.assertFalse(gi.setting_enabled())
        self.assertIsNone(gi.opening_shot())
        self.assertIsNone(gi.opening_narration())

    def test_apply_returns_text_unchanged_at_defaults(self):
        text = "CRITICAL POV RULE: This is FIRST-PERSON perspective. Jason acts."
        self.assertEqual(gi.apply(text, "image"), text)
        self.assertEqual(gi.apply(text, "narrative"), text)

    def test_world_brief_unchanged_at_defaults(self):
        base = ps.PROMPTS["world_initial_state"]
        self.assertEqual(gi.world_brief(base), base)

    def test_first_person_negative_prompt_still_bans_third_person(self):
        self.assertIn("third person perspective", gi.negative_prompt().lower())

    # ── normalization ───────────────────────────────────────────────

    def test_unknown_fields_are_dropped_and_missing_ones_filled(self):
        ps.save_prompts_bulk({gi.CHARACTER_KEY: {"name": "Ada", "sabotage": "boom"}})
        char = gi.get_spec()[gi.CHARACTER_KEY]
        self.assertEqual(char["name"], "Ada")
        self.assertNotIn("sabotage", char)
        self.assertEqual(char["pronouns"], "they/them")   # backfilled default
        self.assertEqual(char["reference_images"], [])

    def test_garbage_spec_block_falls_back_to_defaults(self):
        ps.save_prompts_bulk({gi.CAMERA_KEY: "not a dict"})
        self.assertEqual(gi.camera_mode(), gi.DEFAULT_MODE)

    def test_unknown_camera_mode_falls_back(self):
        self._set_mode("isometric_rts")
        self.assertEqual(gi.camera_mode(), gi.DEFAULT_MODE)

    def test_long_field_values_are_capped(self):
        self._set_character(appearance="x" * 5000)
        self.assertEqual(len(gi.get_spec()[gi.CHARACTER_KEY]["appearance"]), 600)

    def test_save_spec_merges_rather_than_replaces(self):
        self._set_character()
        gi.save_spec({gi.CHARACTER_KEY: {"role": "harbour pilot"}})
        char = gi.get_spec()[gi.CHARACTER_KEY]
        self.assertEqual(char["role"], "harbour pilot")
        self.assertEqual(char["name"], "Wren Alvarez")   # untouched

    def test_character_enabled_needs_more_than_the_toggle(self):
        gi.save_spec({gi.CHARACTER_KEY: {"enabled": True}})
        self.assertFalse(gi.character_enabled())

    # ── stage 1: compile ────────────────────────────────────────────

    def test_camera_directive_names_the_active_mode(self):
        self._set_mode("over_shoulder")
        directive = gi.camera_directive()
        self.assertIn("OVER-THE-SHOULDER", directive)
        self.assertNotIn("FIRST-PERSON", directive)

    def test_character_sheet_only_appears_once_the_body_can_be_seen(self):
        self._set_character()
        gi.save_spec({gi.CAMERA_KEY: {"mode": "first_person", "show_hands": False}})
        self.assertNotIn("PLAYER CHARACTER", gi.image_directive())
        self._set_mode("third_person")
        self.assertIn("Wren Alvarez", gi.image_directive())

    def test_level_plate_lands_in_the_image_directive(self):
        gi.save_spec({gi.SETTING_KEY: {
            "enabled": True,
            "name": "The Kettle Yard",
            "summary": "A flooded shipbreaking yard",
            "landmarks": "the listing tanker",
        }})
        directive = gi.image_directive()
        self.assertIn("The Kettle Yard", directive)
        self.assertIn("the listing tanker", directive)

    def test_narrative_directive_describes_who_and_where(self):
        self._set_character(demeanor="dry, unflappable")
        self._set_mode("third_person")
        sheet = gi.narrative_directive()
        self.assertIn("Wren Alvarez", sheet)
        self.assertIn("dry, unflappable", sheet)
        # Third person means the prose may describe the body from outside.
        self.assertIn("from outside", sheet)

    def test_world_brief_appends_the_cast_sheet(self):
        self._set_character()
        brief = gi.world_brief("BASE WORLD")
        self.assertTrue(brief.startswith("BASE WORLD"))
        self.assertIn("Wren Alvarez", brief)

    def test_opening_shot_uses_the_authored_plate(self):
        gi.save_spec({gi.SETTING_KEY: {
            "enabled": True,
            "name": "The Kettle Yard",
            "opening_shot": "Low tide at dawn, the tanker hull filling frame right",
        }})
        shot = gi.opening_shot()
        self.assertIn("The Kettle Yard", shot["prologue"])
        self.assertIn("Low tide at dawn", shot["vision"])

    def test_opening_shot_puts_the_character_in_frame_in_third_person(self):
        self._set_character()
        self._set_mode("third_person")
        gi.save_spec({gi.SETTING_KEY: {
            "enabled": True, "name": "The Kettle Yard", "opening_shot": "Low tide at dawn.",
        }})
        shot = gi.opening_shot()
        self.assertIn("Wren Alvarez", shot["prologue"])
        self.assertIn("Wren Alvarez is in frame", shot["vision"])

    def test_opening_narration_is_written_from_the_sheet(self):
        self._set_character(backstory="Came back for her sister")
        narration = gi.opening_narration()
        self.assertIn("You are Wren Alvarez", narration)
        self.assertIn("sister", narration)

    # ── stage 2: retune ─────────────────────────────────────────────

    def test_retune_rewrites_perspective_nouns_case_preserving(self):
        self._set_mode("third_person")
        out = gi.retune("FIRST-PERSON rule. This is first-person. First-person again.")
        self.assertIn("THIRD-PERSON", out)
        self.assertIn("third-person", out)
        self.assertIn("Third-person", out)
        self.assertNotIn("irst-person", out.replace("Third-person", "").replace("third-person", ""))

    def test_retune_swaps_pov_for_the_mode_tag(self):
        self._set_mode("over_shoulder")
        self.assertIn("OVER-THE-SHOULDER CHASE CAM", gi.retune("POV RULES apply"))

    def test_retune_is_a_noop_in_first_person(self):
        text = "This is FIRST-PERSON perspective with POV rules."
        self.assertEqual(gi.retune(text), text)

    def test_recast_renames_the_shipped_protagonist(self):
        self._set_character()
        out = gi.retune("Jason Fleece raises the camera. Jason waits.")
        self.assertNotIn("Jason", out)
        self.assertEqual(out.count("Wren Alvarez"), 2)

    def test_recast_leaves_prompts_alone_without_a_named_character(self):
        text = "Jason Fleece raises the camera."
        self.assertEqual(gi.recast(text), text)

    # ── stage 3: reconcile ──────────────────────────────────────────

    def test_reconcile_drops_anti_person_lines_when_the_body_is_in_frame(self):
        self._set_mode("third_person")
        out = gi.reconcile(
            "Keep this line.\n"
            "NEVER show your face, head, or full body.\n"
            "ABSOLUTELY NO PERSON VISIBLE — pure environmental shot.\n"
            "The camera operator does NOT exist in this image.\n"
            "Keep this one too."
        )
        self.assertIn("Keep this line.", out)
        self.assertIn("Keep this one too.", out)
        for gone in ("NEVER show your face", "NO PERSON VISIBLE", "camera operator"):
            self.assertNotIn(gone, out)

    def test_reconcile_is_a_noop_in_first_person(self):
        text = "ABSOLUTELY NO PERSON VISIBLE — pure environmental shot."
        self.assertEqual(gi.reconcile(text), text)

    def test_full_pipeline_leaves_no_surviving_contradiction(self):
        self._set_character()
        self._set_mode("over_shoulder")
        wrapped = ps.PROMPTS["gemini_text_to_image_instructions"].format(prompt="A muddy yard")
        out = gi.apply(wrapped, "image")
        # The directive leads.
        self.assertTrue(out.startswith("🎥 CAMERA DIRECTIVE"))
        # The scene survives.
        self.assertIn("A muddy yard", out)
        # Nothing left arguing for an empty frame or a hidden protagonist.
        for gone in ("NEVER show your face", "NO PERSON VISIBLE", "FIRST-PERSON", "Jason"):
            self.assertNotIn(gone, out)

    # ── stage 4: negate ─────────────────────────────────────────────

    def test_negative_prompt_stops_banning_the_selected_perspective(self):
        self._set_mode("over_shoulder")
        neg = gi.negative_prompt().lower()
        for gone in ("third person", "over shoulder", "behind character", "following someone"):
            self.assertNotIn(gone, neg)
        self.assertIn("first person view", neg)
        # Unrelated bans are untouched.
        self.assertIn("cgi", neg)
        self.assertIn("borders", neg)

    def test_negative_prompt_does_not_duplicate_existing_bans(self):
        self._set_mode("third_person")
        neg = gi.negative_prompt()
        self.assertEqual(neg.lower().count("first person view"), 1)

    # ── accessors ───────────────────────────────────────────────────

    def test_hands_toggle_only_applies_to_first_person(self):
        self.assertTrue(gi.hands_visible())
        gi.save_spec({gi.CAMERA_KEY: {"show_hands": False}})
        self.assertFalse(gi.hands_visible())
        self._set_mode("third_person")
        self.assertFalse(gi.hands_visible())

    def test_every_mode_compiles(self):
        self._set_character()
        for mode in gi.PERSPECTIVE_MODES:
            self._set_mode(mode)
            self.assertTrue(gi.camera_directive())
            self.assertTrue(gi.negative_prompt())
            self.assertTrue(gi.preview()["image_directive"])

    def test_character_sdxl_tags_are_short_and_tag_shaped(self):
        self._set_character()
        tags = gi.character_sdxl_tags()
        self.assertIn("salvage diver", tags)
        self.assertLessEqual(len(tags), 220)
        self.assertNotIn("\n", tags)

    def test_reset_clears_everything(self):
        self._set_character()
        self._set_mode("third_person")
        gi.reset_spec()
        self.assertFalse(gi.is_active())
        self.assertEqual(gi.camera_mode(), gi.DEFAULT_MODE)

    def test_ensure_spec_keys_backfills_an_older_prompt_file(self):
        stripped = {k: v for k, v in dict(ps.PROMPTS).items() if k not in gi.SPEC_KEYS}
        with ps.PROMPTS_PATH.open("w", encoding="utf-8") as f:
            json.dump(stripped, f)
        self._reload()
        self.assertIsNone(ps.PROMPTS.get(gi.CHARACTER_KEY))
        gi.ensure_spec_keys()
        self.assertIsNotNone(ps.PROMPTS.get(gi.CHARACTER_KEY))
        self.assertFalse(gi.is_active())

    def test_spec_keys_are_editable_so_worlds_snapshot_them(self):
        # worlds_store snapshots prompts_store.editable_keys(), so the cast
        # sheet riding along in a saved world depends on this.
        for key in gi.SPEC_KEYS:
            self.assertIn(key, ps.editable_keys())

    def test_editor_schema_carries_the_live_perspective_options(self):
        schema = gi.identity_schema()
        camera = next(b for b in schema if b["id"] == gi.CAMERA_KEY)
        mode_field = next(f for f in camera["fields"] if f["type"] == "mode")
        self.assertEqual(
            {o["id"] for o in mode_field["options"]}, set(gi.PERSPECTIVE_MODES)
        )


class ReferenceImageTestCase(_IdentityFixture):
    """The character-sheet / level-plate store."""

    # A 1x1 PNG, base64'd — smallest thing that is genuinely a valid image.
    PNG = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )

    def test_round_trip_save_resolve_delete(self):
        meta = gi.save_reference(self.PNG, "character", "Wren portrait")
        self.assertTrue(meta["id"].startswith("character_"))
        self.assertEqual(meta["label"], "Wren portrait")

        path = gi.reference_path(meta["id"])
        self.assertIsNotNone(path)
        self.assertTrue(path.exists())

        gi.save_spec({gi.CHARACTER_KEY: {"reference_images": [meta["id"]]}})
        self.assertEqual(gi.character_reference_paths(), [str(path)])

        gi.delete_reference(meta["id"])
        self.assertIsNone(gi.reference_path(meta["id"]))
        # …and the id is unwired from the slot rather than left dangling.
        self.assertEqual(gi.get_spec()[gi.CHARACTER_KEY]["reference_images"], [])

    def test_rejects_non_data_urls_and_bad_base64(self):
        with self.assertRaises(ValueError):
            gi.save_reference("https://example.com/wren.png", "character")
        with self.assertRaises(ValueError):
            gi.save_reference("data:image/png;base64,!!!not base64!!!", "character")

    def test_rejects_unsupported_image_types(self):
        with self.assertRaises(ValueError):
            gi.save_reference("data:image/tiff;base64,AAAA", "setting")

    def test_rejects_oversized_uploads(self):
        import base64
        huge = base64.b64encode(b"\0" * (gi.MAX_REFERENCE_BYTES + 1)).decode()
        with self.assertRaises(ValueError):
            gi.save_reference(f"data:image/png;base64,{huge}", "setting")

    def test_malformed_reference_ids_never_touch_the_filesystem(self):
        for bad in ("../../etc/passwd", "character_zzz", "", "nope"):
            self.assertIsNone(gi.reference_path(bad))

    def test_spec_drops_ids_that_do_not_look_like_references(self):
        gi.save_spec({gi.SETTING_KEY: {"reference_images": ["../../etc/passwd", "ok_but_wrong"]}})
        self.assertEqual(gi.get_spec()[gi.SETTING_KEY]["reference_images"], [])

    def test_manifest_skips_ids_whose_file_vanished(self):
        meta = gi.save_reference(self.PNG, "setting")
        gi.reference_path(meta["id"]).unlink()
        self.assertEqual(gi.reference_manifest([meta["id"]]), [])

    def test_identity_paths_lead_with_the_level_plate(self):
        char = gi.save_reference(self.PNG, "character")
        setting = gi.save_reference(self.PNG, "setting")
        gi.save_spec({
            gi.CHARACTER_KEY: {"reference_images": [char["id"]]},
            gi.SETTING_KEY: {"reference_images": [setting["id"]]},
        })
        paths = gi.identity_reference_paths()
        self.assertEqual(len(paths), 2)
        self.assertEqual(paths[0], str(gi.reference_path(setting["id"])))

    def test_annotation_explains_what_each_plate_is(self):
        char = gi.save_reference(self.PNG, "character")
        setting = gi.save_reference(self.PNG, "setting")
        gi.save_spec({
            gi.CHARACTER_KEY: {"enabled": True, "name": "Wren", "reference_images": [char["id"]]},
            gi.SETTING_KEY: {"reference_images": [setting["id"]]},
        })
        note = gi.reference_annotation(gi.identity_reference_paths())
        self.assertIn("LOCATION PLATE", note)
        self.assertIn("CHARACTER SHEET", note)
        self.assertIn("Wren", note)

    def test_annotation_is_empty_without_plates(self):
        self.assertEqual(gi.reference_annotation([]), "")

    def test_slot_caps_the_number_of_plates(self):
        ids = [gi.save_reference(self.PNG, "character")["id"] for _ in range(5)]
        gi.save_spec({gi.CHARACTER_KEY: {"reference_images": ids}})
        stored = gi.get_spec()[gi.CHARACTER_KEY]["reference_images"]
        self.assertEqual(len(stored), gi.MAX_REFERENCES_PER_SLOT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
