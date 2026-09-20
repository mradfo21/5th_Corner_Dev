"""The INTERACT close-up is a TRANSITION, not a destination.

It went through both mistakes. First it was a photograph of the thing you
poked with a single X on it — a loading screen with production values. Then it
was a place to stand: SPEAK / ATTACK / LEAVE, two of them locked until the turn
behind it finished, and a click required to get out.

Timed against the live server, that second shape was the worse one. The
close-up lands at ~6s and the turn's new scene at ~19s, so the dive was
thirteen seconds of a finished picture with buttons that could not be pressed,
and then a click to dismiss it. The turn was never the problem — a plain
curated choice measures SLOWER (~26s to its scene) and reads fine, because the
world is on screen answering while it happens. The dive was the only place in
the game that turned the wait into a room.

So it now reaches out, shows the thing, says what it caused the moment the
words exist (~3s, sixteen seconds before the picture), and hands the world back
by itself the instant the scene has changed. This file pins that.

Plus the framing rule both dives share: the pull-in toward a detection's box is
half as hard, so a close-up still shares pixels with the frame it came from.

Pure functions plus source assertions on the client. No network.

Run with:
    python -m unittest test_interact_dive_options -v
"""

import os
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")

import encounter

ROOT = Path(__file__).resolve().parent
CLIENT_JS = (ROOT / "static" / "js" / "standalone.js").read_text(
    encoding="utf-8", errors="replace")
MOMENTS_JS = (ROOT / "static" / "js" / "moments.js").read_text(
    encoding="utf-8", errors="replace")
CSS = (ROOT / "static" / "css" / "standalone.css").read_text(
    encoding="utf-8", errors="replace")


def _dive_source():
    start = CLIENT_JS.index("function createInteractDive(")
    end = CLIENT_JS.index("function openInteractMoment(")
    return CLIENT_JS[start:end]


class TestTheCloseUpIsWiderThanTheBox(unittest.TestCase):
    """Cropping tight on the detection box made the dive read as a cut.

    Widening it too far caused the opposite and worse failure: INTERACT on a
    truck came back as a close-up of the monitor beside it, because the crop
    handed to img2img was mostly not the truck.
    """

    def test_the_pull_in_leaves_the_subject_dominant(self):
        # 0.5 meant "crop twice the box", and subjectNormBox's default padding
        # had already widened it 1.24x -- 2.48x per side, so the subject was
        # about a sixth of the pixels. 0.8 keeps context without the ambiguity.
        self.assertIn("const CLOSEUP_ZOOM = 0.8;", CLIENT_JS)

    def test_the_subject_is_most_of_its_own_crop(self):
        """The property that actually matters, stated as arithmetic."""
        pad, zoom = 0.12, 0.8
        per_side = (1 + pad * 2) / zoom
        self.assertLess(per_side, 1.7, "crop is too loose to identify a subject")
        self.assertGreater(1.0 / (per_side ** 2), 0.34,
                           "subject should be over a third of the crop's area")

    def test_both_dives_relax_their_crop(self):
        # SPEAK goes through subjectTalkCropBox; INTERACT crops its own.
        self.assertIn("return relaxCloseUpBox({", CLIENT_JS)
        self.assertIn("relaxCloseUpBox(subjectNormBox(obj),", CLIENT_JS)

    def test_interact_passes_the_other_detections_in(self):
        """Widening must know what else is on screen, or it cannot avoid it."""
        self.assertIn("(state.scanObjects || []).filter((o) => o !== obj)", CLIENT_JS)

    def test_widening_is_given_up_rather_than_annex_another_object(self):
        # Restate the JS ladder: the largest widening that does not newly
        # contain another detection's centre wins, and the tight box always does.
        def relax(box, others, zooms=(0.8, 0.85, 0.9, 0.95, 1.0)):
            cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2

            def widen(z):
                w, h = min(1, box[2] / z), min(1, box[3] / z)
                return (max(0, min(1 - w, cx - w / 2)),
                        max(0, min(1 - h, cy - h / 2)), w, h)

            def inside(b, x, y):
                return b[0] <= x <= b[0] + b[2] and b[1] <= y <= b[1] + b[3]

            for z in zooms:
                cand = widen(z)
                if z >= 1:
                    return cand
                if not any(inside(cand, ox, oy) for ox, oy in others
                           if not inside(box, ox, oy)):
                    return cand
            return widen(1.0)

        # A truck at the left, a monitor close on its right. The widened crop
        # must not reach the monitor's centre.
        truck = (0.10, 0.40, 0.24, 0.30)
        monitor = [(0.42, 0.50)]
        x, y, w, h = relax(truck, monitor)
        self.assertFalse(x <= 0.42 <= x + w and y <= 0.50 <= y + h,
                         "the crop swallowed the neighbouring object")
        self.assertGreaterEqual(w, truck[2], "the crop should never shrink")

        # Nothing nearby: the full widening is taken.
        alone = relax(truck, [(0.90, 0.90)])
        self.assertAlmostEqual(alone[2], truck[2] / 0.8, places=6)

    def test_a_relaxed_box_stays_inside_the_frame(self):
        # The helper is JS, so restate it here: a box already covering most of
        # the frame must not be widened off the edge of it.
        def relax(x, y, w, h, zoom=0.5):
            cx, cy = x + w / 2, y + h / 2
            nw, nh = min(1, w / zoom), min(1, h / zoom)
            return (max(0, min(1 - nw, cx - nw / 2)),
                    max(0, min(1 - nh, cy - nh / 2)), nw, nh)

        for box in ((0.4, 0.4, 0.2, 0.2), (0.0, 0.0, 0.1, 0.1),
                    (0.7, 0.7, 0.3, 0.3), (0.1, 0.1, 0.8, 0.8)):
            x, y, w, h = relax(*box)
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + w, 1.0000001)
            self.assertLessEqual(y + h, 1.0000001)
            self.assertGreaterEqual(w, box[2])


