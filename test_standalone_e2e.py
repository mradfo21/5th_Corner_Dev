"""
test_standalone_e2e.py — Playwright end-to-end browser tests for the
standalone immersive UI (templates/standalone.html + static/js/standalone.js)
served by api.py at /standalone.

These tests spin up `run_local.py --mock` as a real subprocess on a
dedicated test port (fully offline — no API keys, no network calls) and
drive it with a real headless browser, exercising the same HTTP contract
the production UI uses end to end.

Requirements (see requirements-dev.txt):
    pip install -r requirements-dev.txt
    playwright install chromium

Run with:
    python3 -m unittest test_standalone_e2e -v
"""

import os
import shutil
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
class TestStandaloneE2E(unittest.TestCase):
    """Full browser flow: load /standalone, reset, choose, regenerate, VHS toggle."""

    @classmethod
    def setUpClass(cls):
        cls.port = _find_free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        # Own the session this run plays in. `sessions/` lives next to the
        # source, so a dev server (or another test) running from the same
        # checkout writes the SAME sessions/default/state.json this server
        # does — its saves land mid-turn here and the choice slate this
        # suite is waiting on disappears. A port-derived id keeps the two
        # apart on disk.
        cls.session_id = f"e2e{cls.port}"

        env = os.environ.copy()
        env["GEMINI_API_KEY"] = ""
        env["OPENAI_API_KEY"] = ""
        env["ANTHROPIC_API_KEY"] = ""
        env["ELEVENLABS_API_KEY"] = ""

        # The server is a SUBPROCESS, and the authoring sandbox only engages
        # on a store import inside the test process — this module imports no
        # store, so the mock server read and WROTE the real prompts/, worlds/,
        # experiences/ and tunables.json. Every New Game it ran rebound the
        # live prompt file on the developer's machine to whichever World it
        # played; with the active World it was invisible, and the moment the
        # suite played a different one the next real run rolled its lighting
        # against the wrong level's palette. Engage the sandbox explicitly:
        # it exports the SOMEWHERE_* redirects into os.environ, which is the
        # env the server inherits below, so it plays on copies.
        import authoring_sandbox
        sandbox_experiences = Path(authoring_sandbox.engage("standalone e2e")
                                   / "experience_store" / "experiences_dir")
        for var in ("SOMEWHERE_PROMPTS_PATH", "SOMEWHERE_PROMPTS_DEFAULTS_PATH",
                    "SOMEWHERE_WORLDS_DIR", "SOMEWHERE_EXPERIENCES_DIR",
                    "SOMEWHERE_TUNABLES_PATH"):
            env[var] = os.environ[var]
        # And play the SHIPPED experience, whatever this machine has selected.
        # In mock mode the run can only open on a World's cached first frame —
        # a machine whose active World has a stale or missing plate (any
        # prompt edit stamps it stale, and mock mode cannot redraw it) opens
        # on nothing, the client holds its opening blackout for 40s, and every
        # click here times out at 30. That is a fact about the developer's
        # selection, not about the build. Without `.active` the store resolves
        # the factory slug, whose plate ships in the tree.
        (sandbox_experiences / ".active").unlink(missing_ok=True)

        # Discard the server's stdout/stderr rather than piping it: the mock
        # server logs verbosely per request, and an unread PIPE fills its OS
        # buffer and deadlocks the server (page loads then hang). Nothing here
        # reads the server's output, so DEVNULL is the safe sink.
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
            shutil.rmtree(ROOT / "sessions" / cls.session_id, ignore_errors=True)

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.add_init_script(
            # /api/status is per-session; a bare fetch would read 'default'
            # instead of the session this page is bound to.
            "window._statusTurn = () => fetch("
            "  '/api/status?session_id=' + encodeURIComponent(window.__SOMEWHERE_SESSION__ || 'default')"
            ").then(r => r.json()).then(s => s.turn);"
        )
        # Always start each test from a clean game state, in this suite's own
        # session (see setUpClass) — the client threads ?session= through every
        # /api/* call it makes. ?mode=play skips the start menu so R (reset)
        # reaches the game instead of being swallowed by the menu takeover.
        self.page.goto(f"{self.base_url}/standalone?session={self.session_id}&mode=play")
        # The control menu starts collapsed; reset via its keyboard shortcut (R),
        # which works regardless of menu state, instead of the now-hidden button.
        self.page.keyboard.press("r")
        self._skip_opening_cutscene()
        # The generated choices are intentionally NOT shown (the player advances
        # via the forward hub or ACT); they're kept in the DOM so `moveForward`
        # can pick one. So wait for them to be ATTACHED, not visible.
        self.page.wait_for_selector(".choice-btn", state="attached", timeout=15000)

    def _skip_opening_cutscene(self, timeout_s: float = 45.0):
        """Get past the opening the way a player does, or the chrome is covered.

        An authored opening — or the level's approach montage — plays as a
        Moment over the whole viewport, so #forward-btn and #menu-toggle are in
        the DOM and not clickable. Playwright reports that as "element is not
        visible" on a button that is plainly there, which reads as a broken UI
        and is a cutscene nobody dismissed. Three tests in this file failed that
        way, and the same thing accounts for most of test_realtime_e2e.

        Escape is the player's skip: Cutscene.onEsc calls finish(), which POSTs
        /api/cutscene/complete, pops the Moment and releases the parked slate.
        """
        deadline = time.time() + timeout_s
        active_probe = ("() => !!(window.Cutscene && window.Cutscene.isActive "
                        "&& window.Cutscene.isActive())")
        while time.time() < deadline:
            try:
                if not self.page.evaluate(active_probe):
                    return
            except Exception:
                return
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(400)

    def _advance_the_turn(self):
        """Advance the story the way the UI does: click a choice.

        This used to click #forward-btn, and the hub it belonged to is gone —
        `standalone.css` ends with a "Hub reduction" block that sets
        #forward-btn, #free-will-btn, #scan-btn and #camp-btn to
        `display: none !important`, because the choice stack IS the turn
        interface now. So three tests in this file failed with Playwright's
        "element is not visible" on buttons that were deliberately retired,
        which reads as a broken UI rather than as a stale test.

        Returns the prose-entry count seen just before advancing.
        """
        before = len(self.page.query_selector_all(".prose-entry"))
        # Not the 4th row: that one is Custom, and it opens the typed-action
        # field instead of committing a turn.
        self.page.click(".choice-btn:not(.choice-btn-custom)")
        self.page.wait_for_function(
            f"document.querySelectorAll('.prose-entry').length > {before}",
            timeout=30000,
        )
        return before

    def tearDown(self):
        self.page.close()

    def test_standalone_page_loads(self):
        self.assertIn("SOMEWHERE", self.page.title())
        # The control menu starts COLLAPSED; the menu toggle is the persistent
        # chrome, and opening it reveals the control rail.
        self.assertTrue(self.page.is_visible("#menu-toggle"))
        self.assertFalse(self.page.is_visible("#control-rail"))
        self.page.click("#menu-toggle")
        self.page.wait_for_selector("#control-rail", state="visible", timeout=4000)
        self.assertTrue(self.page.is_visible("#control-rail"))
        # The narrative lives in the STORY LOG panel, which is collapsed by
        # default so it never obstructs the art. Open it from the rail (STORY)
        # and the prose feed — with the run's beats — is revealed.
        self.page.click("#btn-story")
        self.page.wait_for_selector("#prose-feed", state="visible", timeout=4000)
        self.assertTrue(self.page.is_visible("#prose-feed"))

    def test_reset_populates_prose_and_choices(self):
        entries = self.page.query_selector_all(".prose-entry")
        self.assertGreaterEqual(len(entries), 1)
        # Choices are hidden by design but must exist in the DOM so the forward
        # hub / keyboard shortcuts can commit one.
        choices = self.page.query_selector_all(".choice-btn")
        self.assertGreaterEqual(len(choices), 1)

    def test_backend_tag_shows_mock(self):
        self.page.wait_for_function(
            "document.getElementById('backend-name').textContent !== '—'", timeout=10000
        )
        # #backend-tag has CSS text-transform: uppercase, so innerText
        # (rendered text) is "MOCK" even though textContent is "mock".
        backend_text = self.page.inner_text("#backend-name")
        self.assertEqual(backend_text.strip().lower(), "mock")

    def test_the_choice_stack_advances_the_turn(self):
        # The choice stack is the turn interface (see the Hub reduction block
        # at the end of standalone.css). Clicking a row commits that action.
        self._advance_the_turn()
        # Then the turn resolves and a fresh choice set eventually appears.
        self.page.wait_for_selector(".choice-btn", state="attached", timeout=20000)
        choices_after = self.page.query_selector_all(".choice-btn")
        self.assertGreaterEqual(len(choices_after), 1)

    def test_the_retired_hubs_are_really_gone(self):
        """The reduction is deliberate, so pin it — otherwise the next person to
        see these ids in the DOM will "fix" the CSS and put the hub back."""
        for hub in ("#forward-btn", "#free-will-btn", "#scan-btn", "#camp-btn"):
            with self.subTest(hub=hub):
                self.assertIsNotNone(self.page.query_selector(hub),
                                     f"{hub} left the DOM — update these tests")
                self.assertFalse(self.page.is_visible(hub),
                                 f"{hub} is visible again")

    def test_keyboard_shortcut_1_picks_first_choice(self):
        prose_count_before = len(self.page.query_selector_all(".prose-entry"))
        self.page.keyboard.press("1")
        self.page.wait_for_function(
            f"document.querySelectorAll('.prose-entry').length > {prose_count_before}",
            timeout=10000,
        )

    def test_free_text_custom_action_submits(self):
        prose_count_before = len(self.page.query_selector_all(".prose-entry"))
        # ACT is the 4th row of the choice stack now, not a hub button — the
        # Hub reduction retired #free-will-btn. The row opens the same typed
        # field it always gated.
        self.page.click(".choice-btn-custom")
        self.page.fill("#custom-input", "Search the wreckage for supplies")
        self.page.click("#custom-submit")
        self.page.wait_for_function(
            f"document.querySelectorAll('.prose-entry').length > {prose_count_before}",
            timeout=30000,
        )
        # Input should clear after submission.
        self.assertEqual(self.page.input_value("#custom-input"), "")

    def test_vhs_toggle_changes_overlay_state(self):
        overlay = self.page.query_selector("#vhs-overlay")
        initial_class = overlay.get_attribute("class") or ""
        self.assertIn("vhs-on", initial_class)
        # VHS toggles via its keyboard shortcut (V) — the button lives in the
        # collapsed menu, but the shortcut works regardless of menu state.
        self.page.keyboard.press("v")
        self.page.wait_for_function(
            "!document.getElementById('vhs-overlay').classList.contains('vhs-on')",
            timeout=5000,
        )
        self.page.keyboard.press("v")
        self.page.wait_for_function(
            "document.getElementById('vhs-overlay').classList.contains('vhs-on')",
            timeout=5000,
        )

    def test_turn_count_increments_via_status_api(self):
        # The visible top HUD was removed, so verify the underlying contract the
        # UI relies on directly: /api/status.turn advances after one completed
        # turn (this legacy path used to leave it frozen at 0).
        # setUp's reset is applied asynchronously server-side, so wait for the
        # turn to settle back to 0 before asserting the baseline — otherwise a
        # prior test's turn can still be read in and this races to a false fail.
        self.page.wait_for_function(
            "_statusTurn().then(t => t === 0)",
            timeout=10000,
        )
        self._advance_the_turn()
        self.page.wait_for_selector(".choice-btn", state="attached", timeout=20000)
        self.page.wait_for_function(
            "_statusTurn().then(t => t >= 1)",
            timeout=10000,
        )

    def test_inventory_hud_hidden_when_empty(self):
        # With no items picked up, the inventory HUD stays hidden.
        self.assertTrue(self.page.is_hidden("#inventory-hud"))

    def test_death_overlay_present_but_hidden(self):
        # The death overlay must exist in the DOM (so death can surface) but
        # stay hidden during normal play.
        self.assertTrue(self.page.query_selector("#death-overlay") is not None)
        self.assertTrue(self.page.is_hidden("#death-overlay"))

    def test_scan_tutorial_is_gone(self):
        # The first-run "How to play" overlay used to intercept clicks on Play.
        # It is removed entirely (not localStorage-gated), so it must not exist.
        self.assertIsNone(self.page.query_selector("#scan-tutorial"))
        self.assertIsNone(self.page.query_selector("#tut-dismiss"))

    def test_scene_audio_requested_for_a_scene(self):
        # Scoring a scene must POST its descriptor to /api/scene_audio (the
        # ElevenLabs music + world-SFX bridge). Mock mode disables image
        # generation, so we drive the exposed SceneAudio module directly — the
        # same code path the scene_image / onGuideImage handlers use — and
        # assert the request the client makes. Runs fully offline (no key ->
        # server replies audio_url: null and the client stays silent).
        self.page.wait_for_function("() => !!window.SceneAudio", timeout=10000)
        with self.page.expect_request("**/api/scene_audio") as req_info:
            self.page.evaluate(
                "() => window.SceneAudio.score('a dark abandoned facility, flickering lights, dread')"
            )
        req = req_info.value
        self.assertEqual(req.method, "POST")
        self.assertIn("abandoned facility", (req.post_data or ""))

    def test_scene_audio_endpoint_degrades_without_key(self):
        # With no ELEVENLABS_API_KEY the endpoint must degrade gracefully to
        # {"audio_url": null} rather than erroring, so the UI simply stays silent.
        result = self.page.evaluate(
            """async () => {
                const r = await fetch('/api/scene_audio', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ prompt: 'a quiet forest at dawn' }),
                });
                return { ok: r.ok, body: await r.json() };
            }"""
        )
        self.assertTrue(result["ok"])
        self.assertIsNone(result["body"].get("audio_url"))


if __name__ == "__main__":
    unittest.main()
