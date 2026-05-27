#!/usr/bin/env python3
"""
Simulation test for the Text Only (no-images) game flow.

This test simulates the complete bot interaction loop — PlayButton → choices → custom action
— without needing a live Discord connection, using async mocks and the real engine.

Run with:
    python3 test_no_images_simulation.py
"""

import os
import sys
import json
import asyncio
import tempfile
import shutil
import unittest
import traceback
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

WORKSPACE = Path(__file__).parent.resolve()
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

# Disable Discord and network so tests are hermetic
os.environ.setdefault("DISCORD_ENABLED", "0")
os.environ.setdefault("RESUME_MODE", "1")
# Prevent real API calls — engine._ask will return a static string
os.environ.setdefault("GEMINI_API_KEY", "test_key_sim")
os.environ.setdefault("OPENAI_API_KEY", "")

print("=" * 70)
print("NO-IMAGES SIMULATION TEST SUITE")
print("=" * 70)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_interaction(channel_id: int = 9999):
    """Build a minimal fake discord.Interaction."""
    inter = MagicMock()
    inter.channel_id = channel_id
    inter.user = MagicMock()
    inter.user.id = 495368711679770641  # owner
    inter.response = MagicMock()
    inter.response.is_done = MagicMock(return_value=False)
    inter.response.defer = AsyncMock()
    inter.response.send_message = AsyncMock()
    inter.message = MagicMock()
    inter.message.delete = AsyncMock()
    inter.channel = MagicMock()
    inter.channel.id = channel_id
    inter.channel.send = AsyncMock(return_value=_make_message())
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    return inter


def _make_message():
    msg = MagicMock()
    msg.delete = AsyncMock()
    msg.edit = AsyncMock()
    return msg


def _stub_engine_asks(eng):
    """Replace all LLM/image calls with deterministic stubs."""
    eng.IMAGE_ENABLED = False
    eng.WORLD_IMAGE_ENABLED = False

    def _fake_ask(prompt, **kwargs):
        return "🔴 The facility hums ominously."

    def _fake_gen_image(*args, **kwargs):
        return (None, "", None)

    eng._ask = _fake_ask
    # Patch _gen_image so no actual image API is called
    import unittest.mock as _mock
    _mock.patch.object(eng, '_gen_image', return_value=(None, "", None)).start()


# ---------------------------------------------------------------------------
# Test 1: engine.generate_intro_turn in no-images mode
# ---------------------------------------------------------------------------

class TestEngineIntroTurnNoImages(unittest.TestCase):
    """generate_intro_turn must return a valid dict when IMAGE_ENABLED=False."""

    def setUp(self):
        import engine
        self.eng = engine
        self._tmp = tempfile.mkdtemp(prefix="sim_intro_")
        self._orig_root = engine.ROOT
        engine.ROOT = Path(self._tmp)
        engine.IMAGE_ENABLED = False
        engine.WORLD_IMAGE_ENABLED = False

    def tearDown(self):
        self.eng.ROOT = self._orig_root
        self.eng.IMAGE_ENABLED = True
        self.eng.WORLD_IMAGE_ENABLED = True
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _mock_choices(self, session):
        """Stub generate_choices so no real API call is made."""
        import choices as _choices
        self._choices_patch = patch.object(
            _choices, 'generate_choices',
            return_value=["Sprint to the gate", "Hide behind the boulder", "Signal for help"]
        )
        self._choices_patch.start()

    def tearDown(self):
        try:
            self._choices_patch.stop()
        except Exception:
            pass
        super().tearDown()

    def test_generate_intro_turn_returns_dict(self):
        session = "sim_intro_test"
        self._mock_choices(session)
        import engine
        engine.IMAGE_ENABLED = False
        engine.ROOT = Path(self._tmp)

        result = engine.generate_intro_turn(session)
        self.assertIsInstance(result, dict, "generate_intro_turn must return a dict")

    def test_generate_intro_turn_has_required_keys(self):
        session = "sim_intro_keys"
        self._mock_choices(session)
        import engine
        engine.IMAGE_ENABLED = False
        engine.ROOT = Path(self._tmp)

        result = engine.generate_intro_turn(session)
        for key in ("dispatch", "vision_dispatch", "choices"):
            self.assertIn(key, result, f"Result must have '{key}' key")

    def test_generate_intro_turn_dispatch_is_nonempty(self):
        session = "sim_intro_dispatch"
        self._mock_choices(session)
        import engine
        engine.IMAGE_ENABLED = False
        engine.ROOT = Path(self._tmp)

        result = engine.generate_intro_turn(session)
        self.assertTrue(result.get("dispatch", ""), "dispatch must not be empty")

    def test_generate_intro_turn_no_image_returned(self):
        session = "sim_intro_noimg"
        self._mock_choices(session)
        import engine
        engine.IMAGE_ENABLED = False
        engine.ROOT = Path(self._tmp)

        result = engine.generate_intro_turn(session)
        self.assertIsNone(
            result.get("dispatch_image"),
            "dispatch_image must be None in no-images mode"
        )

    def test_generate_intro_turn_choices_are_list(self):
        session = "sim_intro_choices"
        self._mock_choices(session)
        import engine
        engine.IMAGE_ENABLED = False
        engine.ROOT = Path(self._tmp)

        result = engine.generate_intro_turn(session)
        self.assertIsInstance(result.get("choices"), list)
        self.assertGreater(len(result["choices"]), 0)


