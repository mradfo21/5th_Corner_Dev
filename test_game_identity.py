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
import unittest.mock
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
        self._orig_sessions_dir = gi.SESSIONS_DIR
        ps.PROMPTS_PATH = tmp / "simulation_prompts.json"
        ps.DEFAULTS_PATH = tmp / "simulation_prompts.defaults.json"
        gi.REFERENCES_DIR = tmp / "references"
        gi.SESSIONS_DIR = tmp / "sessions"
        gi.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

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
        gi.SESSIONS_DIR = self._orig_sessions_dir
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

    # ── defaults are third person: body on screen, pipeline on ──────

    def test_defaults_are_third_person(self):
        self.assertEqual(gi.camera_mode(), "third_person")
        self.assertFalse(gi.is_first_person())
        self.assertTrue(gi.shows_character())
        self.assertTrue(gi.uses_shipped_protagonist())
        self.assertFalse(gi.character_enabled())
        self.assertFalse(gi.setting_enabled())
        self.assertIsNone(gi.opening_shot())
        self.assertIsNone(gi.opening_narration())

    def test_apply_rewrites_first_person_language_at_defaults(self):
        text = "CRITICAL POV RULE: This is FIRST-PERSON perspective. Jason acts."
        out = gi.apply(text, "image")
        self.assertIn("THIRD-PERSON", out)
        self.assertIn("Jason Fleece", out)

    def test_first_person_mode_does_not_put_a_body_in_frame(self):
        self._set_mode("first_person")
        gi.save_spec({gi.CAMERA_KEY: {"show_hands": True}})
        out = gi.apply("A fence in the desert.", "image")
        self.assertNotIn("fully visible", out.lower())
        self.assertFalse(gi.shows_character())

    def test_world_brief_names_the_camera_at_defaults(self):
        base = ps.PROMPTS["world_initial_state"]
        brief = gi.world_brief(base)
        self.assertIn(base, brief)
        self.assertIn("DIRECTOR'S SHEET", brief)
        self.assertIn("Jason Fleece", brief)

    def test_first_person_negative_prompt_still_bans_third_person(self):
        self._set_mode("first_person")
        gi.save_spec({gi.CAMERA_KEY: {"show_hands": True}})
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

    def test_visual_scene_guidance_names_the_body_in_third_person(self):
        self._set_character()
        self._set_mode("third_person")
        guide = gi.visual_scene_guidance()
        self.assertIn("Wren Alvarez", guide)
        self.assertIn("exterior observation", guide)
        self.assertNotIn("through the player's eyes", guide)

    def test_visual_scene_guidance_is_eye_level_in_first_person(self):
        self._set_mode("first_person")
        guide = gi.visual_scene_guidance()
        self.assertIn("through the player's eyes", guide)
        self.assertNotIn("exterior observation", guide)

    def test_third_person_image_rules_lock_follow_continuity(self):
        self._set_mode("third_person")
        directive = gi.camera_directive()
        self.assertIn("screen direction", directive)
        self.assertIn("face-on", directive)

    def test_third_person_locks_action_game_follow_from_behind(self):
        """New scenes were rendering the character walking toward the lens —
        a cinematic arrival, not the 3rd-person action follow cam. The
        compiled camera, the live prefix, and visual_scene guidance all
        have to name that grammar or the image model invents a hero shot."""
        self._set_mode("third_person")
        directive = gi.camera_directive().lower()
        self.assertIn("behind the character", directive)
        self.assertIn("into the space ahead", directive)
        self.assertIn("walking-toward-camera", directive)
        self.assertEqual(gi.reconcile(directive), directive)
        bans = " ".join(gi.mode_config()["negative_add"]).lower()
        self.assertIn("character facing the camera", bans)
        self.assertIn("walking toward camera", bans)
        prefix = gi.live_prefix().lower()
        self.assertIn("behind the character", prefix)
        self.assertIn("into the space ahead", prefix)
        guide = gi.visual_scene_guidance().lower()
        self.assertIn("into the space ahead", guide)
        self.assertIn("walking-toward-camera", guide)

    def test_viewfinder_spec_is_first_person_without_saving(self):
        self._set_character()
        self._set_mode("third_person")
        vf = gi.viewfinder_spec()
        self.assertEqual(gi.camera_mode(), "third_person")
        self.assertTrue(gi.shows_character())
        self.assertEqual(gi.camera_mode(vf), "first_person")
        self.assertFalse(gi.shows_character(vf))
        self.assertFalse(gi.hands_visible(vf))
        self.assertTrue(gi.is_viewfinder_spec(vf))
        self.assertFalse(gi.is_viewfinder_spec())
        self.assertIn("camcorder", vf[gi.CAMERA_KEY]["notes"])
        ban = gi.viewfinder_hero_ban()
        self.assertIn("EMPTY FOREGROUND", ban)
        self.assertNotIn("Wren Alvarez", ban)
        self.assertNotIn("Jason Fleece", ban)
        prefix = gi.live_prefix(vf)
        self.assertIn("EMPTY FOREGROUND", prefix)
        self.assertNotIn("Wren Alvarez", prefix)
        self.assertNotIn("stays in frame", prefix.lower())
        cam = gi.live_camera_contract(vf)
        self.assertEqual(cam.get("look") or "", "")
        self.assertEqual(cam.get("perspective"), "first_person")

    def test_viewfinder_prompt_has_no_character_sheet_or_keep_in_frame(self):
        self._set_character()
        self._set_mode("third_person")
        vf = gi.viewfinder_spec()
        text = gi.apply("Red desert and a rusted tower ahead.", "image", vf)
        self.assertIn("FIRST-PERSON", text)
        self.assertNotIn("PLAYER CHARACTER — WHO IS ON SCREEN", text)
        self.assertNotIn("KEEP Wren", text)
        plates = gi.identity_reference_paths(
            include_character=gi.shows_character(vf) or gi.hands_visible(vf),
            spec=vf,
        )
        self.assertEqual(plates, gi.setting_reference_paths(vf))

    def test_strip_follow_cam_prose_drops_the_name_and_follow_verbs(self):
        self._set_character()
        out = gi.strip_follow_cam_prose(
            "Wren Alvarez stands before the rusted tower, seen by the camera"
        )
        self.assertNotIn("Wren", out)
        self.assertNotIn("Alvarez", out)
        self.assertNotIn("stands before", out.lower())
        self.assertIn("tower", out.lower())

    def test_viewfinder_place_lock_label_names_the_operator(self):
        self._set_character()
        vf = gi.viewfinder_spec()
        label = gi.reference_part_label("sessions/default/images/gameplay.png", vf)
        self.assertIn("CAMERA OPERATOR", label)
        self.assertIn("ERASE the person", label)
        self.assertIn("Do not draw them", label)

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
        self.assertIn("Wren Alvarez", shot["vision"])
        self.assertIn("BEHIND", shot["vision"])

    def test_opening_narration_is_written_from_the_sheet(self):
        self._set_character(backstory="Came back for her sister")
        narration = gi.opening_narration()
        self.assertIn("You are Wren Alvarez", narration)
        self.assertIn("sister", narration)

    def test_third_person_with_empty_cast_emits_the_shipped_protagonist(self):
        """Third person + blank CAST used to say 'the player character' with
        no face, clothes, or gender — every hard cut invented a new stranger.
        The shipped Jason identity is the CAST compiler firing, not a new
        'must be the same gender' rule stacked on top."""
        self._set_mode("third_person")
        self.assertTrue(gi.uses_shipped_protagonist())
        self.assertEqual(gi.display_name(), "Jason Fleece")
        sheet = gi.character_visual_sheet()
        self.assertIn("Jason Fleece", sheet)
        self.assertIn("he/him", sheet)
        self.assertIn("adult man", sheet)
        self.assertIn("olive field jacket", sheet)
        self.assertIn("Jason Fleece", gi.image_directive())
        self.assertIn("THE PLAYER IS: Jason Fleece", gi.narrative_directive())

    def test_authored_character_wins_over_the_shipped_fallback(self):
        self._set_character()
        self._set_mode("third_person")
        self.assertFalse(gi.uses_shipped_protagonist())
        self.assertEqual(gi.display_name(), "Wren Alvarez")
        self.assertNotIn("Jason Fleece", gi.character_visual_sheet())
        self.assertIn("Wren Alvarez", gi.character_visual_sheet())

    def test_recast_look_does_not_emit_jason_wardrobe(self):
        """The Experience editor only shows Name / Role / Look. Jason's
        pronouns, jacket, and camcorder stay in the hidden fields after a
        recast; those leftovers must not reach the image model."""
        self._set_mode("third_person")
        gi.save_spec({gi.CHARACTER_KEY: {
            "enabled": True,
            "name": "maria fleece",
            "role": "investigative photojournalist",
            "appearance": "adult woman, short dark hair, weathered face",
            "pronouns": "he/him",
            "wardrobe": "olive field jacket, dark work pants, boots",
            "signature_gear": "1993 VHS camcorder",
        }})
        sheet = gi.character_visual_sheet()
        self.assertIn("maria fleece", sheet)
        self.assertIn("adult woman", sheet)
        self.assertNotIn("he/him", sheet)
        self.assertNotIn("olive field jacket", sheet)
        self.assertNotIn("1993 VHS camcorder", sheet)
        self.assertNotIn("he/him", gi.protagonist_line())

    # A 1x1 PNG, so the plate below is a real file rather than a bare id.
    _PLATE_PNG = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )

    def test_the_authors_name_is_used_even_when_it_matches_the_shipped_one(self):
        """"Why? This is the character's name."

        Name / Role / Look used to be blanked whenever they matched the shipped
        values and a plate existed, to cover a different bug: the upload skipped
        filling a field that already had text, so a photograph of a woman still
        compiled as "Jason Fleece, adult man". That skip is gone — an
        overwriting draft clears what it could not read, so a plate cannot leave
        the previous person's name behind (see
        test_overwrite_replaces_leftover_jason_from_the_plate).

        What the blanking could never tell apart is a leftover default from a
        name the author typed, so it made "Jason Fleece" an unusable name for
        anyone who uploaded a photo: the sheet said Jason and the prose said
        "the player character". These three are the fields the minimal editor
        shows; a field you can see and edit is your choice.
        """
        self._set_mode("third_person")
        meta = gi.save_reference(self._PLATE_PNG, "character", "plate.png")
        gi.save_spec({gi.CHARACTER_KEY: {
            "enabled": True,
            "name": "Jason Fleece",
            "role": "investigative photojournalist",
            "appearance": "adult man, short dark hair, weathered face, stubble",
            "reference_images": [meta["id"]],
        }})
        self.assertEqual(gi.display_name(), "Jason Fleece")
        self.assertIn("Jason Fleece", gi.protagonist_line())
        self.assertIn("adult man", gi.character_visual_sheet())

    def test_hidden_shipped_leftovers_are_still_dropped(self):
        """The drop that stays: pronouns / wardrobe / gear are not shown in the
        minimal editor, so a shipped value there is not a choice."""
        meta = gi.save_reference(self._PLATE_PNG, "character", "plate.png")
        gi.save_spec({gi.CHARACTER_KEY: {
            "enabled": True,
            "name": "Wren Alvarez",
            "appearance": "adult woman, cropped hair",
            "pronouns": gi.SHIPPED_PROTAGONIST["pronouns"],
            "wardrobe": gi.SHIPPED_PROTAGONIST["wardrobe"],
            "reference_images": [meta["id"]],
        }})
        char = gi.authored_character()
        self.assertEqual(char["pronouns"], "")
        self.assertEqual(char["wardrobe"], "")
        self.assertEqual(char["name"], "Wren Alvarez")

    def test_a_plate_whose_file_is_gone_leaves_the_written_sheet_alone(self):
        """The other side of it. bugs: the shipped sheet named
        `character_54a7f7d76882` and no such file existed anywhere in the repo, so
        the code believed a photograph defined the protagonist while the image
        call attached nothing. Wardrobe then drifted frame to frame."""
        self._set_mode("third_person")
        gi.save_spec({gi.CHARACTER_KEY: {
            "enabled": True,
            "name": "Jason Fleece",
            "role": "investigative photojournalist",
            "appearance": "adult man, short dark hair, weathered face, stubble",
            "reference_images": ["character_a37a470f299d"],
        }})
        self.assertEqual(gi.live_reference_ids(gi.get_spec()[gi.CHARACTER_KEY]), [])
        # The written sheet is all there is, so it has to survive.
        self.assertIn("Jason Fleece", gi.character_visual_sheet())
        # And with no plate behind it, leftover Jason copy is exactly what it
        # looks like: the shipped cast. Reporting a recast on the strength of an
        # id with no file is how this went unnoticed.
        self.assertTrue(gi.is_shipped_cast())

    def test_a_dead_plate_is_reported_to_the_author(self):
        """Silent was the problem: a missing file looks exactly like a working
        one until the look starts drifting."""
        gi.save_spec({gi.CHARACTER_KEY: {
            "enabled": True, "name": "Kelsey Rowe",
            "reference_images": ["character_a37a470f299d"],
        }})
        notes = gi.wiring_notes()[gi.CHARACTER_KEY]
        self.assertTrue(any("missing from disk" in n for n in notes), notes)

    def test_a_plate_does_not_compile_leftover_somewhere_name_or_summary(self):
        """Upload used to skip fill when SOMEWHERE / the fence were already set.
        The plate is where this is; leftover shipped copy must not reach MOVE TO."""
        gi.save_spec({gi.SETTING_KEY: {
            "enabled": True,
            "name": "SOMEWHERE",
            "summary": "1993. The fence. Four Corners. The shipped demo.",
            "reference_images": ["setting_a37a470f299d"],
        }})
        plate = gi.setting_plate()
        self.assertNotIn("SOMEWHERE", plate)
        self.assertNotIn("The fence", plate)
        self.assertNotIn("SOMEWHERE", gi.place_line())
        self.assertNotIn("The fence", gi.place_line())
        narration = gi.opening_narration() or ""
        self.assertNotIn("SOMEWHERE", narration)
        self.assertNotIn("The fence", narration)
        seed = gi.intro_place_state()
        self.assertNotEqual(seed["location"], "desert_edge")
        self.assertNotEqual(seed["environment_type"], "desert")

    def test_opening_shot_leads_with_who_not_the_shipped_place(self):
        self._set_character()
        self._set_mode("third_person")
        gi.save_spec({gi.SETTING_KEY: {
            "enabled": True,
            "name": "SOMEWHERE",
            "summary": "1993. The fence. Four Corners. The shipped demo.",
        }})
        vision = gi.opening_shot()["vision"]
        self.assertLess(vision.index("Wren Alvarez"), vision.index("The fence"))

    def test_keep_character_prefers_the_sheet_over_a_previous_frame(self):
        self._set_character()
        text = gi.keep_character_instruction(has_character_plate=True)
        self.assertIn("CHARACTER SHEET", text)
        self.assertIn("NOT from any previous", text)
        self.assertIn("Wren Alvarez", text)
        extras = gi.keep_character_instruction(
            has_character_plate=True, extras_are_strangers=True,
        )
        self.assertIn("stranger", extras.lower())
        self.assertIn("clone", extras.lower())

    def test_identity_seed_does_not_call_plates_a_previous_frame(self):
        self._set_character()
        text = gi.identity_seed_instruction()
        self.assertIn("NOT a previous", text)
        self.assertIn("Wren Alvarez", text)
        self.assertIn("default photojournalist", text)

    def test_world_brief_leads_with_the_level_plate_after_recast(self):
        gi.save_spec({gi.SETTING_KEY: {
            "enabled": True,
            "name": "The Kettle Yard",
            "summary": "A flooded shipbreaking yard",
        }})
        brief = gi.world_brief("The year is 1993. Horizon. Four Corners.")
        self.assertTrue(brief.startswith("🗺️ LEVEL PLATE"))
        self.assertLess(brief.index("The Kettle Yard"), brief.index("Four Corners"))
        self.assertIn("background lore, not the", brief)

    def test_shipped_level_does_not_reorder_the_world_brief(self):
        gi.save_spec({gi.SETTING_KEY: {
            "enabled": True,
            "name": "SOMEWHERE",
            "summary": "1993. The fence. Four Corners. The shipped demo.",
        }})
        brief = gi.world_brief("BASE WORLD")
        self.assertTrue(brief.startswith("BASE WORLD"))

    def test_keep_place_prefers_the_plate_over_a_previous_frame(self):
        gi.save_spec({gi.SETTING_KEY: {
            "enabled": True,
            "name": "The Kettle Yard",
            "summary": "A flooded shipbreaking yard",
        }})
        text = gi.keep_place_instruction(has_setting_plate=True)
        self.assertIn("LOCATION PLATE", text)
        self.assertIn("NOT from any previous", text)
        self.assertIn("The Kettle Yard", text)

    def test_identity_seed_names_the_place_and_not_just_the_person(self):
        gi.save_spec({gi.SETTING_KEY: {
            "enabled": True,
            "name": "The Kettle Yard",
            "summary": "A flooded shipbreaking yard",
        }})
        text = gi.identity_seed_instruction()
        self.assertIn("The Kettle Yard", text)
        self.assertIn("Horizon desert fence", text)

    def test_intro_place_is_not_the_desert_after_a_level_recast(self):
        gi.save_spec({gi.SETTING_KEY: {
            "enabled": True,
            "name": "The Kettle Yard",
            "opening_shot": "Low tide at dawn.",
        }})
        seed = gi.intro_place_state()
        self.assertIn("Kettle Yard", seed["situation"])
        self.assertEqual(seed["location"], "The Kettle Yard")
        self.assertNotEqual(seed["location"], "desert_edge")
        self.assertNotEqual(seed["environment_type"], "desert")

    def test_intro_place_stays_shipped_at_the_somewhere_plate(self):
        gi.save_spec({gi.SETTING_KEY: {
            "enabled": True,
            "name": "SOMEWHERE",
            "summary": "1993. The fence. Four Corners. The shipped demo.",
        }})
        seed = gi.intro_place_state()
        self.assertEqual(seed["location"], "desert_edge")
        self.assertEqual(seed["environment_type"], "desert")

    def test_authored_art_direction_drops_southwest_after_recast(self):
        ps.save_prompt_field(
            "image_art_direction",
            "WORLD & ERA\n1993 American Southwest industrial horror. chain-link.\n\nLOOK\nPhotoreal still.",
        )
        gi.save_spec({gi.SETTING_KEY: {
            "enabled": True,
            "name": "The Kettle Yard",
            "summary": "A flooded shipbreaking yard",
            "era": "1993, analog only",
            "palette": "rust orange, sodium haze",
        }})
        look = gi.authored_art_direction()
        self.assertIn("The Kettle Yard", look)
        self.assertIn("sodium haze", look)
        self.assertNotIn("American Southwest", look)
        self.assertIn("Photoreal still", look)

    def test_first_person_empty_cast_still_emits_no_sheet(self):
        self._set_mode("first_person")
        gi.save_spec({gi.CAMERA_KEY: {"show_hands": True}})
        self.assertFalse(gi.uses_shipped_protagonist())
        self.assertEqual(gi.character_visual_sheet(), "")
        self.assertEqual(gi.protagonist_line(), "")

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
        self._set_mode("first_person")
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

    def test_naming_a_character_rewrites_the_world_bible(self):
        self.assertIn("Jason Fleece", ps.PROMPTS["world_initial_state"])
        self._set_character()
        self.assertNotIn("Jason", ps.PROMPTS["world_initial_state"])
        self.assertIn("Wren Alvarez", ps.PROMPTS["world_initial_state"])
        self.assertIn("Wren Alvarez", ps.PROMPTS["gemini_text_to_image_instructions"])

    def test_look_only_edit_does_not_rewrite_the_bible(self):
        self._set_character()
        before = ps.PROMPTS["world_initial_state"]
        gi.save_spec({gi.CHARACTER_KEY: {"appearance": "silver hair, scar"}})
        self.assertEqual(ps.PROMPTS["world_initial_state"], before)

    def test_opening_shot_from_character_when_level_is_off(self):
        self._set_character()
        self._set_mode("third_person")
        shot = gi.opening_shot()
        self.assertIsNotNone(shot)
        self.assertIn("Wren Alvarez", shot["prologue"])
        self.assertIn("Wren Alvarez", shot["vision"])
        self.assertIn("BEHIND", shot["vision"])

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
        self._set_mode("first_person")
        text = "ABSOLUTELY NO PERSON VISIBLE — pure environmental shot."
        self.assertEqual(gi.reconcile(text), text)

    def test_retune_does_not_hand_reconcile_a_reason_to_delete_a_good_rule(self):
        """The two stages can sabotage each other, and did.

        `reconcile` deletes a line when it pairs a prohibition with a
        third-person framing term — the signature of "a rule forbidding what the
        active mode requires". But `retune` runs FIRST and rewrites
        "first-person" to "third-person" in place. So an unrelated rule that
        merely mentioned a first-person vantage while forbidding something else
        entirely got "third-person" written into it by stage 2, and was then
        deleted whole by stage 3 for containing the word stage 2 had just added.

        This is how the LEAVE CAMP constraint lost its entire no-vehicle-cabin
        rule ("Do NOT show a vehicle interior, dashboard, steering wheel...")
        in exactly the mode where LEAVE CAMP puts a character on screen — the
        one place a driving-cab render is most obvious. Silent, mode-dependent,
        and invisible in first person, which is the shipped default.
        """
        self._set_mode("third_person")
        rule = ("SOMETHING ELSE: first-person eye-level vantage outdoors. "
                "Do NOT show a vehicle interior, dashboard, or steering wheel.")
        survived = gi.apply(rule + "\nAnother line.", "raw")
        self.assertIn("dashboard", survived)
        self.assertIn("steering wheel", survived)
        # Retune still did its job on the line it spared.
        self.assertIn("third-person eye-level vantage", survived)
        self.assertNotIn("first-person", survived)

    def test_the_camp_leave_rule_survives_the_pipeline_in_every_mode(self):
        """The concrete regression, asserted against the real constant."""
        import engine
        for mode in ("first_person", "third_person", "over_shoulder"):
            with self.subTest(mode):
                self._set_mode(mode)
                out = gi.apply(
                    "Scene text.\n\n" + engine._CAMP_LEAVE_ON_FOOT_CONSTRAINT, "image")
                self.assertIn("dashboard", out)
                self.assertIn("steering wheel", out)

    def test_full_pipeline_leaves_no_surviving_contradiction(self):
        self._set_character()
        self._set_mode("over_shoulder")
        wrapped = ps.PROMPTS["gemini_text_to_image_instructions"].format(prompt="A muddy yard")
        out = gi.apply(wrapped, "image")
        # The directive leads.
        self.assertTrue(out.startswith("🎥 CAMERA:"))
        # ...and does NOT claim precedence over the rest of the payload. It used
        # to open "THIS OVERRIDES ANY CONFLICTING CAMERA LANGUAGE BELOW", which
        # was true of a payload whose shared blocks bypassed this pipeline. Now
        # that nothing below contradicts it, announcing a conflict only invites
        # the model to treat this block as contested rather than as the answer.
        self.assertNotIn("OVERRIDES", out)
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

    def test_negative_prompt_drops_every_presence_and_body_ban(self):
        # The shipped negative prompt has a long FORBIDDEN clause listing every
        # way a protagonist could show up. Any one left in argues against the
        # character the player asked to see.
        ps.save_prompts_bulk({"image_negative_prompt": (
            "CGI, clean modernism. FORBIDDEN: Face visible, head visible, shoulders visible, "
            "full body in frame, person from behind, character's back, protagonist shown, "
            "someone else visible in frame, reflection of face, silhouette, "
            "third person perspective, over shoulder view, behind character, following someone. "
            "ABSOLUTELY NO: Black borders"
        )})
        self._set_mode("third_person")
        neg = gi.negative_prompt().lower()
        for gone in ("face visible", "head visible", "shoulders visible", "full body in frame",
                     "person from behind", "character's back", "protagonist shown",
                     "someone else visible", "reflection of face", "silhouette",
                     "third person", "over shoulder", "behind character", "following someone"):
            self.assertNotIn(gone, neg, f"{gone!r} should not survive into third person")
        # Unrelated bans are untouched.
        self.assertIn("cgi", neg)
        self.assertIn("black borders", neg)

    def test_lines_that_ban_third_person_framing_are_dropped(self):
        # Retune can't fix these — they say "third-person" on purpose — so
        # reconcile has to recognize a prohibition paired with a framing term.
        self._set_mode("over_shoulder")
        out = gi.reconcile(
            "FORBIDDEN (third-person shots will invalidate the entire grid):\n"
            "❌ Showing player from behind, side, or above\n"
            "❌ 'Camera following a character' shots\n"
            "❌ Any frame showing the player's body as a separate entity\n"
            "✓ Camera at IDENTICAL height as the first reference image"
        )
        self.assertEqual(out.strip(), "✓ Camera at IDENTICAL height as the first reference image")

    def test_reconcile_keeps_the_directives_own_rules(self):
        # reconcile runs over text that already carries the compiled directive
        # (the "raw" surface used by the image providers), so it must not eat
        # the very rules it just wrote.
        self._set_character()
        for mode in gi.PERSPECTIVE_MODES:
            self._set_mode(mode)
            directive = gi.image_directive()
            self.assertEqual(gi.reconcile(directive), directive, f"reconcile ate part of {mode}")

    # ── accessors ───────────────────────────────────────────────────

    def test_hands_toggle_only_applies_to_first_person(self):
        self._set_mode("first_person")
        gi.save_spec({gi.CAMERA_KEY: {"show_hands": True}})
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
        self._set_mode("first_person")
        gi.reset_spec()
        self.assertEqual(gi.camera_mode(), gi.DEFAULT_MODE)
        self.assertTrue(gi.shows_character())
        self.assertFalse(gi.character_enabled())

    def test_clear_block_empties_character_and_switches_it_off(self):
        self._set_character()
        spec = gi.clear_block(gi.CHARACTER_KEY)
        char = spec[gi.CHARACTER_KEY]
        self.assertFalse(char["enabled"])
        self.assertEqual(char["name"], "")
        self.assertEqual(char["role"], "")
        self.assertEqual(char["appearance"], "")
        self.assertEqual(char["reference_images"], [])
        self.assertFalse(gi.character_enabled())
        self.assertNotIn(
            "switched on but every field is blank",
            " ".join(gi.wiring_notes()[gi.CHARACTER_KEY]),
        )

    def test_clear_block_empties_level_and_leaves_character(self):
        self._set_character()
        gi.save_spec({gi.SETTING_KEY: {
            "enabled": True,
            "name": "The Kettle Yard",
            "summary": "A flooded shipbreaking yard",
            "landmarks": "the listing tanker",
            "opening_shot": "Low tide at dawn",
        }})
        spec = gi.clear_block(gi.SETTING_KEY)
        setting = spec[gi.SETTING_KEY]
        self.assertFalse(setting["enabled"])
        self.assertEqual(setting["name"], "")
        self.assertEqual(setting["summary"], "")
        self.assertEqual(setting["landmarks"], "")
        self.assertEqual(setting["opening_shot"], "")
        self.assertFalse(gi.setting_enabled())
        self.assertEqual(spec[gi.CHARACTER_KEY]["name"], "Wren Alvarez")

    def test_clear_block_rejects_unknown_keys(self):
        with self.assertRaises(KeyError):
            gi.clear_block("not_a_sheet")

    def test_ensure_spec_keys_backfills_an_older_prompt_file(self):
        stripped = {k: v for k, v in dict(ps.PROMPTS).items() if k not in gi.SPEC_KEYS}
        with ps.PROMPTS_PATH.open("w", encoding="utf-8") as f:
            json.dump(stripped, f)
        self._reload()
        self.assertIsNone(ps.PROMPTS.get(gi.CHARACTER_KEY))
        gi.ensure_spec_keys()
        self.assertIsNotNone(ps.PROMPTS.get(gi.CHARACTER_KEY))
        self.assertEqual(gi.camera_mode(), gi.DEFAULT_MODE)
        self.assertTrue(gi.shows_character())

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

    def test_leftover_somewhere_copy_does_not_beat_a_level_plate(self):
        setting = gi.save_reference(self.PNG, "setting")
        gi.save_spec({gi.SETTING_KEY: {
            "enabled": True,
            "name": "SOMEWHERE",
            "summary": "1993. The fence. Four Corners. The shipped demo.",
            "reference_images": [setting["id"]],
        }})
        brief = gi.world_brief("The year is 1993. Horizon. Four Corners.")
        self.assertTrue(brief.startswith("🗺️ LEVEL PLATE"))
        self.assertNotIn("LOCATION: SOMEWHERE", brief)
        self.assertNotIn("The fence", gi.setting_plate())

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


