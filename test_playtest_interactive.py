"""
test_playtest_interactive.py — keeps the scan-driven playtester honest.

The point of `playtest_interactive.py` is to exercise the SCAN path the way a
player does. That only holds while the harness sends what the real client
sends. If `standalone.js` changes the composed action phrase or the field names
on /api/choose and the harness doesn't follow, the harness keeps passing while
testing an interaction the game no longer has — the worst possible failure for
a test tool, because it reports coverage it isn't providing.

So the checks here are parity checks against the client source plus the pure
selection logic:

  * The MOVE phrasing must match the client's single, universal phrase.
    Both sides used to branch between an "enter" and a "walk over to" wording
    because the SERVER inferred hard-cut-vs-not from that text; now `is_move`
    alone forces the cut (see advance_turn_image_fast in engine.py), so
    there's only one phrase left to keep in sync.
  * `source` / `subject` are the fields the server reads to flag an object
    interaction; a rename on either side silently downgrades every scan turn
    into a plain choice.
  * An empty scan must degrade to a button press rather than stall the run,
    and must be RECORDED as degraded so the summary can't claim scan coverage
    it never got.

No network and no server: everything here is pure functions or source text.

Run with:
    python3 -m unittest test_playtest_interactive -v
"""

import json
import re
import tempfile
import unittest
from pathlib import Path

from PIL import Image

import playtest_interactive as pi

ROOT = Path(__file__).parent.resolve()
CLIENT_JS = (ROOT / "static" / "js" / "standalone.js").read_text(encoding="utf-8", errors="replace")


class TestActionPhrasesMatchTheClient(unittest.TestCase):
    """The composed phrase IS the input the story model sees. If the harness
    words it differently, it is testing a turn no player can produce."""

    def test_interact_phrase_matches_client(self):
        self.assertIn('"Interact with the " + o + "."', CLIENT_JS)
        self.assertEqual(pi.interact_phrase("steel door"), "Interact with the steel door.")

    def test_move_phrase_matches_client(self):
        # ONE phrase for every object, regardless of what it is. This used to
        # branch between an "Enter the X" and a "Walk over to the X" wording,
        # picked from a curated "enterable" word list, because the SERVER
        # decided hard-cut-vs-not by pattern-matching the resulting text —
        # so whether MOVE actually changed the scene depended on whether the
        # object's name happened to be on a list. MOVE is now an
        # unconditional hard cut (see is_move in advance_turn_image_fast), so
        # there's nothing left for the phrasing to negotiate.
        self.assertIn('return "Move to the " + o + ".";', CLIENT_JS)
        for label in ("steel door", "oil pump", "broken glass", "warehouse"):
            self.assertEqual(pi.move_phrase(label), f"Move to the {label}.")

    def test_move_cuts_to_a_new_scene_no_matter_the_object(self):
        # The invariant the old enterable/relocate split existed to fake:
        # every scan-driven MOVE gets a fresh composition. It's no longer
        # decided by the phrase text at all — `is_move` alone forces it (see
        # TestMoveIsAlwaysAHardCut in test_simulation_pacing.py) — so this
        # just documents that the harness's phrase carries none of the old
        # location-change keywords that used to matter.
        for label in ("steel door", "tunnel", "oil pump", "rock", "warning sign"):
            phrase = pi.move_phrase(label).lower()
            self.assertNotIn("enter", phrase)
            self.assertNotIn("cross over", phrase)


