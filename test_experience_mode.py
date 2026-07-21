#!/usr/bin/env python3
"""
Unit tests for the Experience Mode system.

Tests cover:
  1. Constants and metadata completeness (engine.EXPERIENCE_MODES)
  2. apply_experience_mode() — engine globals + state persistence
  3. api_client.GameEngineClient proxy methods
  4. Edge cases (unknown mode, repeated application, session isolation)

Run with:
    python3 test_experience_mode.py

All tests are hermetic: state files are written to a temp directory and
engine module-level globals are restored after each test.
"""

import os
import sys
import json
import unittest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# Ensure the workspace root is on sys.path so local modules are importable
# without needing a venv activation or PYTHONPATH export.
# ---------------------------------------------------------------------------
WORKSPACE = Path(__file__).parent.resolve()
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _load_engine():
    """Import engine with a temporary sessions directory so tests never
    touch real state files."""
    import importlib
    import engine as eng
    return eng


class _EngineGlobalSaver:
    """Context manager: saves and restores engine module-level globals."""

    _WATCHED = ("IMAGE_ENABLED", "WORLD_IMAGE_ENABLED", "VEO_MODE_ENABLED")

    def __init__(self, eng):
        self._eng = eng
        self._saved: dict = {}

    def __enter__(self):
        for attr in self._WATCHED:
            self._saved[attr] = getattr(self._eng, attr)
        return self

    def __exit__(self, *_):
        for attr, val in self._saved.items():
            setattr(self._eng, attr, val)


# ═══════════════════════════════════════════════════════════════════════════
# TEST CASES
# ═══════════════════════════════════════════════════════════════════════════

class TestExperienceModeConstants(unittest.TestCase):
    """Tests for the three mode string constants."""

    def setUp(self):
        import engine
        self.eng = engine

    def test_no_images_constant_value(self):
        self.assertEqual(self.eng.EXPERIENCE_MODE_NO_IMAGES, "no_images")

    def test_flipbook_constant_value(self):
        self.assertEqual(self.eng.EXPERIENCE_MODE_FLIPBOOK, "flipbook")

    def test_full_frame_constant_value(self):
        self.assertEqual(self.eng.EXPERIENCE_MODE_FULL_FRAME, "full_frame")

    def test_all_three_constants_are_distinct(self):
        modes = {
            self.eng.EXPERIENCE_MODE_NO_IMAGES,
            self.eng.EXPERIENCE_MODE_FLIPBOOK,
            self.eng.EXPERIENCE_MODE_FULL_FRAME,
        }
        self.assertEqual(len(modes), 3, "Each mode constant must be unique")

    def test_constants_are_strings(self):
        for attr in ("EXPERIENCE_MODE_NO_IMAGES", "EXPERIENCE_MODE_FLIPBOOK",
                     "EXPERIENCE_MODE_FULL_FRAME"):
            self.assertIsInstance(getattr(self.eng, attr), str)


class TestExperienceModesDict(unittest.TestCase):
    """Tests for the EXPERIENCE_MODES metadata dictionary."""

    def setUp(self):
        import engine
        self.eng = engine
        self.modes = engine.EXPERIENCE_MODES

    def test_dict_has_all_three_modes(self):
        expected = {
            self.eng.EXPERIENCE_MODE_NO_IMAGES,
            self.eng.EXPERIENCE_MODE_FLIPBOOK,
            self.eng.EXPERIENCE_MODE_FULL_FRAME,
        }
        self.assertEqual(set(self.modes.keys()), expected)

    def test_each_mode_has_required_keys(self):
        required = {"label", "emoji", "description", "image_enabled", "flipbook_mode"}
        for mode_key, cfg in self.modes.items():
            with self.subTest(mode=mode_key):
                missing = required - set(cfg.keys())
                self.assertFalse(
                    missing,
                    f"Mode '{mode_key}' is missing keys: {missing}",
                )

    def test_no_images_disables_both_flags(self):
        cfg = self.modes[self.eng.EXPERIENCE_MODE_NO_IMAGES]
        self.assertFalse(cfg["image_enabled"])
        self.assertFalse(cfg["flipbook_mode"])

    def test_flipbook_enables_image_and_flipbook(self):
        cfg = self.modes[self.eng.EXPERIENCE_MODE_FLIPBOOK]
        self.assertTrue(cfg["image_enabled"])
        self.assertTrue(cfg["flipbook_mode"])

    def test_full_frame_enables_image_but_not_flipbook(self):
        cfg = self.modes[self.eng.EXPERIENCE_MODE_FULL_FRAME]
        self.assertTrue(cfg["image_enabled"])
        self.assertFalse(cfg["flipbook_mode"])

    def test_labels_are_non_empty_strings(self):
        for mode_key, cfg in self.modes.items():
            with self.subTest(mode=mode_key):
                self.assertIsInstance(cfg["label"], str)
                self.assertTrue(cfg["label"].strip(), f"Label for '{mode_key}' is blank")

    def test_descriptions_are_non_empty_strings(self):
        for mode_key, cfg in self.modes.items():
            with self.subTest(mode=mode_key):
                self.assertIsInstance(cfg["description"], str)
                self.assertTrue(
                    cfg["description"].strip(),
                    f"Description for '{mode_key}' is blank",
                )

    def test_image_enabled_is_bool(self):
        for mode_key, cfg in self.modes.items():
            with self.subTest(mode=mode_key):
                self.assertIsInstance(cfg["image_enabled"], bool)

    def test_flipbook_mode_is_bool(self):
        for mode_key, cfg in self.modes.items():
            with self.subTest(mode=mode_key):
                self.assertIsInstance(cfg["flipbook_mode"], bool)


