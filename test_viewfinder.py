"""
test_viewfinder.py — first-person PHOTO restage.

The viewfinder path must restage the current still as eyes-on-the-world
without appending history, saving a camera-mode change, or attaching the
character plate. Cache hits skip the renderer. A leaked protagonist retries
once without the 3P pixel reference.

Never touches the network: image gen and detect are mocked.

Run with:
    python -m unittest test_viewfinder -v
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import engine
import game_identity as gi
import prompts_store as ps


class ViewfinderRenderTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self._tmpdir.name)
        self.session = "vf-test"
        self.session_root = tmp / "sessions" / self.session
        self.images = self.session_root / "images"
        self.images.mkdir(parents=True)

        self._orig_sessions = engine.SESSIONS_DIR if hasattr(engine, "SESSIONS_DIR") else None
        self._orig_root = engine.ROOT
        # Point session helpers at the temp tree via _get_session_root.
        self._session_patch = patch.object(
            engine, "_get_session_root",
            side_effect=lambda sid="default": tmp / "sessions" / sid,
        )
        self._session_patch.start()

        self.source = self.images / "gameplay_still.png"
        self.source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake-still")

        state = {
            "current_image_url": f"/images/{self.source.name}",
            "time_of_day": "golden hour",
        }
        history = [{
            "image": str(self.source),
            "image_url": f"/images/{self.source.name}",
            "vision_dispatch": "Jason Fleece stands before the rusted tower",
            "vision_analysis": "Jason Fleece stands before the rusted tower, seen by the camera",
            "spatial_compass": "Ahead: rusted tower ~20ft. Ground: dust. Standing.",
            "setting_type": "outdoor-desert",
            "hard_transition": False,
        }]
        (self.session_root / "state.json").write_text(json.dumps(state), encoding="utf-8")
        (self.session_root / "history.json").write_text(json.dumps(history), encoding="utf-8")

        self._orig_prompts_path = ps.PROMPTS_PATH
        self._orig_defaults_path = ps.DEFAULTS_PATH
        self._orig_refs = gi.REFERENCES_DIR
        ps.PROMPTS_PATH = tmp / "simulation_prompts.json"
        ps.DEFAULTS_PATH = tmp / "simulation_prompts.defaults.json"
        gi.REFERENCES_DIR = tmp / "references"
        payload = {
            "world_initial_state": "Test.",
            "image_negative_prompt": "CGI, third person perspective",
            "gemini_text_to_image_instructions": "SCENE: {prompt}",
            "gemini_image_to_image_instructions": "SHOW: {prompt}",
        }
        payload.update(gi.default_spec())
        payload[gi.CHARACTER_KEY] = {
            "enabled": True,
            "name": "Jason Fleece",
            "role": "photojournalist",
            "appearance": "adult man",
            "wardrobe": "olive jacket",
            "reference_images": [],
        }
        payload[gi.CAMERA_KEY] = {"mode": "third_person", "show_hands": False}
        for path in (ps.PROMPTS_PATH, ps.DEFAULTS_PATH):
            path.write_text(json.dumps(payload), encoding="utf-8")
        ps.PROMPTS._mtime = None
        ps.PROMPTS._last_check = 0.0
        ps.PROMPTS._reload(force=True)

    def tearDown(self):
        self._session_patch.stop()
        ps.PROMPTS_PATH = self._orig_prompts_path
        ps.DEFAULTS_PATH = self._orig_defaults_path
        gi.REFERENCES_DIR = self._orig_refs
        ps.PROMPTS._mtime = None
        ps.PROMPTS._last_check = 0.0
        ps.PROMPTS._reload(force=True)
        self._tmpdir.cleanup()

    def _hist_len(self):
        return len(engine._load_history(self.session))

    def test_render_passes_a_viewfinder_spec_and_place_lock_history(self):
        captured = {}

        def fake_gen(*args, **kwargs):
            captured["kwargs"] = kwargs
            captured["args"] = args
            out = self.images / "generated.png"
            out.write_bytes(b"fp-plate")
            return (str(out), "prompt", None)

        with patch.object(engine, "_gen_image", side_effect=fake_gen):
            path = engine.render_viewfinder_image(self.session, source_path=str(self.source))

        self.assertTrue(path)
        spec = captured["kwargs"]["identity_spec"]
        self.assertTrue(gi.is_viewfinder_spec(spec))
        self.assertFalse(gi.shows_character(spec))
        self.assertEqual(captured["kwargs"]["frame_idx"], 1)
        refs = captured["kwargs"]["history_ref"]
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["image"], str(self.source))
        self.assertNotIn("Jason Fleece", refs[0]["vision_dispatch"])
        self.assertEqual(gi.camera_mode(), "third_person")
        self.assertEqual(self._hist_len(), 1)

    def test_leak_retry_drops_the_3p_pixel_reference(self):
        calls = []

        def fake_gen(*args, **kwargs):
            calls.append(kwargs)
            out = self.images / f"generated_{len(calls)}.png"
            out.write_bytes(b"fp-plate")
            return (str(out), "prompt", None)

        with patch.object(engine, "_gen_image", side_effect=fake_gen):
            first = engine.render_viewfinder_image(
                self.session, attach_source=True, source_path=str(self.source),
            )
            retry = engine.render_viewfinder_image(
                self.session, attach_source=False, source_path=str(self.source),
            )

        self.assertTrue(first and retry)
        self.assertEqual(calls[0]["frame_idx"], 1)
        self.assertEqual(len(calls[0]["history_ref"]), 1)
        self.assertEqual(calls[1]["frame_idx"], 0)
        self.assertEqual(calls[1]["history_ref"], [])

    def test_api_does_not_append_history_or_change_authored_mode(self):
        plate = self.images / "generated.png"
        plate.write_bytes(b"fp-plate")

        def fake_gen(*args, **kwargs):
            return (str(plate), "prompt", None)

        from flask import Flask
        app = Flask(__name__)
        with app.test_request_context(json={"session_id": self.session}):
            with patch.object(engine, "_gen_image", side_effect=fake_gen), \
                 patch.object(engine, "_viewfinder_leaked_character", return_value=False), \
                 patch.object(engine, "IMAGE_ENABLED", True), \
                 patch.object(engine, "LLM_ENABLED", True):
                resp = engine.api_viewfinder()

        payload = resp.get_json()
        self.assertIn("image_url", payload)
        self.assertFalse(payload.get("cached"))
        self.assertTrue(payload["image_url"].startswith("/images/viewfinder_"))
        self.assertEqual(gi.camera_mode(), "third_person")
        self.assertEqual(self._hist_len(), 1)

        cache_files = list(self.images.glob("viewfinder_*.png"))
        self.assertEqual(len(cache_files), 1)

        with app.test_request_context(json={"session_id": self.session}):
            with patch.object(engine, "_gen_image") as gen, \
                 patch.object(engine, "IMAGE_ENABLED", True), \
                 patch.object(engine, "LLM_ENABLED", True):
                cached = engine.api_viewfinder()
        self.assertTrue(cached.get_json().get("cached"))
        gen.assert_not_called()

    def test_leaked_plate_is_not_a_world_seed(self):
        first = self.images / "first.png"
        first.write_bytes(b"leaked")

        def fake_gen(*args, **kwargs):
            return (str(first), "p", None)

        from flask import Flask
        app = Flask(__name__)
        with app.test_request_context(json={"session_id": self.session}):
            with patch.object(engine, "_gen_image", side_effect=fake_gen), \
                 patch.object(engine, "_viewfinder_leaked_character", return_value=True), \
                 patch.object(engine, "IMAGE_ENABLED", True), \
                 patch.object(engine, "LLM_ENABLED", True):
                resp = engine.api_viewfinder()

        if isinstance(resp, tuple):
            resp, status = resp
        else:
            status = resp.status_code
        payload = resp.get_json()
        self.assertEqual(status, 502)
        self.assertEqual(payload.get("error"), "render_failed")
        self.assertEqual(list(self.images.glob("viewfinder_*.png")), [])
        self.assertNotIn("image_url", payload)

    def test_api_uses_uploaded_live_frame_without_mutating_world(self):
        import base64
        plate = self.images / "generated.png"
        plate.write_bytes(b"fp-plate")
        captured = {}

        def fake_gen(*args, **kwargs):
            captured["kwargs"] = kwargs
            return (str(plate), "prompt", None)

        frame = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8" + b"x" * 600).decode("ascii")
        still_url = f"/images/{self.source.name}"
        from flask import Flask
        app = Flask(__name__)
        with app.test_request_context(json={"session_id": self.session, "frame": frame}):
            with patch.object(engine, "_gen_image", side_effect=fake_gen), \
                 patch.object(engine, "_viewfinder_leaked_character", return_value=False), \
                 patch.object(engine, "IMAGE_ENABLED", True), \
                 patch.object(engine, "LLM_ENABLED", True):
                resp = engine.api_viewfinder()

        payload = resp.get_json()
        self.assertIn("image_url", payload)
        self.assertTrue(payload.get("from_live"))
        refs = captured["kwargs"]["history_ref"]
        self.assertEqual(len(refs), 1)
        self.assertTrue(Path(refs[0]["image"]).name.startswith("viewfinder_live_"))
        self.assertTrue(refs[0]["live_capture"])
        self.assertEqual(captured["kwargs"]["frame_idx"], 1)
        self.assertEqual(self._hist_len(), 1)
        hist = engine._load_history(self.session)
        self.assertEqual(Path(hist[0]["image"]).name, self.source.name)
        self.assertEqual((engine.get_state(self.session) or {}).get("current_image_url"), still_url)
        self.assertEqual(gi.camera_mode(), "third_person")

    def test_api_returns_first_person_live_prompt_and_camera(self):
        plate = self.images / "generated.png"
        plate.write_bytes(b"fp-plate")

        def fake_gen(*args, **kwargs):
            return (str(plate), "prompt", None)

        from flask import Flask
        app = Flask(__name__)
        with app.test_request_context(json={"session_id": self.session}):
            with patch.object(engine, "_gen_image", side_effect=fake_gen), \
                 patch.object(engine, "_viewfinder_leaked_character", return_value=False), \
                 patch.object(engine, "IMAGE_ENABLED", True), \
                 patch.object(engine, "LLM_ENABLED", True):
                resp = engine.api_viewfinder()

        payload = resp.get_json()
        self.assertTrue(payload.get("prompt"))
        self.assertIn("own eyes", payload["prompt"].lower())
        self.assertIn("EMPTY FOREGROUND", payload["prompt"])
        self.assertIn("Empty air between the lens", payload["prompt"])
        self.assertEqual((payload.get("camera") or {}).get("look") or "", "")
        self.assertNotIn("Jason Fleece", payload["prompt"])
        self.assertNotRegex(payload["prompt"], r"stays in frame")
        cam = payload.get("camera") or {}
        self.assertEqual(cam.get("mode"), "first_person")
        self.assertFalse(cam.get("shows_character"))
        self.assertEqual(cam.get("perspective"), "first_person")
        self.assertEqual(gi.camera_mode(), "third_person")

        with app.test_request_context(json={"session_id": self.session}):
            with patch.object(engine, "IMAGE_ENABLED", True), \
                 patch.object(engine, "LLM_ENABLED", True):
                cached = engine.api_viewfinder()
        hit = cached.get_json()
        self.assertTrue(hit.get("cached"))
        self.assertTrue(hit.get("prompt"))
        self.assertEqual((hit.get("camera") or {}).get("mode"), "first_person")

    def test_cache_key_changes_when_the_live_view_changes(self):
        a = self.images / "live_a.jpg"
        b = self.images / "live_b.jpg"
        a.write_bytes(b"place-one-" + b"a" * 40)
        b.write_bytes(b"place-two-" + b"b" * 40)
        key_a = engine._viewfinder_cache_key(str(a), "Ahead: tower")
        key_b = engine._viewfinder_cache_key(str(b), "Ahead: tower")
        key_walked = engine._viewfinder_cache_key(str(a), "Ahead: warehouse")
        self.assertNotEqual(key_a, key_b)
        self.assertNotEqual(key_a, key_walked)
        self.assertEqual(key_a, engine._viewfinder_cache_key(str(a), "Ahead: tower"))

    def test_normalize_keeps_player_self_only_when_asked(self):
        parsed = [{
            "label": "Jason Fleece",
            "box_2d": [200, 300, 850, 720],
            "kind": "person",
        }]
        self.assertEqual(engine._normalize_detections(parsed), [])
        kept = engine._normalize_detections(parsed, include_self=True)
        self.assertEqual(len(kept), 1)
        self.assertTrue(engine._is_player_self_label(kept[0]["label"]))

    def test_viewfinder_leak_sees_named_hero_and_large_person(self):
        with patch.object(engine, "_detect_objects", return_value=[{
            "label": "jason fleece", "kind": "person",
            "cx": 0.5, "cy": 0.55, "w": 0.28, "h": 0.7,
        }]):
            self.assertTrue(engine._viewfinder_leaked_character(str(self.source)))
            self.assertTrue(engine._viewfinder_leaked_character(
                str(self.source), allow_other_people=True))
        with patch.object(engine, "_detect_objects", return_value=[{
            "label": "person", "kind": "person",
            "cx": 0.5, "cy": 0.5, "w": 0.35, "h": 0.6,
        }]):
            self.assertTrue(engine._viewfinder_leaked_character(str(self.source)))
            self.assertFalse(engine._viewfinder_leaked_character(
                str(self.source), allow_other_people=True),
                "an NPC in frame must not refuse the viewfinder")
        with patch.object(engine, "_detect_objects", return_value=[{
            "label": "valve", "kind": "object",
            "cx": 0.2, "cy": 0.4, "w": 0.1, "h": 0.1,
        }]):
            self.assertFalse(engine._viewfinder_leaked_character(str(self.source)))

    def test_build_image_prompt_viewfinder_branch(self):
        vf = gi.viewfinder_spec()
        prompt = engine.build_image_prompt(
            player_choice="Raise camera",
            dispatch="A rusted tower ahead on dusty ground.",
            prev_spatial="Ahead: rusted tower ~20ft.",
            spec=vf,
        )
        self.assertIn("VIEWFINDER RESTAGE", prompt)
        self.assertIn("FIRST-PERSON", prompt)
        self.assertIn("EMPTY FOREGROUND", prompt)
        self.assertNotIn("Jason Fleece", prompt)
        self.assertNotIn("TIMEOUT PENALTY", prompt)
        self.assertNotIn("follow camera walked", prompt)
        self.assertNotIn("LOCATION PLATE", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