class TestChoosePayloadFieldsMatchTheClient(unittest.TestCase):
    """`source` and `subject` are how the server knows a turn came from a
    tapped object (engine flags is_interaction off `source`, and pins object
    permanence to `subject`). Both sides must agree on the names."""

    def test_client_sends_source_and_subject(self):
        self.assertIn("source: actionSource", CLIENT_JS)
        self.assertIn("subject: actionSubject", CLIENT_JS)
        self.assertIn('const source = action.id === "move" ? "scan_move" : "scan_interact";', CLIENT_JS)

    def test_scan_detect_opts_into_the_scene_object_cache(self):
        """`purpose: "scan"` is what makes the server remember the labels for the
        turn. Drop it on either side and the consequence stops being grounded in
        what is on screen — silently, because SCAN itself keeps working."""
        self.assertIn('postJSON("/api/detect", { frame: cap.frame, purpose: "scan" })', CLIENT_JS)
        self.assertIn('"purpose": "scan"', (ROOT / "playtest_interactive.py").read_text(
            encoding="utf-8", errors="replace"))

    def test_photo_targeting_stays_a_read_only_probe(self):
        # It polls /api/detect every ~2.5s while the camera is armed; opting that
        # loop into the cache would put a locked state write on a hot path.
        self.assertIn('postJSON("/api/detect", { frame: cap.frame })', CLIENT_JS)

    def test_interact_action_payload(self):
        objs = [{"label": "oil pump", "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.2}]
        action = pi.plan_action("scan_interact", objs, ["press a button"], 0)
        self.assertEqual(action["source"], "scan_interact")
        self.assertEqual(action["subject"], "oil pump")
        self.assertEqual(action["choice"], "Interact with the oil pump.")
        self.assertFalse(action["degraded"])

    def test_move_action_payload(self):
        objs = [{"label": "steel door"}]
        action = pi.plan_action("scan_move", objs, [], 0)
        self.assertEqual(action["source"], "scan_move")
        self.assertEqual(action["subject"], "steel door")
        self.assertEqual(action["choice"], "Move to the steel door.")

    def test_plain_choice_pins_nothing(self):
        # A button press names no specific object, so it must NOT set a
        # subject — doing so would pin permanence to a thing nobody tapped.
        action = pi.plan_action("choice", [{"label": "oil pump"}], ["Run for the fence"], 0)
        self.assertIsNone(action["source"])
        self.assertIsNone(action["subject"])
        self.assertEqual(action["choice"], "Run for the fence")


class TestEmptyScanDegradesVisibly(unittest.TestCase):
    """A scan that finds nothing must not stall the run — but it also must not
    be counted as scan coverage."""

    def test_falls_back_to_a_button_press(self):
        action = pi.plan_action("scan_interact", [], ["Run for the fence"], 0)
        self.assertEqual(action["kind"], "choice")
        self.assertEqual(action["choice"], "Run for the fence")

    def test_fallback_is_flagged_as_degraded(self):
        self.assertTrue(pi.plan_action("scan_move", [], ["Run"], 0)["degraded"])
        self.assertFalse(pi.plan_action("choice", [], ["Run"], 0)["degraded"])

    def test_no_choices_at_all_still_produces_an_action(self):
        action = pi.plan_action("scan_interact", [], [], 0)
        self.assertTrue(action["choice"])


class TestObjectSelection(unittest.TestCase):
    """Which hotspot gets 'clicked'. Always taking objects[0] would hammer one
    detection for a whole run and prove nothing about the rest.

    There used to be an "enterable-preferring" mode for MOVE turns, so a run
    would skew toward doors/tunnels/etc. That existed only because entering
    something was the one way to reliably get a hard cut out of the old
    text classifier. Now every MOVE is an unconditional hard cut regardless
    of what's tapped (see is_move in advance_turn_image_fast), so plain
    round-robin selection is representative for every kind."""

    def test_rotates_across_objects(self):
        objs = [{"label": "a"}, {"label": "b"}, {"label": "c"}]
        picked = [pi.pick_object(objs, i)["label"] for i in range(4)]
        self.assertEqual(picked, ["a", "b", "c", "a"])

    def test_empty_detection_list_selects_nothing(self):
        self.assertIsNone(pi.pick_object([], 0))


class TestScanFrameCapture(unittest.TestCase):
    """The detector is handed the same thing the browser hands it — a
    downscaled JPEG data URL (captureScanFrame in standalone.js)."""

    def test_client_captures_jpeg_at_640(self):
        self.assertIn('toDataURL("image/jpeg", 0.72)', CLIENT_JS)

    def test_realtime_scan_does_not_fall_back_to_the_still(self):
        """When the reactor feed is showing, a failed live grab must not
        silently scan the last Gemini still — that is how MOVE TO offers
        the previous location's nouns."""
        fn = CLIENT_JS.split("function captureScanFrame()", 1)[1].split("function ", 1)[0]
        self.assertIn("scanInRealtime()", fn)
        self.assertIn("return null;", fn)

    def test_a_configured_but_disconnected_renderer_scans_the_still(self):
        """The bug that made SCAN look dead in stills-only play.

        `reactorAvailable()` only means a key is configured; it stays true when
        the stream never connected. Gating the live-grab branch on it captured
        the empty video element, so every SCAN posted a black rectangle and the
        detector honestly answered "nothing here" over a screen full of things.
        Only `scanInRealtime()` asks the question that matters — is the feed
        actually showing — so that is the gate.
        """
        fn = CLIENT_JS.split("function captureScanFrame()", 1)[1].split("function ", 1)[0]
        code = "\n".join(l for l in fn.splitlines() if not l.strip().startswith("//"))
        self.assertNotIn("reactorAvailable()", code)
        # The camera's evidence crop reads the scene the same way and had the
        # same blindness — a black crop filed as a photographed specimen.
        crop = CLIENT_JS.split("function captureSceneRegion(", 1)[1].split("function ", 1)[0]
        crop = "\n".join(l for l in crop.splitlines() if not l.strip().startswith("//"))
        self.assertNotIn("reactorAvailable()", crop)

    def test_an_unpainted_freeze_buffer_is_not_offered_as_a_frame(self):
        """A canvas nobody drew to still reports width 300 (the HTML default),
        so the freeze buffer handed back a fully transparent 'frame'. Callers
        cannot tell that from a real grab — captureFrame must return null."""
        reactor = (ROOT / "static" / "js" / "reactor_renderer.js").read_text(
            encoding="utf-8", errors="replace")
        fn = reactor.split("function captureSource()", 1)[1].split("function ", 1)[0]
        self.assertIn("rstate.freezePainted", fn)
        # And both painters have to raise the flag, or the guard blocks real frames.
        self.assertEqual(reactor.count("rstate.freezePainted = true;"), 2)

    def test_realtime_photo_crop_does_not_fall_back_to_the_still(self):
        """Camera shutter uses captureSceneRegion. Same rule: if the live
        crop fails while realtime is showing, miss — don't file the still."""
        fn = CLIENT_JS.split("function captureSceneRegion(", 1)[1].split("function ", 1)[0]
        self.assertIn("scanInRealtime()", fn)
        self.assertIn("captureRegion", fn)
        self.assertIn("return null;", fn)

    def test_produces_a_jpeg_data_url(self):
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (1600, 900), (30, 40, 50)).save(buf, format="PNG")
        frame = pi.to_scan_frame(buf.getvalue())
        self.assertTrue(frame.startswith("data:image/jpeg;base64,"))

    def test_downscales_wide_frames(self):
        import base64
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (1600, 900), (30, 40, 50)).save(buf, format="PNG")
        frame = pi.to_scan_frame(buf.getvalue(), width=640)
        raw = base64.b64decode(frame.split(",", 1)[1])
        self.assertEqual(Image.open(io.BytesIO(raw)).width, 640)

    def test_small_frames_are_not_upscaled(self):
        import base64
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (320, 200), (30, 40, 50)).save(buf, format="PNG")
        raw = base64.b64decode(pi.to_scan_frame(buf.getvalue()).split(",", 1)[1])
        self.assertEqual(Image.open(io.BytesIO(raw)).width, 320)


