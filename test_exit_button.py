#!/usr/bin/env python3
"""The EXIT button has to actually stop the app.

Two things are being protected here. The first is that it works: pressing it
twice ends the server, and any render running at the time is cancelled rather
than left buying frames in a process nobody is watching. The second is that it
only works *here* — the same Flask app serves the hosted game, where a route
that kills the process on request is a denial-of-service button.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def post(url: str, payload: dict | None = None, timeout: float = 10.0):
    req = urllib.request.Request(
        url, data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read())


def alive(base: str) -> bool:
    try:
        with urllib.request.urlopen(base + "/api/health", timeout=2):
            return True
    except Exception:
        return False


class TheHostedServerRefusesToKillItself(unittest.TestCase):
    """Nobody arms it, so the route is inert — which is the production case."""

    def test_an_unarmed_server_answers_403(self):
        import api

        api._shutdown_armed = False
        client = api.app.test_client()
        resp = client.post("/api/shutdown")
        self.assertEqual(resp.status_code, 403)
        self.assertIn("not available", resp.get_json()["error"])

    def test_a_remote_caller_is_refused_even_when_armed(self):
        import api

        api.enable_shutdown()
        self.addCleanup(setattr, api, "_shutdown_armed", False)
        client = api.app.test_client()
        # Werkzeug lets us claim to be someone else, which is the whole point.
        resp = client.post("/api/shutdown", environ_overrides={"REMOTE_ADDR": "10.0.0.7"})
        self.assertEqual(resp.status_code, 403)
        self.assertIn("local-only", resp.get_json()["error"])


class TheLocalAppStopsWhenAsked(unittest.TestCase):
    """A real process, a real port, a real quit."""

    #: Whether anyone reads the child's stdout. Not draining it is how the
    #: failsafe gets exercised — see the subclass at the bottom.
    drain = True

    def setUp(self):
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        env = dict(os.environ, PORT=str(self.port), STORYGEN_BACKEND="mock")
        # play.py is what arms the route, so the launcher is what gets booted.
        self.proc = subprocess.Popen(
            [sys.executable, "-u", "play.py", "--mock", "--browser",
             "--port", str(self.port)],
            cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        self.log: list[str] = []
        if self.drain:
            import threading

            def pump():
                for line in self.proc.stdout:
                    self.log.append(line)

            threading.Thread(target=pump, daemon=True).start()

        deadline = time.time() + 90
        while time.time() < deadline:
            if alive(self.base):
                return
            if self.proc.poll() is not None:
                self.fail(f"the app died while booting:\n{''.join(self.log)}")
            time.sleep(0.4)
        self.fail("the app never became healthy")

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)

    def quit_and_wait(self, timeout: float):
        status, body = post(self.base + "/api/shutdown")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "closing")

        started = time.time()
        # The response comes back before the exit, deliberately — so the
        # browser gets its 200. The process should follow shortly after.
        self.assertEqual(self.proc.wait(timeout=timeout), 0,
                         "the app did not exit cleanly")
        took = time.time() - started

        deadline = time.time() + 10
        while time.time() < deadline and alive(self.base):
            time.sleep(0.25)
        self.assertFalse(alive(self.base), "the port is still serving")
        return took

    def test_pressing_exit_stops_the_server_and_the_process(self):
        took = self.quit_and_wait(timeout=30)
        self.assertLess(took, 6.0,
                        "the ordinary quit should not need the failsafe")


class QuittingCannotBeBlocked(unittest.TestCase):
    """Leaving must not depend on anything finishing first.

    This was found the hard way: the exit used to log on its way out, and a
    stdout pipe nobody was draining blocked that write forever, so the process
    never reached os._exit. The general shape of the bug is "something on the
    way out hangs", and a shutdown hook that never returns is the deterministic
    version of it — a window that refuses to close does exactly this.
    """

    def test_a_hook_that_never_returns_does_not_keep_the_app_alive(self):
        script = (
            "import sys, time, threading\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "import api\n"
            "api.enable_shutdown(lambda: time.sleep(3600))\n"
            "api._quit_process()\n"
            "time.sleep(3600)\n"   # never reached; here so a bug reads as a hang
        )
        started = time.time()
        proc = subprocess.Popen([sys.executable, "-c", script], cwd=str(ROOT),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            code = proc.wait(timeout=40)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("a hanging shutdown hook kept the process alive")
        self.assertEqual(code, 0)
        self.assertLess(time.time() - started, 40)


class QuittingStopsTheSpending(unittest.TestCase):
    """The render is the expensive one, and it outlives its parent."""

    def test_a_running_render_is_cancelled_on_the_way_out(self):
        import api
        import render_jobs

        cancelled = []

        class FakeJob:
            state = "running"

            def cancel(self, force=False):
                cancelled.append(force)
                self.state = "cancelled"

            def to_dict(self):
                return {"state": self.state}

        original = render_jobs._job
        render_jobs._job = FakeJob()
        self.addCleanup(setattr, render_jobs, "_job", original)

        stopped = api._release_compute()
        self.assertTrue(cancelled, "the render was left running")
        # Quitting means "stop spending right now" — it can't wait a full
        # turn for a graceful stop the way the in-app STOP button can.
        self.assertTrue(cancelled[0], "shutdown must force the render to stop immediately")
        self.assertIn("render", stopped)

    def test_releasing_compute_is_safe_when_nothing_is_running(self):
        import api
        import render_jobs

        original = render_jobs._job
        render_jobs._job = None
        self.addCleanup(setattr, render_jobs, "_job", original)

        self.assertEqual(api._release_compute(), [])


class TheButtonIsWiredToTheRoute(unittest.TestCase):
    """Shape checks — cheap, and they catch a rename that the e2e would miss."""

    def setUp(self):
        self.js = (ROOT / "static" / "js" / "standalone.js").read_text(
            encoding="utf-8", errors="replace")
        self.html = (ROOT / "templates" / "standalone.html").read_text(
            encoding="utf-8", errors="replace")

    def test_the_rail_has_an_exit_button(self):
        self.assertIn('id="btn-exit"', self.html)
        self.assertIn(">EXIT<", self.html)

    def test_the_picker_has_its_own_exit_in_the_nav(self):
        """#start-exit sits under the full-screen picker; BACK/EDITOR work
        because they live in .xp-nav. EXIT has to live there too."""
        self.assertIn('id="xp-exit"', self.html)
        self.assertIn('getElementById("xp-exit")', self.js)
        self.assertIn('el.xpExit.addEventListener("click"', self.js)
        self.assertIn("body.xp-open:has(#xp-exit) #start-exit", (ROOT / "static" / "css" / "standalone.css").read_text(
            encoding="utf-8", errors="replace"))

    def test_the_first_press_says_again_on_the_button(self):
        """The renderer toast sits under the start menu, so arming must
        change the button itself or the player thinks EXIT is dead."""
        quit_block = self.js.split("const Quit = (function ()", 1)[1][:4000]
        self.assertIn('btn.textContent = on ? "AGAIN" : "EXIT"', quit_block)

    def test_the_button_calls_the_shutdown_route(self):
        self.assertIn('postJSON("/api/shutdown"', self.js)
        self.assertIn('el.btnExit.addEventListener("click"', self.js)

    def test_one_press_only_arms_it(self):
        """A misclick on EXIT should not end the session."""
        self.assertIn("function press()", self.js)
        quit_block = self.js.split("const Quit = (function ()", 1)[1][:4000]
        self.assertIn("if (armed) { commit(); return; }", quit_block)
        self.assertIn("arm();", quit_block)

    def test_it_closes_the_live_renderer_before_going(self):
        """The world model is a paid stream; hiding it is not stopping it."""
        quit_block = self.js.split("const Quit = (function ()", 1)[1][:4000]
        self.assertIn("ReactorRenderer.disable", quit_block)

    def test_a_refusal_is_not_reported_as_a_goodbye(self):
        """A 403 throws exactly like a dead socket does; they must not merge."""
        quit_block = self.js.split("const Quit = (function ()", 1)[1][:4000]
        self.assertIn("if (err && err.status)", quit_block)
        self.assertIn("still running", quit_block)


class ClosingTheWindowAnyWayReleasesTheCompute(unittest.TestCase):
    """EXIT is not the only way out, and the others must not leak a render.

    The title bar and Alt+F4 never touch /api/shutdown. A render started before
    one of those runs in its own process, so without this it would outlive the
    app and keep buying frames for a game nobody is watching.
    """

    def test_the_window_closing_cancels_a_running_render(self):
        import types
        import api
        import play

        released = []
        real = api._release_compute
        api._release_compute = lambda: released.append(True) or []
        self.addCleanup(setattr, api, "_release_compute", real)
        self.addCleanup(lambda: api._SHUTDOWN_HOOKS.clear())

        # Stand in for pywebview: a window that opens and is immediately shut,
        # which is what every close path looks like from here.
        class FakeEvents:
            def __init__(self): self.loaded = self
            def __iadd__(self, fn): return self

        class FakeWindow:
            def __init__(self): self.events = FakeEvents()
            def destroy(self): pass
            def load_url(self, url): pass
            def load_html(self, html): pass

        fake = types.ModuleType("webview")
        fake.create_window = lambda *a, **k: FakeWindow()
        fake.start = lambda **k: None          # returns == the window closed
        self.addCleanup(sys.modules.pop, "webview", None)
        sys.modules["webview"] = fake

        real_health = play.wait_for_health
        play.wait_for_health = lambda *a, **k: True
        self.addCleanup(setattr, play, "wait_for_health", real_health)

        self.assertEqual(play.run_window("http://x/game", "http://x/health", False), 0)
        self.assertTrue(released, "closing the window left the render running")


try:
    from playwright.sync_api import sync_playwright
    HAVE_PW = True
except ImportError:  # pragma: no cover
    HAVE_PW = False


@unittest.skipUnless(HAVE_PW, "playwright not installed")
class PressingItInABrowserActuallyQuits(unittest.TestCase):
    """The route works; this asks whether the button reaches it.

    Boots the launcher (not run_local — only play.py arms the route), opens the
    real page, and presses the real button.
    """

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.proc = subprocess.Popen(
            [sys.executable, "-u", "play.py", "--mock", "--browser",
             "--port", str(cls.port)],
            cwd=str(ROOT), env=dict(os.environ, MOCK_MODE="1"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 90
        while time.time() < deadline and not alive(cls.base):
            time.sleep(0.4)
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close()
            cls.playwright.stop()
        finally:
            if cls.proc.poll() is None:
                cls.proc.kill()
                cls.proc.wait(timeout=10)

    def setUp(self):
        if not alive(self.base):
            self.fail("the app was not running when this test started")
        self.page = self.browser.new_page()
        self.page.goto(f"{self.base}/standalone?mode=play")
        # Wait for the opening scene to land before touching anything. The cold
        # open puts a full-screen ceremony over the rail, and a click during it
        # is swallowed rather than queued — which is also true for the player.
        self.page.wait_for_selector(".choice-btn", state="attached", timeout=45000)
        self.page.wait_for_selector("#btn-exit", state="attached", timeout=20000)
        # The rail starts collapsed, so the button exists but isn't reachable.
        self.page.evaluate("document.getElementById('menu-toggle').click()")
        self.page.wait_for_selector("#btn-exit", state="visible", timeout=10000)

    def test_one_press_arms_but_does_not_quit(self):
        self.page.click("#btn-exit")
        self.page.wait_for_selector("#btn-exit.arming", timeout=3000)
        time.sleep(1.0)
        self.assertTrue(alive(self.base),
                        "a single press took the whole app down")

    def test_the_arming_lapses_so_a_misclick_is_harmless(self):
        self.page.click("#btn-exit")
        self.page.wait_for_selector("#btn-exit.arming", timeout=3000)
        # It disarms itself a few seconds later; the exact window is the
        # module's business, so just wait past it.
        self.page.wait_for_selector("#btn-exit.arming", state="detached",
                                    timeout=8000)
        self.assertTrue(alive(self.base))

    def test_zz_two_presses_close_the_app(self):
        """Named to sort last: it stops the server the other tests need."""
        self.page.click("#btn-exit")
        self.page.wait_for_selector("#btn-exit.arming", timeout=3000)
        self.page.click("#btn-exit")

        # The player should be told, not left looking at a frozen game.
        self.page.wait_for_selector("#exit-veil:not(.hidden)", timeout=5000)

        deadline = time.time() + 25
        while time.time() < deadline and alive(self.base):
            time.sleep(0.4)
        self.assertFalse(alive(self.base), "the server is still up")
        self.assertEqual(self.proc.wait(timeout=15), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
