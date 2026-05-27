#!/usr/bin/env python3
"""Regression test for `choices.generate_choices()` defensive parsing.

This suite locks in the fix for "Generating choices failed on latest on
the initial turn" by exercising the four classic Gemini failure modes that
used to bubble up to bot.py's Phase 2 guard and produce generic "Look
around / Move forward / Wait" filler:

  1. Empty `candidates` array.
  2. Candidate with `finishReason: SAFETY` and NO `content.parts`.
  3. Candidate with `parts` containing a `functionCall` but no `text`.
  4. Successful response — the happy path is still untouched.

We also test the source-level invariants:

  5. The choices payload must include BLOCK_NONE safetySettings (or the
     same SAFETY block above will silently strip text again).
  6. The bot's intro fallback builder (`_build_intro_fallback_choices`) must
     never produce the legacy "Look around carefully / Move forward slowly /
     Wait and observe" string — those are explicitly forbidden by the
     player_choice_generation_instructions prompt.

Run hermetically:

    python3 -m unittest test_choices_robustness -v
"""

import os
import sys
import json
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

WORKSPACE = Path(__file__).parent.resolve()
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

# Hermetic environment
os.environ.setdefault("GEMINI_API_KEY", "test_key_choices")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("DISCORD_ENABLED", "0")


def _make_response(status: int, payload: dict) -> MagicMock:
    """Build a fake requests.Response with the given JSON payload."""
    r = MagicMock()
    r.status_code = status
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=payload)
    return r


class TestChoicesParseRobustness(unittest.TestCase):
    """generate_choices must never raise on malformed Gemini responses."""

    @classmethod
    def setUpClass(cls):
        # Import after env is configured so engine.GEMINI_API_KEY is set.
        import choices  # noqa: F401
        cls.choices = choices

    def _call(self, fake_resp_payload, status=200):
        """Invoke generate_choices with a fake `requests.post` injection."""
        with patch("requests.post") as mock_post:
            mock_post.return_value = _make_response(status, fake_resp_payload)
            # Also stub the choice_critic LLM call so we don't hit the
            # network. choice_critic is called from inside generate_choices.
            with patch.object(self.choices.engine, "_ask", return_value=""):
                return self.choices.generate_choices(
                    None,
                    "SITUATION SUMMARY: {situation_summary}\nCURRENT DISPATCH: {dispatch}\nIMAGE DESCRIPTION: {image_description}\n{beat_nudge}\n{seen_elements}\n{injury_state}",
                    last_dispatch="You stand on a lookout tower above the Horizon facility.",
                    n=3,
                    image_url=None,
                    seen_elements="",
                    recent_choices="",
                    caption="",
                    image_description="",
                    world_prompt="rusted lookout tower platform overlooking facility",
                    temperature=0.7,
                    situation_summary="",
                    injury_state="none",
                )

    def test_empty_candidates_returns_contextual_fallback(self):
        """No candidates → 3 contextual choices, never throws."""
        result = self._call({"candidates": []})
        self.assertIsInstance(result, list)
        self.assertGreaterEqual(len(result), 2)
        # Must not be the legacy filler.
        joined = " | ".join(result).lower()
        self.assertNotIn("look around", joined)
        self.assertNotIn("wait and observe", joined)

    def test_safety_blocked_candidate_returns_contextual_fallback(self):
        """SAFETY finishReason with no parts → no crash."""
        result = self._call({
            "candidates": [
                {
                    "finishReason": "SAFETY",
                    "content": {},  # no parts!
                    "safetyRatings": [
                        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "probability": "HIGH"}
                    ],
                }
            ],
            "promptFeedback": {"blockReason": "SAFETY"},
        })
        self.assertIsInstance(result, list)
        self.assertGreaterEqual(len(result), 2)

    def test_parts_without_text_returns_contextual_fallback(self):
        """Parts present but only functionCall (no text) → no crash."""
        result = self._call({
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"functionCall": {"name": "noop", "args": {}}}]},
                }
            ]
        })
        self.assertIsInstance(result, list)
        self.assertGreaterEqual(len(result), 2)

    def test_happy_path_still_parses(self):
        """Normal text response is still parsed and returned (3 choices)."""
        result = self._call({
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{
                        "text": (
                            "1. Vault over the rusted railing\n"
                            "2. Scramble down the rocky slope\n"
                            "3. Crouch low and scan the perimeter\n"
                        )
                    }]},
                }
            ]
        })
        self.assertIsInstance(result, list)
        # choice_critic is stubbed to return "" so we keep the original opts.
        self.assertGreaterEqual(len(result), 2)
        joined = " | ".join(result).lower()
        # At least one of the LLM-supplied choices should survive the
        # downstream filters (vault / scramble / crouch are all VISIBLE in
        # the world_prompt "rusted lookout tower platform overlooking
        # facility" via standard tokens like "rusted" and "platform").
        self.assertTrue(
            any(
                tok in joined
                for tok in ("vault", "scramble", "crouch", "rocky", "perimeter")
            ),
            f"Expected real LLM choices to survive filters, got: {result!r}",
        )


class TestChoicesPayloadHasSafetySettings(unittest.TestCase):
    """Source-level invariant: choices payload disables Gemini safety filters.

    Without BLOCK_NONE, a dark dispatch (e.g. one containing "blood" or
    "viscera" in the seen_elements grounding block) silently strips the
    candidate's `parts` and the resulting parse failure looks like a total
    choices regression to the player.
    """

    def test_choices_payload_has_block_none_settings(self):
        src = (WORKSPACE / "choices.py").read_text(encoding="utf-8")
        # Either as Python literal or formatted strings — must appear.
        self.assertIn("safetySettings", src)
        self.assertIn("BLOCK_NONE", src)
        # All four categories must be disabled.
        for cat in (
            "HARM_CATEGORY_HARASSMENT",
            "HARM_CATEGORY_HATE_SPEECH",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT",
            "HARM_CATEGORY_DANGEROUS_CONTENT",
        ):
            self.assertIn(cat, src, f"Missing {cat} in choices safety settings")


class TestBotIntroFallbackIsContextual(unittest.TestCase):
    """`_build_intro_fallback_choices` must not return legacy corporate filler."""

    def test_fallback_not_legacy_filler(self):
        """The forbidden strings must NEVER be emitted by the fallback."""
        src = (WORKSPACE / "bot.py").read_text(encoding="utf-8")
        # The function must exist…
        self.assertIn("def _build_intro_fallback_choices", src)
        # …and the legacy strings must NOT appear inside its body. We
        # detect the function body via a slice from the def to the next
        # top-level def, which is good enough for a static check.
        start = src.find("def _build_intro_fallback_choices")
        next_def = src.find("\n    def ", start + 1)
        body = src[start:next_def] if next_def != -1 else src[start:]
        forbidden = (
            "Look around carefully",
            "Move forward slowly",
            "Wait and observe",
        )
        for f in forbidden:
            self.assertNotIn(
                f, body,
                f"Intro fallback must not emit legacy corporate filler: {f!r}",
            )

    def test_intro_path_uses_contextual_fallback(self):
        """Both PlayButton and PlayNoImagesButton must call the contextual
        fallback builder when Phase 2 / generate_intro_turn yields no
        choices — never the old hard-coded list."""
        src = (WORKSPACE / "bot.py").read_text(encoding="utf-8")
        # The function must be called at least 3 times (PlayButton with-images
        # path, PlayButton no-images path, PlayNoImagesButton legacy path).
        self.assertGreaterEqual(
            src.count("_build_intro_fallback_choices("),
            3,
            "Intro fallback builder must be called from all intro code paths",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
