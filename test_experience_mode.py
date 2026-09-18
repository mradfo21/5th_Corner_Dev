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
        """Prompt must encode 'characters/events kill, environment only injures'.

        The doctrine survived the prompt trim; its ~450-word presentation (a
        boxed heading, three bulleted cause lists, and a four-question
        checklist) did not. What is asserted here is the rule, not the layout.
        """
        self.assertIn("INJURY IS THE DEFAULT, NOT DEATH", self.prompts_src)
        self.assertIn("Only characters and dramatic events kill", self.prompts_src)
        self.assertIn("wounds badly and never kills", self.prompts_src)

    def test_unlucky_fate_modifier_forbids_cheap_deaths(self):
        """The UNLUCKY fate path must not re-introduce random impalement deaths.

        The rule survived the trim; the "FORBIDDEN UNDER UNLUCKY" heading and its
        three ❌ bullets did not. What matters is that inert scenery still cannot
        kill, which is now stated as one sentence.
        """
        self.assertIn("Only characters and dramatic events may kill", self.engine_src)
        self.assertIn("wounds badly and never", self.engine_src)

    def test_unlucky_cannot_cancel_the_players_action(self):
        """The complication is the action's COST, not its cancellation.

        Every bullet in the old UNLUCKY block could substitute for the action
        rather than charge for it — "equipment fails", "sprained ankle" — and a
        live run duly produced "you lunge forward, BUT the film strip tightens,
        snagging your boot and dragging you to your knees." The move never
        happened. The consequence template already carried "A MOVE ALWAYS
        COMPLETES" and lost the argument, because this modifier is appended after
        it and is far more concrete about what to write. So the rule has to be
        stated at the point of decision, with an example of the failure.
        """
        self.assertIn("cancellation of it", self.engine_src)
        self.assertIn("still happened", self.engine_src)

    def test_the_fate_modifier_does_not_cite_a_heading_that_is_gone(self):
        """It used to tell the model that the death-fairness doctrine "above
        still applies", naming a heading the consequence template no longer has
        (it now reads "INJURY IS THE DEFAULT, NOT DEATH"). Pointing the model at
        a governing rule it cannot locate is worse than not citing one, and a
        prompt rename orphans such a citation silently. The fix is to restate the
        rule rather than cite it, so nothing here should reference a heading."""
        self.assertNotIn("DOCTRINE above", self.engine_src)
        self.assertNotIn("DOCTRINE above still applies", self.engine_src)

    def test_luck_never_becomes_arithmetically_impossible(self):
        """A 13-turn scan_move run rolled LUCKY zero times.

        `lucky_cut = 0.25 - bias` hits zero at bias 0.25, and scan_move adds
        +0.15 unconditionally (every turn is a SCAN interaction) on top of phase
        bias (up to 0.22) and detection bias (up to 0.24). From "escalating"
        onward a break was unavailable, so the world could only be neutral or
        cruel — which reads as a game that has stopped responding to you.
        """
        import engine
        for bias in (0.0, 0.15, 0.27, 0.42, 0.5, 1.0):
            with self.subTest(bias=bias):
                rolls = [engine.compute_fate(bias) for _ in range(4000)]
                lucky = rolls.count("LUCKY") / len(rolls)
                unlucky = rolls.count("UNLUCKY") / len(rolls)
                self.assertGreater(lucky, 0.05, "relief became unavailable")
                self.assertLess(unlucky, 0.62, "misfortune became the default")

    def test_the_base_odds_are_untouched_when_nothing_is_at_stake(self):
        """The floors must not flatten the dial at bias 0 — 25/50/25 stands."""
        import engine
        rolls = [engine.compute_fate(0.0) for _ in range(6000)]
        self.assertAlmostEqual(rolls.count("LUCKY") / len(rolls), 0.25, delta=0.03)
        self.assertAlmostEqual(rolls.count("UNLUCKY") / len(rolls), 0.25, delta=0.03)

    # -- Tension rhythm --
    def test_tension_rhythm_allows_stillness_beats(self):
        """The action_consequence_instructions must allow ~30% stillness beats."""
        self.assertIn("The rest end on stillness", self.prompts_src)
        self.assertIn("Constant crescendo goes numb", self.prompts_src)
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
        self.assertIn("would call it cheap", self.prompts_src)
        self.assertIn("action_consequence_instructions", self.prompts_src)

    # -- Injury state threading --
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
    def test_time_of_day_is_not_a_writer_dial(self):
        # The writer used to be told to darken the light on a phase tip.
        # That invented a new sky in visual_scene, which the image model
        # then rendered — on top of the engine also rewriting time_of_day.
        # The evening is set at reset; the prose must not rename it.
        self.assertIn("The evening of this run is already set", self.prompts_src)
        self.assertNotIn("It darkens one tier when the phase escalates", self.prompts_src)


