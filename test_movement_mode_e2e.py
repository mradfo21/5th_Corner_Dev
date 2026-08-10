"""
test_movement_mode_e2e.py — Playwright end-to-end test for the WSAD / joystick
MOVEMENT ("explore") mode that navigates the realtime (Reactor Happy Oyster)
world like a first-person camera.

It reuses the same harness as test_realtime_e2e.py — run the REAL client JS
against a MOCK Reactor SDK — and proves the movement instrument end to end:

  * The joystick is the CENTER of the action cluster in realtime video mode,
    with the ACT hub pushed to the LEFT and the PHOTO hub to the RIGHT.
  * Two CONTROL MODES, switched from the WORLD EDITOR (persisted per browser):
      - DOOM (default): W/S move, A/D turn, Q/E strafe, no mouse look.
      - FPS: W/S move, A/D strafe, and the MOUSE steers the camera.
  * Mouse look is exercised with REAL mouse events (drag-look) and a real
    click-to-capture pointer lock — not a test-only shim.
  * Holding W (keyboard) drives the LIVE world with Happy Oyster's held move
    command (move {direction:"Front"}) — a native navigation command, no world
    rebuild — and releasing it stops (releases held controls).
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

    def _new_realtime_page(self, mode="doom"):
        page = self.browser.new_page(viewport={"width": 1100, "height": 800})
        self._logs = []
        page.on("console", lambda m: self._logs.append(f"{m.type}: {m.text}"))
        page.on("pageerror", lambda e: self._logs.append(f"PAGEERROR: {e}"))
        # Skip the first-run "tap to scan" tutorial modal so it can't intercept
        # the pointer/keyboard interactions these movement tests drive. Pin the
        # control mode so key→action mapping is deterministic per test.
        page.add_init_script(
            "try { localStorage.setItem('scan_tutorial_seen_v1', '1'); "
            f"localStorage.setItem('input_profile', '{mode}'); }} catch (e) {{}}"
        )
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

    def _scan_now(self, page, timeout=15000):
        """Fire a manual SCAN pass (the button's action). Playwright auto-waits
        for the button to be actionable (enabled + clickable). Scanning is gated
        behind the SCAN button now; nothing detects on its own."""
        page.click("#scan-btn", timeout=timeout)

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

    # A real look-drag over the world: press on the scene and sweep the mouse.
    # This is exactly what a player does — no test-only injection hooks.
    def _look_drag(self, page, dx=0, dy=0, steps=10, start=(550, 360)):
        page.mouse.move(*start)
        page.mouse.down()
        for i in range(steps):
            page.mouse.move(start[0] + dx * (i + 1) / steps,
                            start[1] + dy * (i + 1) / steps)
            page.wait_for_timeout(35)

    def test_doom_mode_is_the_default_and_ad_turn_the_view(self):
        """DOOM (default): W/S move Front/Back, A/D TURN the view, Q/E strafe,
        arrows look. Each fires the native held command and releases on key-up."""
        page = self._new_realtime_page()
        try:
            self._boot_live(page, model="happy-oyster")
            self.assertEqual(page.evaluate("() => window.__InputBindings.current()"), "doom")
            cases = [
                ("w", "move", "direction", "Front"),
                ("s", "move", "direction", "Back"),
                ("a", "look", "direction", "Mouse_Left"),
                ("d", "look", "direction", "Mouse_Right"),
                ("q", "move", "direction", "Left"),
                ("e", "move", "direction", "Right"),
                ("ArrowLeft", "look", "direction", "Mouse_Left"),
                ("ArrowRight", "look", "direction", "Mouse_Right"),
                ("ArrowUp", "look", "direction", "Mouse_Up"),
                ("ArrowDown", "look", "direction", "Mouse_Down"),
            ]
            for key, cmd, param, value in cases:
                self._reset_cmd_log(page)
                page.keyboard.down(key)
                self._wait_cmd(page, cmd, param, value)
                page.keyboard.up(key)
                self._wait_cmd(page, "stop")
            log = page.evaluate("() => window.__MOCK_CMD_LOG__ || []")
            self.assertTrue(all(c["name"] != "set_prompt" for c in log),
                            f"drive should not use set_prompt. log:\n{log}")
        except Exception:
            print("\n=== CONSOLE LOG (doom-keys) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_doom_mode_has_no_mouse_look(self):
        """DOOM is keyboard-only: sweeping the mouse over the world must not
        steer the camera (and must not grab the cursor)."""
        page = self._new_realtime_page()
        try:
            self._boot_live(page, model="happy-oyster")
            self._reset_cmd_log(page)
            self._look_drag(page, dx=-240)
            page.mouse.up()
            page.wait_for_timeout(400)
            steer = page.evaluate(
                """() => (window.__MOCK_CMD_LOG__||[]).filter(c =>
                       ['move','look'].includes(c.name))"""
            )
            self.assertEqual(steer, [], f"DOOM must ignore the mouse, got: {steer}")
            self.assertFalse(page.evaluate("() => document.body.classList.contains('mouse-looking')"))
        except Exception:
            print("\n=== CONSOLE LOG (doom-no-mouse) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_fps_mode_ad_strafe_and_mouse_looks(self):
        """FPS: A/D become STRAFE, and a real mouse sweep steers the camera —
        left sweep looks left, right sweep looks right, and letting go stops."""
        page = self._new_realtime_page(mode="fps")
        try:
            self._boot_live(page, model="happy-oyster")
            self.assertEqual(page.evaluate("() => window.__InputBindings.current()"), "fps")
            for key, value in (("a", "Left"), ("d", "Right")):
                self._reset_cmd_log(page)
                page.keyboard.down(key)
                self._wait_cmd(page, "move", "direction", value)
                page.keyboard.up(key)
                self._wait_cmd(page, "stop")

            self._reset_cmd_log(page)
            self._look_drag(page, dx=-240)
            self._wait_cmd(page, "look", "direction", "Mouse_Left")
            page.mouse.up()
            self._wait_cmd(page, "stop")

            self._reset_cmd_log(page)
            self._look_drag(page, dx=240, start=(400, 360))
            self._wait_cmd(page, "look", "direction", "Mouse_Right")
            page.mouse.up()
            self._wait_cmd(page, "stop")
        except Exception:
            print("\n=== CONSOLE LOG (fps-mouse) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_fps_mouse_look_composes_with_movement_and_tilts(self):
        """Holding W while sweeping the mouse down must do BOTH: keep moving
        forward and tilt the view — mouse look never cancels locomotion."""
        page = self._new_realtime_page(mode="fps")
        try:
            self._boot_live(page, model="happy-oyster")
            self._reset_cmd_log(page)
            page.keyboard.down("w")
            self._wait_cmd(page, "move", "direction", "Front")
            self._look_drag(page, dy=240)
            self._wait_cmd(page, "look", "direction", "Mouse_Down")
            page.mouse.up()
            page.keyboard.up("w")
            self._wait_cmd(page, "stop")
        except Exception:
            print("\n=== CONSOLE LOG (fps-compose) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_fps_mouse_look_drives_lingbot_axes(self):
        """The same mouse input drives the legacy LingBot axes, and the axis
        returns to idle on release so the camera actually halts.

        The rate bound here used to be 3.0 -- BELOW the 3.4 a single keyboard tap
        gets -- so the mouse was capped slower than the keys it replaced, which
        is a large part of why it felt like nothing was happening. It may now use
        the model's real range, and is still bounded well under the 30 the API
        allows."""
        page = self._new_realtime_page(mode="fps")
        try:
            self._boot_live(page, model="lingbot-world-2")
            self._reset_cmd_log(page)
            # A decisive sweep: the rate tracks how much turn is QUEUED, so a
            # short flick legitimately reads low. Give the drive loop a tick to
            # catch up before reading the rate it settled on.
            self._look_drag(page, dx=420, steps=18, start=(300, 360))
            self._wait_cmd(page, "set_look_horizontal", "look_horizontal", "right")
            page.wait_for_timeout(220)
            speeds = page.evaluate(
                """() => (window.__MOCK_CMD_LOG__||[])
                       .filter(c => c.name==='set_rotation_speed_deg')
                       .map(c => c.data.rotation_speed_deg)"""
            )
            self.assertTrue(speeds, f"mouse look should set a rotation speed: {speeds}")
            self.assertLessEqual(max(speeds), 6.0, f"mouse look turn rate out of range: {speeds}")
            self.assertGreater(max(speeds), 3.4,
                               f"a mouse sweep should out-turn a keyboard tap: {speeds}")
            page.mouse.up()
            self._wait_cmd(page, "set_look_horizontal", "look_horizontal", "idle")
        except Exception:
            print("\n=== CONSOLE LOG (fps-lingbot) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_a_plain_click_never_steals_the_cursor(self):
        """Regression: a single click on the world used to silently take pointer
        lock. The game uses clicks, so that hid the cursor and swallowed the
        click — indistinguishable from a freeze. Only a DOUBLE-click may capture."""
        page = self._new_realtime_page(mode="fps")
        try:
            self._boot_live(page, model="happy-oyster")
            # Plain click: no capture.
            page.mouse.move(550, 300)
            page.mouse.click(550, 300)
            page.wait_for_timeout(700)
            self.assertFalse(page.evaluate("() => !!document.pointerLockElement"),
                             "a single click must never capture the pointer")
            # A look-drag: also no capture.
            self._look_drag(page, dx=-200)
            page.mouse.up()
            page.wait_for_timeout(500)
            self.assertFalse(page.evaluate("() => !!document.pointerLockElement"),
                             "a look-drag must never capture the pointer")
            # Double-click: explicit opt-in, capture allowed.
            page.mouse.dblclick(560, 320)
            page.wait_for_function("() => !!document.pointerLockElement", timeout=4000)
            page.wait_for_function(
                "() => document.body.classList.contains('mouse-look-locked')", timeout=4000)
        except Exception:
            print("\n=== CONSOLE LOG (no-cursor-theft) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_capture_is_refused_while_the_world_is_still_black(self):
        """Never grab the cursor before the live world has revealed — a slow first
        scene plus a captured cursor is exactly what "froze on black" looked
        like."""
        page = self._new_realtime_page(mode="fps")
        try:
            self._boot_live(page, model="happy-oyster")
            page.evaluate("""() => {
                window.__realShowing = window.ReactorRenderer.isShowing;
                window.ReactorRenderer.isShowing = () => false;   // world still black
            }""")
            page.mouse.dblclick(560, 320)
            page.wait_for_timeout(900)
            self.assertFalse(page.evaluate("() => !!document.pointerLockElement"),
                             "must not capture the cursor before the world shows")
            page.evaluate("() => { window.ReactorRenderer.isShowing = window.__realShowing; }")
        except Exception:
            print("\n=== CONSOLE LOG (black-world-capture) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_mouse_look_release_restores_scanning(self):
        """Regression: holding the look pointer must not leave the game stuck in
        the "moving" state — that permanently hid the OCR hotspots and disabled
        SCAN. Motion state has to follow ACTUAL camera motion."""
        page = self._new_realtime_page(mode="fps")
        page.add_init_script("window.__MOVE_SETTLE_MS__ = 250;")
        try:
            self._boot_live(page, model="happy-oyster")
            self._look_drag(page, dx=-240)
            page.wait_for_function("() => document.body.classList.contains('moving')", timeout=4000)
            page.mouse.up()
            page.wait_for_function("() => !document.body.classList.contains('moving')", timeout=5000)
        except Exception:
            print("\n=== CONSOLE LOG (scan-restore) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_mouse_look_never_spins_on_forever(self):
        """Regression: the camera used to spin endlessly in whichever direction
        you last swept. A held look command keeps rotating until stopped, and the
        old "turn while the mouse is moving" rule meant ordinary hand tremor
        sustained it forever. Turn is now a BUDGET that drains: a sweep followed
        by 2s of 1px tremor must come to REST and emit exactly one stop."""
        page = self._new_realtime_page(mode="fps")
        try:
            self._boot_live(page, model="happy-oyster")
            result = page.evaluate(
                """async () => {
                    const sleep = (m) => new Promise(r => setTimeout(r, m));
                    window.__MOCK_CMD_LOG__ = [];
                    // A hard sweep left, as if aiming at something.
                    for (let i = 0; i < 12; i++) { window.__MouseLook.__feed(-25, 0); await sleep(10); }
                    const swept = window.__MouseLook.intent();
                    // Then a hand simply resting on the mouse: 1px tremor at 60Hz.
                    let turning = 0;
                    for (let i = 0; i < 120; i++) {
                        window.__MouseLook.__feed(i % 2 ? 1 : -1, 0);
                        await sleep(16);
                        if (window.__MouseLook.intent()) turning++;
                    }
                    return {
                        swept: swept && swept.lookH,
                        turningFrames: turning,
                        resting: window.__MouseLook.intent() === null,
                        stops: (window.__MOCK_CMD_LOG__ || []).filter(c => c.name === 'stop').length,
                        looks: (window.__MOCK_CMD_LOG__ || [])
                            .filter(c => c.name === 'look').map(c => c.data.direction),
                    };
                }"""
            )
            self.assertEqual(result["swept"], "left", "the sweep itself should look left")
            # A sweep is allowed a momentum tail (that's what buys turn on a model
            # whose rate we can't set). What must NOT happen is tremor holding the
            # camera open past it, so the invariant is: it reaches rest inside the
            # window, and tremor never re-issues a look.
            self.assertLess(result["turningFrames"], 90,
                            f"tremor kept the camera turning: {result}")
            self.assertTrue(result["resting"], f"camera never came to rest: {result}")
            self.assertEqual(result["looks"], ["Mouse_Left"],
                             f"tremor should not re-issue look commands: {result}")
            self.assertEqual(result["stops"], 1, f"expected exactly one stop: {result}")
        except Exception:
            print("\n=== CONSOLE LOG (no-endless-spin) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_mouse_look_turn_is_proportional_to_mouse_distance(self):
        """"Tied to the mouse": a longer sweep owes a longer turn, and any sweep
        winds down on its own within a few hundred ms of the mouse stopping."""
        page = self._new_realtime_page(mode="fps")
        try:
            self._boot_live(page, model="happy-oyster")
            result = page.evaluate(
                """async () => {
                    const sleep = (m) => new Promise(r => setTimeout(r, m));
                    async function sweepThenTime(px, steps) {
                        for (let i = 0; i < steps; i++) { window.__MouseLook.__feed(px, 0); await sleep(10); }
                        const t0 = performance.now();
                        for (let i = 0; i < 60; i++) {
                            await sleep(25);
                            if (!window.__MouseLook.intent()) return Math.round(performance.now() - t0);
                        }
                        return -1;  // never stopped
                    }
                    const small = await sweepThenTime(4, 4);
                    await sleep(600);   // let the budget fully drain between sweeps
                    const big = await sweepThenTime(30, 12);
                    return { small: small, big: big };
                }"""
            )
            self.assertGreater(result["small"], 0, f"small sweep never settled: {result}")
            self.assertGreater(result["big"], 0, f"big sweep never settled: {result}")
            self.assertGreater(result["big"], result["small"],
                               f"a longer sweep should owe a longer turn: {result}")
            # Bounded: a full-budget sweep unwinds fast enough to feel connected.
            self.assertLess(result["big"], 1800, f"wind-down too long: {result}")
        except Exception:
            print("\n=== CONSOLE LOG (proportional) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_mouse_look_supports_diagonals(self):
        """Up AND left at once. Models with independent look axes hold a true
        diagonal; Happy Oyster can only hold ONE look verb, so the two are
        interleaved in time slices — either way both axes get driven."""
        # LingBot: independent axes -> both held simultaneously.
        page = self._new_realtime_page(mode="fps")
        try:
            self._boot_live(page, model="lingbot-world-2")
            self._reset_cmd_log(page)
            self._look_drag(page, dx=-180, dy=-180)
            self._wait_cmd(page, "set_look_horizontal", "look_horizontal", "left")
            self._wait_cmd(page, "set_look_vertical", "look_vertical", "up")
            it = page.evaluate("() => window.__MouseLook.intent()")
            self.assertEqual(it["lookH"], "left", f"intent: {it}")
            self.assertEqual(it["lookV"], "up", f"intent: {it}")
            page.mouse.up()
        except Exception:
            print("\n=== CONSOLE LOG (diagonal-lingbot) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

        # Happy Oyster holds ONE look direction, so a diagonal has to commit to
        # the axis the mouse travelled further on. Alternating the two (the old
        # behaviour) restarted the rotation every slice and turned less than a
        # straight sweep did -- see test_a_diagonal_sweep_does_not_churn_the_look_slot.
        page = self._new_realtime_page(mode="fps")
        try:
            self._boot_live(page, model="happy-oyster")
            self.assertTrue(page.evaluate("() => window.ReactorRenderer.looksOneAxisAtATime()"))
            self._reset_cmd_log(page)
            self._look_drag(page, dx=-60, dy=-260, steps=14)   # mostly vertical
            self._wait_cmd(page, "look", "direction", "Mouse_Up")
            page.mouse.up()
        except Exception:
            print("\n=== CONSOLE LOG (diagonal-oyster) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_editor_exposes_look_sensitivity(self):
        """Sensitivity is tunable from the editor CONTROLS row, defaults to 3x,
        persists, clamps, and is inert (disabled) in DOOM where there's no mouse
        look."""
        page = self._new_realtime_page(mode="fps")
        try:
            self._boot_live(page, model="happy-oyster")
            self.assertEqual(page.evaluate("() => window.__InputBindings.sensitivity()"), 8)
            page.keyboard.press("`")
            page.wait_for_selector("#we-input-sens", timeout=8000)
            self.assertEqual(page.evaluate("() => document.getElementById('we-input-sens').value"), "8")
            self.assertFalse(page.evaluate("() => document.getElementById('we-input-sens').disabled"),
                             "slider should be usable in FPS mode")
            # Drag the slider like a player would.
            page.evaluate("""() => {
                const s = document.getElementById('we-input-sens');
                s.value = '7';
                s.dispatchEvent(new Event('input', { bubbles: true }));
            }""")
            self.assertEqual(page.evaluate("() => window.__InputBindings.sensitivity()"), 7)
            self.assertEqual(page.evaluate("() => localStorage.getItem('input_look_sens')"), "7")
            self.assertIn("7", page.evaluate("() => document.getElementById('we-input-sens-val').textContent"))
            # Clamped to the advertised range.
            self.assertEqual(page.evaluate("() => window.__InputBindings.setSensitivity(9999)"), 30)
            self.assertEqual(page.evaluate("() => window.__InputBindings.setSensitivity(-5)"), 1)
            # DOOM has no mouse look, so the control goes inert.
            page.click("#we-input-profile button[data-value='doom']")
            page.wait_for_function(
                "() => document.getElementById('we-input-sens').disabled === true", timeout=4000)
        except Exception:
            print("\n=== CONSOLE LOG (sensitivity-ui) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    # Every distinct move/look direction the world model was actually told,
    # in order, normalised across the two native navigation schemes.
    _DRIVE_SNAPSHOT = """() => {
        const out = [];
        (window.__MOCK_CMD_LOG__ || []).forEach((c) => {
            let s = null;
            if (c.name === 'move') s = 'move:' + c.data.direction;
            else if (c.name === 'look') s = 'look:' + c.data.direction;
            else if (c.name === 'set_move_lateral') s = 'lat:' + c.data.move_lateral;
            else if (c.name === 'set_move_longitudinal') s = 'lon:' + c.data.move_longitudinal;
            else if (c.name === 'set_look_horizontal') s = 'lookH:' + c.data.look_horizontal;
            if (s && !out.includes(s)) out.push(s);
        });
        return out;
    }"""

    def _hold(self, page, keys, hold_ms=900):
        """Hold a key combination, return what the model was told, then release."""
        self._reset_cmd_log(page)
        for k in keys:
            page.keyboard.down(k)
            page.wait_for_timeout(120)
        page.wait_for_timeout(hold_ms)
        got = page.evaluate(self._DRIVE_SNAPSHOT)
        for k in reversed(keys):
            page.keyboard.up(k)
        page.wait_for_timeout(250)
        return got

    def _assert_drive(self, page, model, mode, cases):
        for keys, expected in cases:
            got = self._hold(page, keys)
            for want in expected:
                self.assertIn(
                    want, got,
                    f"{model}/{mode}: holding {'+'.join(keys)} should drive {want}, got {got}")

    def test_strafe_matrix_on_happy_oyster(self):
        """Strafe on Happy Oyster, whose native scheme is a HELD move verb with
        only one slot — the reason W+A used to send just move:Front and drop the
        strafe entirely. FPS: A/D strafe. DOOM: A/D turn instead and Q/E strafe.
        Either way strafe must survive being combined with forward/back."""
        for mode, cases in (
            ("fps", [
                (["a"], ["move:Left"]),
                (["d"], ["move:Right"]),
                (["w", "a"], ["move:Front", "move:Left"]),
                (["w", "d"], ["move:Front", "move:Right"]),
                (["s", "a"], ["move:Back", "move:Left"]),
                (["q"], ["move:Left"]),
                (["e"], ["move:Right"]),
            ]),
            ("doom", [
                (["a"], ["look:Mouse_Left"]),      # DOOM turns, doesn't strafe
                (["d"], ["look:Mouse_Right"]),
                (["q"], ["move:Left"]),            # strafe lives on Q/E here
                (["e"], ["move:Right"]),
                (["w", "q"], ["move:Front", "move:Left"]),
            ]),
        ):
            page = self._new_realtime_page(mode=mode)
            try:
                self._boot_live(page, model="happy-oyster")
                self._assert_drive(page, "happy-oyster", mode, cases)
            except Exception:
                print(f"\n=== CONSOLE LOG (strafe happy-oyster/{mode}) ===\n" + self._dump_logs())
                raise
            finally:
                page.close()

    def test_strafe_matrix_on_lingbot(self):
        """Strafe on LingBot, whose native scheme is independent persistent axes,
        so forward and strafe genuinely hold at once. Same key contract as Happy
        Oyster from the player's side."""
        for mode, cases in (
            ("fps", [
                (["a"], ["lat:left"]),
                (["d"], ["lat:right"]),
                (["w", "a"], ["lon:forward", "lat:left"]),
                (["w", "d"], ["lon:forward", "lat:right"]),
                (["s", "a"], ["lon:back", "lat:left"]),
                (["q"], ["lat:left"]),
                (["e"], ["lat:right"]),
            ]),
            ("doom", [
                (["a"], ["lookH:left"]),
                (["d"], ["lookH:right"]),
                (["q"], ["lat:left"]),
                (["e"], ["lat:right"]),
                (["w", "q"], ["lon:forward", "lat:left"]),
            ]),
        ):
            page = self._new_realtime_page(mode=mode)
            try:
                self._boot_live(page, model="lingbot-world-2")
                self._assert_drive(page, "lingbot-world-2", mode, cases)
            except Exception:
                print(f"\n=== CONSOLE LOG (strafe lingbot/{mode}) ===\n" + self._dump_logs())
                raise
            finally:
                page.close()

    def test_strafe_releases_on_key_up(self):
        """A held strafe MUST be released, or the camera keeps sliding sideways
        forever. Happy Oyster releases with a global stop; LingBot idles the axis."""
        page = self._new_realtime_page(mode="fps")
        try:
            self._boot_live(page, model="happy-oyster")
            self._reset_cmd_log(page)
            page.keyboard.down("a")
            self._wait_cmd(page, "move", "direction", "Left")
            page.keyboard.up("a")
            self._wait_cmd(page, "stop")
        except Exception:
            print("\n=== CONSOLE LOG (strafe release oyster) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

        page = self._new_realtime_page(mode="fps")
        try:
            self._boot_live(page, model="lingbot-world-2")
            self._reset_cmd_log(page)
            page.keyboard.down("a")
            self._wait_cmd(page, "set_move_lateral", "move_lateral", "left")
            page.keyboard.up("a")
            self._wait_cmd(page, "set_move_lateral", "move_lateral", "idle")
        except Exception:
            print("\n=== CONSOLE LOG (strafe release lingbot) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    # Every distinct move/look command IN ORDER, keeping repeats. The strafe
    # tests below dedupe, which is exactly how a churn bug hid: a stream of
    # Front, Left, Front, Left... dedupes to "both directions were driven" and
    # looks like a pass.
    _RAW_DRIVE = """() => (window.__MOCK_CMD_LOG__ || [])
        .filter(c => ['move', 'look', 'stop'].includes(c.name))
        .map(c => c.name + (c.data.direction ? ':' + c.data.direction : ''))"""

    def test_holding_forward_and_strafe_does_not_churn_the_move_slot(self):
        """Regression, and the reason A/D felt dead in normal play.

        Happy Oyster has ONE move slot and each `move` REPLACES the held
        direction. Alternating forward and strafe to fake a diagonal made the
        camera restart every ~130ms -- 15 switches in 2 seconds -- so it
        travelled nowhere in either direction. Since holding W+A is ordinary FPS
        movement, that read as strafe being broken.

        One slot means one direction: the newest press wins, and releasing it
        hands the slot straight back. Four inputs, four commands, no churn."""
        page = self._new_realtime_page(mode="fps")
        try:
            self._boot_live(page, model="happy-oyster")
            self._reset_cmd_log(page)
            page.keyboard.down("w")
            self._wait_cmd(page, "move", "direction", "Front")
            page.keyboard.down("a")
            self._wait_cmd(page, "move", "direction", "Left")
            page.wait_for_timeout(1600)          # hold both, as a player would
            stream = page.evaluate(self._RAW_DRIVE)
            # While both are held the slot must stay put, not oscillate.
            self.assertLessEqual(
                len(stream), 4,
                f"the move slot is churning while W+A are held: {stream}")
            self.assertEqual(stream[-1], "move:Left",
                             f"the newest press should own the slot: {stream}")
            # Releasing the strafe hands the slot back to forward.
            page.keyboard.up("a")
            self._wait_cmd(page, "move", "direction", "Front")
            page.keyboard.up("w")
            self._wait_cmd(page, "stop")
        except Exception:
            print("\n=== CONSOLE LOG (move-slot-churn) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_a_diagonal_sweep_does_not_churn_the_look_slot(self):
        """Same trap on the look axes: alternating Mouse_Left and Mouse_Up
        restarted the rotation every slice, so a diagonal sweep turned LESS than
        a straight one. It must commit to the axis the mouse travelled further
        on."""
        page = self._new_realtime_page(mode="fps")
        try:
            self._boot_live(page, model="happy-oyster")
            self._reset_cmd_log(page)
            self._look_drag(page, dx=-224, dy=-224, steps=14)
            page.wait_for_timeout(200)
            stream = page.evaluate(self._RAW_DRIVE)
            looks = [s for s in stream if s.startswith("look:")]
            self.assertTrue(looks, f"a diagonal sweep must steer: {stream}")
            self.assertLessEqual(
                len(set(looks)), 1,
                f"the look slot is oscillating across a diagonal: {stream}")
            page.mouse.up()
            self._wait_cmd(page, "stop")
        except Exception:
            print("\n=== CONSOLE LOG (look-slot-churn) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_a_sweep_earns_a_usable_amount_of_turn(self):
        """On Happy Oyster the turn RATE is the model's -- there is no rotation
        knob -- so the only thing a sweep can buy is how long the look is HELD.
        A 600ms ceiling therefore capped how far you could ever turn in one
        gesture, which is what made it feel dead whatever the slider said."""
        page = self._new_realtime_page(mode="fps")
        try:
            self._boot_live(page, model="happy-oyster")
            held = page.evaluate(
                """async () => {
                    const sleep = (m) => new Promise(r => setTimeout(r, m));
                    for (let i = 0; i < 15; i++) { window.__MouseLook.__feed(-20, 0); await sleep(12); }
                    const t0 = performance.now();
                    for (let i = 0; i < 200; i++) {
                        await sleep(25);
                        if (!window.__MouseLook.intent()) return Math.round(performance.now() - t0);
                    }
                    return -1;
                }"""
            )
            self.assertGreater(held, 900, f"a full sweep should keep turning: {held}ms")
            # Still bounded — the endless-spin regression stays fixed.
            self.assertLess(held, 2000, f"a sweep must not coast this long: {held}ms")
            page.evaluate("() => window.__MouseLook.releaseLock()")
        except Exception:
            print("\n=== CONSOLE LOG (turn-amount) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_look_drag_release_does_not_burn_a_scan(self):
        """Regression: tapping the world fires a PAID detection pass, and the
        mouseup ending a look-drag is a real click on the scene — so steering the
        camera bought a scan every time you let go. A gesture that MOVED must eat
        its click, while a stationary tap still scans."""
        page = self._new_realtime_page(mode="fps")
        detects = []

        def on_detect(route):
            detects.append(route.request.url)
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"objects": [
                              {"label": "crate", "cx": 0.3, "cy": 0.4, "w": 0.2, "h": 0.2}]}))
        page.route("**/api/detect", on_detect)
        try:
            self._boot_live(page, model="happy-oyster")
            # Steering the camera must not scan.
            self._look_drag(page, dx=-220)
            page.mouse.up()
            page.wait_for_timeout(1500)
            self.assertEqual(len(detects), 0,
                             f"a look-drag must not trigger a scan, got {len(detects)}")
            # A deliberate stationary tap still scans (unchanged behaviour).
            page.mouse.click(620, 300)
            page.wait_for_timeout(1800)
            self.assertEqual(len(detects), 1,
                             f"a stationary world tap should still scan, got {len(detects)}")
        except Exception:
            print("\n=== CONSOLE LOG (scan-gate) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_world_editor_switches_control_mode(self):
        """The CONTROLS switch lives in the WORLD EDITOR: it lists both modes,
        marks the active one, actually re-binds the keys, and persists."""
        page = self._new_realtime_page()
        try:
            self._boot_live(page, model="happy-oyster")
            # ` opens the editor (the EDIT rail button is behind the collapsed menu).
            page.keyboard.press("`")
            page.wait_for_selector("#we-input-profile button", timeout=8000)
            labels = page.evaluate(
                """() => Array.from(document.querySelectorAll('#we-input-profile button'))
                       .map(b => b.textContent.trim())"""
            )
            self.assertEqual(labels, ["DOOM", "FPS"])
            self.assertEqual(
                page.evaluate(
                    """() => (document.querySelector('#we-input-profile button.on')||{}).textContent"""
                ), "DOOM")
            page.click("#we-input-profile button[data-value='fps']")
            page.wait_for_function("() => window.__InputBindings.current() === 'fps'", timeout=4000)
            self.assertEqual(page.evaluate("() => localStorage.getItem('input_profile')"), "fps")
            self.assertEqual(
                page.evaluate(
                    """() => (document.querySelector('#we-input-profile button.on')||{}).textContent"""
                ), "FPS")
            # Close the editor and confirm the NEW binding is live: A strafes.
            page.click("#we-close")
            page.wait_for_timeout(500)
            self._reset_cmd_log(page)
            page.keyboard.down("a")
            self._wait_cmd(page, "move", "direction", "Left")
            page.keyboard.up("a")
            self._wait_cmd(page, "stop")
        except Exception:
            print("\n=== CONSOLE LOG (editor-switch) ===\n" + self._dump_logs())
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
            self._boot_live(page, model="happy-oyster")
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
            # Happy Oyster family: its move/look/stop stay allowed even when caps
            # omit the axes. Force the model since the default is now LingBot.
            self._boot_live(page, model="happy-oyster")
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

    def test_ocr_hotspots_hide_while_moving_and_can_rescan_on_stop(self):
        """The hotspots (and the choices grounded on them) are inaccurate while
        the camera travels: a scanned overlay must tear down the moment movement
        starts, no detection runs while moving, and once you stop a fresh SCAN
        reads the new vantage."""
        page = self._new_realtime_page()
        # Fast settle so the SCAN button re-enables quickly after stopping.
        page.add_init_script("window.__MOVE_SETTLE_MS__ = 250; window.__SCAN_TTL_MS__ = 60000;")
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
            # Press SCAN to surface the hotspots.
            self._scan_now(page)
            page.wait_for_function("document.querySelectorAll('#scan-tags .scan-tag').length >= 1", timeout=12000)
            self.assertFalse(page.evaluate("document.getElementById('scan-layer').classList.contains('hidden')"))

            # Start moving -> the scanned overlay tears down (body.moving, hidden).
            page.keyboard.down("w")
            page.wait_for_function("document.body.classList.contains('moving')", timeout=4000)
            page.wait_for_function("document.getElementById('scan-layer').classList.contains('hidden')", timeout=4000)

            # No detection runs while moving.
            before = len(detects)
            page.wait_for_timeout(700)
            self.assertEqual(len(detects), before, "detection must not run while the camera moves")

            # Stop -> movement clears; a fresh SCAN reads the new vantage.
            page.keyboard.up("w")
            page.wait_for_function("!document.body.classList.contains('moving')", timeout=4000)
            self._scan_now(page)
            page.wait_for_function("document.querySelectorAll('#scan-tags .scan-tag').length >= 1", timeout=8000)
            self.assertFalse(page.evaluate("document.getElementById('scan-layer').classList.contains('hidden')"))
            self.assertGreater(len(detects), before, "a fresh SCAN after stopping must re-detect")
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
