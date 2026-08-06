"""
test_world_authoring.py — proof that an authored world actually reaches the model.

`test_game_identity.py` covers the cast sheet in isolation: does it compile, does
it normalize, is it inert at defaults. This file covers the thing that was
actually broken — the WIRING. A dozen prompt surfaces built their own text
without ever asking who the player is, where they are, or where the camera sits,
so authoring a character and a level changed the still images and nothing else:
the live world model, the flipbook, the video path, the vision loop that decides
what the NEXT frame looks like, the camp shot, the talk personas, and the
per-turn world rewrite all carried the shipped Four Corners photojournalist.

Each test here authors a world, then asserts a specific surface carries it —
and, just as importantly, that the same surface is byte-identical to the shipped
text while the sheet sits at its defaults.

Never touches the network.

Run with:
    python3 -m unittest test_world_authoring -v
"""

import json
import tempfile
import unittest
from pathlib import Path

import prompts_store as ps
import game_identity as gi
import engine
import veo_video_utils


SAMPLE_PROMPTS = {
    "world_initial_state": "The year is 1993. You are Jason Fleece, photojournalist.",
    "world_evolution_instructions": "Never invent a new biome.",
    "action_consequence_instructions": "Return JSON with dispatch and visual_scene.",
    "situation_summary_instructions": "Describe what is happening NOW.",
    # First-person-saturated, like the shipped templates — reconciling that
    # language is what the pipeline is for.
    "gemini_text_to_image_instructions": (
        "SCENE: {prompt}\n"
        "CRITICAL POV RULE: This is FIRST-PERSON perspective.\n"
        "NEVER show any part of a human body.\n"
        "{art_direction}\n{camera_rules}"
    ),
    "gemini_image_to_image_instructions": (
        "SHOW: {prompt}\nThe camera IS your eyes.\n{art_direction}\n{camera_rules}"
    ),
    "image_art_direction": "1993 analog VHS, heavy grain, desaturated.",
    "image_camera_rules": "Eye level. ABSOLUTELY NO PERSON VISIBLE in frame.",
    "image_negative_prompt": (
        "CGI, third person perspective, over shoulder view, behind character, "
        "borders, text overlays"
    ),
    "gemini_flipbook_4panel_prefix": "Sixteen panels, left to right.",
}

# The authored world every test steers toward: a named diver in a flooded
# shipbreaking yard, watched from behind. Nothing about it resembles the
# shipped desert, so any surface that still smells of Horizon has failed.
CHARACTER = {
    "enabled": True,
    "name": "Wren Alvarez",
    "pronouns": "she/her",
    "role": "salvage diver",
    "appearance": "early thirties, cropped black hair",
    "wardrobe": "patched orange dive suit",
    "signature_gear": "sodium lamp",
    "demeanor": "dry, unflappable",
}
SETTING = {
    "enabled": True,
    "name": "The Kettle Yard",
    "summary": "A flooded shipbreaking yard on a tidal flat",
    "era": "1993, analog only",
    "palette": "rust orange, sodium haze",
    "landmarks": "the listing tanker, the crane gantry",
    "opening_shot": "Low tide at dawn, the tanker's hull filling the right of frame",
}


