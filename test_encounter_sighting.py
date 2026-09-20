"""A person in the picture IS the encounter.

The confrontation system had one trigger, the travel clock, and one way for
the frame to have a say in who arrived (the detection witness, at ALERTED or
worse). Meanwhile the game kept putting people IN the picture — a figure at
the end of the corridor, a scavenger over a body, each with a TALK button —
and then rolling a fight with a stranger from a list twenty seconds of walking
later. "We have an awesome encounter system but nothing rational triggers it."

Now every SCAN pass (the auto-scan runs one on every painted picture) hands
its animate figures to a sighting: if there is one and nothing is open, ONE of
them is rolled, their close-up is cut from their own bounding box, and the
client opens the confrontation with that figure — the crop rides into the
plate as a cast plate, the frame is the anchor, the character sheet keeps the
player, and the brief is told who it is, how far, and that they can speak.

Pure functions plus one Flask request against a scratch session. No network.

Run with:
    python -m unittest test_encounter_sighting -v
"""

import io
import os
import random
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")

from PIL import Image

import encounter
import engine

ROOT = Path(__file__).resolve().parent


def _obj(label, kind="person", cx=0.5, cy=0.5, w=0.2, h=0.5, speaks=True):
    return {"label": label, "kind": kind, "cx": cx, "cy": cy, "w": w, "h": h,
            "speaks": speaks}


def _frame_bytes(width=640, height=360):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (40, 60, 40)).save(buf, "PNG")
    return buf.getvalue()


class TestWhoCountsAsASighting(unittest.TestCase):

    def test_people_and_creatures_count_props_and_talking_boxes_do_not(self):
        objs = [_obj("a guard"), _obj("blast door", kind="object"),
                _obj("a beast", kind="creature"), _obj("a dog", kind="animal"),
                _obj("intercom", kind="machine", speaks=True)]
        labels = [o["label"] for o in encounter.sighting_candidates(objs)]
        self.assertEqual(labels, ["a guard", "a beast"])

    def test_a_body_a_statue_or_a_poster_is_not_someone_to_meet(self):
        """"corpse" and "body" are in the detector's own speaker list — a
        TALK with one is a story beat. An encounter with one is not."""
        objs = [_obj("corpse of a soldier"), _obj("a body on the grating"),
                _obj("statue of a saint", kind="character"),
                _obj("poster of a woman"), _obj("your reflection"),
                _obj("a scavenger")]
        labels = [o["label"] for o in encounter.sighting_candidates(objs)]
        self.assertEqual(labels, ["a scavenger"])

    def test_the_player_is_never_the_encounter(self):
        with mock.patch.object(engine, "_is_player_self_label",
                               side_effect=lambda l: l == "Isaac Clarke"):
            objs = [_obj("Isaac Clarke"), _obj("a scavenger")]
            labels = [o["label"] for o in encounter.sighting_candidates(objs)]
        self.assertEqual(labels, ["a scavenger"])

    def test_garbage_is_ignored(self):
        self.assertEqual(encounter.sighting_candidates(None), [])
        self.assertEqual(encounter.sighting_candidates(["x", 3, {"kind": "person"}]), [])


class TestSeveralPeopleMeansARoll(unittest.TestCase):

    def test_one_figure_is_the_figure(self):
        only = _obj("a guard")
        self.assertEqual(encounter.roll_sighting([only])["label"], "a guard")
        self.assertIsNone(encounter.roll_sighting([]))

    def test_several_figures_roll_one_of_them(self):
        pool = [_obj("a guard"), _obj("a scavenger"), _obj("a medic")]
        seen = set()
        for seed in range(40):
            pick = encounter.roll_sighting(pool, rng=random.Random(seed))
            self.assertIn(pick["label"], {"a guard", "a scavenger", "a medic"})
            seen.add(pick["label"])
        self.assertEqual(len(seen), 3, "over forty seeds every figure should get a turn")

    def test_the_roll_returns_a_copy(self):
        pool = [_obj("a guard")]
        pick = encounter.roll_sighting(pool)
        pick["label"] = "changed"
        self.assertEqual(pool[0]["label"], "a guard")


