"""A confrontation has to accept an answer nobody wrote down.

The encounter slate was three model-authored verbs presented as three buttons,
and that was the whole vocabulary of the most consequential moment in the game.
This pins the typed action:

* what the player writes is the verb the simulation runs — it already reached
  the resolve plate and the aftermath turn, and it still has to;
* the LANE picks the outcome weights, so a written action cannot be filed under
  confront just because the keyword list did not recognise it. That was the old
  behaviour (``classify_encounter_lane(verb, 0)`` returns the index-0 lane) and
  it charged combat odds for talking;
* the keyword list stays in front of the model, because it is free and it is
  right about "run" and "hand it over";
* the slate is presented as typography over the frame, the way WATCH shows
  choices — not as lane-coloured panels that expose the bookkeeping.

Pure functions plus source assertions on the client. No network.

Run with:
    python -m unittest test_encounter_custom_action -v
"""

import os
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")

import encounter
import engine

ROOT = Path(__file__).resolve().parent
CLIENT_JS = (ROOT / "static" / "js" / "standalone.js").read_text(
    encoding="utf-8", errors="replace")
MOMENTS_JS = (ROOT / "static" / "js" / "moments.js").read_text(
    encoding="utf-8", errors="replace")
CSS = (ROOT / "static" / "css" / "standalone.css").read_text(
    encoding="utf-8", errors="replace")

SLATE = [
    {"text": "Put the foreman down", "lane": "confront"},
    {"text": "Break away from the foreman", "lane": "evade"},
    {"text": "Hand over the tape", "lane": "parley"},
]

BRIEF = {"character": {"label": "A site foreman"}, "danger": "swinging a bar"}


def _no_model():
    """Assert nothing asks the model. A keyword answer must cost nothing."""
    return mock.patch.object(engine, "_ask",
                             side_effect=AssertionError("asked the model"))


class TestWhatThePlayerWroteIsWhatHappens(unittest.TestCase):
    """The text is the verb, and it is not quietly swapped for a slate item."""

    def test_off_slate_text_survives_as_the_verb(self):
        with _no_model():
            verb, _lane = encounter.match_encounter_choice(
                "hide behind the stacked drums", "", SLATE,
                custom=True, brief=BRIEF)
        self.assertEqual(verb, "hide behind the stacked drums")

    def test_picking_a_slate_line_still_trusts_the_stored_slate(self):
        with _no_model():
            verb, lane = encounter.match_encounter_choice(
                "Put the foreman down", "confront", SLATE, custom=True)
        self.assertEqual((verb, lane), ("Put the foreman down", "confront"))

    def test_an_empty_line_cannot_commit_a_turn(self):
        with _no_model():
            verb, _lane = encounter.match_encounter_choice(
                "   ", "", SLATE, custom=True, brief=BRIEF)
        self.assertEqual(verb, "Hold your ground")


class TestTheLaneIsDecidedHonestly(unittest.TestCase):
    """The lane is the odds. Getting it wrong is the difference between
    walking out of a confrontation and dying in it."""

    def test_a_written_action_is_not_filed_as_a_punch_by_default(self):
        """The bug: every unrecognised line became confront.

        `dance for him` is not force and not flight, so it resolves to parley —
        the lane for dealing with something by means other than violence.
        """
        with mock.patch.object(engine, "_ask",
                               side_effect=RuntimeError("offline")):
            self.assertEqual(
                encounter.classify_custom_lane("dance for him", BRIEF),
                "parley")

    def test_the_old_path_is_untouched_for_authored_verbs(self):
        """Only TYPED text gets read. A model-authored slate keeps the
        index fallback it was built around."""
        with _no_model():
            _verb, lane = encounter.match_encounter_choice(
                "Stare at the treeline", "", SLATE, custom=False)
        self.assertEqual(lane, "confront")

    def test_obvious_wording_never_spends_a_call(self):
        for action, lane in (
            ("hide behind the stacked drums", "evade"),
            ("crawl under the trailer", "evade"),
            ("offer him the data drive", "parley"),
            ("tackle him into the fence", "confront"),
        ):
            with self.subTest(action=action), _no_model():
                self.assertEqual(
                    encounter.classify_custom_lane(action, BRIEF), lane)

    def test_the_model_reads_what_the_keywords_cannot(self):
        seen = {}

        def ask(prompt, **kw):
            seen["prompt"] = prompt
            seen["schema"] = kw.get("response_schema")
            return {"lane": "confront"}

        with mock.patch.object(engine, "_ask", side_effect=ask):
            lane = encounter.classify_custom_lane(
                "throw my camera at his head", BRIEF)
        self.assertEqual(lane, "confront")
        # It is told what it is answering about, and constrained to the lanes.
        self.assertIn("throw my camera at his head", seen["prompt"])
        self.assertIn("A site foreman", seen["prompt"])
        self.assertEqual(seen["schema"], encounter.ENCOUNTER_LANE_SCHEMA)

    def test_a_junk_answer_falls_back_instead_of_inventing_a_lane(self):
        with mock.patch.object(engine, "_ask", return_value={"lane": "vibes"}):
            self.assertEqual(
                encounter.classify_custom_lane("sing to him", BRIEF), "parley")

    def test_a_lane_read_off_the_typed_text_reaches_the_caller(self):
        with mock.patch.object(engine, "_ask", return_value={"lane": "evade"}):
            verb, lane = encounter.match_encounter_choice(
                "put the trailer between us", "", SLATE,
                custom=True, brief=BRIEF)
        self.assertEqual((verb, lane), ("put the trailer between us", "evade"))

    def test_hiding_is_breaking_contact(self):
        """Written actions reach the keyword list too, and the obvious way a
        player avoids something — by not being found — matched nothing."""
        for word in ("hide", "sneak", "crawl"):
            with self.subTest(word=word):
                self.assertIn("evade", encounter.lane_keyword_hits(
                    f"{word} past the gate"))