# ═══════════════════════════════════════════════════════════════════════════
# STRANDED FIXES
#
# Each of these was fixed once on an agent branch that never merged, and the
# bug sat in main for months while the fix sat on a branch. They're guarded
# here so the next long-lived branch can't quietly re-open them.
# ═══════════════════════════════════════════════════════════════════════════


class TestFlipbookTextBleed(unittest.TestCase):
    """Gemini copies text it can see in a reference image, and text it is told
    about in the prompt, into the panels it generates. Both sources of "FRAME
    1" / "0.00s" had to go."""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).parent
        cls.engine_src = (root / "engine.py").read_text(encoding="utf-8")
        cls.prompts = json.loads((root / "prompts" / "simulation_prompts.json").read_text(encoding="utf-8"))

    def test_the_layout_guide_the_engine_attaches_has_nothing_to_copy(self):
        # This used to be a pick between two committed template files, one of
        # which had FRAME/timestamp labels printed on it. The guides are
        # generated per grid shape now (flipbook.build_guide), and the numbered
        # variant exists only for humans reading the order — so the engine must
        # ask for a guide by shape and never for a numbered one.
        import flipbook
        flipbook_path = self.engine_src.split("def _flipbook_generate", 1)[1]
        self.assertIn("flipbook.find_guide(", flipbook_path)
        self.assertNotIn("numbered", flipbook_path.split("def _gen_image", 1)[0])
        import inspect
        self.assertIs(inspect.signature(flipbook.build_guide)
                      .parameters["numbered"].default, False,
                      "blank has to be the default guide")

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


class TestAnOutageNeverBecomesTheVisualTone(unittest.TestCase):
    """The prose path has masked `_ask` sentinels since _is_failure_dispatch
    was written. The IMAGE path had no such guard: the sentinel came back as
    the world's "visual tone", went into the render prompt, and was cached — so
    one transient 403 left every remaining frame of the session being drawn in
    the style of an error message. Found by the playtest harness.
    """

    def setUp(self):
        import engine
        self.engine = engine
        self.sid = "tone-guard-test"
        engine._VISUAL_TONE_CACHE.pop(self.sid, None)

    def tearDown(self):
        self.engine._VISUAL_TONE_CACHE.pop(self.sid, None)

    def _tone(self, answer):
        with patch.object(self.engine, "_ask", return_value=answer):
            return self.engine.summarize_world_prompt_for_image(
                "a world", session_id=self.sid, hard_transition=True)

    def test_a_good_gloss_is_returned_and_cached(self):
        got = self._tone("muted 1993 desert thriller, amber and rust tones")
        self.assertIn("amber and rust", got)
        self.assertEqual(self.engine._VISUAL_TONE_CACHE[self.sid], got)

    def test_a_failure_keeps_the_last_good_tone(self):
        good = self._tone("muted 1993 desert thriller, amber and rust tones")
        after = self._tone("Signal interrupted due to API error...")
        self.assertEqual(after, good)
        self.assertEqual(self.engine._VISUAL_TONE_CACHE[self.sid], good)

    def test_a_failure_with_no_prior_tone_contributes_nothing(self):
        self.assertEqual(
            self._tone("Signal interrupted — GEMINI_API_KEY not configured."), "")
        self.assertNotIn(self.sid, self.engine._VISUAL_TONE_CACHE)

    def test_no_sentinel_can_reach_a_render_prompt(self):
        for bad in ("Signal interrupted due to timeout...",
                    "Signal interrupted due to API error...",
                    "Signal interrupted — Anthropic API key not configured.",
                    "Signal interrupted — could not read image."):
            with self.subTest(bad=bad):
                self.assertNotIn("signal interrupted", self._tone(bad).lower())

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

    def test_the_renderer_script_does_not_redeclare_const_v(self):
        """A second `const v` in armRevealWatchdog made the whole IIFE fail
        to parse, so window.ReactorRenderer never existed and every session
        stayed on stills with a green ACCOUNT lamp."""
        watchdog = self.src.split("function armRevealWatchdog", 1)[1].split("function clearRevealWatchdog", 1)[0]
        self.assertEqual(watchdog.count("const v ="), 1)

    def test_each_distinct_cause_is_classified(self):
        for reason in ("not_configured", "bad_key", "sdk_blocked", "token_exchange_failed", "capacity"):
            self.assertIn(f'"{reason}"', self.src)

    def test_the_toast_uses_the_classified_hint(self):
        client = (Path(__file__).parent / "static" / "js" / "standalone.js").read_text(encoding="utf-8")
        self.assertIn("lastErr && lastErr.hint", client)

    def test_a_warmup_reapply_does_not_restage_the_same_seed(self):
        """The editor poll used to re-apply the cached frame while the freeze
        still covered the video, which reset LingBot and hid the stream."""
        apply = self.src.split("function applyScene", 1)[1][:2800]
        self.assertIn("const sameGuide", apply)
        self.assertIn("rstate.started || rstate.applying || showing", apply)

    def test_stream_health_is_measured_not_assumed(self):
        """A stalled stream keeps showing its last decoded frame, so `status`
        alone cannot tell a running world from a frozen picture of one."""
        self.assertIn("function getTelemetry", self.src)
        self.assertIn("stalled:", self.src)
        self.assertIn("getTelemetry: getTelemetry", self.src)