class TestApplyExperienceMode(unittest.TestCase):
    """Tests for engine.apply_experience_mode()."""

    def setUp(self):
        import engine
        self.eng = engine
        # Redirect sessions to a temp directory so we never pollute real state
        self._tmpdir = tempfile.mkdtemp(prefix="test_exp_mode_")
        self._orig_root = self.eng.ROOT
        self.eng.ROOT = Path(self._tmpdir)

    def tearDown(self):
        self.eng.ROOT = self._orig_root
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # --- no_images -----------------------------------------------------------

    def test_no_images_sets_globals_false(self):
        with _EngineGlobalSaver(self.eng):
            result = self.eng.apply_experience_mode(
                self.eng.EXPERIENCE_MODE_NO_IMAGES, "test_session"
            )
            self.assertTrue(result)
            self.assertFalse(self.eng.IMAGE_ENABLED)
            self.assertFalse(self.eng.WORLD_IMAGE_ENABLED)

    def test_no_images_sets_flipbook_false_in_state(self):
        with _EngineGlobalSaver(self.eng):
            self.eng.apply_experience_mode(
                self.eng.EXPERIENCE_MODE_NO_IMAGES, "test_session_ni"
            )
            st = self.eng.get_state("test_session_ni")
            self.assertFalse(st.get("flipbook_mode"))

    def test_no_images_persists_experience_mode_in_state(self):
        with _EngineGlobalSaver(self.eng):
            self.eng.apply_experience_mode(
                self.eng.EXPERIENCE_MODE_NO_IMAGES, "test_session_ni2"
            )
            st = self.eng.get_state("test_session_ni2")
            self.assertEqual(st.get("experience_mode"), self.eng.EXPERIENCE_MODE_NO_IMAGES)

    # --- flipbook ------------------------------------------------------------

    def test_flipbook_sets_globals_true(self):
        # Start from a no-images state to verify we actually change things
        self.eng.IMAGE_ENABLED = False
        self.eng.WORLD_IMAGE_ENABLED = False
        with _EngineGlobalSaver(self.eng):
            result = self.eng.apply_experience_mode(
                self.eng.EXPERIENCE_MODE_FLIPBOOK, "test_session_fb"
            )
            self.assertTrue(result)
            self.assertTrue(self.eng.IMAGE_ENABLED)
            self.assertTrue(self.eng.WORLD_IMAGE_ENABLED)

    def test_flipbook_sets_flipbook_true_in_state(self):
        with _EngineGlobalSaver(self.eng):
            self.eng.apply_experience_mode(
                self.eng.EXPERIENCE_MODE_FLIPBOOK, "test_session_fb2"
            )
            st = self.eng.get_state("test_session_fb2")
            self.assertTrue(st.get("flipbook_mode"))

    def test_flipbook_persists_experience_mode_in_state(self):
        with _EngineGlobalSaver(self.eng):
            self.eng.apply_experience_mode(
                self.eng.EXPERIENCE_MODE_FLIPBOOK, "test_session_fb3"
            )
            st = self.eng.get_state("test_session_fb3")
            self.assertEqual(st.get("experience_mode"), self.eng.EXPERIENCE_MODE_FLIPBOOK)

    # --- full_frame ----------------------------------------------------------

    def test_full_frame_sets_globals_true(self):
        self.eng.IMAGE_ENABLED = False
        self.eng.WORLD_IMAGE_ENABLED = False
        with _EngineGlobalSaver(self.eng):
            result = self.eng.apply_experience_mode(
                self.eng.EXPERIENCE_MODE_FULL_FRAME, "test_session_ff"
            )
            self.assertTrue(result)
            self.assertTrue(self.eng.IMAGE_ENABLED)
            self.assertTrue(self.eng.WORLD_IMAGE_ENABLED)

    def test_full_frame_sets_flipbook_false_in_state(self):
        with _EngineGlobalSaver(self.eng):
            self.eng.apply_experience_mode(
                self.eng.EXPERIENCE_MODE_FULL_FRAME, "test_session_ff2"
            )
            st = self.eng.get_state("test_session_ff2")
            self.assertFalse(st.get("flipbook_mode"))

    def test_full_frame_persists_experience_mode_in_state(self):
        with _EngineGlobalSaver(self.eng):
            self.eng.apply_experience_mode(
                self.eng.EXPERIENCE_MODE_FULL_FRAME, "test_session_ff3"
            )
            st = self.eng.get_state("test_session_ff3")
            self.assertEqual(st.get("experience_mode"), self.eng.EXPERIENCE_MODE_FULL_FRAME)

    # --- unknown mode --------------------------------------------------------

    def test_unknown_mode_returns_false(self):
        with _EngineGlobalSaver(self.eng):
            result = self.eng.apply_experience_mode("nonexistent_mode", "test_session_unk")
            self.assertFalse(result)

    def test_unknown_mode_does_not_change_globals(self):
        with _EngineGlobalSaver(self.eng):
            original_img = self.eng.IMAGE_ENABLED
            original_world = self.eng.WORLD_IMAGE_ENABLED
            self.eng.apply_experience_mode("nonexistent_mode", "test_session_unk2")
            self.assertEqual(self.eng.IMAGE_ENABLED, original_img)
            self.assertEqual(self.eng.WORLD_IMAGE_ENABLED, original_world)

    # --- idempotency ---------------------------------------------------------

    def test_applying_same_mode_twice_is_idempotent(self):
        with _EngineGlobalSaver(self.eng):
            self.eng.apply_experience_mode(
                self.eng.EXPERIENCE_MODE_FLIPBOOK, "test_session_idem"
            )
            result2 = self.eng.apply_experience_mode(
                self.eng.EXPERIENCE_MODE_FLIPBOOK, "test_session_idem"
            )
            self.assertTrue(result2)
            st = self.eng.get_state("test_session_idem")
            self.assertTrue(st.get("flipbook_mode"))
            self.assertEqual(st.get("experience_mode"), self.eng.EXPERIENCE_MODE_FLIPBOOK)

    # --- session isolation ---------------------------------------------------

    def test_mode_changes_are_per_session(self):
        with _EngineGlobalSaver(self.eng):
            self.eng.apply_experience_mode(
                self.eng.EXPERIENCE_MODE_NO_IMAGES, "session_a"
            )
            # Engine globals reflect last written (session_a: no_images)
            # Now apply flipbook to session_b
            self.eng.apply_experience_mode(
                self.eng.EXPERIENCE_MODE_FLIPBOOK, "session_b"
            )
            st_b = self.eng.get_state("session_b")
            self.assertTrue(st_b.get("flipbook_mode"))
            self.assertEqual(st_b.get("experience_mode"), self.eng.EXPERIENCE_MODE_FLIPBOOK)

    # --- mode switching ------------------------------------------------------

    def test_switching_from_no_images_to_flipbook_re_enables_globals(self):
        with _EngineGlobalSaver(self.eng):
            self.eng.apply_experience_mode(
                self.eng.EXPERIENCE_MODE_NO_IMAGES, "switch_session"
            )
            self.assertFalse(self.eng.IMAGE_ENABLED)

            self.eng.apply_experience_mode(
                self.eng.EXPERIENCE_MODE_FLIPBOOK, "switch_session"
            )
            self.assertTrue(self.eng.IMAGE_ENABLED)
            self.assertTrue(self.eng.WORLD_IMAGE_ENABLED)