class TestVerdict(unittest.TestCase):
    """The summary is the only thing anyone reads. It must not report scan
    coverage a run didn't actually get."""

    def _turn(self, **kw):
        base = {
            "turn": 1, "planned": "scan_interact", "action_kind": "scan_interact",
            "degraded_to_choice": False, "status": "resolved", "detection_labels": ["oil pump"],
            "subject": "oil pump", "scene_changed": True, "choices_changed": True,
            "subject_kept_in_next_scene": True,
        }
        base.update(kw)
        return base

    def _run(self, turns, require_scan=True):
        return {"turns": turns, "require_scan": require_scan}

    def test_healthy_run_passes(self):
        v = pi.summarize(self._run([self._turn(), self._turn(turn=2)]))
        self.assertTrue(v["passed"], v["checks"])

    def test_a_scan_that_never_found_anything_fails(self):
        v = pi.summarize(self._run([
            self._turn(detection_labels=[], action_kind="choice", subject=None,
                       degraded_to_choice=True, subject_kept_in_next_scene=None),
        ]))
        self.assertFalse(v["checks"]["scan finds objects on nearly every frame"])
        self.assertEqual(v["scan_turns_degraded_to_choice"], 1)
        self.assertEqual(v["scan_turns_committed"], 0)

    def test_mostly_blind_scans_fail_even_when_one_scan_worked(self):
        """The check this replaces was "did ANY scan find something", which
        passed a run where four of six scans came back empty on frames full of
        props — it hid the truncated-detection bug for a whole run."""
        blind = self._turn(detection_labels=[], action_kind="choice", subject=None,
                           degraded_to_choice=True, subject_kept_in_next_scene=None)
        v = pi.summarize(self._run([self._turn()] + [dict(blind, turn=i) for i in range(2, 7)]))
        self.assertFalse(v["checks"]["scan finds objects on nearly every frame"])
        self.assertLess(v["scan_hit_rate"], 0.8)

    def test_a_detection_that_is_never_acted_on_fails(self):
        # Detections found but no object interaction committed means the
        # selection step silently broke.
        v = pi.summarize(self._run([
            self._turn(action_kind="choice", subject=None, subject_kept_in_next_scene=None),
        ]))
        self.assertFalse(v["checks"]["scan actions committed as object interactions"])

    def test_a_scan_turn_that_never_changed_the_scene_fails(self):
        v = pi.summarize(self._run([self._turn(scene_changed=False)]))
        self.assertFalse(v["checks"]["scan actions changed the scene"])

    def test_a_vanishing_subject_fails_permanence(self):
        v = pi.summarize(self._run([
            self._turn(subject_kept_in_next_scene=False),
            self._turn(turn=2, subject_kept_in_next_scene=False),
        ]))
        self.assertFalse(v["checks"]["scanned subject persists into next scene"])
        self.assertEqual(v["permanence_rate"], 0.0)

    def test_a_timed_out_turn_fails(self):
        v = pi.summarize(self._run([self._turn(status="timeout")]))
        self.assertFalse(v["checks"]["every turn resolved"])

    def test_selected_objects_are_reported(self):
        v = pi.summarize(self._run([
            self._turn(subject="oil pump"),
            self._turn(turn=2, subject="steel door", action_kind="scan_move"),
        ]))
        self.assertEqual(v["unique_objects_selected"], ["oil pump", "steel door"])

    def test_beats_that_only_use_the_poked_object_fail_the_weave_check(self):
        """The whole point of naming the scene in the consequence prompt is that
        the beat stops being about one thing. A run where every turn mentions
        only the tapped object means the directive never landed."""
        v = pi.summarize(self._run([
            self._turn(onscreen_weave_count=1),
            self._turn(turn=2, onscreen_weave_count=1),
        ]))
        self.assertFalse(v["checks"]["consequence weaves 2+ on-screen objects"])
        self.assertEqual(v["weave_rate"], 0.0)

    def test_a_braided_beat_passes_the_weave_check(self):
        v = pi.summarize(self._run([
            self._turn(onscreen_weave_count=3),
            self._turn(turn=2, onscreen_weave_count=2),
        ]))
        self.assertTrue(v["checks"]["consequence weaves 2+ on-screen objects"])
        self.assertEqual(v["weave_rate"], 1.0)

    def test_a_run_with_nothing_weavable_does_not_fail(self):
        """A scan that named one thing (or nothing) gave the writer no pair to
        braid, so it must not be scored as a miss."""
        v = pi.summarize(self._run([self._turn(onscreen_weave_count=None)]))
        self.assertTrue(v["checks"]["consequence weaves 2+ on-screen objects"])
        self.assertIsNone(v["weave_rate"])


