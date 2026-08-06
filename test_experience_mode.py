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
import re
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
      • The death-fairness doctrine on damage turns
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

    # -- Fairness doctrine governs damage turns --
    def test_damage_turns_are_governed_by_the_fairness_doctrine(self):
        """What stops a hesitation/damage turn from cheaply killing you.

        This used to assert the "Tier 1/2/3" ladder in
        `timeout_penalty_instructions`. That key was read by no code path — it
        even said "`timeout_tier` IS PROVIDED IN THE PROMPT BELOW" when nothing
        computed or passed a tier — so the test passed while the tiered penalty
        it described had never shipped, which is worse than no test at all. The
        key is gone; the doctrine that actually runs lives in the consequence
        prompt, which is what this checks.
        """
        self.assertIn("FAIRNESS", self.prompts_src.upper())
        self.assertIn("action_consequence_instructions", self.prompts_src)

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
        # Lives in action_consequence_instructions, which is read every turn.
        # A duplicate of this rule also sat in world_tick_micro_change_
        # instructions, a key nothing read; that copy is gone.
        self.assertIn("TIME-OF-DAY PROGRESSION (PHASE-LINKED)", self.prompts_src)


# ═══════════════════════════════════════════════════════════════════════════
# STRANDED FIXES
#
# Each of these was fixed once on an agent branch that never merged, and the
# bug sat in main for months while the fix sat on a branch. They're guarded
# here so the next long-lived branch can't quietly re-open them.
# ═══════════════════════════════════════════════════════════════════════════


class TestPersistentInjuries(unittest.TestCase):
    """`state['injuries']` is read by the consequence grounding and every
    choice call, and used to be written by nothing — so the UNLUCKY prompt's
    promise that a wound "becomes a persistent burden the player carries
    forward" was empty for the whole run."""

    def test_a_wound_in_the_prose_is_recorded(self):
        import engine
        st = {"injuries": []}
        self.assertTrue(engine._apply_injuries(
            st, "You vault the rail. Jagged metal opens a deep cut across your forearm."))
        self.assertEqual(len(st["injuries"]), 1)
        self.assertIn("deep cut", st["injuries"][0].lower())

    def test_a_clean_turn_records_nothing(self):
        import engine
        st = {"injuries": []}
        self.assertFalse(engine._apply_injuries(
            st, "You sprint across the yard and reach the gantry untouched."))
        self.assertEqual(st["injuries"], [])

    def test_body_parts_alone_are_not_injuries(self):
        """A false positive follows the player for the rest of the run, so the
        signal list is verbs of harm, not nouns of anatomy."""
        import engine
        st = {"injuries": []}
        self.assertFalse(engine._apply_injuries(st, "You raise a hand and steady your shoulder against the door."))
        self.assertEqual(st["injuries"], [])

    def test_wounds_are_capped_so_a_run_cannot_be_crippled_forever(self):
        import engine
        st = {"injuries": []}
        for i in range(8):
            engine._apply_injuries(st, f"Scalding steam burns your {i} hand.")
        self.assertLessEqual(len(st["injuries"]), 3)

    def test_the_turn_loop_actually_calls_it(self):
        src = (Path(__file__).parent / "engine.py").read_text(encoding="utf-8")
        self.assertIn("_apply_injuries(state, dispatch, is_timeout_penalty)", src)


class TestFlipbookTextBleed(unittest.TestCase):
    """Gemini copies text it can see in a reference image, and text it is told
    about in the prompt, into the panels it generates. Both sources of "FRAME
    1" / "0.00s" had to go."""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).parent
        cls.engine_src = (root / "engine.py").read_text(encoding="utf-8")
        cls.prompts = json.loads((root / "prompts" / "simulation_prompts.json").read_text(encoding="utf-8"))

    def test_blank_grid_template_is_preferred_over_the_numbered_one(self):
        i_blank = self.engine_src.index('if os.path.exists(blank_template_path):')
        i_numbered = self.engine_src.index('elif os.path.exists(numbered_template_path):')
        self.assertLess(i_blank, i_numbered,
                        "the numbered template has FRAME/timestamp labels printed on it")

    def test_no_timestamp_ladder_left_in_the_flipbook_prompt(self):
        leftovers = re.findall(r"\d+\.\d+s", self.prompts["gemini_flipbook_4panel_prefix"])
        self.assertEqual(leftovers, [], f"timestamp literals can be rendered as text: {leftovers}")