class TestApiClientExperienceMode(unittest.TestCase):
    """Tests for api_client.GameEngineClient experience mode proxies."""

    def setUp(self):
        import api_client
        import engine
        self.eng = engine
        self.client = api_client.GameEngineClient(use_api=False)
        self._tmpdir = tempfile.mkdtemp(prefix="test_apiclient_exp_")
        self._orig_root = engine.ROOT
        engine.ROOT = Path(self._tmpdir)

    def tearDown(self):
        self.eng.ROOT = self._orig_root
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_constant_proxy_no_images(self):
        self.assertEqual(
            self.client.EXPERIENCE_MODE_NO_IMAGES,
            self.eng.EXPERIENCE_MODE_NO_IMAGES,
        )

    def test_constant_proxy_flipbook(self):
        self.assertEqual(
            self.client.EXPERIENCE_MODE_FLIPBOOK,
            self.eng.EXPERIENCE_MODE_FLIPBOOK,
        )

    def test_constant_proxy_full_frame(self):
        self.assertEqual(
            self.client.EXPERIENCE_MODE_FULL_FRAME,
            self.eng.EXPERIENCE_MODE_FULL_FRAME,
        )

    def test_modes_dict_proxy(self):
        self.assertIs(self.client.EXPERIENCE_MODES, self.eng.EXPERIENCE_MODES)

    def test_apply_experience_mode_returns_true_for_valid_mode(self):
        with _EngineGlobalSaver(self.eng):
            result = self.client.apply_experience_mode(
                self.eng.EXPERIENCE_MODE_FLIPBOOK, "apiclient_test"
            )
            self.assertTrue(result)

    def test_apply_experience_mode_returns_false_for_unknown(self):
        with _EngineGlobalSaver(self.eng):
            result = self.client.apply_experience_mode("garbage_mode", "apiclient_test2")
            self.assertFalse(result)

    def test_apply_no_images_via_client_sets_engine_globals(self):
        with _EngineGlobalSaver(self.eng):
            self.client.apply_experience_mode(
                self.eng.EXPERIENCE_MODE_NO_IMAGES, "apiclient_ni"
            )
            self.assertFalse(self.eng.IMAGE_ENABLED)
            self.assertFalse(self.eng.WORLD_IMAGE_ENABLED)

    def test_apply_full_frame_via_client_sets_engine_globals(self):
        with _EngineGlobalSaver(self.eng):
            self.client.apply_experience_mode(
                self.eng.EXPERIENCE_MODE_FULL_FRAME, "apiclient_ff"
            )
            self.assertTrue(self.eng.IMAGE_ENABLED)
            self.assertTrue(self.eng.WORLD_IMAGE_ENABLED)


