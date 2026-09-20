"""The face you met in the dive is the face that comes back.

INTERACT is a Moment, not a turn. The dive draws a close-up of the thing the
player tapped and the turn that regenerates the scene runs underneath it — the
client issues both on consecutive lines, so they are two renders of the same
subject started at the same instant with neither aware of the other. The
close-up is generated from that subject's own pixels; the scene is generated
from a wide frame where the same subject is forty pixels tall. They do not
agree, and the disagreement lands at the worst possible moment: the character
the player has just been introduced to is somebody else by the time the world
is handed back, which is the one thing a discovery must not do.

So the scene render holds for the close-up and then carries it in as an extra
labeled reference. Two halves, both pinned here:

  * the rendezvous — an in-process Event between api_choose's turn thread and
    api_talk_portrait's request thread, which has to survive either one
    finishing first, has to release early when the close-up fails, and must
    never make a turn wait for a plate nobody is drawing;
  * the payload — the plate rides high in the reference order and labeled, and
    the prompt stops asking for an empty environment plate, because the
    environment paths spend a great many words demanding exactly that.

Run with:
    python -m unittest test_interact_plate_handoff -v
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import engine  # noqa: E402
import gemini_image_utils as giu  # noqa: E402

CLIENT_JS = (ROOT / "static" / "js" / "standalone.js").read_text(
    encoding="utf-8", errors="replace")


class TheRendezvous(unittest.TestCase):
    """Neither side knows which of them will finish first."""

    def setUp(self):
        engine._INTERACT_PLATES.clear()
        self.addCleanup(engine._INTERACT_PLATES.clear)
        # A real file, because the await only returns plates that resolve.
        self.plate = ROOT / "_test_plate_handoff.png"
        self.plate.write_bytes(b"\x89PNG\r\n\x1a\n")
        self.addCleanup(lambda: self.plate.unlink(missing_ok=True))

    def _await(self, subject="mesa"):
        return engine._await_interact_plate("s1", "scan_interact", subject)

    def test_the_turn_gets_the_close_up_when_the_turn_arrives_first(self):
        engine._arm_interact_plate("s1", "rusted radio")
        threading.Timer(
            0.05, engine._publish_interact_plate,
            args=("s1", "rusted radio", str(self.plate)),
        ).start()
        self.assertEqual(self._await("rusted radio"), [str(self.plate)])

    def test_and_when_the_close_up_arrives_first(self):
        """A cache hit answers instantly, so this ordering is real, not
        theoretical — and arming on top of it used to discard the plate and
        then wait for a second one that nobody was generating."""
        engine._publish_interact_plate("s1", "rusted radio", str(self.plate))
        engine._arm_interact_plate("s1", "rusted radio")
        self.assertEqual(self._await("rusted radio"), [str(self.plate)])

    def test_a_label_that_only_nearly_matches_still_matches(self):
        """The turn is armed from the DETECTED label; the plate is published
        from whatever build_talk_context settled on. Exact equality would spend
        the whole timeout on a definite article."""
        engine._arm_interact_plate("s1", "the rusted radio")
        engine._publish_interact_plate("s1", "rusted radio", str(self.plate))
        self.assertEqual(self._await("the rusted radio"), [str(self.plate)])

    def test_a_failed_close_up_releases_the_turn_instead_of_stalling_it(self):
        """/api/talk/portrait publishes its failures on purpose. Without that
        the scene sits out the entire timeout for a plate that was never
        coming, and INTERACT gets slower the more often the close-up fails."""
        engine._arm_interact_plate("s1", "barrel")
        engine._publish_interact_plate("s1", "barrel", None)
        t0 = time.time()
        self.assertEqual(self._await("barrel"), [])
        self.assertLess(time.time() - t0, 1.0)

    def test_a_plate_is_collected_once(self):
        engine._arm_interact_plate("s1", "barrel")
        engine._publish_interact_plate("s1", "barrel", str(self.plate))
        self.assertEqual(self._await("barrel"), [str(self.plate)])
        self.assertEqual(self._await("barrel"), [])

    def test_a_plate_that_no_longer_exists_on_disk_is_not_offered(self):
        engine._arm_interact_plate("s1", "barrel")
        engine._publish_interact_plate("s1", "barrel", str(ROOT / "_gone.png"))
        self.assertEqual(self._await("barrel"), [])

    def test_an_unrelated_portrait_cannot_strand_the_waiter(self):
        """A TALK portrait finishing beside a dive. Overwriting the record
        would leave the turn waiting on an Event nobody will ever set."""
        engine._arm_interact_plate("s1", "barrel")
        engine._publish_interact_plate("s1", "a guard in a gas mask", str(self.plate))
        rec = engine._INTERACT_PLATES["s1"]
        self.assertEqual(rec["label"], "barrel")
        self.assertFalse(rec["event"].is_set())

    def test_sessions_do_not_share_plates(self):
        engine._arm_interact_plate("s2", "barrel")
        engine._publish_interact_plate("s2", "barrel", str(self.plate))
        self.assertEqual(self._await("barrel"), [])


class NoTurnWaitsForAPlateNobodyIsDrawing(unittest.TestCase):
    """The reason the arm is explicit rather than inferred from the verb.

    A dive only opens when the Moments chrome is present, and the playtest
    harnesses post `scan_interact` straight at the server with no client at
    all. A render that waited on the source alone would add the full timeout to
    every one of those turns — thirty-five seconds each, on the verb the
    harness is built to exercise.
    """

    def setUp(self):
        engine._INTERACT_PLATES.clear()
        self.addCleanup(engine._INTERACT_PLATES.clear)

    def _elapsed(self, *args):
        t0 = time.time()
        got = engine._await_interact_plate(*args)
        return got, time.time() - t0

    def test_an_unarmed_interact_turn_renders_immediately(self):
        got, secs = self._elapsed("s1", "scan_interact", "barrel")
        self.assertEqual(got, [])
        self.assertLess(secs, 0.5)

    def test_every_other_kind_of_turn_renders_immediately(self):
        for source in ("scan_move", "encounter", "talk", "camp_leave", None):
            with self.subTest(source=source):
                got, secs = self._elapsed("s1", source, "barrel")
                self.assertEqual(got, [])
                self.assertLess(secs, 0.5)

    def test_an_interact_turn_with_no_subject_renders_immediately(self):
        got, secs = self._elapsed("s1", "scan_interact", "")
        self.assertEqual(got, [])
        self.assertLess(secs, 0.5)

    def test_the_wait_is_bounded(self):
        engine._arm_interact_plate("s1", "barrel")
        with mock.patch.object(engine, "INTERACT_PLATE_WAIT_S", 0.2):
            got, secs = self._elapsed("s1", "scan_interact", "barrel")
        self.assertEqual(got, [])
        self.assertLess(secs, 3.0)


class TheClientSaysWhenACloseUpIsComing(unittest.TestCase):
    """Source assertions: the flag only goes out when a dive really opened."""

    def test_the_interact_tap_declares_the_close_up(self):
        self.assertIn("awaitingCloseUp: !!(dive && dive.referenceFrame)", CLIENT_JS)

    def test_and_it_reaches_the_wire(self):
        self.assertIn("awaiting_closeup: awaitingCloseUp || undefined,", CLIENT_JS)

    def test_a_dive_with_no_crop_does_not_arm_anything(self):
        """`referenceFrame` is the crop the close-up is generated FROM — with
        no crop the endpoint answers no_crop by design, so there is no plate to
        wait for."""
        start = CLIENT_JS.index("const dive = (action.id === \"interact\"")
        tap = CLIENT_JS[start:start + 1200]
        self.assertIn("dive.referenceFrame", tap)

    def test_the_server_only_arms_on_the_clients_word(self):
        src = (ROOT / "engine.py").read_text(encoding="utf-8")
        self.assertIn(
            'if action_source == "scan_interact" and data.get(\'awaiting_closeup\'):',
            src)


class ThePlateRidesIntoTheScene(unittest.TestCase):
    """What the image layer actually does with a cast plate."""

    def setUp(self):
        self.tmp = ROOT / "_test_plate_payload"
        self.tmp.mkdir(exist_ok=True)
        self.addCleanup(self._clean)
        self.prev = self.tmp / "prev_frame.png"
        self.sheet = self.tmp / "character_sheet.png"
        self.closeup = self.tmp / "companion_guard.png"
        for p in (self.prev, self.sheet, self.closeup):
            p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    def _clean(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _payload(self, **kw):
        """Run the real prompt/parts assembly and capture what would be sent."""
        seen = {}

        def fake_post(url, headers=None, json=None, timeout=None, **_):
            seen["payload"] = json
            raise RuntimeError("stop here — the payload is what we came for")

        with mock.patch.object(giu, "GEMINI_API_KEY", "test-key"), \
             mock.patch.object(giu.requests, "post", fake_post):
            giu.generate_gemini_img2img(
                prompt="A guard stands by the fence twenty feet ahead.",
                caption="scene",
                reference_image_path=[str(self.prev)],
                output_dir=self.tmp,
                **kw,
            )
        parts = seen["payload"]["contents"][0]["parts"]
        texts = [p["text"] for p in parts if "text" in p]
        return parts, texts, texts[-1]

    def _attached_order(self, parts, texts):
        """Which labeled image each attachment is, in wire order."""
        order = []
        for i, part in enumerate(parts):
            if "inlineData" not in part:
                continue
            label = parts[i - 1].get("text", "") if i else ""
            order.append(label)
        return order

    def test_the_close_up_is_attached(self):
        parts, _texts, _prompt = self._payload(cast_plates=[str(self.closeup)])
        self.assertEqual(
            sum(1 for p in parts if "inlineData" in p), 2,
            "the close-up should be an EXTRA reference, not a replacement")

    def test_it_is_labeled_as_a_subject_and_not_as_a_previous_frame(self):
        """Unlabeled, a close-up reads as 'the previous frame' — and the model
        obliges by continuing its framing, so the return leg of the dive comes
        back as a second close-up."""
        parts, texts, _prompt = self._payload(cast_plates=[str(self.closeup)])
        labels = self._attached_order(parts, texts)
        self.assertTrue(any("CLOSE-UP OF A SUBJECT ALREADY IN THIS SCENE" in l
                            for l in labels))
        closeup_label = next(l for l in labels
                             if "CLOSE-UP OF A SUBJECT" in l)
        self.assertIn("Do NOT copy its framing", closeup_label)

    def test_the_players_own_sheet_still_leads(self):
        """Slot 1 is who the model copies. The protagonist must never be the
        one recast to make room for a stranger."""
        parts, texts, _prompt = self._payload(
            identity_paths=[str(self.sheet)], cast_plates=[str(self.closeup)])
        labels = self._attached_order(parts, texts)
        self.assertIn("CHARACTER SHEET", labels[0])
        self.assertIn("CLOSE-UP OF A SUBJECT", labels[1])

    def test_the_close_up_outranks_the_frame_it_would_be_redrawn_from(self):
        """Forty pixels of somebody in a wide frame is not enough to redraw
        them from; trailing plates lose. That is the whole mechanism."""
        parts, texts, _prompt = self._payload(cast_plates=[str(self.closeup)])
        labels = self._attached_order(parts, texts)
        self.assertIn("CLOSE-UP OF A SUBJECT", labels[0])
        self.assertIn("PREVIOUS FRAME", labels[1])

    def test_the_prompt_asks_for_the_same_subject_in_a_wide_shot(self):
        _parts, _texts, prompt = self._payload(cast_plates=[str(self.closeup)])
        self.assertIn("THE SUBJECT FROM THE CLOSE-UP IS IN THIS SCENE", prompt)
        self.assertIn("still the same one", prompt)
        self.assertIn("WIDE SCENE", prompt)

    def test_the_subject_is_not_dressed_as_the_player(self):
        """keep_character_instruction's `extras_are_strangers`. Without it the
        sheet's own wording — 'a previous frame may show a different person,
        ignore that person' — reads as an order to discard the close-up."""
        _parts, _texts, prompt = self._payload(
            identity_paths=[str(self.sheet)], cast_plates=[str(self.closeup)])
        self.assertIn("Any other person is a stranger", prompt)

    def test_nothing_asks_for_the_subject_to_be_deleted(self):
        """The environment paths spend a great many capitals demanding an empty
        plate. Handed a discovered character, that is a request to erase them."""
        _parts, _texts, prompt = self._payload(cast_plates=[str(self.closeup)])
        self.assertNotIn("REMOVE ANY PEOPLE FROM REFERENCE IMAGE", prompt)
        self.assertNotIn("No person in frame", prompt)

    def test_a_render_with_no_plate_is_unchanged(self):
        """Every other turn in the game goes through this same function."""
        _parts, _texts, prompt = self._payload()
        self.assertNotIn("THE SUBJECT FROM THE CLOSE-UP", prompt)
        self.assertNotIn("Not a different subject than the close-up", prompt)

    def test_a_plate_that_is_also_an_identity_plate_is_not_attached_twice(self):
        parts, _texts, _prompt = self._payload(
            identity_paths=[str(self.sheet)], cast_plates=[str(self.sheet)])
        self.assertEqual(sum(1 for p in parts if "inlineData" in p), 2)