# ---------------------------------------------------------------------------
# Test 2: apply_experience_mode then generate_intro_turn
# ---------------------------------------------------------------------------

class TestApplyModeBeforeIntro(unittest.TestCase):
    def setUp(self):
        import engine
        self.eng = engine
        self._tmp = tempfile.mkdtemp(prefix="sim_mode_")
        self._orig_root = engine.ROOT
        engine.ROOT = Path(self._tmp)
        self._choices_patch = patch(
            'choices.generate_choices',
            return_value=["Run", "Hide", "Fight"]
        )
        self._choices_patch.start()

    def tearDown(self):
        self._choices_patch.stop()
        self.eng.ROOT = self._orig_root
        self.eng.IMAGE_ENABLED = True
        self.eng.WORLD_IMAGE_ENABLED = True
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_apply_no_images_then_intro(self):
        import engine
        session = "mode_intro_sim"
        ok = engine.apply_experience_mode(engine.EXPERIENCE_MODE_NO_IMAGES, session)
        self.assertTrue(ok)
        self.assertFalse(engine.IMAGE_ENABLED)
        result = engine.generate_intro_turn(session)
        self.assertIsNone(result.get("dispatch_image"))
        self.assertTrue(result.get("dispatch"))


# ---------------------------------------------------------------------------
# Test 3: Bot no-images PlayButton guard catches exceptions
# ---------------------------------------------------------------------------

class TestPlayButtonNoImagesGuard(unittest.TestCase):
    """Verify the try/except guard in the no-images path catches engine errors."""

    def test_guard_present_in_source(self):
        src = (WORKSPACE / "bot.py").read_text(encoding="utf-8")
        self.assertIn("TOP-LEVEL NO-IMAGES GUARD", src)

    def test_guard_catches_exception_path(self):
        """The guard must produce an error embed rather than propagating."""
        src = (WORKSPACE / "bot.py").read_text(encoding="utf-8")
        guard_start = src.find("TOP-LEVEL NO-IMAGES GUARD")
        guard_end = src.find("return  # No-images path complete", guard_start)
        section = src[guard_start:guard_end]
        self.assertIn("except Exception as _ni_err", section)
        self.assertIn("Start Error", section)


# ---------------------------------------------------------------------------
# Test 4: ChoiceButton text-only behaviours in source
# ---------------------------------------------------------------------------

class TestChoiceButtonTextOnlySource(unittest.TestCase):
    """Verify source-level correctness of ChoiceButton text-only fixes."""

    def setUp(self):
        self.src = (WORKSPACE / "bot.py").read_text(encoding="utf-8")

    def test_recording_sequence_uses_text_only_branch(self):
        self.assertIn("GENERATING NARRATIVE", self.src,
                      "ChoiceButton must show 'GENERATING NARRATIVE' in text-only mode")
        self.assertIn("_text_only", self.src,
                      "_text_only flag must be detected in ChoiceButton")

    def test_flipbook_wait_gated_on_image_enabled(self):
        # Both ChoiceButton and CustomActionModal must gate on IMAGE_ENABLED
        idx1 = self.src.find("BOT FLIPBOOK WAIT] Waiting for flipbook")
        ctx1 = self.src[max(0, idx1 - 80):idx1 + 10]
        self.assertIn("engine.IMAGE_ENABLED", ctx1,
                      "Flipbook wait in ChoiceButton must check engine.IMAGE_ENABLED")

    def test_what_you_see_shown_in_text_only(self):
        self.assertIn("What You See", self.src,
                      "bot.py must show 'What You See' embed on choice turns")
        self.assertIn("👁️ What You See", self.src,
                      "Choice turns must show 👁️ What You See in text-only mode")

    def test_countdown_runs_in_text_only(self):
        # Countdown must start even when has_visual=False
        self.assertIn("_text_only", self.src)
        # The condition must include text-only as an OR branch
        idx = self.src.find("has_visual or _text_only")
        self.assertGreater(idx, -1,
                           "Countdown must start when has_visual OR _text_only")

    def test_choiceview_has_owner_id_on_subsequent_turns(self):
        # The main ChoiceView creation in ChoiceButton must pass owner_id
        idx = self.src.find("view = ChoiceView(disp[\"choices\"], owner_id=OWNER_ID)")
        self.assertGreater(idx, -1,
                           "ChoiceButton must pass owner_id=OWNER_ID to ChoiceView")

    def test_flipbook_toggle_blocked_in_text_only(self):
        self.assertIn(
            "Flipbook is not available in Text Only mode",
            self.src,
            "FlipbookToggleButton must block enabling in text-only mode",
        )

    def test_custom_action_modal_has_same_text_only_fixes(self):
        self.assertIn("_text_only_c", self.src,
                      "CustomActionModal must also detect text-only mode")
        self.assertIn("GENERATING NARRATIVE", self.src)


