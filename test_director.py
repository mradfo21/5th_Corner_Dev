"""Watch used to advance by clicking a random button.

Nobody is at the controls in Watch, so a coin toss decided what the episode
was about — and a third of the time the episode was about walking up to a
door and looking at it. The director picks instead. The things that matter:

* it never stalls a run, whatever the model does;
* it refuses the option that only looks at things;
* it does not take the same shape of action it just took;
* a model answer that is out of range or nonsense falls back rather than
  firing an index nobody offered.

Pure functions only — no network, no API key.

Run with:
    python -m unittest test_director -v
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")

import director


SLATE = [
    "Look through the gap in the fence",
    "Kick the generator over and run",
    "Wait for the lights to come back",
]


class TestTheDirectorAlwaysReturnsAPick(unittest.TestCase):
    """An unattended run must never sit waiting for a decision."""

    def test_a_model_that_raises_still_moves_the_run_on(self):
        fake = mock.Mock()
        fake._ask.side_effect = RuntimeError("no key")
        fake._load_history.return_value = []
        with mock.patch.dict("sys.modules", {"engine": fake}):
            out = director.pick(SLATE, "s")
        self.assertEqual(out["source"], "fallback")
        self.assertIn(out["index"], (0, 1, 2))

    def test_an_index_nobody_offered_is_not_taken(self):
        fake = mock.Mock()
        fake._ask.return_value = '{"index": 7, "why": "off the end"}'
        fake._load_history.return_value = []
        with mock.patch.dict("sys.modules", {"engine": fake}):
            out = director.pick(SLATE, "s")
        self.assertEqual(out["source"], "fallback")
        self.assertLess(out["index"], len(SLATE))

    def test_prose_instead_of_json_falls_back(self):
        fake = mock.Mock()
        fake._ask.return_value = "I think the second one, obviously."
        fake._load_history.return_value = []
        with mock.patch.dict("sys.modules", {"engine": fake}):
            out = director.pick(SLATE, "s")
        self.assertEqual(out["source"], "fallback")

    def test_an_empty_slate_says_so_rather_than_guessing(self):
        out = director.pick([], "s")
        self.assertEqual(out["index"], -1)

    def test_one_option_is_not_worth_a_model_call(self):
        fake = mock.Mock()
        fake._load_history.return_value = []
        with mock.patch.dict("sys.modules", {"engine": fake}):
            out = director.pick(["Open the hatch"], "s")
        self.assertEqual(out["index"], 0)
        fake._ask.assert_not_called()


class TestTheDirectorTakesTheModelsAnswer(unittest.TestCase):

    def test_a_clean_answer_is_used(self):
        fake = mock.Mock()
        fake._ask.return_value = '{"index": 1, "why": "commits, and costs him the light"}'
        fake._load_history.return_value = []
        with mock.patch.dict("sys.modules", {"engine": fake}):
            out = director.pick(SLATE, "s")
        self.assertEqual(out["index"], 1)
        self.assertEqual(out["source"], "llm")
        self.assertIn("commits", out["why"])

    def test_a_fenced_answer_is_still_an_answer(self):
        fake = mock.Mock()
        fake._ask.return_value = '```json\n{"index": 1, "why": "escalates"}\n```'
        fake._load_history.return_value = []
        with mock.patch.dict("sys.modules", {"engine": fake}):
            out = director.pick(SLATE, "s")
        self.assertEqual(out["index"], 1)
        self.assertEqual(out["source"], "llm")

    def test_the_options_and_the_recent_beats_reach_the_prompt(self):
        fake = mock.Mock()
        fake._ask.return_value = '{"index": 1, "why": ""}'
        fake._load_history.return_value = [
            {"choice": "Follow the tyre tracks"},
            {"choice": "Look through the window"},
        ]
        with mock.patch.dict("sys.modules", {"engine": fake}):
            director.pick(SLATE, "s")
        prompt = fake._ask.call_args[0][0]
        for text in SLATE:
            self.assertIn(text, prompt)
        self.assertIn("Follow the tyre tracks", prompt)


class TestTheFallbackIsBetterThanACoinToss(unittest.TestCase):
    """The whole complaint was that Watch kept choosing the boring one."""

    def test_standing_and_looking_is_never_the_pick(self):
        texts = ["Look around the yard", "Scan the treeline",
                 "Smash the padlock off the gate"]
        for _ in range(30):
            self.assertEqual(director.fallback_pick(texts), 2)

    def test_the_verb_just_used_is_avoided(self):
        texts = ["Climb the water tower", "Pry the hatch open"]
        for _ in range(30):
            self.assertEqual(
                director.fallback_pick(texts, ["Climb the ladder"]), 1)

    def test_a_slate_of_nothing_but_looking_still_picks_something(self):
        texts = ["Look left", "Look right"]
        self.assertIn(director.fallback_pick(texts), (0, 1))

    def test_an_empty_slate_picks_nothing(self):
        self.assertEqual(director.fallback_pick([]), -1)


class TestWhatCountsAsStandingStill(unittest.TestCase):

    def test_the_passive_verbs_are_caught(self):
        for text in ("Look at the sign", "Wait by the fence",
                     "Approach the man", "Scan for movement",
                     "Move toward the light"):
            self.assertTrue(director._is_passive(text), text)

    def test_committing_to_something_is_not_passive(self):
        for text in ("Kick the door in", "Hand over the tape",
                     "Throw the lamp at him", "Run for the treeline"):
            self.assertFalse(director._is_passive(text), text)


class TestARunStuckOnOneIdeaIsSteeredOffIt(unittest.TestCase):
    """Told to vary itself, the model picks the most violent option every
    single time and explains, every single time, that it forces a
    confrontation. So the variety is enforced rather than requested."""

    SLATE = ["Follow the man at a distance",
             "Grab the man by the collar",
             "Shout to warn the others"]

    def test_a_fourth_punch_is_not_offered_after_three(self):
        recent = ["Slam him into the fence", "Grab the woman by the collar",
                  "Smash the window with the bar"]
        offered = director.unstuck_indices(self.SLATE, recent)
        self.assertNotIn(1, offered)
        self.assertEqual(offered, [0, 2])

    def test_a_mixed_run_is_not_narrowed(self):
        recent = ["Smash the window", "Run for the gate", "Ask him his name"]
        self.assertEqual(director.unstuck_indices(self.SLATE, recent),
                         [0, 1, 2])

    def test_a_short_run_is_not_narrowed(self):
        self.assertEqual(
            director.unstuck_indices(self.SLATE, ["Smash the window"]),
            [0, 1, 2])

    def test_a_slate_with_no_alternative_keeps_everything(self):
        slate = ["Punch him", "Kick the door in"]
        recent = ["Smash it", "Slam him down", "Crack the glass"]
        self.assertEqual(director.unstuck_indices(slate, recent), [0, 1])

    def test_the_model_cannot_reach_past_what_it_was_offered(self):
        fake = mock.Mock()
        fake._ask.return_value = '{"index": 1, "why": "punch him again"}'
        fake._load_history.return_value = [
            {"choice": "Slam him into the fence"},
            {"choice": "Grab the woman by the collar"},
            {"choice": "Smash the window with the bar"},
        ]
        with mock.patch.dict("sys.modules", {"engine": fake}):
            out = director.pick(self.SLATE, "s")
        self.assertEqual(out["source"], "fallback")
        self.assertIn(out["index"], (0, 2))

    def test_only_the_offered_options_reach_the_prompt(self):
        fake = mock.Mock()
        fake._ask.return_value = '{"index": 2, "why": ""}'
        fake._load_history.return_value = [
            {"choice": "Slam him into the fence"},
            {"choice": "Grab the woman by the collar"},
            {"choice": "Smash the window with the bar"},
        ]
        with mock.patch.dict("sys.modules", {"engine": fake}):
            out = director.pick(self.SLATE, "s")
        prompt = fake._ask.call_args[0][0]
        self.assertNotIn("Grab the man by the collar", prompt)
        self.assertIn("Shout to warn the others", prompt)
        self.assertEqual(out["index"], 2)


class TestWhatKindOfBeatAnActionIs(unittest.TestCase):

    def test_the_shapes_are_told_apart(self):
        cases = {
            "Smash the padlock off the gate": "force",
            "Grab him by the collar": "force",
            "Run for the treeline": "flight",
            "Back away down the corridor": "flight",
            "Shout to warn the others": "talk",
            "Hand over the tape": "talk",
            "Light the flare": "handle",
            "Look through the window": "observe",
        }
        for text, want in cases.items():
            self.assertEqual(director.shape(text), want, text)

    def test_an_unrecognisable_action_is_not_forced_into_a_bucket(self):
        self.assertEqual(director.shape("Reconsider everything"), "other")

    def test_a_verb_hiding_inside_another_word_does_not_count(self):
        # "runway" is not running, "asked" is past tense of a talk verb but
        # "flask" must not read as one.
        self.assertEqual(director.shape("Cross the runway"), "other")


class TestReadingTheRunsRecentHistory(unittest.TestCase):

    def test_the_opening_beat_is_not_an_action(self):
        fake = mock.Mock()
        fake._load_history.return_value = [
            {"choice": "Intro"},
            {"choice": "Walk into the yard"},
        ]
        with mock.patch.dict("sys.modules", {"engine": fake}):
            self.assertEqual(director.recent_actions("s"), ["Walk into the yard"])

    def test_an_unreadable_history_is_not_fatal(self):
        fake = mock.Mock()
        fake._load_history.side_effect = OSError("gone")
        with mock.patch.dict("sys.modules", {"engine": fake}):
            self.assertEqual(director.recent_actions("s"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
