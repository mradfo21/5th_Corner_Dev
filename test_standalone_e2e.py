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

        env = os.environ.copy()
        env["GEMINI_API_KEY"] = ""
        env["OPENAI_API_KEY"] = ""
        env["ANTHROPIC_API_KEY"] = ""

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

    def setUp(self):
        self.page = self.browser.new_page()
        # Always start each test from a clean game state.
        self.page.goto(f"{self.base_url}/standalone")
        # The control menu starts collapsed; reset via its keyboard shortcut (R),
        # which works regardless of menu state, instead of the now-hidden button.
        self.page.keyboard.press("r")
        # The generated choices are intentionally NOT shown (the player advances
        # via the forward hub or ACT); they're kept in the DOM so `moveForward`
        # can pick one. So wait for them to be ATTACHED, not visible.
        self.page.wait_for_selector(".choice-btn", state="attached", timeout=15000)

    def _advance_via_forward(self):
        """Advance the story the way the UI does: the forward (play) hub commits
        one of the hidden generated choices at random. Returns the prose-entry
        count seen just before advancing."""
        before = len(self.page.query_selector_all(".prose-entry"))
        self.page.click("#forward-btn")
        self.page.wait_for_function(
            f"document.querySelectorAll('.prose-entry').length > {before}",
            timeout=10000,
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

    def test_forward_hub_advances_the_turn(self):
        # The forward (play) hub commits one of the generated choices at random.
        self._advance_via_forward()
        # Then the turn resolves and a fresh choice set eventually appears.
        self.page.wait_for_selector(".choice-btn", state="attached", timeout=20000)
        choices_after = self.page.query_selector_all(".choice-btn")
        self.assertGreaterEqual(len(choices_after), 1)

    def test_keyboard_shortcut_1_picks_first_choice(self):
        prose_count_before = len(self.page.query_selector_all(".prose-entry"))
        self.page.keyboard.press("1")
        self.page.wait_for_function(
            f"document.querySelectorAll('.prose-entry').length > {prose_count_before}",
            timeout=10000,
        )

    def test_free_text_custom_action_submits(self):
        prose_count_before = len(self.page.query_selector_all(".prose-entry"))
        # The custom-action field is gated behind the ACT (free-will) hub — it's
        # hidden until you open it, so open the gate before typing.
        self.page.click("#free-will-btn")
        self.page.fill("#custom-input", "Search the wreckage for supplies")
        self.page.click("#custom-submit")
        self.page.wait_for_function(
            f"document.querySelectorAll('.prose-entry').length > {prose_count_before}",
            timeout=10000,
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
            "fetch('/api/status').then(r => r.json()).then(s => s.turn === 0)",
            timeout=10000,
        )
        self._advance_via_forward()
        self.page.wait_for_selector(".choice-btn", state="attached", timeout=20000)
        self.page.wait_for_function(
            "fetch('/api/status').then(r => r.json()).then(s => s.turn >= 1)",
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


if __name__ == "__main__":
    unittest.main()