class TestAntiLoopGatedScansAreNotDetectorMisses(unittest.TestCase):
    """engine.api_detect's anti-loop gate deliberately empties the object
    list when a run is stuck or the player is DETECT_HUNTED, so the choice
    pills' forced flee/egress option is the only affordance left — see the
    comment above `anti_loop_gate` in engine.py. On the wire that is an empty
    `objects` list, identical to a genuine detector miss, so a summary that
    can't tell the two apart scores the gate's own correct behavior as a
    regression on every run that gets hunted for a few turns. A live run
    reproduced exactly this: hit_rate 0.79, zero detect_error, and replaying
    the SAME frame straight through the detector (bypassing the gate) found
    5 objects every time — the "misses" were all gated, not blind."""

    def _turn(self, **kw):
        base = {
            "turn": 1, "planned": "scan_move", "action_kind": "scan_move",
            "degraded_to_choice": False, "status": "resolved", "detection_labels": ["oil pump"],
            "subject": "oil pump", "scene_changed": True, "choices_changed": True,
            "subject_kept_in_next_scene": True, "anti_loop_suppressed": False,
        }
        base.update(kw)
        return base

    def _run(self, turns):
        return {"turns": turns, "require_scan": True}

    def _gated_fallback(self, **kw):
        return self._turn(
            detection_labels=[], action_kind="choice", subject=None,
            degraded_to_choice=True, subject_kept_in_next_scene=None,
            anti_loop_suppressed=True, **kw,
        )

    def test_a_gated_fallback_does_not_fail_the_fallback_check(self):
        v = pi.summarize(self._run([self._turn(), self._gated_fallback(turn=2)]))
        self.assertTrue(v["checks"][
            "scan never fell back to a plain button press (excluding anti-loop gates)"
        ])

    def test_an_ungated_fallback_still_fails_it(self):
        blind = self._turn(detection_labels=[], action_kind="choice", subject=None,
                           degraded_to_choice=True, subject_kept_in_next_scene=None)
        v = pi.summarize(self._run([self._turn(), blind]))
        self.assertFalse(v["checks"][
            "scan never fell back to a plain button press (excluding anti-loop gates)"
        ])

    def test_gated_turns_are_excluded_from_hit_rate_not_counted_as_misses(self):
        # Three real hits, one gated turn — hit rate should be 3/3, not 3/4.
        v = pi.summarize(self._run([
            self._turn(), self._turn(turn=2), self._turn(turn=3),
            self._gated_fallback(turn=4),
        ]))
        self.assertEqual(v["scan_hit_rate"], 1.0)
        self.assertTrue(v["checks"]["scan finds objects on nearly every frame"])

    def test_gated_turns_are_counted_and_reported(self):
        v = pi.summarize(self._run([self._turn(), self._gated_fallback(turn=2)]))
        self.assertEqual(v["scan_turns_anti_loop_gated"], 1)