class TestAuthoredCameraBeatsStaleFirstPerson(unittest.TestCase):
    """Every instance was first person despite a controls override: leftover
    localStorage beat the cast sheet, LingBot never restaged, and CONTROLS
    stayed on first_person until a later Camera.load that often lost the race."""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).parent
        cls.reactor = (root / "static" / "js" / "reactor_renderer.js").read_text(encoding="utf-8")
        cls.client = (root / "static" / "js" / "standalone.js").read_text(encoding="utf-8")

    def test_authored_camera_beats_leftover_localstorage(self):
        fn = self.reactor.split("function happyOysterPerspective", 1)[1].split("function directorParams", 1)[0]
        self.assertIn(
            "rstate.hoPerspective || page || rstate.authoredPerspective || stored",
            fn,
        )

    def test_lingbot_restages_when_the_camera_changes(self):
        fn = self.reactor.split("rebuildWorld:", 1)[1].split("getExperience:", 1)[0]
        self.assertNotIn("if (!isHappyOyster() || !rstate.started) return false;", fn)
        self.assertIn("decoratePrompt", fn)
        self.assertIn("hardTransition: true", fn)

    def test_scene_prompts_carry_the_authored_vantage(self):
        self.assertIn("function decoratePrompt", self.reactor)
        self.assertIn("window.__InputBindings.followCamera", self.reactor)
        self.assertIn("Camera.prefix()", self.client)
        self.assertIn("cameraReady.then(bootRenderer", self.client)
        self.assertIn("resteerLiveFromSheet", self.client)
        self.assertIn("keepSheet: true", self.client)
        self.assertIn("function paintCompiled", self.client)
        self.assertIn("data-compiled-for", self.client)
        steer = self.client.split("function worldSteerPrompt", 1)[1][:1800]
        self.assertIn("Camera.prefix()", steer)
        self.assertNotIn("has(who)", steer)
        self.assertIn("setTimeout(commit, 520)", self.client)
        self.assertIn('addEventListener("input"', self.client)
        decorate = self.reactor.split("function decoratePrompt", 1)[1].split("async function loadConfig", 1)[0]
        self.assertIn("cam.prefix", decorate)
        resolve = self.reactor.split("function resolveModelId", 1)[1].split("async function fetchToken", 1)[0]
        self.assertIn("pick(q) || pick(rstate.cfg.world_model) || pick(stored)", resolve)
        keep = self.client.split("function keepLiveExperience", 1)[1][:2400]
        self.assertIn("samePrompt", keep)


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
