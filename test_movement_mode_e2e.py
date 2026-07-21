"""
test_movement_mode_e2e.py — Playwright end-to-end test for the WSAD / joystick
MOVEMENT ("explore") mode that navigates the realtime (Reactor Happy Oyster)
world like a first-person camera.

It reuses the same harness as test_realtime_e2e.py — run the REAL client JS
against a MOCK Reactor SDK — and proves the movement instrument end to end:

  * The joystick is the CENTER of the action cluster in realtime video mode,
    with the ACT hub pushed to the LEFT and the PHOTO hub to the RIGHT.
  * Holding W (keyboard) drives the LIVE world with Happy Oyster's held move
    command (move {direction:"Front"}) — a native navigation command, no world
    rebuild — and releasing it stops (releases held controls).
  * A / S / D (and the arrow keys) drive the other headings — S=Back, A/D turn
    the view (look Mouse_Left/Mouse_Right).
  * Dragging the stick with a pointer (mouse/touch) in a direction moves that
    way, and the commands land in the same reactor command log the world reads.
  * The legacy LingBot axes (set_move_*/set_rotation_speed_deg) are still driven
    when that model is selected.
  * In still-image mode the joystick is hidden and the keys do not steer.

Run with:
    python3 -m pytest test_movement_mode_e2e.py -v
"""

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.resolve()

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# Reuse the mock SDK + helpers from the realtime e2e so the two stay in lockstep.
from test_realtime_e2e import (  # noqa: E402
    MOCK_SDK_JS,
    TINY_PNG_DATA_URL,
    _find_free_port,
    _wait_for_health,
)


# JS run in the page to: seed a scene "bible" (so a camera re-steer has a base to
# build on), spy on every prompt sent to the world model, and drive the first
# scene so the live video reveals. Returns nothing; the test waits on isShowing.
SEED_AND_SPY = """
(img) => {
  const R = window.__Renderer;
  const base = 'First-person VHS. A dark flooded drainage tunnel, vein-like growth on the walls.';
  R.lastBase = base;
  R.lastScene = { prompt: base, imageUrl: img, hardTransition: false };
  window.__moves = [];
  const orig = window.ReactorRenderer.applyScene.bind(window.ReactorRenderer);
  window.ReactorRenderer.applyScene = (s) => {
    try { if (s && s.prompt) window.__moves.push(s.prompt); } catch (e) {}
    return orig(s);
  };
  orig({ prompt: base, imageUrl: img, hardTransition: false });
}
"""