class TestTheClientOffersIt(unittest.TestCase):
    """A lane the UI cannot reach is a feature nobody has."""

    def _show_choices(self):
        return CLIENT_JS.split("function showChoices(", 1)[1] \
                        .split("\n    function ", 1)[0]

    def test_the_slate_offers_a_typed_action(self):
        fn = self._show_choices()
        self.assertIn("custom:", fn)
        self.assertIn("onSubmit", fn)
        self.assertIn("custom: true", fn)

    def test_the_typed_action_is_posted_as_the_choice(self):
        payload = CLIENT_JS.split('postJSON("/api/encounter/resolve"', 1)[1] \
                           .split("}", 1)[0]
        self.assertIn("choice: text", payload)
        # The flag is what stops the server filing it under confront.
        self.assertIn("custom:", payload)

    def test_typing_does_not_also_commit_a_numbered_choice(self):
        fn = CLIENT_JS.split("function onKey(e) {\n      if (!active)", 1)[1] \
                      .split("\n    function ", 1)[0]
        self.assertIn("customChoiceOpen()", fn)

    def test_escape_leaves_the_prompt_bar_before_it_leaves_the_fight(self):
        fn = CLIENT_JS.split("function onEsc() {", 1)[1] \
                      .split("\n    function ", 1)[0]
        bar = fn.split("closeCustomChoice", 1)
        self.assertEqual(len(bar), 2, "Esc does not close the prompt bar")
        # ...and it does so BEFORE reaching for the evade lane.
        self.assertNotIn("evade", bar[0])

    def test_the_director_does_not_commit_over_someone_typing(self):
        fn = CLIENT_JS.split("function armAutoPick(", 1)[1] \
                      .split("\n    function ", 1)[0]
        self.assertIn("customChoiceOpen()", fn)


class TestTheSharedMomentChromeSupportsIt(unittest.TestCase):
    """The row and the bar live in Moments, so any Moment could offer one."""

    def test_set_choices_can_build_a_typed_row_and_a_prompt_bar(self):
        fn = MOMENTS_JS.split("function setChoices(", 1)[1] \
                       .split("\n  // The typed-action row", 1)[0]
        self.assertIn("opts", fn)
        self.assertIn("buildCustomRow", fn)
        build = MOMENTS_JS.split("function buildCustomRow(", 1)[1] \
                          .split("\n  // For key handlers", 1)[0]
        self.assertIn("moment-choice-custom", build)
        self.assertIn("moment-custom-form", build)
        self.assertIn("moment-custom-input", build)
        self.assertIn("onSubmit", build)
        self.assertIn("maxlength", build)

    def test_the_bar_is_reachable_from_a_key_handler(self):
        for name in ("openCustomChoice", "closeCustomChoice",
                     "customChoiceOpen"):
            with self.subTest(name=name):
                self.assertIn(f"    {name},", MOMENTS_JS)

    def test_clearing_the_slate_forgets_the_bar(self):
        """A stale customState would let Esc close a bar that is gone."""
        fn = MOMENTS_JS.split("function clearChoices(", 1)[1] \
                       .split("\n  function ", 1)[0]
        self.assertIn("customState = null", fn)


class TestItLooksLikeWatchAndNotLikeAMenu(unittest.TestCase):
    """"Displayed like the watch mechanic": typography over the frame."""

    def _encounter_slate_css(self):
        start = CSS.index("body.moment-encounter #moment-choices {")
        end = CSS.index("body.moment-encounter #moment-nameplate-sub")
        return CSS[start:end]

    def test_the_lane_coloured_boxes_are_gone(self):
        block = self._encounter_slate_css()
        self.assertNotIn("border-left: 3px solid", block)
        self.assertIn("background: none", block)

    def test_the_lines_are_set_like_a_watch_choice(self):
        block = self._encounter_slate_css()
        watch = CSS.split(".watch-choice {", 1)[1].split("}", 1)[0]
        for prop in ("color: #eafff2", "text-shadow"):
            with self.subTest(prop=prop):
                self.assertIn(prop, watch)
                self.assertIn(prop, block)

    def test_the_prompt_bar_matches_the_action_bar(self):
        """Same instrument as ACT in normal play, so it reads as the same
        affordance rather than a new one."""
        act = CSS.split("\n#custom-input {", 1)[1].split("}", 1)[0]
        enc = CSS.split(".moment-custom-input {", 1)[1].split("}", 1)[0]
        for prop in ("color: #eafff2", "border-bottom", "font-size: 15px"):
            with self.subTest(prop=prop):
                self.assertIn(prop, act)
                self.assertIn(prop, enc)

    def test_a_number_never_leaks_into_a_conversation_pill(self):
        self.assertIn(".moment-choice-num { display: none; }", CSS)
        self.assertIn("body.moment-encounter .moment-choice-num { display: inline; }",
                      CSS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
