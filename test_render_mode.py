#!/usr/bin/env python3
"""Render mode: picking a model has to actually change what gets drawn.

The feature this covers exists because the old answer was "no". `image_model`
sat in ai_config.json, presets set it, /api/status reported it, the editor had a
picker for it — and gemini_image_utils overwrote it with a constant on every
call. Every playtest ever run used the fast model at 1K no matter what the
config said, and nothing failed, because nothing checked.

So the first group here is the honest question: does the wire request use the
configured model? The rest covers the job around it — a render must own the
renderer while it runs and hand it back afterwards, refuse nonsense before
spending money on it, and never serve a file from outside its own directory.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")

import ai_provider_manager
import gemini_image_utils
import render_jobs

ROOT = Path(__file__).resolve().parent


class ConfigSandbox(unittest.TestCase):
    """Each test gets its own ai_config.json so nothing edits the real one."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "ai_config.json"
        shutil.copy(ROOT / "ai_config.json", self.cfg)
        self._orig_path = ai_provider_manager.AI_CONFIG_PATH
        ai_provider_manager.AI_CONFIG_PATH = self.cfg
        ai_provider_manager._cached_config = None
        ai_provider_manager._cache_timestamp = 0
        self.addCleanup(self._restore)

    def _restore(self):
        ai_provider_manager.AI_CONFIG_PATH = self._orig_path
        ai_provider_manager._cached_config = None
        ai_provider_manager._cache_timestamp = 0
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, **fields):
        data = json.loads(self.cfg.read_text(encoding="utf-8"))
        data.update(fields)
        self.cfg.write_text(json.dumps(data), encoding="utf-8")
        ai_provider_manager._cached_config = None
        ai_provider_manager._cache_timestamp = 0


class TestTheConfiguredModelReachesTheWire(ConfigSandbox):
    """The bug this feature is built on: config said one thing, the request
    said another, and the gap was invisible from every surface that reported it."""

    def test_the_configured_model_is_the_one_used(self):
        self.write(image_provider="gemini", image_model="gemini-3-pro-image")
        self.assertEqual(gemini_image_utils.resolve_model(), "gemini-3-pro-image")

    def test_the_fast_model_is_still_selectable(self):
        self.write(image_provider="gemini", image_model="gemini-3.1-flash-lite-image")
        self.assertEqual(gemini_image_utils.resolve_model(),
                         gemini_image_utils.GEMINI_FLASH_IMAGE)

    def test_a_non_gemini_provider_does_not_send_its_model_to_gemini(self):
        # Krea frames fall back to Gemini when Krea fails. That fallback must
        # not ask Gemini for "krea-2/large".
        self.write(image_provider="krea", image_model="krea-2/large")
        self.assertEqual(gemini_image_utils.resolve_model(),
                         gemini_image_utils.GEMINI_FLASH_IMAGE)

    def test_an_unknown_model_falls_back_rather_than_404ing(self):
        self.write(image_provider="gemini", image_model="gemini-9-imaginary")
        self.assertEqual(gemini_image_utils.resolve_model(),
                         gemini_image_utils.GEMINI_FLASH_IMAGE)


class TestResolution(ConfigSandbox):
    """1K was hardcoded next to the model, with the same consequence."""

    def test_the_configured_size_is_used(self):
        self.write(image_provider="gemini", image_model="gemini-3-pro-image",
                   image_size="4K")
        self.assertEqual(gemini_image_utils.resolve_image_size(), "4K")

    def test_a_size_the_model_cannot_do_is_clamped(self):
        # Lite tops out at 2K. Asking for 4K must land on something real rather
        # than being passed through to fail inside the provider call.
        self.write(image_provider="gemini",
                   image_model="gemini-3.1-flash-lite-image", image_size="4K")
        self.assertEqual(gemini_image_utils.resolve_image_size(), "2K")

    def test_garbage_reads_as_1k(self):
        self.write(image_size="enormous")
        self.assertEqual(ai_provider_manager.get_image_size(), "1K")


class TestAspectRatio(ConfigSandbox):
    """Watch picks a frame shape the same way it picks a size: config, then wire."""

    def test_the_configured_ratio_is_used(self):
        self.write(aspect_ratio="16:9")
        self.assertEqual(gemini_image_utils.resolve_aspect_ratio(), "16:9")

    def test_phone_horizontal_maps_to_21_9(self):
        self.assertEqual(ai_provider_manager.normalize_aspect_ratio("phone_horizontal"),
                         "21:9")
        self.assertEqual(ai_provider_manager.normalize_aspect_ratio("phone-horizontal"),
                         "21:9")
        self.write(aspect_ratio="phone_horizontal")
        self.assertEqual(ai_provider_manager.get_image_aspect_ratio(), "21:9")
        self.assertEqual(gemini_image_utils.resolve_aspect_ratio(), "21:9")

    def test_unset_live_play_stays_4_3(self):
        self.write(aspect_ratio="")
        self.assertEqual(ai_provider_manager.get_image_aspect_ratio(), "4:3")

    def test_garbage_reads_as_4_3(self):
        self.write(aspect_ratio="cinemascope-plus")
        self.assertEqual(ai_provider_manager.get_image_aspect_ratio(), "4:3")


class TestCatalogue(ConfigSandbox):
    """Adding a model should be a config edit, not a code change."""

    def test_nano_banana_pro_is_offered(self):
        entry = ai_provider_manager.find_model("image", "gemini-3-pro-image")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["provider"], "gemini")
        self.assertIn("4K", entry["sizes"])

    def test_every_image_entry_names_its_provider(self):
        for entry in ai_provider_manager.model_catalogue("image"):
            self.assertTrue(entry.get("provider"), entry)

    def test_choosing_a_model_sets_its_provider_too(self):
        # Otherwise you can point the image provider at Gemini and the model at
        # Krea, and the run silently draws with neither of the things you chose.
        ai_provider_manager.apply_models(image_model="krea-2/medium")
        self.assertEqual(ai_provider_manager.get_image_provider(), "krea")

    def test_an_unknown_model_is_refused_not_written(self):
        before = ai_provider_manager.get_image_model()
        with self.assertRaises(ValueError):
            ai_provider_manager.apply_models(image_model="not-a-model")
        self.assertEqual(ai_provider_manager.get_image_model(), before)


class TestOnlySupportedModelsAreOffered(ConfigSandbox):
    """A picker option nobody's key can actually run isn't a choice, it's a
    trap — the render either fails outright (OpenAI with no key returns no
    image) or quietly draws with a different model than the one shown
    (fal falls back to Gemini). Missing a key hides the entry instead."""

    def _keyless(self):
        return mock.patch.dict(os.environ, {
            "KREA_API_KEY": "", "FAL_API_KEY": "",
            "OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": "",
        })

    def test_image_catalogue_is_exactly_the_three_working_generators(self):
        ids = [e["id"] for e in ai_provider_manager.model_catalogue("image")]
        self.assertEqual(ids, [
            "gemini-3-pro-image",
            "gemini-3.1-flash-lite-image",
            "krea-2/medium",
        ])
        labels = {e["id"]: e["label"] for e in ai_provider_manager.model_catalogue("image")}
        self.assertEqual(labels["gemini-3-pro-image"], "Gemini Pro")
        self.assertEqual(labels["gemini-3.1-flash-lite-image"], "Gemini Fast")
        self.assertEqual(labels["krea-2/medium"], "Krea")

    def test_a_provider_missing_its_key_is_left_out(self):
        with self._keyless():
            offered = {e["id"] for e in ai_provider_manager.available_model_catalogue("image")}
        self.assertNotIn("krea-2/medium", offered)
        self.assertNotIn("fal-ai/fast-lightning-sdxl", offered)
        self.assertNotIn("gpt-image-1", offered)
        self.assertNotIn("gemini-3.1-flash-image", offered)
        self.assertNotIn("krea-2/large", offered)
        self.assertNotIn("veo-3.1-generate-preview", offered)

    def test_gemini_never_needs_a_key_of_its_own_to_be_offered(self):
        # The base game already requires GEMINI_API_KEY to do anything at
        # all, so gating Gemini entries on it here would just hide every
        # model instead of the handful actually missing a key.
        with self._keyless():
            offered = {e["id"] for e in ai_provider_manager.available_model_catalogue("image")}
        self.assertIn("gemini-3-pro-image", offered)

    def test_a_provider_with_its_key_set_is_offered(self):
        with mock.patch.dict(os.environ, {"KREA_API_KEY": "sk-test"}):
            offered = {e["id"] for e in ai_provider_manager.available_model_catalogue("image")}
        self.assertIn("krea-2/medium", offered)

    def test_mock_mode_offers_everything_regardless_of_keys(self):
        # Nothing here calls a real API, so filtering by key would just hide
        # models you might specifically be trying to test the picker with.
        ai_provider_manager.set_backend_override("mock")
        self.addCleanup(ai_provider_manager.set_backend_override, None)
        with self._keyless():
            offered = {e["id"] for e in ai_provider_manager.available_model_catalogue("image")}
        self.assertIn("krea-2/medium", offered)
        self.assertIn("gemini-3-pro-image", offered)
        self.assertIn("gemini-3.1-flash-lite-image", offered)
        self.assertNotIn("gpt-image-1", offered)

    def test_model_catalogue_itself_stays_unfiltered(self):
        # find_model/apply_models go through model_catalogue(), not the
        # available_ variant — a model already selected in ai_config.json
        # can't be refused as "unknown" just because its key got unset later.
        with self._keyless():
            known = {e["id"] for e in ai_provider_manager.model_catalogue("image")}
        self.assertIn("krea-2/medium", known)

    def test_the_render_panel_only_offers_what_available_model_catalogue_does(self):
        with self._keyless():
            offered = {e["id"] for e in render_jobs.options()["image_models"]}
        self.assertNotIn("gpt-image-1", offered)
        self.assertIn("gemini-3-pro-image", offered)

    def test_the_in_game_picker_only_lists_the_three_working_generators(self):
        import api
        with mock.patch.dict(os.environ, {"KREA_API_KEY": "sk-test"}):
            payload = api._ai_config_payload()
        names = [p["name"] for p in payload["presets"]]
        labels = [p["label"] for p in payload["presets"]]
        self.assertEqual(names, ["gemini_pro", "gemini", "krea"])
        self.assertEqual(labels, ["Gemini Pro", "Gemini Fast", "Krea"])
        self.assertNotIn("anthropic", names)
        self.assertNotIn("fal", names)
        self.assertNotIn("openai", names)
        self.assertNotIn("veo", names)

    def test_the_in_game_picker_hides_krea_without_its_key(self):
        import api
        with self._keyless():
            payload = api._ai_config_payload()
        names = [p["name"] for p in payload["presets"]]
        self.assertEqual(names, ["gemini_pro", "gemini"])
        self.assertNotIn("krea", names)


class TestTextModelsAreIndependent(ConfigSandbox):
    """Claude is a narrator, not an image preset. Switching stills must
    leave it on, and the editor payload has to list it."""

    def _keyless(self):
        return mock.patch.dict(os.environ, {
            "KREA_API_KEY": "", "FAL_API_KEY": "",
            "OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": "",
        })

    def test_apply_models_points_text_at_claude(self):
        before = ai_provider_manager.get_image_model()
        settings = ai_provider_manager.apply_models(text_model="claude-sonnet-4-5")
        self.assertEqual(settings["text_provider"], "anthropic")
        self.assertEqual(settings["text_model"], "claude-sonnet-4-5")
        self.assertEqual(ai_provider_manager.get_image_model(), before)

    def test_image_preset_switch_keeps_claude(self):
        import api
        ai_provider_manager.apply_models(text_model="claude-opus-4-5")
        result, status = api._ai_switch_result("gemini_pro")
        self.assertEqual(status, 200)
        self.assertEqual(result["text_model"], "claude-opus-4-5")
        self.assertEqual(result["text_provider"], "anthropic")
        self.assertEqual(result["image_model"], "gemini-3-pro-image")

    def test_config_payload_lists_claude(self):
        import api
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            payload = api._ai_config_payload()
        ids = [m["id"] for m in payload["text_models"]]
        self.assertIn("claude-sonnet-4-5", ids)
        self.assertIn("claude-opus-4-5", ids)
        self.assertIn("gpt-4o-mini", ids)
        claude = next(m for m in payload["text_models"] if m["id"] == "claude-sonnet-4-5")
        self.assertTrue(claude["available"])

    def test_claude_is_listed_but_locked_without_a_key(self):
        import api
        with self._keyless(), mock.patch.object(
                ai_provider_manager, "is_mock_active", return_value=False):
            payload = api._ai_config_payload()
        claude = next(m for m in payload["text_models"] if m["id"] == "claude-sonnet-4-5")
        self.assertFalse(claude["available"])