# ---------------------------------------------------------------------------
# Test 5: Async simulation of a full turn in text-only mode
# ---------------------------------------------------------------------------

class TestFullTurnSimulation(unittest.IsolatedAsyncioTestCase):
    """Run generate_intro_turn + advance_turn_image_fast back-to-back."""

    async def asyncSetUp(self):
        import engine
        self.eng = engine
        self._tmp = tempfile.mkdtemp(prefix="sim_full_")
        self._orig_root = engine.ROOT
        engine.ROOT = Path(self._tmp)
        engine.IMAGE_ENABLED = False
        engine.WORLD_IMAGE_ENABLED = False
        self._p1 = patch('choices.generate_choices', return_value=["Advance", "Retreat", "Hide"])
        self._p1.start()
        self._p2 = patch.object(engine, '_gen_image', return_value=(None, "", None))
        self._p2.start()
        # Stub _ask so no network calls
        self._p3 = patch.object(engine, '_ask', return_value="You move forward. The tension rises.")
        self._p3.start()
        # Stub evolve_world_state
        self._p4 = patch('evolve_prompt_file.evolve_world_state', return_value=None)
        self._p4.start()

    async def asyncTearDown(self):
        for p in (self._p1, self._p2, self._p3, self._p4):
            try: p.stop()
            except: pass
        self.eng.ROOT = self._orig_root
        self.eng.IMAGE_ENABLED = True
        self.eng.WORLD_IMAGE_ENABLED = True
        shutil.rmtree(self._tmp, ignore_errors=True)

    async def test_intro_turn_async(self):
        import engine
        loop = asyncio.get_running_loop()
        engine.apply_experience_mode(engine.EXPERIENCE_MODE_NO_IMAGES, "sim_async")
        result = await loop.run_in_executor(None, lambda: engine.generate_intro_turn("sim_async"))
        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("dispatch"))
        self.assertIsNone(result.get("dispatch_image"))

    async def test_advance_turn_no_images(self):
        import engine
        engine.apply_experience_mode(engine.EXPERIENCE_MODE_NO_IMAGES, "sim_advance")
        # Need intro state first
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: engine.generate_intro_turn("sim_advance")
        )
        result = await asyncio.get_running_loop().run_in_executor(
            None, lambda: engine.advance_turn_image_fast("Advance", "NORMAL", False, "sim_advance")
        )
        self.assertIsInstance(result, dict)
        self.assertIsNone(result.get("consequence_image"),
                          "consequence_image must be None in text-only mode")
        self.assertIn("dispatch", result)

    async def test_advance_turn_dispatch_not_empty(self):
        import engine
        engine.apply_experience_mode(engine.EXPERIENCE_MODE_NO_IMAGES, "sim_dispatch_empty")
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: engine.generate_intro_turn("sim_dispatch_empty")
        )
        result = await asyncio.get_running_loop().run_in_executor(
            None, lambda: engine.advance_turn_image_fast("Hide", "NORMAL", False, "sim_dispatch_empty")
        )
        # dispatch should be a non-error string
        dispatch = result.get("dispatch", "")
        self.assertTrue(
            dispatch and dispatch != "Error",
            f"dispatch should be a real narrative string, got: {dispatch!r}"
        )


# ---------------------------------------------------------------------------
# Test 6: FlipbookToggleButton guard
# ---------------------------------------------------------------------------

class TestFlipbookToggleGuard(unittest.TestCase):
    def test_guard_blocks_when_images_disabled(self):
        src = (WORKSPACE / "bot.py").read_text(encoding="utf-8")
        # Find the FlipbookToggleButton callback
        start = src.find("class FlipbookToggleButton")
        end = src.find("class MapButton")
        section = src[start:end]
        self.assertIn("engine.IMAGE_ENABLED", section)
        self.assertIn("Flipbook is not available in Text Only mode", section)

    def test_init_uses_image_enabled(self):
        src = (WORKSPACE / "bot.py").read_text(encoding="utf-8")
        start = src.find("class FlipbookToggleButton")
        init_end = src.find("async def callback", start)
        init_section = src[start:init_end]
        self.assertIn("engine.IMAGE_ENABLED", init_section,
                      "FlipbookToggleButton.__init__ must use engine.IMAGE_ENABLED")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
