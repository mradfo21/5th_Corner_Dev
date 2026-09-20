#!/usr/bin/env python3
"""Regression test for `choices.generate_choices()` defensive parsing.

This suite locks in the fix for "Generating choices failed on latest on
the initial turn" by exercising the four classic Gemini failure modes that
used to bubble up through the choice generator and produce generic "Look
around / Move forward / Wait" filler:

  1. Empty `candidates` array.
  2. Candidate with `finishReason: SAFETY` and NO `content.parts`.
  3. Candidate with `parts` containing a `functionCall` but no `text`.
  4. Successful response — the happy path is still untouched.

We also test the source-level invariants:

  5. The choices payload must include BLOCK_NONE safetySettings (or the
     same SAFETY block above will silently strip text again).

Run hermetically:

    python3 -m unittest test_choices_robustness -v
"""

import os
import re
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
                    "SITUATION SUMMARY: {situation_summary}\nCURRENT DISPATCH: {dispatch}\nIMAGE DESCRIPTION: {image_description}\n{beat_nudge}\n{seen_elements}",
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


class TestChoiceSlateIsAlwaysFull(unittest.TestCase):
    """The slate handed to the UI must always hold `n` options.

    Every stage of generate_choices can REMOVE an option — diversity, the
    meaningless-choice drop, the critic de-duping against recent turns — and
    only a completely EMPTY result was ever refilled. A turn that lost one
    option to a filter therefore served two buttons where the UI lays out
    three, which reads as the game running out of ideas rather than as a
    filter doing its job.
    """

    @classmethod
    def setUpClass(cls):
        import choices  # noqa: F401
        cls.choices = choices

    def _call(self, llm_text, critic_text=""):
        payload = {
            "candidates": [
                {"finishReason": "STOP", "content": {"parts": [{"text": llm_text}]}}
            ]
        }
        with patch("requests.post") as mock_post:
            mock_post.return_value = _make_response(200, payload)
            with patch.object(self.choices.engine, "_ask", return_value=critic_text):
                return self.choices.generate_choices(
                    None,
                    "CURRENT DISPATCH: {dispatch}",
                    last_dispatch="You crouch behind a rusted crate inside the facility yard.",
                    n=3,
                    world_prompt="rusted crates in a facility yard, chain-link fence",
                    temperature=0.7,
                    situation_summary="",
                )

    def test_critic_returning_two_choices_is_topped_up(self):
        result = self._call(
            "1. Vault over the rusted railing\n"
            "2. Scramble down the rocky slope\n"
            "3. Shoulder the crate aside\n",
            critic_text="Vault over the rusted railing\nScramble down the rocky slope\n",
        )
        self.assertEqual(len(result), 3, f"slate came back short: {result!r}")

    def test_single_usable_option_is_topped_up(self):
        result = self._call("1. Vault over the rusted railing\n")
        self.assertEqual(len(result), 3, f"slate came back short: {result!r}")

    def test_slate_never_exceeds_n(self):
        result = self._call(
            "1. Vault over the rusted railing\n"
            "2. Scramble down the rocky slope\n"
            "3. Shoulder the crate aside\n"
            "4. Sprint for the fence gap\n"
            "5. Crawl beneath the pipes\n"
        )
        self.assertEqual(len(result), 3)

    def test_topped_up_choices_are_unique(self):
        result = self._call("1. Vault over the rusted railing\n")
        self.assertEqual(len(set(c.lower() for c in result)), len(result))

    def test_critic_rewrite_cannot_serve_a_long_clause(self):
        """The critic used to rewrite past the parse cap and skip truncate."""
        long_line = (
            "Attempt to pry the jagged, shattered glass shards from the "
            "smashed CRT monitor"
        )
        result = self._call(
            "1. Vault over the rusted railing\n"
            "2. Scramble down the rocky slope\n"
            "3. Shoulder the crate aside\n",
            critic_text=(
                long_line + "\n"
                "Drag the body clear\n"
                "Break and run for open ground\n"
            ),
        )
        self.assertEqual(len(result), 3)
        for line in result:
            self.assertNotIn("…", line)
            self.assertLessEqual(len(line.split()), self.choices.CHOICE_MAX_WORDS)
            self.assertLessEqual(len(line), self.choices.CHOICE_MAX_CHARS)
        self.assertFalse(any("from the" in line.lower() for line in result))

    def test_long_llm_lines_are_clipped_not_dropped(self):
        result = self._call(
            "1. Attempt to pry the jagged shattered glass shards from the monitor\n"
            "2. Drag the body clear of the sparks\n"
            "3. Break and run for open ground\n",
            critic_text="",
        )
        self.assertEqual(len(result), 3)
        joined = " | ".join(result).lower()
        self.assertTrue("pry" in joined or "attempt" in joined)
        for line in result:
            self.assertNotIn("…", line)
            self.assertLessEqual(len(line.split()), self.choices.CHOICE_MAX_WORDS)
            self.assertLessEqual(len(line), self.choices.CHOICE_MAX_CHARS)