class TestWhenASightingMayFire(unittest.TestCase):

    def test_a_quiet_run_fires(self):
        self.assertEqual(encounter.sighting_can_fire({}), (True, "ok"))
        self.assertEqual(encounter.sighting_can_fire({"turn_count": 7}), (True, "ok"))

    def test_the_hard_gates_still_hold(self):
        self.assertEqual(encounter.sighting_can_fire({"encounter": {"x": 1}}),
                         (False, "already_open"))
        self.assertEqual(encounter.sighting_can_fire({"player_state": {"alive": False}}),
                         (False, "game_over"))

    def test_a_short_cooldown_after_the_last_encounter(self):
        """An escape's fresh frame still shows the figure; without this the
        very next auto-scan re-opened the fight."""
        cd = encounter.ENCOUNTER_SIGHT_COOLDOWN_TURNS
        self.assertEqual(encounter.sighting_can_fire(
            {"turn_count": 10, "encounter_last_turn": 10}), (False, "cooldown"))
        self.assertEqual(encounter.sighting_can_fire(
            {"turn_count": 10 + cd - 1, "encounter_last_turn": 10}), (False, "cooldown"))
        self.assertEqual(encounter.sighting_can_fire(
            {"turn_count": 10 + cd, "encounter_last_turn": 10}), (True, "ok"))

    def test_the_figure_who_just_backed_off_is_left_alone_for_a_while(self):
        cd = encounter.ENCOUNTER_SIGHT_COOLDOWN_TURNS
        st = {"turn_count": 10 + cd, "encounter_last_turn": 10,
              "encounter_last_label": "a hooded scavenger"}
        self.assertTrue(encounter.sighting_is_recent_antagonist(st, "hooded scavenger"))
        self.assertTrue(encounter.sighting_is_recent_antagonist(st, "a hooded scavenger"))
        self.assertFalse(encounter.sighting_is_recent_antagonist(st, "a guard"))
        st["turn_count"] = 10 + cd * 2
        self.assertFalse(encounter.sighting_is_recent_antagonist(st, "a hooded scavenger"))
        self.assertFalse(encounter.sighting_is_recent_antagonist({}, "anyone"))


class TestTheSightingIsStagedForThisTurn(unittest.TestCase):

    def test_staged_and_fresh_only_on_its_own_turn(self):
        st = {"turn_count": 3}
        public = encounter.stage_sighting(st, _obj("a guard", h=0.6), crop_path="/tmp/x.png", figures=2)
        self.assertEqual(public["label"], "a guard")
        self.assertEqual(public["source"], "sighting")
        self.assertEqual(public["distance"], "near")
        self.assertEqual(public["figures"], 2)
        self.assertNotIn("crop_path", public, "the client is not told a server path")
        fresh = encounter.fresh_sighting(st)
        self.assertEqual(fresh["crop_path"], "/tmp/x.png")
        st["turn_count"] = 4
        self.assertIsNone(encounter.fresh_sighting(st))

    def test_distance_follows_the_witness_buckets(self):
        self.assertEqual(encounter.sighting_distance(_obj("x", h=engine.WITNESS_NEAR_H)), "near")
        self.assertEqual(encounter.sighting_distance(_obj("x", h=engine.WITNESS_MID_H)), "mid")
        self.assertEqual(encounter.sighting_distance(_obj("x", h=0.05)), "far")
        self.assertEqual(encounter.sighting_distance(None), "far")

    def test_the_pin_consumes_the_sighting_and_remembers_who(self):
        saved = {}

        def _save(st, sid):
            saved.update(st)

        st = {"turn_count": 4, "encounter_sighting": {"label": "a guard", "turn": 4}}
        with mock.patch.object(engine, "_load_state", return_value=st), \
             mock.patch.object(engine, "_save_state", side_effect=_save), \
             mock.patch.object(engine, "_sync_ambient_state"), \
             mock.patch.object(engine, "_load_history", return_value=[]), \
             mock.patch.object(engine, "_save_history"):
            encounter._pin_encounter_plate("s", None, None, {
                "character": {"label": "a guard", "kind": "person"},
                "danger": "d", "stakes": "s", "place_hold": "p"})
        self.assertIsNone(saved.get("encounter_sighting"))
        self.assertEqual(saved.get("encounter_last_label"), "a guard")
        self.assertEqual(saved.get("encounter_last_turn"), 4)


