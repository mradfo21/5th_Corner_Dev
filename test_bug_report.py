"""The bug button has to be trustworthy, because it is the only witness.

A capture is taken once, at the moment of a complaint, and the situation it
describes cannot be reached again -- the world is generated fresh every run. So
a capture that grabs the wrong frame, or dies quietly on a missing session, does
not merely lose information: it sends the next hour of work at a bug the player
never saw. These tests pin the properties that make the folder worth reading.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("GEMINI_API_KEY", "")

import bug_report


def _png(path: Path, level: int) -> None:
    """A 32x32 greyscale PNG at `level`, so luma checks have real pixels."""
    from PIL import Image
    Image.new("L", (32, 32), color=level).save(path)


class TestTheFrameItCaptures(unittest.TestCase):
    """The picture must be the one the player was looking at."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.images = Path(self.tmp.name) / "images"
        self.images.mkdir(parents=True)
        self.addCleanup(self.tmp.cleanup)

    def _with_image_dir(self):
        import engine
        original = engine._get_image_dir
        engine._get_image_dir = lambda session_id='default': self.images
        self.addCleanup(lambda: setattr(engine, "_get_image_dir", original))

    def test_the_frame_on_screen_beats_the_newest_frame_on_disk(self):
        """The whole point: a turn in flight has already written a newer file.

        Reporting the newest PNG would describe a frame nobody has seen yet --
        and the double-buffered A/B layers mean the unseen one is on disk more
        often than not. The client tells us which URL was actually painted.
        """
        seen = self.images / "seen.png"
        unseen = self.images / "written_but_not_painted.png"
        _png(seen, 80)
        _png(unseen, 80)
        os.utime(unseen, (9e9, 9e9))  # decisively the newest
        self._with_image_dir()

        path, note = bug_report._frame_on_screen(
            "default", {"sceneUrl": "http://127.0.0.1:5099/images/seen.png?s=default"})

        self.assertEqual(Path(path).name, "seen.png")
        self.assertIn("active scene layer", note)

    def test_it_falls_back_to_the_newest_frame_and_says_so(self):
        """A capture with no usable URL is still worth having -- but it must
        admit which frame it grabbed, or the report reads as authoritative."""
        old, new = self.images / "old.png", self.images / "new.png"
        _png(old, 80)
        _png(new, 80)
        os.utime(old, (1e9, 1e9))
        self._with_image_dir()

        path, note = bug_report._frame_on_screen("default", {})

        self.assertEqual(Path(path).name, "new.png")
        self.assertIn("did not resolve", note)

    def test_a_missing_frame_is_reported_not_raised(self):
        self._with_image_dir()
        path, note = bug_report._frame_on_screen("default", {})
        self.assertIsNone(path)
        self.assertIn("no frame", note)