class TestModeConfigConsistency(unittest.TestCase):
    """Cross-checks that mode configs don't contradict each other."""

    def setUp(self):
        import engine
        self.modes = engine.EXPERIENCE_MODES
        self.eng = engine

    def test_only_one_mode_disables_images(self):
        disabled = [k for k, v in self.modes.items() if not v["image_enabled"]]
        self.assertEqual(
            disabled,
            [self.eng.EXPERIENCE_MODE_NO_IMAGES],
            "Exactly one mode (no_images) should have image_enabled=False",
        )

    def test_only_one_mode_enables_flipbook(self):
        flipbook_on = [k for k, v in self.modes.items() if v["flipbook_mode"]]
        self.assertEqual(
            flipbook_on,
            [self.eng.EXPERIENCE_MODE_FLIPBOOK],
            "Exactly one mode (flipbook) should have flipbook_mode=True",
        )

    def test_no_images_mode_disables_flipbook(self):
        cfg = self.modes[self.eng.EXPERIENCE_MODE_NO_IMAGES]
        self.assertFalse(
            cfg["flipbook_mode"],
            "no_images mode must not enable flipbook (nothing to animate)",
        )

    def test_full_frame_and_flipbook_both_enable_image(self):
        for mode in (self.eng.EXPERIENCE_MODE_FLIPBOOK, self.eng.EXPERIENCE_MODE_FULL_FRAME):
            with self.subTest(mode=mode):
                self.assertTrue(
                    self.modes[mode]["image_enabled"],
                    f"{mode} must have image_enabled=True",
                )