class TestTheDiveLeavesOnItsOwn(unittest.TestCase):
    """No slate, no click. Reach out, look, get handed back."""

    def test_there_is_nothing_to_press(self):
        """The slate is what made this feel slow: three rows the player had to
        read, wait on, and then click through to leave."""
        src = _dive_source()
        for verb in ("SPEAK", "ATTACK", "LEAVE"):
            self.assertNotIn(f'"{verb}"', src, f"{verb} is still on the dive")
        self.assertNotIn("setChoices", src, "the dive must not offer a slate")

    def test_it_hands_back_on_the_frame_not_on_the_whole_turn(self):
        """The new scene and the choice prompt are several seconds apart
        (~19s vs ~22s+), and waiting for the second buys the player nothing:
        they are being handed back to a PICTURE, and choices landing a beat
        after it is what an ordinary turn looks like anyway."""
        src = _dive_source()
        ready = src[src.index("function readyToHandBack()"):
                    src.index("// makeChoice refuses a turn outright")]
        self.assertIn("return scenePainted || turnOver();", ready)

    def test_the_painted_frame_triggers_the_exit(self):
        src = _dive_source()
        self.assertIn("onNextScenePainted(() => { scenePainted = true; sync(); });", src)
        sync = src[src.index("function sync()"):src.index("onNextScenePainted(")]
        self.assertIn("if (!readyToHandBack()) return;", sync)
        self.assertIn("exitDive();", sync)

    def test_a_close_up_is_never_flashed(self):
        """Only bites when the turn resolves unusually fast or was refused."""
        src = _dive_source()
        self.assertIn("MIN_ON_SCREEN_MS", src)
        self.assertIn("Date.now() - shownAt < MIN_ON_SCREEN_MS", src)

    def test_escape_still_leaves_immediately(self):
        """Auto-exit is the normal way out, but nobody should have to sit
        through a render they have finished with."""
        src = _dive_source()
        leave = src[src.index("function leave()"):src.index("function status()")]
        self.assertIn("exitDive();", leave)
        self.assertNotIn("readyToHandBack", leave)
        interact = CLIENT_JS[CLIENT_JS.index('window.Moments.register("interact"'):
                             CLIENT_JS.index("function createInteractDive(")]
        self.assertIn("dive.leave()", interact)


