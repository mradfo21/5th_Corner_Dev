"""What jumps you should be a roll, not the model's favourite answer.

A play session produced the same encounter over and over: a rough man in work
clothes, then another one, in a world whose bible opens with military raids,
paranormal anomalies and miners the accident changed. Three separate causes,
one per class here:

* the instructions banned most of the world's own roster ("not a comic title,
  rank, or sci-fi class... people and flesh, never machines or energy weapons")
  and offered two examples, both of them a working man in coveralls;
* `kind` was clamped to person/creature/character, so a model that did answer
  "anomaly" had the answer rewritten to "person" before any prompt saw it;
* nothing was ever rolled. Asked the same question with the same bible every
  turn, a model returns its most probable answer every turn, and temperature
  does not move the mode.

So the KIND is now drawn in code from a roster read out of the run's own lore,
and the model only dresses the roll. These tests are about the dice.

Pure functions only - no network, no API key.

Run with:
    python -m unittest test_encounter_roster -v
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")

import encounter


LORE = (
    "The Four Corners, 1993. Horizon Metals ran Shaft 6 until the accident. "
    "The military now raids the region; quarantine sentries hold the fence "
    "line. Miners who breathed the dust came back changed. Activists and "
    "rival stringers chase the same story."
)

ROSTER = [
    "a Horizon quarantine sentry at the fence line",
    "a two-man military raid team sweeping the flats",
    "a miner the dust changed, still in his gear",
    "a pack animal gone wrong, following the smell",
    "a standing column of dust that does not move with the wind",
    "a rival stringer who wants the roll of film",
]


class _State:
    """Stands in for engine's per-session state file."""

    def __init__(self, **seed):
        self.st = dict(seed)

    def load(self, session_id="default"):
        return dict(self.st)

    def save(self, st, session_id="default"):
        self.st = dict(st)


def _patch_engine(state, ask=None):
    """Patch the engine surface encounter.py reaches for, and nothing else."""
    import engine
    patches = [
        mock.patch.object(engine, "_load_state", side_effect=state.load),
        mock.patch.object(engine, "_save_state", side_effect=state.save),
    ]
    if ask is not None:
        patches.append(mock.patch.object(engine, "_ask", side_effect=ask))
    return patches


class TestTheRosterComesOutOfTheLore(unittest.TestCase):
    """A table shipped in encounter.py would go stale the moment the bible
    changed. The list is read from the lore instead, so rewriting the lore
    rewrites what can hunt you."""

    def test_the_bible_is_what_gets_asked_about(self):
        asked = {}

        def ask(prompt, **kw):
            asked["prompt"] = prompt
            asked["kw"] = kw
            return {"kinds": ROSTER}

        state = _State()
        with mock.patch.object(encounter, "_clean_roster",
                               wraps=encounter._clean_roster):
            import experience_store
            with mock.patch.object(experience_store, "lore_brief",
                                   return_value=LORE):
                for p in _patch_engine(state, ask):
                    p.start()
                try:
                    got = encounter.build_encounter_roster("s1")
                finally:
                    mock.patch.stopall()

        self.assertEqual(got, ROSTER)
        self.assertIn("Shaft 6", asked["prompt"])
        self.assertIn("quarantine sentries", asked["prompt"])
        # The one call in the game whose whole job is to not repeat itself.
        self.assertGreaterEqual(asked["kw"].get("temp", 0), 1.0)

    def test_a_world_with_no_lore_rolls_nothing_and_breaks_nothing(self):
        # No bible, no world document: the brief must still work exactly as it
        # did before the roster existed rather than raising into a dead turn.
        state = _State()
        import experience_store
        with mock.patch.object(experience_store, "lore_brief", return_value=""):
            for p in _patch_engine(state, lambda *a, **k: {"kinds": ROSTER}):
                p.start()
            try:
                self.assertEqual(encounter.build_encounter_roster("s1"), [])
                self.assertEqual(encounter.roll_encounter_kind("s1"), "")
            finally:
                mock.patch.stopall()


class TestTheListIsUsable(unittest.TestCase):
    """Whatever shape the answer arrives in, it has to come out as a list of
    distinct short phrases."""

    def test_numbering_bullets_and_duplicates_are_dropped(self):
        got = encounter._clean_roster({"kinds": [
            "1. a quarantine sentry",
            "- a quarantine sentry",
            "\u2022 a raid team on the flats",
            "  ",
            "a raid team on the flats",
            "a changed miner",
        ]})
        self.assertEqual(got, ["a quarantine sentry",
                              "a raid team on the flats",
                              "a changed miner"])

    def test_a_plain_list_of_lines_still_parses(self):
        got = encounter._clean_roster(
            "a quarantine sentry\na changed miner\na rival stringer")
        self.assertEqual(len(got), 3)
        self.assertIn("a changed miner", got)

    def test_the_roster_is_capped(self):
        got = encounter._clean_roster(
            {"kinds": [f"threat number {i}" for i in range(40)]})
        self.assertEqual(len(got), encounter.ENCOUNTER_ROSTER_SIZE)


