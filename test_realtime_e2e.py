"""
test_realtime_e2e.py — Playwright end-to-end test for the REALTIME (Reactor /
LingBot World 2) renderer path, which the normal standalone e2e can't cover
(mock mode disables image generation, and the real renderer needs an external
WebRTC world-model service).

Strategy — run the REAL client JS against a MOCK Reactor SDK:
  * Boot `run_local.py --mock` with REACTOR_API_KEY set + SCENE_RENDERER=reactor
    so /api/reactor/config advertises the realtime renderer as enabled.
  * Intercept the SDK CDN import (esm.sh) and serve a mock ES module that
    simulates the world-model lifecycle (connect -> ready, set_image ->
    image_accepted, start -> a real canvas.captureStream() video track so the
    <video> gets decoded frames and videoWidth > 0).
  * Intercept POST /api/reactor/token -> a fake jwt (no real network).
  * Load /realtime, drive a scene through window.ReactorRenderer.applyScene(),
    and assert the live video actually reveals (ReactorRenderer.isShowing()).

If the realtime state machine is broken (never starts / never reveals), this
test fails and prints the captured [reactor] console log so the stall point is
obvious.

Run with:
    python3 -m unittest test_realtime_e2e -v
"""

import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.resolve()

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


# A 1x1 transparent PNG as a data URL — uploadStill() fetches the scene image,
# and fetch() works fine against a data: URL in the browser, so no image route
# is needed.
TINY_PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