class TestRenderRequestValidation(ConfigSandbox):
    """A render is slow and costs real money. Reject the bad ones up front."""

    def test_turn_count_is_bounded(self):
        # The ceiling is a sanity bound, not a real cap: long-form playtests are
        # the point of the studio, so a big-but-reasonable count is allowed and
        # only the absurd (<=0 or into the thousands) is refused.
        for turns in (0, -3, 5000):
            with self.assertRaises(ValueError):
                render_jobs.validate({"turns": turns})
        # A genuinely long run is fine now.
        self.assertEqual(render_jobs.validate({"turns": 200})["turns"], 200)

    def test_turns_must_be_a_number(self):
        with self.assertRaises(ValueError):
            render_jobs.validate({"turns": "loads"})

    def test_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            render_jobs.validate({"turns": 4, "mode": "freestyle"})

    def test_unknown_models_are_refused(self):
        with self.assertRaises(ValueError):
            render_jobs.validate({"turns": 4, "image_model": "midjourney"})
        with self.assertRaises(ValueError):
            render_jobs.validate({"turns": 4, "text_model": "eliza"})

    def test_move_driven_scanning_is_the_default(self):
        self.assertEqual(render_jobs.validate({"turns": 4})["mode"], "scan_move")

    def test_phone_horizontal_is_accepted_as_21_9(self):
        spec = render_jobs.validate({"turns": 4, "aspect_ratio": "phone_horizontal"})
        self.assertEqual(spec["aspect_ratio"], "21:9")
        spec = render_jobs.validate({"turns": 4, "aspect_ratio": "21:9"})
        self.assertEqual(spec["aspect_ratio"], "21:9")

    def test_unknown_aspect_ratio_is_refused(self):
        with self.assertRaises(ValueError):
            render_jobs.validate({"turns": 4, "aspect_ratio": "12:5"})

    def test_heavy_settings_buy_more_patience(self):
        # A Pro frame at 4K takes minutes. Keeping the CLI's 180s default would
        # abandon a healthy render and call it a timeout.
        light = render_jobs.validate({"turns": 4})
        heavy = render_jobs.validate({
            "turns": 4, "image_model": "gemini-3-pro-image", "image_size": "4K"})
        self.assertGreater(heavy["turn_timeout"], light["turn_timeout"])
        self.assertGreater(heavy["image_grace"], light["image_grace"])