class TestTheSameThingDoesNotArriveTwiceRunning(unittest.TestCase):
    """The complaint was not that rednecks exist, it is that the SECOND
    encounter was another one. A cooldown is the whole fix."""

    def test_consecutive_rolls_differ(self):
        state = _State(encounter_roster=list(ROSTER))
        for p in _patch_engine(state):
            p.start()
        try:
            picks = [encounter.roll_encounter_kind("s1") for _ in range(4)]
        finally:
            mock.patch.stopall()
        self.assertEqual(len(set(picks)), len(picks), picks)
        for pick in picks:
            self.assertIn(pick, ROSTER)

    def test_a_short_roster_reopens_instead_of_dead_ending(self):
        # Two entries and a cooldown of five: once both are on cooldown the pool
        # has to reopen, or the roll returns nothing and the encounter is blank.
        state = _State(encounter_roster=["a sentry", "a raid team"])
        for p in _patch_engine(state):
            p.start()
        try:
            picks = [encounter.roll_encounter_kind("s1") for _ in range(6)]
        finally:
            mock.patch.stopall()
        self.assertTrue(all(picks), picks)
        self.assertEqual(set(picks), {"a sentry", "a raid team"})

    def test_a_cached_roster_is_not_rebuilt(self):
        state = _State(encounter_roster=list(ROSTER))
        calls = []

        def ask(prompt, **kw):
            calls.append(prompt)
            return {"kinds": ROSTER}

        for p in _patch_engine(state, ask):
            p.start()
        try:
            self.assertEqual(encounter.encounter_roster("s1"), ROSTER)
        finally:
            mock.patch.stopall()
        self.assertEqual(calls, [], "the roster is built once per run, not per turn")


class TestTheRollReachesTheBrief(unittest.TestCase):
    """A roll nothing reads is decoration."""

    def _prompt_for(self, rolled):
        seen = {}

        def ask(prompt, **kw):
            seen["prompt"] = prompt
            return {"character": {"label": "A sentry", "kind": "person",
                                  "look": "a figure in a dust mask",
                                  "stance": "hostile"},
                    "motive": "to turn you back from the fence",
                    "danger": "levelling a rifle at your chest",
                    "stakes": "You lose the film."}

        state = _State(world_prompt="open flats at dusk")
        import engine
        with mock.patch.object(encounter, "roll_encounter_kind",
                               return_value=rolled), \
             mock.patch.object(encounter, "encounter_lore_context",
                               return_value="WHAT THIS WORLD IS:\n" + LORE), \
             mock.patch.object(engine, "_load_state", side_effect=state.load), \
             mock.patch.object(engine, "_save_state", side_effect=state.save), \
             mock.patch.object(engine, "_ask", side_effect=ask):
            encounter.build_encounter_brief("s1", vision={})
        return seen.get("prompt", "")

    def test_the_rolled_kind_is_stated_as_the_answer(self):
        got = self._prompt_for("a standing column of dust that does not move")
        self.assertIn("THIS ENCOUNTER IS: a standing column of dust", got)
        self.assertIn("not a suggestion", got)

    def test_no_roll_means_the_brief_reads_as_it_always_did(self):
        got = self._prompt_for("")
        self.assertNotIn("THIS ENCOUNTER IS", got)
        self.assertIn("Return one JSON object", got)

    def test_the_brief_no_longer_bans_the_worlds_own_roster(self):
        got = self._prompt_for("a two-man military raid team")
        low = got.lower()
        for banned in ("do not invent a rank",
                       "never machines or energy weapons",
                       "no sentinel, warden, knight"):
            self.assertNotIn(banned, low, banned)


class TestAnAnomalyIsAllowedToBeOne(unittest.TestCase):
    """`kind` was clamped to three values, and anything else silently became
    "person" — so even a model that answered correctly produced a man."""

    def test_group_and_anomaly_survive_normalisation(self):
        for kind in ("group", "anomaly", "creature", "person"):
            brief = encounter.normalize_encounter_brief({
                "character": {"label": "The dust column", "kind": kind,
                              "look": "a standing shape of grit and static",
                              "stance": "hostile"},
                "danger": "it is already leaning over you",
                "stakes": "You lose the light.",
            })
            self.assertEqual(brief["character"]["kind"], kind)

    def test_a_nonsense_kind_still_falls_back(self):
        brief = encounter.normalize_encounter_brief({
            "character": {"label": "X", "kind": "spaceship",
                          "look": "a hull", "stance": "hostile"},
            "danger": "d", "stakes": "s.",
        })
        self.assertEqual(brief["character"]["kind"], "person")

    def test_a_soldier_reads_as_a_body(self):
        # Every "is this describing somebody?" check runs off _PERSON_NOUNS, and
        # a noun missing there means the description is discarded as camera talk.
        for noun in ("soldier", "animal", "shape", "team"):
            self.assertTrue(
                encounter._first_person_noun(f"a {noun} at the fence line"),
                noun)


class TestTheFallbackIsNotAlwaysAStranger(unittest.TestCase):
    """When the brief call fails, the dice have usually already spoken."""

    def test_the_roll_names_the_canned_brief(self):
        got = encounter.fallback_encounter_brief(
            "open flats, dusk", seed="s1", rolled="a quarantine sentry")
        self.assertEqual(got["character"]["label"], "a quarantine sentry")

    def test_without_a_roll_nothing_changes(self):
        got = encounter.fallback_encounter_brief("open flats, dusk", seed="s1")
        self.assertIn(got["character"]["label"],
                      [b["character"]["label"] for b in encounter._FALLBACK_BRIEFS])


if __name__ == "__main__":
    unittest.main(verbosity=2)
