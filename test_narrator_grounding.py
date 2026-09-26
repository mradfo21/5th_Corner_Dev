"""
test_narrator_grounding.py — what the narrator is actually told before it speaks.

The complaint this exists for: the narrator's lines read as repetitive and
unresponsive to the scene. Both causes were in the inputs, not the model.

  * `{scene}` was the whole `world_prompt` — a 1200-1500 word document about the
    world at large, slow-moving at the top, pasted under the label "CURRENT
    SCENE" with no clip. The narrator was handed a wall of background and almost
    nothing about the frame in front of the player, and that bulk drowned the
    recent beats underneath it. Near-identical inputs produce near-identical
    lines.
  * The shipped direction commanded the CONTENT of every line: "say how you FEEL
    — afraid, uneasy, but determined" and "make it clear you have to find out
    what happened here". Every unfocused narration was ordered onto the same
    beat. That is authored repetition, not model drift.
  * Nothing ever told the narrator what it had already said, so "don't repeat
    yourself" was a hope rather than an instruction.

These tests pin the inputs, which is where the defect lived. Whether a given
line is good is a judgment no test can make; whether the line was written from
the current frame, the current state, and a list of what not to say again, is
exactly what a test can make.

No network: `engine._ask` is stubbed, so the prompt is captured rather than sent.

Run with:
    python3 -m unittest test_narrator_grounding -v
"""

import json
import os
import re
import shutil
import unittest
from pathlib import Path

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("ANTHROPIC_API_KEY", "")

import engine

ROOT = Path(__file__).parent.resolve()
SESSION_ID = "narrator-test"

ON_SCREEN = (
    "A flooded stairwell descending into black water, handrail sheared off, "
    "emergency light strobing red on the wet concrete."
)
# The tail marker sits far past any sane clip: if it reaches the prompt, the
# whole document is being pasted in again.
WORLD_DOC = (
    "THE WORLD: a decommissioned research station in the high desert. " * 60
) + "TAIL_OF_THE_WORLD_DOCUMENT"


def _discard_test_session():
    shutil.rmtree(engine._get_session_root(SESSION_ID), ignore_errors=True)


class NarratorPromptCase(unittest.TestCase):
    """Captures the prompt `_narrator_script` builds instead of sending it."""

    def setUp(self):
        engine.LLM_ENABLED = True
        self._real_ask = engine._ask
        self.prompts = []

        def fake_ask(prompt, **kwargs):
            self.prompts.append(prompt)
            return "The water has reached the third step since I last looked."

        engine._ask = fake_ask
        _discard_test_session()

    def tearDown(self):
        engine._ask = self._real_ask
        _discard_test_session()

    def seed(self, **overrides):
        st = engine._load_state(SESSION_ID)
        st.update({
            "world_prompt": WORLD_DOC,
            "turn_count": 4,
            "current_phase": "normal",
            "player_state": {"alive": True},
            "feed_log": [],
        })
        st.update(overrides)
        engine._save_state(st, SESSION_ID)
        return st

    def narrate(self, focus="", multi=False):
        engine._narrator_script(focus, multi, SESSION_ID)
        self.assertTrue(self.prompts, "the narrator asked the model for nothing")
        return self.prompts[-1]


class TestSceneIsWhatIsOnScreen(NarratorPromptCase):

    def test_prefers_the_vision_of_the_rendered_frame(self):
        self.seed(current_observed_vision=ON_SCREEN,
                  current_image_prompt="a corridor of humming pipes")
        p = self.narrate()
        self.assertIn("flooded stairwell", p)
        self.assertNotIn("humming pipes", p)

    def test_falls_back_to_the_prompt_the_frame_was_drawn_from(self):
        self.seed(current_image_prompt=ON_SCREEN)
        self.assertIn("flooded stairwell", self.narrate())

    def test_falls_back_to_the_world_document_last(self):
        # Still better than nothing on turn one, before any frame has rendered.
        self.seed()
        self.assertIn("research station", self.narrate())

    def test_the_world_document_no_longer_floods_the_prompt(self):
        # The whole point: 1200+ words of background used to be pasted in whole.
        self.seed()
        self.assertNotIn("TAIL_OF_THE_WORLD_DOCUMENT", self.narrate(),
                         "the world document is being pasted in unclipped again")

    def test_the_scene_is_clipped_even_when_it_is_long(self):
        self.seed(current_observed_vision="x" * 5000)
        self.assertNotIn("x" * 500, self.narrate())


class TestTheLiveStateReachesTheLine(NarratorPromptCase):
    """The signals that actually differ turn to turn. Without them the only
    thing separating two narrations is model temperature."""

    def test_time_of_day_is_included(self):
        self.seed(current_observed_vision=ON_SCREEN,
                  time_of_day="3:40am | weather: freezing fog")
        self.assertIn("freezing fog", self.narrate())

    def test_an_escalating_phase_is_named(self):
        self.seed(current_observed_vision=ON_SCREEN, current_phase="critical")
        self.assertIn("critical", self.narrate())

    def test_a_normal_phase_is_not_worth_saying(self):
        self.seed(current_observed_vision=ON_SCREEN, current_phase="normal")
        self.assertNotIn("the situation is normal", self.narrate())

    def test_being_hunted_is_carried(self):
        """This used to assert the tracked injury list reached the line. That
        list is gone with the wound parser; being hunted is the live stake now,
        and the narrator gets the wound prose from RECENT EVENTS regardless."""
        self.seed(current_observed_vision=ON_SCREEN,
                  detection={"heat": 9, "level": 3, "since_turn": 1})
        self.assertIn("hunted", self.narrate())

    def test_staying_hidden_is_not_worth_saying(self):
        self.seed(current_observed_vision=ON_SCREEN,
                  detection={"heat": 0, "level": 0, "since_turn": 0})
        self.assertNotIn("you are hidden", self.narrate())

    def test_the_action_just_taken_reaches_the_line(self):
        self.seed(current_observed_vision=ON_SCREEN,
                  last_choice="Move to the rusted truck")
        p = self.narrate()
        self.assertIn("Move to the rusted truck", p)
        self.assertIn("WHAT YOU JUST DID", p)

    def test_acted_on_the_request_beats_stale_last_choice(self):
        # MOVE TO narrates on the click, before /api/choose writes last_choice.
        self.seed(current_observed_vision=ON_SCREEN,
                  last_choice="Look at the vent")
        engine._narrator_script(
            "The player has just committed to travel to the rusted truck.",
            False, SESSION_ID, acted="Move to the rusted truck",
        )
        p = self.prompts[-1]
        self.assertIn("Move to the rusted truck", p)
        self.assertIn("WHERE THEY WERE LEAVING", p)
        self.assertNotIn("Look at the vent", p)