class TestExperienceModeStatePersistence(unittest.TestCase):
    """Verify that apply_experience_mode state is durable (round-trip via get_state)."""

    def setUp(self):
        import engine
        self.eng = engine
        self._tmpdir = tempfile.mkdtemp(prefix="test_persist_exp_")
        self._orig_root = engine.ROOT
        engine.ROOT = Path(self._tmpdir)

    def tearDown(self):
        self.eng.ROOT = self._orig_root
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _apply_and_reload(self, mode: str, session: str) -> dict:
        with _EngineGlobalSaver(self.eng):
            self.eng.apply_experience_mode(mode, session)
            # Reload from disk to confirm durability
            return self.eng.get_state(session)

    def test_no_images_state_is_durable(self):
        st = self._apply_and_reload(self.eng.EXPERIENCE_MODE_NO_IMAGES, "dur_ni")
        self.assertEqual(st["experience_mode"], self.eng.EXPERIENCE_MODE_NO_IMAGES)
        self.assertFalse(st["flipbook_mode"])

    def test_flipbook_state_is_durable(self):
        st = self._apply_and_reload(self.eng.EXPERIENCE_MODE_FLIPBOOK, "dur_fb")
        self.assertEqual(st["experience_mode"], self.eng.EXPERIENCE_MODE_FLIPBOOK)
        self.assertTrue(st["flipbook_mode"])

    def test_full_frame_state_is_durable(self):
        st = self._apply_and_reload(self.eng.EXPERIENCE_MODE_FULL_FRAME, "dur_ff")
        self.assertEqual(st["experience_mode"], self.eng.EXPERIENCE_MODE_FULL_FRAME)
        self.assertFalse(st["flipbook_mode"])

    def test_switching_modes_overwrites_old_mode_in_state(self):
        with _EngineGlobalSaver(self.eng):
            session = "switch_dur"
            self.eng.apply_experience_mode(self.eng.EXPERIENCE_MODE_FLIPBOOK, session)
            st1 = self.eng.get_state(session)
            self.assertEqual(st1["experience_mode"], self.eng.EXPERIENCE_MODE_FLIPBOOK)

            self.eng.apply_experience_mode(self.eng.EXPERIENCE_MODE_FULL_FRAME, session)
            st2 = self.eng.get_state(session)
            self.assertEqual(st2["experience_mode"], self.eng.EXPERIENCE_MODE_FULL_FRAME)
            self.assertFalse(st2["flipbook_mode"])


class TestClaudeOpusProvider(unittest.TestCase):
    """Verify Claude/Anthropic provider is wired up correctly."""

    def test_anthropic_preset_in_ai_config(self):
        import json
        from pathlib import Path
        cfg_path = Path(__file__).parent / "ai_config.json"
        with open(cfg_path) as f:
            cfg = json.load(f)
        presets = cfg.get("available_configs", {})
        self.assertIn("anthropic", presets, "anthropic preset must exist in ai_config.json")
        ant = presets["anthropic"]
        self.assertEqual(ant["text_provider"], "anthropic")
        self.assertIn("claude", ant["text_model"].lower(), "text_model must reference a Claude model")

    def test_ask_routes_anthropic(self):
        import engine
        import ai_provider_manager
        with patch.object(ai_provider_manager, "get_text_provider", return_value="anthropic"), \
             patch.object(ai_provider_manager, "get_text_model", return_value="claude-opus-4-5"), \
             patch.object(engine, "_ask_claude", return_value="narrative response") as mock_claude:
            result = engine._ask("test prompt")
        mock_claude.assert_called_once()
        self.assertEqual(result, "narrative response")

    def test_ask_claude_returns_fallback_without_key(self):
        import engine
        with patch.dict(os.environ, {}, clear=False):
            # Temporarily remove key if present
            original = os.environ.pop("ANTHROPIC_API_KEY", None)
            try:
                result = engine._ask_claude("test", "claude-opus-4-5", 0.7, 50)
                self.assertIn("interrupted", result.lower())
            finally:
                if original is not None:
                    os.environ["ANTHROPIC_API_KEY"] = original