class TestTheBriefIsToldWhoItSaw(unittest.TestCase):

    def test_a_sighting_is_not_an_attack_and_not_a_roll(self):
        line = encounter.sighting_brief_line(
            {"label": "a guard", "kind": "person", "distance": "mid",
             "figures": 3, "speaks": True, "source": "sighting"})
        self.assertIn("THE PLAYER HAS JUST SEEN: a guard", line)
        self.assertIn("a few strides away", line)
        self.assertIn("2 other figure(s)", line)
        self.assertIn("It can speak", line)
        self.assertIn("what the player is trying to reach", line)
        self.assertNotIn("JUST ATTACKED", line)

    def test_a_silent_creature_reads_as_one(self):
        line = encounter.sighting_brief_line(
            {"label": "a beast", "kind": "creature", "distance": "near", "speaks": False})
        self.assertIn("a creature in it", line)
        self.assertIn("kind is creature", line)
        self.assertIn("It does not speak.", line)
        self.assertNotIn("other figure", line)

    def test_build_encounter_brief_uses_the_sighting_line(self):
        asked = {}

        def fake_ask(prompt, **kw):
            asked["prompt"] = prompt
            asked["image"] = kw.get("image_path")
            return ('{"character": {"label": "a guard", "kind": "person", '
                    '"look": "grey fatigues, a lamp on the helmet", "stance": "desperate"}, '
                    '"motive": "wants the lamp back", "danger": "raises the rifle", '
                    '"stakes": "You lose the corridor.", "place_hold": "the corridor"}')

        with mock.patch.object(engine, "_load_state", return_value={"world_prompt": "w"}), \
             mock.patch.object(engine, "_ask", side_effect=fake_ask), \
             mock.patch.object(encounter, "encounter_lore_context", return_value="LORE"), \
             mock.patch.object(encounter, "roll_encounter_kind") as roll:
            brief = encounter.build_encounter_brief(
                "s", image_path="/crop.png", target={
                    "label": "a guard", "kind": "person", "source": "sighting",
                    "distance": "near", "figures": 1, "speaks": True})
        roll.assert_not_called()
        self.assertIn("THE PLAYER HAS JUST SEEN: a guard", asked["prompt"])
        self.assertEqual(asked["image"], "/crop.png")
        self.assertEqual(brief["character"]["label"], "a guard")

    def test_an_aimed_target_still_reads_as_an_attack(self):
        asked = {}

        def fake_ask(prompt, **kw):
            asked["prompt"] = prompt
            raise RuntimeError("stop")

        with mock.patch.object(engine, "_load_state", return_value={}), \
             mock.patch.object(engine, "_ask", side_effect=fake_ask), \
             mock.patch.object(encounter, "encounter_lore_context", return_value=""):
            encounter.build_encounter_brief("s", target={"label": "the rusted tank"})
        self.assertIn("THE PLAYER HAS JUST ATTACKED: the rusted tank", asked["prompt"])