class ThePlateReachesTheRendererThatIsActuallyUsed(unittest.TestCase):
    """Flipbook is the default, so a plate that only reached the still path
    would reach almost none of the turns anybody plays."""

    SRC = (ROOT / "engine.py").read_text(encoding="utf-8")

    def test_the_still_path_passes_it(self):
        self.assertIn("cast_plates=cast_plate_paths,", self.SRC)

    def test_the_flipbook_takes_it(self):
        sig = self.SRC.split("def _flipbook_generate(", 1)[1].split(")", 1)[0]
        self.assertIn("cast_plates", sig)

    def test_the_flipbook_forwards_it_to_the_image_layer(self):
        body = self.SRC.split("def _flipbook_generate(", 1)[1] \
                       .split("\ndef ", 1)[0]
        self.assertIn("cast_plates=cast_plates,", body)

    def test_the_turn_loop_holds_before_it_renders(self):
        """The order is the feature: wait, THEN render."""
        loop = self.SRC.split("def _process_turn_background(", 1)[1] \
                       .split("\ndef ", 1)[0]
        wait_at = loop.index("cast_plates = _await_interact_plate(")
        render_at = loop.index("scene = _generate_and_append_scene_image(")
        self.assertLess(wait_at, render_at)

    def test_a_viewfinder_restage_never_composites_a_stranger(self):
        """PHOTO is the player's own photograph of the place."""
        impl = self.SRC.split("def _gen_image_impl(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn(
            "cast_plate_paths = [] if game_identity.is_viewfinder_spec(identity_spec)",
            impl)


if __name__ == "__main__":
    unittest.main(verbosity=2)
