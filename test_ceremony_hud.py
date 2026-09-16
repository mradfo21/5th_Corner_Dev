#!/usr/bin/env python3
"""The turn pipeline is one top-right circle, not a bottom action bar."""

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent


class TheCeremonyIsACornerCircle(unittest.TestCase):
    def setUp(self):
        self.css = (ROOT / "static" / "css" / "standalone.css").read_text(
            encoding="utf-8", errors="replace")
        self.js = (ROOT / "static" / "js" / "standalone.js").read_text(
            encoding="utf-8", errors="replace")

    def test_the_hud_sits_top_right_not_across_the_frame(self):
        veil = self.css.split("#processing-veil {", 1)[1][:500]
        self.assertIn("inset: auto", veil)
        self.assertIn("right:", veil)
        self.assertNotIn("align-items: flex-end", veil)

    def test_only_the_live_phase_is_drawn(self):
        self.assertIn(".cere-step.active,", self.css)
        step = self.css.split(".cere-step {", 1)[1][:400]
        self.assertIn("display: none", step)

    def test_the_ring_spins_like_a_loader(self):
        self.assertIn("@keyframes cere-spin", self.css)
        self.assertIn("cere-orb", self.css)

    def test_six_phases_still_exist_they_just_replace_each_other(self):
        steps = self.js.split("const STEPS = [", 1)[1].split("];", 1)[0]
        for key in ("action", "consequence", "world_update",
                    "world_respond", "actions", "guide_image"):
            self.assertIn('key: "' + key + '"', steps)
        self.assertIn("hud:", steps)


if __name__ == "__main__":
    unittest.main(verbosity=2)