class TestRenderOwnsTheRendererAndGivesItBack(ConfigSandbox):
    """Model choice is global. A render that leaves Pro switched on turns every
    subsequent turn of live play into a minute-long wait nobody asked for."""

    def setUp(self):
        super().setUp()
        render_jobs._job = None
        self.addCleanup(setattr, render_jobs, "_job", None)
        # A few tests below let a job touch its own out_dir on disk (the
        # cancel/stop lifecycle). Without this they'd write into the real
        # playtest_results/renders/ instead of the sandbox.
        self._orig_root = render_jobs.RENDER_ROOT
        render_jobs.RENDER_ROOT = self.tmp / "renders"
        self.addCleanup(setattr, render_jobs, "RENDER_ROOT", self._orig_root)

    def test_the_job_applies_its_models_then_restores_them(self):
        ai_provider_manager.apply_models(
            image_model="gemini-3.1-flash-lite-image", image_size="1K",
            aspect_ratio="4:3")
        with mock.patch.object(render_jobs.RenderJob, "start", lambda self: None):
            job = render_jobs.RenderJob(
                render_jobs.validate({"turns": 2, "image_model": "gemini-3-pro-image",
                                      "image_size": "4K", "aspect_ratio": "21:9"}),
                "http://127.0.0.1:5001")
        self.assertEqual(ai_provider_manager.get_image_model(), "gemini-3-pro-image")
        self.assertEqual(ai_provider_manager.get_image_size(), "4K")
        self.assertEqual(ai_provider_manager.get_image_aspect_ratio(), "21:9")

        job._proc = mock.Mock(wait=mock.Mock(return_value=0))
        job._finish()
        self.assertEqual(ai_provider_manager.get_image_model(),
                         "gemini-3.1-flash-lite-image")
        self.assertEqual(ai_provider_manager.get_image_size(), "1K")
        self.assertEqual(ai_provider_manager.get_image_aspect_ratio(), "4:3")

    def test_beats_are_read_fresh_from_live_json_while_running(self):
        # playtest_interactive.py rewrites live.json after every turn; the
        # panel should see whatever's there right now, not a stale copy.
        with mock.patch.object(render_jobs.RenderJob, "start", lambda self: None):
            job = render_jobs.RenderJob(render_jobs.validate({"turns": 4}),
                                         "http://127.0.0.1:5001")
        job.out_dir.mkdir(parents=True, exist_ok=True)
        self.assertEqual(job.beats_so_far(), [])
        (job.out_dir / "live.json").write_text(json.dumps({"beats": [
            {"turn": 1, "kind": "scan_move", "choice": "Move to the crate",
             "subject": "crate", "narrative": "You cross the yard.",
             "next_choices": ["Open it", "Walk on"], "status": "resolved"},
        ]}), encoding="utf-8")
        beats = job.beats_so_far()
        self.assertEqual(len(beats), 1)
        self.assertEqual(beats[0]["turn"], 1)
        self.assertEqual(beats[0]["narrative"], "You cross the yard.")
        self.assertIn("beats", job.to_dict())

    def test_missing_or_broken_live_json_is_just_an_empty_feed(self):
        with mock.patch.object(render_jobs.RenderJob, "start", lambda self: None):
            job = render_jobs.RenderJob(render_jobs.validate({"turns": 4}),
                                         "http://127.0.0.1:5001")
        job.out_dir.mkdir(parents=True, exist_ok=True)
        self.assertEqual(job.beats_so_far(), [])
        (job.out_dir / "live.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(job.beats_so_far(), [])

    def test_a_failed_render_still_hands_the_renderer_back(self):
        ai_provider_manager.apply_models(image_model="gemini-3.1-flash-lite-image")
        with mock.patch.object(render_jobs.RenderJob, "start", lambda self: None):
            job = render_jobs.RenderJob(
                render_jobs.validate({"turns": 2, "image_model": "gemini-3-pro-image"}),
                "http://127.0.0.1:5001")
        job._proc = mock.Mock(wait=mock.Mock(return_value=1))
        job._finish()
        self.assertEqual(job.state, "failed")
        self.assertEqual(ai_provider_manager.get_image_model(),
                         "gemini-3.1-flash-lite-image")

    def test_only_one_render_at_a_time(self):
        with mock.patch.object(render_jobs.RenderJob, "start", lambda self: None):
            render_jobs.start({"turns": 2}, "http://127.0.0.1:5001")
            render_jobs._job.state = "running"
            with self.assertRaises(RuntimeError):
                render_jobs.start({"turns": 2}, "http://127.0.0.1:5001")

    def test_progress_is_read_from_the_harness_output(self):
        self.assertIsNotNone(render_jobs._TURN_RE.match("  turn 7/12 [scan_move] ..."))
        self.assertEqual(
            render_jobs._TURN_RE.match("  turn 7/12 [scan_move] ...").group(1), "7")

    def test_nothing_to_cancel_is_an_error_not_a_crash(self):
        render_jobs._job = None
        with self.assertRaises(RuntimeError):
            render_jobs.cancel()

    def test_cancel_asks_nicely_instead_of_killing_on_the_spot(self):
        # A hard kill happens before the harness reaches its own finalize
        # step, which is the whole "cancel never makes a video" bug. The
        # graceful path touches a sentinel and leaves the process alone.
        with mock.patch.object(render_jobs.RenderJob, "start", lambda self: None):
            job = render_jobs.RenderJob(render_jobs.validate({"turns": 4}),
                                         "http://127.0.0.1:5001")
        job.out_dir.mkdir(parents=True, exist_ok=True)
        proc = mock.Mock()
        proc.poll.return_value = None
        job._proc = proc
        job.cancel()
        self.assertEqual(job.state, "stopping")
        self.assertTrue((job.out_dir / ".stop").exists())
        proc.terminate.assert_not_called()

    def test_a_cooperative_stop_with_turns_done_finishes_like_a_short_done_run(self):
        # The harness noticed the stop file, finished its own finalize step
        # (a real session.json), then exited 0 — that has to read as a good,
        # playable outcome rather than "cancelled".
        with mock.patch.object(render_jobs.RenderJob, "start", lambda self: None):
            job = render_jobs.RenderJob(render_jobs.validate({"turns": 40}),
                                         "http://127.0.0.1:5001")
        job.out_dir.mkdir(parents=True, exist_ok=True)
        (job.out_dir / "session.json").write_text(
            json.dumps({"turns": [{"turn": 1}], "verdict": {"passed": True}}),
            encoding="utf-8")
        job._cancelled = True
        job._proc = mock.Mock(wait=mock.Mock(return_value=0))
        job._finish()
        self.assertEqual(job.state, "stopped")
        self.assertTrue((job.out_dir / "render.json").exists())

    def test_a_stop_before_any_turn_landed_leaves_nothing_behind(self):
        # Force-killed at app shutdown, or stopped in the first second —
        # either way there is no session, so the empty folder is discarded
        # rather than kept around as a dead entry to browse to.
        with mock.patch.object(render_jobs.RenderJob, "start", lambda self: None):
            job = render_jobs.RenderJob(render_jobs.validate({"turns": 40}),
                                         "http://127.0.0.1:5001")
        job.out_dir.mkdir(parents=True, exist_ok=True)
        job._cancelled = True
        job._proc = mock.Mock(wait=mock.Mock(return_value=-15))
        job._finish()
        self.assertEqual(job.state, "cancelled")
        self.assertFalse(job.out_dir.exists())

    def test_force_cancel_kills_immediately_for_app_shutdown(self):
        with mock.patch.object(render_jobs.RenderJob, "start", lambda self: None):
            job = render_jobs.RenderJob(render_jobs.validate({"turns": 4}),
                                         "http://127.0.0.1:5001")
        job.out_dir.mkdir(parents=True, exist_ok=True)
        proc = mock.Mock()
        proc.poll.return_value = None
        job._proc = proc
        job.cancel(force=True)
        proc.terminate.assert_called_once()


class TestArtifactServing(unittest.TestCase):
    """Reported paths are server-generated, but they come back over HTTP."""

    def test_a_path_outside_the_render_root_is_refused(self):
        for bad in ("../../ai_config.json", "../../../etc/passwd"):
            with self.assertRaises(ValueError):
                render_jobs.artifact_path(bad)

    def test_a_missing_file_is_a_miss_not_an_escape(self):
        with self.assertRaises(FileNotFoundError):
            render_jobs.artifact_path("render_nope/frames/turn_01_view.png")


class RenderRootSandbox(unittest.TestCase):
    """A fake render folder, so review can be tested without spending money."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_root, self._orig_bundles = render_jobs.RENDER_ROOT, render_jobs.BUNDLE_DIR
        render_jobs.RENDER_ROOT = self.tmp
        render_jobs.BUNDLE_DIR = self.tmp / "_bundles"
        self.addCleanup(self._restore)
        self.run = self.tmp / "render_20260101_010101"
        (self.run / "frames").mkdir(parents=True)
        for n in (1, 2):
            for kind in ("view", "choices", "selected"):
                (self.run / "frames" / f"turn_{n:02d}_{kind}.png").write_bytes(b"png")
        (self.run / "frames" / "turn_03_view.png").write_bytes(b"png")
        (self.run / "session.json").write_text(json.dumps({
            "started_at": "2026-01-01T01:01:01+00:00",
            "finished_at": "2026-01-01T01:11:01+00:00",
            "mode": "scan_move",
            "turns_requested": 2,
            "verdict": {"passed": True, "checks": {"every turn resolved": True}},
            "reset": {"narratives": ["The desert exhales."]},
            "final_view_frame": "frames\\turn_03_view.png",
            "video_pause_s": 0.5,
            "gif_ms": 1100,
            "turns": [{
                "turn": n,
                "choice_text": f"Enter the shed {n}",
                "subject": "shed",
                "action_kind": "scan_move",
                "narrative": f"Narrative {n}.",
                "prev_choices": ["Wait", "Look around"],
                "detections": [
                    {"label": "shed", "cx": 0.42, "cy": 0.55, "w": 0.2, "h": 0.18},
                    {"label": "fence", "cx": 0.78, "cy": 0.40, "w": 0.12, "h": 0.3},
                ],
                "selected_object": {
                    "label": "shed", "cx": 0.42, "cy": 0.55, "w": 0.2, "h": 0.18,
                },
                # The harness writes OS-native paths; on Windows these have
                # backslashes, which are not path separators in a URL.
                "view_frame": f"frames\\turn_{n:02d}_view.png",
                "choices_frame": f"frames\\turn_{n:02d}_choices.png",
                "selected_frame": f"frames\\turn_{n:02d}_selected.png",
                "scene_changed": True,
                "game_status": {"image_model": "gemini-3-pro-image",
                                "image_provider": "gemini", "chaos": n,
                                "phase": "normal", "health": 100,
                                "time_of_day": "7:14pm | weather: hazy"},
            } for n in (1, 2)],
        }), encoding="utf-8")
        for name in ("SUMMARY.md", "playtest.gif", "playtest_review.mp4",
                     "playtest_view.mp4", "playtest_choices.mp4", "playtest_selected.mp4"):
            (self.run / name).write_bytes(b"x")

    def _restore(self):
        render_jobs.RENDER_ROOT = self._orig_root
        render_jobs.BUNDLE_DIR = self._orig_bundles
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestBrowsingPastRenders(RenderRootSandbox):
    """A render's output outlives the job object, the process and the reboot,
    so the list has to come off disk. Reviewing only the most recent one would
    make every earlier render unreachable the moment the next one starts."""

    def test_a_finished_render_shows_up_without_a_live_job(self):
        render_jobs._job = None
        runs = render_jobs.history()["renders"]
        self.assertEqual([r["id"] for r in runs], ["render_20260101_010101"])
        self.assertEqual(runs[0]["turns_done"], 2)
        self.assertTrue(runs[0]["passed"])

    def test_the_models_are_recovered_from_the_transcript(self):
        # Renders made before the sidecar existed still have to say what drew
        # them, or the list can't tell a Pro run from a Lite one.
        self.assertEqual(render_jobs.history()["renders"][0]["models"]["image_model"],
                         "gemini-3-pro-image")

    def test_the_bundle_directory_is_not_listed_as_a_render(self):
        (self.tmp / "_bundles").mkdir()
        self.assertEqual(len(render_jobs.history()["renders"]), 1)

    def test_newest_first(self):
        newer = self.tmp / "render_20260202_020202"
        (newer / "frames").mkdir(parents=True)
        # Give it something to show, or the "skip dead runs" filter below
        # would drop it and this would stop testing sort order at all.
        (newer / "playtest.gif").write_bytes(b"x")
        self.assertEqual(render_jobs.history()["renders"][0]["id"],
                         "render_20260202_020202")

    def test_a_run_with_nothing_landed_is_not_in_the_library(self):
        # A render stopped before its first turn (or force-killed) has no
        # session, no gif, no video — nothing worth browsing to or clicking.
        (self.tmp / "render_20260303_030303" / "frames").mkdir(parents=True)
        ids = [r["id"] for r in render_jobs.history()["renders"]]
        self.assertNotIn("render_20260303_030303", ids)

    def test_a_thumbnail_is_offered_for_the_list(self):
        thumb = render_jobs.history()["renders"][0]["thumbnail"]
        self.assertEqual(thumb, "render_20260101_010101/frames/turn_01_view.png")
        self.assertTrue(render_jobs.artifact_path(thumb).is_file())
        run = render_jobs.history()["renders"][0]
        self.assertIn("frames/turn_01_view.png", run["frames"][0])
        self.assertTrue(run["gif"])

    def test_the_catalogue_flags_a_run_as_playable(self):
        # The list has to tell the app which runs have footage, or a click can
        # land on an empty video player.
        run = render_jobs.history()["renders"][0]
        self.assertTrue(run["has_video"])
        self.assertTrue(render_jobs.artifact_path(run["video"]).is_file())

    def test_a_swept_run_still_shows_a_thumbnail(self):
        # After clean_artifacts drops the stills, the GIF (which it keeps) is
        # the preview, so the catalogue tile is never blank for a playable run.
        shutil.rmtree(self.run / "frames")
        run = render_jobs.history()["renders"][0]
        self.assertEqual(run["thumbnail"], "render_20260101_010101/playtest.gif")
        self.assertTrue(run["has_video"])


class TestReviewDetail(RenderRootSandbox):

    def test_each_turn_carries_its_picture_and_its_story(self):
        turn = render_jobs.detail("render_20260101_010101")["turns"][0]
        self.assertEqual(turn["action"], "Enter the shed 1")
        self.assertEqual(turn["narrative"], "Narrative 1.")
        self.assertEqual(turn["view"], "render_20260101_010101/frames/turn_01_view.png")

    def test_frame_paths_are_url_shaped_not_windows_shaped(self):
        # The transcript stores "frames\turn_01_view.png" on Windows. A
        # backslash is a literal character in a URL, so the frame 404s.
        for turn in render_jobs.detail("render_20260101_010101")["turns"]:
            for kind in ("view", "choices", "selected"):
                self.assertNotIn("\\", turn[kind])
                self.assertTrue(render_jobs.artifact_path(turn[kind]).is_file())

    def test_all_three_frame_variants_are_reachable(self):
        art = render_jobs.detail("render_20260101_010101")["artifacts"]
        # VIEW has one extra: the final generation the last SELECTED choice
        # produced, one past the last turn.
        for kind, expected in (("view", 3), ("choices", 2), ("selected", 2)):
            self.assertEqual(len(art[f"{kind}_frames"]), expected)

    def test_the_final_view_closes_the_loop(self):
        # The last turn's SELECTED choice has to produce something reviewable
        # too, or the run ends on a picture of an option rather than an outcome.
        detail = render_jobs.detail("render_20260101_010101")
        self.assertEqual(detail["final_view"], "render_20260101_010101/frames/turn_03_view.png")

    def test_the_deliverables_are_all_listed(self):
        art = render_jobs.detail("render_20260101_010101")["artifacts"]
        for key in ("gif", "review_video", "view_video", "choices_video",
                    "selected_video", "summary", "transcript"):
            self.assertIn(key, art)

    def test_an_older_run_still_plays_through_its_legacy_video_names(self):
        # Runs made before the naming settled wrote scene/scan/result.mp4. They
        # still have to resolve to the view/choices/selected slots the player
        # asks for, or the whole back catalogue reads as unplayable.
        old = self.tmp / "render_20251201_120000"
        old.mkdir()
        for name in ("playtest_scene.mp4", "playtest_scan.mp4", "playtest_result.mp4"):
            (old / name).write_bytes(b"x")
        art = render_jobs.detail("render_20251201_120000")["artifacts"]
        self.assertEqual(art["view_video"], "render_20251201_120000/playtest_scene.mp4")
        self.assertEqual(art["choices_video"], "render_20251201_120000/playtest_scan.mp4")
        self.assertEqual(art["selected_video"], "render_20251201_120000/playtest_result.mp4")

    def test_a_run_id_cannot_climb_out_of_the_render_root(self):
        for bad in ("..", "../..", "render_20260101_010101/frames"):
            with self.assertRaises((ValueError, FileNotFoundError)):
                render_jobs.detail(bad)

    def test_watch_overlay_beats_come_from_the_recorded_turns(self):
        # Watch paints from detail()["beats"], not an empty theater.
        detail = render_jobs.detail("render_20260101_010101")
        beats = detail["beats"]
        self.assertEqual(len(beats), 2)
        self.assertEqual(beats[0]["choice"], "Enter the shed 1")
        self.assertEqual(beats[0]["subject"], "shed")
        self.assertEqual(len(beats[0]["boxes"]), 2)
        self.assertEqual(beats[0]["boxes"][0]["label"], "shed")
        self.assertAlmostEqual(beats[0]["boxes"][0]["cx"], 0.42)
        self.assertEqual(beats[0]["choices"], ["Wait", "Look around"])
        self.assertEqual(detail["overlay"]["review_page_s"], 1.1)
        self.assertTrue(detail["overlay"]["burned_in"])

    def test_watch_overlay_prefers_live_json_when_present(self):
        (self.run / "live.json").write_text(json.dumps({"beats": [
            {"turn": 9, "kind": "choice", "phase": "chosen",
             "choice": "Open it", "choices": ["Open it", "Walk on"],
             "boxes": [], "narrative": "The latch gives."},
        ]}), encoding="utf-8")
        beats = render_jobs.detail("render_20260101_010101")["beats"]
        self.assertEqual(len(beats), 1)
        self.assertEqual(beats[0]["choice"], "Open it")
        self.assertEqual(beats[0]["choices"], ["Open it", "Walk on"])

    def test_watch_overlay_is_empty_when_nothing_was_recorded(self):
        bare = self.tmp / "render_bare_old"
        bare.mkdir()
        (bare / "session.json").write_text("{}", encoding="utf-8")
        detail = render_jobs.detail("render_bare_old")
        self.assertEqual(detail["beats"], [])
        self.assertFalse(detail["overlay"]["burned_in"])


class TestExport(RenderRootSandbox):
    """Export means one file with everything in it, not six right-clicks."""

    def test_the_zip_holds_the_whole_run(self):
        import zipfile
        with zipfile.ZipFile(render_jobs.bundle("render_20260101_010101")) as z:
            names = z.namelist()
        self.assertIsNone(zipfile.ZipFile(
            render_jobs.bundle("render_20260101_010101")).testzip())
        for tail in ("SUMMARY.md", "session.json", "playtest.gif",
                     "playtest_review.mp4", "frames/turn_02_selected.png"):
            self.assertTrue(any(n.endswith(tail) for n in names), tail)

    def test_the_zip_is_reused_until_the_run_changes(self):
        first = render_jobs.bundle("render_20260101_010101")
        stamp = first.stat().st_mtime_ns
        self.assertEqual(render_jobs.bundle("render_20260101_010101").stat().st_mtime_ns,
                         stamp)

    def test_the_zip_is_served_through_the_same_guarded_resolver(self):
        render_jobs.bundle("render_20260101_010101")
        path = render_jobs.artifact_path("_bundles/render_20260101_010101.zip")
        self.assertTrue(path.is_file())

    def test_a_bogus_run_cannot_be_zipped(self):
        with self.assertRaises((ValueError, FileNotFoundError)):
            render_jobs.bundle("../..")


class TestTheServerAdvertisesWhatTheFormNeeds(ConfigSandbox):
    """The picker is drawn from the server so a new model shows up without a
    client change — the reason the catalogue is data in the first place."""

    def test_options_carry_models_sizes_and_modes(self):
        opts = render_jobs.options()
        self.assertTrue(opts["image_models"])
        self.assertTrue(opts["text_models"])
        self.assertEqual(tuple(opts["sizes"]), ai_provider_manager.IMAGE_SIZES)
        self.assertIn("scan_move", [m["id"] for m in opts["modes"]])
        ids = [r["id"] for r in opts["aspect_ratios"]]
        self.assertEqual(ids[0], "16:9")
        self.assertIn("21:9", ids)
        phone = next(r for r in opts["aspect_ratios"] if r["id"] == "21:9")
        self.assertIn("phone", (phone.get("label") or "").lower()
                      + (phone.get("title") or "").lower()
                      + (phone.get("alias") or "").lower())

    def test_renders_default_to_the_fast_model(self):
        # Watch is the same room as Play. Fast stills are the default;
        # Pro is a desk option, not a splash.
        d = render_jobs.options()["defaults"]
        self.assertEqual(d["image_model"], "gemini-3.1-flash-lite-image")
        self.assertEqual(d["image_size"], "1K")
        self.assertEqual(d["aspect_ratio"], "16:9")

    def test_the_default_model_is_one_the_catalogue_knows(self):
        d = render_jobs.options()["defaults"]
        self.assertIsNotNone(ai_provider_manager.find_model("image", d["image_model"]))
        self.assertIn(d["image_size"], ai_provider_manager.IMAGE_SIZES)
        self.assertIn(d["aspect_ratio"], ai_provider_manager.IMAGE_ASPECT_RATIOS)


class TestTheCatalogueNamesRealModels(unittest.TestCase):
    """A model id in the catalogue is a promise that the provider has it.

    Get it wrong and nothing complains until a render is minutes in and every
    frame comes back empty — the picker looked right, the config looked right,
    and the 404 happened inside a background thread. Worth one network call:
    this build's first guess at Nano Banana Pro was `gemini-3.1-pro-image`,
    following the naming of the Flash models, and Google actually ships it as
    `gemini-3-pro-image`.
    """

    def setUp(self):
        self.key = os.environ.get("GEMINI_API_KEY", "")
        if not self.key:
            self.skipTest("no GEMINI_API_KEY — cannot ask what exists")

    def test_every_gemini_image_model_offered_actually_exists(self):
        import requests
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         headers={"x-goog-api-key": self.key},
                         params={"pageSize": 200}, timeout=30)
        if r.status_code != 200:
            self.skipTest(f"model list unavailable (HTTP {r.status_code})")
        live = {m["name"].removeprefix("models/") for m in r.json().get("models", [])}
        offered = {e["id"] for e in ai_provider_manager.model_catalogue("image")
                   if e.get("provider") == "gemini"}
        self.assertTrue(offered <= live,
                        f"catalogue names models Gemini doesn't have: {sorted(offered - live)}")


class TestTheEndpointsExist(unittest.TestCase):
    """Source assertions for the seams between the panel and the job."""

    @classmethod
    def setUpClass(cls):
        cls.api = (ROOT / "api.py").read_text(encoding="utf-8")
        cls.client = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")
        cls.html = (ROOT / "templates/standalone.html").read_text(encoding="utf-8")

    def test_every_route_the_panel_calls_is_registered(self):
        for route in ("/api/render/options", "/api/render/start",
                      "/api/render/status", "/api/render/cancel",
                      "/api/render/history"):
            self.assertIn(f"@app.route('{route}'", self.api)
            self.assertIn(f'"{route}"', self.client)

    def test_the_review_and_export_routes_are_registered(self):
        for route in ("/api/render/run/<run_id>", "/api/render/download/<run_id>"):
            self.assertIn(f"@app.route('{route}'", self.api)
        self.assertIn('"/api/render/run/"', self.client)
        self.assertIn('"/api/render/download/"', self.client)

    def test_artifacts_are_served_through_the_guarded_resolver(self):
        self.assertIn("render_jobs.artifact_path(rel)", self.api)

    def test_the_same_url_can_be_viewed_or_downloaded(self):
        self.assertIn("request.args.get('download')", self.api)
        self.assertIn("as_attachment=True", self.api)
        self.assertIn("?download=1", self.client)

    def test_the_reviewer_owns_the_keyboard_while_it_is_open(self):
        # It is full-screen over the game. Arrows must step turns rather than
        # driving the player around underneath it.
        self.assertIn("if (Review.visible()) {", self.client)
        self.assertIn("Review.onKey(e)", self.client)
        self.assertIn("Review.init();", self.client)

    def test_frame_review_is_folds_not_a_checks_dump(self):
        # Timeline centered under the picture; generated copy lives in
        # collapsible panels; harness pass/fail text does not belong here.
        self.assertIn('class="rv-strip-wrap"', self.html)
        self.assertIn('class="rv-fold"', self.html)
        self.assertIn(">This turn</summary>", self.html)
        self.assertIn(">State</summary>", self.html)
        self.assertIn(">Files</summary>", self.html)
        self.assertNotIn('id="rv-verdict"', self.html)
        self.assertNotIn("All checks passed", self.client)
        self.assertIn("setFold(", self.client)
        css = (ROOT / "static/css/standalone.css").read_text(encoding="utf-8")
        self.assertIn(".rv-strip-wrap", css)
        self.assertIn(".rv-fold > summary", css)

    def test_past_renders_are_reachable_from_the_panel(self):
        self.assertIn('id="render-history"', self.html)
        self.assertIn('id="render-review"', self.html)
        # A past run opens in the video player (WATCH), which links across to
        # the frame-by-frame reviewer for debugging.
        self.assertIn("WatchPlayer.open(run.id)", self.client)
        self.assertIn('id="wp-more"', self.html)
        self.assertIn('id="wp-play"', self.html)
        self.assertIn('id="wp-seek"', self.html)
        self.assertNotIn('id="wp-video" class="wp-video" controls', self.html)
        self.assertNotIn("FRAME REVIEW", self.html)
        self.assertIn(">STILLS</button>", self.html)
        self.assertIn('id="wp-cuts"', self.html)
        self.assertIn('id="watch-review"', self.html)
        self.assertIn("const WatchOverlay", self.client)
        self.assertIn("WatchOverlay.stateAtTime", self.client)
        self.assertIn("WatchOverlay.stateFromBeat", self.client)
        self.assertIn('id="wp-overlay"', self.html)
        self.assertNotIn("scan-tutorial", self.client)
        self.assertNotIn("id=\"scan-tutorial\"", self.html)

    def test_a_busy_render_answers_409_rather_than_queueing(self):
        self.assertIn("code=409", self.api)

    def test_watch_setup_remembers_turns(self):
        # TURNS used to be restamped from the factory default (40) every time
        # options loaded, so a typed value never survived leaving SETUP.
        self.assertIn('id="render-turns"', self.html)
        self.assertIn("somewhere.render.desk", self.client)
        self.assertIn("function rememberTurns", self.client)
        self.assertIn("function turnsToShow", self.client)
        self.assertIn("function applySavedTurns", self.client)
        self.assertIn("applySavedTurns();", self.client)
        self.assertNotIn("el.renderTurns.value = d.turns || 40", self.client)

    def test_watch_mode_hosts_the_render_studio(self):
        # RENDER is no longer a rail button: it moved into the first-class WATCH
        # studio. Play's picker offers it under PLAY, K jumps to it, and Render
        # still initializes (its panel is re-parented into the studio at runtime).
        self.assertIn('id="xp-watch"', self.html)
        self.assertNotIn('id="start-watch"', self.html)
        self.assertIn('id="watch-mode"', self.html)
        self.assertIn('StartMenu.switchMode("watch")', self.client)
        self.assertIn("Render.init();", self.client)

    def test_the_boot_gate_cannot_hold_the_ui_forever(self):
        """The worst failure state in the app, and it had no floor.

        `body.awaiting-first-scene` sets #menu-toggle, #control-rail and
        #danger-health to display:none, so while the gate is up there is no
        chrome at all — not even a way to reach reset. The 20s ceiling that
        exists to release it re-armed itself for as long as a Moment was on
        screen, which is correct for a montage that legitimately runs long and
        catastrophic for one that never finishes: the opening cutscene IS a
        Moment, its shots are generated, and /api/cutscene/complete is what
        pops it. If that never lands the run sits on a black screen with no
        controls, forever.

        Twenty-five realtime e2e tests were in exactly that state, timing out
        on a menu button CSS had hidden. Two other suites already work around
        it by stripping the class off `body` by hand, which is the tell.
        """
        self.assertIn("BOOT_GATE_ABSOLUTE_MS", self.client)
        gate = self.client.split("function armBootGateCeiling(", 1)[1] \
                          .split("\n  }", 1)[0]
        # The re-arm must be conditional on the absolute ceiling, not on the
        # Moment alone.
        self.assertIn("BOOT_GATE_ABSOLUTE_MS", gate)
        self.assertIn("Moments.isActive()", gate)
        self.assertLess(gate.index("BOOT_GATE_ABSOLUTE_MS"),
                        gate.index("Moments.isActive()"),
                        "the ceiling has to be checked before the Moment "
                        "re-arms the timer, or it re-arms forever")
        # Generous enough not to cut a real montage off: four shots held four
        # seconds each, plus the generation that made them.
        absolute = int(self.client.split("BOOT_GATE_ABSOLUTE_MS = ", 1)[1]
                       .split(";", 1)[0])
        normal = int(self.client.split("BOOT_GATE_MAX_HOLD_MS = ", 1)[1]
                     .split(";", 1)[0])
        self.assertGreater(absolute, normal * 2)
        self.assertLessEqual(absolute, 180000)

    def test_the_start_menu_arrives_from_a_textless_splash(self):
        self.assertIn('id="start-splash"', self.html)
        self.assertIn("start-splash-bar", self.html)
        self.assertNotIn("start-splash-ring", self.html)
        self.assertNotIn("start-splash-glow", self.html)
        self.assertIn("scheduleReveal", self.client)
        self.assertIn("start-arrived", self.client)
        # Splash must be on the first paint — start-menu is display:none
        # until body.start-menu-on, and the ACT / PLAY hubs used to flash
        # through that gap before JS ran.
        self.assertIn('class="start-menu-on awaiting-first-scene"', self.html)
        css = (ROOT / "static/css/standalone.css").read_text(encoding="utf-8")
        self.assertIn("body.start-menu-on #action-wheel", css)
        self.assertIn("body.awaiting-first-scene #action-wheel", css)
        self.assertNotIn('class="boot-brand"',
                         (ROOT / "templates/lobby.html").read_text(encoding="utf-8"))
        splash = self.html.split('id="start-splash"', 1)[1].split("start-menu-inner", 1)[0]
        self.assertNotIn("SOMEWHERE", splash)
        self.assertIn('class="start-brand"', self.html)
        self.assertIn("SOMEWHERE", self.html.split("start-menu-inner", 1)[1])
        self.assertIn("ensureDom", self.client)

    def test_every_menu_sits_on_the_same_gradient_field(self):
        """The corner washes were sized as localized blobs (50%/40% of the
        viewport), which read as smudges in the corners of an otherwise flat
        card — and made moving between two menus look like a cut between two
        backgrounds rather than a move within one room.

        They are one field now, larger than the screen, and the three menus
        share the tokens so they cannot drift apart.
        """
        css = (ROOT / "static/css/standalone.css").read_text(encoding="utf-8")
        root = css.split(":root {", 1)[1].split("}", 1)[0]
        for token in ("--menu-wash-bl", "--menu-wash-br"):
            self.assertIn(token, root, f"{token} is not a shared token")
        # Bigger than the viewport on both axes, or it is not covering anything.
        for token in ("--menu-wash-bl", "--menu-wash-br"):
            value = root.split(token + ":", 1)[1].split(";", 1)[0]
            sizes = [int(part.strip().rstrip("%"))
                     for part in value.split() if part.strip().endswith("%")]
            self.assertEqual(len(sizes), 2, f"{token} needs two radii")
            for n in sizes:
                self.assertGreater(n, 100, f"{token} is smaller than the screen")

        for screen in ("#start-menu {", "#watch-mode {", ".exit-veil {"):
            with self.subTest(screen=screen):
                block = css.split(screen, 1)[1].split("\n}", 1)[0]
                self.assertIn("var(--menu-wash-br)", block,
                              f"{screen} is not on the shared field")

    def test_the_play_transition_is_a_field_not_three_circles(self):
        """Enlarging the MENU washes did not touch this, and it is what you
        actually look at when you press PLAY: the Buck veil drifts three blooms
        across the screen. They were 54 / 62 / 46vmax and animated up from
        0.22-0.4 scale, so the transition opened on three small circles with
        their own visible edges — "I still see the 3 little gradients".

        Two things have to hold, and the second is the one that was missed: an
        element larger than the screen still reads as a circle if the animation
        starts it at a quarter size.
        """
        css = (ROOT / "static/css/standalone.css").read_text(encoding="utf-8")
        for cls in ("buck-bloom-a", "buck-bloom-b", "buck-bloom-c"):
            with self.subTest(bloom=cls):
                block = css.split(f".{cls} {{", 1)[1].split("}", 1)[0]
                width = block.split("width:", 1)[1].split("vmax", 1)[0].strip()
                self.assertGreater(
                    int(width), 100,
                    f"{cls} is smaller than the viewport — it has a rim")

                frames = css.split(f"@keyframes {cls} {{", 1)[1].split("\n}", 1)[0]
                start = frames.split("scale(", 1)[1].split(")", 1)[0]
                self.assertGreater(
                    float(start), 0.8,
                    f"{cls} still opens at scale {start} — a small circle "
                    f"however large the element is")

    def test_the_title_card_is_plain_and_not_last_run_s_wallpaper(self):
        """The menu used to paper itself with the last run's final frame.

        Through `brightness(0.72)` with the VHS grain over it that read as mud,
        and it made the title screen a different picture every launch — of a run
        you had already finished. Switched off at the one choke point that
        paints it; the machinery stays, because Signal still hands that still to
        the scene layer on the way into a run (lock / hold / takeHold) and still
        paints the Watch TV's ghost.
        """
        self.assertIn("const WALLPAPER = false", self.client)
        # Scoped to the Signal module: there is an earlier apply() in the
        # device-detection code that has nothing to do with the menu.
        signal = self.client.split("const Signal = (function ()", 1)[1]
        apply_fn = signal.split("    function apply() {", 1)[1] \
                         .split("\n    }", 1)[0]
        self.assertIn("if (!WALLPAPER)", apply_fn)
        # The hold-plate duty is NOT what was turned off.
        self.assertIn("function takeHold", self.client)
        self.assertIn("Signal.lock(\"play\")", self.client)

    def test_last_run_is_the_wallpaper_under_the_wordmark(self):
        css = (ROOT / "static/css/standalone.css").read_text(encoding="utf-8")
        self.assertNotIn("background-clip: text", css)
        self.assertNotIn("mix-blend-mode: destination-in", css)
        signal_css = css.split(".start-signal {", 1)[1].split(".buck-veil", 1)[0]
        self.assertIn("object-fit: cover", signal_css)
        self.assertNotIn("opacity: 0.20", signal_css)
        brand = self.html.split('id="start-brand"', 1)[1].split("start-tiles", 1)[0]
        self.assertNotIn("start-signal", brand)
        self.assertIn('id="start-signal"', self.html.split("start-menu-inner", 1)[0])
        self.assertIn("/api/tape", self.client)
        self.assertIn("start-signal-canvas", self.client)
        lobby = (ROOT / "static/js/lobby.js").read_text(encoding="utf-8")
        self.assertIn("BrandFill", lobby)
        self.assertIn("/api/tape", lobby)

    def test_play_opens_an_experience_picker(self):
        # PLAY is "what experience", not a co-equal Watch choice. The selected
        # tile's still fills the picker full-bleed; it used to sit at 20% over
        # the menu's green gradient, which read as black with a tint rather
        # than as the film you were about to start. Scrims carry legibility.
        self.assertIn('id="xp-picker"', self.html)
        self.assertIn('id="xp-play"', self.html)
        self.assertIn('id="xp-edit"', self.html)
        self.assertIn("confirmEdit()", self.client)
        self.assertIn("function adoptSelection", self.client)
        edit_fn = self.client.split("async function confirmEdit", 1)[1].split("function onKey", 1)[0]
        self.assertNotIn("settleWatch", edit_fn)
        self.assertIn('from: "picker"', edit_fn)
        self.assertIn("ensurePlayViewport", self.client)
        self.assertIn("paintViewportFromFrame", self.client)
        self.assertIn("function restoreReturn", self.client)
        self.assertIn('id="we-save"', self.html)
        self.assertIn('id="we-picture-bar"', self.html)
        self.assertIn("function flushSave", self.client)
        self.assertIn("resolveEditingWorldId", self.client)
        self.assertIn('id="xp-track"', self.html)
        self.assertIn('id="xp-watch"', self.html)
        self.assertIn('id="xp-stage-img-b"', self.html)
        self.assertIn("keepCellInTrack", self.client)
        picker_js = self.client.split("function paintStage(", 1)[1].split("function begin(", 1)[0]
        self.assertNotIn("scrollIntoView", picker_js)
        # The editor says so when a save kicks a re-render. Asserted on the
        # copy that ships: "Updating picture" was never in the client, at HEAD
        # or otherwise, so this line had been failing since it was written.
        self.assertIn("updating the picture", self.client)
        self.assertIn("confirmWatch()", self.client)
        self.assertIn("returnToPicker()", self.client)
        self.assertNotIn('id="start-watch"', self.html)
        self.assertNotIn("start-watch-quiet", self.html)
        self.assertIn("/api/experiences", self.client)
        self.assertIn("/api/experiences/activate", self.client)
        self.assertIn("openPicker()", self.client)
        self.assertIn("confirmPlay()", self.client)
        self.assertIn("@app.route('/api/experiences'", self.api)
        css = (ROOT / "static/css/standalone.css").read_text(encoding="utf-8")
        start_css = css.split("#start-menu {", 1)[1].split("#start-menu::before", 1)[0]
        self.assertIn("overflow: clip", start_css)
        self.assertIn(".xp-stage.has-img .xp-stage-img", css)
        self.assertIn(".xp-stage::after", css)
        self.assertIn(".xp-dock", css)
        self.assertIn("@keyframes xp-play-glow", css)
        self.assertIn(".xp-hero", css)
        self.assertIn(".xp-watch", css)
        self.assertIn("Back to Play", self.html)
        self.assertIn("watch-back-lbl\">PLAY", self.html)

    def test_play_from_the_picker_restarts_the_run(self):
        # EDITOR boots a live session. Closing it returns to the picker
        # without ending that session. PLAY has to reset, or you resume
        # the editor's run instead of starting the Experience.
        settle_play = self.client.split("function settlePlayFromPicker", 1)[1].split(
            "async function activateSelected", 1
        )[0]
        self.assertIn("resetGame()", settle_play)
        self.assertNotIn("if (!booted)", settle_play)

    def test_mode_hops_share_a_buck_veil(self):
        # Play, Watch, Keys, and Coin swap under one growing mint wash so the
        # cut never reads as a snap. Deep links skip it; reduced motion snaps.
        self.assertIn('id="buck-veil"', self.html)
        self.assertIn("buck-bloom-a", self.html)
        self.assertIn("const Buck", self.client)
        self.assertIn("Buck.play(settlePlay)", self.client)
        self.assertIn("Buck.play(settleWatch)", self.client)
        self.assertIn("Buck.play(applyOpen)", self.client)
        self.assertIn("StartMenu.returnHome()", self.client)
        self.assertIn("enterPlay({ instant: true })", self.client)
        css = (ROOT / "static/css/standalone.css").read_text(encoding="utf-8")
        self.assertIn(".buck-veil", css)
        self.assertIn("@keyframes buck-bloom-a", css)
        self.assertIn("@keyframes buck-wash-drift", css)

    def test_watch_is_a_desk_and_tv_not_a_splash(self):
        # TV fills the room with the current Experience. SETUP is a
        # top-center handle; the playtest strip is not the landing.
        self.assertIn('id="watch-desk"', self.html)
        self.assertIn('id="watch-tv"', self.html)
        self.assertIn('id="watch-tv-idle"', self.html)
        self.assertIn('id="watch-reel"', self.html)
        self.assertIn('id="watch-tv-play"', self.html)
        self.assertIn(">WATCH</button>", self.html)
        self.assertIn("SETUP", self.html)
        self.assertIn("watch-desk-open", self.client)
        self.assertIn('id="watch-runs-toggle"', self.html)
        self.assertIn("watch-runs-open", self.client)
        self.assertIn("function setRunsOpen", self.client)
        self.assertNotIn("A full playtest of the world exactly as your editor", self.html)
        self.assertIn(">START</button>", self.html)
        self.assertNotIn("START RENDER", self.html)
        self.assertIn("el.watchDesk.appendChild(el.renderForm)", self.client)
        self.assertIn("paintExperience()", self.client)
        self.assertIn("function beginWatch()", self.client)
        self.assertIn("has-experience", self.client)
        self.assertIn("currentPlate", self.client)
        self.assertIn('Render.show("form")', self.client)
        self.assertIn('size: "1K"', self.client)
        self.assertIn('id="render-aspect"', self.html)
        self.assertIn('aspect: "16:9"', self.client)
        self.assertIn("body.aspect_ratio = sel.aspect", self.client)
        self.assertIn("PHONE", self.client)
        css = (ROOT / "static/css/standalone.css").read_text(encoding="utf-8")
        self.assertIn("left: 50%", css)
        self.assertIn("translateX(-50%)", css)
        self.assertIn("-webkit-mask-image: linear-gradient", css)
        we_play = css.split(
            "body.world-editor-on:not(.start-menu-on) #world-editor::before {", 1
        )[1].split("}", 1)[0]
        self.assertIn("90deg", we_play)
        self.assertIn("transparent 68%", we_play)
        self.assertIn("backdrop-filter: none", we_play)
        self.assertIn("0.97", we_play)
        self.assertIn("has-experience", css)
        reel = css.split("body.mode-watch .watch-reel,", 1)[1].split("{", 1)[1].split("}", 1)[0]
        self.assertIn("display: none", reel)
        self.assertIn("#watch-mode.watch-runs-open .watch-reel", css)

    def test_the_editor_leaves_the_live_picture_playable(self):
        # VITAL and the action wheel sit on the clear viewport, not under the
        # wash, and a first-frame regen must not dim a live Reactor stream.
        css = (ROOT / "static/css/standalone.css").read_text(encoding="utf-8")
        recede = css.split(
            "body.world-editor-on:not(.mode-watch) #inventory-hud,", 1
        )[1].split("pointer-events: none;", 1)[0]
        self.assertNotIn("#danger-health", recede)
        self.assertNotIn("#action-wheel", recede)
        park = css.split(
            "body.world-editor-on:not(.mode-watch):not(.start-menu-on) #danger-health {",
            1,
        )[1].split("}", 1)[0]
        self.assertIn("z-index: 52", park)
        self.assertIn("left: 50%", park)
        self.assertNotIn("right: calc", park)
        self.assertIn("body.world-editor-on.world-frame-rendering:not(.realtime-on) .scene", css)
        self.assertIn("body.realtime-on #reactor-video", css)
        self.assertIn("#reactor-video:not(.hidden)", css)
        self.assertIn("#reactor-freeze.show", css)
        self.assertIn("keepLiveExperience()", self.client)
        self.assertIn("function present()", self.client)

    def test_the_live_run_is_a_timeline_of_expandable_dots(self):
        # Progress-bar + stacked beat cards got replaced by a growing rail.
        # START RENDER has to wipe the last picture immediately, not after
        # the first new frame arrives.
        self.assertIn('id="render-rail"', self.html)
        self.assertIn('id="render-dot-card"', self.html)
        self.assertNotIn('id="render-feed"', self.html)
        self.assertIn("function resetLiveStage()", self.client)
        self.assertIn("function paintRail(", self.client)
        self.assertIn("STOP &amp; SAVE", self.html)

    def test_the_run_takes_its_settings_from_the_live_editor(self):
        # It drives the same endpoints the browser does against this server, so
        # there is no second copy of the game settings to fall out of sync.
        self.assertIn("playtest_interactive.py", (ROOT / "render_jobs.py").read_text(encoding="utf-8"))
        self.assertIn("request.host_url", self.api)


class TestStillsAreAFloorNotAMode(unittest.TestCase):
    """A leftover stills preference must not lock Play or the editor.

    The product starts on stills and upgrades to the world model whenever a
    Reactor key is present. Persisting scene_renderer=image was how a green
    ACCOUNT lamp still left the session stuck on STILLS.
    """

    @classmethod
    def setUpClass(cls):
        cls.client = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")
        cls.editor = (ROOT / "static/js/editor_graph.js").read_text(encoding="utf-8")
        cls.reactor = (ROOT / "static/js/reactor_renderer.js").read_text(encoding="utf-8")
        cls.api = (ROOT / "api.py").read_text(encoding="utf-8")

    def test_boot_forgets_a_stale_image_preference(self):
        self.assertIn('localStorage.removeItem("scene_renderer")', self.client)
        self.assertNotIn('localStorage.setItem("scene_renderer"', self.client)

    def test_upgrade_path_exists_and_fallback_does_not_lock_image(self):
        self.assertIn("upgradeToLive(", self.client)
        self.assertIn("onReactorKeyChanged(", self.client)
        fallback = self.client.split("fallbackToStills(message, opts)", 1)[1].split("},", 1)[0]
        self.assertNotIn('this.mode = "image"', fallback)

    def test_picture_sheet_is_live_video_not_a_stills_toggle(self):
        self.assertIn('label: "Live video"', self.editor)
        self.assertNotIn('label: "Still images"', self.editor)
        self.assertIn("/api/keys", self.editor)

    def test_move_to_does_not_flash_the_leaving_world(self):
        """A MOVE TO pre-fade must stay black. Freezing the outgoing video
        under the veil, then lifting, is the original-image flash."""
        self.assertIn("function freezeLeavingWorld", self.reactor)
        freeze = self.reactor.split("function freezeLeavingWorld", 1)[1]
        freeze = freeze.split("function ", 1)[0]
        self.assertIn("awaitingReanchor", freeze)
        self.assertIn("captureVideoToFreeze", freeze)
        lingbot = self.reactor.split("async function applyRunningLingbot", 1)[1]
        lingbot = lingbot.split("async function establishHelios", 1)[0]
        self.assertIn("freezeLeavingWorld()", lingbot)
        self.assertNotIn("captureVideoToFreeze()", lingbot)
        oyster = self.reactor.split("async function applyRunningHappyOyster", 1)[1]
        oyster = oyster.split("const DRIVERS", 1)[0]
        self.assertIn("freezeLeavingWorld()", oyster)
        fade = self.reactor.split("function beginSceneFade(opts)", 1)[1]
        fade = fade.split("function freezeLeavingWorld", 1)[0]
        self.assertIn("alreadyDown", fade)

    def test_config_believes_the_account_lamp(self):
        self.assertIn("has_provider_key(\"reactor\")", self.api)
        self.assertIn('cache: "no-store"', self.reactor)
        self.assertIn("ACCOUNT has a Reactor key", self.reactor)


class TestAnEmptyReactorBalanceSettlesOnStillsAtOnce(unittest.TestCase):
    """Out of credits is the one Reactor failure that cannot self-heal, and
    it used to be handled as though it were a hiccup.

    Reactor answers `402 {"error":"credits_depleted"}`. That matched none of
    the classifier's patterns, so it read as "unknown" — a retryable class —
    and the session spent the retry ladder (3 attempts with backoff) plus the
    background resume (8 attempts at 25s) opening doomed sessions, roughly
    four minutes of "Realtime reconnecting…" that never once said the word
    credits. Play still worked underneath, because stills are the floor, but
    it spent the first minutes of every session fighting a lost battle.

    Stills stay a floor and not a lock: lockedStills is untouched, so buying
    credits and reloading — or saving a key in ACCOUNT, or pressing G —
    upgrades exactly as before.
    """

    @classmethod
    def setUpClass(cls):
        cls.client = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")
        cls.reactor = (ROOT / "static/js/reactor_renderer.js").read_text(encoding="utf-8")

    def test_a_402_is_named_as_credits_rather_than_unknown(self):
        classify = self.reactor.split("function classifyConnectError", 1)[1]
        classify = classify.split("function setStatus", 1)[0]
        self.assertIn('reason = "out_of_credits"', classify)
        self.assertIn(r"/\b402\b/", classify)
        self.assertIn("credits?[_\\s-]?depleted", classify)
        # The player has to be told what to actually do about it.
        self.assertIn("Reactor credits are gone", classify)

    def test_the_classifier_returns_the_terminal_flag_it_computes(self):
        classify = self.reactor.split("function classifyConnectError", 1)[1]
        classify = classify.split("function setStatus", 1)[0]
        self.assertIn("terminal: terminal", classify)
        # A rejected key and an unconfigured server are equally unwaitable.
        for cause in ('reason = "not_configured"', 'reason = "bad_key"'):
            after = classify.split(cause, 1)[1].split("} else if", 1)[0]
            self.assertIn("terminal = true", after)
        # Capacity and a blocked CDN genuinely do come back on their own.
        for cause in ('reason = "capacity"', 'reason = "sdk_blocked"'):
            after = classify.split(cause, 1)[1].split("} else if", 1)[0]
            self.assertNotIn("terminal = true", after)

    def test_connecting_is_refused_at_the_lowest_level(self):
        # Three call sites reach past the Renderer facade and call enable()
        # directly (the editor's keepLiveExperience, the Watch film, the
        # retry ladder), so the guard cannot live in the facade alone.
        enable = self.reactor.split("async function enable(opts)", 1)[1]
        enable = enable.split("async function disable", 1)[0]
        self.assertIn("rstate.lastError.terminal", enable)
        self.assertIn("opts.force", enable)

    def test_a_deliberate_retry_still_gets_through(self):
        enable = self.reactor.split("async function enable(opts)", 1)[1]
        enable = enable.split("async function disable", 1)[0]
        self.assertIn("if (opts && opts.force) rstate.lastError = null;", enable)
        # G, ACCOUNT and setMode are the player saying "try again, I fixed it".
        for call in ('reason: "key", force: true',
                     'reason: "toggle", force: true',
                     'reason: "setMode", hard: true, force: true'):
            self.assertIn(call, self.client)
        self.assertIn("enable({ force: !!opts.force })", self.client)

    def test_the_background_resume_does_not_poll_a_dead_account(self):
        arm = self.client.split("_armBackgroundResume() {", 1)[1]
        arm = arm.split("_cancelBackgroundResume() {", 1)[0]
        self.assertIn("if (this._terminalStills) return;", arm)
        fallback = self.client.split("fallbackToStills(message, opts)", 1)[1]
        fallback = fallback.split("_armBackgroundResume() {", 1)[0]
        self.assertIn("this._terminalStills = !!(opts && opts.terminal);", fallback)
        self.assertIn("if (this._terminalStills) this._cancelBackgroundResume();",
                      fallback)

    def test_the_error_handler_skips_the_retry_ladder_entirely(self):
        self.assertIn("if (lastErr && lastErr.terminal) {", self.client)
        handler = self.client.split("if (lastErr && lastErr.terminal) {", 1)[1]
        handler = handler.split("const isCapacity", 1)[0]
        self.assertIn("fallbackToStills(lastErr.hint, { terminal: true })", handler)

    def test_the_rail_lamp_does_not_claim_live_over_a_dead_stream(self):
        # mode stays "reactor" on the stills floor, so a lamp driven by mode
        # alone reads LIVE while nothing is live — the exact dead toggle the
        # renderer button was added to prevent.
        fn = self.client.split("function updateRendererButton() {", 1)[1]
        fn = fn.split("function updateModelSwitcher", 1)[0]
        self.assertIn("const groundedForGood = Renderer._terminalStills;", fn)
        self.assertIn('(!reactorMode || groundedForGood) ? "STILL"', fn)
        self.assertIn("getLastError() || {}).hint", fn)
        self.assertIn('". Click to retry (G)"', fn)

    def test_stills_stay_a_floor_and_never_become_a_lock(self):
        # The whole point of the original design: a failure must not leave
        # the session unable to upgrade once the cause is fixed.
        fallback = self.client.split("fallbackToStills(message, opts)", 1)[1]
        fallback = fallback.split("_armBackgroundResume() {", 1)[0]
        self.assertNotIn("this.lockedStills = true", fallback)
        self.assertNotIn('localStorage.setItem("scene_renderer"', self.client)
        upgrade = self.client.split("async upgradeToLive(opts)", 1)[1]
        upgrade = upgrade.split("_armBackgroundResume() {", 1)[0]
        self.assertIn("if (this._terminalStills && !opts.force) return false;", upgrade)


class TestTheLiveFilmIsCatalogued(RenderRootSandbox):
    """A Watch run recorded from the live world model is the Film cut. Stills
    runs keep working exactly as before — the live film is purely additive."""

    def test_live_video_is_a_known_artifact_backed_by_mp4_or_webm(self):
        self.assertIn("live_video", render_jobs._ARTIFACTS)
        self.assertEqual(render_jobs._ARTIFACTS["live_video"],
                         ["playtest_live.mp4", "playtest_live.webm"])

    def test_a_live_run_prefers_the_live_film_as_the_one_to_play(self):
        # The recorded animated world is what "play this run" should open.
        (self.run / "playtest_live.mp4").write_bytes(b"x")
        art = render_jobs._artifacts_of(self.run)
        self.assertEqual(render_jobs._best_video(art), art["live_video"])
        self.assertEqual(art["live_video"], "render_20260101_010101/playtest_live.mp4")

    def test_the_webm_backs_the_film_when_no_mp4_was_remuxed(self):
        (self.run / "playtest_live.webm").write_bytes(b"x")
        art = render_jobs._artifacts_of(self.run)
        self.assertEqual(art["live_video"], "render_20260101_010101/playtest_live.webm")

    def test_a_stills_only_run_still_counts_as_playable(self):
        # No live film present: the flipbook walkthrough is still the video, so
        # the catalogue keeps flagging the run playable exactly as before.
        render_jobs._job = None
        run = render_jobs.history()["renders"][0]
        self.assertTrue(run["has_video"])
        self.assertEqual(run["video"], "render_20260101_010101/playtest_review.mp4")

    def test_a_live_run_reports_the_live_film_to_the_catalogue(self):
        render_jobs._job = None
        (self.run / "playtest_live.mp4").write_bytes(b"x")
        run = render_jobs.history()["renders"][0]
        self.assertTrue(run["has_video"])
        self.assertEqual(run["video"], "render_20260101_010101/playtest_live.mp4")


class TestTheLiveFilmUpload(RenderRootSandbox):
    """The browser uploads the recorded clip; the server may only write it into
    the run this browser is (or just was) driving."""

    class _FakeJob:
        def __init__(self, run_id, out_dir):
            self.id = run_id
            self.out_dir = out_dir

    def _install_job(self):
        job = self._FakeJob("render_20260101_010101", self.run)
        self._orig_job = render_jobs._job
        render_jobs._job = job
        self.addCleanup(lambda: setattr(render_jobs, "_job", self._orig_job))
        return job

    def test_it_writes_the_webm_and_timeline_into_the_run(self):
        self._install_job()
        # Force the ffmpeg branch off so this test doesn't depend on a codec.
        with mock.patch.object(render_jobs, "_ffmpeg_exe", return_value=None):
            out = render_jobs.save_live(
                "render_20260101_010101", b"a-webm-blob" * 64,
                json.dumps([{"t": 0.0, "turn": 1, "phase": "scene"}]))
        self.assertTrue(out["ok"])
        self.assertTrue((self.run / "playtest_live.webm").is_file())
        tl = json.loads((self.run / "live_timeline.json").read_text(encoding="utf-8"))
        self.assertEqual(tl["timeline"][0]["turn"], 1)

    def test_it_refuses_a_film_for_the_wrong_run(self):
        self._install_job()
        with self.assertRaises(ValueError):
            render_jobs.save_live("render_somewhere_else", b"x" * 8192)

    def test_it_refuses_when_no_render_is_current(self):
        self._orig_job = render_jobs._job
        render_jobs._job = None
        self.addCleanup(lambda: setattr(render_jobs, "_job", self._orig_job))
        with self.assertRaises(ValueError):
            render_jobs.save_live("render_20260101_010101", b"x" * 8192)

    def test_it_refuses_a_film_over_the_size_cap(self):
        self._install_job()
        with self.assertRaises(ValueError):
            render_jobs.save_live("render_20260101_010101",
                                  b"x" * (render_jobs._LIVE_FILM_MAX_BYTES + 1))

    def test_the_recorded_timeline_rides_into_the_review_payload(self):
        self._install_job()
        (self.run / "live_timeline.json").write_text(
            json.dumps({"timeline": [{"t": 0.0, "turn": 1, "phase": "scene"},
                                     {"t": 4.2, "turn": 2, "phase": "scene"}]}),
            encoding="utf-8")
        overlay = render_jobs.detail("render_20260101_010101")["overlay"]
        self.assertEqual(overlay.get("kind"), "live")
        self.assertEqual(len(overlay["timeline"]), 2)

    def test_the_mp4_is_encoded_clean_for_a_background_loop(self):
        # The Film is meant to loop silently as a movie backdrop: H.264 on a
        # broadly-decodable pixel format, even dimensions, faststart, no audio.
        self._install_job()
        captured = {}

        def fake_run(argv, *a, **kw):
            captured["argv"] = argv
            # Simulate a successful encode by producing the output file.
            Path(argv[-1]).write_bytes(b"mp4")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(render_jobs, "_ffmpeg_exe", return_value="ffmpeg"), \
                mock.patch.object(render_jobs.subprocess, "run", side_effect=fake_run):
            out = render_jobs.save_live("render_20260101_010101", b"webm" * 64)
        self.assertTrue(out["mp4"])
        argv = captured["argv"]
        self.assertIn("-an", argv)
        self.assertIn("libx264", argv)
        self.assertIn("yuv420p", argv)
        self.assertIn("+faststart", argv)
        self.assertTrue((self.run / "playtest_live.mp4").is_file())


class TestTheSceneShowsYouWhatYouCanTouch(unittest.TestCase):
    """SCAN is how you interact with anything in this world, and it was the
    least discoverable thing in the game: the hotspots were the only signal
    that the picture could be touched, and they only appeared once you already
    knew to ask for them — then took themselves away again five seconds later.

    A scene now reads itself when it lands, once, and the tags stay for as long
    as the shot does. These pin both halves plus the budget, because the budget
    is why it was manual to begin with."""

    @classmethod
    def setUpClass(cls):
        cls.client = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "static/css/standalone.css").read_text(encoding="utf-8")

    def test_a_painted_scene_reads_itself(self):
        """markScenePainted is the only place that honestly means "a picture
        is on screen", because it runs after the image has decoded and been
        swapped in. Arming anywhere earlier scans the shot being replaced."""
        painted = self.client.index("function markScenePainted(")
        body = self.client[painted:painted + 1200]
        self.assertIn("AutoScan.arm()", body)

    def _apply_scene(self):
        """Renderer.applyScene, down to the end of its reactor branch."""
        start = self.client.index("applyScene(imageUrl, prompt, meta) {")
        return self.client[start:start + 6000]

    def test_the_live_renderer_is_armed_too(self):
        """Realtime never paints a still the player looks at — the video is
        the picture, and setScene only stages a floor behind it — so
        markScenePainted's arm never fires there. A live session came up with
        no hotspots at all until this second seam existed."""
        body = self._apply_scene()
        self.assertIn("AutoScan.arm()", body)
        self.assertLess(body.index("setScene("), body.index("AutoScan.arm()"),
                        "setScene clears the spent flag, so it has to run first")

    def test_it_is_armed_before_the_realtime_early_return(self):
        """markScenePainted returns early for a live session. Arming after
        that would silently give realtime runs no hotspots at all."""
        painted = self.client.index("function markScenePainted(")
        body = self.client[painted:painted + 1200]
        self.assertLess(body.index("AutoScan.arm()"), body.index("scanInRealtime()"))

    def test_the_pass_is_claimed_before_it_is_fired(self):
        """triggerScan is async. Claiming the budget after it resolves lets a
        second arm land mid-flight and buy a second detect call. The claim is
        released again when nothing actually went out — every guard in
        triggerScan is an early return, and in realtime the video can be
        mid-re-anchor with nothing honest to capture."""
        self.assertRegex(
            self.client,
            r"state\.autoScanDone = true;[\s\S]{0,500}?"
            r"if \(triggerScan\(null, \{ auto: true \}\) === false\) \{"
            r"\s*\n\s*state\.autoScanDone = false;")

    def test_a_declined_scan_says_so(self):
        """The release above is only honest if triggerScan reports it."""
        body = self.client[self.client.index("function triggerScan("):]
        body = body[:body.index("\n  }\n")]
        self.assertIn("if (!scanAvailable()) return false;", body)
        self.assertIn("return true;", body)

    def test_only_a_new_picture_refills_the_budget(self):
        """The spent flag is cleared where a new picture arrives and nowhere
        else, so the teardown paths (travel, instruments) can cancel a pending
        pass without handing out a fresh one against the same shot.

        One site per renderer: stills paint through setScene, and realtime
        never paints a still the player looks at at all — the video is the
        picture — so it arms off the world re-anchoring instead."""
        start = self.client.index("function setScene(")
        stills = self.client[start:start + 900]
        self.assertIn("state.autoScanDone = false", stills)
        self.assertIn("closeScan();", stills)
        self.assertIn("state.autoScanDone = false; AutoScan.arm();",
                      self._apply_scene())

    def test_coming_back_from_an_interaction_restores_the_hotspots(self):
        """The reported bug: auto-scan never re-activated after leaving an
        interaction or a conversation.

        The one-pass budget is per picture, but closeScan() spends nothing —
        it just tears the tags down. So every instrument that borrows the
        screen returned the player to the same shot with no hotspots on it
        and no way back except finding the SCAN button, which is the exact
        discoverability hole auto-scan exists to close."""
        body = self.client[self.client.index("    function rearm() {"):]
        body = body[:body.index("\n    }")]
        # Idempotent: a shot whose tags survived is already answered.
        self.assertIn("if (state.scanOn) return;", body)
        self.assertIn("if (state.scanBusy) return;", body)
        self.assertIn("state.autoScanDone = false;", body)
        self.assertIn("arm();", body)
        self.assertIn("return { arm, cancel, rearm, debug };", self.client)

    def test_every_way_back_to_the_world_re_arms_it(self):
        """The camera, the tape, the free-will box and a finished turn each
        hand the world back, and each used to do it with bare pixels."""
        for fn in ("function closeTouch(", "function closeTape(",
                   "function closeFreeWill(", "function hideVeil("):
            body = self.client[self.client.index(fn):]
            body = body[:body.index("\n  }")]
            self.assertIn("AutoScan.rearm()", body,
                          f"{fn} must give the scan pass back")

    def test_a_moment_hands_the_world_back_readable(self):
        """Conversation, interact, encounter, cutscene and camp all leave
        through the same chokepoint, so the re-arm lives there rather than
        being re-derived in five exit handlers."""
        moments = (ROOT / "static/js/moments.js").read_text(encoding="utf-8")
        body = moments[moments.index("  function resumeUnderlay() {"):]
        body = body[:body.index("\n  }")]
        self.assertIn("window.__AutoScan.rearm()", body)

    def test_a_menu_is_not_a_picture_you_are_playing(self):
        """The start menu paints a real rendered still as its backdrop, and it
        paints it through setScene like any other picture. Without this gate
        the game spent a Gemini detection call and hung six hotspots over the
        main menu on EVERY boot — measured on the real client, which is the
        only place it shows: the realtime e2e page never paints a still, so no
        test could have caught it.

        Watch mode is the same argument: those scenes are watched, not played,
        and there is nothing in them to interact with."""
        body = self.client[self.client.index("const AutoScan = "):]
        body = body[:body.index("try { window.__AutoScan")]
        self.assertIn("function inPlay()", body)
        self.assertIn('c.contains("start-menu-on")', body)
        self.assertIn('c.contains("mode-watch")', body)
        # ...and blocked() has to actually consult it.
        gate = body[body.index("function blocked()"):]
        self.assertIn("if (!inPlay()) return true;", gate[:400])

    def test_entering_play_arms_it_too(self):
        """A resumed session returns from ensurePlayViewport without rendering
        anything, so the scene already on screen never paints again — that arm
        is the only one it will ever get."""
        for fn in ("function settlePlay()", "function ensurePlayViewport("):
            body = self.client[self.client.index(fn):]
            body = body[:body.index("\n    }\n")]
            self.assertIn("AutoScan.arm()", body, fn)

    def test_a_blocked_view_waits_rather_than_burning_the_pass(self):
        """The image lands before the choices do, so a scene routinely paints
        while the turn is still resolving. triggerScan's guards are all early
        returns — firing into that would drop the pass in silence."""
        self.assertIn("AUTO_SCAN_POLL_MS", self.client)
        self.assertIn("AUTO_SCAN_GIVE_UP_MS", self.client)

    def test_the_hotspots_do_not_time_out(self):
        """The tags describe the picture, and the picture is still there.
        Every path that genuinely invalidates them calls closeScan()."""
        self.assertRegex(
            self.client,
            r"typeof window\.__SCAN_TTL_MS__ === \"number\"\)\s*\?\s*"
            r"window\.__SCAN_TTL_MS__\s*:\s*0")
        self.assertIn("if (!(SCAN_TTL_MS > 0)) return;", self.client)

    def test_there_is_a_way_to_switch_it_off(self):
        self.assertIn("window.__AUTO_SCAN__ !== false", self.client)

    def test_a_quiet_room_arrives_quietly(self):
        """Reported straight after this shipped: "NOTHING TO INTERACT WITH
        HERE" pulsing under the choices on arrival.

        That line is a fine answer to a SCAN the player pressed. Volunteered
        by the automatic pass in every quiet room it is the scene telling you
        not to bother — and since the pass happens on arrival now, it greeted
        the player with it. The automatic read asked nothing on their behalf,
        so it reports nothing."""
        body = self.client[self.client.index("function scanHintFor("):]
        body = body[:body.index("\n  }")]
        self.assertIn("if (auto) return \"\";", body)
        self.assertIn("triggerScan(null, { auto: true })", self.client)

    def test_the_hint_is_a_message_not_a_furnishing(self):
        """The tags persist because the picture they describe is still there.
        A hint answers the tap you just made and is then over — it used to be
        cleared by the scan fade, which no longer runs, so a single empty
        SCAN left it up for the rest of the scene."""
        self.assertIn("SCAN_HINT_MS", self.client)
        body = self.client[self.client.index("function setScanHint("):]
        body = body[:body.index("\n  }")]
        self.assertIn("state.scanHintTimer", body)

    def test_the_read_looks_like_a_read(self):
        """Without the scanline the labels just blink into existence with
        nothing to explain them."""
        self.assertIn("playScanSweep()", self.client)
        self.assertIn("#scan-layer.sweeping::after", self.css)
        self.assertIn("@keyframes scan-sweep", self.css)

    def test_the_sweep_respects_reduced_motion(self):
        # The stylesheet has several reduced-motion blocks; the rule has to be
        # inside one of them rather than merely present in the file.
        blocks = self.css.split("@media (prefers-reduced-motion: reduce)")[1:]
        self.assertTrue(
            any("#scan-layer::after { display: none; }" in b[:2000] for b in blocks),
            "the scanline must be suppressed under reduced motion")


class TestYouCanSpeakYourAction(unittest.TestCase):
    """Dictation for the free-will box.

    It uses the browser's own SpeechRecognition rather than recording audio
    and shipping it off to be transcribed, and that choice is the feature:
    words land in the box WHILE you are still talking, because the engine
    streams interim guesses and revises them. A record-then-upload round trip
    cannot do that at any speed. It also needs no API key, no server route and
    no per-use cost."""

    @classmethod
    def setUpClass(cls):
        cls.client = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")
        cls.html = (ROOT / "templates/standalone.html").read_text(encoding="utf-8")
        cls.css = (ROOT / "static/css/standalone.css").read_text(encoding="utf-8")

    def test_the_button_sits_left_of_the_box(self):
        form = self.html[self.html.index('<form id="custom-form"'):]
        form = form[:form.index("</form>")]
        self.assertLess(form.index('id="custom-mic"'), form.index('id="custom-input"'))

    def test_it_can_never_submit_the_form(self):
        """A button inside a form defaults to type=submit, which would fire
        the action off the moment you reached for the microphone."""
        mic = self.html[self.html.index('<button id="custom-mic"'):]
        self.assertIn('type="button"', mic[:200])

    def test_a_browser_that_cannot_listen_shows_no_microphone(self):
        """A dead control is worse than no control. It ships hidden and only
        the JS, having found an engine, reveals it."""
        self.assertIn('id="custom-mic"', self.html)
        mic = self.html[self.html.index('<button id="custom-mic"'):]
        self.assertIn('class="hidden"', mic[:260])
        self.assertIn('if (!available()) return;', self.client)
        self.assertIn('el.customMic.classList.remove("hidden")', self.client)

    def test_it_uses_the_native_engine(self):
        self.assertIn("window.SpeechRecognition || window.webkitSpeechRecognition",
                      self.client)

    def test_words_appear_while_you_are_still_talking(self):
        """interimResults is the whole reason this feels instant rather than
        like submitting a recording."""
        self.assertIn("rec.interimResults = true;", self.client)

    def test_it_listens_through_the_pauses(self):
        """Engines stop at the first breath. Without continuous plus the
        restart in onend, dictating one sentence takes three clicks."""
        self.assertIn("rec.continuous = true;", self.client)
        onend = self.client[self.client.index("rec.onend = ()"):]
        self.assertIn("if (!live) return;", onend[:200])
        self.assertIn("rec.start()", onend[:300])

    def test_recording_is_impossible_to_miss(self):
        self.assertIn("#custom-mic.recording", self.css)
        self.assertIn("@keyframes mic-pulse", self.css)
        self.assertIn("#action-wheel.dictating #custom-input", self.css)

    def test_a_mic_left_on_does_not_listen_to_the_room_forever(self):
        self.assertIn("MAX_MS", self.client)

    def test_it_adds_to_what_you_typed_instead_of_replacing_it(self):
        self.assertIn("baseText = (el.customInput && el.customInput.value)", self.client)

    def test_speech_obeys_the_same_length_limit_as_typing(self):
        """maxlength is not enforced for programmatic writes, so a ramble
        would otherwise sail past what the player is allowed to type."""
        self.assertIn('getAttribute("maxlength")', self.client)

    def test_every_way_out_releases_the_microphone(self):
        """Sending, closing the box, or Escape. A microphone still listening
        after its box is gone is the worst bug this feature could have."""
        submit = self.client[self.client.index("function submitCustomAction("):]
        self.assertIn("Dictation.stop()", submit[:400])
        close = self.client[self.client.index("function closeFreeWill("):]
        self.assertIn("Dictation.stop()", close[:300])
        self.assertIn("if (Dictation.isLive()) { Dictation.stop(); return; }", self.client)

    def test_a_blocked_microphone_says_so(self):
        self.assertIn('err === "not-allowed"', self.client)
        self.assertIn("Microphone blocked", self.client)

    def test_a_silent_pause_is_not_an_error(self):
        """`no-speech` fires constantly while someone thinks mid-sentence."""
        self.assertIn('err === "no-speech"', self.client)

    def test_it_is_wired_into_the_boot(self):
        self.assertIn("Dictation.init();", self.client)

    def test_a_dead_speech_engine_falls_back_instead_of_failing(self):
        """The reported bug. The native engine being PRESENT says nothing
        about whether it WORKS: a Chromium build without Google's speech key
        exposes SpeechRecognition and then fails every attempt with
        `network`, and corporate DNS does the same to real Chrome. There is
        no capability check for it — you find out by trying — so the first
        failure has to hand over to recording mid-click, keeping the button
        red so the player never learns there were two paths."""
        self.assertIn('if (err === "network")', self.client)
        self.assertIn("speechDead = true;", self.client)
        self.assertIn("startRecording({ resumed: true })", self.client)

    def test_it_does_not_retry_a_dead_engine_all_session(self):
        self.assertIn("if (Engine && !speechDead) startSpeech();", self.client)

    def test_the_recorded_path_says_it_is_working(self):
        """Only the fallback has the upload gap; an unlabelled pause there
        reads as a dead button."""
        self.assertIn("setBusy(", self.client)
        self.assertIn("transcribing", self.css)

    def test_the_recorder_always_releases_the_microphone(self):
        self.assertIn("function releaseStream(", self.client)
        self.assertIn("getTracks().forEach((t) => t.stop())", self.client)

    def test_a_fumbled_click_does_not_upload_silence(self):
        """Duration still decides this one: a clip too brief to hold a word
        is not worth a round trip whatever its level."""
        self.assertIn("heldMs < 350", self.client)

    def test_it_knows_whether_the_microphone_heard_anything(self):
        """The reported bug: "the mic doesn't work, it just says Didn't catch
        that". It did work, and that message was the problem — it could not
        tell a muted input from a mumble from a failing server.

        The old check was blob.size < 2048, which only ever caught DIGITAL
        silence. Measured in a real browser: four seconds of true silence is
        1.2 KB and slips under it, three seconds of an empty room is 48 KB
        and sails over. So every dead-input case uploaded, was paid for, came
        back empty and was blamed on the player's diction."""
        self.assertIn("function startMeter(", self.client)
        self.assertIn("getFloatTimeDomainData", self.client)
        self.assertIn("SILENT_PEAK", self.client)
        self.assertIn("No sound from", self.client)

    def test_the_level_is_visible_while_you_speak(self):
        """A red button only proves the button is red. The ring proves the
        microphone is hearing you, which is the thing a player otherwise has
        no way to find out."""
        self.assertIn('setProperty("--mic-level"', self.client)
        self.assertIn("--mic-level", self.css)

    def test_a_recorder_that_never_flushes_still_hands_back_audio(self):
        """Electron and some mobile builds do not reliably fire the final
        dataavailable, and one blob at the end is indistinguishable from a
        player who said nothing."""
        self.assertIn("media.start(250)", self.client)

    def test_every_kind_of_nothing_says_which_kind_it_was(self):
        """A missing API key, a rate limit, a dead upstream and a genuine
        silence all used to arrive as "" and be reported identically."""
        self.assertIn("REASON_TEXT", self.client)
        engine_src = (ROOT / "engine.py").read_text(encoding="utf-8")
        body = engine_src[engine_src.index("def _transcribe_audio("):]
        body = body[:body.index("def api_transcribe(")]
        for reason in ("disabled", "too_short", "upstream_error", "no_speech"):
            self.assertIn(f'_why("{reason}")', body)

    def test_a_dead_speech_engine_is_remembered_across_reloads(self):
        """The engine takes ~600ms to fail and the handover then waits on
        getUserMedia. Rediscovering that on the first click of every page
        load costs the player the front of their sentence, every time."""
        self.assertIn("SPEECH_DEAD_KEY", self.client)
        self.assertIn("function markSpeechDead()", self.client)

    def test_enter_waits_for_a_transcription_still_in_flight(self):
        """The recorded path finishes AFTER the stop, so hitting Enter the
        moment you stopped talking read an empty box and did nothing — and
        then the words appeared."""
        submit = self.client[self.client.index("function submitCustomAction("):]
        self.assertIn("Dictation.isTranscribing()", submit[:900])
        self.assertIn("Dictation.whenSettled(", submit[:900])


class TestDictationDoesNotInventWords(unittest.TestCase):
    """The prototype's worst failure, found by feeding it silence.

    The first prompt told the model the clip was "a player saying what they
    want their character to do next in a game". Handed one second of pure
    silence it answered "I'm going to go to the tavern." — told what kind of
    sentence to expect and given no audio, it produced one. A dictation box
    that invents an action the player never spoke is far worse than one that
    mishears them, because the player cannot tell that is what happened."""

    @classmethod
    def setUpClass(cls):
        cls.engine = (ROOT / "engine.py").read_text(encoding="utf-8")

    def _prompt(self):
        body = self.engine[self.engine.index("def _transcribe_audio("):]
        return body[:body.index("try:")]

    def test_the_prompt_does_not_describe_the_subject_matter(self):
        """Describing the job is fine. Describing what the sentence will
        probably be about is what produced the tavern."""
        prompt = self._prompt().lower()
        for seed in ("game", "player", "character", "action"):
            self.assertNotIn(seed, prompt.split("prompt = (")[1],
                             f"the transcription prompt must not mention {seed!r}")

    def test_it_asks_for_a_sentinel_rather_than_an_empty_reply(self):
        """Models are reliably bad at returning nothing and much better at
        returning a specific token."""
        self.assertIn("NO_SPEECH", self._prompt())

    def test_a_result_with_no_letters_in_it_is_not_speech(self):
        """Reported: the player said "approach the mesa" and the box filled
        with "00:00".

        Asked for words and having none, the model reaches for a subtitle
        artifact instead of the sentinel. The prompt already forbids
        timestamps and it produced one anyway, so the shape is rejected
        rather than left to instructions. A real action in this game always
        has a verb in it; digits alone never are one."""
        body = self.engine[self.engine.index("def _transcribe_audio("):]
        self.assertIn(r'if not re.search(r"[^\W\d_]", text, re.UNICODE):', body)

    def test_the_client_refuses_the_same_junk(self):
        """Both engines write through the same paint(), and both can hand
        back something that is not speech — so the guard cannot live only on
        the server."""
        client = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")
        self.assertIn("function spoken(text)", client)
        self.assertIn(r"/[^\W\d_]/u.test(s)", client)
        self.assertIn("const said = spoken((res && res.text)", client)

    def test_every_way_of_saying_nothing_reads_as_nothing(self):
        """The sentinel plus the phrasings a model reaches for when it
        decides to be helpful instead of literal. None may reach the box."""
        body = self.engine[self.engine.index("def _transcribe_audio("):]
        for phrasing in ("no_speech", "no speech detected", "silence",
                         "inaudible", "unintelligible"):
            self.assertIn(phrasing, body)


class TestTheLiveFilmSeamsAreWired(unittest.TestCase):
    """Source-level seams that CI can check without a live GPU / WebRTC."""

    @classmethod
    def setUpClass(cls):
        cls.client = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")
        cls.html = (ROOT / "templates/standalone.html").read_text(encoding="utf-8")
        cls.api = (ROOT / "api.py").read_text(encoding="utf-8")
        cls.css = (ROOT / "static/css/standalone.css").read_text(encoding="utf-8")
        cls.harness = (ROOT / "playtest_interactive.py").read_text(encoding="utf-8")

    def test_the_desk_offers_a_picture_choice(self):
        self.assertIn('id="render-picture"', self.html)
        self.assertIn("wantsLiveFilm", self.client)
        self.assertIn("liveFilmAvailable", self.client)

    def test_the_film_module_records_the_reactor_stream(self):
        self.assertIn("WatchFilm", self.client)
        self.assertIn("captureStream", self.client)
        self.assertIn("MediaRecorder", self.client)
        self.assertIn("/api/render/live", self.client)
        self.assertIn("function preview(", self.client)

    def test_the_film_is_a_smooth_silent_take(self):
        # A clean movie BG: scenes blend (soft transition, no black cuts) and
        # the recording carries video only (no world audio).
        film = self.client.split("const WatchFilm", 1)[1].split("const WatchMode", 1)[0]
        self.assertIn("hardTransition: false", film)
        self.assertIn("new MediaStream(vtracks)", film)

    def test_leaving_watch_saves_the_film(self):
        # A live film in progress must be handed off when the studio closes.
        self.assertIn("WatchFilm.abort()", self.client)

    def test_pressing_watch_starts_a_new_run(self):
        # WATCH starts a generated run. Replaying the last film is RUNS —
        # beginWatch used to fetch /api/render/history and open that, or
        # fall through into SETUP + WatchFilm.preview().
        watch = self.client.split("function beginWatch()", 1)[1].split("function placeExtras", 1)[0]
        self.assertIn("Render.start()", watch)
        self.assertIn("playSelected()", watch)
        self.assertNotIn("/api/render/history", watch)
        self.assertNotIn("WatchFilm.preview()", watch)
        self.assertNotIn("WatchPlayer.open", watch)
        self.assertNotIn("setDeskOpen(true)", watch)
        self.assertNotIn("Signal.lock(\"play\")", watch)

    def test_watch_does_not_land_on_a_stale_finished_run(self):
        # Entering Watch used to poll the last job and put its ceremony on
        # the TV. Finished films belong in RUNS; only a run that just
        # completed in this room should take the picture.
        paint = self.client.split("function paint(job)", 1)[1].split("let lastDoneRemaining", 1)[0]
        self.assertIn('!wasRunning && document.body.classList.contains("mode-watch")', paint)
        self.assertIn('show("form")', paint)

    def test_the_upload_endpoint_exists(self):
        self.assertIn("@app.route('/api/render/live'", self.api)
        self.assertIn("render_jobs.save_live(", self.api)

    def test_the_player_prefers_the_live_film_and_maps_overlays_by_time(self):
        self.assertIn('id: "live_video", label: "Film"', self.client)
        self.assertIn("stateAtTimeline", self.client)
        variants = self.client.split("const WatchPlayer", 1)[1].split("const VARIANTS = [", 1)[1].split("];", 1)[0]
        self.assertLess(variants.find("live_video"), variants.find("view_video"))
        self.assertLess(variants.find("view_video"), variants.find("review_video"))

    def test_watch_holds_the_last_still_instead_of_autoplaying_a_cut(self):
        # Finishing used to skip the last frame and autoplay Story/debug.
        paint = self.client.split("function paint(job)", 1)[1].split("let lastDoneRemaining", 1)[0]
        self.assertIn("updateProgressFrames(job.frames", paint)
        self.assertIn("paintStageFrame()", paint)
        ceremony = self.client.split("function paintCeremony(art, job)", 1)[1].split("function waitForReel", 1)[0]
        self.assertIn("paintHoldStill", ceremony)
        self.assertIn("REPLAY", ceremony)
        self.assertNotIn("node.play()", ceremony.split('if (watch)')[1].split("if (node && vid)")[0])

    def test_the_harness_puts_the_scene_prompt_on_live_beats(self):
        self.assertIn('"image_prompt": t.get("image_prompt")', self.harness)

    def test_the_reactor_stack_is_pinned_into_the_watch_tv(self):
        self.assertIn("body.mode-watch.watch-live #reactor-video", self.css)


if __name__ == "__main__":
    unittest.main()