class _AuthoredWorldFixture(unittest.TestCase):
    """Temp prompt file so the committed simulation_prompts.json is never touched."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self._tmpdir.name)

        self._orig_prompts_path = ps.PROMPTS_PATH
        self._orig_defaults_path = ps.DEFAULTS_PATH
        ps.PROMPTS_PATH = tmp / "simulation_prompts.json"
        ps.DEFAULTS_PATH = tmp / "simulation_prompts.defaults.json"

        payload = dict(SAMPLE_PROMPTS)
        payload.update(gi.default_spec())
        for path in (ps.PROMPTS_PATH, ps.DEFAULTS_PATH):
            with path.open("w", encoding="utf-8") as f:
                json.dump(payload, f)
        self._reload()

    def tearDown(self):
        ps.PROMPTS_PATH = self._orig_prompts_path
        ps.DEFAULTS_PATH = self._orig_defaults_path
        self._reload()
        self._tmpdir.cleanup()

    def _reload(self):
        ps.PROMPTS._mtime = None
        ps.PROMPTS._last_check = 0.0
        ps.PROMPTS._reload(force=True)

    def _author_world(self, mode="over_shoulder"):
        gi.save_spec({
            gi.CHARACTER_KEY: dict(CHARACTER),
            gi.SETTING_KEY: dict(SETTING),
            gi.CAMERA_KEY: {"mode": mode},
        })


class CompactHelpersTestCase(_AuthoredWorldFixture):
    """The one-line versions of the cast sheet, for prompts too small to take
    the full directive."""

    def test_helpers_are_empty_at_defaults(self):
        self.assertEqual(gi.place_line(), "")
        self.assertEqual(gi.place_summary(), "")
        self.assertEqual(gi.protagonist_line(), "")
        self.assertEqual(gi.scene_grounding(), "")
        self.assertEqual(
            gi.structure_lines(),
            {"who": "", "where": "", "environment": "", "tone": ""},
        )

    def test_world_anchor_is_the_shipped_anchor_at_defaults(self):
        shipped = engine.REALTIME_STYLE_ANCHOR
        self.assertEqual(gi.world_anchor(shipped), shipped)

    def test_place_line_carries_name_palette_and_landmarks(self):
        self._author_world()
        line = gi.place_line()
        self.assertIn("The Kettle Yard", line)
        self.assertIn("sodium haze", line)
        self.assertIn("crane gantry", line)

    def test_scene_grounding_names_camera_place_and_character(self):
        self._author_world()
        grounding = gi.scene_grounding()
        self.assertIn("over-the-shoulder", grounding)
        self.assertIn("The Kettle Yard", grounding)
        self.assertIn("Wren Alvarez", grounding)

    def test_world_anchor_retunes_perspective_and_adds_the_level(self):
        self._author_world()
        anchor = gi.world_anchor(
            "A navigable first-person world you can walk through, 1993 analog VHS"
        )
        self.assertNotIn("first-person", anchor.lower())
        self.assertIn("over-the-shoulder", anchor)
        self.assertIn("sodium haze", anchor)
        self.assertIn("Wren Alvarez", anchor)

    def test_world_anchor_can_skip_framing_for_a_non_player_subject(self):
        self._author_world()
        anchor = gi.world_anchor(
            "cinematic medium shot, shallow depth of field",
            include_character=False,
            include_vantage=False,
        )
        self.assertIn("sodium haze", anchor)
        self.assertNotIn("Wren Alvarez", anchor)
        self.assertNotIn("over-the-shoulder", anchor)


class ReconcileSafetyTestCase(_AuthoredWorldFixture):
    """Reconcile deletes whole LINES, so a single-line prompt is one offending
    clause away from being deleted in its entirety."""

    def test_reconcile_never_blanks_a_prompt(self):
        self._author_world()
        single_line = "A quiet empty camp with no people, only the fire."
        self.assertEqual(gi.reconcile(single_line), single_line)

    def test_reconcile_still_strips_when_something_survives(self):
        self._author_world()
        text = "Show the crane gantry at dawn.\nABSOLUTELY NO PERSON VISIBLE."
        out = gi.reconcile(text)
        self.assertIn("crane gantry", out)
        self.assertNotIn("NO PERSON VISIBLE", out)


class RealtimeWorldModelTestCase(_AuthoredWorldFixture):
    """The live world model — the surface with no negative prompt, no directive
    block, and no reference plates, so the anchor is the whole contract."""

    def test_anchor_is_untouched_at_defaults(self):
        base = engine.build_realtime_base("a corridor", "")
        self.assertIn(engine.REALTIME_STYLE_ANCHOR.rstrip(". "), base)
        self.assertNotIn("The place is", base)

    def test_authored_level_and_camera_reach_the_live_world(self):
        self._author_world()
        base = engine.build_realtime_base("a corridor", "")
        self.assertIn("The Kettle Yard", base)
        self.assertIn("over-the-shoulder", base)
        self.assertNotIn("first-person", base.lower())

    def test_action_beat_follows_the_character_in_third_person(self):
        self._author_world()
        beat = engine.realtime_action_beat("Vault the railing")
        self.assertIn("Wren Alvarez", beat)
        self.assertNotIn("the view shifts as you", beat)

    def test_action_beat_is_unchanged_in_first_person(self):
        self.assertEqual(
            engine.realtime_action_beat("Vault the railing"),
            "Motion: the view shifts as you vault the railing.",
        )


class StillImagePromptTestCase(_AuthoredWorldFixture):
    """The full still-image prompt, composed the way a turn composes it:
    build_image_prompt() then the VHS wrapper."""

    def _full_prompt(self):
        scene = engine.build_image_prompt(
            dispatch="Water sluices off the deck plating.",
            player_choice="Climb the gantry ladder",
            narrative_dispatch="Your boots find the rung.",
        )
        return engine._build_vhs_prompt(scene, use_img2img=False)

    def test_authored_world_leads_the_prompt(self):
        self._author_world()
        prompt = self._full_prompt()
        # The directive leads, where image models weight hardest — ahead of the
        # scene description and ahead of the shared art direction.
        self.assertLess(prompt.index("CAMERA DIRECTIVE"), 40)
        self.assertLess(prompt.index("CAMERA DIRECTIVE"), prompt.index("1993 analog VHS"))
        self.assertIn("OVER-THE-SHOULDER THIRD-PERSON VIEW", prompt)
        self.assertIn("Wren Alvarez", prompt)
        self.assertIn("patched orange dive suit", prompt)
        self.assertIn("The Kettle Yard", prompt)
        self.assertIn("crane gantry", prompt)

    def test_nothing_left_in_the_prompt_argues_against_the_camera(self):
        self._author_world()
        prompt = self._full_prompt()
        self.assertNotIn("ABSOLUTELY NO PERSON/PLAYER VISIBLE", prompt)
        self.assertNotIn("ABSOLUTELY NO PERSON VISIBLE", prompt)
        self.assertNotIn("NEVER show any part of a human body", prompt)
        self.assertNotIn("FIRST-PERSON perspective", prompt)
        negative = prompt.split("NEGATIVE PROMPT")[-1].lower()
        self.assertNotIn("third person perspective", negative)
        self.assertNotIn("over shoulder view", negative)
        # …while unrelated bans survive the strip.
        self.assertIn("borders", negative)

    def test_prompt_is_first_person_at_defaults(self):
        prompt = self._full_prompt()
        self.assertNotIn("CAMERA DIRECTIVE", prompt)
        self.assertIn("ABSOLUTELY NO PERSON/PLAYER VISIBLE", prompt)
        self.assertIn("FIRST-PERSON perspective", prompt)
        self.assertIn("third person perspective", prompt.split("NEGATIVE PROMPT")[-1].lower())


class FlipbookTestCase(_AuthoredWorldFixture):
    """A 16-panel grid is ONE image, so its wrapper blocks are inherited by
    every panel."""

    def test_camera_block_is_first_person_at_defaults(self):
        block = engine._flipbook_camera_block()
        self.assertIn("First-person POV, camera strapped to player's chest/head", block)
        self.assertIn("Camera following a character", block)

    def test_camera_block_stops_forbidding_the_requested_shot(self):
        self._author_world()
        block = engine._flipbook_camera_block()
        self.assertNotIn("Camera following a character", block)
        self.assertIn("Wren Alvarez", block)
        self.assertIn("OVER-THE-SHOULDER", block)

    def test_hands_rule_follows_the_show_hands_toggle(self):
        gi.save_spec({gi.CAMERA_KEY: {"mode": "first_person", "show_hands": False}})
        self.assertNotIn("Hands may appear", engine._flipbook_camera_block())

    def test_action_block_is_byte_identical_at_defaults(self):
        block = engine._flipbook_action_block("Kick the door", "The door gives", True)
        self.assertIn("FIRST-PERSON ONLY - NO 3RD PERSON ALLOWED", block)
        self.assertIn("YOU MUST OBEY THIS COMMAND AT ALL COSTS", block)

    def test_action_block_puts_the_character_on_screen(self):
        self._author_world()
        block = engine._flipbook_action_block("Kick the door", "The door gives", True)
        self.assertNotIn("NO 3RD PERSON ALLOWED", block)
        self.assertIn("Wren Alvarez", block)
        self.assertIn("YOU MUST OBEY THIS COMMAND AT ALL COSTS", block)

    def test_shot_block_follows_the_camera(self):
        self.assertIn("ONE CONTINUOUS FIRST-PERSON SHOT", engine._flipbook_shot_block(False))
        self._author_world()
        self.assertNotIn("FIRST-PERSON", engine._flipbook_shot_block(False))


class VeoVideoTestCase(_AuthoredWorldFixture):
    """Veo takes one text prompt and no negative prompt, so a hardcoded
    'NEVER show the player character' was unarguable."""

    def test_first_person_doctrine_is_intact_at_defaults(self):
        prompt = veo_video_utils._build_veo_cinematic_prompt("A flooded hold", "wade in")
        self.assertIn("NEVER show the player character", prompt)

    def test_third_person_keeps_the_character_on_screen(self):
        self._author_world()
        prompt = veo_video_utils._build_veo_cinematic_prompt("A flooded hold", "wade in")
        self.assertNotIn("NEVER show the player character", prompt)
        self.assertIn("Wren Alvarez", prompt)


class VisionLoopTestCase(_AuthoredWorldFixture):
    """SCAN tags and danger grading both have to know whether the person in
    frame is the player."""

    def test_scan_ignores_the_players_hands_in_first_person(self):
        self.assertIn("viewer's own hands", engine._detect_self_rule())

    def test_scan_ignores_the_player_character_in_third_person(self):
        self._author_world()
        rule = engine._detect_self_rule()
        self.assertIn("Wren Alvarez", rule)
        self.assertIn("do NOT tag them", rule)


class CampAndPortraitTestCase(_AuthoredWorldFixture):
    """Fixed Moments still happen somewhere, and that somewhere is the
    player's level."""

    def test_camp_is_high_desert_at_defaults(self):
        prompt = engine._build_camp_prompt([], jeep_included=False)
        self.assertIn("Four Corners high-desert scrub", prompt)

    def test_camp_takes_the_authored_terrain_and_camera(self):
        self._author_world()
        prompt = engine._build_camp_prompt([], jeep_included=False)
        self.assertNotIn("Four Corners", prompt)
        self.assertIn("Kettle Yard", prompt)
        self.assertIn("over-the-shoulder", prompt)

    def test_portrait_takes_the_palette_but_not_the_player(self):
        self._author_world()
        prompt = engine.build_portrait_prompt(
            {"subject": {"label": "diver", "kind": "person"}, "situation": {}}
        )
        self.assertIn("sodium haze", prompt)
        self.assertNotIn("Wren Alvarez", prompt)


