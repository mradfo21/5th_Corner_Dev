"""
test_local_vision_e2e.py — Playwright end-to-end proof that the SCAN tool works
with NO API KEY AT ALL, driven by the on-device detector (local_vision.py).

Unlike test_realtime_e2e.py, nothing here is mocked: the browser captures a real
frame, posts it to a real /api/detect, and MediaPipe plus the scene prompt answer
it. That end-to-end path is the whole point of the local backend — before it,
/api/detect returned [] without GEMINI_API_KEY and SCAN was dead in local dev —
so it is worth one test that would fail if any link in it broke.

The server runs `run_local.py --mock` (fully offline, keys blanked) with
SCENE_RENDERER=image so there is a still frame on screen to scan.

Requirements (see requirements-dev.txt):
    pip install -r requirements-dev.txt
    playwright install chromium

Run with:
    python3 -m unittest test_local_vision_e2e -v
"""

import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.resolve()

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

import local_vision

# The frame the browser will scan, and the prompt a real turn would have written
# alongside it. The prompt is the local detector's vocabulary, so the two have to
# describe the same place.
FRAME_URL = "/static/img/scene_exterior.png"
SCENE_PROMPT = (
    "You are at the chain-link perimeter fence of the processing plant, "
    "flashlight raised. Ahead, rusted silos rise against a bruised sky and a "
    "floodlight burns over the loading dock. An abandoned armored personnel "
    "carrier sits to your left, hatch open. To the right a warning sign hangs "
    "from the gate. In the distance a water tower breaks the horizon."
)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(base_url: str, timeout_s: float = 40.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=2) as resp:
                if resp.status == 200:
                    return json.load(resp)
        except Exception:
            pass
        time.sleep(0.4)
    return None


@unittest.skipUnless(PLAYWRIGHT_AVAILABLE, "playwright not installed — see requirements-dev.txt")
@unittest.skipUnless(local_vision.available(),
                     "mediapipe / the .tflite model is not installed here")
class TestLocalScanE2E(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.port = _find_free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"

        env = os.environ.copy()
        # No keys of any kind: if SCAN produces tags anyway, they came from the
        # on-device detector and nothing else.
        env["GEMINI_API_KEY"] = ""
        env["OPENAI_API_KEY"] = ""
        env["ANTHROPIC_API_KEY"] = ""
        env["DETECT_BACKEND"] = "local"
        # Stills renderer: the reactor video has nothing to connect to offline,
        # and SCAN needs something on screen to capture.
        env["SCENE_RENDERER"] = "image"

        # DEVNULL rather than PIPE: the mock server logs per request, and an
        # unread pipe fills its OS buffer and deadlocks the server.
        cls.server_proc = subprocess.Popen(
            [sys.executable, "run_local.py", "--mock", "--no-browser", "--port", str(cls.port)],
            cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        cls.health = _wait_for_health(cls.base_url)
        if not cls.health:
            cls.server_proc.terminate()
            raise RuntimeError(f"Server on {cls.base_url} did not become healthy in time")

        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close()
            cls.playwright.stop()
        finally:
            cls.server_proc.terminate()
            try:
                cls.server_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.server_proc.kill()

    def test_health_reports_the_local_backend(self):
        detect = self.health.get("detect") or {}
        self.assertEqual(detect.get("backend"), "local")
        self.assertTrue((detect.get("local") or {}).get("available"),
                        f"local detector should be loaded: {detect}")

    def test_health_reports_which_build_is_serving(self):
        """"Did my push deploy?" must be answerable without reading behaviour.

        The deploy dashboard's event list is easy to read stale, which makes a
        live deploy look like a missing one. uptime_s is the tell either way: a
        push restarts the worker, so an uptime older than the push means the new
        commit is not live.
        """
        build = self.health.get("build") or {}
        self.assertIn("commit", build)
        self.assertIn("branch", build)
        self.assertIsInstance(build.get("uptime_s"), (int, float))
        self.assertGreaterEqual(build["uptime_s"], 0.0)

    def test_scan_tags_render_from_on_device_detections(self):
        page = self.browser.new_page(viewport={"width": 1280, "height": 720})
        detect_responses = []
        page.on("response", lambda r: (
            detect_responses.append(r.json())
            if "/api/detect" in r.url and r.status == 200 else None))

        # Skip first-run onboarding; ?talkdev exposes the client's own QA hooks.
        page.add_init_script(
            "try { localStorage.setItem('scan_tutorial_seen_v1','1'); } catch(e){}")
        page.goto(f"{self.base_url}/standalone?talkdev")
        page.keyboard.press("r")
        page.wait_for_selector(".choice-btn", state="attached", timeout=25000)

        # The mock backend renders no images, so give the session the scene prompt
        # a real turn would have written — it is the local detector's vocabulary.
        # State is read from disk per request, so the running server picks this up
        # on the next /api/detect. (Only scalars are seeded this way. Injecting a
        # feed item to get the frame PAINTED is not reliable from outside the
        # server process: the client only renders feed items newer than the last
        # id it saw, and ids come from a per-process counter. The frame does not
        # need to be painted for this test — forceStill() hands the scan pass a
        # decoded still directly, which is the input path under test.)
        import engine
        state = engine.get_state("default")
        state["current_image_prompt"] = SCENE_PROMPT
        engine._save_state(state, "default")

        # Prime the still and fire the scan the player would fire.
        self.assertTrue(page.evaluate(f"window.__SCAN__.forceStill('{FRAME_URL}')"),
                        "the test frame failed to decode in the browser")
        page.wait_for_selector(".scan-tag", timeout=25000)

        self.assertTrue(detect_responses, "the client never called /api/detect")
        objects = detect_responses[-1].get("objects") or []
        self.assertTrue(objects, "the on-device detector returned no objects")

        labels = {o["label"] for o in objects}
        # The prompt supplies the open vocabulary COCO's 80 classes cannot: these
        # labels are only reachable by reading the scene prompt.
        self.assertTrue(
            labels & {"rusted silos", "abandoned armored personnel carrier",
                      "processing plant", "loading dock", "water tower"},
            f"expected story-grounded labels from the scene prompt, got {labels}")

        # The player's own flashlight hand is the most confident COCO "person" in
        # this frame. It must never be offered as someone to talk to.
        self.assertEqual([o["label"] for o in objects if o.get("speaks")], [],
                         "the operator's own hand must not become a talkable subject")

        tags = page.eval_on_selector_all(".scan-tag", "els => els.length")
        self.assertGreater(tags, 0, "detections did not become SCAN tags")
        page.close()


if __name__ == "__main__":
    unittest.main()