class TestRealtimeAnchorIsNotAnFPS(unittest.TestCase):
    """The realtime path has no negative prompt, so anything the world model
    must not draw has to be banned inside the anchor. "First-person" plus a
    motion verb is the strongest FPS cue there is, and the models answered it
    with a weapon, a crosshair and a health bar."""

    def test_anchor_bans_weapons_and_hud(self):
        import engine
        anchor = engine.REALTIME_STYLE_ANCHOR.lower()
        for term in ("no weapon", "no crosshair", "no hud"):
            self.assertIn(term, anchor)

    def test_the_ban_survives_into_the_built_prompt(self):
        import engine
        prompt = engine.build_realtime_prompt("A flooded hold.", "", "Climb the ladder").lower()
        self.assertIn("no crosshair", prompt)
        self.assertLess(len(prompt), 2000, "world-model prompts are capped at 2000 chars")


class TestTapeIsPerRun(unittest.TestCase):
    """/api/tape used to rebuild the reel by globbing the image directory by
    mtime, which spliced every run that session had ever played into one tape
    — and always read the 'default' session, so a player on their own session
    watched somebody else's."""

    def test_scene_images_are_recorded_on_the_run(self):
        src = (Path(__file__).parent / "engine.py").read_text(encoding="utf-8")
        self.assertIn("st['tape_frames'] = tape[-400:]", src)

    def test_reset_starts_a_fresh_reel(self):
        src = (Path(__file__).parent / "engine.py").read_text(encoding="utf-8")
        self.assertIn('"tape_frames": []', src)

    def test_the_endpoint_is_session_scoped_and_reads_the_run(self):
        src = (Path(__file__).parent / "api.py").read_text(encoding="utf-8")
        tape = src[src.index("def api_tape("):]
        tape = tape[:tape.index("\n@app.route")]
        self.assertIn("tape_frames", tape)
        self.assertNotIn("_get_image_dir('default')", tape)


class TestDegradedTurnsStayInFiction(unittest.TestCase):
    """`_ask` returns sentinel strings on failure, and the turn loop only
    guarded against an EMPTY dispatch — so "Signal interrupted due to
    timeout..." was narrated to the player as the story."""

    def test_sentinels_are_recognised_as_failures(self):
        import engine
        for bad in ("Signal interrupted due to timeout...",
                    "Signal interrupted — Anthropic API key not configured.",
                    "The transmission wavers... static fills the air.",
                    ""):
            self.assertTrue(engine._is_failure_dispatch(bad), bad)

    def test_real_prose_is_not_mistaken_for_a_failure(self):
        import engine
        self.assertFalse(engine._is_failure_dispatch(
            "You vault the rail and land hard on the deck plating."))

    def test_the_replacement_is_world_neutral(self):
        """These run in whatever level the player authored, so they describe
        the camera failing, never the setting."""
        import engine
        blob = " ".join(engine._DIEGETIC_DISPATCHES).lower()
        for setting_word in ("desert", "facility", "quarantine", "horizon", "mesa"):
            self.assertNotIn(setting_word, blob)

    def test_the_turn_loop_masks_before_narrating(self):
        src = (Path(__file__).parent / "engine.py").read_text(encoding="utf-8")
        self.assertIn("degraded = _is_failure_dispatch(dispatch)", src)
        self.assertIn("dispatch = _diegetic_dispatch(choice)", src)

    def test_autoplay_can_still_tell_a_masked_failure_apart(self):
        """Masking makes the text look real, so the flag is the only signal
        left — without it a fully broken run reports 100% real narrative."""
        src = (Path(__file__).parent / "autoplay.py").read_text(encoding="utf-8")
        self.assertIn("degraded_turn", src)
        self.assertIn("not degraded_turn", src)