class WorldSeedTestCase(_AuthoredWorldFixture):
    """The run's world document — what every other prompt reads from."""

    def test_intro_seed_keeps_the_world_state_and_the_cast_sheet(self):
        self._author_world()
        seed = engine._intro_world_seed("You arrive at The Kettle Yard.")
        self.assertIn("The year is 1993", seed)          # world_initial_state survived
        self.assertIn("DIRECTOR'S SHEET", seed)          # cast sheet was folded in
        self.assertIn("OPENING SHOT:", seed)

    def test_intro_seed_is_just_the_world_state_at_defaults(self):
        seed = engine._intro_world_seed("You survey the Horizon facility.")
        self.assertTrue(seed.startswith(ps.PROMPTS["world_initial_state"]))
        self.assertNotIn("DIRECTOR'S SHEET", seed)


class EditorPreviewTestCase(_AuthoredWorldFixture):
    """What the editors show back. Half the cast sheet compiles into the IMAGE
    blocks, so a preview that only ever showed the director's sheet made
    appearance, wardrobe, era, and palette look like dead controls."""

    def test_character_preview_contains_the_visual_fields(self):
        self._author_world()
        block = gi.block_preview()[gi.CHARACTER_KEY]
        self.assertIn("patched orange dive suit", block["image"])
        self.assertIn("cropped black hair", block["image"])
        self.assertIn("salvage diver", block["narrative"])

    def test_setting_preview_contains_palette_and_landmarks(self):
        self._author_world()
        block = gi.block_preview()[gi.SETTING_KEY]
        self.assertIn("sodium haze", block["image"])
        self.assertIn("crane gantry", block["image"])

    def test_camera_preview_carries_its_own_negative_prompt(self):
        self._author_world()
        block = gi.block_preview()[gi.CAMERA_KEY]
        self.assertIn("OVER-THE-SHOULDER", block["image"])
        self.assertNotIn("third person perspective", block["negative"].lower())

    def test_no_notes_when_everything_is_wired(self):
        self._author_world()
        self.assertFalse(any(gi.wiring_notes().values()))

    def test_note_when_the_camera_cannot_see_the_character(self):
        gi.save_spec({
            gi.CHARACTER_KEY: dict(CHARACTER),
            gi.CAMERA_KEY: {"mode": "first_person", "show_hands": False},
        })
        notes = gi.wiring_notes()[gi.CHARACTER_KEY]
        self.assertTrue(any("never sees your character" in n for n in notes))

    def test_note_when_a_block_is_enabled_but_empty(self):
        gi.save_spec({gi.SETTING_KEY: {"enabled": True}})
        notes = gi.wiring_notes()[gi.SETTING_KEY]
        self.assertTrue(any("every field is blank" in n for n in notes))

    def test_note_when_a_filled_in_block_is_switched_off(self):
        self._author_world()
        gi.save_spec({gi.SETTING_KEY: {"enabled": False}})
        notes = gi.wiring_notes()[gi.SETTING_KEY]
        self.assertTrue(any("Switched off" in n for n in notes))

    def test_preview_exposes_the_compact_forms(self):
        self._author_world()
        compact = gi.preview()["compact"]
        self.assertIn("The Kettle Yard", compact["place_line"])
        self.assertIn("Wren Alvarez", compact["protagonist_line"])
        self.assertIn("over-the-shoulder", compact["vantage"])