class TestThePlateKnowsTheFigureIsAlreadyThere(unittest.TestCase):

    def _brief(self):
        return encounter.normalize_encounter_brief({
            "character": {"label": "a guard", "kind": "person",
                          "look": "grey fatigues, a lamp on the helmet", "stance": "hostile"},
            "danger": "raises the rifle", "stakes": "You lose the corridor.",
            "place_hold": "the corridor",
        })

    def test_a_sighting_copies_the_close_up_and_adds_nobody(self):
        with mock.patch("game_identity.shows_character", return_value=True):
            prompt = encounter.build_encounter_plate_prompt(
                self._brief(), img2img=True, setting="indoor",
                target={"label": "a guard", "source": "sighting"})
        self.assertIn("The a guard is ALREADY in this photograph", prompt)
        self.assertIn("CLOSE-UP of them is attached", prompt)
        self.assertIn("add NOBODY", prompt)
        self.assertNotIn("has just struck it", prompt)
        self.assertNotIn("Add EXACTLY ONE new person", prompt)

    def test_an_aimed_target_is_still_the_thing_that_was_struck(self):
        with mock.patch("game_identity.shows_character", return_value=True):
            prompt = encounter.build_encounter_plate_prompt(
                self._brief(), img2img=True, setting="indoor",
                target={"label": "the rusted tank"})
        self.assertIn("has just struck it", prompt)
        self.assertIn("Add EXACTLY ONE new person", prompt)

    def test_the_grid_carries_the_close_up_as_a_cast_plate(self):
        tmp = Path(tempfile.mkdtemp())
        crop = tmp / "sighting_guard.png"
        Image.new("RGB", (64, 64)).save(crop)
        ref = tmp / "frame.png"
        Image.new("RGB", (64, 64)).save(ref)
        calls = []

        def fake_grid(**kw):
            calls.append(kw)
            return None

        try:
            with mock.patch.object(engine, "_load_state", return_value={"flipbook_mode": True}), \
                 mock.patch.object(engine, "flipbook_active", return_value=True), \
                 mock.patch.object(engine, "_get_image_dir", return_value=str(tmp)), \
                 mock.patch.object(engine, "_flipbook_generate", side_effect=fake_grid), \
                 mock.patch.object(encounter, "encounter_identity_paths", return_value=[]):
                encounter._plate_sequence("s", "prompt", str(ref), caption="c",
                                          two_shot="a guard", two_shot_look="fatigues",
                                          cast_plates=[str(crop), str(tmp / "missing.png")])
            self.assertEqual(calls[0]["cast_plates"], [str(crop)],
                             "the close-up rides as a cast plate; a missing file does not")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestTheCloseUpIsCutFromTheFrame(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._dir = mock.patch.object(engine, "_get_image_dir", return_value=str(self.tmp))
        self._dir.start()

    def tearDown(self):
        self._dir.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_box_pads_and_pulls_the_top_up_for_the_head(self):
        plain = engine._norm_box_from_subject(_obj("g", cx=0.5, cy=0.6, w=0.2, h=0.4), pad=0.18)
        box = engine._sighting_box(_obj("g", cx=0.5, cy=0.6, w=0.2, h=0.4))
        self.assertLess(box["y"], plain["y"])
        self.assertGreater(box["h"], plain["h"])
        self.assertGreaterEqual(box["y"], 0.0)
        self.assertLessEqual(box["y"] + box["h"], 1.0 + 1e-6)
        self.assertIsNone(engine._sighting_box({"cx": 0.5, "cy": 0.5, "w": 0.001, "h": 0.001}))

    def test_a_crop_lands_on_disk_at_the_figures_size(self):
        path = engine._crop_sighting_from_bytes(
            _frame_bytes(640, 360), _obj("a guard", cx=0.5, cy=0.55, w=0.25, h=0.6), "s")
        self.assertTrue(path and os.path.exists(path), path)
        self.assertIn("sighting_a_guard_", os.path.basename(path))
        with Image.open(path) as im:
            self.assertGreaterEqual(im.size[0], 48)
            self.assertLess(im.size[0], 640)

    def test_a_far_figure_is_upscaled_to_something_the_model_can_read(self):
        """The first live sighting was 52x127 and came back dressed from the
        roster, not from its own pixels."""
        path = engine._crop_sighting_from_bytes(
            _frame_bytes(1376, 768), _obj("a soldier", cx=0.75, cy=0.55, w=0.03, h=0.14), "s")
        self.assertTrue(path and os.path.exists(path), path)
        with Image.open(path) as im:
            self.assertGreaterEqual(min(im.size), engine._SIGHTING_CROP_LEGIBLE_PX)

    def test_a_smear_is_not_a_close_up(self):
        path = engine._crop_sighting_from_bytes(
            _frame_bytes(640, 360), _obj("a speck", cx=0.9, cy=0.2, w=0.03, h=0.03), "s")
        self.assertIsNone(path)

    def test_the_same_crop_from_a_frame_on_disk(self):
        frame = self.tmp / "frame.png"
        frame.write_bytes(_frame_bytes(640, 360))
        path = engine._crop_sighting_from_path(
            str(frame), _obj("a medic", cx=0.3, cy=0.5, w=0.2, h=0.5), "s")
        self.assertTrue(path and os.path.exists(path))
        self.assertIsNone(engine._crop_sighting_from_path(str(self.tmp / "nope.png"), _obj("x"), "s"))


class TestStagingFromADetectPass(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._dir = mock.patch.object(engine, "_get_image_dir", return_value=str(self.tmp))
        self._dir.start()

    def tearDown(self):
        self._dir.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_one_figure_is_rolled_cropped_and_staged(self):
        st = {"turn_count": 5}
        public = engine._stage_encounter_sighting(
            st, "s", [_obj("a guard"), _obj("a scavenger")], _frame_bytes())
        self.assertIn(public["label"], {"a guard", "a scavenger"})
        self.assertEqual(public["figures"], 2)
        sight = st["encounter_sighting"]
        self.assertEqual(sight["turn"], 5)
        self.assertTrue(os.path.exists(sight["crop_path"]))
        self.assertNotIn("crop_path", public)

    def test_nothing_fires_over_an_open_fight_or_inside_the_cooldown(self):
        self.assertIsNone(engine._stage_encounter_sighting(
            {"encounter": {"x": 1}}, "s", [_obj("a guard")], _frame_bytes()))
        st = {"turn_count": 3, "encounter_last_turn": 3}
        self.assertIsNone(engine._stage_encounter_sighting(st, "s", [_obj("a guard")], _frame_bytes()))
        self.assertNotIn("encounter_sighting", st)

    def test_the_figure_who_just_backed_off_does_not_reopen_it(self):
        cd = encounter.ENCOUNTER_SIGHT_COOLDOWN_TURNS
        st = {"turn_count": 3 + cd, "encounter_last_turn": 3, "encounter_last_label": "a guard"}
        self.assertIsNone(engine._stage_encounter_sighting(st, "s", [_obj("a guard")], _frame_bytes()))
        public = engine._stage_encounter_sighting(
            st, "s", [_obj("a guard"), _obj("a medic")], _frame_bytes())
        self.assertEqual(public["label"], "a medic")

    def test_no_figures_no_sighting(self):
        st = {"turn_count": 1}
        self.assertIsNone(engine._stage_encounter_sighting(st, "s", [], _frame_bytes()))
        self.assertNotIn("encounter_sighting", st)


class TestTheDetectEndpointOpensIt(unittest.TestCase):
    """One real request: the detector (stubbed) sees a person, the response
    names the figure to open on, and the session holds the staged sighting
    with its close-up on disk."""

    SESSION = "sighting_test_session"

    def setUp(self):
        import api
        self.client = api.app.test_client()
        self.sess_dir = ROOT / "sessions" / self.SESSION

    def tearDown(self):
        shutil.rmtree(self.sess_dir, ignore_errors=True)

    def _post(self, objects, purpose="scan"):
        import base64
        frame = "data:image/png;base64," + base64.b64encode(_frame_bytes()).decode("ascii")
        body = {"frame": frame, "session_id": self.SESSION}
        if purpose:
            body["purpose"] = purpose
        import api
        with mock.patch.object(engine, "_detect_objects", return_value=objects), \
             mock.patch.object(engine, "_detect_scene_prior", return_value=""), \
             mock.patch.object(api, "_spend_blocked", return_value=None):
            return self.client.post("/api/detect", json=body).get_json()

    def test_a_person_in_frame_comes_back_as_the_encounter(self):
        res = self._post([_obj("a guard", h=0.6), _obj("blast door", kind="object")])
        self.assertEqual(res["encounter_with"]["label"], "a guard")
        self.assertEqual(res["encounter_with"]["source"], "sighting")
        self.assertEqual(res["encounter_with"]["distance"], "near")
        self.assertEqual([o["label"] for o in res["objects"]], ["a guard", "blast door"])
        st = engine._load_state(self.SESSION)
        self.assertEqual(st["encounter_sighting"]["label"], "a guard")
        self.assertTrue(os.path.exists(st["encounter_sighting"]["crop_path"]))

    def test_a_read_only_probe_does_not_stage_anything(self):
        res = self._post([_obj("a guard")], purpose=None)
        self.assertNotIn("encounter_with", res)

    def test_an_empty_room_stages_nothing(self):
        res = self._post([_obj("blast door", kind="object")])
        self.assertNotIn("encounter_with", res)


class TestBeginOpensOnTheSighting(unittest.TestCase):
    """api_begin with a sighting: the target carries the staged close-up,
    the brief and both plate paths get it, and the state's sighting is spent."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.crop = self.tmp / "sighting_guard.png"
        Image.new("RGB", (80, 80)).save(self.crop)
        self.still = self.tmp / "plate_f04.png"
        Image.new("RGB", (80, 80)).save(self.still)
        # The frame the client posts; api_begin unlinks it when it is done.
        self.frame = self.tmp / "portrait_ref_1.png"
        self.frame.write_bytes(_frame_bytes())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _begin(self, body, state):
        import api
        seen = {}

        def fake_brief(session_id, image_path=None, target=None, **kw):
            seen["brief_image"] = image_path
            seen["target"] = dict(target or {})
            return encounter.normalize_encounter_brief({
                "character": {"label": "a guard", "kind": "person",
                              "look": "grey fatigues", "stance": "desperate"},
                "danger": "raises the rifle", "stakes": "You lose the corridor.",
                "place_hold": "the corridor"})

        def fake_seq(session_id, prompt, ref_path, **kw):
            seen["seq"] = dict(kw)
            seen["prompt"] = prompt
            return {"payload": {"frames": []}, "still": str(self.still)}

        with mock.patch.object(engine, "_rate_limited", return_value=False), \
             mock.patch.object(engine, "_resolve_request_session_id", return_value="s"), \
             mock.patch.object(engine, "_load_state", return_value=state), \
             mock.patch.object(engine, "_save_portrait_reference",
                               return_value=str(self.frame) if body.get("frame") else None), \
             mock.patch.object(engine, "_to_web_image_url", return_value="/img/plate.png"), \
             mock.patch.object(encounter, "_confrontation_plate_path", return_value=None), \
             mock.patch.object(encounter, "read_place_lock", return_value={"setting": "indoor"}), \
             mock.patch.object(encounter, "build_encounter_brief", side_effect=fake_brief), \
             mock.patch.object(encounter, "_plate_sequence", side_effect=fake_seq), \
             mock.patch.object(encounter, "_ground_brief_on_plate"), \
             mock.patch.object(encounter, "_generate_encounter_choices",
                               return_value=[{"text": "Rush him", "lane": "confront"},
                                             {"text": "Back away", "lane": "evade"},
                                             {"text": "Call out", "lane": "parley"}]), \
             mock.patch.object(encounter, "_pin_encounter_plate") as pin, \
             mock.patch.object(encounter, "_record_encounter_companion"), \
             mock.patch.object(encounter, "world_flavor", return_value=""), \
             mock.patch.object(encounter, "set_look_session"), \
             mock.patch.object(api, "_spend_blocked", return_value=None):
            res = api.app.test_client().post("/api/encounter/begin", json=body).get_json()
        seen["pin_calls"] = pin.call_args_list
        return res, seen

    def test_the_staged_close_up_reaches_the_brief_and_the_plate(self):
        state = {"turn_count": 4, "encounter_sighting": {
            "label": "a guard", "kind": "person", "turn": 4, "source": "sighting",
            "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.6, "distance": "near",
            "figures": 1, "speaks": True, "crop_path": str(self.crop)}}
        res, seen = self._begin(
            {"session_id": "s", "frame": "data:image/png;base64,AAAA",
             "subject": {"label": "a guard", "source": "sighting",
                         "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.6}},
            state)
        self.assertEqual(res["encounter"]["source"], "sighting")
        self.assertEqual(seen["target"]["source"], "sighting")
        self.assertEqual(seen["target"]["crop_path"], str(self.crop))
        self.assertEqual(seen["brief_image"], str(self.crop),
                         "the brief describes the figure off its own pixels")
        self.assertEqual(seen["seq"]["cast_plates"], [str(self.crop)])
        self.assertIn("ALREADY in this photograph", seen["prompt"])
        self.assertTrue(seen["pin_calls"])

    def test_a_fresh_sighting_opens_even_when_the_client_sent_no_subject(self):
        state = {"turn_count": 4, "encounter_sighting": {
            "label": "a medic", "kind": "person", "turn": 4, "source": "sighting",
            "cx": 0.4, "cy": 0.5, "w": 0.2, "h": 0.5, "distance": "mid",
            "figures": 1, "speaks": True, "crop_path": str(self.crop)}}
        res, seen = self._begin({"session_id": "s"}, state)
        self.assertEqual(seen["target"]["label"], "a medic")
        self.assertEqual(res["encounter"]["source"], "sighting")

    def test_a_stale_sighting_is_not_the_encounter(self):
        state = {"turn_count": 9, "encounter_sighting": {
            "label": "a medic", "kind": "person", "turn": 4, "source": "sighting",
            "crop_path": str(self.crop)}}
        with mock.patch.object(encounter, "onscreen_threat_target", return_value=None):
            res, seen = self._begin({"session_id": "s"}, state)
        self.assertEqual(seen["target"], {})
        self.assertEqual(res["encounter"]["source"], "roll")
        self.assertIsNone(seen["seq"].get("cast_plates"))


class TestTheClientOpensIt(unittest.TestCase):
    """The wiring lives in the client: the detect response's figure opens the
    Encounter Moment with that subject, and start() carries its box."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "static" / "js" / "standalone.js").read_text(encoding="utf-8", errors="replace")

    def test_the_detect_result_opens_a_sighting(self):
        self.assertIn("res.encounter_with", self.js)
        self.assertIn("openSightingEncounter(res.encounter_with)", self.js)

    def test_the_opener_hands_the_figure_to_the_encounter(self):
        body = self.js.split("function openSightingEncounter(", 1)[1].split("\n  }\n", 1)[0]
        self.assertIn("Encounter.start({ subject: subject, sighting: true })", body)
        self.assertIn("closeScan()", body)
        self.assertIn("SIGHTING_BEAT_MS", body)

    def test_start_carries_the_box_and_says_it_is_a_sighting(self):
        body = self.js.split("async function start(opts)", 1)[1].split("await hitch()", 1)[0]
        self.assertIn('forcedSubject.source = "sighting"', body)
        for k in ('"cx"', '"cy"', '"w"', '"h"'):
            self.assertIn(k, body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
