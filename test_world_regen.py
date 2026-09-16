"""The editor's promise: supply nothing and the world fills itself in.

It held for one source (a plate) on two blocks (character, level) and nowhere
else, so every field the schema gained stayed blank on a world authored in words —
`setting_reference.goal` was "", and `camera_perspective.lens`/`.notes` could not
be filled by any path at all. These pin the wider contract and, just as much, pin
that it REPORTS: a regeneration nobody can see is indistinguishable from one that
did not happen, which is how the world stayed hollow through a dozen runs.
"""
import os
import unittest
import unittest.mock

os.environ.setdefault("GEMINI_API_KEY", "")

import game_identity as gi
import world_regen


class TestTheContractCoversTheWholeEditor(unittest.TestCase):
    def test_camera_can_be_drafted_even_though_it_has_no_plate(self):
        """The block that no fill path could reach.

        A camera has no photograph to read, so `supports_images` is correctly
        false — and every fill route was gated on exactly that, which left lens
        and notes permanently blank.
        """
        self.assertNotIn(gi.CAMERA_KEY, gi.image_fillable_blocks())
        self.assertIn(gi.CAMERA_KEY, gi.text_fillable_blocks())
        ids = [f["id"] for f in gi.fillable_text_fields(gi.CAMERA_KEY)]
        self.assertEqual(sorted(ids), ["lens", "notes"])

    def test_every_block_with_text_fields_is_reachable(self):
        for block in gi.SPEC_KEYS:
            if gi.fillable_text_fields(block):
                self.assertIn(block, gi.text_fillable_blocks(), block)


class TestThePlan(unittest.TestCase):
    def test_it_names_what_is_missing_without_changing_anything(self):
        with unittest.mock.patch.object(gi, "empty_fill_fields",
                                        return_value=["goal"]), \
             unittest.mock.patch.object(gi, "text_fillable_blocks",
                                        return_value=["setting_reference"]), \
             unittest.mock.patch("scene_audio.get_music_direction",
                                 return_value="a score"):
            plan = world_regen.plan()
        self.assertEqual(plan["blocks"]["setting_reference"]["empty"], ["goal"])
        self.assertFalse(plan["music"]["empty"])
        self.assertEqual(plan["missing"], 1)

    def test_blank_music_counts_as_missing(self):
        with unittest.mock.patch.object(gi, "text_fillable_blocks", return_value=[]), \
             unittest.mock.patch("scene_audio.get_music_direction", return_value=""):
            plan = world_regen.plan()
        self.assertTrue(plan["music"]["empty"])
        self.assertEqual(plan["missing"], 1)


