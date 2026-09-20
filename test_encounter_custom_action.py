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

import json
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


class TestATypedActionIsNeverRefused(unittest.TestCase):
    """Reported twice: "I asked for a custom action to go to kansas city and it
    didn't do it", then "I tried 'fly to antartica' and it didn't even try".

    The second one came back as "You attempt to take flight, but your body
    remains pinned to the unforgiving red earth" — which is verbatim the shape
    `action_consequence_instructions` already forbids in capitals. The rule was
    not missing; it was being contradicted by the FREE WILL block injected right
    next to the action, which said "Show the ATTEMPT". An attempt is a thing
    that can fail, so anything the model judged impossible came back failing.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "engine.py").read_text(encoding="utf-8", errors="replace")
        block = cls.src.split("free_will_header = (", 1)[1] \
                       .split("\n            )", 1)[0]
        # The PROMPT only, as the MODEL receives it. Two things get in the way
        # of reading it off the source: the comment above the fix quotes the
        # old wording it replaced, and a sentence long enough to matter is
        # split across adjacent string literals, so searching the raw file for
        # it finds nothing however present it is.
        import re
        prompt = "\n".join(ln for ln in block.splitlines()
                           if not ln.strip().startswith("#"))
        cls.header = re.sub(r'"\s*\n\s*"', "", prompt)

    def test_the_block_no_longer_asks_for_an_attempt(self):
        self.assertNotIn("Show the ATTEMPT", self.header)
        self.assertIn("The action HAPPENS", self.header)

    def test_it_names_the_exact_phrasings_that_came_back(self):
        low = self.header.lower()
        for banned in ("you try to", "you attempt to", "you start to", "remains"):
            with self.subTest(phrase=banned):
                self.assertIn(banned, low)

    def test_an_impossible_action_is_made_possible_not_denied(self):
        """The precedent is the player's own: "get picked up by a superhero"
        was honoured — a figure in a kinetic suit hauled them into the air —
        while "fly to antarctica" was refused. Same kind of ask, opposite
        answers. The world supplies the means and charges for it."""
        low = self.header.lower()
        self.assertIn("the world supplies the means", low)
        self.assertIn("charge for it", low)
        self.assertIn("refusing outright is the one answer that is always wrong",
                      low)

    def test_the_consequence_prompt_still_agrees_with_it(self):
        """Both halves have to say the same thing, or whichever sits closer to
        the action wins — which is exactly how this bug worked."""
        prompts = json.loads(
            (ROOT / "prompts" / "simulation_prompts.json").read_text(
                encoding="utf-8"))
        rules = prompts["action_consequence_instructions"]
        self.assertIn('NO "you try but fail."', rules)
        self.assertIn("The player's action HAPPENS", rules)


class TestAFightNeverStrandsThePlayerInATypedAction(unittest.TestCase):
    """Reported as "getting stuck in custom action after encounter".

    The WHEEL's typed-action box (`#custom-input`, opened by the Custom row) is
    a different instrument from the Moment's own prompt bar, and nothing used to
    close it when a fight took the screen. A rolled encounter can interrupt any
    turn, so it can land while somebody is mid-sentence — and on the way out the
    release sets `state.processing` for the aftermath turn. That left every road
    shut at once: Enter did nothing (submitCustomAction returned on
    state.processing, silently), and SCAN stayed disabled because
    `state.freeWillOpen` was never cleared.
    """

    def _encounter_start(self):
        return CLIENT_JS.split("async function start(opts) {", 1)[1] \
                        .split("\n    async function ", 1)[0]

    def test_opening_a_fight_closes_the_typed_action_box(self):
        self.assertIn("closeFreeWill(true)", self._encounter_start())

    def test_it_closes_before_the_fight_takes_the_screen(self):
        """After the guards — a fight that does not start must not clear the
        box out from under a player who is still writing into a live world."""
        fn = self._encounter_start()
        before, after = fn.split("closeFreeWill(true)", 1)
        self.assertIn("if (active || resolving || state.processing", before,
                      "the box is closed before start() has decided to run")
        self.assertIn("Moments.push", after,
                      "the box is still open when the Moment takes the screen")

    def test_a_submit_that_cannot_land_says_so(self):
        """Silence is what made a busy pipeline read as a dead key."""
        fn = CLIENT_JS.split("function submitCustomAction(e) {", 1)[1] \
                      .split("\n  // ----", 1)[0]
        gate = fn.split("if (state.processing) {", 1)
        self.assertEqual(len(gate), 2,
                         "submitCustomAction still returns silently when busy")
        self.assertIn("showRendererToast", gate[1].split("}", 1)[0])

    def test_the_line_they_wrote_is_not_thrown_away(self):
        """The turn lands in seconds; retyping it is a punishment for timing."""
        fn = CLIENT_JS.split("function submitCustomAction(e) {", 1)[1] \
                      .split("\n  // ----", 1)[0]
        busy = fn.split("if (state.processing) {", 1)[1].split("}", 1)[0]
        self.assertNotIn("customInput.value", busy)
        self.assertNotIn("closeFreeWill", busy)


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


class TestTheSlateSaysWhatYouAreAboutToDo(unittest.TestCase):
    """The rows used to read attack / flee / reason — the LANE, one word each.

    That was right while a fight ran four rounds: a vivid line the picture
    cannot honour is a broken promise, and it had to survive being re-read every
    round against a plate that had not moved. A fight is one exchange now, a
    committed verb usually ends it, and the resolve plate is generated FROM that
    verb — so the picture has to honour the line exactly once. Three identical
    words every fight was the least dramatic thing on screen, and the model had
    already written something far better underneath them.
    """

    def _show_choices(self):
        return CLIENT_JS.split("function showChoices(", 1)[1] \
                        .split("\n    function ", 1)[0]

    def test_the_row_reads_the_written_verb(self):
        fn = self._show_choices()
        self.assertIn("label: text || laneWord(lane, idx)", fn,
                      "the slate is still showing the lane instead of the verb")

    def test_the_lane_still_rides_along(self):
        """It is what the server rolls against: the verb tells you what you are
        doing, not the odds you are accepting."""
        self.assertIn("laneWord: laneWord(lane, idx)", self._show_choices())

    def test_the_eyebrow_is_readable_by_attr(self):
        """`content: attr(data-lane)` only reads the pseudo-element's OWN
        element. With the attribute on the button alone the rule matched,
        computed at the right size and colour, and drew an empty string."""
        self.assertIn("body.dataset.lane = eyebrow", MOMENTS_JS)
        self.assertIn(".moment-choice-text[data-lane]::before", CSS)
        self.assertIn("content: attr(data-lane)", CSS)

    def test_a_sentence_is_not_set_like_a_label(self):
        """Uppercase at 0.26em tracking was right for one word. "CRUSH HIS
        THROAT WITH CAMERA" set that way is a shout that wraps."""
        row = CSS.split(
            "body.moment-encounter .moment-choice:not(.moment-choice-custom) "
            ".moment-choice-text {", 1)[1].split("}", 1)[0]
        self.assertIn("text-transform: none", row)
        self.assertNotIn("text-transform: uppercase", row)
        # ...and the label look moves to the eyebrow, where one word belongs.
        brow = CSS.split(".moment-choice-text[data-lane]::before {", 1)[1] \
                  .split("}", 1)[0]
        self.assertIn("text-transform: uppercase", brow)


class TestItLooksLikeWatchAndNotLikeAMenu(unittest.TestCase):
    """"Displayed like the watch mechanic": typography over the frame."""

    def _encounter_slate_css(self):
        # Bounded by the rule that follows the slate. It used to be the
        # nameplate's SUB line; a confrontation draws no text now, so the
        # nameplate is hidden outright and that selector is gone.
        start = CSS.index("body.moment-encounter #moment-choices {")
        end = CSS.index("body.moment-encounter #moment-nameplate")
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
