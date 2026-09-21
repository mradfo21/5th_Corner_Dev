"""An encounter has to read as a fight, not a slot machine with pictures.

Each class here pins one thing a play session got wrong:

* the nameplate, the prose, and the image were three different people,
  because the cast lock used the look the brief INVENTED rather than the one
  the plate actually drew;
* the player got mistaken for the enemy whenever the protagonist was not the
  one hardcoded PRESS-vest character;
* committing to a verb could not change the situation, so a fight either
  looped or killed you, and there was no way to win one;
* the action beat was rendered with the standoff's "locked-off two-shot"
  framing, so a landed punch came back as two people standing apart;
* a first-person world still demanded a two-shot, which left the player out
  of frame and handed the composition to the stranger.

Pure functions only - no network, no API key.

Run with:
    python -m unittest test_encounter_fight -v
"""

import os
import random
import re
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")

import encounter
import engine
import game_identity


def _brief(**over):
    base = {
        "character": {
            "label": "A hooded scavenger",
            "kind": "person",
            "look": "an oversized grease-stained yellow raincoat, hood pulled low",
            "stance": "hostile",
        },
        "danger": "swinging a length of rebar at your head",
        "stakes": "You do not walk away clean.",
        "place_hold": "rusted pier deck, fog",
    }
    base.update(over)
    return encounter.normalize_encounter_brief(base)


