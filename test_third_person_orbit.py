#!/usr/bin/env python3
"""Third person orbits left/right. Pitch stays a first-person instrument."""

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent


class ThirdPersonOrbitsYawOnly(unittest.TestCase):
    def setUp(self):
        self.js = (ROOT / "static" / "js" / "standalone.js").read_text(
            encoding="utf-8", errors="replace")

    def test_follow_cams_are_marked_yaw_only(self):
        cam = self.js.split("yawOnly()", 1)[1][:400]
        self.assertIn("third_person", cam)
        self.assertIn("over_shoulder", cam)

    def test_mouse_feed_drops_vertical_on_follow_cams(self):
        feed = self.js.split("function feed(dx, dy)", 1)[1][:400]
        self.assertIn("yawOnlyLook()", feed)
        self.assertIn("dy = 0", feed)

    def test_drive_layer_zeros_pitch_after_keys_and_mouse(self):
        compose = self.js.split("function compose()", 1)[1][:2200]
        self.assertGreaterEqual(compose.count("Camera.yawOnly"), 2)

    def test_controls_hide_look_up_down_on_follow_cams(self):
        self.assertIn("hidePitch && (act.id === \"pitchUp\"", self.js)
        self.assertIn("mouse orbits", self.js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
