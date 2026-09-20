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


class TestTheFrameIsAWitness(unittest.TestCase):
    """The signal table above can only HEAR. That makes it a closed loop — the
    model deciding whether the model got seen — and it is why the dial read as
    invented: phrase it "the guard's attention settles on your position" and
    nothing moves.

    Two vision passes already look at the frame the player is looking at (the
    SCAN detector and the scene analysis behind /api/observe) and both threw
    this answer away. These pin reading it — and, just as hard, NOT inventing
    a reading when the frame did not give one."""

    # ── the SCAN detector: geometry, no orientation ──────────────────────
    def test_a_body_in_frame_is_a_watcher(self):
        seen = engine.witness_from_detections(
            [{"label": "guard", "kind": "person", "h": 0.7, "w": 0.3}])
        self.assertEqual(seen["watchers"], 1)
        self.assertEqual(seen["distance"], "near")
        self.assertGreater(engine.witness_signal(seen), 0)

    def test_machinery_is_not_a_watcher(self):
        """The detector labels far more things `machine` than are pointed
        anywhere, and a sensor that fires on every pump and generator in frame
        is the ambient-prose failure again in a new coat."""
        self.assertEqual(engine.witness_signal(engine.witness_from_detections([
            {"label": "oil pump", "kind": "machine", "h": 0.8},
            {"label": "crate", "kind": "object", "h": 0.9},
        ])), 0)

    def test_the_nearest_body_sets_the_distance(self):
        near = engine.witness_from_detections([{"kind": "person", "h": 0.8}])
        far = engine.witness_from_detections([{"kind": "person", "h": 0.04}])
        self.assertGreater(engine.witness_signal(near), engine.witness_signal(far))

    def test_it_remembers_which_thing_was_closest(self):
        """The label is what lets a hunted encounter be the thing that was
        actually on screen instead of a fresh roster draw."""
        seen = engine.witness_from_detections([
            {"label": "dog", "kind": "animal", "h": 0.2},
            {"label": "site foreman", "kind": "person", "h": 0.6},
        ])
        self.assertEqual(seen["label"], "site foreman")

    def test_a_detector_that_failed_is_not_an_empty_room(self):
        """"I could not look" and "nobody is there" are different answers.
        Conflating them lets a broken vision call silently clear the dial,
        which is the failure mode this whole system exists to remove."""
        self.assertIsNone(engine.witness_from_detections(None))
        self.assertEqual(engine.witness_from_detections([])["watchers"], 0)

    # ── the scene analysis: one parsed line off a call already being made ──
    def test_the_watchers_line_is_read(self):
        seen = engine.witness_from_vision({"watchers": "2 | facing | near"})
        self.assertEqual(
            (seen["watchers"], seen["facing"], seen["distance"]),
            (2, "facing", "near"))

    def test_an_analysis_from_before_the_field_existed_does_not_lie(self):
        """_vision_analyze_all caches to disk. Every entry written before
        WATCHERS existed comes back without it, and a missing field must read
        as "no reading", never as an empty room."""
        for stale in ({}, {"description": "a corridor"}, {"watchers": ""},
                      {"watchers": "   "}, {"watchers": "no idea"}):
            self.assertIsNone(engine.witness_from_vision(stale), stale)

    def test_a_malformed_line_never_raises(self):
        for junk in ("lots | facing | near", "|||", "?", None, 7, [], {}):
            engine.witness_from_vision(junk)

    def test_an_empty_room_written_in_words_still_counts(self):
        """The format asks for a digit and usually gets one, but "none" is the
        obvious thing to write for an empty frame and it is a real answer, not
        a failed read. Parsed as "no reading", an empty room could never cool
        the dial."""
        for phrasing in ("none | none | far", "nobody | none | far",
                         "no one visible | none | far"):
            seen = engine.witness_from_vision({"watchers": phrasing})
            self.assertIsNotNone(seen, phrasing)
            self.assertEqual(seen["watchers"], 0, phrasing)

    def test_looking_at_you_outranks_standing_near_you(self):
        facing = engine.witness_from_vision({"watchers": "1 | facing | mid"})
        away = engine.witness_from_vision({"watchers": "1 | away | mid"})
        self.assertGreater(engine.witness_signal(facing),
                           engine.witness_signal(away))

    def test_a_back_turned_raises_nothing_at_any_distance(self):
        """The dial measures what the world KNOWS. Something that has not
        looked at the player knows nothing, however close it is standing —
        that is exposure, and witness_holds is where exposure is priced."""
        for distance in ("near", "mid", "far"):
            seen = engine.witness_from_vision(
                {"watchers": f"3 | away | {distance}"})
            self.assertEqual(engine.witness_signal(seen), 0, distance)
        self.assertTrue(engine.witness_holds(
            engine.witness_from_vision({"watchers": "1 | away | near"})))

    def test_a_crowd_looking_at_you_is_worse_than_one_person(self):
        one = engine.witness_from_vision({"watchers": "1 | facing | mid"})
        many = engine.witness_from_vision({"watchers": "4 | facing | mid"})
        self.assertGreater(engine.witness_signal(many),
                           engine.witness_signal(one))

    def test_a_crowd_with_its_back_turned_is_still_scenery(self):
        self.assertEqual(engine.witness_signal(
            engine.witness_from_vision({"watchers": "5 | away | far"})), 0)

    def test_an_empty_frame_reads_as_empty(self):
        empty = engine.witness_from_vision({"watchers": "0 | none | far"})
        self.assertEqual(engine.witness_signal(empty), 0)
        self.assertFalse(engine.witness_holds(empty))


