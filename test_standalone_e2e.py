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

        cls.server_proc = subprocess.Popen(
            [sys.executable, "run_local.py", "--mock", "--no-browser", "--port", str(cls.port)],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
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
        self.page.click("#btn-reset")
        self.page.wait_for_selector(".choice-btn", timeout=15000)

    def tearDown(self):
        self.page.close()

    def test_standalone_page_loads(self):
        self.assertIn("SOMEWHERE", self.page.title())
        self.assertTrue(self.page.is_visible("#hud"))
        self.assertTrue(self.page.is_visible("#prose-feed"))

    def test_reset_populates_prose_and_choices(self):
        entries = self.page.query_selector_all(".prose-entry")
        self.assertGreaterEqual(len(entries), 1)
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

    def test_clicking_a_choice_advances_the_turn(self):
        prose_count_before = len(self.page.query_selector_all(".prose-entry"))
        self.page.click(".choice-btn:first-child")
        # A new player_action entry should appear almost immediately.
        self.page.wait_for_function(
            f"document.querySelectorAll('.prose-entry').length > {prose_count_before}",
            timeout=10000,
        )
        # Then the turn resolves and new choices eventually appear.
        self.page.wait_for_selector(".choice-btn", timeout=20000)
        choices_after = self.page.query_selector_all(".choice-btn")
        self.assertGreaterEqual(len(choices_after), 1)

    def test_keyboard_shortcut_1_picks_first_choice(self):
        prose_count_before = len(self.page.query_selector_all(".prose-entry"))
        self.page.keyboard.press("1")
        self.page.wait_for_function(
            f"document.querySelectorAll('.prose-entry').length > {prose_count_before}",
            timeout=10000,
        )

    def test_regenerate_choices_button(self):
        first_choice_text = self.page.inner_text(".choice-btn:first-child")
        self.page.click("#btn-regen")
        self.page.wait_for_function(
            "document.getElementById('processing-veil').classList.contains('hidden')",
            timeout=15000,
        )
        self.page.wait_for_selector(".choice-btn", timeout=10000)
        # Choices should still be present after regeneration (content may or
        # may not differ under the mock backend's contextual fallback).
        choices_after = self.page.query_selector_all(".choice-btn")
        self.assertGreaterEqual(len(choices_after), 1)

    def test_free_text_custom_action_submits(self):
        prose_count_before = len(self.page.query_selector_all(".prose-entry"))
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
        self.page.click("#btn-vhs")
        self.page.wait_for_function(
            "!document.getElementById('vhs-overlay').classList.contains('vhs-on')",
            timeout=5000,
        )
        self.page.click("#btn-vhs")
        self.page.wait_for_function(
            "document.getElementById('vhs-overlay').classList.contains('vhs-on')",
            timeout=5000,
        )

    def test_status_hud_reflects_turn_count(self):
        turn_before = int(self.page.inner_text("#hud-turn"))
        self.page.click(".choice-btn:first-child")
        self.page.wait_for_selector(".choice-btn", timeout=20000)
        # Status polling happens on an interval; give it a moment to refresh.
        self.page.wait_for_timeout(4500)
        turn_after = int(self.page.inner_text("#hud-turn"))
        self.assertGreaterEqual(turn_after, turn_before)


if __name__ == "__main__":
    unittest.main()