class TestFastPathRequiresFullSlate(unittest.TestCase):
    """The turn loop's fast path must not serve a short slate.

    advance_turn_choices_deferred can reuse the next-action options the
    consequence call already produced and skip two LLM round-trips. It used
    to take that shortcut on as few as TWO clean options, so the saving was
    paid for with a button the player never got. Falling back to the full
    generator on a short list costs one round-trip on those turns only.
    """

    def test_fast_path_threshold_is_the_full_slate(self):
        src = (WORKSPACE / "engine.py").read_text(encoding="utf-8")
        block = src.split("# FAST PATH:", 1)[1].split("if _pregen is not None:", 1)[0]
        self.assertIn("len(_cleaned) >= SLATE_SIZE", block)
        self.assertNotIn("len(_cleaned) >= 2", block)

    def test_slate_size_is_shared_with_the_generator(self):
        import choices
        self.assertEqual(choices.SLATE_SIZE, 3)
        src = (WORKSPACE / "engine.py").read_text(encoding="utf-8")
        self.assertIn("from choices import drop_meaningless_choices, enforce_diversity, SLATE_SIZE", src)


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


class TestMeaninglessChoiceFilter(unittest.TestCase):
    """The dead-turn filter must catch observation/waiting choices even when
    they're dressed with a leading adverb (the 'Carefully inspect …' slip)."""

    def setUp(self):
        import choices
        self.choices = choices

    def test_adverb_dressed_observation_is_meaningless(self):
        for c in (
            "Carefully inspect the pulsing, vein-like filaments of the Red Biome creeping",
            "Quietly study the door",
            "Slowly examine the mass",
            "Cautiously observe the guards",
            "Silently watch the corridor",
            "Inspect the panel",
            "Wait and listen",
        ):
            self.assertTrue(
                self.choices.is_meaningless_choice(c),
                f"dead turn slipped through the filter: {c!r}",
            )

    def test_forward_movement_choices_are_kept(self):
        for c in (
            "Advance on the loading dock",
            "Cross to the pulsing growth",
            "Sprint down the corridor",
            "Vault over the fence",
            "Slip through the gap to the next room",
            "Apply pressure to the wound",  # a real -ly-prefixed action verb
        ):
            self.assertFalse(
                self.choices.is_meaningless_choice(c),
                f"a valid forward action was wrongly filtered: {c!r}",
            )