class TestTheDialAnswersThePicture(unittest.TestCase):
    """What the witness is FOR: the number the player watches has to move for
    reasons they can see on screen."""

    @staticmethod
    def _saw(st, line):
        engine.record_witness(st, engine.witness_from_vision({"watchers": line}))

    @staticmethod
    def _next_turn(st):
        """What _process_turn_background does between turns. Readings are
        per-frame, so a multi-turn test that never advances the count is one
        turn's worth of passes merging with each other forever."""
        st["turn_count"] = int(st.get("turn_count", 0) or 0) + 1

    def test_a_silent_turn_with_someone_watching_raises_it(self):
        """The failure in one sentence: a guard can be in frame staring down
        the lens and, as long as the narrator writes around it, the engine
        believes the player is hidden."""
        st = {}
        self._saw(st, "1 | facing | near")
        det, rose = engine.apply_detection(st, "Dust drifts through the doorway.")
        self.assertTrue(rose)
        self.assertGreater(det["heat"], 0)
        self.assertEqual(det["source"], "frame")

    def test_an_empty_frame_and_a_quiet_turn_still_cools(self):
        st = {}
        engine.apply_detection(st, "The guard spots you and gives chase.")
        hot = st["detection"]["heat"]
        self._saw(st, "0 | none | far")
        engine.apply_detection(st, "You move on through the dark.", is_move=True)
        self.assertLess(st["detection"]["heat"], hot)

    def test_standing_beside_a_body_does_not_cool(self):
        """Heat bleeding off under a calm sentence is correct in an empty room
        and a lie when there is somebody in the doorway. The prose sensor could
        not tell those apart."""
        st = {}
        engine.apply_detection(st, "The guard spots you and gives chase.")
        hot = st["detection"]["heat"]
        self._saw(st, "1 | away | near")
        engine.apply_detection(st, "You hold still and wait.")
        self.assertEqual(st["detection"]["heat"], hot)
        self.assertEqual(st["detection"]["source"], "frame")

    def test_a_yard_of_oblivious_bodies_cannot_hunt_you(self):
        """The runaway a live run found. Every turn the frame held creatures,
        every turn added a little, nothing ever looked up, and the run went
        hidden to hunted in four turns. A picture in which nothing has looked
        at the player is not evidence the world has seen them."""
        st = {}
        for _ in range(12):
            self._saw(st, "3 | away | near")
            engine.apply_detection(st, "You pick your way between them.",
                                   is_move=True)
            self._next_turn(st)
        self.assertLessEqual(st["detection"]["level"], engine.DETECT_SUSPICIOUS)

    def test_nor_can_meddling_in_front_of_something_oblivious(self):
        """The hole the first version of the ceiling left. It keyed on the
        frame term, so the meddling bonus walked straight through: one creature
        with its back turned, a valve to open every turn, and a live run
        climbed to hunted with nothing having ever looked up. The cap is on
        what an unwatched frame may conclude, whatever produced the gain."""
        st = {}
        for _ in range(12):
            self._saw(st, "1 | away | near")
            engine.apply_detection(st, "You lever the valve around.",
                                   interaction=True, is_move=True,
                                   fate="UNLUCKY")
            self._next_turn(st)
        self.assertLessEqual(st["detection"]["level"], engine.DETECT_SUSPICIOUS)

    def test_nor_can_a_detector_that_cannot_see_orientation(self):
        """Same ceiling, reached the other way: SCAN alone reports `unknown`,
        and a guess is not an observation that anything looked."""
        st = {}
        for _ in range(12):
            engine.record_witness(st, engine.witness_from_detections(
                [{"label": "worker", "kind": "person", "h": 0.8}]))
            engine.apply_detection(st, "You keep moving.", is_move=True)
            self._next_turn(st)
        self.assertLessEqual(st["detection"]["level"], engine.DETECT_SUSPICIOUS)

    def test_but_one_of_them_turning_round_is_enough(self):
        """The other side of it: the ceiling must not make the player
        untouchable, or hiding stops being a risk and becomes a state."""
        st = {}
        for _ in range(4):
            self._saw(st, "3 | away | near")
            engine.apply_detection(st, "You edge past.", is_move=True)
            self._next_turn(st)
        self._saw(st, "3 | facing | near")
        engine.apply_detection(st, "You freeze.")
        self.assertGreaterEqual(st["detection"]["level"], engine.DETECT_ALERTED)

    def test_one_pass_seeing_a_face_beats_another_seeing_a_back(self):
        """Inside a single turn the two passes routinely lock onto different
        bodies. One of them having looked up is the fact that matters."""
        st = {}
        self._saw(st, "3 | away | near")
        self._saw(st, "1 | facing | mid")
        self.assertEqual(engine.current_witness(st)["facing"], "facing")

    def test_the_ceiling_does_not_hold_back_the_prose(self):
        """A single frame cannot show footsteps closing from behind. The cap is
        on what the PICTURE may conclude, not on the turn."""
        st = {}
        self._saw(st, "2 | away | near")
        engine.apply_detection(
            st, "It is chasing you, closing on you fast, hunting you.")
        engine.apply_detection(
            st, "It is still closing on you, bearing down on you.")
        self.assertGreaterEqual(st["detection"]["level"], engine.DETECT_HUNTED)

    def test_the_frame_is_a_second_sensor_not_a_veto(self):
        """A still cannot show footsteps closing behind you. An empty frame
        must not overrule prose that says the player is being chased."""
        st = {}
        self._saw(st, "0 | none | far")
        _det, rose = engine.apply_detection(
            st, "The guard spots you and gives chase.")
        self.assertTrue(rose)
        self.assertEqual(st["detection"]["source"], "prose")

    def test_last_turns_frame_is_not_this_turns(self):
        """A witness describes ONE frame. Once the turn resolves, that frame is
        gone and the reading is an opinion about somewhere the player no longer
        is — the same staleness rule scene_objects_for_turn enforces on the
        SCAN label cache, for the same reason."""
        st = {"turn_count": 4}
        self._saw(st, "3 | facing | near")
        st["turn_count"] = 5
        self.assertIsNone(engine.current_witness(st))
        engine.apply_detection(st, "You move on through the dark.", is_move=True)
        self.assertEqual(st["detection"]["heat"], 0)

    def test_a_later_pass_cannot_erase_what_an_earlier_one_saw(self):
        """The reground reads the frame at the decision point, then the player
        taps SCAN at an empty patch of floor. The figure the first pass saw must
        not be erased by the second."""
        st = {}
        self._saw(st, "2 | facing | near")
        engine.record_witness(st, engine.witness_from_detections([]))
        self.assertGreater(
            engine.witness_signal(engine.current_witness(st)), 0)

    def test_the_pass_that_can_see_orientation_owns_orientation(self):
        """The bug a live run found, and the reason the two passes are merged
        rather than ranked. The scene analysis said "3 | away | mid" — three
        things with their backs turned. The SCAN detector said three animate
        boxes at `unknown`, because boxes have no orientation. Scoring them and
        taking the winner meant `unknown + near` (2) beat `away + mid` (0), so
        the engine decided the player was being watched at close range by
        things it had just been told were facing away. Four turns from hidden
        to hunted in a yard where nothing had looked up."""
        st = {}
        self._saw(st, "3 | away | mid")
        engine.record_witness(st, engine.witness_from_detections(
            [{"label": "creature", "kind": "creature", "h": 0.71},
             {"label": "person", "kind": "person", "h": 0.32},
             {"label": "person", "kind": "person", "h": 0.38}]))
        merged = engine.current_witness(st)
        self.assertEqual(merged["facing"], "away")
        self.assertEqual(merged["distance"], "near")   # geometry is literal
        self.assertEqual(merged["label"], "creature")  # only SCAN has nouns
        self.assertEqual(merged["watchers"], 3)
        self.assertLess(engine.witness_signal(merged), 3)

    def test_an_empty_pass_does_not_donate_its_none(self):
        """A pass that found nobody reports `none | far`. That is not an
        observation about the figures the other pass DID find."""
        st = {}
        engine.record_witness(st, engine.witness_from_detections([]))
        self._saw(st, "2 | facing | near")
        merged = engine.current_witness(st)
        self.assertEqual(merged["facing"], "facing")
        self.assertEqual(merged["watchers"], 2)

    def test_a_shape_across_the_yard_is_allowed_to_cool(self):
        """`holds` is near-only. As near-or-mid, a yard with anything at all in
        the middle distance could never cool — and these scenes almost always
        have something in the middle distance, which is a dial with no way down
        rather than one that answers the room."""
        st = {}
        engine.apply_detection(st, "The guard spots you and gives chase.")
        hot = st["detection"]["heat"]
        self._saw(st, "2 | away | mid")
        engine.apply_detection(st, "You move on through the dark.", is_move=True)
        self.assertLess(st["detection"]["heat"], hot)

    def test_a_pass_that_failed_changes_nothing(self):
        st = {}
        self._saw(st, "2 | facing | near")
        before = dict(engine.current_witness(st))
        engine.record_witness(st, None)
        self.assertEqual(engine.current_witness(st), before)

    def test_the_still_path_reads_the_frame_it_just_drew(self):
        """That frame is what the player sits looking at while they decide, so
        it belongs to the NEXT turn — turn_count has not been bumped yet when
        Phase 2 records it."""
        st = {"turn_count": 7}
        engine.record_witness(
            st, engine.witness_from_vision({"watchers": "1 | facing | near"}),
            for_turn=8)
        self.assertIsNone(engine.current_witness(st))
        st["turn_count"] = 8
        self.assertIsNotNone(engine.current_witness(st))

    def test_a_junk_source_on_disk_is_dropped(self):
        self.assertEqual(
            engine.get_detection({"detection": {"source": "vibes"}})["source"], "")

    def test_a_corrupt_witness_does_not_crash_a_turn(self):
        for junk in ("watched", 3, None, [], {"watchers": "lots"}):
            engine.apply_detection({"detection_witness": junk}, "You wait.")


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

    def test_but_only_when_something_is_there_to_hear_it(self):
        """The runaway a live run found, and the worst one, because it needed
        no sensor to misfire at all. SCAN's MOVE TO and INTERACT both count as
        interaction, the bonus was unconditional, and any gain skips cooling —
        so exploring with the SCAN verbs gained heat every turn and shed it
        never. Ten turns alone in an empty utility corridor, both sensors
        reading zero on every one, and the game said HUNTED."""
        st = {}
        for _ in range(10):
            engine.record_witness(st, engine.witness_from_vision(
                {"watchers": "0 | none | far"}))
            engine.apply_detection(st, "You ease the rusted valve open.",
                                   interaction=True, is_move=True)
            st["turn_count"] = int(st.get("turn_count", 0)) + 1
        self.assertEqual(st["detection"]["level"], engine.DETECT_HIDDEN)

    def test_noise_in_an_empty_room_is_not_reported_as_the_story(self):
        """It is also not nothing — you made a racket, you did not get
        quieter — but calling it "prose" would be the HUD claiming the
        narration said something it never said."""
        st = {"detection": {"heat": 3, "level": 1}}
        engine.record_witness(st, engine.witness_from_vision(
            {"watchers": "0 | none | far"}))
        engine.apply_detection(st, "You lever the hatch open.",
                               interaction=True, is_move=True)
        self.assertEqual(st["detection"]["heat"], 3)
        self.assertEqual(st["detection"]["source"], "noise")

    def test_meddling_in_front_of_someone_is_still_loud(self):
        st = {}
        engine.record_witness(st, engine.witness_from_vision(
            {"watchers": "2 | away | near"}))
        engine.apply_detection(st, "You ease the panel open.", interaction=True)
        self.assertGreater(st["detection"]["heat"], 0)

    def test_a_missing_reading_does_not_buy_silence(self):
        """No witness means the vision pass failed or never ran, which is not
        evidence of an empty room. Offline runs keep the old behaviour."""
        st = {}
        engine.apply_detection(st, "You ease the panel open.", interaction=True)
        self.assertGreater(st["detection"]["heat"], 0)

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

    def test_the_frame_is_read_alongside_the_prose(self):
        self.assertIn("gain = max(heard, seen)", self.src)

    def test_the_vision_prompt_actually_asks_who_can_see_the_player(self):
        """The entire frame sensor is one line in a prompt that was already
        being sent. Drop the line and every witness silently becomes None,
        detection falls back to reading word choice, and nothing fails loudly."""
        self.assertIn("WATCHERS: <count> | <facing|away|none> | <near|mid|far>",
                      self.src)
        self.assertIn('"watchers":      watchers.strip(),', self.src)

    def test_watchers_cannot_leak_into_the_scene_description(self):
        """DESCRIPTION swallows continuation lines until it hits a known field
        label. That description is the spatial anchor for the NEXT image
        prompt, so a label missing from the stop-list does not just lose the
        witness — it poisons the render continuity loop."""
        self.assertIn('"SPATIAL:", "SETTING:", "TIME:", "COLOR:", "WATCHERS:"',
                      self.src)

    def test_every_pass_that_looks_at_a_frame_records_what_it_saw(self):
        """Three call sites, one per way the game can put a picture on screen:
        the SCAN tap, the realtime observe reground, and the still path's read
        of the frame it just rendered. Each rides a vision call that was
        already happening; none of them adds a request."""
        self.assertIn("record_witness(st, witness)", self.src)       # SCAN
        self.assertIn("record_witness(st, v_witness)", self.src)     # observe
        self.assertIn('record_witness(state, _vision_holder["witness"]',
                      self.src)                                       # stills

    def test_the_scan_witness_is_read_before_the_anti_loop_gate(self):
        """That gate empties `objects` to force the player toward an egress
        choice. It is not a claim that the frame is empty, and recording a
        clean witness from the emptied list would tell the engine nobody is
        there at the exact moment a run is being hunted."""
        read = self.src.index("witness = witness_from_detections(objects")
        gate = self.src.index("[ANTI-LOOP] SCAN suppressed")
        self.assertLess(read, gate)


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

    def test_the_hud_is_told_which_sensor_moved_the_dial(self):
        """A number that answers the picture is believable; one that moves for
        reasons the player cannot observe is the thing this replaced."""
        self.assertIn('"detection_source"', self.src)

    def test_preload_stills_are_exposed(self):
        """Start-menu signal lock reads last still + level plate from status.
        Those fields must be real state, not invented URLs."""
        self.assertIn('"current_image_url"', self.src)
        self.assertIn('"setting_plate_url"', self.src)
        self.assertIn("_setting_plate_url", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
