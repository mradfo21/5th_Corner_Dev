"""
test_movement_mode_e2e.py — Playwright end-to-end test for the WSAD / joystick
MOVEMENT ("explore") mode that steers the realtime (Reactor / LingBot World 2)
video like a first-person camera.

It reuses the same harness as test_realtime_e2e.py — run the REAL client JS
against a MOCK Reactor SDK — and proves the movement instrument end to end:

  * The joystick is the CENTER of the action cluster in realtime video mode,
    with the ACT hub pushed to the LEFT and the PHOTO hub to the RIGHT.
  * Holding W (keyboard) re-steers the LIVE stream with a first-person camera
    beat ("Camera: the camera pushes forward…") — a prompt hot-swap, no new
    guide image — and releasing it brings the camera to a halt.
  * A / S / D (and the arrow keys) drive the other headings.
  * Dragging the stick with a pointer (mouse/touch) in a direction moves that
    way (360°), and the steers land in the same reactor set_prompt log the world
    model reads.
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

    def _boot_live(self, page):
        """Load /realtime, connect the mock world model, and reveal live video."""
        page.goto(f"{self.base_url}/realtime", wait_until="domcontentloaded")
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

    def _camera_moves(self, page):
        return page.evaluate("() => (window.__moves || []).filter(p => p.indexOf('Camera:') >= 0)")

    def test_wasd_keys_steer_the_camera(self):
        """Each of W/A/S/D must steer the live video toward the matching heading
        with a first-person camera re-steer, and releasing must halt it."""
        page = self._new_realtime_page()
        try:
            self._boot_live(page)

            cases = [
                ("w", "pushes forward"),
                ("s", "pulls backward"),
                ("a", "to the left"),
                ("d", "to the right"),
            ]
            for key, phrase in cases:
                page.evaluate("() => { window.__moves = []; }")
                page.keyboard.down(key)
                page.wait_for_function(
                    "(p) => (window.__moves||[]).some(m => m.indexOf('Camera:') >= 0 && m.indexOf(p) >= 0)",
                    arg=phrase, timeout=6000,
                )
                page.keyboard.up(key)
                # Releasing brings the camera to rest.
                page.wait_for_function(
                    "() => (window.__moves||[]).some(m => m.indexOf('eases to a halt') >= 0)",
                    timeout=6000,
                )
            # The steers are real prompt hot-swaps: the world model got set_prompt.
            cmds = page.evaluate("() => window.__MOCK_CMDS__ || []")
            self.assertIn("set_prompt", cmds, f"movement never re-steered. logs:\n{self._dump_logs()}")
        except Exception:
            print("\n=== CONSOLE LOG (wasd) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_pointer_drag_steers_360(self):
        """Dragging the stick with a pointer (mouse/touch path) moves the camera
        in that direction — here, straight up = forward."""
        page = self._new_realtime_page()
        try:
            self._boot_live(page)
            box = page.evaluate(
                """() => { const r = document.getElementById('move-pad').getBoundingClientRect();
                           return { cx: r.left + r.width/2, cy: r.top + r.height/2, r: r.width/2 }; }"""
            )
            page.evaluate("() => { window.__moves = []; }")
            page.mouse.move(box["cx"], box["cy"])
            page.mouse.down()
            # Push the nub firmly upward (forward) beyond the deadzone.
            page.mouse.move(box["cx"], box["cy"] - box["r"], steps=6)
            page.wait_for_function(
                "() => (window.__moves||[]).some(m => m.indexOf('Camera:') >= 0 && m.indexOf('pushes forward') >= 0)",
                timeout=6000,
            )
            # The pad reads as engaged while dragging.
            self.assertTrue(page.evaluate("() => document.getElementById('move-pad').classList.contains('engaged')"))
            page.mouse.up()
            page.wait_for_function("() => !document.getElementById('move-pad').classList.contains('engaged')", timeout=4000)
        except Exception:
            print("\n=== CONSOLE LOG (pointer) ===\n" + self._dump_logs())
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
            page.evaluate("() => { window.__moves = []; }")
            page.keyboard.down("w")
            page.wait_for_timeout(600)
            page.keyboard.up("w")
            moves = self._camera_moves(page)
            self.assertEqual(moves, [], f"keys must not steer in still mode, got: {moves}")
        except Exception:
            print("\n=== CONSOLE LOG (still) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