class TestDegradedChoicesRotate(unittest.TestCase):
    """Several degraded turns in a row used to serve an identical slate, which
    reads as the game having frozen even though it still accepts input."""

    def _slate(self, scene):
        # `choices` imports ai_provider_manager inside the function, so patch
        # the module itself rather than an attribute on `choices`.
        import ai_provider_manager, choices
        # Short-circuit to the fallback path without touching the network.
        with patch.object(ai_provider_manager, "is_mock_active", return_value=True):
            return choices.generate_choices(None, "", "", world_prompt=scene)

    def test_different_scenes_get_different_fallback_slates(self):
        a = self._slate("a flooded shipbreaking yard at dawn")
        b = self._slate("a concrete corridor deep underground")
        self.assertEqual(len(a), 3)
        self.assertEqual(len(b), 3)
        self.assertNotEqual(a, b)

    def test_the_same_scene_is_stable(self):
        scene = "a flooded shipbreaking yard at dawn"
        self.assertEqual(self._slate(scene), self._slate(scene))

    def test_a_scene_matching_no_keywords_still_gets_three(self):
        """It used to fall through with only the two stealth options."""
        self.assertEqual(len(self._slate("an empty white room")), 3)


class TestRealtimeFailuresAreNamed(unittest.TestCase):
    """Capacity was the only failure with a name, so an unconfigured server, a
    blocked SDK and a rejected key all produced the same shrug."""

    @classmethod
    def setUpClass(cls):
        cls.src = (Path(__file__).parent / "static" / "js" / "reactor_renderer.js").read_text(encoding="utf-8")

    def test_each_distinct_cause_is_classified(self):
        for reason in ("not_configured", "bad_key", "sdk_blocked", "token_exchange_failed", "capacity"):
            self.assertIn(f'"{reason}"', self.src)

    def test_the_toast_uses_the_classified_hint(self):
        client = (Path(__file__).parent / "static" / "js" / "standalone.js").read_text(encoding="utf-8")
        self.assertIn("lastErr && lastErr.hint", client)

    def test_stream_health_is_measured_not_assumed(self):
        """A stalled stream keeps showing its last decoded frame, so `status`
        alone cannot tell a running world from a frozen picture of one."""
        self.assertIn("function getTelemetry", self.src)
        self.assertIn("stalled:", self.src)
        self.assertIn("getTelemetry: getTelemetry", self.src)


class TestInvestigationGroundsTheTurn(unittest.TestCase):
    """The client sent `investigation_id`, which only meant something in that
    tab — nothing was uploaded — so the engine dropped it and "loaded, describe
    your action" was a promise the turn never kept."""

    def test_the_client_sends_the_capture_not_just_the_id(self):
        src = (Path(__file__).parent / "static" / "js" / "standalone.js").read_text(encoding="utf-8")
        self.assertIn("investigation_frame: investigationFrame", src)

    def test_the_engine_ingests_it_as_an_img2img_reference(self):
        src = (Path(__file__).parent / "engine.py").read_text(encoding="utf-8")
        self.assertIn("investigation_frame = data.get('investigation_frame')", src)
        self.assertIn("_ingest_realtime_frame(investigation_frame, session_id)", src)


class TestPresenceIsPerRun(unittest.TestCase):
    """Main gives each visitor their own persisted instance, so a global
    headcount would tell someone alone in a private run that four people are
    watching."""

    def setUp(self):
        import presence
        presence.reset()

    def test_two_runs_do_not_see_each_other(self):
        import presence
        presence.touch("runA", "v1")
        presence.touch("runA", "v2")
        presence.touch("runB", "v3")
        self.assertEqual(presence.snapshot("runA")["count"], 2)
        self.assertEqual(presence.snapshot("runB")["count"], 1)

    def test_acting_marks_a_viewer_as_steering(self):
        import presence
        presence.touch("runA", "v1")
        snap = presence.touch("runA", "v2", active=True)
        self.assertEqual(snap["count"], 2)
        self.assertEqual(snap["active_count"], 1)

    def test_leaving_drops_the_viewer_immediately(self):
        import presence
        presence.touch("runA", "v1")
        presence.touch("runA", "v2")
        presence.leave("runA", "v2")
        self.assertEqual(presence.snapshot("runA")["count"], 1)

    def test_a_stale_viewer_times_out(self):
        import presence, time as _t
        presence.touch("runA", "v1")
        with patch.object(presence.time, "time", return_value=_t.time() + presence.PRESENCE_TTL_SECONDS + 5):
            self.assertEqual(presence.snapshot("runA")["count"], 0)

    def test_a_garbage_viewer_id_cannot_wedge_it(self):
        import presence
        self.assertEqual(presence.touch("runA", "")["count"], 0)
        presence.leave("runA", None)  # must not raise


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