class ImageFillTestCase(_IdentityFixture):
    """Empty sheets draft themselves from a reference plate; authored text stays."""

    PNG = ReferenceImageTestCase.PNG

    def setUp(self):
        super().setUp()
        import ai_provider_manager as apm
        self._apm = apm
        apm.set_backend_override("mock")

    def tearDown(self):
        self._apm.set_backend_override(None)
        super().tearDown()

    def _attach(self, kind="character"):
        meta = gi.save_reference(self.PNG, kind, "plate.png")
        key = gi.CHARACTER_KEY if kind == "character" else gi.SETTING_KEY
        gi.save_spec({key: {"reference_images": [meta["id"]]}})
        return meta, key

    def test_a_sheet_with_no_plate_still_drafts_from_the_world(self):
        """The half of the contract that was missing.

        Every route into autofill went through vision, so a sheet could only be
        drafted from an attached plate — and a world authored in words has no
        plate. Each field the schema gained therefore stayed permanently empty on
        those worlds: setting_reference.goal was "", level_goal() fell back to
        naming a landmark, and the opening montage told the player they had come
        here to reach a fence they were already standing at.
        """
        ps.save_prompts_bulk({"world_initial_state":
                              "A 1993 quarantine perimeter under a red mesa. " * 40})
        gi.save_spec({gi.SETTING_KEY: {"name": "the fence", "summary": "",
                                       "goal": "", "reference_images": []}})

        drafted = {"summary": "A company perimeter nobody maintains.",
                   "goal": "The pump house with the red door."}
        with unittest.mock.patch.object(gi, "infer_fields_from_text",
                                        return_value=drafted) as text_fill:
            result = gi.apply_image_fill(gi.SETTING_KEY)

        self.assertTrue(text_fill.called, "no plate must not mean no draft")
        self.assertEqual(result["source"], "text")
        self.assertFalse(result["skipped"], result)
        spec = gi.get_spec()[gi.SETTING_KEY]
        self.assertEqual(spec["goal"], "The pump house with the red door.")

    def test_a_plate_that_cannot_answer_a_field_defers_to_the_world(self):
        """A photograph of a valley cannot say what the player came here for.

        So the image path fills what it can see and the prose fills the rest;
        choosing a reference image has to leave the sheet COMPLETE, not merely
        complete in the fields a photograph happens to answer.
        """
        meta, key = self._attach("level")
        gi.save_spec({key: {"goal": "", "summary": ""}})

        with unittest.mock.patch.object(gi, "infer_fields_from_image",
                                        return_value={"summary": "A fenced yard."}), \
             unittest.mock.patch.object(gi, "infer_fields_from_text",
                                        return_value={"goal": "The pump house."}) as txt:
            result = gi.apply_image_fill(key, ref_id=meta["id"])

        self.assertEqual(result["source"], "vision+text")
        self.assertIn("goal", txt.call_args[0][1], "only the blanks go to the prose")
        spec = gi.get_spec()[key]
        self.assertEqual(spec["summary"], "A fenced yard.")
        self.assertEqual(spec["goal"], "The pump house.")

    def test_a_world_with_nothing_written_drafts_nothing(self):
        """No bible and no filled fields means there is nothing to read."""
        ps.save_prompts_bulk({"world_initial_state": ""})
        gi.save_spec({gi.SETTING_KEY: {"name": "", "summary": "", "goal": "",
                                       "era": "", "palette": "", "landmarks": "",
                                       "opening_shot": "", "reference_images": []}})
        self.assertEqual(gi.infer_fields_from_text(gi.SETTING_KEY), {})

    def test_fillable_blocks_are_character_and_level(self):
        self.assertEqual(
            set(gi.image_fillable_blocks()),
            {gi.CHARACTER_KEY, gi.SETTING_KEY},
        )
        self.assertIsNone(gi.block_for_image_kind("camera"))
        self.assertEqual(gi.block_for_image_kind("level"), gi.SETTING_KEY)

    def test_empty_character_fields_fill_from_image(self):
        meta, key = self._attach("character")
        result = gi.apply_image_fill(key, ref_id=meta["id"])
        self.assertFalse(result["skipped"])
        self.assertEqual(result["backend"], "mock")
        self.assertIn("name", result["filled"])
        spec = gi.get_spec()[key]
        self.assertEqual(spec["name"], "Mock Wren")
        self.assertEqual(spec["role"], "field researcher")
        self.assertTrue(spec["appearance"])
        self.assertTrue(spec["enabled"])

    def test_empty_level_fields_fill_from_image(self):
        meta, key = self._attach("setting")
        result = gi.apply_image_fill(key, ref_id=meta["id"])
        self.assertIn("name", result["filled"])
        spec = gi.get_spec()[key]
        self.assertEqual(spec["name"], "The Open Ground")
        self.assertTrue(spec["summary"])
        self.assertTrue(spec["landmarks"])
        self.assertTrue(spec["opening_shot"])
        self.assertTrue(spec["enabled"])

    def test_existing_text_is_not_overwritten(self):
        meta, key = self._attach("character")
        gi.save_spec({key: {"name": "Wren Alvarez", "role": "salvage diver"}})
        result = gi.apply_image_fill(key, ref_id=meta["id"])
        spec = gi.get_spec()[key]
        self.assertEqual(spec["name"], "Wren Alvarez")
        self.assertEqual(spec["role"], "salvage diver")
        self.assertNotIn("name", result["filled"])
        self.assertNotIn("role", result["filled"])
        self.assertIn("appearance", result["filled"])
        self.assertTrue(spec["appearance"])

    def test_skips_vision_when_every_text_field_is_filled(self):
        meta, key = self._attach("character")
        patch = {f["id"]: "kept" for f in gi.fillable_text_fields(key)}
        gi.save_spec({key: patch})
        result = gi.apply_image_fill(key, ref_id=meta["id"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "all_filled")
        self.assertEqual(gi.get_spec()[key]["name"], "kept")

    def test_overwrite_replaces_leftover_jason_from_the_plate(self):
        meta, key = self._attach("character")
        gi.save_spec({key: {
            "name": "Jason Fleece",
            "role": "investigative photojournalist",
            "appearance": "adult man, short dark hair, weathered face, stubble",
        }})
        result = gi.apply_image_fill(key, ref_id=meta["id"], overwrite=True)
        spec = gi.get_spec()[key]
        self.assertFalse(result["skipped"])
        self.assertEqual(spec["name"], "Mock Wren")
        self.assertEqual(spec["role"], "field researcher")
        self.assertNotIn("Jason", spec["name"])
        self.assertNotIn("adult man", spec["appearance"])

    def test_attach_and_fill_is_one_write_after_the_draft(self):
        """The editor must not see a plate-only snapshot of leftover CAST."""
        gi.save_spec({gi.CHARACTER_KEY: {
            "enabled": True,
            "name": "Jason Fleece",
            "role": "investigative photojournalist",
            "appearance": "adult man, stubble",
            "reference_images": [],
        }})
        meta = gi.save_reference(self.PNG, "character", "plate.png")
        before_refs = list(gi.get_spec()[gi.CHARACTER_KEY].get("reference_images") or [])
        self.assertEqual(before_refs, [])
        result = gi.attach_reference_and_fill(gi.CHARACTER_KEY, meta["id"])
        spec = gi.get_spec()[gi.CHARACTER_KEY]
        self.assertEqual(spec["name"], "Mock Wren")
        self.assertEqual(spec["reference_images"], [meta["id"]])
        self.assertTrue(spec["enabled"])
        self.assertIn("name", result["fields"])
        self.assertEqual(result["attached"], meta["id"])

    def test_parse_fill_json_accepts_markdown_fences(self):
        parsed = gi._parse_fill_json("```json\n{\"name\": \"Ivy\", \"role\": \"scout\"}\n```")
        self.assertEqual(parsed["name"], "Ivy")
        self.assertEqual(parsed["role"], "scout")

    def test_mock_path_never_needs_a_network(self):
        import os
        old_gemini = os.environ.pop("GEMINI_API_KEY", None)
        old_openai = os.environ.pop("OPENAI_API_KEY", None)
        try:
            meta, key = self._attach("character")
            result = gi.apply_image_fill(key, ref_id=meta["id"])
            self.assertEqual(result["backend"], "mock")
            self.assertTrue(result["filled"])
        finally:
            if old_gemini is not None:
                os.environ["GEMINI_API_KEY"] = old_gemini
            if old_openai is not None:
                os.environ["OPENAI_API_KEY"] = old_openai


class TestTheDraftDoesNotWearTheExample(_IdentityFixture):
    """Reported as "my character appeared as an orange jump suit for a frame".

    The fill prompt handed the model each field's PLACEHOLDER as the hint, so
    asking for a wardrobe read `- "wardrobe": Wardrobe — Patched orange dive
    suit, mismatched boots, canvas satchel.` and the model echoed it back. Five
    of the eight character fields on the reporter's sheet were verbatim
    placeholders — the dive suit, "Dented Nikon F3, sodium lamp", she/her, the
    temperament and the backstory — over a photograph of somebody else. All
    five are `advanced`, so the minimal editor never showed them.
    """

    def _placeholder(self, block_id, fid):
        field = next(f for f in gi.fillable_text_fields(block_id) if f["id"] == fid)
        return field["placeholder"]

    def test_the_prompt_does_not_offer_the_placeholder_as_the_answer(self):
        prompt = gi._identity_fill_prompt(gi.CHARACTER_KEY, ["wardrobe"])
        ph = self._placeholder(gi.CHARACTER_KEY, "wardrobe")
        keys = prompt.split("Return ONLY a JSON object", 1)[1]
        keys = keys.split("These are the editor's own example strings", 1)[0]
        self.assertNotIn(ph, keys,
                         "the key list must describe the field, not answer it")

    def test_an_example_that_is_shown_is_shown_as_one_to_avoid(self):
        prompt = gi._identity_fill_prompt(gi.CHARACTER_KEY, ["wardrobe"])
        ph = self._placeholder(gi.CHARACTER_KEY, "wardrobe")
        if ph in prompt:
            self.assertIn("never return", prompt.lower())
            self.assertIn("do not reuse their content", prompt.lower())

    def test_an_echoed_placeholder_is_dropped(self):
        ph = self._placeholder(gi.CHARACTER_KEY, "wardrobe")
        kept = gi._drop_placeholder_echoes(
            gi.CHARACTER_KEY,
            {"wardrobe": ph, "appearance": "a woman in a blue press vest"},
        )
        self.assertNotIn("wardrobe", kept)
        self.assertEqual(kept["appearance"], "a woman in a blue press vest")

    def test_the_echo_check_is_not_fooled_by_case_or_trailing_stops(self):
        ph = self._placeholder(gi.CHARACTER_KEY, "signature_gear")
        kept = gi._drop_placeholder_echoes(
            gi.CHARACTER_KEY, {"signature_gear": ph.upper() + "."})
        self.assertEqual(kept, {})

    def test_a_real_answer_survives(self):
        kept = gi._drop_placeholder_echoes(
            gi.CHARACTER_KEY, {"wardrobe": "a scorched hazmat smock"})
        self.assertEqual(kept["wardrobe"], "a scorched hazmat smock")

    def test_no_shipped_sheet_wears_a_placeholder(self):
        """The live file and every World snapshot, so a cleaned sheet cannot
        be quietly restored by binding a World that still holds one."""
        root = Path(__file__).resolve().parent
        targets = [("prompts/simulation_prompts.json", None)]
        targets += [(f"worlds/{p.name}", None) for p in sorted((root / "worlds").glob("*.json"))
                    if not p.name.endswith(".frame.json")]
        offenders = []
        for rel, _ in targets:
            path = root / rel
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            blob = data.get("prompts") if "worlds/" in rel else data
            if not isinstance(blob, dict):
                continue
            for block_id in (gi.CHARACTER_KEY, gi.SETTING_KEY):
                block = blob.get(block_id)
                if not isinstance(block, dict):
                    continue
                for field in gi.fillable_text_fields(block_id):
                    ph = str(field.get("placeholder") or "").strip()
                    val = str(block.get(field["id"]) or "").strip()
                    if ph and val and gi._norm_field(val) == gi._norm_field(ph):
                        offenders.append(f"{rel}:{block_id}.{field['id']}")
        self.assertEqual(offenders, [],
                         "these are the editor's examples saved as real values")


class TestTheOpeningShotObeysItsOwnCamera(_IdentityFixture):
    """The plate this writes is the opening montage's reference AND the frame
    turn one continues from, so whatever pose it describes propagates through
    the whole run.

    It used to say the protagonist "is in frame, seen by the camera ... standing
    in <place>" — the exact shot the follow-cam rig bans two paragraphs earlier
    ("no walking-toward-camera arrival, no front-facing portrait"). The rig is
    rules in capitals; this is concrete prose about the subject, and concrete
    wins (see the precedence note in engine.build_image_prompt). So every
    follow-cam world opened on the hero strolling at the lens, stood still.
    """

    def _vision(self, mode):
        self._set_character()
        self._set_mode(mode)
        return gi.opening_shot()["vision"]

    def test_a_follow_cam_opening_faces_into_the_scene(self):
        for mode in ("third_person", "over_shoulder"):
            with self.subTest(mode=mode):
                v = self._vision(mode)
                self.assertIn("BEHIND", v)
                self.assertIn("facing INTO the place", v)

    def test_it_never_asks_for_the_shot_the_rig_forbids(self):
        for mode in ("third_person", "over_shoulder", "fixed_cinematic"):
            with self.subTest(mode=mode):
                v = self._vision(mode).lower()
                self.assertNotIn("seen by the camera", v)
                self.assertNotIn("standing in", v)

    def test_the_pose_is_a_moment_not_a_portrait(self):
        """"Standing" is why the opening had no charge: the first frame of a
        horror game was a man stood still, waiting to be looked at."""
        v = self._vision("third_person")
        self.assertIn("mid-stride", v)
        self.assertIn("arrested motion, not a pose", v)

    def test_a_locked_off_camera_is_not_told_to_shoot_from_behind(self):
        """Fixed cinematic is an angle the character walks INTO, so "from
        behind" would fight the rig rather than serve it."""
        v = self._vision("fixed_cinematic")
        self.assertNotIn("BEHIND", v)
        self.assertIn("not looking at the lens", v)
        self.assertIn("dwarfed by the space", v)

    def test_the_character_is_still_named_and_described(self):
        v = self._vision("third_person")
        self.assertIn("Wren Alvarez", v)
        self.assertIn("orange dive suit", v)

    def test_first_person_still_declines(self):
        self._set_character()
        self._set_mode("first_person")
        self.assertIsNone(gi.opening_shot())


if __name__ == "__main__":
    unittest.main(verbosity=2)
