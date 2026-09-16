#!/usr/bin/env python3
"""The danger systems have to be simulated, not narrated.

Detection was a thing the prose talked about and the engine did not model.
`in_combat` was initialised False and written by nothing at all; a guard could
spot the player and give chase and the next turn began from hidden. The result
read as hallucinated, because it was: the story asserted stakes that no state
backed.

There was a hit-point pool here too, tested alongside it. It was removed — see
engine's "how a run ends" — because it was extracted by scraping injury words
out of free prose, and that could not reliably tell new harm from a restatement
of old harm. Detection survived the cull because it is driven by events the
engine already knows about, so it is the danger dial now.

These tests hold the line on the part that makes it real — that a number moves
when the fiction says it should, that it does NOT move when the fiction is just
being atmospheric, and that what the player is shown matches what the engine
believes.

    python -m unittest test_danger_simulation -v
"""

from __future__ import annotations

import unittest

import engine


class TestDetectionSignals(unittest.TestCase):
    """The signal table is the whole system: everything downstream is arithmetic
    on what this decides. It is also where the injury extractor went wrong, by
    matching harm words in prose that was being metaphorical."""

    def test_being_chased_outweighs_being_seen(self):
        chase = engine.detection_signal("The guard gives chase across the yard.")
        seen = engine.detection_signal("The guard spots you across the yard.")
        watched = engine.detection_signal("The figure turns toward you.")
        self.assertGreater(chase, seen)
        self.assertGreater(seen, watched)

    def test_ambient_prose_does_not_alert_anything(self):
        """Every one of these fired on the first draft of the signal table.
        Scene-setting is most of what a narrator writes, so anything that can
        match it will match constantly and the dial becomes noise."""
        for quiet in (
            "Nothing stirs. The corridor is empty and silent.",
            "You pause and listen. Something moves in the dark ahead.",
            "The silence of the warehouse is punctured by a metallic tap.",
            "Dust settles. A pipe clatters somewhere below.",
            "You cock your head, listening for footsteps that do not come.",
        ):
            self.assertEqual(engine.detection_signal(quiet), 0, quiet)

    def test_the_player_noticing_something_is_not_being_noticed(self):
        """"notices" as a bare word made the player's own perception read as
        the world spotting them, which inverts the entire mechanic."""
        self.assertEqual(
            engine.detection_signal("You notice a rusted valve on the far wall."), 0)

    def test_the_player_crying_out_is_not_an_alarm(self):
        self.assertEqual(
            engine.detection_signal(
                "The searing heat forces a strangled cry from your throat."), 0)