class TestChoiceLength(unittest.TestCase):
    def setUp(self):
        import choices
        self.choices = choices

    def test_long_clause_is_clipped_without_ellipsis(self):
        raw = (
            "Attempt to pry the jagged, shattered glass shards from the "
            "smashed CRT monitor"
        )
        out = self.choices.truncate_choice(raw)
        self.assertNotIn("…", out)
        self.assertNotIn("...", out)
        self.assertLessEqual(len(out.split()), self.choices.CHOICE_MAX_WORDS)
        self.assertLessEqual(len(out), self.choices.CHOICE_MAX_CHARS)
        self.assertTrue(out.lower().startswith("pry"))
        self.assertNotIn("attempt", out.lower())

    def test_short_choice_is_left_alone(self):
        self.assertEqual(self.choices.truncate_choice("Pry the glass free"), "Pry the glass free")

    def test_does_not_end_on_a_dangling_the(self):
        out = self.choices.truncate_choice(
            "Attempt to pry the jagged shattered glass shards from the"
        )
        self.assertFalse(out.lower().endswith(" the"))
        self.assertFalse(out.lower().endswith(" from"))

    def test_enforce_diversity_clips_the_slate(self):
        slate = self.choices.enforce_diversity([
            "Attempt to pry the jagged, shattered glass shards from the monitor",
            "Drag",
            "Break and run for open ground past the fence line toward the mesa",
        ])
        for line in slate:
            self.assertNotIn("…", line)
            self.assertLessEqual(len(line.split()), self.choices.CHOICE_MAX_WORDS)
            self.assertLessEqual(len(line), self.choices.CHOICE_MAX_CHARS)


class TestChoiceImageResolvesSessionFrames(unittest.TestCase):
    """Realtime observe writes JPEG grabs under sessions/<id>/images/.
    generate_choices used to look in the legacy images/ folder and never
    attached the live frame, so MOVE-TO-style slates stayed on the last
    still's nouns."""

    def test_generate_choices_uses_session_aware_resolver(self):
        src = Path(__file__).parent.joinpath("choices.py").read_text(encoding="utf-8")
        block = src.split("if image_url:", 1)[1].split("print(f\"[GEMINI TEXT]", 1)[0]
        self.assertIn("_resolve_image_path", block)
        self.assertIn("_sniff_image_mime", block)
        self.assertNotIn('Path("images") / image_url.replace("/images/", "")', block)


class TestStallChoicesAreCaughtByShapeNotSpelling(unittest.TestCase):
    """The stall list was phrase-literal, so it only caught the exact wordings
    someone had already seen. A live run offered — and the harness played —
    "Press yourself against ribbed wall", a dead turn that shares no marker
    with the "press your back against the wall" already on the list."""

    def setUp(self):
        import choices
        self.choices = choices

    def test_braced_against_a_surface_is_a_dead_turn(self):
        for stall in (
            "Press yourself against ribbed wall",
            "Hug the tunnel wall",
            "Press your back against the wall",
            "Flatten yourself against the rock",
            "Cling to the rusted ledge",
            "Hunker behind the low stone wall",
            "Pin your shoulder against the hatch",
        ):
            with self.subTest(stall=stall):
                self.assertTrue(self.choices.is_meaningless_choice(stall))

    def test_real_actions_are_not_swept_up(self):
        # These all contain a bracing verb or a surface noun and must survive.
        for good in (
            "Smash through the fleshy wall",
            "Throw yourself through the window",
            "Crawl beneath the pipe rack",
            "Duck through the low opening",
            "Vault into the dark tunnel",
            "Kick open rusted gate",
            "Wrench free the metal grate",
        ):
            with self.subTest(good=good):
                self.assertFalse(self.choices.is_meaningless_choice(good))


