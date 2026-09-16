"""The INTERACT close-up has to be somewhere you can DO something.

The dive used to be a photograph of the thing you poked with a single X on it,
which is a loading screen with production values. This pins the three answers
it now offers, and the two rules that make them honest:

* SPEAK is live immediately — it is the thing worth doing while the INTERACT
  turn draws — and nests the conversation on the close-up already on screen;
* ATTACK and LEAVE both depend on the frame that turn is still making (one
  walks out onto it, the other stages a fight in it), so they are on the slate
  LOCKED until it lands rather than missing or lying;
* an attacked object is the encounter, not a prompt for one: the roster draw is
  skipped and the plate is told the thing is ALREADY in the photograph, so the
  fight does not arrive as a stranger standing next to it.

Plus the framing change both dives share: the pull-in toward a detection's box
is half as hard, so a close-up still shares pixels with the frame it came from.

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


class TestTheSlate(unittest.TestCase):
    """Three answers, and only one of them available while the turn draws."""

    def test_all_three_verbs_are_offered(self):
        src = _dive_source()
        for verb in ("SPEAK", "ATTACK", "LEAVE"):
            self.assertIn(f'"{verb}"', src, f"{verb} is missing from the dive slate")

    def test_speak_is_never_locked(self):
        # The dive exists so the ~30s turn is time spent doing something. If
        # SPEAK waited for the frame too, the close-up would be a progress bar
        # again for the whole of that wait.
        src = _dive_source()
        slate = src[src.index("function slate()"):src.index("function onTop()")]
        speak_row = next(ln for ln in slate.splitlines() if '"SPEAK"' in ln)
        self.assertNotIn("locked", speak_row)
        self.assertEqual(slate.count("locked: locked"), 2)

    def test_the_two_exits_wait_for_the_frame(self):
        src = _dive_source()
        # Both exits check it before doing anything irreversible, not just the
        # slate's disabled state — Esc reaches leave() without a click.
        self.assertIn("if (done || exiting || !settled()) return;", src)   # attack
        self.assertIn("if (settled()) { exitDive(); return; }", src)       # leave

    def test_a_locked_row_cannot_be_clicked(self):
        set_choices = MOMENTS_JS[MOMENTS_JS.index("function setChoices("):
                                 MOMENTS_JS.index("let customState = null;")]
        self.assertIn("moment-choice-locked", set_choices)
        self.assertIn("btn.disabled = true;", set_choices)
        # The early return is what keeps the click / hover listeners off it.
        self.assertIn("box.appendChild(btn);\n        return;", set_choices)

    def test_a_locked_row_is_still_readable(self):
        self.assertIn(".moment-choice-locked", CSS)
        self.assertIn("body.moment-interact .moment-choice-locked", CSS)


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
        self.assertIn("return scenePainted ? !state.processing : turnOver();", src)

    def test_turn_over_waits_out_the_guide_image(self):
        # Ceremony stays active while the still renders, AFTER the prose and
        # choices have landed. Releasing on awaitingResolution alone would put
        # LEAVE up seconds before the frame it promises.
        src = _dive_source()
        over = src[src.index("function turnOver()"):src.index('// "There is something')]
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

    def test_a_dive_with_no_turn_behind_it_is_not_locked(self):
        src = _dive_source()
        self.assertIn("function armTurn(dispatched)", src)
        self.assertIn("if (!turnArmed) return false;", src)
        self.assertIn("if (!turnExpected) return true;", src)

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
        # choreography. Retiring anyway would leave a close-up whose slate is
        # wired to a dead dive.
        src = _dive_source()
        ex = src[src.index("async function exitDive()"):src.index("function leave()")]
        self.assertIn("if (onTop()) { apply(); return false; }", ex)
        self.assertLess(ex.index("if (onTop())"), ex.index("retire();"))

    def test_the_run_ending_clears_the_dive_out_of_the_way(self):
        # enterGameOver closes a conversation and aborts an encounter, but it
        # knows nothing about the Moment stack — so a dive would sit over the
        # death overlay with a slate whose verbs all refuse.
        src = _dive_source()
        self.assertIn("if (state.gameOver) { exitDive(); return; }", src)
        speak = src[src.index("function speak()"):src.index("function slate()")]
        self.assertIn("state.gameOver", speak)

    def test_the_nameplate_does_not_claim_a_change_that_never_happened(self):
        src = _dive_source()
        status = src[src.index("function status()"):src.index("function apply()")]
        self.assertIn('scenePainted ? "the scene has changed"', status)


class TestSpeakNestsOnTheCloseUp(unittest.TestCase):
    def test_speaking_cancels_a_pending_leave(self):
        # Esc while the frame is developing leaves a standing request to go.
        # Without clearing it, picking SPEAK and then hanging up ejected the
        # player out of the dive the instant the conversation closed.
        src = _dive_source()
        speak = src[src.index("function speak()"):src.index("function slate()")]
        self.assertIn("wantsOut = false;", speak)
        self.assertLess(speak.index("wantsOut = false;"), speak.index("Talk.start("))

    def test_talk_is_handed_the_plate_already_on_screen(self):
        src = _dive_source()
        self.assertIn("Talk.start(", src)
        self.assertIn("reference_image: dive.closeUpUrl || dive.referenceFrame", src)

    def test_the_dive_redraws_itself_when_the_conversation_pops(self):
        # Moments.pop clears the portrait and the choices on a nested pop, so
        # without a resume hook hanging up lands on an empty letterbox.
        interact = CLIENT_JS[CLIENT_JS.index('window.Moments.register("interact"'):
                             CLIENT_JS.index("function createInteractDive(")]
        self.assertIn("async resume(entry)", interact)
        self.assertIn("dive.restore()", interact)
        # And the portrait frame has to come back out of hiding with it.
        set_portrait = MOMENTS_JS[MOMENTS_JS.index("function setPortrait("):
                                  MOMENTS_JS.index("function clearPortrait(")]
        self.assertIn('p.classList.remove("hidden")', set_portrait)

    def test_the_dive_does_not_paint_over_an_open_conversation(self):
        src = _dive_source()
        self.assertIn("function onTop()", src)
        self.assertIn("if (done || exiting || !onTop()) return;", src)


class TestAttackHandsTheFightItsTarget(unittest.TestCase):
    def test_the_client_names_the_subject(self):
        src = _dive_source()
        self.assertIn("Encounter.start({ subject: obj })", src)
        self.assertIn("subject: forcedSubject || undefined", CLIENT_JS)

    def test_the_dive_is_left_before_the_encounter_opens(self):
        # Encounter.start refuses under an open Moment, and its plate is
        # img2img off what is on screen — so the exit is load-bearing, not
        # cosmetic, it has to be awaited, and a refused pop must abort the
        # swing rather than fire a fight the player cannot see.
        src = _dive_source()
        attack = src[src.index("async function attack()"):src.index("// SPEAK nests")]
        self.assertIn("if (!(await exitDive())) return;", attack)
        self.assertLess(attack.index("await exitDive()"),
                        attack.index("Encounter.start"))

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

        def spy_brief(session_id="default", image_path=None, place_hold="",
                      vision=None, target=None):
            seen["brief"] = target
            return {"character": {"label": "x", "kind": "person", "look": "y"},
                    "danger": "z"}

        def spy_plate(brief, img2img=True, setting="", target=None):
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