class TestTheWaitSaysSomething(unittest.TestCase):
    """The consequence prose lands ~3s in; the picture not until ~19s.

    The sentence saying what the player just caused was being written to the
    feed behind the letterbox, where the HUD is hidden — so the dive showed a
    static close-up and nothing else for sixteen seconds."""

    def test_the_dive_subscribes_to_the_consequence(self):
        src = _dive_source()
        self.assertIn("onNextConsequence((text) => {", src)
        self.assertIn("consequence = clipToSentence(text, 120);", src)

    def test_the_turn_publishes_it_exactly_where_the_ceremony_sees_it(self):
        self.assertIn("function onNextConsequence(", CLIENT_JS)
        self.assertIn("function flushConsequenceWaiters(", CLIENT_JS)
        # Same branch the ceremony uses for "the consequence landed".
        block = CLIENT_JS[CLIENT_JS.index('Ceremony.reach("consequence");'):]
        self.assertIn("flushConsequenceWaiters(item.content", block[:400])

    def test_it_reaches_the_nameplate(self):
        src = _dive_source()
        status = src[src.index("function status()"):src.index("// A Moment on top")]
        self.assertIn("if (consequence) return consequence;", status)

    def test_a_subscriber_fires_once_and_is_dropped(self):
        """A dive that outlived its turn must not eat the next one's prose."""
        flush = CLIENT_JS[CLIENT_JS.index("function flushConsequenceWaiters("):]
        flush = flush[:flush.index("\n  }")]
        self.assertIn("splice(0, consequenceWaiters.length)", flush)

    def test_a_long_beat_is_cut_at_a_sentence(self):
        """Restate clipToSentence: whole sentences only, because a hard cut
        mid-clause reads as the text being broken rather than brief."""
        def clip(text, cap=120):
            import re as _re
            t = _re.sub(r"\s+", " ", text or "").strip()
            if not t:
                return ""
            m = _re.search(r"(?<!\.)[.!?](?!\.)", t)
            first = t[:m.end()] if m else t
            return first if len(first) <= cap else first[:cap - 1].rstrip() + "\u2026"

        self.assertEqual(clip("The lid gives. Something shifts inside."),
                         "The lid gives.")
        self.assertEqual(clip("Nothing moves"), "Nothing moves")
        self.assertEqual(clip(""), "")
        self.assertTrue(clip("x" * 400).endswith("\u2026"))
        self.assertLessEqual(len(clip("x" * 400)), 120)


class TestTheDiveAlwaysLetsYouOut(unittest.TestCase):
    """The exits wait for the turn, and every way a turn can end releases them.

    Watching only for a painted frame was not enough. A turn can finish having
    drawn nothing at all — the still gets content-filtered, the generator is
    off, the watchdog gives up — and a dive gated purely on paint then sat
    locked for its whole three-minute hold-out while the world behind it had
    already moved on and put fresh choices on the wheel.
    """

    def test_the_turn_ending_releases_the_dive_even_with_no_new_frame(self):
        src = _dive_source()
        self.assertIn("function turnOver()", src)
        self.assertIn("return scenePainted || turnOver();", src)

    def test_turn_over_waits_out_the_guide_image(self):
        # Ceremony stays active while the still renders, AFTER the prose and
        # choices have landed. Without it, "the turn ended" would be true
        # seconds before there was any new frame to hand the player back to.
        src = _dive_source()
        over = src[src.index("function turnOver()"):src.index("// Time to give the world back")]
        self.assertIn("Ceremony.isActive", over)
        self.assertIn("!state.awaitingResolution", over)
        self.assertIn("!state.processing", over)

    def test_every_turn_ending_clears_the_ceremony(self):
        # turnOver() leans on that, so pin it: hideVeil is the one funnel every
        # failure path goes through, and it must reset the ceremony.
        client = CLIENT_JS[CLIENT_JS.index("function hideVeil()"):]
        client = client[:client.index("\n  }")]
        self.assertIn("Ceremony.reset();", client)
        self.assertIn("state.processing = false;", client)

    def test_a_dive_with_no_turn_behind_it_shows_the_close_up_and_goes(self):
        """makeChoice refuses outright in some states. With nothing coming,
        the close-up IS the whole beat — held long enough to read as one
        rather than flashed past."""
        src = _dive_source()
        self.assertIn("function armTurn(dispatched)", src)
        self.assertIn("if (!turnArmed) return false;", src)
        self.assertIn("if (!turnExpected) return Date.now() - armedAt >= NO_TURN_DWELL_MS;",
                      src)

    def test_the_press_tells_the_dive_whether_a_turn_went_out(self):
        commit = CLIENT_JS[CLIENT_JS.index("function commitScanAction("):]
        commit = commit[:commit.index("\n  // ---")]
        # openInteractMoment has to hand the dive back synchronously for this
        # ordering to be possible at all.
        self.assertIn("? openInteractMoment(obj)", commit)
        self.assertLess(commit.index("openInteractMoment(obj)"), commit.index("makeChoice("))
        self.assertLess(commit.index("makeChoice("),
                        commit.index("dive.armTurn(state.awaitingResolution)"))

    def test_open_returns_the_dive_without_awaiting_the_close_up(self):
        src = CLIENT_JS[CLIENT_JS.index("function openInteractMoment("):]
        src = src[:src.index("\n  }")]
        self.assertNotIn("async function openInteractMoment", CLIENT_JS)
        self.assertIn("return dive;", src)

    def test_a_refused_pop_does_not_strand_the_player(self):
        # Moments.pop drops the request while another Moment is mid-
        # choreography. Retiring anyway would leave a close-up nothing is
        # watching, and nothing left to take it down.
        src = _dive_source()
        ex = src[src.index("async function exitDive()"):src.index("// Esc. The auto-exit")]
        self.assertIn("if (onTop()) { apply(); return false; }", ex)
        self.assertLess(ex.index("if (onTop())"), ex.index("retire();"))

    def test_the_run_ending_clears_the_dive_out_of_the_way(self):
        # enterGameOver closes a conversation and aborts an encounter, but it
        # knows nothing about the Moment stack — so a dive would sit over the
        # death overlay waiting for a turn that is never coming.
        src = _dive_source()
        self.assertIn("if (state.gameOver) { exitDive(); return; }", src)

    def test_the_hold_out_is_the_backstop_under_the_turn_watchdog(self):
        src = _dive_source()
        self.assertIn("HOLD_MAX_MS", src)
        self.assertIn("> HOLD_MAX_MS", src)

    def test_the_nameplate_does_not_claim_a_change_that_never_happened(self):
        src = _dive_source()
        status = src[src.index("function status()"):src.index("// A Moment on top")]
        self.assertIn('scenePainted ? "the scene has changed"', status)

    def test_the_dive_does_not_paint_over_a_moment_above_it(self):
        src = _dive_source()
        self.assertIn("function onTop()", src)
        self.assertIn("if (done || exiting || !onTop()) return;", src)


