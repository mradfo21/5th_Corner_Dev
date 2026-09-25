"""THE TAPE — a run is kept, played back and exported (run_tape.py).

Each class pins one thing the feature exists for:

* a run is still there after New Game, which deletes every picture in the
  session's images folder (engine.purge_run_media);
* every panel of a beat is on the tape, not only the last one, with what the
  player did and what the narration said;
* the player and the exported animatic pace the tape by one rule;
* the export is a pack an image-to-video model can take shot by shot: each
  shot's first frame, last frame, and the words between them.

No network, no models. Frames are tiny PNGs written into a temp sessions dir.

    python -m pytest -q test_run_tape.py
"""
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")

from PIL import Image

import run_tape


def _png(path: Path, color=(40, 60, 80), size=(64, 36)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


class _Sessions(unittest.TestCase):
    """A throwaway sessions root with a `default` images folder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tape-test-"))
        p = mock.patch.object(run_tape, "SESSIONS_ROOT", self.tmp)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        d = mock.patch.object(run_tape, "_describe", lambda st=None: {
            "experience": (st or {}).get("experience_id", "somewhere"),
            "experience_name": "The Fifth Corner", "world": "Horizon", "protagonist": "Jason"})
        d.start()
        self.addCleanup(d.stop)
        self.images = self.tmp / "default" / "images"

    def frames(self, stem, n=4):
        return [str(_png(self.images / f"{stem}_f{i:02d}.png", (10 * i, 20, 30))) for i in range(1, n + 1)]


class ARunSurvivesNewGame(_Sessions):

    def test_the_frames_outlive_the_images_purge(self):
        rid = run_tape.begin("default", {"experience_id": "somewhere"})
        run_tape.record("default", "turn", self.frames("t1"), frame_ms=420,
                        action="You head for the culvert", prose="You scramble over the fence.", turn=1)
        # what engine.purge_run_media does at New Game
        for f in self.images.glob("*.png"):
            f.unlink()
        run_tape.begin("default", {})
        tape = run_tape.load("default", rid)
        self.assertEqual(tape["ending"], "abandoned")
        names = tape["shots"][0]["frames"]
        self.assertEqual(len(names), 4)
        for n in names:
            self.assertIsNotNone(run_tape.frame_file("default", rid, n))

    def test_web_urls_resolve_to_the_session_images(self):
        run_tape.begin("default", {})
        self.frames("t1", 1)
        shot = run_tape.record("default", "turn", ["/images/t1_f01.png"], turn=1)
        self.assertEqual(shot["frames"], ["t1_f01.png"])

    def test_an_empty_run_is_not_kept(self):
        rid = run_tape.begin("default", {})
        run_tape.begin("default", {})
        self.assertFalse((self.tmp / "default" / "runs" / rid).exists())

    def test_old_runs_are_pruned_and_the_current_one_never_is(self):
        with mock.patch.object(run_tape, "KEEP_RUNS", 2):
            made = []
            for i in range(4):
                rid = run_tape.begin("default", {})
                run_tape.record("default", "turn", self.frames(f"p{i}", 1), turn=1)
                made.append(rid)
                # ids are per second; keep them distinct without sleeping
                run_tape._write_json(run_tape._current_path("default"), {"run": rid})
            run_tape.prune("default", keep=2)
            kept = {r["id"] for r in run_tape.runs("default")}
            self.assertIn(made[-1], kept)
            self.assertLessEqual(len(kept), 3)

    def test_death_ends_the_tape_and_keeps_it_current(self):
        rid = run_tape.begin("default", {})
        run_tape.record("default", "death", self.frames("d"), turn=4)
        run_tape.end("default", "died", "Killed by the coyote")
        self.assertEqual(run_tape.current_id("default"), rid)
        s = run_tape.runs("default")[0]
        self.assertEqual((s["ending"], s["cause"], s["current"]), ("died", "Killed by the coyote", True))


class EveryBeatIsOnTheTape(_Sessions):

    def test_all_panels_in_order_with_the_words(self):
        run_tape.begin("default", {})
        run_tape.record("default", "turn", self.frames("a"), frame_ms=420,
                        action="You ram the truck into them", prose="The fender folds.", turn=9)
        s = run_tape.load("default", "current")["shots"][0]
        self.assertEqual(s["frames"], [f"a_f{i:02d}.png" for i in range(1, 5)])
        self.assertEqual(s["action"], "You ram the truck into them")
        self.assertEqual(len(s["urls"]), 4)
        self.assertTrue(s["urls"][0].startswith("/api/reel/frame/"))

    def test_the_same_picture_twice_is_one_shot(self):
        run_tape.begin("default", {})
        f = self.frames("same", 1)
        run_tape.record("default", "turn", f, turn=1)
        self.assertIsNone(run_tape.record("default", "opening", f, turn=1))
        self.assertEqual(len(run_tape.load("default", "current")["shots"]), 1)

    def test_a_missing_frame_records_nothing_and_never_raises(self):
        run_tape.begin("default", {})
        self.assertIsNone(run_tape.record("default", "turn", ["/images/nope.png"]))

    def test_summary_counts_turns_fights_and_the_cover(self):
        run_tape.begin("default", {"experience_id": "somewhere"})
        run_tape.record("default", "turn", self.frames("t1", 2), turn=1)
        run_tape.record("default", "encounter", self.frames("e1", 2), turn=2)
        run_tape.record("default", "round", self.frames("r1", 2), turn=2)
        s = run_tape.runs("default", experience="somewhere")[0]
        self.assertEqual((s["turns"], s["fights"], s["frames"]), (2, 1, 6))
        self.assertIn("r1_f02.png", s["cover"])
        self.assertEqual(run_tape.runs("default", experience="another"), [])

    def test_frame_names_cannot_escape_the_run(self):
        rid = run_tape.begin("default", {})
        self.assertIsNone(run_tape.frame_file("default", rid, "../tape.json"))
        self.assertIsNone(run_tape.frame_file("default", "../../etc", "x.png"))


class OnePacingRule(unittest.TestCase):

    def test_a_flipbook_plays_then_holds_to_be_read(self):
        t = run_tape.timing({"kind": "turn", "frames": ["a", "b", "c", "d"], "frame_ms": 420,
                             "action": "You ram the truck into them"})
        self.assertEqual(t[:3], [672, 672, 672])
        self.assertGreaterEqual(sum(t), 2400)

    def test_a_still_holds_and_the_montage_is_slow(self):
        self.assertGreaterEqual(run_tape.timing({"kind": "turn", "frames": ["a"]})[0], 3200)
        self.assertEqual(run_tape.timing({"kind": "montage", "frames": ["a", "b"]}), [3400, 3400])

    def test_a_death_is_slower_than_a_turn(self):
        d = run_tape.timing({"kind": "death", "frames": ["a", "b", "c", "d"]})
        self.assertEqual(d[:3], [1150, 1150, 1150])
        self.assertGreaterEqual(d[-1], 4200)


class TheExportIsAShotPack(_Sessions):

    def _run(self):
        rid = run_tape.begin("default", {})
        run_tape.record("default", "montage", self.frames("m", 1), prose="Horizon, 1993.", turn=0)
        run_tape.record("default", "turn", self.frames("t1"), frame_ms=420,
                        action="You ram the truck into them", prose="The fender folds.",
                        prompt="a truck in a fence", turn=1)
        run_tape.record("default", "death", self.frames("d1"), action="You kick the coyote",
                        caption="The coyote hits you hard. 23 damage. You don't get up.", turn=2)
        return rid

    def test_shots_json_has_first_and_last_frames_and_the_motion_between(self):
        rid = self._run()
        path = run_tape.export_pack("default", rid, self.tmp / "out", animatic=False)
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            root = names[0].split("/")[0]
            shots = json.loads(z.read(f"{root}/shots.json"))
            self.assertIn(f"{root}/captions.srt", names)
            self.assertIn(f"{root}/README.txt", names)
            frames = [n for n in names if "/frames/" in n]
        self.assertEqual(len(frames), 9)
        self.assertEqual(len(shots["shots"]), 3)
        turn = shots["shots"][1]
        self.assertEqual(turn["first_frame"], "frames/0002_s002.png")
        self.assertEqual(turn["last_frame"], "frames/0005_s002.png")
        self.assertIn("You ram the truck into them.", turn["video_prompt"])
        self.assertIn("The fender folds.", turn["video_prompt"])
        self.assertAlmostEqual(shots["duration_s"],
                               sum(s["duration_s"] for s in shots["shots"]), places=2)
        # shot N+1 starts where shot N stopped
        self.assertAlmostEqual(shots["shots"][2]["start_s"],
                               turn["start_s"] + turn["duration_s"], places=2)

    def test_captions_are_timed_to_the_tape(self):
        rid = self._run()
        srt = run_tape.captions_srt(run_tape._read_json(run_tape._tape_path("default", rid), {}))
        self.assertIn("You ram the truck into them", srt)
        self.assertIn("00:00:03,400 --> ", srt)

    @unittest.skipUnless(run_tape._ffmpeg(), "no ffmpeg")
    def test_the_animatic_builds(self):
        rid = self._run()
        out = run_tape.build_animatic("default", rid, self.tmp / "a.mp4")
        self.assertTrue(out and out.stat().st_size > 1000)

    def test_an_export_job_finishes_and_is_not_saved_outside_on_a_server(self):
        rid = self._run()
        job = run_tape.export_job("default", rid, "pack")
        for _ in range(200):
            if job.get("state") != "running":
                break
            import time
            time.sleep(0.05)
            job = run_tape.export_job("default", rid, "pack")
        self.assertEqual(job["state"], "done", job)
        self.assertIsNone(job.get("saved_to"))   # LOCAL_APP is off


class TheEngineSaysItInTheSecondPerson(unittest.TestCase):

    def test_turn_actions(self):
        import engine
        self.assertEqual(engine._tape_action("Head for the Culvert Drainage Trough"),
                         "You head for the Culvert Drainage Trough")
        self.assertEqual(engine._tape_action("[ENCOUNTER] a man"), "")
        self.assertEqual(engine._tape_action("Initialize Simulation"), "")


class TheEndpoints(_Sessions):

    def test_runs_run_and_frame_are_served(self):
        import api
        rid = run_tape.begin("default", {"experience_id": "somewhere"})
        run_tape.record("default", "turn", self.frames("t1", 2), turn=1, action="You look")
        c = api.app.test_client()
        runs = c.get("/api/reel/runs?experience=somewhere").get_json()["runs"]
        self.assertEqual(runs[0]["id"], rid)
        tape = c.get("/api/reel/run/current").get_json()
        self.assertEqual(tape["shots"][0]["action"], "You look")
        r = c.get(tape["shots"][0]["urls"][0])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.get(f"/api/reel/frame/{rid}/..%2Ftape.json").status_code, 404)
        self.assertEqual(c.get("/api/reel/run/20990101-000000-ffff").status_code, 404)


if __name__ == "__main__":
    unittest.main()