class TestTheSlateRemembersWhatWasAlreadyPlayed(unittest.TestCase):
    """The slate is built from the rendered frame, and img2img keeps that frame
    visually continuous — so the same landmarks stay on screen and the model
    re-derives the same options from them. A live 12-turn run offered a version
    of "vault the chain link fence" on six consecutive turns and the player
    crossed the same fence four times.

    The cause was plumbing, not the model: `recent_choices` was hardcoded to ''
    at every engine call site AND the choice template had no {recent_choices}
    slot, so both the prompt and the de-dupe filters were inert.
    """

    def setUp(self):
        import choices
        self.choices = choices

    def test_a_reworded_repeat_is_dropped(self):
        kept = self.choices.drop_recently_done(
            ["Vault over the chain link fence", "Sprint toward the red mesa"],
            ["Vault the chain link fence"],
        )
        self.assertNotIn("Vault over the chain link fence", kept)
        self.assertIn("Sprint toward the red mesa", kept)

    def test_it_never_hands_back_an_empty_slate(self):
        # A repeated option still beats no buttons at all.
        kept = self.choices.drop_recently_done(
            ["Vault the chain link fence"], ["Vault the chain link fence"],
        )
        self.assertTrue(kept)

    def test_no_memory_is_a_passthrough(self):
        opts = ["Vault the fence", "Sprint to the mesa"]
        self.assertEqual(self.choices.drop_recently_done(opts, []), opts)
        self.assertEqual(self.choices.drop_recently_done(opts, ""), opts)

    def test_normalize_accepts_the_shapes_callers_actually_pass(self):
        self.assertEqual(self.choices.normalize_recent(""), [])
        self.assertEqual(self.choices.normalize_recent(None), [])
        self.assertEqual(self.choices.normalize_recent("x"), ["x"])
        self.assertEqual(self.choices.normalize_recent(["x", "y"]), ["x", "y"])

    def test_the_template_has_a_slot_for_it_in_every_bound_copy(self):
        """`prompts/simulation_prompts.json` is a derived scratch pad — binding
        a World writes that World's frozen snapshot over it. The first attempt
        at this fix patched only the live file and reverted the moment the
        harness picked a World. The slot has to exist in every copy or it is
        not really there. (Same trap as TestTheBoundWorldsCarryTheShippedVoice
        in test_narrator_grounding.)"""
        root = Path(__file__).parent
        key = "player_choice_generation_instructions"

        def template_of(path):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return None
            if isinstance(doc.get(key), str):
                return doc[key]
            prompts = doc.get("prompts")
            if isinstance(prompts, dict) and isinstance(prompts.get(key), str):
                return prompts[key]
            return None

        checked = 0
        stale = []
        candidates = [root / "prompts" / "simulation_prompts.json",
                      root / "prompts" / "simulation_prompts.defaults.json"]
        candidates += sorted(p for p in (root / "worlds").glob("*.json")
                             if not p.name.endswith(".frame.json"))
        for path in candidates:
            tmpl = template_of(path)
            if tmpl is None:
                continue
            checked += 1
            if "{recent_choices}" not in tmpl:
                stale.append(path.name)
        self.assertGreater(checked, 1, "found no choice templates to check")
        self.assertEqual(
            stale, [],
            "these copies will overwrite the slate's memory on the next "
            "Play/reset: " + ", ".join(stale))

    def test_every_mid_game_call_site_passes_the_memory(self):
        """This is the regression that matters: the argument existed, the
        filters consumed it, and every call site passed ''. Only the two intro
        paths may pass nothing — at the opening there is no history to repeat."""
        src = Path(__file__).parent.joinpath("engine.py").read_text(encoding="utf-8")
        lines = src.splitlines()
        empty_at = [i for i, l in enumerate(lines) if "recent_choices=''" in l]
        wired_at = [i for i, l in enumerate(lines)
                    if "recent_choices=recent_actions_taken(state)" in l]
        self.assertGreaterEqual(len(wired_at), 2, "mid-game slates lost their memory")

        def enclosing(idx):
            for j in range(idx, -1, -1):
                m = re.match(r"^\s*def (\w+)", lines[j])
                if m:
                    return m.group(1)
            return "?"

        still_empty = sorted(enclosing(i) for i in empty_at)
        self.assertEqual(
            still_empty, ["generate_intro_choices_deferred", "generate_intro_turn"],
            f"a mid-game slate is still being built with no memory: {still_empty}",
        )

    def test_recent_actions_come_back_newest_last_and_without_the_beat(self):
        import engine
        state = {"recent_events": [
            "Vault the fence -> chain-link rattles",
            "Kick the hood -> metal buckles",
        ]}
        self.assertEqual(
            engine.recent_actions_taken(state),
            ["Vault the fence", "Kick the hood"],
        )
        self.assertEqual(engine.recent_actions_taken({}), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