class EnableOnIntentTestCase(_AuthoredWorldFixture):
    """The toggle is for A/B-ing a character, not a gate you have to remember.

    As a gate it was the worst kind of trap: you'd write a protagonist, watch
    every field save successfully, watch the game ignore all of it, and have
    nothing on screen explaining why.
    """

    def test_naming_a_character_switches_the_block_on(self):
        self.assertFalse(gi.get_spec()[gi.CHARACTER_KEY]["enabled"])
        gi.save_spec({gi.CHARACTER_KEY: {"name": "Wren Alvarez"}})
        self.assertTrue(gi.character_enabled())

    def test_describing_a_level_switches_the_block_on(self):
        gi.save_spec({gi.SETTING_KEY: {"summary": "A flooded shipbreaking yard"}})
        self.assertTrue(gi.setting_enabled())

    def test_switching_off_explicitly_is_respected(self):
        self._author_world()
        gi.save_spec({gi.CHARACTER_KEY: {"enabled": False}})
        self.assertFalse(gi.character_enabled())
        # …and stays off while you keep editing in the same request.
        gi.save_spec({gi.CHARACTER_KEY: {"enabled": False, "role": "diver"}})
        self.assertFalse(gi.character_enabled())

    def test_deleting_a_reference_plate_never_enables_anything(self):
        gi.save_spec({gi.CHARACTER_KEY: {"reference_images": []}})
        self.assertFalse(gi.get_spec()[gi.CHARACTER_KEY]["enabled"])

    def test_camera_block_has_no_enable_to_infer(self):
        gi.save_spec({gi.CAMERA_KEY: {"lens": "28mm"}})
        self.assertEqual(gi.camera_mode(), gi.DEFAULT_MODE)