class TestTheNarratorKnowsHowYouActed(NarratorPromptCase):
    """The narrator is supposed to be narrating YOUR story, and it could not.

    Every route into the game — a curated choice, a hotspot you walked to, a
    hotspot you opened, a person you spoke to, a sentence you typed yourself
    — arrived as one anonymous string of choice text. A voice that cannot
    tell those apart answers all of them the same way, which is what made it
    read as talking over the game rather than about it."""

    def deed(self, kind, target, acted="Do the thing"):
        engine._narrator_script("", False, SESSION_ID, acted=acted,
                                deed_kind=kind, deed_target=target)
        return self.prompts[-1]

    def test_walking_toward_a_thing_reads_as_walking_toward_it(self):
        self.seed(current_observed_vision=ON_SCREEN)
        p = self.deed("move", "the rusted truck")
        self.assertIn("You walked toward the rusted truck.", p)

    def test_handling_a_thing_is_not_the_same_event_as_choosing_one(self):
        self.seed(current_observed_vision=ON_SCREEN)
        self.assertIn("You put your hands on the drum.",
                      self.deed("interact", "the drum"))
        self.assertIn("You took one of the options in front of you.",
                      self.deed("choice", ""))

    def test_your_own_words_are_marked_as_your_own(self):
        self.seed(current_observed_vision=ON_SCREEN)
        p = self.deed("custom", "", acted="pry the hatch open with the bar")
        self.assertIn("You did something nobody offered you.", p)
        self.assertIn("pry the hatch open with the bar", p)

    def test_speaking_to_someone_is_a_deed_too(self):
        self.seed(current_observed_vision=ON_SCREEN)
        self.assertIn("You spoke to the warden.",
                      self.deed("talk", "the warden"))

    def test_the_deed_is_addressed_to_the_voice_living_it(self):
        """The brief already casts the narrator as the one doing this ("You
        are {self}, speaking into a tape"). Written in the third person —
        "They walked toward the pond" — the model took the hint and started
        addressing the player as "you", which is a different narrator in a
        different game from the one the VOICE line describes."""
        self.seed(current_observed_vision=ON_SCREEN)
        p = self.deed("move", "the rusted truck")
        self.assertNotIn("They walked", p)

    def test_the_deed_comes_before_the_frame(self):
        """A brief buries whatever it puts last, and the frame is the thing
        the narrator must NOT be describing."""
        self.seed(current_observed_vision=ON_SCREEN)
        p = self.deed("move", "the rusted truck")
        self.assertLess(p.index("WHAT YOU JUST DID"),
                        p.index("flooded stairwell"))

    def test_the_frame_is_labelled_as_something_not_to_describe(self):
        self.seed(current_observed_vision=ON_SCREEN)
        self.assertIn("do not describe it back to them", self.deed("move", "x"))

    def test_an_unknown_kind_still_carries_the_words(self):
        """The client can always add a route the server has not met yet; that
        must degrade to the verbatim action, never to a crash or an empty
        block."""
        self.seed(current_observed_vision=ON_SCREEN)
        p = self.deed("teleported", "", acted="Step through the gate")
        self.assertIn("Step through the gate", p)
        self.assertIn("WHAT YOU JUST DID", p)