class TestAnAimedEncounterStillWorks(unittest.TestCase):
    """ATTACK went with the slate, so nothing aims an encounter today — but
    the server half is the wiring any future "pick a fight with THAT" verb
    comes back through, and it stays pinned rather than rotting."""

    def test_no_client_path_aims_one_any_more(self):
        self.assertNotIn("Encounter.start({ subject: obj })", CLIENT_JS)
        # The endpoint still carries a target when one is given.
        self.assertIn("subject: forcedSubject || undefined", CLIENT_JS)

    def test_a_named_target_skips_the_roster_roll(self):
        with mock.patch.object(encounter, "roll_encounter_kind") as rolled, \
                mock.patch.object(encounter, "encounter_lore_context", return_value=""), \
                mock.patch("engine._ask", return_value="") as ask:
            encounter.build_encounter_brief(
                "default", target={"label": "a rusted radio mast", "kind": "object"})
        rolled.assert_not_called()
        prompt = ask.call_args.args[0] if ask.call_args.args else ask.call_args.kwargs["prompt"]
        self.assertIn("THE PLAYER HAS JUST ATTACKED: a rusted radio mast", prompt)
        self.assertNotIn("THIS ENCOUNTER IS:", prompt)

    def test_an_unaimed_encounter_still_rolls(self):
        with mock.patch.object(encounter, "roll_encounter_kind",
                               return_value="something out of Shaft 6") as rolled, \
                mock.patch.object(encounter, "encounter_lore_context", return_value=""), \
                mock.patch("engine._ask", return_value="") as ask:
            encounter.build_encounter_brief("default")
        rolled.assert_called_once()
        prompt = ask.call_args.args[0] if ask.call_args.args else ask.call_args.kwargs["prompt"]
        self.assertIn("THIS ENCOUNTER IS: something out of Shaft 6", prompt)

    def test_a_failed_brief_still_falls_back_to_the_target(self):
        # No LLM: the fallback brief must still be about the thing the player
        # hit, or ATTACK quietly turns into a random encounter.
        with mock.patch.object(encounter, "encounter_lore_context", return_value=""), \
                mock.patch("engine._ask", side_effect=RuntimeError("no key")):
            brief = encounter.build_encounter_brief(
                "default", target={"label": "a rusted radio mast"})
        self.assertIn("radio mast", brief["character"]["label"].lower())