class TestRegenerate(unittest.TestCase):
    def test_a_plate_block_uses_vision_and_a_bare_block_uses_text(self):
        """Each block draws on the source it actually has."""
        def spec():
            return {"player_character": {"reference_images": ["ref1"]},
                    "camera_perspective": {"reference_images": []}}

        with unittest.mock.patch.object(gi, "text_fillable_blocks",
                                        return_value=["player_character",
                                                      "camera_perspective"]), \
             unittest.mock.patch.object(gi, "image_fillable_blocks",
                                        return_value=["player_character"]), \
             unittest.mock.patch.object(gi, "get_spec", side_effect=spec), \
             unittest.mock.patch.object(gi, "apply_image_fill",
                                        return_value={"filled": {"name": "A"},
                                                      "source": "vision"}) as img, \
             unittest.mock.patch.object(gi, "apply_text_fill",
                                        return_value={"filled": {"lens": "35mm"},
                                                      "source": "text"}) as txt, \
             unittest.mock.patch.object(world_regen, "_regen_music",
                                        return_value={"skipped": True,
                                                      "reason": "already_written"}):
            report = world_regen.regenerate()

        self.assertTrue(img.called, "a block with a plate should use it")
        self.assertTrue(txt.called, "a block without one still gets drafted")
        self.assertEqual(report["filled"], 2)

    def test_a_block_that_raises_does_not_take_the_pass_down(self):
        """One broken block must not stop the rest of the world regenerating."""
        with unittest.mock.patch.object(gi, "text_fillable_blocks",
                                        return_value=["setting_reference"]), \
             unittest.mock.patch.object(gi, "image_fillable_blocks", return_value=[]), \
             unittest.mock.patch.object(gi, "get_spec", return_value={}), \
             unittest.mock.patch.object(gi, "apply_text_fill",
                                        side_effect=RuntimeError("boom")), \
             unittest.mock.patch.object(world_regen, "_regen_music",
                                        return_value={"skipped": True, "reason": "x"}):
            report = world_regen.regenerate()
        self.assertIn("raised: boom", report["blocks"]["setting_reference"]["reason"])
        self.assertEqual(report["filled"], 0)

    def test_authored_text_survives_unless_overwrite_is_asked_for(self):
        seen = {}

        def record(block, *, overwrite=False):
            seen[block] = overwrite
            return {"filled": {}, "skipped": True, "reason": "all_filled"}

        with unittest.mock.patch.object(gi, "text_fillable_blocks",
                                        return_value=["setting_reference"]), \
             unittest.mock.patch.object(gi, "image_fillable_blocks", return_value=[]), \
             unittest.mock.patch.object(gi, "get_spec", return_value={}), \
             unittest.mock.patch.object(gi, "apply_text_fill", side_effect=record), \
             unittest.mock.patch.object(world_regen, "_regen_music",
                                        return_value={"skipped": True, "reason": "x"}):
            world_regen.regenerate()
            self.assertFalse(seen["setting_reference"])
            world_regen.regenerate(overwrite=True)
            self.assertTrue(seen["setting_reference"])


class TestMusic(unittest.TestCase):
    def test_a_written_score_is_left_alone(self):
        with unittest.mock.patch("scene_audio.get_music_direction",
                                 return_value="sparse analog drones"):
            result = world_regen._regen_music(overwrite=False)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "already_written")

    def test_a_paragraph_is_refused_rather_than_saved(self):
        """A score note that long is not usable as one, and worse than the default."""
        with unittest.mock.patch("scene_audio.get_music_direction", return_value=""), \
             unittest.mock.patch("prompts_store.PROMPTS",
                                 {"world_initial_state": "A 1993 quarantine. " * 40}), \
             unittest.mock.patch("engine._ask", return_value="x " * 200), \
             unittest.mock.patch("scene_audio.set_music_direction") as saved:
            result = world_regen._regen_music(overwrite=False)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "nothing_usable")
        self.assertFalse(saved.called)

    def test_it_will_not_invent_a_score_for_a_world_with_no_prose(self):
        with unittest.mock.patch("scene_audio.get_music_direction", return_value=""), \
             unittest.mock.patch("prompts_store.PROMPTS", {"world_initial_state": ""}), \
             unittest.mock.patch("scene_audio.set_music_direction") as saved:
            result = world_regen._regen_music(overwrite=False)
        self.assertEqual(result["reason"], "no_world_to_read")
        self.assertFalse(saved.called)


class TestItSaysWhatItDid(unittest.TestCase):
    def test_the_report_names_every_field_and_its_source(self):
        lines = "\n".join(world_regen.describe({
            "blocks": {
                "setting_reference": {"filled": {"goal": "The pump house."},
                                      "source": "text"},
                "player_character": {"filled": {}, "reason": "all_filled"},
            },
            "music": {"skipped": False, "direction": "analog drones"},
            "filled": 2,
        }))
        self.assertIn("setting_reference: drafted 1 field(s) from text", lines)
        self.assertIn("goal = The pump house.", lines)
        self.assertIn("player_character: nothing drafted (all_filled)", lines)
        self.assertIn("music: analog drones", lines)
        self.assertIn("TOTAL fields drafted: 2", lines)


if __name__ == "__main__":
    unittest.main()
