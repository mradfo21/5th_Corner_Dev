"""The opening montage is four photographs of a place with NOBODY in them.

Reported: "i saw a random character in the opening cutscene, then i see our
hero". Three things put a stranger there, each pinned here:

  * in third person, generate_with_gemini appended "THE PLAYER CHARACTER IS IN
    THIS SHOT ... ignore any instruction that demands an empty scene", and
    game_identity.reconcile deleted the montage's own "EMPTY OF PEOPLE" line
    — so the model drew a person, with no identity plate to make it the hero;
  * the safety sanitizer rewrote every "shot" to "fired at" and matched inside
    words ("screenshot" -> "screenfired at", "medieval" -> "medinegative"),
    so the montage's instructions reached the model garbled;
  * window.StartMenu was never set, so the Cutscene's "is the menu open?"
    answered no and a SAVED run's opening montage was generated at launch,
    behind the menu, for a run the player was about to replace.

Run with:
    python -m unittest test_opening_montage -v
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("GEMINI_API_KEY", "")
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import gemini_image_utils as giu  # noqa: E402


class _Captured(Exception):
    pass


def _prompt_sent(**kwargs) -> str:
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None, **_):
        seen["payload"] = json
        raise _Captured()

    with mock.patch.object(giu, "GEMINI_API_KEY", "test-key"), \
            mock.patch.object(giu.requests, "post", side_effect=fake_post), \
            mock.patch.object(giu.game_identity, "shows_character", return_value=True):
        giu.generate_with_gemini(prompt=kwargs.pop("prompt"), caption="t", hd_mode=False, **kwargs)
    parts = seen["payload"]["contents"][0]["parts"]
    return "\n".join(p.get("text", "") for p in parts)


MONTAGE_LINE = ("ALL FOUR PANELS ARE COMPLETELY EMPTY OF PEOPLE — no figure, no face. "
                "Establish the shot from scratch.")


class TheOpeningMontageIsEmpty(unittest.TestCase):

    def test_environment_only_keeps_the_empty_rule_in_third_person(self):
        sent = _prompt_sent(prompt=MONTAGE_LINE, environment_only=True)
        self.assertIn("EMPTY OF PEOPLE", sent)
        self.assertNotIn("THE PLAYER CHARACTER IS IN THIS", sent)
        self.assertIn("No person in frame", sent)

    def test_a_turn_frame_still_shows_the_hero(self):
        """Unchanged for every other caller: third person puts the character in."""
        sent = _prompt_sent(prompt="A street at night.")
        self.assertIn("THE PLAYER CHARACTER IS IN THIS", sent)

    def test_the_opening_asks_for_an_empty_picture(self):
        src = (ROOT / "cutscene.py").read_text(encoding="utf-8")
        start = src.index("grid_file = generate_with_gemini(")
        self.assertIn("environment_only=True", src[start:start + 900])


class ASlowRenderIsGivenTime(unittest.TestCase):
    """The opening montage is one pro render at 2K; 30s is the flash budget,
    and on 2026-09-23 it timed out at exactly that and the run had no first
    frame."""

    def _timeout_for(self, **kwargs):
        seen = {}

        def fake_post(url, headers=None, json=None, timeout=None, **_):
            seen["timeout"] = timeout
            raise _Captured()

        with mock.patch.object(giu, "GEMINI_API_KEY", "test-key"), \
                mock.patch.object(giu.requests, "post", side_effect=fake_post):
            try:
                giu.generate_with_gemini(prompt="A yard.", caption="t", hd_mode=False,
                                         environment_only=True, **kwargs)
            except _Captured:
                pass
        return seen.get("timeout")

    def test_a_pro_render_at_2k_gets_longer(self):
        self.assertEqual(self._timeout_for(model="gemini-3-pro-image", image_size="2K"), 75)

    def test_a_fast_render_keeps_its_short_leash(self):
        self.assertEqual(self._timeout_for(model="gemini-3.1-flash-lite-image", image_size="1K"), 30)


class TheSanitizerLeavesTheCameraAlone(unittest.TestCase):

    def test_the_cameras_shot_is_not_violence(self):
        out = giu._sanitize_for_safety("Establish the shot from scratch; an establishing shot.")
        self.assertIn("the shot from scratch", out)
        self.assertNotIn("fired at", out)

    def test_whole_words_only(self):
        out = giu._sanitize_for_safety("A screenshot of a medieval Hispanic street, shotgun shells, a trombone.")
        self.assertEqual(out, "A screenshot of a medieval Hispanic street, shotgun shells, a trombone.")

    def test_violent_uses_are_still_softened(self):
        out = giu._sanitize_for_safety("The guard was shot. They shot him. blood on the floor.")
        self.assertIn("was fired at", out)
        self.assertIn("fired at him", out)
        self.assertIn("red liquid", out)


class TheMenuHoldsASavedRunsOpening(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "static" / "js" / "standalone.js").read_text(encoding="utf-8")

    def test_start_menu_is_reachable_from_window(self):
        self.assertIn("try { window.StartMenu = StartMenu; } catch (_) {}", self.js)

    def test_a_cutscene_before_boot_is_held_not_generated(self):
        at = self.js.index("    function beforeBoot() {")
        body = self.js[self.js.index("    function onFeedItem(item) {", at):][:1200]
        self.assertLess(body.index("if (beforeBoot())"), body.index("play(opts);"))


if __name__ == "__main__":
    unittest.main()