class TestTheLockedCharacterIsTheOneOnScreen(unittest.TestCase):
    """The brief invents a look before any image exists. The plate is what
    the player saw, so the plate is what every later frame must lock to.

    Taken verbatim from the session that prompted this: the brief said
    "yellow raincoat", the plate drew a green combat jacket, and the resolve
    prompt then named both at once.
    """

    PLATE = (
        'A woman wearing a blue "PRESS" cap and tactical vest, and a man in a '
        "green combat jacket and beanie stand in the foreground. They occupy "
        "a metallic deck."
    )
    PLAYER = {
        "enabled": True,
        "name": "Wren Alvarez",
        "wardrobe": 'blue "PRESS" cap, tactical vest',
        "appearance": "adult woman",
        "signature_gear": "1993 VHS camcorder",
    }

    def setUp(self):
        for target, value in (
            ("game_identity.authored_character", self.PLAYER),
            ("game_identity.display_name", "Wren Alvarez"),
        ):
            p = mock.patch(target, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def test_the_lock_follows_the_plate_not_the_invented_brief(self):
        out = encounter.adopt_plate_look(_brief(), self.PLATE)
        locked = out["character"]["locked_look"].lower()
        self.assertIn("green combat jacket", locked)
        self.assertNotIn("raincoat", locked)

    def test_the_look_and_the_nameplate_describe_the_same_person(self):
        out = encounter.adopt_plate_look(_brief(), self.PLATE)
        look = out["character"]["look"].lower()
        label = out["character"]["label"].lower()
        garment = re.search(r"\b(jacket|coat|vest|overcoat|raincoat)\b", label)
        self.assertIsNotNone(garment, f"nameplate names no garment: {label!r}")
        self.assertIn(garment.group(1), look)

    def test_a_plate_showing_only_the_player_keeps_the_briefs_look(self):
        """Promoting the protagonist to hostile is the worse failure."""
        player_only = (
            'A woman wearing a blue "PRESS" cap and tactical vest stands alone '
            "on a fog-covered metallic deck."
        )
        out = encounter.adopt_plate_look(_brief(), player_only)
        locked = out["character"]["locked_look"].lower()
        self.assertIn("raincoat", locked)
        self.assertNotIn("press", locked)


class TestThePlayerIsNotTheEnemy(unittest.TestCase):
    """Player detection was a literal set of one character's costume words."""

    JASON = {
        "enabled": True,
        "name": "Jason Fleece",
        "wardrobe": "olive field jacket, dark work pants, boots",
        "appearance": "adult man, short dark hair, stubble",
        "signature_gear": "1993 VHS camcorder",
    }

    def setUp(self):
        patcher = mock.patch("game_identity.authored_character", return_value=self.JASON)
        patcher.start()
        self.addCleanup(patcher.stop)
        name = mock.patch("game_identity.display_name", return_value="Jason Fleece")
        name.start()
        self.addCleanup(name.stop)

    def test_a_protagonist_who_wears_no_vest_is_still_recognised(self):
        self.assertTrue(
            encounter.look_clones_player(
                "The man on the left wears an olive field jacket and a shoulder bag"
            )
        )

    def test_naming_the_protagonist_counts(self):
        self.assertTrue(encounter.look_clones_player("Jason Fleece, camcorder raised"))

    def test_a_real_stranger_is_left_alone(self):
        self.assertFalse(
            encounter.look_clones_player("a man in a green combat jacket and beanie")
        )

    def test_where_they_stood_is_not_part_of_who_they_are(self):
        """A hard cut moves the camera, so "on the left" describes nobody."""
        look = encounter.plate_stranger_look(
            "The man on the left wears a brown baseball cap and a tan jacket",
            fallback="x",
        )
        self.assertNotIn("on the left", look)
        self.assertIn("brown baseball cap", look)

    def test_the_stranger_survives_a_plate_that_describes_the_player_first(self):
        seen = (
            "A third-person view over the shoulder of a man in an olive field "
            "jacket, facing a gaunt figure in a tattered grey overcoat gripping "
            "a length of rebar."
        )
        look = encounter.plate_stranger_look(seen, fallback="a wary figure").lower()
        self.assertIn("grey overcoat", look)
        self.assertNotIn("olive field jacket", look)


class TestTheFightEscalates(unittest.TestCase):
    """Rounds used to be independent and identical, so a verb changed nothing."""

    def test_pressing_a_confront_moves_the_other_body(self):
        """A landed confront ALWAYS shows. It either finishes them where they
        stand or staggers them; what it can never do is leave them `ready`.

        That last case is the one the player felt: the slate promises "ONE
        committed, extreme act - the thing that cannot be undone", the prose
        wrote the skull crushing, and the state machine then said nothing had
        happened and offered to do it again.
        """
        for seed in range(50):
            with self.subTest(seed=seed):
                self.assertIn(
                    encounter.advance_enemy_state(
                        "confront", "survive", "ready", rng=random.Random(seed)),
                    ("down", "staggered"))

    def test_a_committed_verb_can_end_it_where_it_stands(self):
        """Not merely possible — the common case. Needing two landed blows to
        win is what made an ordinary fight four rounds and two minutes long."""
        downs = sum(
            encounter.advance_enemy_state("confront", "survive", "ready",
                                          rng=random.Random(s)) == "down"
            for s in range(400))
        self.assertGreater(downs, 200, "a decisive verb usually is not decisive")

    def test_a_staggered_body_can_be_put_down(self):
        rng = random.Random(1)
        self.assertEqual(
            encounter.advance_enemy_state("confront", "survive", "staggered", rng=rng),
            "down",
        )

    def test_putting_them_down_ends_the_encounter(self):
        self.assertTrue(
            encounter.encounter_releases("survive", {"enemy_state": "down"})
        )

    def test_a_fight_that_is_still_going_does_not_release(self):
        self.assertFalse(
            encounter.encounter_releases("survive", {"enemy_state": "staggered"})
        )

    def test_pressing_an_advantage_beats_opening_cold(self):
        ready = encounter.encounter_outcome_weights("confront", enemy_state="ready")
        stag = encounter.encounter_outcome_weights("confront", enemy_state="staggered")
        self.assertGreater(stag["survive"], ready["survive"])
        self.assertLess(stag["die"], ready["die"])

    def test_opening_with_a_punch_is_not_a_coin_flip_on_death(self):
        w = encounter.encounter_outcome_weights("confront", "hostile", "person")
        self.assertLess(w["die"] / sum(w.values()), 0.15)

    def test_the_exchange_survives_a_normalize_round_trip(self):
        b = _brief()
        b["enemy_state"] = "staggered"
        b["round_no"] = 3
        again = encounter.normalize_encounter_brief(b)
        self.assertEqual(again["enemy_state"], "staggered")
        self.assertEqual(again["round_no"], 3)


class TestAFightIsTwoExchangesAtTheOutside(unittest.TestCase):
    """Reported as "encounters take far too long ... they need to last 1-2
    turns max". Every round is a real generation, so a four-round fight was two
    minutes of standing still, and the odds alone only ever made a long tail
    less likely rather than impossible."""

    def _play(self, lanes, kind="person", stance="hostile", seed=0, cap=12):
        """One fight to its release. Returns (rounds, outcome, enemy_state)."""
        rng = random.Random(seed)
        state, cond = "ready", "ok"
        for rnd in range(1, cap + 1):
            rolled = encounter.roll_encounter_outcome(
                lanes[(rnd - 1) % len(lanes)], stance=stance, kind=kind,
                condition=cond, fate="NORMAL", rng=rng,
                enemy_state=state, round_no=rnd)
            if encounter.encounter_releases(rolled["outcome"], rolled):
                return rnd, rolled["outcome"], rolled["enemy_state"]
            state, cond = rolled["enemy_state"], rolled["condition"]
        return cap + 1, "NEVER", state

    def test_no_lane_can_run_past_the_cap(self):
        for label, lanes, kind in (
            ("confront", ["confront"], "person"),
            ("evade", ["evade"], "person"),
            ("parley", ["parley"], "person"),
            ("rotating", ["confront", "evade", "parley"], "person"),
            # The lane that could not settle anything: talking at a thing that
            # does not talk. It has to end anyway.
            ("creature parley", ["parley"], "creature"),
            ("creature evade", ["evade"], "creature"),
        ):
            with self.subTest(lane=label):
                worst = max(self._play(lanes, kind=kind, seed=s)[0]
                            for s in range(400))
                self.assertLessEqual(worst, encounter.ENCOUNTER_MAX_ROUNDS)

    def test_most_fights_end_in_a_single_exchange(self):
        one = sum(self._play(["confront"], seed=s)[0] == 1 for s in range(400))
        self.assertGreater(one, 200)

    def test_the_last_exchange_settles_whatever_the_dice_say(self):
        """Not "is likely to" — the cap is the point."""
        for lane, kind in (("confront", "person"), ("parley", "person"),
                           ("evade", "person"), ("parley", "creature")):
            with self.subTest(lane=lane, kind=kind):
                for s in range(120):
                    rolled = encounter.roll_encounter_outcome(
                        lane, kind=kind, rng=random.Random(s),
                        enemy_state="ready",
                        round_no=encounter.ENCOUNTER_MAX_ROUNDS)
                    self.assertTrue(
                        encounter.encounter_releases(rolled["outcome"], rolled),
                        f"{lane}/{kind} did not end on the last exchange")

    def test_the_cap_never_rewrites_a_death(self):
        """How a run ends is the roll's call, not a pacing rule's."""
        rolled = encounter.roll_encounter_outcome(
            "evade", rng=_Rigged(die=True), enemy_state="ready",
            round_no=encounter.ENCOUNTER_MAX_ROUNDS)
        self.assertEqual(rolled["outcome"], "die")
        self.assertFalse(rolled["alive"])


class _Rigged:
    """An rng that always picks the last bucket — i.e. `die`."""

    def __init__(self, die=True):
        self.die = die

    def random(self):
        return 1.0 if self.die else 0.0

    def uniform(self, a, b):
        return b


class TestTheActionBeatIsShotLikeAFight(unittest.TestCase):

    def _resolve(self, **over):
        b = _brief()
        b.update(over)
        return encounter.build_encounter_resolve_prompt(
            b, "Drive your shoulder into him", "confront",
            over.pop("outcome", "survive"), setting="outdoor",
        )

    def test_the_action_beat_drops_the_standoff_framing(self):
        prompt = self._resolve(round_no=1, enemy_state="ready")
        self.assertNotIn("locked-off two-shot", prompt)
        self.assertIn("NOT two people standing apart", prompt)

    def test_the_camera_pushes_in_as_the_fight_goes_on(self):
        first = self._resolve(round_no=1, enemy_state="ready")
        later = self._resolve(round_no=3, enemy_state="staggered")
        self.assertIn("Medium shot", first)
        self.assertNotIn("Medium shot", later)

    def test_the_finish_reads_as_a_finish(self):
        prompt = self._resolve(round_no=2, enemy_state="down")
        self.assertIn("THIS IS THE FINISH", prompt)

    def test_the_standoff_plate_still_holds_the_place(self):
        prompt = encounter.build_encounter_plate_prompt(
            _brief(), img2img=True, setting="outdoor")
        self.assertIn("PLACE LOCK", prompt)


class TestFirstPersonDoesNotDrawASecondPlayer(unittest.TestCase):

    def _plate(self, shows_player):
        with mock.patch("game_identity.shows_character", return_value=shows_player):
            return encounter.build_encounter_plate_prompt(
                _brief(), img2img=True, setting="outdoor")

    def test_first_person_asks_for_one_figure_down_the_lens(self):
        prompt = self._plate(False)
        self.assertIn("FIRST-PERSON POV", prompt)
        self.assertIn("EXACTLY ONE person is visible", prompt)
        self.assertNotIn("TWO DISTINCT PEOPLE", prompt)

    def test_third_person_still_asks_for_the_two_shot(self):
        prompt = self._plate(True)
        self.assertIn("TWO DISTINCT PEOPLE", prompt)
        self.assertNotIn("FIRST-PERSON POV", prompt)


class TestAShotDescriptionIsNotAPerson(unittest.TestCase):
    """A vision description narrates the photograph, so it opens by naming the
    shot. That whole sentence was handed over as the stranger's appearance, so
    the plate prompt asked for "a newly introduced person named 'A person' - A
    first-person perspective shows a hand holding a two-way radio". Gemini drew
    the camera instruction: a first-person hand in a plaid sleeve, no standoff,
    and the player replaced by whoever owned that sleeve. One frame later the
    resolve copied faces off its reference and the right player came back,
    which is exactly how it looked in play - wrong on entry, right after the
    first choice."""

    CAMERA_TALK = ("A first-person perspective shows a hand holding a black "
                   "two-way radio with a digital watch on the wrist")

    def test_the_viewpoint_lead_in_is_not_kept_as_a_look(self):
        self.assertNotIn("first-person",
                         encounter.distinct_enemy_look(self.CAMERA_TALK).lower())

    def test_a_shot_with_no_person_in_it_falls_back_to_a_stranger(self):
        # The fallback is a pool now (DEFAULT_STRANGER_LOOKS) rather than the
        # one weathered man in a work coat, so what matters is that we landed
        # on one of them instead of on a description of the photograph.
        for shot in (self.CAMERA_TALK, "POV of a hand gripping a wrench"):
            with self.subTest(shot=shot):
                got = encounter.distinct_enemy_look(shot)
                self.assertTrue(encounter.is_default_stranger_look(got), got)

    def test_a_recast_level_never_meets_the_shipped_deserts_strangers(self):
        """The fallback pool is written for the Horizon desert — Horizon
        Industries, red dust, the mesa, mine cable — and it fired in a
        cyberpunk sub-level: the roster's "rogue police officer" tripped the
        clone rule (the player there IS an armoured officer) and what walked
        into the fight was "a woman in a bleached Horizon lab coat". A fallback
        may be generic; it may not be somebody else's world."""
        import game_identity
        from unittest import mock
        banned = ("horizon", "mesa", "red dust", "mine cable", "blackwood")
        with mock.patch.object(game_identity, "is_shipped_setting", return_value=False):
            for kind in ("person", "group", "creature", "anomaly", "character"):
                for seed in ("a", "b", "c", "d", "e", "f"):
                    got = encounter.default_stranger_look(seed, kind).lower()
                    for word in banned:
                        self.assertNotIn(word, got, f"{kind}/{seed}: {got}")
                    self.assertTrue(encounter.is_default_stranger_look(got), got)
        with mock.patch.object(game_identity, "is_shipped_setting", return_value=True):
            got = {encounter.default_stranger_look(str(i), "person") for i in range(40)}
            self.assertTrue(any("horizon" in g.lower() for g in got),
                            "the shipped level keeps its own strangers")

    def test_the_person_survives_when_the_framing_is_stripped_off(self):
        for narrated, person in (
            ("The image shows a man in a red jacket", "a man in a red jacket"),
            ("A wide shot depicts a guard in a long coat", "a guard in a long coat"),
            ("Close-up of a woman in a leather jacket", "a woman in a leather jacket"),
        ):
            self.assertEqual(person, encounter.distinct_enemy_look(narrated))

    def test_a_plain_description_is_left_alone(self):
        look = "a man in a grease-stained grey coverall and knit cap"
        self.assertEqual(look, encounter.distinct_enemy_look(look))

    def test_first_person_no_longer_certifies_itself_as_a_person(self):
        # A hyphen is a word boundary, so \bperson\b matched inside
        # "first-person" and the phrase that broke the frame passed every
        # "is this a human?" check in the module.
        self.assertEqual("", encounter._first_person_noun("A first-person perspective"))
        self.assertEqual("man", encounter._first_person_noun("a man in a coat"))

    def test_the_brief_built_from_a_frame_never_carries_the_framing(self):
        brief = encounter.brief_from_vision({"description": self.CAMERA_TALK,
                                             "setting": "outdoor industrial yard"})
        self.assertNotIn("perspective", brief["character"]["look"].lower())
        self.assertNotIn("first-person", brief["character"]["look"].lower())

    def test_the_plate_prompt_cannot_be_handed_a_camera_move(self):
        brief = _brief(character={"label": "A person", "kind": "person",
                                  "look": self.CAMERA_TALK, "stance": "hostile"})
        with mock.patch("game_identity.shows_character", return_value=True):
            prompt = encounter.build_encounter_plate_prompt(
                brief, img2img=True, setting="outdoor")
        self.assertNotIn("first-person perspective", prompt.lower())
        self.assertIn("TWO DISTINCT PEOPLE", prompt)


class TestThePlayerSurvivesTheStandoffPlate(unittest.TestCase):
    """The plate locked the PLACE and then said "ADD the new character", which
    left the people unprotected: the newcomer was drawn large in front and the
    player was dropped out of their own standoff. The resolve never had this
    problem because hold_cast makes it copy faces out of the reference."""

    def _plate(self, shows_player, img2img=True):
        with mock.patch("game_identity.shows_character", return_value=shows_player):
            return encounter.build_encounter_plate_prompt(
                _brief(), img2img=img2img, setting="outdoor")

    def test_the_player_is_copied_off_the_reference_not_reinvented(self):
        prompt = self._plate(True)
        self.assertIn("CARRY THE PLAYER OVER", prompt)
        self.assertIn("IN FRAME", prompt)

    def test_only_one_person_joins_the_photograph(self):
        self.assertIn("EXACTLY ONE new person", self._plate(True))

    def test_first_person_worlds_are_left_alone(self):
        # There is no player body to carry over when the camera is their eyes.
        self.assertNotIn("CARRY THE PLAYER OVER", self._plate(False))

    def test_text_to_image_has_no_reference_to_carry_anyone_from(self):
        self.assertNotIn("CARRY THE PLAYER OVER", self._plate(True, img2img=False))


class TestTheEnemyCannotWearThePlayersClothes(unittest.TestCase):
    """By the third frame the enemy was wearing the player's vest and cap.

    Two things let that happen. The clone guard only knew the player from
    the character sheet, so an outfit the image model invented — a green
    vest on a protagonist written as "olive field jacket" — belonged to
    nobody and was free to migrate. And the cast lock ended with a
    hardcoded "must not wear that vest, cap, or PRESS gear", left over from
    a different protagonist, which introduced a vest and a cap to a world
    that had neither.
    """

    # One person is in an exploration frame, and it is the player.
    FRAME = ("A man in a green quilted vest and a dark baseball cap stands "
             "in a dirt yard, holding a camcorder, chain-link fence and a "
             "rusted pickup truck behind him.")

    def setUp(self):
        encounter._LOOK_READ_CACHE.clear()
        self._seen = mock.patch.object(
            encounter, "observed_player_look", return_value=self.FRAME)
        self._seen.start()
        self.addCleanup(self._seen.stop)
        self.addCleanup(encounter._LOOK_READ_CACHE.clear)

    def test_an_outfit_the_sheet_never_mentioned_is_still_the_players(self):
        self.assertTrue(encounter.look_clones_player(
            "a man in a green quilted vest and a dark baseball cap"))

    def test_taking_only_part_of_it_still_counts(self):
        self.assertTrue(encounter.look_clones_player("a man in a baseball cap"))

    def test_a_stranger_in_their_own_clothes_is_left_alone(self):
        for look in ("a man in a grease-stained grey coverall",
                     "a woman in a torn red windbreaker"):
            with self.subTest(look=look):
                self.assertFalse(encounter.look_clones_player(look))
                self.assertEqual(look, encounter.distinct_enemy_look(look))

    def test_the_scenery_around_the_player_is_not_their_wardrobe(self):
        # Learning from the whole frame description would make the yard part
        # of the outfit, and then anyone standing in it reads as the player.
        self.assertFalse(encounter.look_clones_player(
            "a man beside a chain-link fence near a rusted truck"))

    def test_a_stolen_look_never_reaches_the_prompt(self):
        brief = _brief()
        encounter.adopt_plate_look(
            brief, "A man in a green quilted vest and a dark baseball cap "
                   "swings at another man in a green quilted vest.")
        locked = brief["character"].get("locked_look") or ""
        self.assertFalse(encounter.look_clones_player(locked), locked)

    def test_the_cast_lock_bans_the_outfit_that_exists(self):
        lock = encounter.player_cast_lock()
        self.assertIn("green quilted vest", lock)
        self.assertNotIn("PRESS", lock)

    def test_the_wardrobe_phrase_stops_at_the_clothes(self):
        # An unpunctuated description runs straight past the outfit.
        got = encounter.observed_player_wardrobe()
        self.assertNotIn("yard", got)
        self.assertIn("vest", got)


class TestThePlateLocksOntoTheStrangerNotThePlayer(unittest.TestCase):
    """The decisive step in the cast-rotation bug.

    A plate is a two-shot and the prompt introduces the player first, so
    "take the first clause that mentions clothes" picked the protagonist
    nearly every time and wrote their outfit into the enemy's locked look.
    From then on every frame drew the enemy in the player's clothes. The
    brief invented the stranger before any pixels existed, so it is the
    evidence for which described person is not the player.
    """

    def setUp(self):
        # Without this the guard learns from whatever the live default
        # session last rendered, so the result changes between runs.
        encounter._LOOK_READ_CACHE.clear()
        p = mock.patch.object(encounter, "observed_player_look", return_value="")
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(encounter._LOOK_READ_CACHE.clear)

    def _locked(self, plate, invented):
        return encounter.plate_stranger_look(plate, fallback=invented).lower()

    def test_the_player_described_first_does_not_become_the_enemy(self):
        got = self._locked(
            "A man in a green quilted vest and a dark baseball cap faces an "
            "older man in a plaid shirt holding a shotgun.",
            "an older man wearing a baseball cap and plaid shirt")
        self.assertIn("plaid", got)
        self.assertNotIn("quilted", got)

    def test_two_people_in_one_comma_free_sentence_are_separated(self):
        # Punctuation alone left both people in a single clause.
        got = self._locked(
            "A man in a green vest faces an older man in a plaid shirt.",
            "an older man in a plaid shirt")
        self.assertIn("plaid", got)
        self.assertNotIn("green vest", got)

    def test_the_player_described_second_is_also_avoided(self):
        # The fix must weigh who the clause resembles, not flip the order.
        got = self._locked(
            "A man in a heavy rubber apron blocks the path while a man in an "
            "olive field jacket raises a camcorder.",
            "a worker in a heavy rubber apron")
        self.assertIn("apron", got)
        self.assertNotIn("olive", got)

    def test_the_connector_is_not_part_of_the_costume(self):
        got = self._locked(
            "A man in a green vest, and another man in a grease-stained "
            "coverall steps toward him.",
            "a mechanic in a grease-stained coverall")
        self.assertIn("coverall", got)
        for junk in ("another", "and ", "steps"):
            self.assertNotIn(junk, got)

    def test_one_persons_description_is_not_cut_at_its_commas(self):
        # "The man has long, matted hair and a torn flannel shirt" was being
        # split at the comma, and the stub "The man has long" still held a
        # person noun — so the enemy was locked to the phrase "The man has
        # long" and every later frame was prompted with it.
        # The guard was written as `assertNotIn("the man has long,", got + ",")`
        # to catch a value that STOPS at "long". The comma it appends is also
        # the comma inside the intact sentence, so it fired on the correct
        # output and could never pass alongside the "flannel" assertion above.
        got = self._locked(
            "The man has long, matted hair and a torn flannel shirt.",
            "a dishevelled man in a torn flannel shirt")
        self.assertIn("flannel", got)
        self.assertIn("matted hair", got)
        self.assertFalse(got.rstrip(" .,").endswith("the man has long"), got)


class TestTheObservedLookComesFromTheImageNotTheSheet(unittest.TestCase):
    """A history entry carries two description-shaped fields and only one of
    them is evidence. ``vision_analysis`` is what vision saw in the rendered
    frame; ``vision_dispatch`` beside it is the character sheet restated as
    prose, so it always agrees with the sheet and can never reveal that the
    drawing has drifted. Reading the wrong one makes the whole guard inert.
    """

    DRAWN = ("A man in a green quilted vest and a dark baseball cap stands "
             "in a dirt yard holding a camcorder.")
    SHEET = ("Jason Fleece is in frame — investigative photojournalist, "
             "olive field jacket, dark work pants, boots.")

    def _look(self, hist):
        import engine
        encounter._LOOK_READ_CACHE.clear()
        with mock.patch.object(engine, "_load_history", return_value=hist):
            encounter.set_look_session("default")
            return encounter.observed_player_look()

    def tearDown(self):
        encounter._LOOK_READ_CACHE.clear()

    def test_the_rendered_frame_wins_over_the_restated_sheet(self):
        got = self._look([{"choice": "walk", "vision_analysis": self.DRAWN,
                           "vision_dispatch": self.SHEET}])
        self.assertEqual(self.DRAWN, got)

    def test_the_restated_sheet_is_not_treated_as_evidence(self):
        got = self._look([{"choice": "walk", "vision_dispatch": self.SHEET}])
        self.assertEqual("", got)

    def test_two_person_frames_are_skipped(self):
        # A resolve frame cannot say which of the two people is the player.
        got = self._look([
            {"choice": "walk", "vision_analysis": self.DRAWN},
            {"choice": "__encounter_resolve__", "encounter": True,
             "vision_analysis": "Two men in green vests grapple in the dirt."},
        ])
        self.assertEqual(self.DRAWN, got)


class TestNoFrameMeansNoGuessing(unittest.TestCase):
    """With nothing rendered yet there is no observed look, and the guard
    must fall back to the sheet rather than start refusing everything."""

    def setUp(self):
        encounter._LOOK_READ_CACHE.clear()
        p = mock.patch.object(encounter, "observed_player_look", return_value="")
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(encounter._LOOK_READ_CACHE.clear)

    def test_ordinary_strangers_still_pass(self):
        for look in ("a man in a grease-stained grey coverall",
                     "a woman in a torn red windbreaker",
                     "a man in a green quilted vest"):
            with self.subTest(look=look):
                self.assertFalse(encounter.look_clones_player(look))

    def test_the_sheet_is_still_enforced(self):
        # This asserted against "an olive field jacket carrying a camcorder",
        # which was the SHIPPED protagonist when it was written. The shipped
        # sheet is now the traveler, so the test was measuring a retired
        # character and failing on a guard that was working. Bring the sheet
        # with us: what matters is that a distinctive authored item is
        # enforced, whoever happens to ship.
        import game_identity
        sheet = {"name": "Wren Ashlock", "wardrobe": "an olive field jacket",
                 "signature_gear": "a camcorder", "appearance": "adult"}
        with mock.patch.object(game_identity, "authored_character",
                               return_value=sheet):
            self.assertTrue(encounter.look_clones_player(
                "a man in an olive field jacket carrying a camcorder"))


class TestTheEncounterIsTheNextShotNotANewProduction(unittest.TestCase):
    """Walking into a fight used to cut to a different-looking film.

    The plate is handed the previous frame as an img2img reference and was
    then told, twice, to restage it "from a new lens" — with the editor's
    camera deliberately stripped out via ``include_vantage=False``. So the
    one surface that most needed to feel continuous was the only one that
    threw the world's camera away.
    """

    def _plate(self):
        with mock.patch("game_identity.shows_character", return_value=True):
            return encounter.build_encounter_plate_prompt(
                _brief(), img2img=True, setting="outdoor")

    def test_the_editor_camera_survives_into_the_fight(self):
        vantage = "third-person follow-cam several metres behind the walker"
        with mock.patch("game_identity.vantage", return_value=vantage), \
             mock.patch("game_identity.is_active", return_value=True):
            prompt = self._plate()
        self.assertIn(vantage, prompt)

    def test_nothing_asks_for_a_different_camera(self):
        prompt = self._plate().lower()
        for banned in ("new lens", "restaged", "new camera"):
            self.assertNotIn(banned, prompt)

    def test_the_reference_frame_is_held_not_reshot(self):
        prompt = self._plate()
        self.assertIn("PLACE LOCK", prompt)
        self.assertIn("same camera", prompt)

    def test_the_beat_is_the_one_before_violence(self):
        prompt = self._plate().lower()
        self.assertTrue(
            any(w in prompt for w in ("about to", "stillness", "before")),
            "the plate should read as anticipation, not a landed hit")
        for landed in ("no choke", "nothing has landed"):
            self.assertIn(landed, prompt)

    def test_the_same_order_is_not_given_twice(self):
        # The authored anchor used to repeat the place lock the code appends,
        # so the model got "do not teleport" twice and a pile of negations.
        sentences = [s.strip().lower()
                     for s in re.split(r"(?<=[.!?])\s+", self._plate())
                     if len(s.strip()) > 12]
        dupes = {s for s in sentences if sentences.count(s) > 1}
        self.assertEqual(set(), dupes)


class TestAnAnchorNeverEndsMidThought(unittest.TestCase):
    """``look_line`` cut the art direction at 180 characters on a word
    boundary and bolted a full stop on, which turned "Threats are human or
    biological - never robots or future tech" into "…biological - never." A
    dangling negation with no object, riding in every image prompt the game
    sent, encounter or not."""

    def _look(self, art):
        with mock.patch.dict(game_identity.PROMPTS,
                             {"image_art_direction": art}, clear=False):
            return game_identity.look_line()

    def test_the_negation_keeps_the_thing_it_negates(self):
        art = ("1993 American Southwest industrial horror. Period technology "
               "only: CRT monitors, fluorescent tubes, chain-link, rusted "
               "plant, 1990s trucks. Threats are human or biological - never "
               "robots or future tech. Touchstones: The X-Files.")
        got = self._look(art)
        self.assertNotIn("never.", got)
        self.assertTrue(got.endswith("."), got)

    def test_it_prefers_a_whole_sentence(self):
        art = "A. " + ("word " * 60) + "tail."
        got = self._look(art)
        self.assertLessEqual(len(got), 181)
        self.assertTrue(got.endswith("."), got)

    def test_a_word_cut_still_never_dangles(self):
        art = "x" * 120 + " and the quick brown fox " + "y" * 90
        got = self._look(art)
        self.assertFalse(
            re.search(r"\b(?:and|the|or|of|to|a|never)\.$", got), got)


class TestWinningAFightGetsYouOutOfIt(unittest.TestCase):
    """An encounter's only exit is the aftermath turn reporting back as a
    `player_choice_prompt` feed item. The client refused that item unless
    `releasePending` was already set, and it is not set until after
    waitSceneReady() and the 1350ms verdict card have both finished. A won
    fight skips image generation server-side, so the aftermath could land
    inside that window, get dropped, and never be re-sent - finish() never
    ran, so the aftermath frame was never applied and the fight stayed open
    forever. The global turn watchdog stands down while an encounter is busy,
    so nothing else could recover it either."""

    CLIENT = open("static/js/standalone.js", encoding="utf-8").read()

    def test_the_aftermath_signal_is_taken_whenever_it_lands(self):
        self.assertNotIn('item.type === "player_choice_prompt" && releasePending',
                         self.CLIENT)
        self.assertIn('if (item.type === "player_choice_prompt") {', self.CLIENT)

    def test_an_early_signal_is_held_instead_of_dropped(self):
        body = self.CLIENT.split("function requestFinish(result) {", 1)[1]
        body = body.split("\n    }", 1)[0]
        # The old guard returned outright when the release was not armed yet.
        self.assertNotIn("if (!releasePending && !(result && result.survived === false)) return;",
                         body)
        self.assertIn("pendingFinish = payload;", body)

    def test_the_held_signal_is_consumed_once_the_release_arms(self):
        tail = self.CLIENT.split("releasePending = true;\n      if (pendingFinish)", 1)
        self.assertEqual(2, len(tail), "release no longer drains pendingFinish")
        self.assertIn("finish(next);", tail[1][:200])

    def test_a_released_fight_cannot_hang_forever(self):
        self.assertIn("function armReleaseWatchdog()", self.CLIENT)
        self.assertIn("armReleaseWatchdog();", self.CLIENT)
        self.assertIn("RELEASE_WATCHDOG_MS", self.CLIENT)

    def test_the_backstop_is_cancelled_on_every_exit(self):
        # A stale timer firing into a new fight would eject the player from it.
        self.assertGreaterEqual(self.CLIENT.count("clearReleaseWatchdog()"), 4)

    def test_only_escape_needs_a_fresh_world_frame(self):
        # This is what makes a won fight's aftermath fast enough to race the
        # verdict ceremony in the first place.
        self.assertTrue(encounter.encounter_turn_skip_image("survive"))
        self.assertTrue(encounter.encounter_turn_skip_image("die"))
        self.assertFalse(encounter.encounter_turn_skip_image("escape"))


class TestTheSlateOffersThreeDifferentAnswers(unittest.TestCase):
    """Violence, escape, peace — not three verbs from the same scuffle.

    The third lane used to be "use": turn their grip or their weapon against
    them, which is a second way to hit someone. Every slate came out as three
    variations on grappling and none of them was a decision.
    """

    def test_the_lanes_are_violence_escape_and_peace(self):
        self.assertEqual(encounter.ENCOUNTER_LANES,
                         ("confront", "evade", "parley"))

    def test_the_asked_for_slate_is_not_three_fighting_moves(self):
        for text in (encounter.DEFAULT_CHOICE_INSTRUCTIONS,
                     encounter.DEFAULT_CHOICE_OVERLAY):
            low = text.lower()
            self.assertIn("parley" if "parley" in low else "peace", low)
            self.assertNotIn("their momentum", low)
            self.assertNotIn("(3) turn the moment against", low)
        instructions = encounter.DEFAULT_CHOICE_INSTRUCTIONS.lower()
        # The old confront lane asked for a shove, which is what made the
        # violence option feel like nothing.
        self.assertIn("extreme", instructions)
        self.assertIn("never shove", instructions)
        self.assertIn("keys confront, evade, parley", instructions)

    def test_the_schema_asks_for_the_peace_lane(self):
        props = encounter.ENCOUNTER_CHOICE_SCHEMA["properties"]
        self.assertIn("parley", props)
        self.assertNotIn("use", props)
        self.assertEqual(sorted(encounter.ENCOUNTER_CHOICE_SCHEMA["required"]),
                         ["confront", "evade", "parley"])

    def test_a_dict_of_lanes_survives_the_rename(self):
        slate = encounter._parse_choice_payload({
            "confront": "Drive the pipe through him",
            "evade": "Bolt for the fence line",
            "parley": "Hand over the tape",
        })
        self.assertEqual([c["lane"] for c in slate],
                         ["confront", "evade", "parley"])

    def test_a_model_answering_with_the_old_key_keeps_its_slate(self):
        slate = encounter._parse_choice_payload({
            "confront": "Drive the pipe through him",
            "evade": "Bolt for the fence line",
            "use": "Hand over the tape",
        })
        self.assertEqual(len(slate), 3)
        self.assertEqual(slate[-1]["text"], "Hand over the tape")
        self.assertEqual(slate[-1]["lane"], "parley")

    def test_peaceful_wording_is_read_as_the_peace_lane(self):
        for verb in ("Hand over the tape", "Tell them who sent you",
                     "Give them the camera", "Stand down and talk"):
            with self.subTest(verb=verb):
                self.assertEqual(encounter.classify_encounter_lane(verb),
                                 "parley")

    def test_extreme_wording_is_read_as_violence(self):
        for verb in ("Smash the lamp into him", "Choke him out",
                     "Put the foreman down"):
            with self.subTest(verb=verb):
                self.assertEqual(encounter.classify_encounter_lane(verb),
                                 "confront")

    def test_the_fallback_slate_also_fans_three_ways(self):
        slate = encounter.fallback_encounter_choices(
            {"character": {"label": "A site foreman", "look": "a man in a hard hat"}})
        self.assertEqual([c["lane"] for c in slate],
                         ["confront", "evade", "parley"])

    def test_talking_can_settle_a_person_but_never_a_creature(self):
        rng = random.Random(11)
        person = {encounter.advance_enemy_state(
            "parley", "survive", "ready", rng=rng,
            stance="opportunistic", kind="person") for _ in range(200)}
        self.assertIn("standing_down", person)

        rng = random.Random(11)
        creature = {encounter.advance_enemy_state(
            "parley", "survive", "ready", rng=rng,
            stance="hostile", kind="creature") for _ in range(200)}
        self.assertNotIn("standing_down", creature)

    def test_a_settled_body_stays_settled(self):
        rng = random.Random(3)
        for _ in range(50):
            self.assertEqual(
                encounter.advance_enemy_state("confront", "survive",
                                              "standing_down", rng=rng),
                "standing_down")

    def test_peace_is_the_safest_lane_but_not_a_free_pass(self):
        talk = encounter.encounter_outcome_weights("parley", stance="hostile")
        hit = encounter.encounter_outcome_weights("confront", stance="hostile")
        self.assertLess(talk["die"] + talk["wounded"],
                        hit["die"] + hit["wounded"])
        beast = encounter.encounter_outcome_weights("parley", kind="creature")
        self.assertGreater(beast["wounded"], talk["wounded"])

    def test_a_clean_escape_or_parley_is_not_thrown_away(self):
        """The filter that swaps landscape choices for canned text only knew
        the old grappling verbs, so it replaced the model's real slate."""
        brief = {"character": {"label": "A man in plaid shirt"}}
        for text in ("Sprint away dropping the bag", "Toss him your camera bag",
                     "Hand over the camera gear", "Shatter his skull",
                     "Flee while dropping the camera"):
            with self.subTest(text=text):
                self.assertTrue(encounter.choice_addresses_threat(text, brief))

    def test_landscape_choices_are_still_replaced(self):
        brief = {"character": {"label": "A man in plaid shirt"}}
        for text in ("Sprint toward the mining processor",
                     "Vault over the debris pile",
                     "Climb the chain-link fence"):
            with self.subTest(text=text):
                self.assertFalse(encounter.choice_addresses_threat(text, brief))

    def test_a_pronoun_hiding_inside_another_word_does_not_count(self):
        # "her" lives inside "other" and "he" inside "shed"; a substring
        # test called these choices grounded when they name no one.
        brief = {"character": {"label": "A drifter"}}
        self.assertFalse(
            encounter.choice_addresses_threat("Cross to the other shed", brief))

    def test_the_whole_slate_survives_the_filter(self):
        brief = {"character": {"label": "A man in plaid shirt"}}
        slate = encounter.prefer_threat_choices([
            {"text": "Drive a blade through him", "lane": "confront"},
            {"text": "Sprint past him into brush", "lane": "evade"},
            {"text": "Toss your camera bag away", "lane": "parley"},
        ], brief)
        self.assertEqual([c["text"] for c in slate],
                         ["Drive a blade through him",
                          "Sprint past him into brush",
                          "Toss your camera bag away"])

    def test_violence_is_not_allowed_to_conjure_a_weapon(self):
        low = encounter.DEFAULT_CHOICE_INSTRUCTIONS.lower()
        self.assertIn("not arm them with a weapon", low)

    def test_the_stakes_line_reports_a_body_that_gave_up(self):
        brief = {"character": {"label": "A site foreman"},
                 "enemy_state": "standing_down"}
        line = encounter.stakes_after_verb(brief, "Hand over the tape",
                                           "parley", "survive")
        self.assertIn("stood down", line.lower())
        self.assertNotIn("still here", line.lower())

    def test_walking_away_from_a_deal_is_not_narrated_as_a_scramble(self):
        brief = {"character": {"label": "A man"}}
        talked = encounter.stakes_after_verb(brief, "Toss him your memory card",
                                              "parley", "escape")
        ran = encounter.stakes_after_verb(brief, "Sprint past the truck",
                                           "evade", "escape")
        self.assertIn("let you go", talked.lower())
        self.assertIn("broke clear", ran.lower())


class TestBeingSeenCostsYouTheFight(unittest.TestCase):
    """Detection was handed to the narrator every turn and to nothing else. A
    run could be hunted across three locations and the moment something
    actually walked up it rolled exactly like a run that had never been seen —
    which is what made the dial a readout rather than a stake.

    Hidden is initiative: it did not know you were there. Hunted is the
    inverse — it is here BECAUSE it followed you, so running is the one answer
    it has already solved for."""

    def test_the_jump_is_worth_having(self):
        hidden = encounter.encounter_outcome_weights("confront", detection=0)
        hunted = encounter.encounter_outcome_weights("confront", detection=3)
        self.assertGreater(hidden["survive"], hunted["survive"])
        self.assertLess(hidden["die"], hunted["die"])

    def test_you_cannot_outrun_what_followed_you_here(self):
        hidden = encounter.encounter_outcome_weights("evade", detection=0)
        hunted = encounter.encounter_outcome_weights("evade", detection=3)
        self.assertGreater(hidden["escape"], hunted["escape"])

    def test_escaping_gets_harder_every_rung_of_the_ladder(self):
        escapes = [encounter.encounter_outcome_weights("evade", detection=d)["escape"]
                   for d in range(4)]
        self.assertEqual(escapes, sorted(escapes, reverse=True))
        self.assertEqual(len(set(escapes)), 4)

    def test_a_lane_that_cannot_kill_you_still_cannot(self):
        """`die: 0` on the safe lanes is a design statement about the lane, not
        a number that happened to round down. Being watched on the way in has
        to cost you through escape and wounded, not quietly make talking to an
        opportunist lethal."""
        for lane, stance in (("parley", "opportunistic"),
                             ("confront", "opportunistic")):
            for level in range(4):
                with self.subTest(lane=lane, detection=level):
                    self.assertEqual(encounter.encounter_outcome_weights(
                        lane, stance=stance, detection=level)["die"], 0)

    def test_an_unknown_level_rolls_exactly_as_it_always_did(self):
        """Encounter records written before this existed have no level on
        them, and `hidden` is a real bonus rather than the baseline — so
        defaulting an unknown to 0 would hand every legacy fight a stealth
        advantage it never earned."""
        for lane in encounter.ENCOUNTER_LANES:
            with self.subTest(lane=lane):
                self.assertEqual(
                    encounter.encounter_outcome_weights(lane, detection=None),
                    encounter.encounter_outcome_weights(lane))
                self.assertNotEqual(
                    encounter.encounter_outcome_weights(lane, detection=3),
                    encounter.encounter_outcome_weights(lane))

    def test_the_odds_never_go_negative_or_empty(self):
        for lane in encounter.ENCOUNTER_LANES:
            for stance in encounter.ENCOUNTER_STANCES:
                for level in range(4):
                    w = encounter.encounter_outcome_weights(
                        lane, stance=stance, condition="wounded",
                        fate="UNLUCKY", detection=level)
                    self.assertTrue(all(v >= 0 for v in w.values()), w)
                    self.assertGreater(sum(w.values()), 0)

    def test_the_fight_resolves_against_the_level_it_opened_on(self):
        """A multi-round exchange must not get easier because the dial cooled
        between rounds. You do not become un-followed halfway through being
        caught."""
        src = (Path(__file__).parent / "encounter.py").read_text(
            encoding="utf-8", errors="replace")
        self.assertIn('opened_at = enc.get("detection")', src)
        self.assertIn("detection=opened_at", src)

    def test_the_level_survives_the_rebuild_after_the_plate_lands(self):
        """align_brief_to_plate re-normalizes the brief from a fixed set of
        fields, which is how `_sequence` used to get dropped. A level only
        stamped in api_begin would be gone before api_resolve looked."""
        brief = encounter.normalize_encounter_brief({
            "character": {"label": "A site foreman", "stance": "hostile"},
            "danger": "swinging a bar", "stakes": "Move.", "detection": 3,
        })
        self.assertEqual(brief["detection"], 3)
        self.assertEqual(
            encounter.align_brief_to_plate(brief).get("detection"), 3)

    def test_a_brief_with_no_level_does_not_invent_one(self):
        brief = encounter.normalize_encounter_brief(
            {"character": {"label": "A drifter"}, "danger": "x", "stakes": "y"})
        self.assertIsNone(brief.get("detection"))

    def test_the_encounter_opens_where_the_dial_says_it_does(self):
        """Hidden has to buy the beat BEFORE being noticed, or hiding is just
        a number that goes down."""
        self.assertIn("HAS NOT BEEN SEEN", encounter._DETECTION_BRIEF[0])
        self.assertIn("HUNTING", encounter._DETECTION_BRIEF[3])
        self.assertEqual(len(encounter._DETECTION_BRIEF), 4)


class TestWhatWalksUpIsWhatYouSaw(unittest.TestCase):
    """The roster draw is a coincidence: something wandered across your path.
    Once the world knows where the player is that is the wrong story, so at
    alerted or worse the encounter becomes the thing the frame last saw —
    through the `target` path build_encounter_brief already has, which skips
    the draw and briefs the thing in the photograph."""

    def test_a_hidden_run_still_rolls_the_roster(self):
        st = {"turn_count": 2,
              "detection_witness": {"watchers": 1, "facing": "facing",
                                    "distance": "near", "label": "a guard",
                                    "at_turn": 2, "source": "scan"}}
        with mock.patch.object(engine, "_load_state", return_value=st):
            self.assertIsNone(encounter.onscreen_threat_target("s", detection=0))
            self.assertIsNone(encounter.onscreen_threat_target("s", detection=1))

    def test_an_alerted_run_meets_what_it_was_looking_at(self):
        st = {"turn_count": 2,
              "detection_witness": {"watchers": 1, "facing": "facing",
                                    "distance": "near", "label": "a site foreman",
                                    "at_turn": 2, "source": "scan"}}
        with mock.patch.object(engine, "_load_state", return_value=st):
            for level in (2, 3):
                target = encounter.onscreen_threat_target("s", detection=level)
                self.assertEqual(target["label"], "a site foreman")

    def test_a_stale_or_nameless_witness_leaves_the_roll_alone(self):
        """The scene analysis reports a count but no nouns, and last turn's
        frame is somewhere the player no longer is. Either way the roster draw
        is still the honest answer."""
        nameless = {"turn_count": 2, "detection_witness": {
            "watchers": 2, "facing": "facing", "distance": "near",
            "label": "", "at_turn": 2, "source": "vision"}}
        stale = {"turn_count": 9, "detection_witness": {
            "watchers": 2, "facing": "facing", "distance": "near",
            "label": "a guard", "at_turn": 2, "source": "scan"}}
        for st in (nameless, stale, {"turn_count": 0}):
            with mock.patch.object(engine, "_load_state", return_value=st):
                self.assertIsNone(
                    encounter.onscreen_threat_target("s", detection=3))

    def test_unreadable_state_does_not_stop_the_encounter(self):
        with mock.patch.object(engine, "_load_state", side_effect=OSError("gone")):
            self.assertIsNone(encounter.onscreen_threat_target("s", detection=3))
            self.assertEqual(encounter.session_detection("s"), 0)


class TestAFightActuallyEnds(unittest.TestCase):
    """`encounter_releases` only fires on escape, death, or the other body
    going down, and `advance_enemy_state` is the only thing that reaches
    "down". Walk the real roll loop so a change to either cannot quietly make
    confrontations unwinnable."""

    def _rounds_to_end(self, lane, trials=3000, cap=60):
        rng = random.Random(1234)
        worst = 0
        for _ in range(trials):
            enemy, cond, n = "ready", "ok", 0
            while n < cap:
                n += 1
                r = encounter.roll_encounter_outcome(
                    lane, stance="hostile", kind="person", condition=cond,
                    fate="NORMAL", enemy_state=enemy, round_no=n, rng=rng)
                enemy, cond = r["enemy_state"], r["condition"]
                if encounter.encounter_releases(r["outcome"], {"enemy_state": enemy}):
                    break
            else:
                self.fail(f"{lane} never ended within {cap} rounds")
            worst = max(worst, n)
        return worst

    def test_every_lane_terminates(self):
        for lane in encounter.ENCOUNTER_LANES:
            with self.subTest(lane=lane):
                self.assertLess(self._rounds_to_end(lane), 40)

    def test_putting_them_down_is_a_way_out(self):
        self.assertTrue(encounter.encounter_releases("survive", {"enemy_state": "down"}))
        self.assertFalse(encounter.encounter_releases("survive", {"enemy_state": "staggered"}))

    def test_talking_them_down_is_also_a_way_out(self):
        self.assertTrue(
            encounter.encounter_releases("survive", {"enemy_state": "standing_down"}))

    def test_pressing_a_staggered_body_can_finish_it(self):
        rng = random.Random(7)
        got = {encounter.advance_enemy_state("confront", "survive", "staggered", rng=rng)
               for _ in range(80)}
        self.assertIn("down", got)

    def test_pressing_the_same_verb_converges(self):
        """A flat per-round chance has a long tail, and a playtest sat in it:
        "crush his skull with boot" landed four times running, the prose said
        the skull yielded and the player was standing over him, and the state
        machine still said `staggered` with the Moment still open. However good
        the individual beats are, a fight that does not answer reads as broken.
        """
        rng = random.Random(99)
        unsettled = 0
        for _ in range(3000):
            enemy, cond = "ready", "ok"
            for n in range(1, 5):
                r = encounter.roll_encounter_outcome(
                    "confront", stance="hostile", kind="person", condition=cond,
                    fate="NORMAL", enemy_state=enemy, round_no=n, rng=rng)
                enemy, cond = r["enemy_state"], r["condition"]
                if encounter.encounter_releases(r["outcome"], {"enemy_state": enemy}):
                    break
            else:
                unsettled += 1
        # Was ~1 fight in 10 before the per-round gain; the harness caps its
        # encounter probe at four exchanges and filed every one of those.
        self.assertLess(unsettled / 3000.0, 0.04,
                        f"{unsettled}/3000 fights still open after four "
                        f"committed confronts")

    def test_the_first_exchange_gets_the_base_odds(self):
        """The gain is for pressing an advantage, not for swinging once."""
        for base in (encounter.CONFRONT_STAGGER_CHANCE,
                     encounter.CONFRONT_DOWN_CHANCE):
            with self.subTest(base=base):
                self.assertAlmostEqual(encounter._settle_chance(base, 1), base)

    def test_two_committed_verbs_usually_finish_a_fight(self):
        """What advance_enemy_state's docstring has always promised. At the
        old 0.55/0.62 it happened about a third of the time, so the prose ran
        away from the state machine: a playtest landed a crate to the face and
        two skulls against monitors and the man was still `ready`."""
        rng = random.Random(2024)
        finished = 0
        trials = 3000
        for _ in range(trials):
            enemy, cond = "ready", "ok"
            for n in (1, 2):
                r = encounter.roll_encounter_outcome(
                    "confront", stance="hostile", kind="person", condition=cond,
                    fate="NORMAL", enemy_state=enemy, round_no=n, rng=rng)
                enemy, cond = r["enemy_state"], r["condition"]
                if encounter.encounter_releases(r["outcome"], {"enemy_state": enemy}):
                    finished += 1
                    break
        self.assertGreater(finished / trials, 0.6,
                           f"only {finished}/{trials} fights ended in two verbs")

    def test_the_gain_is_capped(self):
        self.assertLessEqual(encounter._settle_chance(0.62, 50),
                             encounter.ENEMY_STATE_MAX_CHANCE)


class TestAFightKnowsWhatWorldItIsIn(unittest.TestCase):
    """An encounter is the one beat that does not render through
    engine._gen_image_impl, so it never saw summarize_world_prompt_for_image.
    It came back not merely somewhere else but in a different FILM: a playtest
    cut from a dusk red-mesa scrapyard on 1993 stock into a damp conifer forest
    under flat grey daylight, with every lock in the prompt satisfied.
    """

    GLOSS = "muted 1993 desert thriller, amber and rust tones, oppressive haze"

    def _brief(self):
        return {
            "character": {"label": "A man in field jacket", "kind": "person",
                          "stance": "opportunistic", "look": "a man in a field jacket"},
            "place_hold": "the rusted truck chassis and the scorched gravel",
        }

    def test_the_resolve_carries_the_look_of_the_run(self):
        p = encounter.build_encounter_resolve_prompt(
            self._brief(), "Crush his skull with boot.", "confront", "survive",
            setting="outdoor", world_flavor=self.GLOSS)
        self.assertIn("amber and rust", p)
        self.assertIn("same film", p)

    def test_the_standoff_plate_carries_it_too(self):
        p = encounter.build_encounter_plate_prompt(
            self._brief(), img2img=True, setting="outdoor",
            world_flavor=self.GLOSS)
        self.assertIn("amber and rust", p)

    def test_the_fight_still_holds_its_own_location(self):
        p = encounter.build_encounter_resolve_prompt(
            self._brief(), "Shove him back", "confront", "survive",
            setting="outdoor", world_flavor=self.GLOSS)
        self.assertIn("stays in the location it started in", p)

    def test_it_does_not_tell_the_world_which_places_it_may_contain(self):
        """The bible's biome absolutes were eased on purpose — the world is
        free to range somewhere strange between scenes. This line is about
        holding ONE fight together, not about fencing the world in."""
        p = encounter.build_encounter_resolve_prompt(
            self._brief(), "Shove him back", "confront", "survive",
            setting="outdoor", world_flavor=self.GLOSS)
        for banned in ("biome", "desert only", "must belong to that world"):
            self.assertNotIn(banned, p.lower())

    def test_no_gloss_adds_no_line(self):
        p = encounter.build_encounter_resolve_prompt(
            self._brief(), "Shove him back", "confront", "survive",
            setting="outdoor", world_flavor="")
        self.assertNotIn("same film", p)
        self.assertNotIn("WORLD —", p)


class TestTheCameraStopsFollowingThemWhenItIsOver(unittest.TestCase):
    """Breaking away was followed by turns of the camera trailing the person
    you just escaped, because the enemy stayed the object-permanence subject
    past the release."""

    SRC = open("encounter.py", encoding="utf-8").read()

    def test_the_subject_is_dropped_once_the_encounter_releases(self):
        self.assertIn(
            '"subject": None if released else engine._permanence_subject(subject)',
            self.SRC,
        )

    def test_a_released_encounter_is_cleared_from_the_state(self):
        engine_src = open("engine.py", encoding="utf-8").read()
        self.assertIn("_encounter.clear_encounter(SID)", engine_src)


class TestAWorldStitchDoesNotRecastYou(unittest.TestCase):
    """Most saved World snapshots carry an empty cast sheet, and loading one
    dropped the run's protagonist, so the player became the shipped one."""

    SRC = open("engine.py", encoding="utf-8").read()

    def test_the_protagonist_is_carried_across_a_world_load(self):
        self.assertIn("prior_cast", self.SRC)
        self.assertIn("kept protagonist", self.SRC)

    def test_a_fight_does_not_survive_a_world_stitch(self):
        # The stitch forgets the previous World through one helper now (it
        # grew past the fight — see test_world_stitch); it has to call it, and
        # the helper has to drop the fight.
        block = self.SRC.split("def apply_experience_world", 1)[1][:3000]
        self.assertIn("_clear_world_scoped_state(state)", block)
        st = {"turn_count": 3, "encounter": {"label": "a guard"},
              "encounter_outcome": "escape", "encounter_resolving": True}
        engine._clear_world_scoped_state(st)
        for key in ("encounter", "encounter_outcome", "encounter_resolving"):
            self.assertNotIn(key, st)


class TestChoiceTextIsNotTruncatedMidPhrase(unittest.TestCase):

    def test_a_clipped_label_does_not_end_on_a_conjunction(self):
        for raw in (
            "A man in green combat jacket and beanie",
            "A gaunt figure in a tattered grey wool overcoat",
            "A woman with a rifle slung over the",
        ):
            got = encounter._clip(raw, "", 40)
            self.assertFalse(
                re.search(r"\s(and|or|with|in|on|a|an|the|of|at|to|from|for)$", got, re.I),
                f"{raw!r} clipped to a dangling word: {got!r}",
            )


class TestThePlateIsShotOnTheGamesCamera(unittest.TestCase):
    """The plate carried a one-line vantage and nothing else, while every
    other frame in the game is handed the whole camera block. So the one
    render that most needed to match the shot before it was the only one that
    never heard the rig, the shot size, or "never turn them to face the lens"
    — and it staged the standoff however it liked."""

    def _plate(self, img2img=True):
        with mock.patch("game_identity.shows_character", return_value=True):
            return encounter.build_encounter_plate_prompt(
                _brief(), img2img=img2img, setting="outdoor")

    def test_the_camera_block_reaches_the_plate(self):
        directive = game_identity.camera_directive()
        prompt = self._plate()
        for line in directive.splitlines():
            line = line.strip("• ").strip()
            if len(line) > 20:
                self.assertIn(line, prompt)

    def test_a_held_reference_is_not_recomposed(self):
        # "Hold the established camera" followed by four sentences of
        # restaging (thirds, diagonals, different sizes, get close and
        # off-axis) is a recompose, and it outvoted both the camera block and
        # the reference frame underneath.
        held = self._plate(img2img=True)
        self.assertNotIn("COMPOSITION", held)
        self.assertNotIn("on the thirds", held)
        self.assertIn("composition that already exists", held)

    def test_an_empty_frame_still_gets_staged(self):
        # Text-to-image has no reference to hold, so the film-still staging
        # is the only thing standing between it and two people centred and
        # squared up.
        self.assertIn("COMPOSITION", self._plate(img2img=False))


class TestTheCastLockDoesNotFightTheReference(unittest.TestCase):
    """CARRY THE PLAYER OVER says to copy the player's face and clothes out
    of the attached frame. The cast lock's closing sentence said a previous
    frame showing someone else is wrong and to ignore that person. Both are
    hard rules about the same body, so the model was free to pick — and what
    came back was the sheet loosely re-imagined: right man, wrong coat."""

    def _plate(self, img2img):
        with mock.patch("game_identity.shows_character", return_value=True):
            return encounter.build_encounter_plate_prompt(
                _brief(), img2img=img2img, setting="outdoor")

    def test_the_attached_frame_is_trusted_to_be_the_player(self):
        prompt = self._plate(True)
        self.assertIn("CARRY THE PLAYER OVER", prompt)
        self.assertNotIn("ignore that person", prompt)

    def test_a_recast_is_still_ignored_when_there_is_no_reference(self):
        # Without a frame to carry anyone over from, the leftover still on
        # disk may well be the protagonist the author just replaced.
        self.assertIn("ignore that person", self._plate(False))
        self.assertIn("ignore that person", encounter.player_cast_lock())


class TestThePlaceLockReadsWhatTheTurnActuallyWrote(unittest.TestCase):
    """``read_place_lock`` asked history for `description` / `caption`, and no
    turn has ever written either — the key is `vision_analysis`. So the
    description half of the lock was empty on every encounter ever rolled:
    the brief was briefed on "VISIBLE: ", invented a location out of the world
    bible (all corridors and concrete floors), and the plate restaged an open
    desert into it."""

    FRAME = ("A man in an olive field jacket stands facing away beside a "
             "chain-link fence, a pickup truck on cracked desert earth.")

    def _lock(self, entry):
        with mock.patch("engine._load_history", return_value=[entry]):
            return encounter.read_place_lock("place-lock-test")

    def test_the_frame_description_reaches_the_lock(self):
        lock = self._lock({"vision_analysis": self.FRAME,
                           "setting_type": "outdoor-desert",
                           "spatial_compass": "Ahead: open terrain"})
        self.assertEqual(self.FRAME, lock["description"])
        self.assertTrue(lock["outdoor"])
        self.assertIn("outdoor-desert", lock["place_hold"])

    def test_an_unseen_frame_does_not_claim_to_be_indoors(self):
        # Nothing looked at it yet. `outdoor` False here is honest; the plate
        # must not then assert an interior on the strength of it.
        lock = self._lock({"choice": "Initialize Simulation"})
        self.assertEqual("", lock["description"])
        self.assertEqual("", lock["place_hold"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