class TestDeathIsNoticed(unittest.TestCase):
    """engine.py's game_over batch ships its own player_choice_prompt (the
    "Restart Simulation" button, from _structure_choices_for_feed) in the
    SAME feed slice. A run that missed this once played sixteen more turns
    against a dead session — SCANning the GAME OVER frame, MOVEing to
    whatever it found, burning API calls on a session that ended — because
    the harness saw the co-occurring prompt and called it "resolved" before
    ever checking for game_over."""

    class _FakeClient:
        def __init__(self, batches):
            self._batches = list(batches)

        def get(self, path):
            return self._batches.pop(0) if self._batches else []

    def test_a_death_batch_is_not_read_as_an_ordinary_resolution(self):
        batch = [
            {"type": "narrative_event", "content": "You succumb."},
            {"type": "game_over", "content": "You have succumbed to the horrors."},
            {"type": "player_choice_prompt", "content": "GAME OVER",
             "choices": [{"text": "Restart Simulation"}]},
        ]
        items, elapsed, status = pi.wait_for_turn(self._FakeClient([batch]), 0)
        self.assertEqual(status, "death")
        self.assertEqual(items, batch)

    def test_an_ordinary_turn_still_resolves(self):
        batch = [
            {"type": "narrative_event", "content": "You keep going."},
            {"type": "player_choice_prompt", "content": "What do you do?",
             "choices": [{"text": "Run"}, {"text": "Hide"}]},
        ]
        _, _, status = pi.wait_for_turn(self._FakeClient([batch]), 0)
        self.assertEqual(status, "resolved")


class TestChoiceOverlay(unittest.TestCase):
    """CHOICES/SELECTED for a plain-choice turn render like the live game's
    own choice buttons — glowing text, no background plate (see .choice-btn
    in static/css/standalone.css) — instead of the opaque debug caption strip
    `_choice_menu`/`_captioned` used to paste over the frame. These pin that
    contract: same canvas as the source, genuinely transparent elsewhere, and
    still tolerant of the "nothing to show" case."""

    def test_keeps_the_same_canvas_as_the_source(self):
        # Both this and `_boxes` feed straight into the GIF/review video
        # unscaled, so neither may grow or shrink the frame.
        img = Image.new("RGB", (200, 120), (10, 10, 10))
        out = pi._choice_overlay(img, ["Run", "Hide"], picked="Run")
        self.assertEqual(out.size, img.size)

    def test_is_additive_not_an_opaque_box(self):
        # A far corner, nowhere near the bottom-center text, must be
        # untouched — proof this is a transparent overlay, not a plate
        # pasted over part of the frame.
        img = Image.new("RGB", (400, 300), (10, 20, 30))
        out = pi._choice_overlay(img, ["Run", "Hide"], picked=None)
        self.assertEqual(out.getpixel((2, 2)), (10, 20, 30))

    def test_handles_no_choices_without_crashing(self):
        img = Image.new("RGB", (200, 120), (0, 0, 0))
        out = pi._choice_overlay(img, [], picked=None)
        self.assertEqual(out.size, img.size)


class TestObjectLabelMatching(unittest.TestCase):
    """How the harness decides an on-screen object made it into the writing."""

    def test_a_label_counts_when_any_significant_word_appears(self):
        hits = pi.labels_present(
            ["blast door", "support beam", "mercenary"],
            "You heave your weight against the heavy blast door as the beam groans.",
        )
        self.assertEqual(hits, ["blast door", "support beam"])

    def test_short_words_alone_never_match(self):
        # A two-letter fragment would match almost any prose.
        self.assertEqual(pi.labels_present(["a b"], "the blast door"), [])

    def test_no_labels_and_no_text_are_both_safe(self):
        self.assertEqual(pi.labels_present([], "anything"), [])
        self.assertEqual(pi.labels_present(["creature"], ""), [])