# Mock Reactor SDK, served in place of the pinned esm.sh module. Implements just
# the surface reactor_renderer.js uses. On `start` it emits a REAL video track
# (canvas.captureStream) so the <video> decodes frames and reveals exactly like
# production — the single hand-off the renderer waits on.
MOCK_SDK_JS = r"""
export class Reactor {
  constructor(opts) { this._h = {}; this._opts = opts || {}; this._timer = null; }
  on(evt, fn) { (this._h[evt] = this._h[evt] || []).push(fn); }
  _emit(evt) {
    const args = Array.prototype.slice.call(arguments, 1);
    (this._h[evt] || []).forEach((fn) => { try { fn.apply(null, args); } catch (e) { console.error("mock handler", evt, e); } });
  }
  async connect(jwt) {
    this._jwt = jwt;
    window.__MOCK_REACTOR_CONNECTED__ = true;
    // Simulate a TRANSIENT connect error on the first attempt only (mirrors a
    // flaky first WebRTC/session attempt on mobile). The client should retry
    // and recover rather than sticking on "Realtime unavailable".
    if (window.__MOCK_ERROR_ONCE__ && !window.__MOCK_ERRORED__) {
      window.__MOCK_ERRORED__ = true;
      setTimeout(() => this._emit("error", { recoverable: false, message: "transient mock error" }), 20);
      return true;
    }
    setTimeout(() => this._emit("statusChanged", "ready"), 20);
    return true;
  }
  async uploadFile(file) {
    window.__MOCK_UPLOADS__ = (window.__MOCK_UPLOADS__ || 0) + 1;
    return { id: "file_" + Math.random().toString(36).slice(2) };
  }
  async sendCommand(name, data) {
    window.__MOCK_CMDS__ = window.__MOCK_CMDS__ || [];
    window.__MOCK_CMDS__.push(name);
    if (name === "set_image") {
      setTimeout(() => this._emit("message", { type: "image_accepted", data: {} }), 10);
    } else if (name === "set_prompt") {
      setTimeout(() => this._emit("message", { type: "prompt_accepted", data: {} }), 10);
    } else if (name === "start") {
      setTimeout(() => this._emit("message", { type: "generation_started", data: {} }), 10);
      // Simulate a stalled model: accept every command but never produce a
      // video track (the real "guide image shows but realtime won't start").
      if (!window.__MOCK_NO_VIDEO__) setTimeout(() => this._startVideo(), 30);
    } else if (name === "reset") {
      if (this._timer) { clearInterval(this._timer); this._timer = null; }
      setTimeout(() => this._emit("message", { type: "generation_reset", data: {} }), 10);
    }
    return true;
  }
  _startVideo() {
    try {
      const canvas = document.createElement("canvas");
      canvas.width = 320; canvas.height = 240;
      const ctx = canvas.getContext("2d");
      let hue = 0;
      const draw = () => { hue = (hue + 11) % 360; ctx.fillStyle = "hsl(" + hue + ",70%,50%)"; ctx.fillRect(0, 0, 320, 240); };
      draw();
      if (this._timer) clearInterval(this._timer);
      this._timer = setInterval(draw, 66); // keep presenting frames
      const stream = canvas.captureStream(15);
      const track = stream.getVideoTracks()[0];
      this._emit("trackReceived", "main_video", track, stream);
    } catch (e) { console.error("mock _startVideo failed", e); }
  }
  async disconnect() { if (this._timer) { clearInterval(this._timer); this._timer = null; } }
}
export default { Reactor };
"""


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(base_url: str, timeout_s: float = 25.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


@unittest.skipUnless(PLAYWRIGHT_AVAILABLE, "playwright not installed — see requirements-dev.txt")
class TestRealtimeRenderer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _find_free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"

        env = os.environ.copy()
        env["GEMINI_API_KEY"] = ""
        env["OPENAI_API_KEY"] = ""
        env["ANTHROPIC_API_KEY"] = ""
        # Advertise realtime as enabled + default renderer so /realtime forces it.
        env["REACTOR_API_KEY"] = "test-key-not-used"
        env["SCENE_RENDERER"] = "reactor"

        # Discard the server's stdout/stderr rather than piping it: an unread
        # PIPE fills its OS buffer and can deadlock the mock server under load.
        # Nothing here reads the server's output, so DEVNULL is the safe sink.
        cls.server_proc = subprocess.Popen(
            [sys.executable, "run_local.py", "--mock", "--no-browser", "--port", str(cls.port)],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not _wait_for_health(cls.base_url):
            cls.server_proc.terminate()
            raise RuntimeError(f"Server on {cls.base_url} did not become healthy in time")

        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            headless=True,
            args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"],
        )

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

    def _new_realtime_page(self):
        page = self.browser.new_page()
        self._logs = []
        page.on("console", lambda m: self._logs.append(f"{m.type}: {m.text}"))
        page.on("pageerror", lambda e: self._logs.append(f"PAGEERROR: {e}"))

        # Serve the mock SDK in place of the pinned CDN module.
        page.route(
            "https://esm.sh/**",
            lambda route: route.fulfill(
                status=200,
                content_type="application/javascript",
                body=MOCK_SDK_JS,
            ),
        )
        # Fake the token exchange (no real Reactor network).
        page.route(
            "**/api/reactor/token",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"jwt": "mock.jwt.token", "expires_at": 9999999999}',
            ),
        )
        return page

    def _dump_logs(self):
        return "\n".join(self._logs[-60:])

    def test_realtime_video_reveals_on_scene(self):
        """The core contract: enable realtime, apply a scene (guide image +
        prompt), and the live video must actually show (isShowing() -> true)."""
        page = self._new_realtime_page()
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            # Realtime renderer should exist and be forced on.
            page.wait_for_function("window.ReactorRenderer !== undefined", timeout=10000)

            # Wait until the mock SDK has connected and the renderer is ready.
            page.wait_for_function("window.__MOCK_REACTOR_CONNECTED__ === true", timeout=10000)
            page.wait_for_function("window.ReactorRenderer.isReady() === true", timeout=10000)

            # Drive a guide-image scene through the realtime facade.
            page.evaluate(
                """(img) => window.ReactorRenderer.applyScene({
                    prompt: 'First-person VHS. A dark drainage pipe interior. Motion: you crawl forward.',
                    imageUrl: img,
                    hardTransition: false,
                })""",
                TINY_PNG_DATA_URL,
            )

            # The single hand-off: the live video reveals with decoded frames.
            page.wait_for_function("window.ReactorRenderer.isShowing() === true", timeout=15000)
            self.assertTrue(page.evaluate("window.ReactorRenderer.getStatus()") == "live")

            cmds = page.evaluate("window.__MOCK_CMDS__ || []")
            self.assertIn("set_image", cmds, f"set_image never sent. logs:\n{self._dump_logs()}")
            self.assertIn("start", cmds, f"start never sent. logs:\n{self._dump_logs()}")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (video_reveals) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_realtime_reanchor_on_new_guide_image(self):
        """After the first scene is live, a NEW guide image must re-anchor
        (reset -> re-establish) and reveal the video again — the flow the recent
        transition fixes touch."""
        page = self._new_realtime_page()
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isReady() === true", timeout=15000)

            page.evaluate(
                """(img) => window.ReactorRenderer.applyScene({prompt: 'scene one', imageUrl: img, hardTransition: false})""",
                TINY_PNG_DATA_URL,
            )
            page.wait_for_function("window.ReactorRenderer.isShowing() === true", timeout=15000)

            # A second, DIFFERENT guide image (distinct data URL) -> re-anchor.
            other_png = TINY_PNG_DATA_URL.replace("iVBOR", "iVBOR")  # same bytes; use a query-like suffix
            page.evaluate(
                """() => window.ReactorRenderer.applyScene({prompt: 'scene two', imageUrl: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==', hardTransition: false})"""
            )
            # Reset must be issued for the re-anchor, then the video reveals again.
            page.wait_for_function("(window.__MOCK_CMDS__||[]).filter(c=>c==='reset').length >= 1", timeout=10000)
            page.wait_for_function("window.ReactorRenderer.isShowing() === true", timeout=15000)
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (reanchor) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_stalled_model_surfaces_diagnostic(self):
        """Reproduce 'guide image shows but realtime won't start': the model
        accepts every command but never emits a video track. The reveal
        watchdog must fire (video_stalled) so the stall is visible/reportable,
        the video must not be showing, and status must never claim 'live'."""
        page = self._new_realtime_page()
        # Suppress the mock video track + shorten the watchdog so the test is fast.
        page.add_init_script("window.__MOCK_NO_VIDEO__ = true; window.__REACTOR_REVEAL_WATCHDOG_MS__ = 1500;")
        events = []
        page.expose_function("__record_evt", lambda name: events.append(name))
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isReady() === true", timeout=15000)
            # Tap into lifecycle events so we can assert the stall is surfaced.
            page.evaluate(
                """() => {
                    const prev = window.ReactorRenderer.onEvent;
                    window.ReactorRenderer.onEvent = (name, data) => {
                        try { window.__record_evt(name); } catch (_) {}
                        if (typeof prev === 'function') prev(name, data);
                    };
                }"""
            )
            page.evaluate(
                "(img) => window.ReactorRenderer.applyScene({prompt: 'stalled scene', imageUrl: img, hardTransition: false})",
                TINY_PNG_DATA_URL,
            )
            # start is issued...
            page.wait_for_function("(window.__MOCK_CMDS__||[]).includes('start')", timeout=10000)
            # ...but no frames, so the watchdog must declare it stalled.
            page.wait_for_function("window.ReactorRenderer.isShowing() === false", timeout=5000)
            page.wait_for_timeout(2500)  # let the 1.5s watchdog fire
            self.assertIn("video_stalled", events, f"watchdog never fired. logs:\n{self._dump_logs()}")
            self.assertFalse(page.evaluate("window.ReactorRenderer.isShowing()"))
            self.assertNotEqual(page.evaluate("window.ReactorRenderer.getStatus()"), "live")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (stalled) ===\n" + self._dump_logs())
            print("events:", events)
            raise
        finally:
            page.close()

    def test_realtime_autoplay_waits_for_video_and_does_not_storm(self):
        """Realtime auto-play must advance off the LIVE video (after it shows +
        a watch window), not off the scene_image feed item — and must not fire a
        storm of /api/choose that stacks re-anchors and blacks out the stream."""
        page = self._new_realtime_page()
        page.add_init_script("window.__AUTOPLAY_WATCH_MS__ = 800;")  # short watch for the test

        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "scene one", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}, {"text": "Wait"}]},
        ]
        chooses = []
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        # /api/choose accepts the advance but returns NO new prompt, so auto-play
        # has nothing new to advance to — it must NOT keep firing.
        def choose_handler(route):
            chooses.append(route.request.url)
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps([{"id": 100 + len(chooses), "type": "player_action", "content": "you go"}]))
        page.route("**/api/choose", choose_handler)

        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isShowing() === true", timeout=15000)
            # Turn on auto-play.
            page.evaluate("() => document.getElementById('autoplay-btn').click()")
            # It should advance ONCE after the (short) watch window…
            waited = 0
            while len(chooses) < 1 and waited < 8000:
                page.wait_for_timeout(200); waited += 200
            self.assertGreaterEqual(len(chooses), 1, f"auto-play never advanced. logs:\n{self._dump_logs()}")
            # …and then STOP (no new prompt arrived), rather than storming.
            page.wait_for_timeout(2500)
            self.assertLessEqual(len(chooses), 1, f"auto-play stormed /api/choose ({len(chooses)}x) — would stack re-anchors")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (autoplay) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_world_model_inspector_logs_prompts_and_events(self):
        """The right-side world-model inspector must sequentially log what we SEND
        (set_prompt with the prompt text) and what the model REPORTS (accepted /
        generation started / video live), so the black box is inspectable."""
        page = self._new_realtime_page()
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isReady() === true", timeout=15000)
            page.evaluate(
                "(img) => window.ReactorRenderer.applyScene({prompt: 'A dark drainage pipe, vein-like growth ahead', imageUrl: img, hardTransition: false})",
                TINY_PNG_DATA_URL,
            )
            page.wait_for_function("window.ReactorRenderer.isShowing() === true", timeout=15000)
            # The world-model log/selector now starts COLLAPSED behind the MODEL
            # button — open it (via the menu) before asserting it's visible.
            page.click("#menu-toggle")
            page.click("#btn-model")
            # The log panel exists and is shown once opened in realtime mode.
            self.assertNotEqual(page.evaluate("getComputedStyle(document.getElementById('rt-log')).display"), "none")
            # Wait for log entries to accumulate.
            page.wait_for_function("document.querySelectorAll('#rt-log-list .rt-e').length >= 3", timeout=8000)
            text = page.evaluate("document.getElementById('rt-log-list').innerText")
            self.assertIn("set_prompt", text, f"inspector didn't log the prompt we sent. log:\n{text}")
            # It logged the actual prompt text we injected.
            self.assertIn("drainage pipe", text, f"inspector didn't show the injected prompt text. log:\n{text}")
            # And a model lifecycle signal (accepted / generation / live).
            self.assertTrue(
                any(k in text for k in ("accepted", "generation started", "video live")),
                f"inspector didn't log any model lifecycle event. log:\n{text}",
            )
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (inspector) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_camera_tap_captures_evidence(self):
        """The camera (SNAP) tool: armed from the hub, a single-finger TAP
        (press + release) on the scene captures a photo of that spot as
        'evidence' — files it (POST /api/investigate kind=photo), prints the
        scoring RECEIPT (which reveals appraised items and lights the EVIDENCE
        HUD), and adds it to the case file — WITHOUT resolving a turn (no
        /api/choose) or steering the stream (no set_prompt). Pointer-driven so
        it works on touch/iOS; the shot fires on release so a pinch never fires
        a stray capture."""
        page = self._new_realtime_page()
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "A loading dock.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        chooses = []
        investigates = []
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        page.route("**/api/choose", lambda r: (chooses.append(r.request.url), r.fulfill(status=200, content_type="application/json", body="[]")))

        def inv_handler(route):
            investigates.append(route.request.post_data)
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True, "id": 7, "image_url": "/images/e.jpg", "kind": "photo"}))
        page.route("**/api/investigate", inv_handler)

        # Mock the appraisal so the receipt reveal + score are deterministic.
        def photo_handler(route):
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "items": [
                    {"label": "rusted valve", "interest": 4, "note": "recently turned"},
                    {"label": "wet boot print", "interest": 3, "note": "leads left"},
                ],
                "caption": "A tight frame on the dripping valve.",
                "mood": "ominous",
            }))
        page.route("**/api/photo", photo_handler)
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isShowing() === true", timeout=15000)
            # The camera hub is shown; the reticle is a CAMERA (not a hand/glass).
            self.assertNotEqual(page.evaluate("getComputedStyle(document.getElementById('realtime-btn')).display"), "none")
            self.assertTrue(page.evaluate("!!document.querySelector('#touch-reticle .touch-cam')"))
            self.assertIsNone(page.evaluate("document.querySelector('#touch-reticle .touch-hand')"))
            # Arm it, then TAP a spot via press+release pointer events (touch/iOS).
            page.evaluate("document.getElementById('realtime-btn').click()")
            self.assertTrue(page.evaluate("document.getElementById('realtime-btn').classList.contains('aiming')"))
            page.evaluate("window.__MOCK_CMDS__ = []")
            page.evaluate(
                """() => {
                    const L = document.getElementById('touch-layer');
                    const o = {clientX: 240, clientY: 200, pointerId: 1, cancelable: true, bubbles: true};
                    L.dispatchEvent(new PointerEvent('pointerdown', o));
                    L.dispatchEvent(new PointerEvent('pointerup', o));
                }"""
            )
            # It files a photo specimen...
            for _ in range(60):
                if investigates:
                    break
                page.wait_for_timeout(100)
            self.assertGreaterEqual(len(investigates), 1, f"a tap must capture a photo. logs:\n{self._dump_logs()}")
            payload = json.loads(investigates[0] or "{}")
            self.assertEqual(payload.get("kind"), "photo")
            self.assertTrue((payload.get("texture") or "").startswith("data:image"))
            # ...prints the receipt and reveals the appraised items...
            page.wait_for_function("document.getElementById('photo-receipt').classList.contains('show')", timeout=6000)
            page.wait_for_function("document.querySelectorAll('#photo-receipt .receipt-item').length >= 1", timeout=8000)
            # ...lights the EVIDENCE score HUD with points on the board...
            page.wait_for_function(
                "!document.getElementById('evidence-hud').classList.contains('hidden')", timeout=6000)
            page.wait_for_function(
                "Number(document.querySelector('#evidence-hud .ev-total').getAttribute('data-val')) > 0", timeout=8000)
            # ...adds it to the case file...
            page.wait_for_function("document.querySelectorAll('#investigations-strip .inv-thumb').length >= 1", timeout=8000)
            # ...and does NOT steer or resolve a turn.
            cmds = page.evaluate("window.__MOCK_CMDS__ || []")
            self.assertNotIn("set_prompt", cmds, "camera capture must not steer the stream")
            self.assertEqual(len(chooses), 0, "camera capture must not resolve a turn")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (camera) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_camera_zoom_scales_scene_and_suppresses_pinch_capture(self):
        """Optical zoom: while the camera is armed, the mouse wheel magnifies the
        scene (a CSS transform on the video layer) within bounds, the readout
        tracks it, the magnification RE-ANCHORS to the reticle as it moves (so
        the zoomed view follows the aim, FPS-scope style), and a two-finger
        PINCH zooms WITHOUT firing a capture (no /api/investigate) — only a
        clean single-finger tap shoots."""
        page = self._new_realtime_page()
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "A loading dock.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        investigates = []
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        page.route("**/api/photo", lambda r: r.fulfill(status=200, content_type="application/json", body='{"items":[]}'))
        page.route("**/api/investigate", lambda r: (investigates.append(r.request.post_data),
                   r.fulfill(status=200, content_type="application/json", body='{"ok":true,"id":1,"kind":"photo"}')))
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isShowing() === true", timeout=15000)
            # Arm the camera -> a gentle push-in scales the scene right away.
            page.evaluate("document.getElementById('realtime-btn').click()")
            page.wait_for_function("(document.getElementById('reactor-video').style.transform || '').includes('scale(')", timeout=4000)
            z_armed = page.evaluate("parseFloat(document.getElementById('touch-zoom').textContent)")
            self.assertGreater(z_armed, 1.0, "arming should push gently into the scene")

            # Wheel up (deltaY < 0) zooms IN: the readout climbs and the layer scales up.
            page.evaluate("""() => document.getElementById('touch-layer').dispatchEvent(
                new WheelEvent('wheel', {deltaY: -600, cancelable: true, bubbles: true}))""")
            z_in = page.evaluate("parseFloat(document.getElementById('touch-zoom').textContent)")
            self.assertGreater(z_in, z_armed, "wheel up must zoom in")
            transform_a = page.evaluate("document.getElementById('reactor-video').style.transform")
            self.assertIn("scale(", transform_a)
            # The zoom is anchored to the reticle (translate + scale), so moving the
            # aim point re-anchors the magnified view (it follows the cursor).
            page.evaluate("""() => document.getElementById('touch-layer').dispatchEvent(
                new PointerEvent('pointermove', {pointerId: 5, clientX: 120, clientY: 130, cancelable:true, bubbles:true}))""")
            transform_b = page.evaluate("document.getElementById('reactor-video').style.transform")
            self.assertIn("translate(", transform_b)
            self.assertNotEqual(transform_a, transform_b, "zoom must re-anchor to follow the reticle")

            # A big wheel-down clamps back to the wide bound (1.0x, transform cleared).
            page.evaluate("""() => document.getElementById('touch-layer').dispatchEvent(
                new WheelEvent('wheel', {deltaY: 6000, cancelable: true, bubbles: true}))""")
            z_min = page.evaluate("parseFloat(document.getElementById('touch-zoom').textContent)")
            self.assertAlmostEqual(z_min, 1.0, places=1)

            # A two-finger PINCH must NOT capture (only single-finger release does).
            page.evaluate("""() => {
                const L = document.getElementById('touch-layer');
                L.dispatchEvent(new PointerEvent('pointerdown', {pointerId: 11, clientX: 200, clientY: 200, cancelable:true, bubbles:true}));
                L.dispatchEvent(new PointerEvent('pointerdown', {pointerId: 12, clientX: 300, clientY: 200, cancelable:true, bubbles:true}));
                L.dispatchEvent(new PointerEvent('pointermove', {pointerId: 12, clientX: 380, clientY: 200, cancelable:true, bubbles:true}));
                L.dispatchEvent(new PointerEvent('pointerup', {pointerId: 12, clientX: 380, clientY: 200, cancelable:true, bubbles:true}));
                L.dispatchEvent(new PointerEvent('pointerup', {pointerId: 11, clientX: 200, clientY: 200, cancelable:true, bubbles:true}));
            }""")
            page.wait_for_timeout(400)
            self.assertEqual(len(investigates), 0, "a pinch gesture must not fire a capture")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (camera-zoom) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_photography_win_condition_closes_the_case(self):
        """The dossier census is the win condition: documenting the target number
        of DISTINCT subjects closes the case and shows the CASE CLOSED win
        overlay with a rank grade. The score also fills the case-file bar."""
        page = self._new_realtime_page()
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "A loading dock.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        page.route("**/api/investigate", lambda r: r.fulfill(status=200, content_type="application/json", body='{"ok":true,"id":1,"kind":"photo"}'))
        # One shot that documents 8 distinct subjects — enough to close the case.
        subjects = ["figure", "valve", "brush pile", "structure", "lantern",
                    "crate", "wire", "boot print"]
        page.route("**/api/photo", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({
            "items": [{"label": s, "interest": 4, "note": "clue"} for s in subjects],
            "caption": "A dense, telling frame.", "mood": "ominous"})))
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isShowing() === true", timeout=15000)
            # Arm the camera: the dossier HUD (with the case goal) is revealed.
            page.evaluate("document.getElementById('realtime-btn').click()")
            page.wait_for_function("!document.getElementById('evidence-hud').classList.contains('hidden')", timeout=5000)
            self.assertIn("/8", page.evaluate("document.querySelector('#evidence-hud .ev-case-count').textContent"))
            # Take the shot (press + release).
            page.evaluate("""() => {
                const L = document.getElementById('touch-layer');
                const o = {clientX: 640, clientY: 360, pointerId: 1, cancelable: true, bubbles: true};
                L.dispatchEvent(new PointerEvent('pointerdown', o));
                L.dispatchEvent(new PointerEvent('pointerup', o));
            }""")
            # The case closes: the win overlay appears with a rank grade.
            page.wait_for_function("!document.getElementById('case-overlay').classList.contains('hidden')", timeout=15000)
            rank = page.evaluate("document.getElementById('case-rank-letter').textContent")
            self.assertIn(rank, ["D", "C", "B", "A", "S"], f"a rank grade must be shown, got {rank!r}")
            self.assertEqual(page.evaluate("document.getElementById('case-subjects').textContent"), "8")
            # Starting a NEW CASE clears the win overlay and resets the census.
            page.evaluate("document.getElementById('case-restart').click()")
            page.wait_for_function("document.getElementById('case-overlay').classList.contains('hidden')", timeout=8000)
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (win) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_camera_exits_via_button_rightclick_and_esc(self):
        """Exiting the camera must be simple and forgiving: clicking the PHOTO
        button again toggles it OFF (the controls sit above the capture overlay
        so the click isn't swallowed), right-clicking the scene exits, and Esc
        exits — all without leaving the aiming overlay stuck open."""
        page = self._new_realtime_page()
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "A loading dock.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isShowing() === true", timeout=15000)

            def arm():
                page.evaluate("document.getElementById('realtime-btn').click()")
                self.assertTrue(page.evaluate("document.getElementById('realtime-btn').classList.contains('aiming')"))
                self.assertFalse(page.evaluate("document.getElementById('touch-layer').classList.contains('hidden')"))

            def assert_closed(how):
                self.assertFalse(page.evaluate("document.getElementById('realtime-btn').classList.contains('aiming')"),
                                 f"camera must un-arm via {how}")
                self.assertTrue(page.evaluate("document.getElementById('touch-layer').classList.contains('hidden')"),
                                f"aiming overlay must close via {how}")

            # The controls sit above the capture overlay while aiming, so the
            # PHOTO button is actually clickable to toggle off.
            arm()
            self.assertGreaterEqual(
                page.evaluate("Number(getComputedStyle(document.getElementById('action-wheel')).zIndex) || 0"), 41,
                "controls must sit above the capture overlay while aiming")
            page.evaluate("document.getElementById('realtime-btn').click()")  # click PHOTO again
            assert_closed("clicking PHOTO again")

            # Right-click anywhere on the scene exits (no browser menu).
            arm()
            page.evaluate("""() => document.getElementById('touch-layer').dispatchEvent(
                new MouseEvent('contextmenu', {cancelable: true, bubbles: true}))""")
            assert_closed("right-click")

            # Esc exits.
            arm()
            page.evaluate("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
            assert_closed("Esc")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (camera-exit) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_realtime_scan_tool_tags_and_interact(self):
        """The realtime SCAN tool must: be visible in /realtime, arm into a
        non-modal scanning overlay, surface recognized objects as starfield tags
        (from a mocked /api/detect over the live video frame), and let a tag's
        little + button steer the LIVE stream (a set_prompt hot-swap) anchored to
        that object WITHOUT resolving a turn (no /api/choose) or re-anchoring
        (no reset)."""
        page = self._new_realtime_page()
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "First-person VHS. A loading dock.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        chooses = []
        choose_bodies = []
        detects = []
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))

        def choose_handler(route):
            try:
                choose_bodies.append(route.request.post_data)
            except Exception:
                choose_bodies.append(None)
            chooses.append(route.request.url)
            route.fulfill(status=200, content_type="application/json", body="[]")
        page.route("**/api/choose", choose_handler)
        # Mock object recognition: two things the player can poke.
        def detect_handler(route):
            detects.append(route.request.url)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "objects": [
                    {"label": "wooden crate", "cx": 0.3, "cy": 0.45, "w": 0.2, "h": 0.2},
                    {"label": "steel door", "cx": 0.72, "cy": 0.5, "w": 0.15, "h": 0.4},
                ]
            }))
        page.route("**/api/detect", detect_handler)
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isShowing() === true", timeout=15000)
            # The SCAN tool is revealed in realtime mode.
            self.assertNotEqual(page.evaluate("getComputedStyle(document.getElementById('scan-btn')).display"), "none")
            # Arm it -> scanning: the overlay opens and an initial scan fires.
            page.evaluate("document.getElementById('scan-btn').click()")
            self.assertTrue(page.evaluate("document.getElementById('scan-btn').classList.contains('scanning')"))
            self.assertFalse(page.evaluate("document.getElementById('scan-layer').classList.contains('hidden')"))
            # Starfield tags appear from the recognized objects.
            page.wait_for_function("document.querySelectorAll('#scan-tags .scan-tag').length >= 2", timeout=10000)
            self.assertGreaterEqual(len(detects), 1, "SCAN never called /api/detect")
            labels = page.evaluate("Array.from(document.querySelectorAll('.scan-tag-label')).map(e=>e.textContent)")
            self.assertIn("wooden crate", labels)
            self.assertIn("steel door", labels)
            # Every tag carries its little act button.
            self.assertTrue(page.evaluate("!!document.querySelector('.scan-tag .scan-tag-act')"))
            # Poke the first (wooden crate) tag -> its action icons appear.
            page.evaluate("""() => {
                const tag = Array.from(document.querySelectorAll('.scan-tag'))
                    .find(t => t.querySelector('.scan-tag-label').textContent === 'wooden crate');
                tag.querySelector('.scan-tag-act').click();
            }""")
            self.assertTrue(page.evaluate("!!document.querySelector('.scan-tag.acting')"))
            # INTERACT and MOVE TO action icons are offered (no typing).
            self.assertTrue(page.evaluate("!!document.querySelector('.scan-tag.acting .scan-action-interact')"))
            self.assertTrue(page.evaluate("!!document.querySelector('.scan-tag.acting .scan-action-move')"))
            # Tap INTERACT -> a FULL TURN (/api/choose) with the composed prompt.
            page.evaluate("window.__MOCK_CMDS__ = []")
            page.evaluate("document.querySelector('.scan-tag.acting .scan-action-interact').click()")
            # Wait (Python-side) for the /api/choose call the full turn makes.
            for _ in range(60):
                if chooses:
                    break
                page.wait_for_timeout(100)
            self.assertGreaterEqual(len(chooses), 1, f"SCAN action must resolve a FULL turn (/api/choose). logs:\n{self._dump_logs()}")
            payload = json.loads(choose_bodies[0] or "{}")
            choice = payload.get("choice") or ""
            # The composed prompt is verb + the detected object's name.
            self.assertEqual(choice.strip(), "Interact with the wooden crate.",
                             f"action must be composed from verb + object; got {choice!r}")
            # A full turn is NOT a live re-steer: no set_prompt hot-swap fired.
            cmds = page.evaluate("window.__MOCK_CMDS__ || []")
            self.assertNotIn("set_prompt", cmds, "SCAN action must commit a turn, not live-steer the stream")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (scan) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_scan_prewarms_detection_in_background(self):
        """Detection must pre-warm in the background once a scene is on screen —
        even before SCAN is opened — so arming SCAN shows tags INSTANTLY (from the
        cache) instead of waiting for the first round-trip."""
        page = self._new_realtime_page()
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "A dock.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        detects = []
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        page.route("**/api/choose", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))

        def detect_handler(route):
            detects.append(route.request.url)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "objects": [{"label": "wooden crate", "cx": 0.3, "cy": 0.4, "w": 0.2, "h": 0.2}]}))
        page.route("**/api/detect", detect_handler)
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isShowing() === true", timeout=15000)
            # The background pre-warm fires WITHOUT the player opening SCAN.
            for _ in range(90):
                if detects:
                    break
                page.wait_for_timeout(100)
            self.assertGreaterEqual(len(detects), 1, f"detection must pre-warm in the background. logs:\n{self._dump_logs()}")
            self.assertFalse(page.evaluate("document.getElementById('scan-btn').classList.contains('scanning')"),
                             "pre-warm must not arm SCAN by itself")
            self.assertEqual(page.evaluate("document.querySelectorAll('#scan-tags .scan-tag').length"), 0,
                             "pre-warm must not render tags before SCAN is opened")
            # Opening SCAN renders the cached tag IMMEDIATELY (synchronously).
            page.evaluate("document.getElementById('scan-btn').click()")
            self.assertGreaterEqual(page.evaluate("document.querySelectorAll('#scan-tags .scan-tag').length"), 1,
                                    "SCAN must show pre-warmed tags instantly on open")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (scan-prewarm) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_realtime_scan_move_action(self):
        """MOVE TO on a non-enterable object composes an APPROACH prompt (naming
        the object + a forward-movement cue so the scene actually changes) and
        commits a full turn — no typing."""
        page = self._new_realtime_page()
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "First-person VHS. A loading dock.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        choose_bodies = []
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        page.route("**/api/detect", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({
            "objects": [{"label": "rusty valve", "cx": 0.4, "cy": 0.45, "w": 0.2, "h": 0.2}]})))

        def choose_handler(route):
            try:
                choose_bodies.append(route.request.post_data)
            except Exception:
                choose_bodies.append(None)
            route.fulfill(status=200, content_type="application/json", body="[]")
        page.route("**/api/choose", choose_handler)
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isShowing() === true", timeout=15000)
            page.evaluate("document.getElementById('scan-btn').click()")
            page.wait_for_function("document.querySelectorAll('#scan-tags .scan-tag').length >= 1", timeout=10000)
            # Open the tag's action icons and tap MOVE TO (no typing).
            page.evaluate("document.querySelector('.scan-tag .scan-tag-act').click()")
            page.wait_for_function("!!document.querySelector('.scan-tag.acting .scan-action-move')", timeout=5000)
            page.evaluate("document.querySelector('.scan-tag.acting .scan-action-move').click()")
            for _ in range(60):
                if choose_bodies:
                    break
                page.wait_for_timeout(100)
            self.assertGreaterEqual(len(choose_bodies), 1, f"MOVE action must commit a turn. logs:\n{self._dump_logs()}")
            choice = (json.loads(choose_bodies[0] or "{}").get("choice") or "").strip().lower()
            # A non-enterable object -> APPROACH phrasing (forward movement cue so
            # the scene actually changes), naming the object.
            self.assertIn("rusty valve", choice, f"move action must name the object; got {choice!r}")
            self.assertIn("approach", choice, f"move-to-approach must cue forward movement; got {choice!r}")
            self.assertNotIn("enter the", choice, f"a valve is not enterable; got {choice!r}")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (scan-move) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_realtime_scan_move_enters_a_passage(self):
        """MOVE TO on an enterable object (door/opening/room/vehicle/…) must phrase
        the action as an ENTRY ('Enter the <object> …') so the engine cuts to a
        fresh interior scene instead of drifting in place."""
        page = self._new_realtime_page()
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "A loading dock.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        choose_bodies = []
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        page.route("**/api/detect", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({
            "objects": [{"label": "steel door", "cx": 0.5, "cy": 0.45, "w": 0.2, "h": 0.3}]})))

        def choose_handler(route):
            try:
                choose_bodies.append(route.request.post_data)
            except Exception:
                choose_bodies.append(None)
            route.fulfill(status=200, content_type="application/json", body="[]")
        page.route("**/api/choose", choose_handler)
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isShowing() === true", timeout=15000)
            page.evaluate("document.getElementById('scan-btn').click()")
            page.wait_for_function("document.querySelectorAll('#scan-tags .scan-tag').length >= 1", timeout=10000)
            page.evaluate("document.querySelector('.scan-tag .scan-tag-act').click()")
            page.wait_for_function("!!document.querySelector('.scan-tag.acting .scan-action-move')", timeout=5000)
            page.evaluate("document.querySelector('.scan-tag.acting .scan-action-move').click()")
            for _ in range(60):
                if choose_bodies:
                    break
                page.wait_for_timeout(100)
            self.assertGreaterEqual(len(choose_bodies), 1, f"MOVE action must commit a turn. logs:\n{self._dump_logs()}")
            choice = (json.loads(choose_bodies[0] or "{}").get("choice") or "").strip()
            self.assertTrue(choice.lower().startswith("enter the steel door"),
                            f"MOVE TO an enterable object must be an entry; got {choice!r}")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (scan-move-enter) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_realtime_scan_persists_across_renderer_switch(self):
        """SCAN works in BOTH renderers, so switching from realtime to still
        images must NOT tear it down — the hub stays available and the overlay
        stays armed (it re-maps its tags to the still on the next scan)."""
        page = self._new_realtime_page()
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "First-person VHS. A loading dock.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        page.route("**/api/detect", lambda r: r.fulfill(status=200, content_type="application/json", body='{"objects":[]}'))
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isShowing() === true", timeout=15000)
            page.evaluate("document.getElementById('scan-btn').click()")
            self.assertTrue(page.evaluate("document.getElementById('scan-btn').classList.contains('scanning')"))
            # Switch to still images via the renderer toggle -> SCAN STAYS armed.
            page.evaluate("document.getElementById('btn-renderer').click()")
            page.wait_for_function("document.body.classList.contains('realtime-on') === false", timeout=5000)
            self.assertTrue(page.evaluate("document.getElementById('scan-btn').classList.contains('scanning')"),
                            "SCAN must stay armed after switching to still images")
            self.assertFalse(page.evaluate("document.getElementById('scan-layer').classList.contains('hidden')"),
                             "the scan overlay must stay open across the renderer switch")
            # The SCAN hub remains visible in stills mode (unlike TOUCH).
            self.assertNotEqual(page.evaluate("getComputedStyle(document.getElementById('scan-btn')).display"), "none")
            self.assertEqual(page.evaluate("getComputedStyle(document.getElementById('realtime-btn')).display"), "none")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (scan-switch) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_scan_tool_works_in_stills_mode(self):
        """SCAN must also work with the still-image renderer: arm it, scan the
        current still (not a video), tags twinkle in, and a tag's + button
        commits a full turn anchored on that object — no realtime video needed."""
        page = self._new_realtime_page()
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "A dim corridor.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        chooses = []
        choose_bodies = []
        detects = []
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))

        def choose_handler(route):
            try:
                choose_bodies.append(route.request.post_data)
            except Exception:
                choose_bodies.append(None)
            chooses.append(route.request.url)
            route.fulfill(status=200, content_type="application/json", body="[]")
        page.route("**/api/choose", choose_handler)

        def detect_handler(route):
            detects.append(route.request.url)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "objects": [{"label": "steel door", "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.3}]}))
        page.route("**/api/detect", detect_handler)
        try:
            # Force the STILL-image renderer (no realtime video).
            page.goto(f"{self.base_url}/standalone?renderer=image", wait_until="domcontentloaded")
            page.wait_for_selector("#scan-btn", state="attached", timeout=10000)
            # SCAN hub is available in stills mode too.
            self.assertNotEqual(page.evaluate("getComputedStyle(document.getElementById('scan-btn')).display"), "none")
            # The still must be on screen (a scene background is set).
            page.wait_for_function(
                "(document.getElementById('sceneA').style.backgroundImage||"
                "document.getElementById('sceneB').style.backgroundImage||'').length > 0",
                timeout=10000,
            )
            # Arm SCAN -> it scans the STILL and tags twinkle in.
            page.evaluate("document.getElementById('scan-btn').click()")
            self.assertTrue(page.evaluate("document.getElementById('scan-btn').classList.contains('scanning')"))
            page.wait_for_function("document.querySelectorAll('#scan-tags .scan-tag').length >= 1", timeout=10000)
            self.assertGreaterEqual(len(detects), 1, "SCAN never called /api/detect in stills mode")
            labels = page.evaluate("Array.from(document.querySelectorAll('.scan-tag-label')).map(e=>e.textContent)")
            self.assertIn("steel door", labels)
            # Poke the tag, tap INTERACT -> commits a full turn on the object.
            page.evaluate("document.querySelector('.scan-tag .scan-tag-act').click()")
            page.wait_for_function("!!document.querySelector('.scan-tag.acting .scan-action-interact')", timeout=5000)
            page.evaluate("document.querySelector('.scan-tag.acting .scan-action-interact').click()")
            for _ in range(60):
                if chooses:
                    break
                page.wait_for_timeout(100)
            self.assertGreaterEqual(len(chooses), 1, f"stills SCAN action didn't commit a turn. logs:\n{self._dump_logs()}")
            choice = json.loads(choose_bodies[0] or "{}").get("choice") or ""
            self.assertEqual(choice.strip(), "Interact with the steel door.",
                             f"action must be composed from verb + object; got {choice!r}")
        except Exception:
            print("\n=== CONSOLE LOG (scan-stills) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_scan_ceases_when_tag_action_submitted(self):
        """Submitting a SCAN tag action must END the scan session (close the
        overlay + clear the labels) rather than leaving tags hovering over the
        scene while the turn plays out."""
        page = self._new_realtime_page()
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "A loading dock.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        page.route("**/api/choose", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        page.route("**/api/detect", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({
            "objects": [{"label": "wooden crate", "cx": 0.3, "cy": 0.45, "w": 0.2, "h": 0.2}]})))
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isShowing() === true", timeout=15000)
            page.evaluate("document.getElementById('scan-btn').click()")
            page.wait_for_function("document.querySelectorAll('#scan-tags .scan-tag').length >= 1", timeout=10000)
            # Poke a tag, tap an action icon -> scan must CEASE.
            page.evaluate("document.querySelector('.scan-tag .scan-tag-act').click()")
            page.wait_for_function("!!document.querySelector('.scan-tag.acting .scan-action-interact')", timeout=5000)
            page.evaluate("document.querySelector('.scan-tag.acting .scan-action-interact').click()")
            # Scan overlay closes, the SCAN hub is no longer armed, and the labels
            # are gone.
            page.wait_for_function("document.getElementById('scan-layer').classList.contains('hidden')", timeout=6000)
            self.assertFalse(page.evaluate("document.getElementById('scan-btn').classList.contains('scanning')"),
                             "SCAN must cease (un-arm) after submitting a tag action")
            self.assertEqual(page.evaluate("document.querySelectorAll('#scan-tags .scan-tag').length"), 0,
                             "SCAN labels must be cleared after submitting a tag action")
        except Exception:
            print("\n=== CONSOLE LOG (scan-cease) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_photograph_mechanic_files_a_photo_specimen(self):
        """The photograph groundwork: pressing C captures a framed subset of the
        scene as a 'photo' specimen (POST /api/investigate kind=photo) and files
        it to the case file with a shutter."""
        page = self._new_realtime_page()
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "A loading dock.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        investigates = []
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))

        def inv_handler(route):
            investigates.append(route.request.post_data)
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True, "id": 1, "image_url": "/images/photo.jpg", "kind": "photo"}))
        page.route("**/api/investigate", inv_handler)
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isShowing() === true", timeout=15000)
            # Press C -> capturePhoto.
            page.evaluate("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'c', bubbles: true}))")
            for _ in range(60):
                if investigates:
                    break
                page.wait_for_timeout(100)
            self.assertGreaterEqual(len(investigates), 1, f"C must file a photo specimen. logs:\n{self._dump_logs()}")
            payload = json.loads(investigates[0] or "{}")
            self.assertEqual(payload.get("kind"), "photo")
            self.assertTrue((payload.get("texture") or "").startswith("data:image"))
            page.wait_for_function("document.querySelectorAll('#investigations-strip .inv-thumb.kind-photo').length >= 1", timeout=8000)
        except Exception:
            print("\n=== CONSOLE LOG (photo) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_ceremony_animates_all_steps_to_done(self):
        """The turn pipeline (Action → Consequence → World Updating → World
        Responding → Actions Generating → Guide Image) must animate through ALL
        steps and resolve. (In mock/image-disabled mode the guide-image step
        resolves immediately since no still is coming.)"""
        page = self._new_realtime_page()
        try:
            # The ceremony is renderer-agnostic; use image mode so the flow
            # matches the known-good standalone path (no reactor/video timing).
            page.goto(f"{self.base_url}/standalone?renderer=image", wait_until="domcontentloaded")
            # Intro choices from the auto-restart (uses the real mock backend).
            page.wait_for_selector(".choice-btn", state="attached", timeout=20000)
            page.evaluate("document.querySelector('.choice-btn').click()")  # begins the ceremony
            # The turn resolves and the ceremony reaches its green resolved state
            # with EVERY step marked done.
            page.wait_for_function(
                "document.getElementById('ceremony') && document.getElementById('ceremony').classList.contains('resolved')",
                timeout=25000,
            )
            done = page.evaluate(
                "Array.from(document.querySelectorAll('#ceremony-steps .cere-step')).map(n => n.classList.contains('done'))"
            )
            self.assertEqual(len(done), 6, f"expected 6 steps (incl. Guide Image), got {len(done)}")
            self.assertTrue(all(done), f"not all ceremony steps completed (incl. Guide Image): {done}")
            self.assertTrue(
                page.evaluate("!!document.querySelector('#ceremony-steps .cere-step[data-key=\"guide_image\"]')"),
                "the Guide Image ceremony step is missing",
            )
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (ceremony) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_loading_realtime_auto_restarts(self):
        """Loading /realtime must auto-restart from scratch: the client bootstrap
        POSTs /api/reset on load (no resuming the in-progress session)."""
        page = self._new_realtime_page()
        resets = []
        page.on("request", lambda r: (
            resets.append(r.url) if (r.method == "POST" and r.url.rstrip("/").endswith("/api/reset")) else None
        ))
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            # The fresh-start reset should be issued shortly after load.
            waited = 0
            while not resets and waited < 8000:
                page.wait_for_timeout(250)
                waited += 250
            self.assertTrue(resets, f"/api/reset was not POSTed on load. logs:\n{self._dump_logs()}")
        finally:
            page.close()

    def test_no_underlying_still_in_realtime_and_freeze_covers_reanchor(self):
        """The user-facing contract: in /realtime the underlying Gemini still is
        NEVER painted (so the original image can't flash between guide images),
        and the freeze back-buffer covers the re-anchor switch."""
        page = self._new_realtime_page()
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isReady() === true", timeout=15000)

            # Drive scene one through the FULL client facade (window.Renderer is
            # not exposed, so exercise the reactor facade the way standalone does
            # + verify standalone never painted a still).
            page.evaluate(
                "(img) => window.ReactorRenderer.applyScene({prompt: 'scene one', imageUrl: img, hardTransition: false})",
                TINY_PNG_DATA_URL,
            )
            page.wait_for_function("window.ReactorRenderer.isShowing() === true", timeout=15000)

            # Re-anchor onto a DIFFERENT guide image; watch the freeze cover it.
            saw_freeze = {"v": False}
            def poll_freeze():
                return page.evaluate("(document.getElementById('reactor-freeze')||{classList:{contains:()=>false}}).classList.contains('show')")
            page.evaluate(
                "() => window.ReactorRenderer.applyScene({prompt: 'scene two', imageUrl: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==', hardTransition: false})"
            )
            # Poll briefly for the freeze cover to appear during the switch.
            for _ in range(40):
                if poll_freeze():
                    saw_freeze["v"] = True
                    break
                page.wait_for_timeout(25)
            self.assertTrue(saw_freeze["v"], f"freeze buffer never covered the re-anchor. logs:\n{self._dump_logs()}")

            # And the live video comes back after the switch.
            page.wait_for_function("window.ReactorRenderer.isShowing() === true", timeout=15000)

            # The still scene layers must never have been painted in realtime mode.
            still_bgs = page.evaluate(
                "[getComputedStyle(document.getElementById('sceneA')).backgroundImage, getComputedStyle(document.getElementById('sceneB')).backgroundImage]"
            )
            self.assertTrue(all(bg in ("none", "") for bg in still_bgs), f"a Gemini still was painted in realtime mode: {still_bgs}")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (no-still/freeze) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_realtime_recovers_from_transient_error(self):
        """A transient realtime error (flaky first WebRTC/session attempt, common
        on mobile) must NOT permanently drop to stills with 'Realtime
        unavailable' — the client retries and self-heals back to live video."""
        page = self._new_realtime_page()
        page.add_init_script("window.__MOCK_ERROR_ONCE__ = true;")
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "A dock.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer !== undefined", timeout=10000)
            # The first connect errors; after retry the live video must reveal.
            page.wait_for_function("window.ReactorRenderer.isShowing() === true", timeout=20000)
            self.assertTrue(page.evaluate("window.__MOCK_ERRORED__ === true"), "the transient error path must have fired")
            # And it must NOT have fallen back to stills.
            self.assertFalse(page.evaluate("document.body.classList.contains('realtime-on') === false && !!document.getElementById('reactor-video').classList.contains('hidden')"))
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (rt-recover) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_realtime_paints_still_floor_so_never_black(self):
        """Anti-black safety net: in realtime mode the Gemini still is painted as
        a SILENT floor on the scene layer (beneath the live video), so if the
        video can't present frames (warming up / stalled / autoplay-blocked) the
        player sees the still instead of a black screen. Delivered via the real
        feed flow (Renderer.applyScene), not ReactorRenderer directly."""
        page = self._new_realtime_page()
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "A dock.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isReady() === true", timeout=15000)
            # The still floor gets painted onto a scene layer via Renderer.applyScene.
            page.wait_for_function(
                "(document.getElementById('sceneA').style.backgroundImage||"
                "document.getElementById('sceneB').style.backgroundImage||'').length > 0",
                timeout=10000,
            )
            floor = page.evaluate(
                "document.getElementById('sceneA').style.backgroundImage || document.getElementById('sceneB').style.backgroundImage || ''"
            )
            self.assertIn("data:image", floor, "the still floor must be painted so realtime is never just black")
            # And the live video still reveals on top of the floor.
            page.wait_for_function("window.ReactorRenderer.isShowing() === true", timeout=15000)
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (still-floor) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_realtime_no_frames_shows_still_not_black(self):
        """The exact 'just black' failure: the model accepts commands but never
        produces video frames (mirrors iOS Low Power Mode blocking autoplay, or a
        stalled stream). The <video> must stay HIDDEN (so a black non-playing
        video can't cover the scene) and the Gemini still floor must be visible —
        i.e. the player sees the still, not black."""
        page = self._new_realtime_page()
        # Suppress the mock video track + shorten the watchdog.
        page.add_init_script("window.__MOCK_NO_VIDEO__ = true; window.__REACTOR_REVEAL_WATCHDOG_MS__ = 1500;")
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "A dock.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isReady() === true", timeout=15000)
            # The still floor is painted so there's something to see...
            page.wait_for_function(
                "(document.getElementById('sceneA').style.backgroundImage||"
                "document.getElementById('sceneB').style.backgroundImage||'').length > 0",
                timeout=10000,
            )
            page.wait_for_timeout(2500)  # let the reveal watchdog window pass
            # ...no frames ever arrive, so the video must NOT be showing...
            self.assertFalse(page.evaluate("window.ReactorRenderer.isShowing()"))
            # ...and the black video must stay HIDDEN so it can't cover the still.
            self.assertTrue(page.evaluate("document.getElementById('reactor-video').classList.contains('hidden')"),
                            "black non-playing video must stay hidden so it can't cover the still")
            floor = page.evaluate(
                "document.getElementById('sceneA').style.backgroundImage || document.getElementById('sceneB').style.backgroundImage || ''"
            )
            self.assertIn("data:image", floor, "the still must be visible (not black) when the video has no frames")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (no-frames-still) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_realtime_starts_from_feed_scene_image(self):
        """FAITHFUL full-flow reproduction: don't touch ReactorRenderer directly.
        Instead deliver a scene_image through /api/feed exactly like the backend
        does, and let standalone.js's init -> bootstrap -> Renderer.applyScene
        drive the realtime renderer. This is the path that 'fails to start' in
        production, so it must reveal the live video end to end."""
        page = self._new_realtime_page()

        # Scripted feed: intro narrative + a guide-image scene + a choice prompt
        # (so bootstrap RESUMES rather than resetting). After id 3, nothing new.
        scene_items = [
            {"id": 1, "type": "narrative", "content": "You awaken in a drainage pipe."},
            {
                "id": 2,
                "type": "scene_image",
                "content": "",
                "image_url": TINY_PNG_DATA_URL,
                "metadata": {
                    "prompt": "First-person VHS. Dark drainage pipe. Motion: you crawl forward.",
                    "base": "First-person VHS. Dark drainage pipe.",
                    "hard_transition": False,
                },
            },
            {
                "id": 3,
                "type": "player_choice_prompt",
                "content": "What do you do?",
                "choices": [{"text": "Crawl forward"}, {"text": "Hold still"}],
            },
        ]

        # Loading /realtime auto-restarts, so the fresh intro (with the guide
        # scene_image) comes back from POST /api/reset. Feed polling returns
        # nothing new after that.
        def reset_handler(route):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items))

        page.route("**/api/reset", reset_handler)
        page.route("**/api/feed*", lambda route: route.fulfill(status=200, content_type="application/json", body="[]"))

        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isReady() === true", timeout=15000)
            # The scene_image is delivered by the auto-restart's /api/reset; the
            # realtime renderer must establish and reveal the live video with no
            # manual ReactorRenderer calls.
            page.wait_for_function("window.ReactorRenderer.isShowing() === true", timeout=15000)
            cmds = page.evaluate("window.__MOCK_CMDS__ || []")
            self.assertIn("start", cmds, f"realtime never started from the feed. logs:\n{self._dump_logs()}")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (feed_scene_image) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_static_burst_masks_reveal_not_teardown(self):
        """Staging/timing contract: the VCR static burst must be tied to the
        ACTUAL visible switch (freeze->video reveal / 'video_showing'), NOT to the
        realtime teardown ('reset' command). Firing it at teardown put the static
        seconds before the real switch — 'static, then a held still, then an
        abrupt jump to video'. A re-anchor here must NOT flash static between the
        reset and the reveal; the burst must land at (or after) the reveal."""
        page = self._new_realtime_page()
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isReady() === true", timeout=15000)

            # Scene one -> live video.
            page.evaluate(
                "(img) => window.ReactorRenderer.applyScene({prompt: 'scene one', imageUrl: img, hardTransition: false})",
                TINY_PNG_DATA_URL,
            )
            page.wait_for_function("window.ReactorRenderer.isShowing() === true", timeout=15000)

            # Instrument: timestamp every static burst (the #scene-glitch element
            # gains the 'burst' class) and the reset / reveal lifecycle events.
            # The onEvent wrapper still calls standalone's handler, so the real
            # glitch (fired by that handler on 'video_showing') is preserved.
            page.evaluate(
                """() => {
                    window.__BURST_TS__ = [];
                    window.__EVT_TS__ = {};
                    const g = document.getElementById('scene-glitch');
                    let had = false;
                    const mo = new MutationObserver(() => {
                        const now = g.classList.contains('burst');
                        if (now && !had) window.__BURST_TS__.push(performance.now());
                        had = now;
                    });
                    mo.observe(g, { attributes: true, attributeFilter: ['class'] });
                    const prev = window.ReactorRenderer.onEvent;
                    window.ReactorRenderer.onEvent = (name, data) => {
                        const t = performance.now();
                        if (name === 'command_sent' && data && data.command === 'reset') window.__EVT_TS__.reset = t;
                        else if (name === 'video_showing') window.__EVT_TS__.video_showing = t;
                        if (typeof prev === 'function') prev(name, data);
                    };
                }"""
            )
            # Clear any burst from scene one's own reveal, then re-anchor onto a
            # DIFFERENT guide image (reset -> re-establish -> reveal).
            page.evaluate("() => { window.__BURST_TS__ = []; }")
            page.evaluate(
                "() => window.ReactorRenderer.applyScene({prompt: 'scene two', imageUrl: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==', hardTransition: false})"
            )
            # Wait for the re-anchor's reset AND its subsequent reveal.
            page.wait_for_function("window.__EVT_TS__ && window.__EVT_TS__.reset != null", timeout=10000)
            page.wait_for_function("window.__EVT_TS__ && window.__EVT_TS__.video_showing != null", timeout=15000)
            # Let any (erroneous) teardown-time burst register.
            page.wait_for_timeout(150)

            evt = page.evaluate("window.__EVT_TS__")
            bursts = page.evaluate("window.__BURST_TS__ || []")
            reset_t = evt.get("reset")
            reveal_t = evt.get("video_showing")
            self.assertIsNotNone(reset_t, "re-anchor never issued a reset")
            self.assertIsNotNone(reveal_t, "re-anchor never revealed the video")
            # The reveal must genuinely come AFTER teardown (there IS a warmup gap).
            self.assertGreater(reveal_t, reset_t, "reveal did not follow the reset")
            # No static burst may fire in the teardown->reveal warmup window: the
            # burst must mask the reveal, so its first occurrence is at/after it
            # (small epsilon for the event/DOM-mutation ordering at reveal time).
            premature = [t for t in bursts if t < reveal_t - 40]
            self.assertEqual(
                premature, [],
                f"static burst fired before the reveal (masking nothing): "
                f"reset@{reset_t:.0f} reveal@{reveal_t:.0f} bursts={bursts}\n{self._dump_logs()}",
            )
            # And the reveal itself IS masked by a burst.
            self.assertTrue(
                any(t >= reveal_t - 40 for t in bursts),
                f"no static burst masked the reveal. reveal@{reveal_t:.0f} bursts={bursts}\n{self._dump_logs()}",
            )
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (burst_timing) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