@unittest.skipUnless(PLAYWRIGHT_AVAILABLE, "playwright not installed — see requirements-dev.txt")
class TestMovementMode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _find_free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"

        env = os.environ.copy()
        env["GEMINI_API_KEY"] = ""
        env["OPENAI_API_KEY"] = ""
        env["ANTHROPIC_API_KEY"] = ""
        env["REACTOR_API_KEY"] = "test-key-not-used"
        env["SCENE_RENDERER"] = "reactor"

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
        page = self.browser.new_page(viewport={"width": 1100, "height": 800})
        self._logs = []
        page.on("console", lambda m: self._logs.append(f"{m.type}: {m.text}"))
        page.on("pageerror", lambda e: self._logs.append(f"PAGEERROR: {e}"))
        page.route(
            "https://esm.sh/**",
            lambda route: route.fulfill(status=200, content_type="application/javascript", body=MOCK_SDK_JS),
        )
        page.route(
            "**/api/reactor/token",
            lambda route: route.fulfill(
                status=200, content_type="application/json",
                body='{"jwt": "mock.jwt.token", "expires_at": 9999999999}',
            ),
        )
        return page

    def _dump_logs(self):
        return "\n".join(self._logs[-60:])

    def _boot_live(self, page, model=None):
        """Load /realtime, connect the mock world model, and reveal live video.

        `model` optionally forces a specific world model (via ?model=), e.g.
        "lingbot-world-2" to exercise the legacy axis navigation."""
        url = f"{self.base_url}/realtime"
        if model:
            url += f"?model={model}"
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_function("window.ReactorRenderer && window.ReactorRenderer.isReady() === true", timeout=15000)
        page.wait_for_function("window.__Renderer !== undefined", timeout=10000)
        page.evaluate(SEED_AND_SPY, TINY_PNG_DATA_URL)
        page.wait_for_function("window.ReactorRenderer.isShowing() === true", timeout=15000)
        # Simulate the idle state the app reaches once a turn resolves (hideVeil):
        # the boot gate lifts and the action cluster becomes interactive. Our test
        # boots via the reactor facade directly, bypassing the feed that normally
        # clears these, so clear them here.
        page.evaluate(
            "() => { document.body.classList.remove('awaiting-first-scene');"
            " const w = document.getElementById('action-wheel'); if (w) w.classList.remove('turn-active'); }"
        )

    def _reset_cmd_log(self, page):
        page.evaluate("() => { window.__MOCK_CMD_LOG__ = []; }")

    def _wait_cmd(self, page, name, param=None, value=None, timeout=6000):
        page.wait_for_function(
            """([n,p,v]) => (window.__MOCK_CMD_LOG__||[]).some(
                   c => c.name===n && (p===null || c.data[p]===v))""",
            arg=[name, param, value], timeout=timeout,
        )

    def test_wasd_keys_drive_forward_back_and_look(self):
        """The FULL Happy Oyster navigation surface: W/S move forward/back (move
        Front/Back), A/D turn (look Mouse_Left/Right), Q/E strafe (move
        Left/Right), ←/→ turn (look Mouse_Left/Right), and ↑/↓ tilt the view (look
        Mouse_Up/Down). Each fires the native held command and releases (stop) on
        key-up; nothing is faked with set_prompt."""
        page = self._new_realtime_page()
        try:
            self._boot_live(page)
            # key -> (command, param, held direction)
            cases = [
                ("w", "move", "direction", "Front"),
                ("s", "move", "direction", "Back"),
                ("a", "look", "direction", "Mouse_Left"),
                ("d", "look", "direction", "Mouse_Right"),
                ("q", "move", "direction", "Left"),
                ("e", "move", "direction", "Right"),
                ("ArrowUp", "look", "direction", "Mouse_Up"),
                ("ArrowDown", "look", "direction", "Mouse_Down"),
                ("ArrowLeft", "look", "direction", "Mouse_Left"),
                ("ArrowRight", "look", "direction", "Mouse_Right"),
            ]
            for key, cmd, param, value in cases:
                self._reset_cmd_log(page)
                page.keyboard.down(key)
                self._wait_cmd(page, cmd, param, value)
                page.keyboard.up(key)
                # Held control MUST be released on key-up (a single `stop`), or
                # the camera keeps going.
                self._wait_cmd(page, "stop")
            # Movement uses native move/look verbs, never a set_prompt fake.
            log = page.evaluate("() => window.__MOCK_CMD_LOG__ || []")
            self.assertTrue(all(c["name"] != "set_prompt" for c in log),
                            f"drive should not use set_prompt. log:\n{log}")
        except Exception:
            print("\n=== CONSOLE LOG (drive-keys) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_looking_sends_a_gentle_constant_rotation_speed(self):
        """Legacy LingBot navigation: holding a look key must set a GENTLE,
        CONSTANT rotation speed (no hold-time acceleration — that was
        disorienting/hard to aim), well under the model default of 5. (Happy
        Oyster has no turn-rate knob, so this is exercised on lingbot-world-2.)"""
        page = self._new_realtime_page()
        try:
            self._boot_live(page, model="lingbot-world-2")
            self._reset_cmd_log(page)
            page.keyboard.down("a")
            self._wait_cmd(page, "set_rotation_speed_deg")
            page.wait_for_timeout(1500)  # hold a while — speed must NOT creep up
            page.keyboard.up("a")
            speeds = page.evaluate(
                """() => (window.__MOCK_CMD_LOG__||[])
                       .filter(c => c.name==='set_rotation_speed_deg')
                       .map(c => c.data.rotation_speed_deg)"""
            )
            self.assertTrue(len(speeds) >= 1, f"no rotation speed sent. speeds={speeds}")
            # Bounded within the model's allowed range (0..30), not runaway.
            self.assertLessEqual(max(speeds), 6.0, f"look speed out of expected range: {speeds}")
            # Constant: holding must not accelerate the look.
            self.assertLessEqual(max(speeds) - min(speeds), 0.5, f"look speed should be constant: {speeds}")
        except Exception:
            print("\n=== CONSOLE LOG (rotation) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_pointer_drag_drives_and_looks(self):
        """Dragging the stick drives + steers on Happy Oyster: up = move Front,
        left = look Mouse_Left, releasing (stop) on let-go."""
        page = self._new_realtime_page()
        try:
            self._boot_live(page)
            box = page.evaluate(
                """() => { const r = document.getElementById('move-pad').getBoundingClientRect();
                           return { cx: r.left + r.width/2, cy: r.top + r.height/2, r: r.width/2 }; }"""
            )
            # Drag straight up -> forward.
            self._reset_cmd_log(page)
            page.mouse.move(box["cx"], box["cy"])
            page.mouse.down()
            page.mouse.move(box["cx"], box["cy"] - box["r"], steps=6)
            self._wait_cmd(page, "move", "direction", "Front")
            self.assertTrue(page.evaluate("() => document.getElementById('move-pad').classList.contains('engaged')"))
            # Drag to the left -> look left.
            page.mouse.move(box["cx"] - box["r"], box["cy"], steps=6)
            self._wait_cmd(page, "look", "direction", "Mouse_Left")
            page.mouse.up()
            # Release stops the held controls.
            self._wait_cmd(page, "stop")
            page.wait_for_function("() => !document.getElementById('move-pad').classList.contains('engaged')", timeout=4000)
        except Exception:
            print("\n=== CONSOLE LOG (pointer) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_movement_fires_even_when_caps_omit_the_axes(self):
        """Production repro: the SDK advertises a capability list that does NOT
        include the movement commands. The family bypass must still send the
        native command (this is the bug where 'I could never move'). Exercised on
        Happy Oyster (the default) — its move/look/stop are always allowed for
        the family even when caps omit them."""
        page = self._new_realtime_page()
        page.add_init_script(
            "window.__MOCK_CAPS__ = { commands: "
            "['set_prompt','set_image','set_seed','start','pause','resume','reset'], "
            "tracks: [{ name: 'main_video', kind: 'video', direction: 'recvonly' }] };"
        )
        try:
            self._boot_live(page)
            # Capabilities are known and omit movement — yet motion is supported
            # for the Happy Oyster family, and the command must actually be sent.
            self.assertTrue(page.evaluate("() => window.ReactorRenderer.motionSupported()"))
            self._reset_cmd_log(page)
            page.keyboard.down("w")
            self._wait_cmd(page, "move", "direction", "Front")
            page.keyboard.up("w")
            self._wait_cmd(page, "stop")
        except Exception:
            print("\n=== CONSOLE LOG (caps-omit) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_joystick_is_centered_with_act_left_and_photo_right(self):
        """Layout contract: in realtime video mode the joystick is the visible
        CENTER of the action cluster, ACT sits to its LEFT, PHOTO to its RIGHT."""
        page = self._new_realtime_page()
        try:
            self._boot_live(page)
            self.assertTrue(page.evaluate("() => document.body.classList.contains('realtime-on')"))
            geo = page.evaluate(
                """() => {
                    const c = (id) => { const e = document.getElementById(id);
                        const r = e.getBoundingClientRect();
                        const s = getComputedStyle(e);
                        return { x: r.left + r.width/2, shown: s.display !== 'none' && r.width > 0 }; };
                    return { pad: c('move-pad'), act: c('free-will-btn'), photo: c('realtime-btn') };
                }"""
            )
            self.assertTrue(geo["pad"]["shown"], "joystick not visible in realtime mode")
            self.assertTrue(geo["act"]["shown"] and geo["photo"]["shown"])
            self.assertLess(geo["act"]["x"], geo["pad"]["x"], "ACT should be LEFT of the joystick")
            self.assertLess(geo["pad"]["x"], geo["photo"]["x"], "PHOTO should be RIGHT of the joystick")
            # Joystick is centered on the viewport (within a small tolerance).
            center = page.evaluate("() => window.innerWidth / 2")
            self.assertLess(abs(geo["pad"]["x"] - center), 6, "joystick should be centered")
        except Exception:
            print("\n=== CONSOLE LOG (layout) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_ocr_hotspots_hide_while_moving_and_regenerate_on_stop(self):
        """The OCR hotspots (and the choices grounded on them) are inaccurate
        while the camera travels: they must hide the moment movement starts, no
        detection runs while moving, and once you stop they re-detect and reappear."""
        page = self._new_realtime_page()
        # Fast settle + no turn cooldown so the post-stop re-detect fires quickly.
        page.add_init_script("window.__MOVE_SETTLE_MS__ = 250; window.__SCAN_TURN_COOLDOWN_MS__ = 0;")
        scene_items = [
            {"id": 1, "type": "narrative", "content": "Intro."},
            {"id": 2, "type": "scene_image", "content": "", "image_url": TINY_PNG_DATA_URL,
             "metadata": {"prompt": "scene one", "base": "First-person VHS. A loading dock.", "hard_transition": False}},
            {"id": 3, "type": "player_choice_prompt", "content": "?", "choices": [{"text": "Go"}]},
        ]
        detects = []
        page.route("**/api/reset", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(scene_items)))
        page.route("**/api/feed*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
        page.route("**/api/choose", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))

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
            # Hotspots appear on their own once the scene is on screen.
            page.wait_for_function("document.querySelectorAll('#scan-tags .scan-tag').length >= 1", timeout=12000)
            self.assertFalse(page.evaluate("document.getElementById('scan-layer').classList.contains('hidden')"))

            # Start moving -> hotspots hide (body.moving, scan-layer hidden).
            page.keyboard.down("w")
            page.wait_for_function("document.body.classList.contains('moving')", timeout=4000)
            page.wait_for_function("document.getElementById('scan-layer').classList.contains('hidden')", timeout=4000)

            # No detection runs while moving.
            before = len(detects)
            page.wait_for_timeout(700)
            self.assertEqual(len(detects), before, "detection must not run while the camera moves")

            # Stop -> movement clears, and hotspots re-detect + reappear.
            page.keyboard.up("w")
            page.wait_for_function("!document.body.classList.contains('moving')", timeout=4000)
            page.wait_for_function("document.querySelectorAll('#scan-tags .scan-tag').length >= 1", timeout=8000)
            self.assertFalse(page.evaluate("document.getElementById('scan-layer').classList.contains('hidden')"))
            self.assertGreater(len(detects), before, "hotspots must regenerate (re-detect) after stopping")
        except Exception:
            print("\n=== CONSOLE LOG (hotspots-while-moving) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_still_mode_hides_joystick_and_disables_keys(self):
        """In still-image mode there is nothing to steer: the joystick is hidden
        and W/A/S/D do not re-steer anything."""
        page = self._new_realtime_page()
        try:
            self._boot_live(page)
            page.evaluate("() => window.__Renderer.setMode('image')")
            page.wait_for_function("() => !document.body.classList.contains('realtime-on')", timeout=5000)
            shown = page.evaluate(
                "() => { const e = document.getElementById('move-pad'); return getComputedStyle(e).display !== 'none'; }"
            )
            self.assertFalse(shown, "joystick should be hidden in still mode")
            self.assertFalse(page.evaluate("() => window.__Movement.enabled()"))
            self._reset_cmd_log(page)
            page.keyboard.down("w")
            page.wait_for_timeout(600)
            page.keyboard.up("w")
            move_cmds = page.evaluate(
                """() => (window.__MOCK_CMD_LOG__||[]).filter(c =>
                       ['move','look','stop'].includes(c.name) ||
                       c.name.indexOf('set_move') === 0 || c.name.indexOf('set_look') === 0)"""
            )
            self.assertEqual(move_cmds, [], f"keys must not drive in still mode, got: {move_cmds}")
        except Exception:
            print("\n=== CONSOLE LOG (still) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