class TestFlipbookTracks(unittest.TestCase):
    """The videos are the artefact a human actually reviews. Each frame kind
    gets its own track for reading one aspect across a run — and each track
    has to stay in the order the run produced it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.frames = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _write(self, name: str) -> Path:
        p = self.frames / name
        Image.new("RGB", (16, 12), (20, 20, 20)).save(p)
        return p

    @staticmethod
    def _clock(times: dict[str, float]):
        """Stand-in for file creation time. No portable call can set a real one,
        and on Windows os.utime moves neither ctime nor birthtime."""
        return lambda p: times.get(p.name, 0.0)

    def test_each_kind_lands_in_its_own_track(self):
        for turn in (1, 2):
            for kind in ("view", "choices", "selected"):
                self._write(f"turn_{turn:02d}_{kind}.png")
        tracks = pi.collect_frame_tracks(self.frames)
        self.assertEqual(sorted(tracks), ["choices", "selected", "view"])
        for kind, paths in tracks.items():
            self.assertEqual([p.name for p in paths],
                             [f"turn_01_{kind}.png", f"turn_02_{kind}.png"])

    def test_a_missing_choices_frame_does_not_shift_the_other_tracks(self):
        # A frame that never fetched (no picture at all that turn) is
        # legitimately absent. It must not borrow frames from the others.
        self._write("turn_01_view.png")
        self._write("turn_01_choices.png")
        self._write("turn_02_view.png")
        tracks = pi.collect_frame_tracks(self.frames)
        self.assertEqual(len(tracks["view"]), 2)
        self.assertEqual(len(tracks["choices"]), 1)
        self.assertEqual(tracks["selected"], [])

    def test_turn_number_breaks_creation_time_ties(self):
        # Same-second writes are common on fast turns, and turn 10 sorting
        # before turn 2 is what a plain filename sort would give.
        for turn in (2, 10):
            self._write(f"turn_{turn:02d}_view.png")
        clock = self._clock({"turn_02_view.png": 100.0, "turn_10_view.png": 100.0})
        self.assertEqual(
            [p.name for p in pi.collect_frame_tracks(self.frames, clock)["view"]],
            ["turn_02_view.png", "turn_10_view.png"])

    def test_a_frame_written_late_plays_late(self):
        self._write("turn_01_view.png")
        self._write("turn_02_view.png")
        clock = self._clock({"turn_01_view.png": 500.0, "turn_02_view.png": 100.0})
        self.assertEqual(
            [p.name for p in pi.collect_frame_tracks(self.frames, clock)["view"]],
            ["turn_02_view.png", "turn_01_view.png"])

    def test_canvas_is_even_sided_so_h264_can_encode_it(self):
        # Odd dimensions are a hard encoder failure under yuv420p.
        odd = self.frames / "odd.png"
        Image.new("RGB", (641, 361)).save(odd)
        width, height = pi._video_canvas([odd])
        self.assertEqual((width % 2, height % 2), (0, 0))

    def test_canvas_fits_the_largest_frame_in_the_track(self):
        # A provider that switches aspect ratio mid-run would otherwise abort
        # the encode instead of letterboxing.
        small, large = self.frames / "s.png", self.frames / "l.png"
        Image.new("RGB", (320, 240)).save(small)
        Image.new("RGB", (640, 400)).save(large)
        self.assertEqual(pi._video_canvas([small, large]), (640, 400))

    def test_canvas_keeps_1080_and_does_not_upscale(self):
        hd = self.frames / "hd.png"
        tiny = self.frames / "tiny.png"
        Image.new("RGB", (1920, 1080)).save(hd)
        Image.new("RGB", (640, 360)).save(tiny)
        self.assertEqual(pi._video_canvas([hd]), (1920, 1080))
        self.assertEqual(pi._video_canvas([tiny]), (640, 360))

    def test_canvas_caps_wider_than_1080p(self):
        huge = self.frames / "huge.png"
        Image.new("RGB", (2560, 1440)).save(huge)
        self.assertEqual(pi._video_canvas([huge]), (1920, 1080))


class TestH264EncodeArgs(unittest.TestCase):
    def test_watch_encode_is_high_profile_not_a_phone_stream(self):
        args = pi._h264_encode_args(keyint=15)
        joined = " ".join(args)
        self.assertIn("-profile:v high", joined)
        self.assertIn("-tune stillimage", joined)
        self.assertIn("-crf 16", joined)
        self.assertNotIn("baseline", joined)
        self.assertNotIn("veryfast", joined)


class TestFrameWritesAreAtomic(unittest.TestCase):
    """render_jobs.py lists and serves frames from another process while a
    run is still writing them. Image.save()/write_bytes() straight to the
    real filename leaves a window where that reader can open a file that
    exists but isn't fully written yet — a torn read that shows up in the
    browser as a frame that's only partly decoded. Temp-file-then-rename
    closes that window: the real filename only ever appears fully formed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_the_real_file_lands_complete(self):
        path = self.dir / "turn_01_view.png"
        pi._save_frame_atomic(Image.new("RGB", (64, 48), (10, 20, 30)), path)
        self.assertTrue(path.exists())
        self.assertEqual(Image.open(path).size, (64, 48))

    def test_no_leftover_temp_file(self):
        path = self.dir / "turn_01_view.png"
        pi._save_frame_atomic(Image.new("RGB", (16, 16)), path)
        self.assertFalse((self.dir / "turn_01_view.png.tmp").exists())

    def test_bytes_variant_is_also_atomic(self):
        path = self.dir / "turn_01_view.png"
        pi._save_bytes_atomic(b"raw-bytes-not-a-real-png", path)
        self.assertEqual(path.read_bytes(), b"raw-bytes-not-a-real-png")
        self.assertFalse((self.dir / "turn_01_view.png.tmp").exists())

    def test_temp_files_are_invisible_to_the_frame_glob(self):
        # render_jobs.py's _frames()/latest_frame() glob for "turn_*_view.png"
        # — the in-flight ".tmp" name must never match that pattern, or a
        # half-written frame would show up in the count/list before it's done.
        path = self.dir / "turn_02_view.png"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(b"partial")
        try:
            matches = list(self.dir.glob("turn_*_view.png"))
            self.assertEqual(matches, [])
        finally:
            tmp.unlink()