class TestTheBeatsAreToldNotInferred(NarratorPromptCase):
    """"Narrator now repeating lines changing just a few words."

    Two things caused that and only one of them was the model.

    The brief asked the narrator to read ALREADY SAID THIS RUN and work out
    which half of FACT / QUESTION it did last, which makes every line depend
    on it classifying its own previous output — and it does not: a measured
    run gave four questions in a row, three of them the same Horizon
    paraphrase reworded. The server knows how many lines have gone by, so it
    picks and states the shapes.

    The deeper cause was that there were only ever TWO shapes, over one finite
    pool: the brief says "ONE LINE OF HISTORY, AND NOTHING ELSE", reached out
    of a background document a few thousand characters long. Once the obvious
    facts are spent, rewording is the only move left. Alternating harder
    cannot fix a shortage of shapes, so a narration is two beats now and the
    PAIR rotates.
    """

    def test_a_narration_is_two_beats(self):
        self.seed(current_observed_vision=ON_SCREEN, narrator_recent=[])
        p = self.narrate()
        self.assertIn("THIS NARRATION IS TWO BEATS", p)
        self.assertIn("BEAT ONE", p)
        self.assertIn("BEAT TWO", p)

    def test_the_shape_outranks_the_one_sentence_rule_above_it(self):
        """The authored brief ends on "ONE short sentence". Anything that means
        to change the shape has to say it does — the radio-play format block
        makes the same move for the same reason."""
        self.seed(current_observed_vision=ON_SCREEN, narrator_recent=[])
        self.assertIn("replaces any one-sentence or one-line rule above",
                      self.narrate())

    def test_the_first_narration_of_a_run_opens_on_a_fact(self):
        """History is still the register; it is no longer the only shape."""
        self.seed(current_observed_vision=ON_SCREEN, narrator_recent=[])
        self.assertIn("BEAT ONE — FACT", self.narrate())

    def test_the_pair_changes_every_narration(self):
        """The whole complaint, as a property: no two consecutive narrations
        may be asked for the same two shapes."""
        seen = []
        for n in range(len(engine._BEAT_CYCLE) + 1):
            self.prompts = []
            self.seed(current_observed_vision=ON_SCREEN, narrator_beat=n,
                      narrator_recent=[f"Line number {i}." for i in range(n)])
            p = self.narrate()
            pair = re.findall(r"BEAT (?:ONE|TWO) — ([A-Z]+):", p)
            self.assertEqual(len(pair), 2, p)
            seen.append(tuple(pair))
        for a, b in zip(seen, seen[1:]):
            self.assertNotEqual(a, b, f"two narrations running asked for {a}")

    def test_the_rotation_keeps_turning_past_the_memory_cap(self):
        """The bug that made the whole rotation look like it did not work.

        `narrator_recent` is capped at NARRATOR_MEMORY, so its length stops
        growing — and the index came off that length. With a memory of six and
        a cycle of six it pinned at `6 % 6 == 0` from the seventh narration on,
        so every later line in a run got FACT then QUESTION. Measured live:
        eight narrations running, all the same shape, on a session that had
        already spoken more than six times."""
        full = [f"Line number {i}." for i in range(engine.NARRATOR_MEMORY)]
        seen = []
        for beat in range(engine.NARRATOR_MEMORY,
                          engine.NARRATOR_MEMORY + len(engine._BEAT_CYCLE)):
            d = engine._beat_directive(full, "", spoken_count=beat)
            seen.append(tuple(re.findall(r"BEAT (?:ONE|TWO) — ([A-Z]+):", d)))
        self.assertEqual(len(set(seen)), len(engine._BEAT_CYCLE),
                         "the rotation stopped turning once memory was full")

    def test_the_counter_is_kept_apart_from_the_capped_list(self):
        sid = "narrbeatcount"
        try:
            for n in (1, 2, 3):
                engine._remember_narration(
                    [{"character": "narrator", "text": f"Line {n}."}], sid)
                st = engine._load_state(sid)
                self.assertEqual(st.get("narrator_beat"), n)
        finally:
            shutil.rmtree(engine._get_session_root(sid), ignore_errors=True)

    def test_no_shape_opens_two_narrations_running(self):
        openers = [pair[0] for pair in engine._BEAT_CYCLE]
        for a, b in zip(openers, openers[1:] + openers[:1]):
            self.assertNotEqual(a, b, f"{a} opens twice in a row")

    def test_the_second_beat_turns_on_the_first(self):
        self.seed(current_observed_vision=ON_SCREEN, narrator_recent=[])
        p = self.narrate()
        self.assertIn("The second turns on the first", p)
        self.assertIn("does not restate it", p)

    def test_the_voice_is_his_own_thinking(self):
        """"it needs to be personalized, like his own internal thinking." """
        self.seed(current_observed_vision=ON_SCREEN, narrator_recent=[])
        self.assertIn("This is you thinking, not a report being read",
                      self.narrate())

    def test_the_last_opening_is_named_so_a_stale_shape_is_caught(self):
        """The repeats all began the same way. "Do not repeat yourself" never
        catches a fresh sentence with a reused shape; naming the words does."""
        self.seed(current_observed_vision=ON_SCREEN,
                  narrator_recent=["Why would Horizon keep the site open?"])
        self.assertIn('Your last line began "Why would Horizon"', self.narrate())

    def test_it_is_the_last_thing_the_model_reads(self):
        """`{avoid}` sits near the top of the brief, above the long WHAT YOU
        SAY / HOW YOU SAY IT sections — and those end on "ONE LINE OF
        HISTORY", which is emphatic and, being last, wins. Putting the beat
        shapes up there produced three facts in a row."""
        self.seed(current_observed_vision=ON_SCREEN,
                  narrator_recent=["Horizon signed the permits in 1974."])
        p = self.narrate()
        self.assertGreater(p.index("THIS NARRATION IS TWO BEATS"),
                           p.index("ONE LINE OF HISTORY"))
        self.assertLess(p.rstrip().rindex("BEAT TWO"), len(p.rstrip()) - 1)

    def test_it_is_pure_and_safe_on_junk(self):
        for spoken in ([], ["A flat statement."], [""], None,
                       ["x"] * (len(engine._BEAT_CYCLE) * 3)):
            with self.subTest(spoken=spoken):
                self.assertIn("BEAT ONE", engine._beat_directive(spoken or []))


