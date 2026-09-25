"""GENERATE fills what a World lacks: a bible and a real level name.

Asked: "SWAT has no world bible, so its opening montage uses the built-in
scene descriptions. THE FIFTH CORNER has no level name. Why aren't these
fixed in the generation process?" (CHANGELOG, "GENERATE writes the bible a
World was missing").

Never touches the network: the model call is mocked.

Run: python -m unittest test_world_gaps -v
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

import engine  # noqa: F401  (world_gaps imports it lazily)
import game_identity as gi
import prompts_store
import world_gaps
import worlds_store

ROOT = Path(__file__).resolve().parent
THIN = ("A playable place in third person. The camera follows a person through space. "
        "Each action moves the body or changes the room.")
SWAT = {
    "world_initial_state": THIN,
    "setting_reference": {"enabled": True, "name": "World",
                          "summary": "A rain-slicked street choked by a protest and a robot police blockade.",
                          "goal": "The president has been kidnapped."},
    "player_character": {"name": "Simon 'Ghost' Riley", "role": "Special operations operative"},
}
PREMISE = "You move through a city at war with its own police. " * 40


class WhatAWorldLacks(unittest.TestCase):
    def test_the_blank_place_lacks_both(self):
        self.assertEqual(world_gaps.gaps(SWAT), ["bible", "level_name"])

    def test_an_authored_world_lacks_nothing(self):
        p = dict(SWAT, world_initial_state="x" * 500,
                 setting_reference=dict(SWAT["setting_reference"], name="Sector Four"))
        self.assertEqual(world_gaps.gaps(p), [])

    def test_placeholder_names(self):
        for n in ("", "World", "world 2", "New Level", "an open place", "Untitled World"):
            self.assertTrue(world_gaps.is_placeholder_name(n), n)
        for n in ("SOMEWHERE", "Sector 044 Sub-Level", "The Riot Line"):
            self.assertFalse(world_gaps.is_placeholder_name(n), n)


class FillingThem(unittest.TestCase):
    def setUp(self):
        self.live = dict(SWAT)
        self.patched = {}
        self.saved = {}
        p = [
            mock.patch.object(prompts_store, "PROMPTS", self.live),
            mock.patch.object(worlds_store, "bound_slug", return_value="world"),
            mock.patch.object(worlds_store, "patch_world_prompts",
                              side_effect=lambda slug, f: self.patched.update({slug: f}) or True),
            mock.patch.object(prompts_store, "save_prompts_bulk",
                              side_effect=lambda f: self.saved.update(f) or {}),
        ]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def ask(self, reply):
        return mock.patch.object(engine, "_ask", return_value=json.dumps(reply))

    def test_it_writes_both_into_the_world_and_the_live_sheet(self):
        with self.ask({"level_name": "The Riot Line", "premise": PREMISE}) as ask:
            got = world_gaps.fill("world")
        self.assertEqual(got["setting_reference"]["name"], "The Riot Line")
        # the rest of the Level sheet is untouched
        self.assertEqual(got["setting_reference"]["goal"], "The president has been kidnapped.")
        self.assertIn("world", self.patched)
        self.assertEqual(self.saved["setting_reference"]["name"], "The Riot Line")
        # the author's own words are kept, after the drafted premise
        bible = got["world_initial_state"]
        self.assertTrue(bible.startswith(PREMISE.strip()))
        self.assertTrue(bible.endswith(THIN))
        # the draft is written from what IS authored, lore included
        prompt = ask.call_args[0][0]
        self.assertIn("robot police blockade", prompt)
        self.assertIn("Ghost", prompt)
        self.assertTrue(ask.call_args.kwargs.get("use_lore"))

    def test_nothing_missing_means_no_call(self):
        self.live.update(world_initial_state="x" * 500,
                         setting_reference=dict(SWAT["setting_reference"], name="Sector Four"))
        with self.ask({}) as ask:
            self.assertEqual(world_gaps.fill("world"), {})
        ask.assert_not_called()
        self.assertEqual(self.patched, {})

    def test_only_the_missing_part_is_written(self):
        self.live.update(setting_reference=dict(SWAT["setting_reference"], name="Sector Four"))
        with self.ask({"level_name": "Something Else", "premise": PREMISE}):
            got = world_gaps.fill("world")
        self.assertNotIn("setting_reference", got)
        self.assertIn("world_initial_state", got)

    def test_a_failed_or_thin_draft_leaves_the_world_alone(self):
        with mock.patch.object(engine, "_ask", side_effect=RuntimeError("down")):
            self.assertEqual(world_gaps.fill("world"), {})
        with self.ask({"level_name": "World", "premise": "too short"}):
            self.assertEqual(world_gaps.fill("world"), {})
        self.assertEqual(self.patched, {})
        self.assertEqual(self.saved, {})

    def test_it_only_writes_the_world_that_is_bound(self):
        with self.ask({"level_name": "The Riot Line", "premise": PREMISE}) as ask:
            self.assertEqual(world_gaps.fill("somewhere"), {})
        ask.assert_not_called()


class TheFifthCornerKeepsItsName(unittest.TestCase):
    def spec(self):
        spec = gi.get_spec()
        spec = json.loads(json.dumps(spec))
        spec[gi.SETTING_KEY] = dict(spec[gi.SETTING_KEY], enabled=True, name="SOMEWHERE",
                                    summary="Horizon Industries quarantine perimeter under the red mesa.",
                                    era="1993")
        return spec

    def test_its_own_world_keeps_the_name(self):
        with mock.patch.object(worlds_store, "bound_slug", return_value="somewhere"):
            self.assertEqual(gi.authored_setting(self.spec())["name"], "SOMEWHERE")

    def test_a_recast_over_another_world_still_drops_it(self):
        with mock.patch.object(worlds_store, "bound_slug", return_value="world"):
            self.assertEqual(gi.authored_setting(self.spec())["name"], "")


class WhereItRuns(unittest.TestCase):
    def test_generate_and_the_run_start_both_fill(self):
        api = (ROOT / "api.py").read_text(encoding="utf-8")
        gen = api[api.index("def admin_studio_world_frames_reset"):]
        self.assertLess(gen.index("world_gaps.fill(slug"), gen.index("world_frames.force_reset("))
        self.assertLess(gen.index("world_gaps.fill(slug"), gen.index("look_book.prebuild_for_generate("))
        eng = (ROOT / "engine.py").read_text(encoding="utf-8")
        prep = eng[eng.index("def prepare_level_look_book"):][:2500]
        self.assertLess(prep.index("world_gaps.fill(bound"), prep.index("look_book.prepare_for_level("))


if __name__ == "__main__":
    unittest.main()
