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
from unittest import mock

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")

import encounter
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
        rng = random.Random(1)
        self.assertEqual(
            encounter.advance_enemy_state("confront", "survive", "ready", rng=rng),
            "staggered",
        )

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
        self.assertEqual(encounter._DEFAULT_STRANGER_LOOK,
                         encounter.distinct_enemy_look(self.CAMERA_TALK))
        self.assertEqual(encounter._DEFAULT_STRANGER_LOOK,
                         encounter.distinct_enemy_look("POV of a hand gripping a wrench"))

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
        for lane in ("confront", "evade", "use"):
            with self.subTest(lane=lane):
                self.assertLess(self._rounds_to_end(lane), 40)

    def test_putting_them_down_is_a_way_out(self):
        self.assertTrue(encounter.encounter_releases("survive", {"enemy_state": "down"}))
        self.assertFalse(encounter.encounter_releases("survive", {"enemy_state": "staggered"}))

    def test_pressing_a_staggered_body_can_finish_it(self):
        rng = random.Random(7)
        got = {encounter.advance_enemy_state("confront", "survive", "staggered", rng=rng)
               for _ in range(80)}
        self.assertIn("down", got)


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
        block = self.SRC.split("def apply_experience_world", 1)[1][:3000]
        for key in ("encounter", "encounter_outcome", "encounter_resolving"):
            self.assertIn(f'state.pop("{key}", None)', block)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