class TestDetectionStateMachine(unittest.TestCase):
    """A guard spotting you has to still be true on the next turn, or it was
    never a mechanic."""

    def test_being_seen_raises_the_level(self):
        st = {}
        det, rose = engine.apply_detection(st, "The guard spots you and gives chase.")
        self.assertTrue(rose)
        self.assertGreaterEqual(det["level"], engine.DETECT_ALERTED)

    def test_it_persists_into_the_next_turn(self):
        """The bug this whole system exists to fix: state was rebuilt from the
        current turn's prose, so last turn's pursuit simply evaporated."""
        st = {}
        engine.apply_detection(st, "The guard spots you and gives chase.")
        det, _ = engine.apply_detection(st, "You round the corner, breathing hard.")
        self.assertGreater(det["level"], engine.DETECT_HIDDEN)

    def test_a_quiet_turn_cools_it_and_moving_cools_faster(self):
        loud = "An alarm blares. The guard spots you and gives chase."
        still, moving = {}, {}
        engine.apply_detection(still, loud)
        engine.apply_detection(moving, loud)
        engine.apply_detection(still, "You wait, listening.", is_move=False)
        engine.apply_detection(moving, "You run for the fence line.", is_move=True)
        self.assertLess(moving["detection"]["heat"], still["detection"]["heat"])

    def test_you_can_eventually_lose_them(self):
        st = {}
        engine.apply_detection(st, "The guard spots you and gives chase.")
        for _ in range(8):
            engine.apply_detection(st, "You move on through the dark.", is_move=True)
        self.assertEqual(st["detection"]["level"], engine.DETECT_HIDDEN)

    def test_heat_is_bounded(self):
        st = {}
        for _ in range(12):
            engine.apply_detection(st, "The guard spots you and gives chase.")
        self.assertLessEqual(st["detection"]["heat"], engine.DETECT_HEAT_MAX)

    def test_meddling_is_loud(self):
        quiet, loud = {}, {}
        engine.apply_detection(quiet, "You ease the panel open.", interaction=False)
        engine.apply_detection(loud, "You ease the panel open.", interaction=True)
        self.assertGreater(loud["detection"]["heat"], quiet["detection"]["heat"])

    def test_in_combat_finally_means_something(self):
        """It was initialised False and written by nothing, while /api/status
        reported it every single turn."""
        st = {}
        engine.apply_detection(st, "You creep along the wall.", is_move=True)
        self.assertFalse(st["in_combat"])
        engine.apply_detection(st, "The guard spots you and gives chase.")
        self.assertTrue(st["in_combat"])

    def test_running_shakes_a_pursuer_even_mid_chase(self):
        """The loop this closes: while hunted, the narrator is instructed to
        write a chase every turn, that prose re-fires the signal, and heat sits
        at the ceiling for the rest of the run. A 20-turn render climbed to max
        by turn six and never came down. Fleeing has to beat the prose."""
        st = {}
        for _ in range(4):
            engine.apply_detection(st, "It is chasing you, closing on you fast.")
        self.assertEqual(st["detection"]["level"], engine.DETECT_HUNTED)

        pinned = st["detection"]["heat"]
        engine.apply_detection(st, "It is still chasing you, closing on you.",
                               fleeing=True)
        self.assertLess(st["detection"]["heat"], pinned)

    def test_committed_flight_gets_you_clear(self):
        st = {}
        for _ in range(4):
            engine.apply_detection(st, "It is chasing you, closing on you fast.")
        for _ in range(4):
            engine.apply_detection(st, "It is still behind you, chasing you.",
                                   fleeing=True, is_move=True)
        self.assertEqual(st["detection"]["level"], engine.DETECT_HIDDEN)
        self.assertFalse(st["in_combat"])

    def test_being_hunted_always_offers_a_way_out(self):
        """Fleeing is the only thing that sheds heat, so a hunted slate with no
        escape on it is a trap with no visible edges."""
        deeper = ["Crawl deeper into the shaft", "Force the hatch",
                  "Push further into the dark"]
        out = engine.enforce_egress_option(list(deeper), 0, engine.DETECT_HUNTED)
        self.assertTrue(any(engine.is_egress_choice(c) for c in out))
        self.assertEqual(len(out), len(deeper))

    def test_a_calm_slate_is_left_alone(self):
        deeper = ["Crawl deeper into the shaft", "Force the hatch",
                  "Push further into the dark"]
        self.assertEqual(
            engine.enforce_egress_option(list(deeper), 0, engine.DETECT_HIDDEN),
            deeper)

    def test_every_forced_flight_option_counts_as_fleeing(self):
        """The one that would have quietly broken the whole escape valve: a
        forced option the engine does not recognise as egress sheds no heat, so
        the game offers the player a way out that does not work."""
        for option in engine._FLIGHT_FALLBACKS + engine._EGRESS_FALLBACKS:
            self.assertTrue(engine.is_egress_choice(option), option)

    def test_the_words_a_model_would_actually_use_count_as_fleeing(self):
        for phrasing in ("Run for the loading door", "Break and run for the fence",
                         "Sprint down the corridor", "Turn and flee into the dark",
                         "Scramble out through the gap", "Put distance between you"):
            self.assertTrue(engine.is_egress_choice(phrasing), phrasing)

    def test_prose_that_merely_contains_run_is_not_an_escape(self):
        for staying in ("Run your hand along the pipe seam",
                        "Run the cable to the junction box"):
            self.assertFalse(engine.is_egress_choice(staying), staying)

    def test_the_forced_escape_is_flight_not_a_stroll(self):
        """Walking calmly back the way you came is not an answer to something
        chasing you, and offering it as one reads as the game not noticing."""
        deeper = ["Crawl deeper", "Force the hatch", "Push into the dark"]
        out = engine.enforce_egress_option(list(deeper), 0, engine.DETECT_HUNTED)
        self.assertIn(out[-1], engine._FLIGHT_FALLBACKS)

    def test_a_hunted_run_is_not_handed_one_sentence_forever(self):
        """A live render forced the identical option eight turns running: the
        rotation keyed on streak and detection, and a hunted run holds both
        constant."""
        deeper = ["Crawl deeper", "Force the hatch", "Push into the dark"]
        forced = {engine.enforce_egress_option(
            list(deeper), 0, engine.DETECT_HUNTED, turn)[-1] for turn in range(4)}
        self.assertGreater(len(forced), 1)

    def test_a_level_drop_is_not_reported_as_a_rise(self):
        st = {}
        engine.apply_detection(st, "The guard spots you and gives chase.")
        _det, rose = engine.apply_detection(st, "You slip away into the dark.",
                                            is_move=True)
        self.assertFalse(rose)