class TestTheGoalIsMentionedOccasionally(NarratorPromptCase):
    """"it needs to reference the goal occasionally."

    The level sheet has a "What you're here for" field and the narrator never
    saw it: `place_summary()` leaves the goal out by design, so the one voice
    speaking the player's own thoughts was the one with no idea what it came
    for. Occasionally is the point — a voice that names the objective every
    line is the "never summarise the mission" failure the brief already bans.
    """

    GOAL = "The pump house with the red door, where the manifests are bolted to the wall."

    def _pairs_over_a_cycle(self):
        return [engine._BEAT_CYCLE[i % len(engine._BEAT_CYCLE)]
                for i in range(len(engine._BEAT_CYCLE))]

    def test_the_goal_comes_round_once_a_cycle(self):
        with_goal = [p for p in self._pairs_over_a_cycle() if "GOAL" in p]
        self.assertEqual(len(with_goal), 1,
                         "the goal should be occasional, not every line")

    def test_the_goal_text_rides_along_on_that_narration(self):
        idx = next(i for i, p in enumerate(engine._BEAT_CYCLE) if "GOAL" in p)
        d = engine._beat_directive([f"Line {i}." for i in range(idx)], self.GOAL)
        self.assertIn("GOAL", d)
        self.assertIn("WHAT YOU CAME HERE FOR", d)
        self.assertIn("pump house", d)

    def test_it_is_never_asked_for_as_a_mission_recap(self):
        idx = next(i for i, p in enumerate(engine._BEAT_CYCLE) if "GOAL" in p)
        d = engine._beat_directive([f"Line {i}." for i in range(idx)], self.GOAL)
        self.assertIn("Not the mission recited", d)

    def test_a_level_with_no_goal_spends_the_slot_on_something_else(self):
        """Asking him to measure against nothing wastes a turn of the cycle."""
        idx = next(i for i, p in enumerate(engine._BEAT_CYCLE) if "GOAL" in p)
        d = engine._beat_directive([f"Line {i}." for i in range(idx)], "")
        self.assertNotIn("BEAT ONE — GOAL", d)
        self.assertNotIn("BEAT TWO — GOAL", d)
        self.assertNotIn("WHAT YOU CAME HERE FOR", d)
        self.assertIn("BEAT ONE", d)

    def test_the_goal_is_read_off_the_level_sheet(self):
        src = (ROOT / "engine.py").read_text(encoding="utf-8")
        fn = src.split("def _narrator_goal(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('.get("goal")', fn)
        self.assertIn("setting_authored", fn)


class TestARewordIsARepeat(NarratorPromptCase):
    """"changing just a few words" is the failure a paraphrase ban misses.

    Showing the model its last four lines and saying "or any paraphrase of
    them" is what already shipped, and it still produced three lines running
    about Horizon signing permits. A fresh sentence about the same subject
    passes every check that looks at sentences, so this one looks at subjects.
    """

    def test_a_noun_used_once_is_the_scene_and_is_left_alone(self):
        """The fence and the mesa are supposed to recur — they are the place."""
        self.assertEqual(
            engine._narration_rut_words(["The fence was cut here."]), [])

    def test_a_noun_used_twice_is_a_rut_and_is_named(self):
        rut = engine._narration_rut_words([
            "Horizon signed the permits in 1974.",
            "Why did Horizon keep signing them?",
        ])
        self.assertIn("horizon", rut)
        self.assertIn("signed", rut + ["signed"])  # stem may differ; horizon is the tell

    def test_common_words_are_not_evidence_of_anything(self):
        rut = engine._narration_rut_words([
            "They were here before that.",
            "There was something here after that.",
        ])
        for junk in ("there", "were", "something", "before", "after", "that"):
            self.assertNotIn(junk, rut)

    def test_the_rut_reaches_the_prompt_as_a_ban(self):
        self.seed(current_observed_vision=ON_SCREEN, narrator_recent=[
            "Horizon signed the permits in 1974.",
            "Why did Horizon keep signing them?",
        ])
        p = self.narrate()
        self.assertIn("already built more than one line on", p)
        self.assertIn("horizon", p)

    def test_nothing_repeated_yet_adds_no_ban(self):
        self.seed(current_observed_vision=ON_SCREEN,
                  narrator_recent=["The fence was cut here."])
        self.assertNotIn("already built more than one line on", self.narrate())

    def test_it_is_bounded(self):
        many = [f"Alpha bravo charlie delta echo foxtrot golf hotel {i}."
                for i in range(8)]
        self.assertLessEqual(len(engine._narration_rut_words(many)), 6)


class TestThePairedNarrationIsNotOneLineTwice(unittest.TestCase):
    """MOVE TO asks for two narrations in ONE request (`follow_focus`), so the
    per-IP rate limit is not hit twice.

    Both were generated from the same `narrator_recent` snapshot and only
    remembered afterwards — so every guard that reads that snapshot (the beat
    rotation, the forbidden opening, the rut words) gave the second line the
    identical answer it gave the first. The likeliest pair in the game to come
    back as one sentence said twice, on the game's most frequent verb.
    """

    SRC = (ROOT / "engine.py").read_text(encoding="utf-8")
    CLIENT = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")

    def _worldbuild(self):
        return self.SRC.split("def api_narrator_worldbuild(", 1)[1] \
                       .split("\ndef ", 1)[0]

    def test_the_first_line_is_remembered_before_the_second_is_asked_for(self):
        body = self._worldbuild()
        remember = body.index("_remember_narration(script, session_id)")
        follow = body.index("follow_script = _narrator_script(")
        self.assertLess(remember, follow)

    def test_the_second_line_is_remembered_too(self):
        self.assertIn("_remember_narration(follow_script, session_id)",
                      self._worldbuild())

    def test_the_client_no_longer_dictates_the_shapes(self):
        """MOVE TO is the main verb, so pinning it to "the fact, then the
        question" pinned the most frequent narration in the game to the one
        pairing the voice already overused."""
        self.assertNotIn("the fact, then the question", self.CLIENT)
        self.assertNotIn("This one is the QUESTION hanging off", self.CLIENT)

    def test_but_it_still_says_when_each_line_lands(self):
        self.assertIn("over the fade to black", self.CLIENT)
        self.assertIn("A beat after the line before it", self.CLIENT)

    def test_and_still_bans_the_invented_backstory(self):
        """The invented dead brother came from this focus asking for "a guilt,
        a debt, a person they lost". The ban stays; only the shape moved."""
        self.assertIn("INVENT NOTHING about yourself", self.CLIENT)


class TestDoNotRepeatYourself(NarratorPromptCase):
    """Shown as OPENINGS, not as whole lines.

    Four complete sentences under a "do not repeat" heading are four examples,
    and few-shot pull beats a negative instruction. Measured on the same model
    and prompt: a fresh session gave six different beat shapes over six
    narrations; a session whose remembered lines were all fact-then-question
    gave eight fact-then-questions running, obeying the ban on the words while
    copying the shape it was being shown. A stem still catches a literal
    restart, and it is not a form anything can be modelled on.
    """

    def test_what_was_said_is_shown_to_the_model(self):
        self.seed(current_observed_vision=ON_SCREEN,
                  narrator_recent=["I have to find out what happened here."])
        p = self.narrate()
        self.assertIn("I have to find out", p)
        self.assertIn("do not open this way again", p.lower())

    def test_it_is_a_stem_and_not_a_copyable_line(self):
        self.seed(current_observed_vision=ON_SCREEN,
                  narrator_recent=["I have to find out what happened here."])
        p = self.narrate()
        self.assertNotIn("I have to find out what happened here.", p)
        self.assertIn("…", p)

    def test_nothing_about_their_shape_is_a_template(self):
        self.seed(current_observed_vision=ON_SCREEN,
                  narrator_recent=["The dark is thicker down here."])
        self.assertIn("Nothing about their shape is a template", self.narrate())

    def test_a_short_line_needs_no_ellipsis(self):
        self.seed(current_observed_vision=ON_SCREEN,
                  narrator_recent=["Nobody came back."])
        p = self.narrate()
        self.assertIn("Nobody came back.", p)
        self.assertNotIn("Nobody came back.…", p)

    def test_nothing_spoken_yet_adds_no_block(self):
        self.seed(current_observed_vision=ON_SCREEN)
        self.assertNotIn("do not repeat", self.narrate().lower())

    def test_the_avoid_list_reaches_the_focused_line_too(self):
        # The MOVE TO bridge is the most repeated line in the game — it fires on
        # every travel beat, so it is the one that most needs this.
        self.seed(current_observed_vision=ON_SCREEN,
                  narrator_recent=["The dark is thicker down here."])
        p = self.narrate(focus="Say one line about leaving for the water tower.")
        self.assertIn("The dark is thicker down here.", p)

    def test_the_avoid_list_reaches_the_multi_voice_script(self):
        self.seed(current_observed_vision=ON_SCREEN,
                  narrator_recent=["Something moved in the water."])
        p = self.narrate(multi=True)
        self.assertIn("Something moved in the water.", p)


class TestOnlyTheSpokenLineIsSpoken(unittest.TestCase):
    """Asked for one sentence of dialogue, a model hands back a screenplay.

    Observed against the live model: a sound cue on its own line ("*Click.*")
    and the action echoed as a heading above the real line. Both reached the
    voice, and because the clip keeps the FIRST sentence, the narrator's
    entire contribution to that turn was the word "Click"."""

    def test_a_sound_cue_is_not_a_line(self):
        out = engine._strip_narration_staging(
            "*Click.*\n\nThe lid groaned like dry bone.")
        self.assertEqual(out, "The lid groaned like dry bone.")

    def test_a_bracketed_stage_direction_goes_too(self):
        for cue in ("[static]", "(a door closes somewhere)", "_thud_"):
            out = engine._strip_narration_staging(cue + "\n\nNobody came back.")
            self.assertEqual(out, "Nobody came back.", f"{cue!r} survived")

    def test_the_action_echoed_as_a_heading_is_dropped(self):
        out = engine._strip_narration_staging(
            "Spoke with the miner.\n\nHis teeth were the same rusted iron.",
            acted="Spoke with the miner")
        self.assertEqual(out, "His teeth were the same rusted iron.")

    def test_an_echo_with_nothing_after_it_is_kept(self):
        """A weak line still beats no line."""
        out = engine._strip_narration_staging("Spoke with the miner.",
                                              acted="Spoke with the miner")
        self.assertEqual(out, "Spoke with the miner.")

    def test_emphasis_markers_never_reach_the_voice(self):
        """TTS reads them out as asterisks."""
        self.assertEqual(
            engine._strip_narration_staging("The water was *still* warm."),
            "The water was still warm.")

    def test_an_ordinary_line_is_left_alone(self):
        line = "The water in this pond is too still to be salt."
        self.assertEqual(engine._strip_narration_staging(line), line)

    def test_the_clip_runs_on_the_cleaned_text(self):
        """The bug that made this matter: the clip keeps the first sentence,
        so a cue in front of the line became the whole line."""
        raw = "*Click.*\n\nThe lid groaned."
        self.assertEqual(engine._clip_narration(raw), "*Click.")
        self.assertEqual(
            engine._clip_narration(engine._strip_narration_staging(raw)),
            "The lid groaned.")


class TestNarrationIsClippedToABeat(unittest.TestCase):
    """The register is short declaratives stacked into a beat. Clipping at the
    first full stop could only ever deliver the first third of a line."""

    def test_one_sentence_is_the_default(self):
        self.assertEqual(
            engine._clip_narration("The gate was open. Nobody had opened it."),
            "The gate was open.")

    def test_a_beat_keeps_its_short_declaratives(self):
        line = "The gate was open. Nobody had opened it. I went in."
        self.assertEqual(engine._clip_narration(line, 3), line)

    def test_the_ceiling_still_holds(self):
        line = "One. Two. Three. Four."
        self.assertEqual(engine._clip_narration(line, 3), "One. Two. Three.")

    def test_an_ellipsis_is_a_pause_not_an_ending(self):
        self.assertEqual(
            engine._clip_narration("The water was still... too still."),
            "The water was still... too still.")

    def test_an_unterminated_line_gets_its_full_stop(self):
        self.assertEqual(engine._clip_narration("Nobody came back"),
                         "Nobody came back.")

    def test_a_long_tail_is_dropped_rather_than_read_out(self):
        long_second = " " + ("and the dust went on for miles " * 12) + "."
        out = engine._clip_narration("The rig was dead." + long_second, 3)
        self.assertEqual(out, "The rig was dead.")

    def test_one_sentence_is_the_floor_even_when_it_runs_long(self):
        """Better a long first sentence than a line cut off mid-thought."""
        long_first = "The rig " + ("went on and on " * 30) + "."
        self.assertEqual(engine._clip_narration(long_first, 3), long_first)


class TestNarrationMemory(unittest.TestCase):

    def setUp(self):
        _discard_test_session()

    def tearDown(self):
        _discard_test_session()

    def test_records_what_was_said(self):
        engine._load_state(SESSION_ID)
        engine._remember_narration([{"character": "narrator", "text": "The water is rising."}], SESSION_ID)
        st = engine._load_state(SESSION_ID)
        self.assertEqual(st.get("narrator_recent"), ["The water is rising."])

    def test_accumulates_across_narrations(self):
        engine._remember_narration([{"text": "One."}], SESSION_ID)
        engine._remember_narration([{"text": "Two."}], SESSION_ID)
        self.assertEqual(engine._load_state(SESSION_ID).get("narrator_recent"), ["One.", "Two."])

    def test_is_capped(self):
        for i in range(engine.NARRATOR_MEMORY + 5):
            engine._remember_narration([{"text": f"line {i}."}], SESSION_ID)
        kept = engine._load_state(SESSION_ID).get("narrator_recent") or []
        self.assertEqual(len(kept), engine.NARRATOR_MEMORY)
        self.assertNotIn("line 0.", kept, "the oldest lines fall off the end")

    def test_empty_and_malformed_scripts_are_survivable(self):
        # Narration the player is waiting on must never fail because a
        # bookkeeping write did.
        for junk in ([], None, [{}], [{"text": "   "}]):
            engine._remember_narration(junk, SESSION_ID)
        self.assertFalse(engine._load_state(SESSION_ID).get("narrator_recent"))


class TestTheAuthoredDirection(unittest.TestCase):
    """The shipped `narrator_direction` is the actual brief the model reads, so
    the repetition complaint has to be answerable here or nowhere."""

    @classmethod
    def setUpClass(cls):
        cls.live = json.loads((ROOT / "prompts/simulation_prompts.json").read_text(encoding="utf-8"))
        cls.defaults = json.loads((ROOT / "prompts/simulation_prompts.defaults.json").read_text(encoding="utf-8"))

    def test_shipped_and_default_agree(self):
        self.assertEqual(self.live["narrator_direction"], self.defaults["narrator_direction"])

    def test_it_does_not_dictate_the_feeling(self):
        d = self.live["narrator_direction"].lower()
        self.assertNotIn("afraid, uneasy, but determined", d)

    def test_it_does_not_dictate_the_intent(self):
        # "Make it clear you have to find out what happened here" on every line
        # is why every line said it.
        self.assertNotIn("have to find out what happened here",
                         self.live["narrator_direction"].lower())

    def test_it_asks_the_line_to_be_specific_to_this_moment(self):
        self.assertIn("{scene}", self.live["narrator_direction"])
        self.assertIn("specific", self.live["narrator_direction"].lower())

    def test_the_line_is_one_buried_fact_out_of_the_lore(self):
        """The narrator's whole register. `_ask(use_lore=True)` prepends the
        Experience bible to this call, and a buried fact set down flat is what
        the voice IS — not a caption of the frame, not the action played back.

        This was briefly replaced with action-consequence narration to answer
        a complaint about repetition. It answered the wrong thing: the lines
        stopped repeating and stopped being the narrator."""
        d = self.live["narrator_direction"]
        self.assertIn("ONE LINE OF HISTORY, AND NOTHING ELSE", d)
        self.assertIn("HISTORICAL BACKGROUND", d)
        self.assertIn("ONE buried piece", d)
        self.assertIn("Do not caption the frame", d)

    def test_the_fact_is_chosen_by_the_deed_not_by_the_room(self):
        """The actual defect behind "he repeats himself".

        The rule was "choose the fact by where the player is standing", which
        keys off the ROOM — so every line in a room reached for the same
        shelf of the bible however the player had just acted on it. Keyed off
        the deed, walking to a thing and prying it open are different
        questions and pull different facts."""
        d = self.live["narrator_direction"]
        self.assertIn("CHOOSE THE FACT BY WHAT THE PLAYER JUST DID", d)
        self.assertNotIn("Choose the fact by where the player is standing", d)
        low = d.lower()
        self.assertIn("walked toward", low)
        self.assertIn("put their hands on", low)
        self.assertIn("spoke to", low)
        # The test of whether the line belongs to this run at all.
        self.assertIn("would fit their run unchanged", low)

    def test_the_brief_defers_the_shape_to_the_beats(self):
        """The brief used to carry its own "FACT, THEN QUESTION, THEN FACT"
        section, with worked examples, while the engine appended a rotation of
        six shapes as a tail. Two instructions, and the exemplified one won: on
        a session whose remembered lines were all fact-then-question, eight
        narrations running came back fact-then-question no matter which beats
        the tail named. Whichever of the two is more concrete wins, so the
        brief must not hold a competing shape at all."""
        d = self.live["narrator_direction"]
        self.assertNotIn("FACT, THEN QUESTION, THEN FACT", d)
        self.assertIn("TWO BEATS", d)
        self.assertIn("named at the very END of this brief", d)
        # The heading it used to tell the narrator to go and classify for
        # itself. That block is still supplied — it is `{avoid}`, written by
        # the engine — but reading it is no longer how the shape gets decided.
        self.assertNotIn("see which one you did last", d)
        self.assertIn("{avoid}", d)

    def test_a_non_history_beat_is_allowed_to_be_one(self):
        """"ONE LINE OF HISTORY, AND NOTHING ELSE" as an unconditional heading
        is what made every shape collapse back into a fact."""
        d = self.live["narrator_direction"]
        self.assertIn("WHEN A BEAT ASKS FOR ANYTHING ELSE", d)
        self.assertIn("no history required", d)

    def test_it_asks_for_two_short_sentences(self):
        d = self.live["narrator_direction"]
        self.assertIn("Two short sentences", d)
        self.assertIn("fifteen words", d)

    def test_thinking_out_loud_is_not_banned_as_backstory(self):
        """"it needs to be personalized, like his own internal thinking."

        The no-invented-biography rule was reading as a ban on interiority
        too, so the voice had nothing left but recitation."""
        d = self.live["narrator_direction"]
        self.assertIn("bans BIOGRAPHY, not thinking", d)
        self.assertIn("what you have decided about it", d)

    def test_it_forbids_inventing_a_backstory(self):
        """Reported as the narrator sounding "bugged, like the old narrator" —
        a lost brother it had never mentioned, different every trip. Nothing
        in the premise or the lore supplies one, so when a brief asked for a
        buried private motive the model simply made one up.

        The history the narrator may reach for is the WORLD'S, never its own."""
        low = self.live["narrator_direction"].lower()
        self.assertIn("never invent a private history for yourself", low)
        self.assertIn("no lost brother", low)

    def test_a_move_spends_two_narrations(self):
        """A trip is the one beat with room for two, so it plays them as a
        pair: one on the way out, one a second later over the black.

        The focus says WHEN and WHAT ABOUT; the SHAPE is the server's, because
        naming it here pinned the game's most frequent narration — MOVE TO is
        the main verb — to "the fact, then the question" on every single trip,
        which is the pairing the voice already overused. The invented-brother
        ban stays: that came from this focus asking for "a guilt, a debt, a
        person they lost", and nothing in the lore supplies one."""
        js = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")
        self.assertNotIn("REVEAL A DARK TRUTH", js)
        self.assertNotIn("the fact, then the question", js)
        self.assertIn("over the fade to black", js)
        self.assertIn("A beat after the line before it", js)
        self.assertIn("INVENT NOTHING about yourself", js)

    def test_the_move_focuses_fit_inside_the_server_clip(self):
        """`focus` and `follow_focus` are clipped to 240 chars server-side.
        A focus that runs past it loses its tail silently, which is how a
        carefully worded instruction turns into half a sentence."""
        js = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")
        run = js[js.index("const bridgeFocus ="):js.index("AgentLog.push(\"narrator\", \"transition")]
        # Every quoted fragment the two focuses are assembled from.
        parts = re.findall(r'"((?:[^"\\]|\\.)*)"', run)
        longest_focus = sum(len(p) for p in parts)
        self.assertLess(longest_focus, 240 * 2 + 80,
                        "the MOVE TO focuses have grown past what the server keeps")

    def test_a_focus_does_not_overwrite_the_brief(self):
        """A focus is injected as "follow these exactly, ahead of any mood
        note below", so it outranks the direction. One that describes WHAT to
        say therefore replaces the narrator's voice with its own paraphrase —
        which is how the register got lost. A focus states the situation."""
        js = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")
        commit = js[js.index("function onCommit(choiceText, deed) {"):]
        commit = commit[:commit.index("\n    }")]
        focus = commit[commit.index("focus:"):]
        self.assertIn("the silence after the player commits", focus)
        # The things that belong to the brief, not to a per-trigger override.
        for content_rule in ("say what", "means for the person"):
            self.assertNotIn(content_rule, focus.lower())

    def test_it_asks_for_a_beat_not_a_monologue(self):
        """One sentence became two when the shapes became a rotating pair: the
        first sets something down, the second turns on it. Still a beat — a
        ceiling of fifteen words each, over in about eight seconds spoken."""
        d = self.live["narrator_direction"].lower()
        self.assertIn("two short sentences", d)
        self.assertIn("fifteen words", d)
        self.assertNotIn("two or three sentences", d)

    def test_it_carries_every_placeholder_the_engine_supplies(self):
        from prompts_store import PROMPT_SCHEMA
        field = next(f for f in PROMPT_SCHEMA if f["id"] == "narrator_direction")
        for var in field["format_vars"]:
            self.assertIn("{" + var + "}", self.live["narrator_direction"],
                          f"the shipped direction drops {{{var}}}")

    def test_the_declared_vars_match_what_the_engine_passes(self):
        from prompts_store import PROMPT_SCHEMA
        field = next(f for f in PROMPT_SCHEMA if f["id"] == "narrator_direction")
        # A var the engine does not pass raises KeyError at narration time and
        # silently drops the authored voice for the shipped one.
        self.assertEqual(
            set(field["format_vars"]),
            {"world", "self", "premise", "scene", "recent", "focus", "avoid"},
        )


class TestTheBoundWorldsCarryTheShippedVoice(unittest.TestCase):
    """Editing the prompt file alone does nothing, and does it silently.

    `prompts/simulation_prompts.json` is a derived scratch pad: a run binds to
    its start World and `worlds_store.load_world` writes that snapshot's
    prompts over the live file. Every saved World had its own frozen copy of
    the narrator direction, so rewriting the shipped one changed the editor and
    nothing a player ever heard — the next Play restored the old voice.
    """

    RETIRED = "speaking quietly to yourself"

    @classmethod
    def setUpClass(cls):
        cls.worlds = ROOT / "worlds"
        if not cls.worlds.is_dir():
            raise unittest.SkipTest("no saved worlds in this checkout")

    def test_no_saved_world_still_carries_the_retired_direction(self):
        stale = []
        for path in sorted(self.worlds.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            prompts = (data or {}).get("prompts")
            if not isinstance(prompts, dict):
                continue
            if self.RETIRED in (prompts.get("narrator_direction") or ""):
                stale.append(path.name)
        self.assertEqual(
            stale, [],
            "these Worlds will overwrite the shipped narrator on the next "
            "Play/reset: " + ", ".join(stale))


class TestColdOpenWaitsForTheOpening(unittest.TestCase):
    """The 4.2s timed cold open talked over the title card while the opening
    cinematic was still rendering, and its focus ordered every first line onto
    'I feel uneasy / I have to find out what happened'."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")

    def test_reset_arms_the_cold_open_instead_of_firing_it_on_a_timer(self):
        self.assertIn("Narrator.armColdOpen()", self.js)
        # The old boot fired a 4.2s timer regardless of the cinematic.
        self.assertNotIn("setTimeout(() => Narrator.coldOpen()", self.js)

    def test_cold_open_defers_while_the_cinematic_is_rendering(self):
        busy = self.js.split("function openingBusy()", 1)[1][:500]
        self.assertIn("Cutscene.isGenerating", busy)
        self.assertNotIn("Cutscene.isActive", busy)
        self.assertIn("OpeningFade.isHolding", busy)
        cold = self.js.split("function coldOpen()", 1)[1].split(
            "function onOpeningReady", 1)[0]
        self.assertIn("openingBusy()", cold)

    def test_the_montage_starting_releases_the_cold_open(self):
        """Voice-over: the narrator starts when shots exist and playback
        begins, not when the montage is over."""
        enter = self.js.split("async function enterFromServer", 1)[1]
        enter = enter.split("async function play(opts)", 1)[0]
        after_play = enter.split("playing = true", 1)[1]
        self.assertIn("Narrator.onOpeningReady()",
                      after_play.split("return true", 1)[0])

    def test_cold_open_does_not_dictate_unease_or_the_mission(self):
        self.assertNotIn(
            "Say how uneasy you feel and that you need to find out what happened",
            self.js)
        self.assertNotIn("You have just woken up here", self.js)
        cold = self.js.split("function coldOpen()", 1)[1].split(
            "function onOpeningReady", 1)[0]
        self.assertIn("opening cinematic", cold)
        self.assertNotIn("uneasy", cold)
        self.assertIn("Do not state the mission", cold)

    def test_the_cold_open_is_the_beat_allowed_to_carry_backstory(self):
        """Everywhere else the narrator is pinned to the frame in front of it.
        Over the opening montage that reads as a caption, so this is the one
        focus that sends it into the history."""
        cold = self.js.split("function coldOpen()", 1)[1].split(
            "function onOpeningReady", 1)[0]
        self.assertIn("history of this place", cold)
        self.assertIn("how things came to be this way", cold)


if __name__ == "__main__":
    unittest.main(verbosity=2)

class TestTheNarratorSpeaksWhenItShould(unittest.TestCase):
    """Reported as "it plays on the opening, twice after my first choice, then
    never again". Three separate causes, all of them here."""

    @classmethod
    def setUpClass(cls):
        cls.js = (Path(__file__).resolve().parent / "static" / "js"
                  / "standalone.js").read_text(encoding="utf-8")

    def test_every_committed_choice_gets_a_line(self):
        """"Never again" was literal: the only triggers were the cold open and
        MOVE TO, so an ordinary choice never narrated at all."""
        self.assertIn("function onCommit(choiceText, deed)", self.js)
        self.assertIn(
            "Narrator.onCommit(choiceText, narratorDeed(actionSource, actionSubject))",
            self.js)
        commit = self.js.split("function onCommit(choiceText, deed) {", 1)[1].split(
            "\n    }", 1)[0]
        for guard in ("state.gameOver", "Talk.isOpen()", "state.audioUnlocked",
                      "openingBusy()", "busy || playing"):
            self.assertIn(guard, commit, f"onCommit must decline on {guard}")

    def test_a_move_does_not_get_two_narrations_at_once(self):
        """MOVE TO already fires transition(), which is the deliberate TWO
        lines the player heard as "twice". Firing onCommit as well would hit
        narrate()'s busy check and cancel the transition instead of adding
        to it."""
        self.assertIn("if (!moveTarget) {", self.js)

    def test_the_caption_waits_for_the_voice(self):
        """show() ran before the SDK loaded and the websocket opened, so the
        line was on screen seconds before it was spoken. Since 2026-09-25 a
        line is a file (VoiceOut), and its caption goes up on `playing`."""
        seg = self.js.split("function speakSegment(seg, myGen, pending) {", 1)[1].split(
            "\n    async function play(", 1)[0]
        self.assertIn("const reveal = () => show(seg.character, seg.text);", seg)
        self.assertIn("onStart: reveal", seg)
        self.assertNotIn("\n        show(seg.character, seg.text);", seg)
        # a line that plays but never reports `playing` must still subtitle
        self.assertIn("setTimeout(() => { if (myGen === gen) reveal(); }, 1500)", seg)

    def test_the_bar_is_positioned_by_the_stylesheet(self):
        """An inline bottom offset pushed the caption up by the action wheel's
        height, back into the choice stack's band - the exact collision the
        stylesheet had moved it to the bottom edge to avoid."""
        self.assertNotIn('el.narratorBar.style.bottom', self.js)
        css = (Path(__file__).resolve().parent / "static" / "css"
               / "standalone.css").read_text(encoding="utf-8")
        line = css.split("#narrator-line {", 1)[1].split("}", 1)[0]
        size = float(line.split("font-size:", 1)[1].split("px", 1)[0].strip())
        self.assertLessEqual(size, 10.0, "the caption is a subtitle, not a choice")
