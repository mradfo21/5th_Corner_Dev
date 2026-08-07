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

    def test_fps_mouse_look_drives_lingbot_axes_gently(self):
        """The same mouse input drives the legacy LingBot axes, with a turn rate
        well under the keyboard band (latent video punishes fast turns), and the
        axis returns to idle on release so the camera actually halts."""
        page = self._new_realtime_page(mode="fps")
        try:
            self._boot_live(page, model="lingbot-world-2")
            self._reset_cmd_log(page)
            self._look_drag(page, dx=240, start=(400, 360))
            self._wait_cmd(page, "set_look_horizontal", "look_horizontal", "right")
            speeds = page.evaluate(
                """() => (window.__MOCK_CMD_LOG__||[])
                       .filter(c => c.name==='set_rotation_speed_deg')
                       .map(c => c.data.rotation_speed_deg)"""
            )
            self.assertTrue(speeds, f"mouse look should set a rotation speed: {speeds}")
            self.assertLessEqual(max(speeds), 3.0, f"mouse look must stay subtle: {speeds}")
            page.mouse.up()
            self._wait_cmd(page, "set_look_horizontal", "look_horizontal", "idle")
        except Exception:
            print("\n=== CONSOLE LOG (fps-lingbot) ===\n" + self._dump_logs())
            raise
        finally:
            page.close()

    def test_clicking_the_world_captures_the_pointer_in_fps(self):
        """A plain click on the world takes real pointer lock (true FPS feel),
        while a look-DRAG must not — releasing a drag should never silently
        swallow the cursor."""
        page = self._new_realtime_page(mode="fps")
        try:
            self._boot_live(page, model="happy-oyster")
            # Drag, then release: no capture.
            self._look_drag(page, dx=-200)
            page.mouse.up()
            page.wait_for_timeout(500)
            self.assertFalse(page.evaluate("() => !!document.pointerLockElement"),
                             "a look-drag must not capture the pointer")
            # Plain click: capture.
            page.mouse.move(550, 300)
            page.mouse.down()
            page.mouse.up()
            page.wait_for_function("() => !!document.pointerLockElement", timeout=4000)
            # pointerlockchange dispatches after the property flips, so wait for
            # the class rather than reading it in the same tick.
            page.wait_for_function(
                "() => document.body.classList.contains('mouse-look-locked')", timeout=4000)
        except Exception:
            print("\n=== CONSOLE LOG (fps-lock) ===\n" + self._dump_logs())
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
            # Only the tail of the sweep may still be draining; tremor adds nothing.
            self.assertLess(result["turningFrames"], 40,
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
            self.assertLess(result["big"], 900, f"wind-down too long: {result}")
        except Exception:
            print("\n=== CONSOLE LOG (proportional) ===\n" + self._dump_logs())
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
