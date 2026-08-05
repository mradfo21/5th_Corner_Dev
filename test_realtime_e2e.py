"""
test_realtime_e2e.py — Playwright end-to-end test for the REALTIME (Reactor
Happy Oyster) renderer path, which the normal standalone e2e can't cover
(mock mode disables image generation, and the real renderer needs an external
WebRTC world-model service).

Strategy — run the REAL client JS against a MOCK Reactor SDK:
  * Boot `run_local.py --mock` with REACTOR_API_KEY set + SCENE_RENDERER=reactor
    so /api/reactor/config advertises the realtime renderer as enabled (Happy
    Oyster is the default model).
  * Intercept the SDK CDN import (esm.sh) and serve a mock ES module that
    simulates the world-model lifecycle (connect -> ready, create_world ->
    world_state(ready), start_travel -> a real canvas.captureStream() video track
    so the <video> gets decoded frames and videoWidth > 0). The legacy LingBot
    protocol (set_image/set_prompt/start/reset) is also simulated for the models
    still selectable from the switcher.
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
# the surface reactor_renderer.js uses, for BOTH protocol families the renderer
# drives:
#   • Happy Oyster (the DEFAULT): create_world -> world_state(ready) ->
#     start_travel -> a REAL video track; held move/look/interact/stop are
#     recorded so the movement tests can assert them.
#   • LingBot / seed_locked (legacy, still selectable): set_image ->
#     image_accepted, set_prompt -> prompt_accepted, start -> generation_started
#     + a video track; persistent set_move_*/set_look_* axes echo `state`.
# On start_travel / start it emits a canvas.captureStream() video track so the
# <video> decodes frames and reveals exactly like production — the single
# hand-off the renderer waits on.
MOCK_SDK_JS = r"""
export class Reactor {
  constructor(opts) {
    this._h = {}; this._opts = opts || {}; this._timer = null;
    // Optional capabilities payload so tests can exercise the production
    // "capabilities known" path (real SDK advertises a command/track schema).
    this._caps = (typeof window !== "undefined" && window.__MOCK_CAPS__) || null;
  }
  getCapabilities() { return this._caps; }
  on(evt, fn) { (this._h[evt] = this._h[evt] || []).push(fn); }
  _emit(evt) {
    const args = Array.prototype.slice.call(arguments, 1);
    (this._h[evt] || []).forEach((fn) => { try { fn.apply(null, args); } catch (e) { console.error("mock handler", evt, e); } });
  }
  async connect(jwt) {
    this._jwt = jwt;
    window.__MOCK_REACTOR_CONNECTED__ = true;
    // Advertise the capability schema (if the test set one) before ready, like
    // the real SDK does — so the renderer's command/track gating runs for real.
    if (this._caps) setTimeout(() => this._emit("capabilitiesReceived", this._caps), 8);
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
  _emitState() {
    const a = this._axes;
    const map = { move_longitudinal: { forward: "w", back: "s" }, move_lateral: { strafe_left: "q", strafe_right: "e" },
                  look_horizontal: { left: "a", right: "d" }, look_vertical: { up: "i", down: "k" } };
    const parts = [];
    ["move_longitudinal", "move_lateral", "look_horizontal", "look_vertical"].forEach((k) => {
      const t = map[k][a[k]]; if (t) parts.push(t);
    });
    this._emit("message", { type: "state", data: {
      current_action: parts.length ? parts.join("+") : "still",
      move_longitudinal: a.move_longitudinal, move_lateral: a.move_lateral,
      look_horizontal: a.look_horizontal, look_vertical: a.look_vertical,
      rotation_speed_deg: a.rotation_speed_deg, current_chunk: this._chunk || 0,
    }});
  }
  async sendCommand(name, data) {
    window.__MOCK_CMDS__ = window.__MOCK_CMDS__ || [];
    window.__MOCK_CMDS__.push(name);
    // Full record (name + data) so movement tests can assert exact axis values.
    window.__MOCK_CMD_LOG__ = window.__MOCK_CMD_LOG__ || [];
    window.__MOCK_CMD_LOG__.push({ name: name, data: data || {} });
    // Track LingBot World 2's persistent movement/look axes and echo `state`.
    this._axes = this._axes || { move_longitudinal: "idle", move_lateral: "idle",
      look_horizontal: "idle", look_vertical: "idle", rotation_speed_deg: 5.0 };
    const AX = { set_move_longitudinal: "move_longitudinal", set_move_lateral: "move_lateral",
      set_look_horizontal: "look_horizontal", set_look_vertical: "look_vertical" };
    if (AX[name]) { this._axes[AX[name]] = (data || {})[AX[name]]; setTimeout(() => this._emitState(), 5); }
    else if (name === "set_rotation_speed_deg") { this._axes.rotation_speed_deg = (data || {}).rotation_speed_deg; setTimeout(() => this._emitState(), 5); }
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
    } else if (name === "create_world") {
      // Happy Oyster: a short build, then a ready world_state (create_world
      // resolves on that). A prior world's stream keeps running until travel.
      window.__MOCK_WORLDS__ = (window.__MOCK_WORLDS__ || 0) + 1;
      const p = data || {};
      setTimeout(() => this._emit("message", { type: "world_state", data: {
        phase: "building", prompt: p.prompt || "", mode: 1 } }), 6);
      setTimeout(() => this._emit("message", { type: "world_state", data: {
        phase: "ready",
        encrypted_world_id: "world_" + Math.random().toString(36).slice(2),
        prompt: p.prompt || "", first_frame: p.first_frame_image_url || "", mode: 1,
      } }), 14);
    } else if (name === "attach_world") {
      // Reopen a saved world: ready immediately (no build), echoing its id.
      window.__MOCK_ATTACHES__ = (window.__MOCK_ATTACHES__ || 0) + 1;
      const id = (data || {}).encrypted_world_id || "";
      setTimeout(() => this._emit("message", { type: "world_state", data: {
        phase: "ready", encrypted_world_id: id, mode: 1 } }), 8);
    } else if (name === "start_travel") {
      // Advertise this world's interaction verbs, then stream video (unless the
      // stall path is being exercised).
      setTimeout(() => this._emit("message", { type: "travel_state", data: {
        status: "running", user_instructions: [], chapters: [],
        character_actions: ["Jump", "Attack"], environment_actions: ["Open"] } }), 8);
      if (!window.__MOCK_NO_VIDEO__) setTimeout(() => this._startVideo(), 30);
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

        # Scan hotspots fade out on a TTL (SCAN_TTL_MS). Stretch it way out for
        # tests so a scanned tag stays put through multi-step assertions; the
        # dedicated fade-out test overrides this back to a short value.
        page.add_init_script("window.__SCAN_TTL_MS__ = 60000;")
        # Start past first-run onboarding so the "tap to scan" tutorial modal
        # never pops up and intercepts the pointer clicks these tests make.
        page.add_init_script("try { localStorage.setItem('scan_tutorial_seen_v1', '1'); } catch (e) {}")

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
        # Pin the advertised default world model to Happy Oyster. This suite is
        # written around Happy Oyster's build/travel protocol (see the test
        # docstrings), so it must not depend on the PRODUCTION default — which is
        # now LingBot World 2. Mock /api/reactor/config so the renderer always
        # boots Happy Oyster here, regardless of the server-side default.
        page.route(
            "**/api/reactor/config",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "enabled": True,
                    "renderer": "reactor",
                    "world_model": "happy-oyster",
                    "model_name": "reactor/happy-oyster",
                    "available_models": [
                        {"id": "happy-oyster", "label": "Happy Oyster", "sdk_name": "reactor/happy-oyster", "requires_seed_image": False, "protocol": "happy_oyster"},
                        {"id": "lingbot-world-2", "label": "LingBot World 2", "sdk_name": "reactor/lingbot-world-2", "requires_seed_image": True, "protocol": "seed_locked"},
                        {"id": "helios", "label": "Helios", "sdk_name": "reactor/helios", "requires_seed_image": False, "protocol": "blend"},
                    ],
                    "allow_custom_models": True,
                    "sdk_name_prefix": "reactor/",
                }),
            ),
        )
        return page

    def _new_realtime_mobile_page(self, device_name="iPhone 13"):
        """Like _new_realtime_page, but in a phone-emulated context (real
        touch + a phone UA + a portrait phone viewport) so the mobile
        `html.is-mobile` device path — small letterboxed "contain" media fit,
        collapsed beacon-dot scan tags — is actually exercised, not just the
        desktop layout at a smaller window size. Caller must close the
        returned page's context too (self._mobile_context)."""
        device = self.playwright.devices[device_name]
        context = self.browser.new_context(**device)
        self._mobile_context = context
        page = context.new_page()
        self._logs = []
        page.on("console", lambda m: self._logs.append(f"{m.type}: {m.text}"))
        page.on("pageerror", lambda e: self._logs.append(f"PAGEERROR: {e}"))
        page.add_init_script("window.__SCAN_TTL_MS__ = 60000;")
        page.route(
            "https://esm.sh/**",
            lambda route: route.fulfill(status=200, content_type="application/javascript", body=MOCK_SDK_JS),
        )
        page.route(
            "**/api/reactor/token",
            lambda route: route.fulfill(status=200, content_type="application/json",
                                         body='{"jwt": "mock.jwt.token", "expires_at": 9999999999}'),
        )
        return page

    def _dump_logs(self):
        return "\n".join(self._logs[-60:])

    def _scan_now(self, page, timeout=15000):
        """Fire a manual SCAN pass the way a player would: click the SCAN button.
        Playwright auto-waits for it to be actionable (enabled + not receding
        behind a turn), so this naturally waits out any in-flight turn. Scanning
        is gated behind this button now — nothing detects on its own."""
        page.click("#scan-btn", timeout=timeout)

    def test_realtime_video_reveals_on_scene(self):
        """The core contract: enable realtime, apply a scene (world prompt +
        first-frame image), and the live video must actually show
        (isShowing() -> true). Default model is Happy Oyster: build a world
        (create_world) then travel it (start_travel)."""
        page = self._new_realtime_page()
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            # Realtime renderer should exist and be forced on.
            page.wait_for_function("window.ReactorRenderer !== undefined", timeout=10000)

            # Wait until the mock SDK has connected and the renderer is ready.
            page.wait_for_function("window.__MOCK_REACTOR_CONNECTED__ === true", timeout=10000)
            page.wait_for_function("window.ReactorRenderer.isReady() === true", timeout=10000)

            # Drive a first-frame scene through the realtime facade.
            page.evaluate(
                """(img) => window.ReactorRenderer.applyScene({
                    prompt: 'First-person VHS. A dark drainage pipe interior you can walk through.',
                    imageUrl: img,
                    hardTransition: false,
                })""",
                TINY_PNG_DATA_URL,
            )

            # The single hand-off: the live video reveals with decoded frames.
            page.wait_for_function("window.ReactorRenderer.isShowing() === true", timeout=15000)
            self.assertTrue(page.evaluate("window.ReactorRenderer.getStatus()") == "live")

            cmds = page.evaluate("window.__MOCK_CMDS__ || []")
            self.assertIn("create_world", cmds, f"create_world never sent. logs:\n{self._dump_logs()}")
            self.assertIn("start_travel", cmds, f"start_travel never sent. logs:\n{self._dump_logs()}")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (video_reveals) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_realtime_reanchor_on_new_guide_image(self):
        """After the first scene is live, a NEW guide image must re-anchor and
        reveal the video again. On Happy Oyster a new scene is a NEW WORLD, so
        the re-anchor issues a SECOND create_world (+ start_travel) rather than a
        reset-in-place."""
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
            page.evaluate(
                """() => window.ReactorRenderer.applyScene({prompt: 'scene two', imageUrl: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==', hardTransition: false})"""
            )
            # A second world must be built for the re-anchor, then the video
            # reveals again.
            page.wait_for_function("(window.__MOCK_CMDS__||[]).filter(c=>c==='create_world').length >= 2", timeout=10000)
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
            # start_travel is issued...
            page.wait_for_function("(window.__MOCK_CMDS__||[]).includes('start_travel')", timeout=10000)
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
        (create_world with the world prompt) and what the model REPORTS (world
        ready / travelling / video live), so the black box is inspectable."""
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
            self.assertIn("create_world", text, f"inspector didn't log the world build we sent. log:\n{text}")
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
        # A detected subject dead-center: framing it makes the shot WORTHY.
        page.route("**/api/detect", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({
            "objects": [{"label": "figure", "cx": 0.5, "cy": 0.5, "w": 0.25, "h": 0.4}]})))
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isShowing() === true", timeout=15000)
            # The camera hub is shown; the reticle is a CAMERA (not a hand/glass).
            self.assertNotEqual(page.evaluate("getComputedStyle(document.getElementById('realtime-btn')).display"), "none")
            self.assertTrue(page.evaluate("!!document.querySelector('#touch-reticle .touch-cam')"))
            self.assertIsNone(page.evaluate("document.querySelector('#touch-reticle .touch-hand')"))
            # Arm it — photographable targets surface from the live detection.
            page.evaluate("document.getElementById('realtime-btn').click()")
            self.assertTrue(page.evaluate("document.getElementById('realtime-btn').classList.contains('aiming')"))
            page.wait_for_function("document.querySelectorAll('#touch-targets .photo-target').length >= 1", timeout=8000)
            page.evaluate("window.__MOCK_CMDS__ = []")
            # TAP the centered subject (press+release) — a worthy shot.
            page.evaluate(
                """() => {
                    const L = document.getElementById('touch-layer');
                    const o = {clientX: window.innerWidth/2, clientY: window.innerHeight/2, pointerId: 1, cancelable: true, bubbles: true};
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
        """Optical zoom: the camera arms on the FULL frame (1.0x), the mouse wheel
        then magnifies the scene (a CSS scale transform on the video layer) within
        bounds, the readout tracks it, a big wheel-down clamps back to the wide
        1.0x bound (transform cleared), and a two-finger PINCH zooms WITHOUT
        firing a capture (no /api/investigate) — only a clean single-finger tap
        shoots."""
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
        page.route("**/api/detect", lambda r: r.fulfill(status=200, content_type="application/json", body='{"objects":[]}'))
        page.route("**/api/investigate", lambda r: (investigates.append(r.request.post_data),
                   r.fulfill(status=200, content_type="application/json", body='{"ok":true,"id":1,"kind":"photo"}')))
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isShowing() === true", timeout=15000)
            # Arm the camera -> opens on the full frame (1.0x): no scale transform.
            page.evaluate("document.getElementById('realtime-btn').click()")
            page.wait_for_function("!document.getElementById('touch-layer').classList.contains('hidden')", timeout=4000)
            z_armed = page.evaluate("parseFloat(document.getElementById('touch-zoom').textContent)")
            self.assertAlmostEqual(z_armed, 1.0, places=1, msg="arming opens on the full 16:9 frame")
            self.assertNotIn("scale(", page.evaluate("document.getElementById('reactor-video').style.transform || ''"),
                             "the full-frame view is not magnified")

            # Wheel up (deltaY < 0) zooms IN: the readout climbs and the layer scales up.
            page.evaluate("""() => document.getElementById('touch-layer').dispatchEvent(
                new WheelEvent('wheel', {deltaY: -600, cancelable: true, bubbles: true}))""")
            page.wait_for_function(
                "(document.getElementById('reactor-video').style.transform || '').includes('scale(')", timeout=4000)
            z_in = page.evaluate("parseFloat(document.getElementById('touch-zoom').textContent)")
            self.assertGreater(z_in, z_armed, "wheel up must zoom in")

            # A big wheel-down clamps back to the wide bound (1.0x, transform cleared).
            page.evaluate("""() => document.getElementById('touch-layer').dispatchEvent(
                new WheelEvent('wheel', {deltaY: 6000, cancelable: true, bubbles: true}))""")
            page.wait_for_function(
                "!(document.getElementById('reactor-video').style.transform || '').includes('scale(')", timeout=4000)
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
        # One shot that documents enough distinct subjects to close the case. The
        # exact goal is read from the live Evidence tracker (CASE_TARGET) so this
        # test never drifts if the census target is retuned.
        subject_pool = ["figure", "valve", "brush pile", "structure", "lantern",
                        "crate", "wire", "boot print", "ladder", "tarp",
                        "generator", "barrel", "sign", "toolbox", "gauge", "cable"]
        photo_items = {"body": None}

        def photo_handler(route):
            route.fulfill(status=200, content_type="application/json", body=photo_items["body"])
        page.route("**/api/photo", photo_handler)
        # A centered detected subject makes the shot worthy (gate).
        page.route("**/api/detect", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({
            "objects": [{"label": "figure", "cx": 0.5, "cy": 0.5, "w": 0.25, "h": 0.4}]})))
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isShowing() === true", timeout=15000)
            # Arm the camera: the dossier HUD (with the case goal) is revealed.
            page.evaluate("document.getElementById('realtime-btn').click()")
            page.wait_for_function("!document.getElementById('evidence-hud').classList.contains('hidden')", timeout=5000)
            # The census goal is whatever the live tracker reports (CASE_TARGET).
            goal = page.evaluate("window.Evidence.target()")
            self.assertGreaterEqual(goal, 1)
            self.assertIn("/" + str(goal), page.evaluate("document.querySelector('#evidence-hud .ev-case-count').textContent"))
            # Document exactly `goal` distinct subjects in one dense frame.
            subjects = subject_pool[:goal]
            self.assertEqual(len(subjects), goal, "need enough distinct subjects to close the case")
            photo_items["body"] = json.dumps({
                "items": [{"label": s, "interest": 4, "note": "clue"} for s in subjects],
                "caption": "A dense, telling frame.", "mood": "ominous"})
            page.wait_for_function("document.querySelectorAll('#touch-targets .photo-target').length >= 1", timeout=8000)
            # Take the shot at the centered subject (press + release).
            page.evaluate("""() => {
                const L = document.getElementById('touch-layer');
                const o = {clientX: window.innerWidth/2, clientY: window.innerHeight/2, pointerId: 1, cancelable: true, bubbles: true};
                L.dispatchEvent(new PointerEvent('pointerdown', o));
                L.dispatchEvent(new PointerEvent('pointerup', o));
            }""")
            # The case closes: the win overlay appears with a rank grade.
            page.wait_for_function("!document.getElementById('case-overlay').classList.contains('hidden')", timeout=15000)
            rank = page.evaluate("document.getElementById('case-rank-letter').textContent")
            self.assertIn(rank, ["D", "C", "B", "A", "S"], f"a rank grade must be shown, got {rank!r}")
            self.assertEqual(page.evaluate("document.getElementById('case-subjects').textContent"), str(goal))
            # Starting a NEW CASE clears the win overlay and resets the census.
            page.evaluate("document.getElementById('case-restart').click()")
            page.wait_for_function("document.getElementById('case-overlay').classList.contains('hidden')", timeout=8000)
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (win) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_photo_worthy_shot_requires_a_framed_subject(self):
        """A shot only gathers evidence when a DETECTED subject is framed. The
        viewfinder is a fixed, centered window, so every shot captures the center;
        an EMPTY frame (nothing detected) misses (no receipt, no /api/photo),
        while a centered detected subject is worthy (receipt shows). This uses
        genuine in-game perception data."""
        page = self._new_realtime_page()
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "A loading dock.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        photos = []
        detects = []
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        page.route("**/api/investigate", lambda r: r.fulfill(status=200, content_type="application/json", body='{"ok":true,"id":1,"kind":"photo"}'))

        # First detection returns NOTHING (empty frame -> a shot misses); after
        # that, a centered subject appears (a shot becomes worthy).
        def detect_handler(route):
            detects.append(1)
            objs = [] if len(detects) == 1 else [
                {"label": "figure", "cx": 0.5, "cy": 0.5, "w": 0.25, "h": 0.4}]
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"objects": objs}))
        page.route("**/api/detect", detect_handler)

        def photo_handler(route):
            photos.append(route.request.url)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "items": [{"label": "figure", "interest": 4, "note": "a witness"}], "caption": "There.", "mood": "tense"}))
        page.route("**/api/photo", photo_handler)

        def shoot_center(pointer_id):
            page.evaluate(
                """(pid) => {
                    const L = document.getElementById('touch-layer');
                    const o = {clientX: window.innerWidth/2, clientY: window.innerHeight/2, pointerId: pid, cancelable: true, bubbles: true};
                    L.dispatchEvent(new PointerEvent('pointerdown', o));
                    L.dispatchEvent(new PointerEvent('pointerup', o));
                }""", pointer_id)
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isShowing() === true", timeout=15000)
            page.evaluate("document.getElementById('realtime-btn').click()")
            # Wait for the first (empty) detection to return, so the worthy-shot
            # gate is live (detection has run) but no subject is framed.
            for _ in range(80):
                if detects:
                    break
                page.wait_for_timeout(100)
            self.assertGreaterEqual(len(detects), 1, "photo detection never ran")
            page.wait_for_timeout(300)  # let the detect result mark detection live

            # MISS: shoot the centered (empty) frame -> no receipt, no appraisal.
            shoot_center(2)
            page.wait_for_timeout(700)
            self.assertEqual(len(photos), 0, "an empty frame must not appraise the shot")
            self.assertFalse(page.evaluate("document.getElementById('photo-receipt').classList.contains('show')"),
                             "a miss must not show the receipt")

            # WORTHY: once the centered subject is detected, framing it develops a receipt.
            page.wait_for_function("document.querySelectorAll('#touch-targets .photo-target').length >= 1", timeout=8000)
            shoot_center(3)
            page.wait_for_function("document.getElementById('photo-receipt').classList.contains('show')", timeout=6000)
            self.assertGreaterEqual(len(photos), 1, "a worthy shot must appraise the frame")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (worthy) ===\n" + self._dump_logs())
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
        non-modal scanning overlay,         surface recognized objects as starfield tags
        (from a mocked /api/detect over the live video frame), and let a tag's
        little + button steer the LIVE world anchored to that object WITHOUT
        resolving a turn (no /api/choose) or rebuilding the world. On Happy Oyster
        that steer is a real interact({action}) verb command."""
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
            # Scanning is gated behind the SCAN button: nothing detects until the
            # player presses it. It must not have fired on its own.
            self.assertEqual(len(detects), 0, "detection must not run before SCAN is pressed")
            self._scan_now(page)
            # After the scan pass the overlay is live and the tags render.
            page.wait_for_function("document.querySelectorAll('#scan-tags .scan-tag').length >= 2", timeout=10000)
            self.assertFalse(page.evaluate("document.getElementById('scan-layer').classList.contains('hidden')"))
            self.assertGreaterEqual(len(detects), 1, "pressing SCAN must call /api/detect")
            labels = page.evaluate("Array.from(document.querySelectorAll('.scan-tag-label')).map(e=>e.textContent)")
            self.assertIn("wooden crate", labels)
            self.assertIn("steel door", labels)
            # Clicking a tag (the whole tag is the click target) opens its actions.
            page.evaluate("""() => {
                const tag = Array.from(document.querySelectorAll('.scan-tag'))
                    .find(t => t.querySelector('.scan-tag-label').textContent === 'wooden crate');
                tag.click();
            }""")
            self.assertTrue(page.evaluate("!!document.querySelector('.scan-tag.acting')"))
            # INTERACT and MOVE TO action icons are offered (no typing).
            self.assertTrue(page.evaluate("!!document.querySelector('.scan-tag.acting .scan-action-interact')"))
            self.assertTrue(page.evaluate("!!document.querySelector('.scan-tag.acting .scan-action-move')"))
            # Tap INTERACT in realtime -> a LIVE interaction on the running world
            # (Happy Oyster interact({action}) verb), NOT a full turn: the world
            # reacts in place, so /api/choose is never called and no world rebuild
            # (create_world) fires.
            page.evaluate("window.__MOCK_CMDS__ = []")
            page.evaluate("document.querySelector('.scan-tag.acting .scan-action-interact').click()")
            # INSTANT press-ceremony: a ring pulse blooms at the object the moment
            # the button is pressed — proof of the press that does NOT depend on
            # the world model reacting (it's spawned synchronously in the handler).
            self.assertGreaterEqual(
                page.evaluate("document.querySelectorAll('#scan-layer .scan-pulse.scan-pulse-interact').length"), 1,
                f"INTERACT must show an instant pulse at the object. logs:\n{self._dump_logs()}")
            # The live steer is a synchronous interact verb on the running world.
            page.wait_for_function("(window.__MOCK_CMDS__ || []).includes('interact')", timeout=5000)
            cmds = page.evaluate("window.__MOCK_CMDS__ || []")
            self.assertIn("interact", cmds, "realtime INTERACT must fire a live interaction verb on the world")
            self.assertNotIn("create_world", cmds, "realtime INTERACT must not rebuild the world")
            # Give any (erroneous) turn a moment to fire, then assert none did.
            page.wait_for_timeout(500)
            self.assertEqual(len(chooses), 0, f"realtime INTERACT must NOT resolve a full turn (/api/choose). logs:\n{self._dump_logs()}")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (scan) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_realtime_interact_steers_without_a_cached_scene_base(self):
        """Regression: INTERACT must inject a LIVE world interaction even when the
        standalone layer never cached a scene bible (Renderer.lastBase/lastScene
        are null) — the exact state native movement/exploration mode leaves the
        renderer in. On Happy Oyster it fires a real interact({action}) verb on
        the running world, NOT silently drop to a full turn (which would pop the
        progress bar for what should be an instant poke)."""
        page = self._new_realtime_page()
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "First-person VHS. A canyon.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        chooses = []
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        page.route("**/api/detect", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({
            "objects": [{"label": "rusty valve", "cx": 0.4, "cy": 0.45, "w": 0.2, "h": 0.2}]})))

        def choose_handler(route):
            chooses.append(route.request.url)
            route.fulfill(status=200, content_type="application/json", body="[]")
        page.route("**/api/choose", choose_handler)
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isShowing() === true", timeout=15000)
            self._scan_now(page)
            page.wait_for_function("document.querySelectorAll('#scan-tags .scan-tag').length >= 1", timeout=12000)
            # Simulate native-movement state: wipe the standalone layer's cached
            # scene bible so the ONLY base source left is the reactor's live prompt.
            page.evaluate("window.__Renderer.lastBase = null; window.__Renderer.lastScene = null;")
            # The reactor is running a prompt (it established the stream), so the
            # steer base must resolve via getPrompt() and never be empty.
            self.assertTrue(page.evaluate("!!(window.__Renderer.steerBase && window.__Renderer.steerBase())"),
                            f"steer base must never be empty while realtime is live. logs:\n{self._dump_logs()}")
            page.evaluate("document.querySelector('.scan-tag').click()")
            page.wait_for_function("!!document.querySelector('.scan-tag.acting .scan-action-interact')", timeout=5000)
            page.evaluate("window.__MOCK_CMDS__ = []")
            page.evaluate("document.querySelector('.scan-tag.acting .scan-action-interact').click()")
            # Must live-interact on the running world, NOT resolve a full turn.
            page.wait_for_function("(window.__MOCK_CMDS__ || []).includes('interact')", timeout=5000)
            page.wait_for_timeout(500)
            self.assertEqual(len(chooses), 0,
                             f"INTERACT with no cached base must still steer live, not pop a turn. logs:\n{self._dump_logs()}")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (scan-no-base) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_hotspots_are_gated_behind_the_scan_button(self):
        """Detection is gated behind the SCAN button (Gemini recognition is our
        biggest cost). With a scene on screen but SCAN not pressed, NOTHING
        detects and no tags render. Pressing SCAN fires exactly one /api/detect
        pass and the interaction hotspots (starfield tags) appear."""
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
            # The SCAN button exists (it's how you scan now).
            self.assertIsNotNone(page.query_selector("#scan-btn"), "the SCAN button must exist")
            # Give any (erroneous) ambient detection a beat to fire, then assert
            # none did and no tags are on screen — scanning is strictly manual.
            page.wait_for_timeout(800)
            self.assertEqual(len(detects), 0, f"nothing may detect before SCAN is pressed. logs:\n{self._dump_logs()}")
            self.assertEqual(page.evaluate("document.querySelectorAll('#scan-tags .scan-tag').length"), 0,
                             "no hotspots may render before SCAN is pressed")
            # Press SCAN -> exactly one detect pass -> tags render.
            self._scan_now(page)
            page.wait_for_function("document.querySelectorAll('#scan-tags .scan-tag').length >= 1", timeout=12000)
            self.assertGreaterEqual(len(detects), 1, f"pressing SCAN must run detection. logs:\n{self._dump_logs()}")
            self.assertFalse(page.evaluate("document.getElementById('scan-layer').classList.contains('hidden')"),
                             "the hotspot overlay must be live after a scan")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (scan-gated) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_tapping_the_world_triggers_a_scan(self):
        """Tapping the scene itself (when no instrument/mode is open) fires the
        same on-demand scan the button does, with a tactile ripple at the tap
        point. Clicking a control instead is left to that control — it must not
        scan the world."""
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
            page.wait_for_timeout(500)
            # Clicking a CONTROL (the action wheel container) must NOT scan.
            page.eval_on_selector("#action-wheel", "el => el.click()")
            page.wait_for_timeout(300)
            self.assertEqual(len(detects), 0, f"clicking a control must not scan the world. logs:\n{self._dump_logs()}")
            # Tap the WORLD (empty scene, clear of the wheel/objectives/rail).
            page.mouse.click(430, 300)
            # A tactile ripple blooms at the tap point...
            page.wait_for_selector(".world-tap-ripple", timeout=1500)
            # ...and the tap fires exactly the same scan the button does.
            page.wait_for_function("document.querySelectorAll('#scan-tags .scan-tag').length >= 1", timeout=12000)
            self.assertGreaterEqual(len(detects), 1, f"tapping the world must run detection. logs:\n{self._dump_logs()}")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (world-tap) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_tapping_a_subject_creates_its_targeted_field_objective(self):
        """Tapping ON a recognized subject must deterministically surface its
        "Photograph the X" field objective — not sometimes, depending on
        box-size ranking or how many bounties already exist — and lock the
        on-screen tag with a `.targeted` treatment. Re-tapping the same
        subject must re-affirm the objective rather than going silent."""
        page = self._new_realtime_page()
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "An oil platform.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        page.route("**/api/choose", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        # Two subjects placed far apart on screen so a tap unambiguously aims
        # at exactly one of them.
        page.route("**/api/detect", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({
            "objects": [
                {"label": "oil rig", "cx": 0.62, "cy": 0.45, "w": 0.15, "h": 0.2},
                {"label": "shipping container", "cx": 0.2, "cy": 0.45, "w": 0.15, "h": 0.15},
            ]})))
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isShowing() === true", timeout=15000)
            # First, an ambient SCAN (the button — no tap point) just to learn
            # where the detector places "oil rig" on screen this run.
            self._scan_now(page)
            page.wait_for_function("document.querySelectorAll('#scan-tags .scan-tag').length >= 2", timeout=12000)
            rig_pos = page.evaluate("""() => {
                const tag = Array.from(document.querySelectorAll('.scan-tag'))
                    .find(t => t.querySelector('.scan-tag-label').textContent === 'oil rig');
                return { x: parseFloat(tag.dataset.sx), y: parseFloat(tag.dataset.sy) };
            }""")
            self.assertIsNotNone(rig_pos)
            # Tap just OFF the tag itself (so the click lands on the scene
            # surface, not the tag's own click handler) but still comfortably
            # inside its "near" radius and far from the other subject.
            tap_x, tap_y = rig_pos["x"], max(60, rig_pos["y"] - 55)
            page.mouse.click(tap_x, tap_y)
            page.wait_for_selector(".world-tap-ripple", timeout=1500)
            # The board must now carry a FIELD bounty naming the oil rig
            # specifically — because that's what was tapped, not because of
            # box-size ranking or how many bounties already existed.
            page.wait_for_function(
                "Array.from(document.querySelectorAll('#obj-list .obj-item.kind-field .obj-title'))"
                ".some(e => e.textContent.toLowerCase().includes('oil rig'))",
                timeout=10000,
            )
            # The tapped tag itself gets the lock-on treatment.
            page.wait_for_function("""() => {
                const tag = Array.from(document.querySelectorAll('.scan-tag'))
                    .find(t => t.querySelector('.scan-tag-label').textContent === 'oil rig');
                return !!tag && tag.classList.contains('targeted');
            }""", timeout=5000)
            # Re-tapping the SAME subject must re-affirm, not go silent — the
            # objective stays on the board.
            page.mouse.click(tap_x, tap_y)
            page.wait_for_timeout(400)
            self.assertTrue(page.evaluate(
                "Array.from(document.querySelectorAll('#obj-list .obj-item.kind-field .obj-title'))"
                ".some(e => e.textContent.toLowerCase().includes('oil rig'))"
            ), f"re-tapping the same subject must keep its objective. logs:\n{self._dump_logs()}")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (tap-targeted-objective) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_tapping_a_subject_works_on_mobile(self):
        """The tap-targeting path must work on an actual phone-emulated
        context too — not just a small desktop window. On mobile the scan
        tags collapse into small round "beacon" dots (see the mobile scan-tag
        CSS) and the scene uses the letterboxed "contain" media fit, both of
        which are higher-specificity CSS paths that could silently swallow
        the desktop-tuned `.targeted` look; assert the computed style (not
        just the class) actually renders the amber lock-on treatment, and
        that the underlying objective still fires."""
        page = self._new_realtime_mobile_page()
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "An oil platform.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        page.route("**/api/choose", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        page.route("**/api/detect", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({
            "objects": [
                {"label": "oil rig", "cx": 0.62, "cy": 0.45, "w": 0.15, "h": 0.2},
                {"label": "shipping container", "cx": 0.15, "cy": 0.75, "w": 0.15, "h": 0.15},
            ]})))
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isShowing() === true", timeout=15000)
            # Confirm the device layer actually resolved this as mobile (the
            # phone-only CSS paths this test cares about only apply then).
            self.assertTrue(page.evaluate("document.documentElement.classList.contains('is-mobile')"),
                             f"phone emulation must resolve to is-mobile. logs:\n{self._dump_logs()}")
            self._scan_now(page)
            page.wait_for_function("document.querySelectorAll('#scan-tags .scan-tag').length >= 2", timeout=12000)
            rig_pos = page.evaluate("""() => {
                const tag = Array.from(document.querySelectorAll('.scan-tag'))
                    .find(t => t.querySelector('.scan-tag-label').textContent === 'oil rig');
                return { x: parseFloat(tag.dataset.sx), y: parseFloat(tag.dataset.sy) };
            }""")
            self.assertIsNotNone(rig_pos)
            # Tap near (not exactly on) the beacon, same as the desktop test.
            tap_x, tap_y = rig_pos["x"], max(60, rig_pos["y"] - 55)
            page.mouse.click(tap_x, tap_y)
            page.wait_for_selector(".world-tap-ripple", timeout=1500)
            page.wait_for_function(
                "Array.from(document.querySelectorAll('#obj-list .obj-item.kind-field .obj-title'))"
                ".some(e => e.textContent.toLowerCase().includes('oil rig'))",
                timeout=10000,
            )
            page.wait_for_function("""() => {
                const tag = Array.from(document.querySelectorAll('.scan-tag'))
                    .find(t => t.querySelector('.scan-tag-label').textContent === 'oil rig');
                return !!tag && tag.classList.contains('targeted');
            }""", timeout=5000)
            # The mobile beacon CSS (higher-specificity, ID-scoped) must not
            # silently swallow the lock-on look back to the default green —
            # check the RENDERED border color, not just the class name. (Let
            # the tag's own border-color transition settle first so this
            # doesn't catch it mid-fade from the base beacon color.)
            page.wait_for_timeout(300)
            border_color = page.evaluate("""() => {
                const tag = Array.from(document.querySelectorAll('.scan-tag'))
                    .find(t => t.querySelector('.scan-tag-label').textContent === 'oil rig');
                return getComputedStyle(tag).borderColor;
            }""")
            self.assertTrue(border_color.startswith("rgb(255"),
                             f"targeted beacon must render amber, not the default green. got {border_color}. logs:\n{self._dump_logs()}")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (tap-targeted-mobile) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()
            if getattr(self, "_mobile_context", None):
                self._mobile_context.close()
                self._mobile_context = None

    def test_tapping_empty_ground_gives_an_explicit_miss_hint(self):
        """Tapping a spot with nothing detected nearby must say so explicitly
        via the scan hint instead of silently doing nothing — every world tap
        should read as either a found objective or a clear, legible miss."""
        page = self._new_realtime_page()
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "An empty field.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        page.route("**/api/choose", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        # A single subject tucked in the far corner, well clear of where we tap.
        page.route("**/api/detect", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({
            "objects": [{"label": "distant beacon", "cx": 0.04, "cy": 0.04, "w": 0.06, "h": 0.06}]})))
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isShowing() === true", timeout=15000)
            # Tap the same "clear of the wheel/objectives/rail" spot the other
            # world-tap test uses — far from the corner subject.
            page.mouse.click(430, 300)
            page.wait_for_selector(".world-tap-ripple", timeout=1500)
            page.wait_for_function(
                "document.getElementById('scan-hint') &&"
                " document.getElementById('scan-hint').textContent.includes('nothing worth investigating')",
                timeout=10000,
            )
            self.assertFalse(page.evaluate(
                "document.getElementById('scan-hint').classList.contains('hidden')"
            ), f"the miss hint must actually be shown. logs:\n{self._dump_logs()}")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (tap-miss-hint) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_scanned_hotspots_fade_out_on_their_own(self):
        """After a scan lands, the hotspots FADE OUT on their own after
        SCAN_TTL_MS so they can never go stale — the overlay tears itself down
        and the player must press SCAN again for a fresh read."""
        page = self._new_realtime_page()
        # Short TTL so the fade happens quickly (override the test-wide long TTL).
        page.add_init_script("window.__SCAN_TTL_MS__ = 700;")
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "A dock.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        page.route("**/api/choose", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        page.route("**/api/detect", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({
            "objects": [{"label": "wooden crate", "cx": 0.3, "cy": 0.4, "w": 0.2, "h": 0.2}]})))
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isShowing() === true", timeout=15000)
            self._scan_now(page)
            # Tags appear...
            page.wait_for_function("document.querySelectorAll('#scan-tags .scan-tag').length >= 1", timeout=12000)
            # ...then fade out on their own and the overlay tears down.
            page.wait_for_function("document.querySelectorAll('#scan-tags .scan-tag').length === 0", timeout=6000)
            page.wait_for_function("document.getElementById('scan-layer').classList.contains('hidden')", timeout=4000)
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (scan-fade) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_turn_recovers_when_server_never_resolves(self):
        """A committed turn must never leave the player permanently stuck on the
        progress bar. If the server turn stalls (LLM error/rate-limit, or a lost
        feed item) and no player_choice_prompt ever arrives, the client watchdog
        releases the UI: the veil clears and actionable recovery choices appear so
        the game can continue."""
        page = self._new_realtime_page()
        page.add_init_script("window.__TURN_WATCHDOG_MS__ = 1200;")  # fast watchdog for the test
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "A dock.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        # The turn NEVER resolves: /api/choose accepts the action but the feed
        # stays empty forever (no player_choice_prompt is ever delivered).
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        page.route("**/api/choose", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        page.route("**/api/detect", lambda r: r.fulfill(status=200, content_type="application/json", body='{"objects": []}'))
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isShowing() === true", timeout=15000)
            # Commit a choice from the intro prompt.
            page.wait_for_function("document.querySelectorAll('#choices-container .choice-btn').length >= 1", timeout=10000)
            page.evaluate("document.querySelector('#choices-container .choice-btn').click()")
            # Veil goes up while the (doomed) turn runs.
            page.wait_for_function("!document.getElementById('processing-veil').classList.contains('hidden')", timeout=5000)
            # The watchdog must recover: veil clears and recovery choices appear.
            page.wait_for_function(
                "document.getElementById('processing-veil').classList.contains('hidden')", timeout=6000)
            page.wait_for_function(
                "Array.from(document.querySelectorAll('#choices-container .choice-btn')).some(b => /move forward/i.test(b.textContent))",
                timeout=6000)
            self.assertFalse(
                page.evaluate("document.querySelectorAll('#choices-container .choice-btn').length === 0"),
                f"watchdog must restore actionable choices. logs:\n{self._dump_logs()}")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (turn-watchdog) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_realtime_scan_move_action(self):
        """MOVE TO on a non-enterable object composes a RELOCATION prompt (naming
        the object + a hard-transition cue so the scenery fully changes) and
        commits a full turn — no typing. MOVE always changes the scene."""
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
            # Press SCAN to surface the hotspots; click the tag to open actions.
            self._scan_now(page)
            page.wait_for_function("document.querySelectorAll('#scan-tags .scan-tag').length >= 1", timeout=12000)
            page.evaluate("document.querySelector('.scan-tag').click()")
            page.wait_for_function("!!document.querySelector('.scan-tag.acting .scan-action-move')", timeout=5000)
            page.evaluate("document.querySelector('.scan-tag.acting .scan-action-move').click()")
            # INSTANT press-ceremony: MOVE also blooms a ring pulse at the object
            # the moment it's pressed (spawned synchronously, survives the tag
            # clear that the committing turn triggers).
            self.assertGreaterEqual(
                page.evaluate("document.querySelectorAll('#scan-layer .scan-pulse.scan-pulse-move').length"), 1,
                f"MOVE must show an instant pulse at the object. logs:\n{self._dump_logs()}")
            for _ in range(60):
                if choose_bodies:
                    break
                page.wait_for_timeout(100)
            self.assertGreaterEqual(len(choose_bodies), 1, f"MOVE action must commit a turn. logs:\n{self._dump_logs()}")
            choice = (json.loads(choose_bodies[0] or "{}").get("choice") or "").strip().lower()
            # A non-enterable object -> RELOCATION phrasing ("cross over" is a
            # hard-transition trigger so the scenery fully changes), naming the
            # object. MOVE is always a full change of scenery, never a static drift.
            self.assertIn("rusty valve", choice, f"move action must name the object; got {choice!r}")
            self.assertIn("cross over", choice, f"move must cue a full change of scenery (hard transition); got {choice!r}")
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
            self._scan_now(page)
            page.wait_for_function("document.querySelectorAll('#scan-tags .scan-tag').length >= 1", timeout=12000)
            page.evaluate("document.querySelector('.scan-tag').click()")
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

    def test_scan_works_in_both_renderers_and_clears_on_switch(self):
        """The SCAN button exists in BOTH renderers. A scan is tied to one
        specific source (live video vs still), so switching renderers must tear
        the current hotspots down (they'd be mis-mapped) — the player re-scans
        the new source with the button."""
        page = self._new_realtime_page()
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "First-person VHS. A loading dock.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        page.route("**/api/detect", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({
            "objects": [{"label": "wooden crate", "cx": 0.3, "cy": 0.4, "w": 0.2, "h": 0.2}]})))
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isShowing() === true", timeout=15000)
            # The SCAN button exists in realtime; a scan brings the overlay live.
            self.assertIsNotNone(page.query_selector("#scan-btn"), "the SCAN button must exist in realtime")
            self._scan_now(page)
            page.wait_for_function("document.querySelectorAll('#scan-tags .scan-tag').length >= 1", timeout=12000)
            # Switch to still images via the renderer toggle -> overlay tears down.
            page.evaluate("document.getElementById('btn-renderer').click()")
            page.wait_for_function("document.body.classList.contains('realtime-on') === false", timeout=5000)
            page.wait_for_function("document.getElementById('scan-layer').classList.contains('hidden')", timeout=5000)
            # ...and the SCAN button is still there in stills mode.
            self.assertIsNotNone(page.query_selector("#scan-btn"), "the SCAN button must exist in stills mode too")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (scan-switch) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_scan_tool_works_in_stills_mode(self):
        """SCAN must also work with the still-image renderer: pressing it detects
        the current still (not a video), the tags appear, and clicking a tag
        commits a full turn anchored on that object."""
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
            page.wait_for_selector("#scan-layer", state="attached", timeout=10000)
            # The still must be on screen (a scene background is set).
            page.wait_for_function(
                "(document.getElementById('sceneA').style.backgroundImage||"
                "document.getElementById('sceneB').style.backgroundImage||'').length > 0",
                timeout=10000,
            )
            # Press SCAN -> it scans the STILL and the tags appear.
            self._scan_now(page)
            page.wait_for_function("document.querySelectorAll('#scan-tags .scan-tag').length >= 1", timeout=12000)
            self.assertGreaterEqual(len(detects), 1, "SCAN never called /api/detect in stills mode")
            labels = page.evaluate("Array.from(document.querySelectorAll('.scan-tag-label')).map(e=>e.textContent)")
            self.assertIn("steel door", labels)
            # Click the tag, tap INTERACT -> commits a full turn on the object.
            page.evaluate("document.querySelector('.scan-tag').click()")
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

    def test_stills_hotspots_refresh_after_a_turn(self):
        """The 'works well in stills mode' guarantee: after a turn changes the
        still, the old scene's hotspots are dropped and a fresh SCAN on the new
        still surfaces the NEW scene's hotspots (the old labels must not persist).
        Uses a NEW image + different detected objects for the new scene."""
        page = self._new_realtime_page()
        SCENE_B_PNG = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lK3Q6wAAAABJRU5ErkJggg=="
        )
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "A dim corridor.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        # After the turn, the feed delivers a NEW still + a fresh prompt.
        next_items = [
            {"id": 10, "type": "scene_image", "content": "", "image_url": SCENE_B_PNG,
             "metadata": {"prompt": "scene two", "base": "A flooded vault.", "hard_transition": True}},
            {"id": 11, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Onward"}]},
        ]
        state = {"chose": False, "detect_calls": 0}
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))

        def feed_handler(route):
            body = json.dumps(next_items) if state["chose"] else "[]"
            route.fulfill(status=200, content_type="application/json", body=body)
        page.route("**/api/feed*", feed_handler)

        def choose_handler(route):
            state["chose"] = True
            route.fulfill(status=200, content_type="application/json", body="[]")
        page.route("**/api/choose", choose_handler)

        def detect_handler(route):
            state["detect_calls"] += 1
            # Different objects before vs after the turn, proving re-detection.
            label = "old crate" if not state["chose"] else "new lantern"
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "objects": [{"label": label, "cx": 0.4, "cy": 0.45, "w": 0.2, "h": 0.2}]}))
        page.route("**/api/detect", detect_handler)
        try:
            page.goto(f"{self.base_url}/standalone?renderer=image", wait_until="domcontentloaded")
            page.wait_for_selector("#scan-layer", state="attached", timeout=10000)
            page.wait_for_function(
                "(document.getElementById('sceneA').style.backgroundImage||"
                "document.getElementById('sceneB').style.backgroundImage||'').length > 0", timeout=10000)
            # Scan scene one -> its hotspot appears.
            self._scan_now(page)
            page.wait_for_function(
                "Array.from(document.querySelectorAll('.scan-tag-label')).some(e=>e.textContent==='old crate')",
                timeout=12000)
            # Commit an action on it -> turn resolves via the feed's new scene.
            page.evaluate("document.querySelector('.scan-tag').click()")
            page.wait_for_function("!!document.querySelector('.scan-tag.acting .scan-action-interact')", timeout=5000)
            page.evaluate("document.querySelector('.scan-tag.acting .scan-action-interact').click()")
            # The new still lands; the old crate hotspot is dropped with the scene.
            page.wait_for_function(
                "!Array.from(document.querySelectorAll('.scan-tag-label')).some(e=>e.textContent==='old crate')",
                timeout=15000)
            # Scan the NEW scene -> its fresh hotspot appears (re-detected on demand).
            self._scan_now(page)
            page.wait_for_function(
                "Array.from(document.querySelectorAll('.scan-tag-label')).some(e=>e.textContent==='new lantern')",
                timeout=15000)
            # And the stale label must be gone.
            self.assertNotIn("old crate", page.evaluate(
                "Array.from(document.querySelectorAll('.scan-tag-label')).map(e=>e.textContent)"),
                f"stale hotspot must not persist onto the new scene. logs:\n{self._dump_logs()}")
        except Exception:
            print("\n=== CONSOLE LOG (stills-refresh) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_scan_action_clears_tags_for_the_turn(self):
        """Committing a TURN-resolving hotspot action (MOVE TO) must clear the
        stale labels while the turn plays out (they shouldn't hover over a scene
        that's about to change). The player re-scans the new scene once it
        settles. (INTERACT is a live in-place poke and deliberately keeps its
        tag; it's MOVE that changes the scene and clears them.)"""
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
            self._scan_now(page)
            page.wait_for_function("document.querySelectorAll('#scan-tags .scan-tag').length >= 1", timeout=12000)
            # Click a tag, tap MOVE TO (a turn-resolving action) -> tags must
            # clear for the turn.
            page.evaluate("document.querySelector('.scan-tag').click()")
            page.wait_for_function("!!document.querySelector('.scan-tag.acting .scan-action-move')", timeout=5000)
            page.evaluate("document.querySelector('.scan-tag.acting .scan-action-move').click()")
            # The stale labels are gone while the turn resolves.
            page.wait_for_function("document.querySelectorAll('#scan-tags .scan-tag').length === 0", timeout=6000)
        except Exception:
            print("\n=== CONSOLE LOG (scan-clear) ===\n" + self._dump_logs())
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
            self.assertIn("start_travel", cmds, f"realtime never started from the feed. logs:\n{self._dump_logs()}")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (feed_scene_image) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_static_burst_masks_reveal_not_teardown(self):
        """Staging/timing contract: the VCR static burst must be tied to the
        ACTUAL visible switch (freeze->video reveal / 'video_showing'), NOT to the
        realtime teardown (the world rebuild). Firing it at teardown put the
        static seconds before the real switch — 'static, then a held still, then
        an abrupt jump to video'. A re-anchor here must NOT flash static between
        the rebuild and the reveal; the burst must land at (or after) the reveal."""
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
                        // Happy Oyster tears down by building a NEW world
                        // (create_world), not a reset-in-place.
                        if (name === 'command_sent' && data && data.command === 'create_world') window.__EVT_TS__.rebuild = t;
                        else if (name === 'video_showing') window.__EVT_TS__.video_showing = t;
                        if (typeof prev === 'function') prev(name, data);
                    };
                }"""
            )
            # Clear any burst from scene one's own reveal, then re-anchor onto a
            # DIFFERENT guide image (rebuild -> re-travel -> reveal).
            page.evaluate("() => { window.__BURST_TS__ = []; }")
            page.evaluate(
                "() => window.ReactorRenderer.applyScene({prompt: 'scene two', imageUrl: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==', hardTransition: false})"
            )
            # Wait for the re-anchor's world rebuild AND its subsequent reveal.
            page.wait_for_function("window.__EVT_TS__ && window.__EVT_TS__.rebuild != null", timeout=10000)
            page.wait_for_function("window.__EVT_TS__ && window.__EVT_TS__.video_showing != null", timeout=15000)
            # Let any (erroneous) teardown-time burst register.
            page.wait_for_timeout(150)

            evt = page.evaluate("window.__EVT_TS__")
            bursts = page.evaluate("window.__BURST_TS__ || []")
            rebuild_t = evt.get("rebuild")
            reveal_t = evt.get("video_showing")
            self.assertIsNotNone(rebuild_t, "re-anchor never rebuilt the world")
            self.assertIsNotNone(reveal_t, "re-anchor never revealed the video")
            # The reveal must genuinely come AFTER teardown (there IS a warmup gap).
            self.assertGreater(reveal_t, rebuild_t, "reveal did not follow the rebuild")
            # No static burst may fire in the teardown->reveal warmup window: the
            # burst must mask the reveal, so its first occurrence is at/after it
            # (small epsilon for the event/DOM-mutation ordering at reveal time).
            premature = [t for t in bursts if t < reveal_t - 40]
            self.assertEqual(
                premature, [],
                f"static burst fired before the reveal (masking nothing): "
                f"rebuild@{rebuild_t:.0f} reveal@{reveal_t:.0f} bursts={bursts}\n{self._dump_logs()}",
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

    def _seed_live_scene(self, page):
        """Boot /realtime, apply a first scene, and wait for the live video."""
        page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
        page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isReady() === true", timeout=15000)
        page.evaluate(
            "(img) => window.ReactorRenderer.applyScene({prompt: 'A dim loading dock you can walk through', imageUrl: img, hardTransition: false})",
            TINY_PNG_DATA_URL,
        )
        page.wait_for_function("window.ReactorRenderer.isShowing() === true", timeout=15000)

    def test_interaction_verb_bar_fires_verbs(self):
        """Happy Oyster's interaction verbs are a real UX: the verb bar shows the
        built-in survival set PLUS the verbs THIS world advertises (travel_state),
        a momentary verb (Jump) fires interact({action}), and a held verb (Sprint)
        engages on press and releases (stop) on let-go."""
        page = self._new_realtime_page()
        try:
            self._seed_live_scene(page)
            # The verb bar appears with real verbs (built-ins + advertised "Open").
            page.wait_for_function("document.querySelectorAll('#verb-bar .verb-btn').length >= 1", timeout=8000)
            verbs = page.evaluate(
                "Array.from(document.querySelectorAll('#verb-bar .verb-btn')).map(b => b.dataset.verb)")
            for v in ["Jump", "Attack", "Crouch", "Sprint", "Open"]:
                self.assertIn(v, verbs, f"verb bar missing {v}: {verbs}")

            # Momentary verb: tapping Jump fires interact({action:'Jump'}).
            page.evaluate("window.__MOCK_CMD_LOG__ = []")
            page.evaluate("document.querySelector('#verb-bar .verb-btn[data-verb=\"Jump\"]').click()")
            page.wait_for_function(
                """() => (window.__MOCK_CMD_LOG__||[]).some(c => c.name==='interact' && c.data.action==='Jump')""",
                timeout=5000)

            # Held verb: pressing Sprint engages interact({action:'Sprint'});
            # releasing it issues a stop (held controls released).
            page.evaluate("window.__MOCK_CMD_LOG__ = []")
            page.evaluate(
                """() => { const b = document.querySelector('#verb-bar .verb-btn[data-verb=\"Sprint\"]');
                    b.dispatchEvent(new PointerEvent('pointerdown', {pointerId: 21, cancelable:true, bubbles:true})); }""")
            page.wait_for_function(
                """() => (window.__MOCK_CMD_LOG__||[]).some(c => c.name==='interact' && c.data.action==='Sprint')""",
                timeout=5000)
            page.evaluate(
                """() => { const b = document.querySelector('#verb-bar .verb-btn[data-verb=\"Sprint\"]');
                    b.dispatchEvent(new PointerEvent('pointerup', {pointerId: 21, cancelable:true, bubbles:true})); }""")
            page.wait_for_function(
                """() => (window.__MOCK_CMD_LOG__||[]).some(c => c.name==='stop')""", timeout=5000)
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (verb-bar) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_happy_oyster_perspective_and_experience_options(self):
        """The two session-fixed Happy Oyster knobs are exposed + functional:
        switching VIEW to third-person rebuilds the world (create_world with
        perspective:third_person), and switching MODE to Director rebuilds it as a
        Directing world (create_world with resolution/layout/narrative, no
        perspective)."""
        page = self._new_realtime_page()
        try:
            self._seed_live_scene(page)
            # Open the WORLD MODEL panel; the Happy Oyster options are shown.
            page.click("#menu-toggle")
            page.click("#btn-model")
            page.wait_for_function("!document.getElementById('rt-ho-opts').classList.contains('hidden')", timeout=5000)

            # Switch perspective -> third person: the world rebuilds with it.
            page.evaluate("window.__MOCK_CMD_LOG__ = []")
            page.evaluate("document.querySelector('#rt-ho-perspective .rt-ho-btn[data-value=\"third_person\"]').click()")
            page.wait_for_function(
                """() => (window.__MOCK_CMD_LOG__||[]).some(c => c.name==='create_world' && c.data.perspective==='third_person')""",
                timeout=8000)
            # Let the rebuilt world go fully live (travelling) before the next toggle.
            page.wait_for_function("window.ReactorRenderer.isShowing() === true", timeout=15000)

            # Switch experience -> Director: rebuilds as a Directing world (director
            # params present, perspective absent).
            page.evaluate("window.__MOCK_CMD_LOG__ = []")
            page.evaluate("document.querySelector('#rt-ho-experience .rt-ho-btn[data-value=\"director\"]').click()")
            page.wait_for_function(
                """() => (window.__MOCK_CMD_LOG__||[]).some(c => c.name==='create_world' && c.data.resolution && c.data.layout && c.data.narrative && !c.data.perspective)""",
                timeout=8000)
            # Director hides the Adventure-only joystick.
            page.wait_for_function("document.body.classList.contains('ho-director')", timeout=4000)
            self.assertEqual(
                page.evaluate("getComputedStyle(document.getElementById('move-pad')).display"), "none",
                "the joystick must hide in the Director experience")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (ho-options) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_world_drift_resteers_without_restaging(self):
        """Ambient world drift must keep the live stream EVOLVING between turns
        without tearing the world down.

        The turn loop used to be the only thing that spoke to the world model, so
        a long deliberation sat on one frozen prompt. Drift is a prompt-only
        hot-swap (set_prompt) on models that support live steering — never a
        reset/create_world, which is what made an atmospheric beat look like a
        black re-anchor. Happy Oyster Adventure is fixed once built, so drift
        must refuse there rather than rebuild on a timer.
        """
        page = self._new_realtime_page()
        try:
            # Speed the ask loop up for the test; the SERVER still owns pacing.
            page.add_init_script("window.__WORLD_DRIFT_ASK_MS__ = 250;")
            self._seed_live_scene(page)

            # WorldDrift is wired at boot.
            self.assertTrue(page.evaluate("!!window.__WorldDrift"),
                            "WorldDrift module must be exposed for the idle ask loop")
            # Happy Oyster Adventure: a new prompt rebuilds the world — drift
            # must refuse rather than rebuild on a timer.
            self.assertFalse(
                page.evaluate("window.ReactorRenderer.supportsLiveSteer()"),
                "Happy Oyster Adventure must report supportsLiveSteer=false")
            self.assertFalse(
                page.evaluate("window.__WorldDrift.idle()"),
                "WorldDrift.idle() must be false on a non-steerable adventure world")
            applied = page.evaluate(
                """() => window.Renderer.applyDrift({
                    prompt: 'A dim loading dock. Dust settles on the floor.',
                    base: 'A dim loading dock you can walk through',
                    drift: true, hard_transition: false,
                })""")
            self.assertFalse(applied, "applyDrift must refuse on Happy Oyster Adventure")

            # Switch to LingBot (seed_locked): live set_prompt is the contract.
            page.evaluate("window.Renderer.setWorldModel('lingbot-world-2')")
            page.wait_for_function(
                "window.ReactorRenderer.getModel() === 'lingbot-world-2'", timeout=8000)
            page.wait_for_function("window.ReactorRenderer.isReady() === true", timeout=15000)
            page.evaluate(
                "(img) => window.ReactorRenderer.applyScene({prompt: 'First-person VHS corridor', imageUrl: img, hardTransition: false})",
                TINY_PNG_DATA_URL,
            )
            page.wait_for_function("window.ReactorRenderer.isShowing() === true", timeout=15000)
            self.assertTrue(
                page.evaluate("window.ReactorRenderer.supportsLiveSteer()"),
                "LingBot must support live prompt steering")

            # Seed the standalone scene bible so applyDrift has a base to build on.
            page.evaluate(
                """() => {
                    window.Renderer.lastBase = 'First-person VHS corridor';
                    window.Renderer.lastScene = {
                        prompt: 'First-person VHS corridor',
                        imageUrl: null, hardTransition: false,
                    };
                }""")
            page.evaluate("window.__MOCK_CMD_LOG__ = []; window.__MOCK_CMDS__ = []")
            page.evaluate(
                """() => window.Renderer.applyDrift({
                    prompt: 'First-person VHS corridor Dust settles across the floor.',
                    base: 'First-person VHS corridor',
                    drift: true, hard_transition: false,
                })""")
            page.wait_for_function(
                """() => (window.__MOCK_CMD_LOG__||[]).some(
                    c => c.name === 'set_prompt'
                      && (c.data.prompt||'').indexOf('Dust settles') >= 0)""",
                timeout=8000)
            cmds = page.evaluate("window.__MOCK_CMDS__ || []")
            self.assertIn("set_prompt", cmds)
            self.assertNotIn("reset", cmds,
                             "a drift must NOT reset/re-stage a seed-locked world")
            self.assertNotIn("create_world", cmds,
                             "a drift must NOT rebuild a Happy Oyster world")

            # The feed path must take the same applyDrift shortcut — a world_drift
            # item routed through the normal scene path would re-stage.
            page.evaluate("window.__MOCK_CMD_LOG__ = []; window.__MOCK_CMDS__ = []")
            page.evaluate(
                """() => {
                    // Reach the feed renderer the same way pollOnce does.
                    const item = {
                        id: 900001,
                        type: 'world_drift',
                        content: 'A door slams deeper in the facility.',
                        metadata: {
                            prompt: 'First-person VHS corridor A door slams deeper in the facility.',
                            base: 'First-person VHS corridor',
                            drift: true, hard_transition: false,
                        },
                    };
                    // renderItem is closed over; drive through the public path the
                    // poller uses by appending via the same applyDrift the case
                    // above already proved, then assert the feed handler wires it.
                    // Source-level: WorldDrift + renderItem world_drift branch are
                    // covered by test_world_drift.TestClientWiring; here we just
                    // confirm the live apply stays prompt-only.
                    window.Renderer.applyDrift(item.metadata);
                }""")
            page.wait_for_function(
                """() => (window.__MOCK_CMD_LOG__||[]).some(
                    c => c.name === 'set_prompt'
                      && (c.data.prompt||'').indexOf('door slams') >= 0)""",
                timeout=8000)
            cmds = page.evaluate("window.__MOCK_CMDS__ || []")
            self.assertNotIn("reset", cmds)
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (world-drift) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_attach_world_reused_on_revisit(self):
        """attach_world is genuinely utilized: revisiting a scene whose world we
        already built (same guide image) reopens it with attach_world instead of
        regenerating with create_world."""
        page = self._new_realtime_page()
        try:
            page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
            page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isReady() === true", timeout=15000)
            img_a = TINY_PNG_DATA_URL
            img_b = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
            # (evaluate passes ONE arg — destructure [prompt, img] from it.)
            apply = "(a) => window.ReactorRenderer.applyScene({prompt: a[0], imageUrl: a[1], hardTransition: false})"
            # Scene A -> builds world #1.
            page.evaluate(apply, ["scene A", img_a])
            page.wait_for_function("window.ReactorRenderer.isShowing() === true", timeout=15000)
            page.wait_for_function("(window.__MOCK_CMDS__||[]).filter(c=>c==='create_world').length >= 1", timeout=8000)
            # Scene B -> builds world #2.
            page.evaluate(apply, ["scene B", img_b])
            page.wait_for_function("(window.__MOCK_CMDS__||[]).filter(c=>c==='create_world').length >= 2", timeout=10000)
            page.wait_for_function("window.ReactorRenderer.isShowing() === true", timeout=15000)
            # Revisit Scene A (SAME prompt + guide image) -> must ATTACH the saved
            # world, not build a third one. (A different prompt at the same image
            # would correctly rebuild — that's a narrative update, tested below.)
            page.evaluate("window.__MOCK_ATTACHES__ = 0")
            page.evaluate(apply, ["scene A", img_a])
            page.wait_for_function("(window.__MOCK_ATTACHES__||0) >= 1", timeout=10000)
            page.wait_for_function("window.ReactorRenderer.isShowing() === true", timeout=15000)
            builds = page.evaluate("(window.__MOCK_CMDS__||[]).filter(c=>c==='create_world').length")
            self.assertEqual(builds, 2, "revisit must not build a new world (should attach)")

            # A NEW prompt at the SAME image is a narrative update, not a revisit:
            # it must REBUILD (create_world), never reopen the stale world.
            page.evaluate("window.__MOCK_ATTACHES__ = 0")
            page.evaluate(apply, ["scene A but the lights have gone out", img_a])
            page.wait_for_function("(window.__MOCK_CMDS__||[]).filter(c=>c==='create_world').length >= 3", timeout=10000)
            self.assertEqual(page.evaluate("(window.__MOCK_ATTACHES__||0)"), 0,
                             "a changed prompt at the same image must rebuild, not attach the stale world")
        except Exception:
            print("\n=== REACTOR CONSOLE LOG (attach-revisit) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