class TestTheEndpointCarriesTheTarget(unittest.TestCase):
    """The wiring between the client's ATTACK and the brief that answers it."""

    class _Stop(Exception):
        """Bail out of api_begin once both spies have been reached."""

    def _begin(self, payload):
        import engine
        from flask import Flask

        seen = {}

        # `**_` on both: these spies exist to capture the target, and api_begin
        # grows keyword arguments for reasons that have nothing to do with it
        # (world_flavor, detection). Pinning the full signature here just turns
        # every unrelated addition into three red tests in this file.
        def spy_brief(session_id="default", image_path=None, place_hold="",
                      vision=None, target=None, detection=0, **_):
            seen["brief"] = target
            seen["detection"] = detection
            return {"character": {"label": "x", "kind": "person", "look": "y"},
                    "danger": "z"}

        def spy_plate(brief, img2img=True, setting="", target=None, **_):
            seen["plate"] = target
            raise self._Stop()

        app = Flask(__name__)
        with app.test_request_context("/api/encounter/begin", json=payload), \
                mock.patch.object(engine, "_rate_limited", return_value=False), \
                mock.patch.object(engine, "_resolve_request_session_id",
                                  return_value="default"), \
                mock.patch.object(engine, "_load_state", return_value={}), \
                mock.patch.object(engine, "_save_portrait_reference",
                                  return_value="ref.png"), \
                mock.patch.object(encounter, "set_look_session"), \
                mock.patch.object(encounter, "read_place_lock",
                                  return_value={"place_hold": "", "setting": "outdoor"}), \
                mock.patch.object(encounter, "build_encounter_brief", spy_brief), \
                mock.patch.object(encounter, "build_encounter_plate_prompt", spy_plate):
            with self.assertRaises(self._Stop):
                encounter.api_begin()
        return seen

    def test_a_posted_subject_reaches_both_the_brief_and_the_plate(self):
        seen = self._begin({"frame": "data:image/jpeg;base64,AA",
                            "subject": {"label": "a rusted radio mast",
                                        "kind": "object"}})
        self.assertEqual(seen["brief"]["label"], "a rusted radio mast")
        self.assertEqual(seen["plate"]["label"], "a rusted radio mast")

    def test_a_rolled_encounter_carries_no_target(self):
        seen = self._begin({"frame": "data:image/jpeg;base64,AA"})
        self.assertIsNone(seen["brief"])
        self.assertIsNone(seen["plate"])

    def test_a_subject_with_no_label_is_not_a_target(self):
        # An empty label would otherwise reach the prompt as "THE PLAYER HAS
        # JUST ATTACKED: " and suppress the roster roll for nothing.
        seen = self._begin({"frame": "data:image/jpeg;base64,AA",
                            "subject": {"label": "   "}})
        self.assertIsNone(seen["brief"])

    def test_the_brief_is_told_how_much_the_world_already_knew(self):
        """How the encounter OPENS hangs on this: hidden buys the beat before
        being noticed, hunted means the thing followed you here."""
        seen = self._begin({"frame": "data:image/jpeg;base64,AA"})
        self.assertEqual(seen["detection"], 0)


class TestThePlateKnowsTheThingIsAlreadyThere(unittest.TestCase):
    BRIEF = {
        "character": {"label": "a rusted radio mast", "kind": "anomaly",
                      "look": "corroded lattice, guy-wires singing"},
        "danger": "it is leaning down over you",
    }

    def test_an_aimed_plate_does_not_add_a_second_one(self):
        prompt = encounter.build_encounter_plate_prompt(
            self.BRIEF, img2img=True, setting="outdoor",
            target={"label": "a rusted radio mast"})
        self.assertIn("ALREADY in this photograph", prompt)
        self.assertIn("do not add a second one", prompt)
        self.assertNotIn("ADD the new character and danger INTO", prompt)

    def test_a_rolled_plate_still_adds_the_arrival(self):
        prompt = encounter.build_encounter_plate_prompt(
            self.BRIEF, img2img=True, setting="outdoor")
        self.assertIn("ADD the new character and danger INTO", prompt)
        self.assertNotIn("ALREADY in this photograph", prompt)

    def test_the_place_lock_survives_either_way(self):
        for target in (None, {"label": "a rusted radio mast"}):
            prompt = encounter.build_encounter_plate_prompt(
                self.BRIEF, img2img=True, setting="outdoor", target=target)
            self.assertIn("PLACE LOCK — HARD", prompt)
            self.assertIn("do not teleport", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
