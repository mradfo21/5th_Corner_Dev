"""Regression tests for the with-images intro flow hardening.

These tests lock in the fix for the "Generating choices failed on the initial
turn" production regression. They verify source-level invariants in `bot.py`
so the with-images PlayButton path can never again leave the channel silent
when image generation or choice generation hangs or crashes.

Why source-level checks instead of end-to-end discord.py mocks?  The
with-images intro mixes Discord views, asyncio tasks, executor threads, Gemini
HTTP calls, and PIL image manipulation.  A faithful e2e harness would itself
become a regression risk.  These checks instead pin the small set of
invariants the production bug analysis identified:

  1. There is a top-level try/except guard around the with-images intro flow.
  2. The Phase 1 image task is awaited with a hard timeout (`asyncio.wait_for`).
  3. The Phase 2 choices task is awaited with a hard timeout.
  4. When Phase 1 returns nothing usable, a synthesised fallback dict is built
     so that Phase 2 still has a prologue / vision_dispatch to work with.
  5. The contextual fallback choice builder is still invoked when Phase 2 is
     empty or times out.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path


BOT_SRC = Path(__file__).resolve().parent / "bot.py"
BOT_TEXT = BOT_SRC.read_text(encoding="utf-8")


class TestWithImagesIntroHardening(unittest.TestCase):
    """Static invariants on the with-images intro path."""

    def test_top_level_guard_exists(self):
        """A wrapping try/except must surround the with-images body."""
        self.assertIn(
            "TOP-LEVEL WITH-IMAGES GUARD",
            BOT_TEXT,
            "Top-level guard banner removed — with-images intro can crash silently again.",
        )
        self.assertIn(
            "_run_with_images_intro",
            BOT_TEXT,
            "Inner with-images intro method was renamed or removed.",
        )
        self.assertIn(
            "[PLAY-WITHIMAGES ERROR] Unhandled exception",
            BOT_TEXT,
            "Unhandled-exception log marker removed — production debug signal lost.",
        )

    def test_phase1_image_task_has_hard_timeout(self):
        """Phase 1 (image gen) must be awaited via asyncio.wait_for, not raw await."""
        self.assertRegex(
            BOT_TEXT,
            r"asyncio\.wait_for\(image_task,\s*timeout=\s*\d+",
            "image_task is no longer wrapped in asyncio.wait_for — a hung image gen "
            "will stall the entire intro and the player will never see choices.",
        )

    def test_phase2_choices_task_has_hard_timeout(self):
        """Phase 2 (choice gen) must be awaited via asyncio.wait_for."""
        self.assertRegex(
            BOT_TEXT,
            r"asyncio\.wait_for\(choices_task,\s*timeout=\s*\d+",
            "choices_task is no longer wrapped in asyncio.wait_for — Phase 2 can "
            "stall forever and the player is stuck on '⚙️ Generating choices...'.",
        )

    def test_phase1_synthesises_fallback_when_image_fails(self):
        """Even if Phase 1 returns no image, intro_phase1 must be a dict.

        This is the safety net that lets Phase 2 still produce intro choices
        when Gemini's image API is hung, rate-limited, or returns None.
        """
        # The synthesised dict must include prologue/vision_dispatch/world_prompt
        # so generate_intro_choices_deferred (and the contextual fallback) have
        # text to work with.
        self.assertIn(
            '"prologue": "You survey the Horizon facility from a distant ridge."',
            BOT_TEXT,
            "Synthesised Phase 1 fallback dict was removed — Phase 2 may receive a "
            "None payload and crash before sending any choices.",
        )
        self.assertIn(
            "isinstance(intro_phase1, dict)",
            BOT_TEXT,
            "Type guard on intro_phase1 was removed — None or unexpected payloads "
            "can bubble into intro_phase1.get() calls and crash silently.",
        )

    def test_contextual_fallback_still_used_for_phase2(self):
        """Empty / timed-out Phase 2 must still route through _build_intro_fallback_choices."""
        self.assertIn(
            "_build_intro_fallback_choices",
            BOT_TEXT,
            "Contextual fallback builder removed — empty Phase 2 will surface as "
            "no choices at all, which is the production regression we fixed.",
        )
        self.assertIn(
            "Choice generator hiccup",
            BOT_TEXT,
            "Soft user-facing notice removed — failures will become invisible again.",
        )

    def test_no_images_path_guard_still_present(self):
        """We must not have regressed the existing no-images guard."""
        self.assertIn(
            "TOP-LEVEL NO-IMAGES GUARD",
            BOT_TEXT,
            "The earlier no-images guard banner was removed.",
        )


class TestWithImagesIntroOrdering(unittest.TestCase):
    """Invariants on the order of operations in the with-images intro path.

    The wrap-in-try guard only works if it actually surrounds the body.  These
    tests assert that the guard's `await self._run_with_images_intro(...)` line
    comes BEFORE the `_run_with_images_intro` definition (i.e. callback delegates
    to the inner method), and that both `image_task` and `choices_task` timeouts
    appear inside the inner method.
    """

    def setUp(self):
        self.text = BOT_TEXT

    def _index_of(self, needle: str) -> int:
        i = self.text.find(needle)
        self.assertNotEqual(
            i, -1, f"Required marker not found in bot.py: {needle!r}"
        )
        return i

    def test_callback_delegates_to_inner_method_before_inner_definition(self):
        """callback().await self._run_with_images_intro() must come before the
        `async def _run_with_images_intro` definition in source order."""
        call_idx = self._index_of("await self._run_with_images_intro(")
        def_idx = self._index_of("async def _run_with_images_intro(")
        self.assertLess(
            call_idx,
            def_idx,
            "callback no longer delegates to _run_with_images_intro before its "
            "definition — the top-level guard cannot fire.",
        )

    def test_image_timeout_appears_inside_inner_method(self):
        def_idx = self._index_of("async def _run_with_images_intro(")
        body = self.text[def_idx : def_idx + 30000]
        self.assertRegex(
            body,
            r"asyncio\.wait_for\(image_task,\s*timeout=",
            "image_task timeout no longer lives inside _run_with_images_intro.",
        )

    def test_choices_timeout_appears_inside_inner_method(self):
        def_idx = self._index_of("async def _run_with_images_intro(")
        body = self.text[def_idx : def_idx + 30000]
        self.assertRegex(
            body,
            r"asyncio\.wait_for\(choices_task,\s*timeout=",
            "choices_task timeout no longer lives inside _run_with_images_intro.",
        )


class TestPhase1FallbackIsActuallyUsable(unittest.TestCase):
    """The synthesised Phase 1 fallback dict must contain everything the
    downstream choice generator and embed-sender expect: dispatch / prologue /
    vision_dispatch / dispatch_image / world_prompt."""

    def test_synthesised_fallback_has_required_keys(self):
        # Pull the literal dict text out of bot.py and require each key.
        m = re.search(
            r"intro_phase1\s*=\s*\{[^}]*\}",
            BOT_TEXT,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(
            m,
            "Could not locate the synthesised intro_phase1 fallback dict in bot.py.",
        )
        block = m.group(0)
        for key in ("dispatch", "prologue", "vision_dispatch", "dispatch_image", "world_prompt"):
            self.assertIn(
                f'"{key}"',
                block,
                f"Synthesised intro_phase1 fallback dict is missing required key "
                f"{key!r}.  Phase 2 / embed code will crash on .get().",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