class TestLiveBeatsFeed(unittest.TestCase):
    """render_jobs.py polls live.json from another process while a turn may
    be mid-write, so this is the one write in the whole harness that has to
    be atomic — and it's the only thing standing between a render in
    progress and a bare frame counter to look at."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _read(self):
        return json.loads((self.out_dir / "live.json").read_text(encoding="utf-8"))

    def test_writes_the_fields_a_feed_needs(self):
        turns = [{
            "turn": 1, "action_kind": "scan_move", "choice_text": "Move to the crate.",
            "selected_object": "crate", "subject": "crate",
            "narrative": "You cross the yard toward the crate.",
            "new_choices": ["Open it", "Walk on"], "status": "resolved",
        }]
        pi._write_live_beats(self.out_dir, turns)
        beats = self._read()["beats"]
        self.assertEqual(beats[0]["turn"], 1)
        self.assertEqual(beats[0]["kind"], "scan_move")
        self.assertEqual(beats[0]["choice"], "Move to the crate.")
        self.assertEqual(beats[0]["subject"], "crate")
        self.assertEqual(beats[0]["narrative"], "You cross the yard toward the crate.")
        self.assertEqual(beats[0]["next_choices"], ["Open it", "Walk on"])
        self.assertEqual(beats[0]["status"], "resolved")
        self.assertEqual(beats[0]["phase"], "resolved")

    def test_a_plain_choice_turn_falls_back_to_subject_not_selected_object(self):
        # scan turns carry selected_object; plain choice turns only ever set
        # `subject` (and only that field even exists as None there) — either
        # way the feed needs *something* to show as what got picked.
        turns = [{
            "turn": 1, "action_kind": "choice", "choice_text": "Run for the fence",
            "selected_object": None, "subject": None, "narrative": "",
            "new_choices": [], "status": "resolved",
        }]
        pi._write_live_beats(self.out_dir, turns)
        self.assertIsNone(self._read()["beats"][0]["subject"])

    def test_overwrites_rather_than_appends(self):
        pi._write_live_beats(self.out_dir, [{"turn": 1, "choice_text": "a"}])
        pi._write_live_beats(self.out_dir, [{"turn": 1, "choice_text": "a"},
                                             {"turn": 2, "choice_text": "b"}])
        self.assertEqual(len(self._read()["beats"]), 2)

    def test_no_leftover_temp_file(self):
        pi._write_live_beats(self.out_dir, [{"turn": 1, "choice_text": "a"}])
        self.assertFalse((self.out_dir / ".live.json.tmp").exists())

    def test_a_pending_beat_lands_before_the_turn_resolves(self):
        # This is the "one frame behind" fix: the panel has to see SCAN/TOOK
        # while the image is still drawing, not after the next frame arrives.
        pi._write_live_beats(self.out_dir, [], pending={
            "turn": 3, "action_kind": "scan_move", "choice_text": "Move to the crate.",
            "subject": "crate", "detection_labels": ["crate", "fence"],
            "prev_choices": ["Run", "Hide"], "phase": "chosen", "status": "pending",
        })
        beat = self._read()["beats"][0]
        self.assertEqual(beat["turn"], 3)
        self.assertEqual(beat["phase"], "chosen")
        self.assertEqual(beat["status"], "pending")
        self.assertEqual(beat["detections"], ["crate", "fence"])
        self.assertEqual(beat["choices"], ["Run", "Hide"])

    def test_detector_dicts_never_land_as_subject_or_scan_labels(self):
        # The live panel stringifies whatever we write. A leftover detection
        # dict becomes "[object Object]" in the ticker — so every human-facing
        # field has to already be a label.
        pi._write_live_beats(self.out_dir, [], pending={
            "turn": 2, "action_kind": "scan_move",
            "choice_text": "Move to the barrel.",
            "selected_object": {"label": "barrel", "box": [1, 2, 3, 4]},
            "detections": [{"label": "barrel"}, {"label": "fence"}],
            "prev_choices": [{"text": "Run"}, "Hide"],
            "phase": "chosen", "status": "pending",
        })
        beat = self._read()["beats"][0]
        self.assertEqual(beat["subject"], "barrel")
        self.assertEqual(beat["choice"], "Move to the barrel.")
        self.assertEqual(beat["detections"], ["barrel", "fence"])
        self.assertEqual(beat["choices"], ["Run", "Hide"])
        raw = (self.out_dir / "live.json").read_text(encoding="utf-8")
        self.assertNotIn("[object Object]", raw)
        self.assertNotIn('"box"', raw)

    def test_in_flight_scan_keeps_detection_boxes(self):
        # The overlay draws as soon as SCAN returns, on the VIEW still —
        # labels-only detections would leave the stage dead until the
        # CHOICES PNG lands.
        pi._write_live_beats(self.out_dir, [], pending={
            "turn": 4, "action_kind": "scan_move",
            "detections": [
                {"label": "wooden pallet", "cx": 0.42, "cy": 0.61, "w": 0.18, "h": 0.14},
                {"label": "pickup truck", "cx": 0.71, "cy": 0.48, "w": 0.3, "h": 0.22},
            ],
            "phase": "scanned", "status": "pending",
        })
        beat = self._read()["beats"][0]
        self.assertEqual(beat["phase"], "scanned")
        self.assertEqual(beat["detections"], ["wooden pallet", "pickup truck"])
        self.assertEqual(len(beat["boxes"]), 2)
        self.assertEqual(beat["boxes"][0]["label"], "wooden pallet")
        self.assertAlmostEqual(beat["boxes"][0]["cx"], 0.42)
        self.assertAlmostEqual(beat["boxes"][0]["cy"], 0.61)
        self.assertAlmostEqual(beat["boxes"][0]["w"], 0.18)
        self.assertAlmostEqual(beat["boxes"][0]["h"], 0.14)
        self.assertIsNone(beat["pick"])

    def test_chosen_beat_includes_boxes_and_the_picked_object(self):
        picked = {"label": "wooden pallet", "cx": 0.42, "cy": 0.61, "w": 0.18, "h": 0.14}
        pi._write_live_beats(self.out_dir, [], pending={
            "turn": 4, "action_kind": "scan_move",
            "choice_text": "Move to the wooden pallet.",
            "subject": "wooden pallet",
            "selected_object": picked,
            "detections": [
                picked,
                {"label": "pickup truck", "cx": 0.71, "cy": 0.48, "w": 0.3, "h": 0.22},
            ],
            "phase": "chosen", "status": "pending",
        })
        beat = self._read()["beats"][0]
        self.assertEqual(beat["phase"], "chosen")
        self.assertEqual(beat["choice"], "Move to the wooden pallet.")
        self.assertEqual(beat["subject"], "wooden pallet")
        self.assertEqual(beat["detections"], ["wooden pallet", "pickup truck"])
        self.assertEqual(beat["pick"]["label"], "wooden pallet")
        self.assertAlmostEqual(beat["pick"]["cx"], 0.42)
        self.assertEqual(len(beat["boxes"]), 2)

    def test_resolved_turn_keeps_boxes_from_full_detections(self):
        pi._write_live_beats(self.out_dir, [{
            "turn": 1, "action_kind": "scan_move",
            "choice_text": "Move to the crate.",
            "subject": "crate",
            "detections": [{"label": "crate", "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.2}],
            "detection_labels": ["crate"],
            "status": "resolved",
        }])
        beat = self._read()["beats"][0]
        self.assertEqual(beat["detections"], ["crate"])
        self.assertEqual(beat["boxes"][0]["label"], "crate")
        self.assertAlmostEqual(beat["boxes"][0]["cx"], 0.5)

    def test_a_bad_out_dir_does_not_raise(self):
        # The feed is a nicety layered on top of a run that's actually
        # producing frames/gif/video — it must never be what breaks a render.
        pi._write_live_beats(self.out_dir / "no" / "such" / "dir", [{"turn": 1}])

    def test_live_panel_reads_boxes_for_the_scan_overlay(self):
        # Harness and Watch panel have to agree: labels stay on `detections`,
        # geometry lives on `boxes` so the stage can draw before the next frame.
        self.assertIn("function paintScanOverlay", CLIENT_JS)
        self.assertIn("beat.boxes", CLIENT_JS)
        self.assertIn("beat.pick", CLIENT_JS)
        self.assertIn("WatchOverlay.stateFromBeat", CLIENT_JS)
        self.assertIn("WatchOverlay.paint", CLIENT_JS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