class TestTheFrameTheClientSends(unittest.TestCase):
    """The reliable path: the client hands over the pixels it is displaying.

    Resolving the scene URL server-side has to guess, because a scene can be a
    generated frame, an authored world plate or a moment portrait, each served by
    a different route. The first version guessed and captured a frame the player
    had never seen -- which is worse than no frame, because it looks authoritative.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name) / "frame.png"

    def _data_url(self, level: int) -> str:
        import base64
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("L", (48, 48), color=level).save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    def test_it_writes_the_bytes_it_was_given(self):
        note = bug_report._frame_from_client(self._data_url(90), self.dest)
        self.assertIsNotNone(note)
        self.assertIn("exact pixels", note)
        self.assertTrue(self.dest.exists())
        self.assertEqual(bug_report._luma(self.dest)["mean"], 90.0)

    def test_a_url_the_client_could_not_fetch_defers_to_the_fallback(self):
        for bad in ("", None, "http://127.0.0.1/images/x.png", "data:image/png,notbase64"):
            self.assertIsNone(bug_report._frame_from_client(bad, self.dest),
                              f"{bad!r} should not produce a frame")

    def test_a_truncated_payload_is_rejected_rather_than_written(self):
        """A 40-byte 'PNG' would sail through PIL as a broken file and then get
        reported as a black frame -- exactly the wrong conclusion."""
        self.assertIsNone(
            bug_report._frame_from_client("data:image/png;base64,QQ==", self.dest))
        self.assertFalse(self.dest.exists())


class TestTheLogStaysReadable(unittest.TestCase):
    def test_routine_request_logging_is_filtered_out_of_the_summary(self):
        """The client polls /api/feed every 800ms. Left in, the access log buries
        the two lines that say what actually failed."""
        lines = [
            '08:31:04 127.0.0.1 - - [16/Sep] "GET /api/feed?since_id=1 HTTP/1.1" 200 -',
            '08:31:05 [ENCOUNTER] flipbook plate: no grid came back - staying a still',
            '08:31:06 127.0.0.1 - - [16/Sep] "POST /api/choose HTTP/1.1" 200 -',
            '08:31:07 ERROR: [ENCOUNTER] resolve plate missing the other body',
        ]
        kept = bug_report._interesting_log(lines)
        self.assertEqual(len(kept), 2, kept)
        self.assertTrue(all("no grid" in k or "missing the other body" in k for k in kept))


class TestTheVerdicts(unittest.TestCase):
    """"It went black" has two completely different causes; say which."""

    def test_a_black_file_blames_the_generation(self):
        verdicts = bug_report._verdicts({}, {"mean": 2.0, "dark_fraction": 1.0})
        self.assertTrue(any("black" in v and "generation failed" in v for v in verdicts),
                        verdicts)

    def test_a_healthy_file_points_at_the_paint_instead(self):
        verdicts = bug_report._verdicts({}, {"mean": 55.0, "dark_fraction": 0.02})
        joined = " ".join(verdicts)
        self.assertIn("frame on disk looks fine", joined)
        self.assertIn("painted over it", joined)

    def test_a_covering_layer_is_named(self):
        """The failure a screenshot cannot show, so the report has to say it."""
        verdicts = bug_report._verdicts(
            {"layers": [{"sel": "#reactor-video", "covering": True, "op": "1", "z": "30"}]},
            {"mean": 55.0, "dark_fraction": 0.0})
        self.assertTrue(any("#reactor-video" in v and "covering" in v for v in verdicts),
                        verdicts)

    def test_client_errors_are_surfaced_with_the_first_message(self):
        verdicts = bug_report._verdicts(
            {"errors": [{"message": "Cannot read properties of null (reading 'style')"}]}, None)
        self.assertTrue(any("Cannot read properties of null" in v for v in verdicts), verdicts)

    def test_a_turn_in_flight_is_called_out_so_it_is_not_misread_as_stuck(self):
        verdicts = bug_report._verdicts({"turnActive": True}, None)
        self.assertTrue(any("in flight" in v for v in verdicts), verdicts)

    def test_no_client_payload_is_admitted_rather_than_glossed(self):
        verdicts = bug_report._verdicts({}, None)
        self.assertTrue(any("No client payload" in v for v in verdicts), verdicts)


class TestTheLogTap(unittest.TestCase):
    """The server log has to travel with the capture.

    Where stderr lands depends on how the app was started, so the capture
    cannot go hunting for a file. The tail lives in the process.
    """

    def test_it_keeps_what_was_printed_and_still_prints_it(self):
        import io
        original = sys.stderr
        sink = io.StringIO()
        sys.stderr = bug_report._Tee(sink)
        try:
            print("[ENCOUNTER] flipbook plate: no grid came back", file=sys.stderr)
        finally:
            sys.stderr = original

        self.assertIn("no grid came back", sink.getvalue(),
                      "the tee must not swallow the line it is copying")
        self.assertTrue(any("no grid came back" in line for line in bug_report.log_tail()),
                        "the line should also be retained for the next capture")

    def test_the_tail_is_bounded(self):
        self.assertEqual(bug_report._LOG_TAIL.maxlen, bug_report.LOG_TAIL_LINES)


class TestCaptureWritesAUsableFolder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bugs = Path(self.tmp.name) / "bugs"
        original = bug_report.BUGS_DIR
        bug_report.BUGS_DIR = self.bugs
        self.addCleanup(lambda: setattr(bug_report, "BUGS_DIR", original))

        self.images = Path(self.tmp.name) / "images"
        self.images.mkdir(parents=True)
        _png(self.images / "frame_on_screen.png", 3)   # black on disk
        import engine
        original_dir = engine._get_image_dir
        engine._get_image_dir = lambda session_id='default': self.images
        self.addCleanup(lambda: setattr(engine, "_get_image_dir", original_dir))

    def test_it_writes_the_report_the_frame_and_the_state(self):
        result = bug_report.capture(
            "default",
            screen={"sceneUrl": "/images/frame_on_screen.png",
                    "cls": "mode-play moment-active moment-encounter",
                    "prose": "The figure steps out of the dark.",
                    "choices": ["Smash the camera into them.", "Drop the camera and run."],
                    "layers": [{"sel": "#reactor-video", "covering": True, "op": "1", "z": "30"}],
                    "errors": []},
            note="the plate went black and the nameplate stayed on ...")

        self.assertTrue(result["ok"], result)
        folder = Path(result["path"])
        for name in ("REPORT.md", "frame.png", "meta.json", "screen.json"):
            self.assertTrue((folder / name).exists(), f"{name} missing from {result['files']}")

        report = (folder / "REPORT.md").read_text(encoding="utf-8")
        self.assertIn("the plate went black", report, "the player's note leads the report")
        self.assertIn("Smash the camera into them.", report, "the offer on screen is recorded")
        self.assertIn("#reactor-video", report, "the covering layer is named")
        # The frame was black on disk, so the report must blame the generation
        # rather than send anyone looking at the compositor.
        self.assertTrue(any("generation failed" in v for v in result["verdicts"]),
                        result["verdicts"])

    def test_two_captures_in_the_same_second_do_not_collide(self):
        first = bug_report.capture("default", screen={}, note="one")
        second = bug_report.capture("default", screen={}, note="two")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(json.loads((Path(second["path"]) / "meta.json")
                                    .read_text(encoding="utf-8"))["note"], "two")

    def test_a_broken_session_still_produces_a_folder(self):
        """A capture must never fail because the thing it documents is broken.

        The states most worth reporting are the ones where something is already
        wrong, so every collection step is optional and the report says what it
        could not get.
        """
        import engine
        original = engine.get_state
        engine.get_state = lambda session_id='default': (_ for _ in ()).throw(RuntimeError("state is gone"))
        self.addCleanup(lambda: setattr(engine, "get_state", original))

        result = bug_report.capture("default", screen={}, note="everything is on fire")

        self.assertTrue(result["ok"], result)
        self.assertTrue((Path(result["path"]) / "REPORT.md").exists())
        self.assertTrue(any("state is gone" in p for p in result["problems"]), result["problems"])

    def test_a_hostile_session_id_cannot_escape_the_bugs_directory(self):
        result = bug_report.capture("../../etc", screen={}, note="")
        self.assertTrue(result["ok"], result)
        self.assertEqual(Path(result["path"]).parent.resolve(), self.bugs.resolve())


class TestRecent(unittest.TestCase):
    def test_it_lists_newest_first_and_survives_a_missing_directory(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        original = bug_report.BUGS_DIR
        bug_report.BUGS_DIR = Path(tmp.name) / "never_created"
        self.addCleanup(lambda: setattr(bug_report, "BUGS_DIR", original))
        self.assertEqual(bug_report.recent(), [])


if __name__ == "__main__":
    unittest.main()