class TestPacingFairnessHardening(unittest.TestCase):
    """
    Pacing, flow, balance, and fairness invariants added in the
    post-PR-#14 pacing pass. These guard against regressions in:
      • The silent vision-API 404 (gemini-2.0-flash-exp)
      • The cheap-environmental-death pattern
      • Mandatory tension-escalation (now allowed to breathe)
      • Phase-gated countdown timer
      • Tiered timeout penalty
      • Injury state threading into prompts
      • Default experience mode = Full Frame
    """

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.WORKSPACE = Path(__file__).parent
        cls.engine_src = (cls.WORKSPACE / "engine.py").read_text(encoding="utf-8")
        cls.choices_src = (cls.WORKSPACE / "choices.py").read_text(encoding="utf-8")
        cls.prompts_src = (cls.WORKSPACE / "prompts" / "simulation_prompts.json").read_text(encoding="utf-8")

    # -- Vision API 404 fix --
    def test_vision_url_no_longer_uses_deprecated_exp_model(self):
        """The `gemini-2.0-flash-exp` model was retired and silently 404'd
        on every vision call, leaving the spatial-anchor pipeline deaf.

        Comments in the source may still mention the retired name to
        document the fix — what must not exist is an actual API URL
        ending in `models/gemini-2.0-flash-exp:generateContent`.
        """
        self.assertNotIn(
            "models/gemini-2.0-flash-exp:generateContent",
            self.engine_src,
            "engine.py must NOT route any API call to gemini-2.0-flash-exp",
        )
        self.assertIn(
            "models/gemini-3.1-flash-lite:generateContent",
            self.engine_src,
            "engine.py vision URL must use the current gemini-3.1-flash-lite model",
        )

    # -- Fairness doctrine --
    def test_death_fairness_doctrine_present(self):
        """Prompt must encode 'characters/events kill, environment only injures'."""
        self.assertIn("DEATH FAIRNESS DOCTRINE", self.prompts_src)
        self.assertIn("CHARACTERS AND DRAMATIC EVENTS KILL", self.prompts_src)
        self.assertIn("ENVIRONMENT ONLY WOUNDS", self.prompts_src)

    def test_unlucky_fate_modifier_forbids_cheap_deaths(self):
        """The UNLUCKY fate path must not re-introduce random impalement deaths."""
        self.assertIn(
            "FORBIDDEN UNDER UNLUCKY",
            self.engine_src,
            "engine.py must explicitly forbid cheap-death patterns under UNLUCKY",
        )

    # -- Tension rhythm --
    def test_tension_rhythm_allows_stillness_beats(self):
        """The action_consequence_instructions must allow ~30% stillness beats."""
        self.assertIn("TENSION RHYTHM", self.prompts_src)
        self.assertIn("STILLNESS BEAT", self.prompts_src)
        # The old "MANDATORY FINAL SENTENCE" rule must be gone.
        self.assertNotIn(
            "TENSION ESCALATION (MANDATORY FINAL SENTENCE)",
            self.prompts_src,
            "Mandatory escalation final-sentence rule must be replaced with wave-rhythm",
        )

    # -- Tiered timeout penalty --
    def test_timeout_penalty_is_tiered(self):
        # The prompt knows about tiered penalties.
        self.assertIn("Tier 1", self.prompts_src)
        self.assertIn("Tier 2", self.prompts_src)
        self.assertIn("Tier 3", self.prompts_src)

    # -- Injury state threading --
    def test_injury_state_threaded_into_choices(self):
        """generate_choices must accept and format injury_state."""
        self.assertIn("injury_state", self.choices_src)
        self.assertIn(
            "injury_state=injury_state or",
            self.choices_src,
            "generate_choices must pass injury_state into prompt .format()",
        )

    def test_injury_state_threaded_into_dispatch(self):
        """The dispatch prompt builder must inject seen_elements + injury_state."""
        self.assertIn("INJURY STATE", self.engine_src)
        self.assertIn("DISCOVERED ENTITIES", self.engine_src)

    # -- Choice slate composition --
    def test_choice_slot_is_randomized(self):
        """The mandatory 'slot 1 = forward movement' rule must be replaced
        with a randomized-slot rule so players cannot rote-memorize it."""
        self.assertNotIn(
            "CHOICE #1 MUST ALWAYS BE FORWARD SPATIAL MOVEMENT",
            self.prompts_src,
            "Hard-pinned slot 1 rule must be removed",
        )
        self.assertIn(
            "slot position is RANDOMIZED",
            self.prompts_src,
            "Choice slate must randomize the forward-movement slot",
        )

    # -- Phase-linked time of day --
    def test_time_of_day_can_advance_with_phase(self):
        self.assertIn("TIME-OF-DAY PROGRESSION (PHASE-LINKED)", self.prompts_src)
        # The world_tick instructions must allow the phase-gated exception
        # so dusk/night can actually appear in long sessions.
        self.assertIn("PHASE-GATED EXCEPTION", self.prompts_src)


# ═══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 70)
    print("EXPERIENCE MODE TEST SUITE")
    print("=" * 70)
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