class CastSheetSurfaceTestCase(unittest.TestCase):
    """How many controls the cast sheet actually asks you to fill in.

    Reads the real schema, since the point is the shipped form's shape.
    """

    def test_every_field_declares_a_tier(self):
        for block in gi.identity_schema():
            for field in block["fields"]:
                self.assertIn(field["tier"], (gi.TIER_ESSENTIAL, gi.TIER_ADVANCED),
                              f"{block['id']}.{field['id']}")

    def test_essentials_alone_can_author_a_whole_world(self):
        """Everything the compile stages actually need has to be reachable
        without opening a single disclosure — otherwise 'advanced' is hiding
        required input, which is worse than showing everything."""
        essential = {
            block["id"]: {f["id"] for f in block["fields"] if f["tier"] == gi.TIER_ESSENTIAL}
            for block in gi.identity_schema()
        }
        # A character the image model can lock onto, and that turns the sheet on.
        self.assertLessEqual({"enabled", "name", "role", "appearance"},
                             essential[gi.CHARACTER_KEY])
        # A level with a first frame and recurring geography.
        self.assertLessEqual({"enabled", "name", "summary", "landmarks", "opening_shot"},
                             essential[gi.SETTING_KEY])
        # The camera switch itself.
        self.assertIn("mode", essential[gi.CAMERA_KEY])

    def test_the_default_form_stays_small(self):
        """A cap, not a target. Twenty equal-looking inputs is where the sheet
        stops reading as 'who am I and where am I'."""
        shown = sum(
            1 for block in gi.identity_schema()
            for f in block["fields"] if f["tier"] == gi.TIER_ESSENTIAL
        )
        self.assertLessEqual(shown, 12)


class WorldEvolutionTestCase(_AuthoredWorldFixture):
    """The per-turn rewrite. Its output becomes the world every other prompt
    reads next turn, so anything it drifts away from is gone for good."""

    def test_structure_lines_are_empty_at_defaults(self):
        lines = gi.structure_lines()
        self.assertFalse(any(lines.values()))

    def test_structure_lines_describe_the_authored_world(self):
        self._author_world()
        lines = gi.structure_lines()
        self.assertIn("Wren Alvarez", lines["who"])
        self.assertIn("The Kettle Yard", lines["where"])
        self.assertIn("shipbreaking", lines["environment"])
        self.assertIn("sodium haze", lines["tone"])

    def test_house_rules_prompt_is_editable_in_the_studio(self):
        self.assertIn("world_evolution_instructions", ps.PROMPT_SCHEMA_BY_ID)
        self.assertEqual(
            ps.PROMPT_SCHEMA_BY_ID["world_evolution_instructions"]["group"], "world"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