class TestTheNarratorIsToldTheTruth(unittest.TestCase):
    """If the prompt doesn't carry the simulated state, the prose invents its
    own and the HUD ends up contradicting the story in front of the player."""

    def test_the_directive_reports_detection(self):
        """One chase beat is "they are onto you"; a sustained one is a hunt.
        Two, because that is the difference the levels are for."""
        st = {}
        engine.apply_detection(st, "The guard spots you and gives chase.")
        self.assertIn("ALERTED", engine.condition_directive(st).upper())
        engine.apply_detection(st, "It is still chasing you, closing on you fast.")
        self.assertIn("HUNTED", engine.condition_directive(st).upper())

    def test_a_hidden_player_is_not_told_they_are_hunted(self):
        self.assertIn("HIDDEN", engine.condition_directive({}).upper())


class TestChaosAnswersToDetection(unittest.TestCase):
    def test_being_spotted_raises_tension(self):
        calm = engine.chaos_events("NORMAL", False, False, spotted=False)
        seen = engine.chaos_events("NORMAL", False, False, spotted=True)
        self.assertEqual(calm, [])
        self.assertTrue(seen)

    def test_it_lands_in_the_dial(self):
        st = {"chaos_level": 0}
        engine.apply_chaos(st, fate="NORMAL", escalated=False,
                           interaction=False, spotted=True)
        self.assertGreater(st["chaos_level"], 0)


class TestStateCarriesTheNewFields(unittest.TestCase):
    """A save written before these systems existed must not make every consumer
    fall back to its own hardcoded default."""

    def test_detection_defaults_cleanly_on_a_bare_state(self):
        det = engine.get_detection({})
        self.assertEqual(det["level"], engine.DETECT_HIDDEN)
        self.assertEqual(det["heat"], 0)

    def test_a_corrupt_detection_field_does_not_crash(self):
        for junk in ("hunted", 3, None, [], {"heat": "lots"}):
            det = engine.get_detection({"detection": junk})
            self.assertIsInstance(det["heat"], int)


class TestTheTurnLoopActuallyRunsThem(unittest.TestCase):
    """The injury tracker existed for months before anything called it, and its
    list stayed empty for whole runs while four prompts read it. A system nobody
    invokes is indistinguishable from one that doesn't exist, so the wiring gets
    its own guard — that tracker is gone now, but detection inherited the same
    trap and the same guard."""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.src = (Path(__file__).parent / "engine.py").read_text(encoding="utf-8")

    def test_detection_is_read_from_the_turn(self):
        # Both channels: the caption carries the geometry, the narrative
        # carries "it gives chase". Reading only the caption meant a turn
        # could announce pursuit in prose and never be registered.
        self.assertIn('apply_detection(state, f"{dispatch}\\n{vision_dispatch}"', self.src)

    def test_the_narrator_is_handed_the_condition(self):
        self.assertIn("condition_directive(state)", self.src)

    def test_detection_biases_fate(self):
        self.assertIn("detect_bias", self.src)


class TestApiReportsWhatTheEngineBelieves(unittest.TestCase):
    """The HUD reads /api/status. If the endpoint invents its own defaults the
    player is shown one thing while the simulation runs on another — which is
    exactly how health spent months displaying 100 on a bleeding player."""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.src = (Path(__file__).parent / "api.py").read_text(encoding="utf-8")

    def test_health_is_no_longer_reported_at_all(self):
        """The pool is gone, so reporting it would be inventing a default —
        the very failure this class exists to catch."""
        self.assertNotIn('"health"', self.src)
        self.assertNotIn('"health_max"', self.src)
        self.assertNotIn('"injuries"', self.src)

    def test_detection_is_exposed(self):
        self.assertIn('"detection"', self.src)
        self.assertIn("engine.get_detection", self.src)

    def test_preload_stills_are_exposed(self):
        """Start-menu signal lock reads last still + level plate from status.
        Those fields must be real state, not invented URLs."""
        self.assertIn('"current_image_url"', self.src)
        self.assertIn('"setting_plate_url"', self.src)
        self.assertIn("_setting_plate_url", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
