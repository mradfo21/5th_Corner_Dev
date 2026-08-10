"""
test_editor_graph_e2e.py — Playwright end-to-end tests for THE ORGANISM, the
World Editor's recursive circle graph (static/js/editor_graph.js, designed in
EDITOR_GRAPH_PLAN.md).

The graph is the editor's default view, so these tests cover the whole
navigation contract the old tab strip used to carry:

  * the root cell is THE GAME, and its children are the layers plus Builds
  * one tap selects a cell, the next (or a double-tap) dives into it, and the
    breadcrumb trail follows you down
  * tapping the periphery — the parent's membrane, visible as a halo around
    the framed cell — surfaces you back up, and so does Esc
  * a vertex (a prompt, a spec sheet) animates the window up, and edits made
    there land in the same unsaved-edits buffer the flat list uses
  * a runtime-control vertex IS its toggle: tapping it clicks the live button
  * the flat list is still one click away and still works

Anything that writes to disk (prompts.json) captures the original value first
and restores it in a finally, so a test run leaves the repo's prompt file
exactly as it found it.

Requirements (see requirements-dev.txt):
    pip install -r requirements-dev.txt
    playwright install chromium

Run with:
    python3 -m unittest test_editor_graph_e2e -v
"""

import json
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

PHONE = {"width": 430, "height": 932}


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(base_url: str, timeout_s: float = 30.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


@unittest.skipUnless(PLAYWRIGHT_AVAILABLE, "playwright not installed — see requirements-dev.txt")
class TestEditorGraphE2E(unittest.TestCase):
    """Drive the graph in a real browser, on a phone-sized viewport."""

    @classmethod
    def setUpClass(cls):
        cls.port = _find_free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"

        env = os.environ.copy()
        env["GEMINI_API_KEY"] = ""
        env["OPENAI_API_KEY"] = ""
        env["ANTHROPIC_API_KEY"] = ""

        # DEVNULL, not PIPE: the mock server logs per request and an unread pipe
        # fills its OS buffer and deadlocks the server mid-page-load.
        cls.server_proc = subprocess.Popen(
            [sys.executable, "run_local.py", "--mock", "--no-browser", "--port", str(cls.port)],
            cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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

    # ---- studio API, for capture/restore around anything that persists ----
    def _studio_content(self) -> dict:
        with urllib.request.urlopen(f"{self.base_url}/api/admin/studio/content", timeout=10) as r:
            payload = json.loads(r.read().decode("utf-8"))
        return payload.get("data", payload)

    def _put_prompt(self, key: str, value: str) -> None:
        req = urllib.request.Request(
            f"{self.base_url}/api/admin/studio/prompts",
            data=json.dumps({"data": {key: value}}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        urllib.request.urlopen(req, timeout=10).read()

    # ---- browser helpers -------------------------------------------------
    def setUp(self):
        self.page = self.browser.new_page(viewport=PHONE, is_mobile=True, has_touch=True)
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.add_init_script(
            "try { localStorage.setItem('scan_tutorial_seen_v1', '1'); } catch (e) {}"
        )
        self.page.goto(f"{self.base_url}/standalone")
        self.page.keyboard.press("r")
        self.page.wait_for_selector(".choice-btn", state="attached", timeout=20000)
        # ` opens the editor (the EDIT rail button is behind the collapsed menu).
        self.page.keyboard.press("`")
        self.page.wait_for_selector("#we-graph", state="visible", timeout=10000)
        # The root cell only exists once the studio content has loaded.
        self.page.wait_for_function(
            """() => document.querySelectorAll('#eg-world .eg-node.is-child').length > 0""",
            timeout=10000)
        self._settle()

    def tearDown(self):
        try:
            self.assertEqual(self.errors, [], f"page errors: {self.errors}")
        finally:
            self.page.close()

    def _settle(self, ms: int = 900):
        """Let the zoom animation (620ms) finish."""
        self.page.wait_for_timeout(ms)

    def _centre(self, node_id: str):
        pt = self.page.evaluate(
            """(id) => {
                 const c = document.querySelector(
                   '#eg-world .eg-node[data-id="' + id + '"] .eg-cell');
                 if (!c) return null;
                 const r = c.getBoundingClientRect();
                 return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
               }""", node_id)
        self.assertIsNotNone(pt, f"no cell drawn for {node_id}")
        return pt

    def _tap(self, node_id: str):
        pt = self._centre(node_id)
        self.page.mouse.click(pt["x"], pt["y"])

    def _dive(self, node_id: str):
        pt = self._centre(node_id)
        self.page.mouse.dblclick(pt["x"], pt["y"])
        self._settle()

    def _trail(self):
        return self.page.evaluate(
            """() => Array.from(document.querySelectorAll('#eg-trail .eg-crumb button'))
                          .map(b => b.textContent)""")

    def _children(self):
        return self.page.evaluate(
            """() => Array.from(document.querySelectorAll('#eg-world .eg-node.is-child'))
                          .map(g => g.getAttribute('data-id'))""")

    # ---- tests -----------------------------------------------------------
    def test_root_cell_is_the_game_with_a_layer_per_child(self):
        """The editor opens as one circle — the game — holding the layers."""
        self.assertEqual(self._trail(), ["The Game"])
        self.assertEqual(
            self.page.text_content("#eg-caption-name").strip(), "The Game")
        kids = self._children()
        for expected in ("layer:engine", "layer:game", "layer:level",
                         "layer:character", "group:builds"):
            self.assertIn(expected, kids)
        # Nothing deeper is interactive yet: you dive to reach it.
        self.assertNotIn("group:system", kids)
        # The flat list isn't rendered behind the graph.
        self.assertEqual(
            self.page.evaluate(
                "getComputedStyle(document.querySelector('.we-scroll')).display"), "none")

    def test_tap_selects_then_dives_and_the_trail_follows(self):
        """A tap says what a cell is; the next one goes inside it."""
        self._tap("layer:engine")
        self.assertEqual(self.page.text_content("#eg-caption-name").strip(), "Engine")
        self.assertTrue(self.page.evaluate(
            """() => document.querySelector('#eg-world .eg-node[data-id="layer:engine"]')
                             .classList.contains('is-selected')"""))
        self.assertEqual(self._trail(), ["The Game"], "selecting shouldn't move you")

        # The second tap dives — a gesture that only counts under 340ms reads
        # as a broken control, so this must work without double-tap timing.
        self._tap("layer:engine")
        self._settle()
        self.assertEqual(self._trail(), ["The Game", "Engine"])
        kids = self._children()
        self.assertIn("group:system", kids)
        self.assertIn("group:advanced:engine", kids)
        self.assertTrue(any(k.startswith("prompt:") for k in kids),
                        f"engine should hold prompt vertices, got {kids}")

    def test_periphery_and_escape_surface_back_up(self):
        """The parent's halo around the framed cell is the way back out."""
        self._dive("layer:engine")
        self._dive("group:system")
        self.assertEqual(self._trail(), ["The Game", "Engine", "System"])

        # A corner of the canvas is always outside the inscribed focus circle,
        # i.e. inside the parent — which is the "up" target.
        corner = self.page.evaluate(
            """() => {
                 const r = document.getElementById('eg-canvas').getBoundingClientRect();
                 return { x: r.x + 10, y: r.y + r.height - 10 };
               }""")
        self.page.mouse.click(corner["x"], corner["y"])
        self._settle()
        self.assertEqual(self._trail(), ["The Game", "Engine"])

        self.page.keyboard.press("Escape")
        self._settle()
        self.assertEqual(self._trail(), ["The Game"])
        # At the root there's nowhere left to surface to, so Esc closes.
        self.page.keyboard.press("Escape")
        self.page.wait_for_function(
            "() => !document.body.classList.contains('world-editor-on')", timeout=4000)

    def test_a_breadcrumb_jumps_straight_back(self):
        self._dive("layer:engine")
        self._dive("group:advanced:engine")
        self.assertEqual(len(self._trail()), 3)
        self.page.click('#eg-trail .eg-crumb button:has-text("The Game")')
        self._settle()
        self.assertEqual(self._trail(), ["The Game"])

    def test_prompt_vertex_opens_a_window_and_edits_share_the_buffer(self):
        """At a vertex you edit. The text goes to the same unsaved buffer the
        flat list uses, so the footer's Apply Live picks it up."""
        self._dive("layer:engine")
        key = self.page.evaluate(
            """() => (Array.from(document.querySelectorAll('#eg-world .eg-node.is-child'))
                       .map(g => g.getAttribute('data-id'))
                       .find(id => id.indexOf('prompt:') === 0) || '').slice(7)""")
        self.assertTrue(key, "engine should expose at least one prompt vertex")
        self._tap("prompt:" + key)
        self.page.wait_for_selector("#eg-sheet.is-open", timeout=4000)
        self.page.wait_for_timeout(500)

        original = self.page.input_value("#eg-sheet .eg-text")
        self.assertTrue(len(original) > 50, "the window should hold the real prompt")
        self.assertTrue(
            self.page.is_hidden("#we-dirty"), "nothing is unsaved yet")

        self.page.fill("#eg-sheet .eg-text", original + "\n\nA NOTE FROM THE GRAPH.")
        self.page.wait_for_selector("#we-dirty", state="visible", timeout=4000)
        self.assertIn("unsaved", self.page.text_content("#eg-sheet .eg-count"))

        # Closing the window keeps the edit (it's the shared buffer, not a
        # draft owned by the sheet) — and reopening the vertex shows it back.
        self.page.click("#eg-sheet-close")
        self.page.wait_for_selector("#eg-sheet.is-open", state="hidden", timeout=4000)
        self.assertTrue(self.page.is_visible("#we-dirty"))
        self._tap("prompt:" + key)
        self.page.wait_for_selector("#eg-sheet.is-open", timeout=4000)
        self.assertIn("A NOTE FROM THE GRAPH",
                      self.page.input_value("#eg-sheet .eg-text"))
        # Nothing was written to disk: the buffer is in-memory until you save.
        self.assertNotIn("A NOTE FROM THE GRAPH", self._studio_content()["prompts"][key])

    def test_the_window_hands_off_to_the_full_editor(self):
        """Long prose needs the pop-out editor (line numbers, diff, Ctrl+S), so
        the window hands the same prompt over rather than reimplementing it."""
        self._dive("layer:engine")
        key = self.page.evaluate(
            """() => (Array.from(document.querySelectorAll('#eg-world .eg-node.is-child'))
                       .map(g => g.getAttribute('data-id'))
                       .find(id => id.indexOf('prompt:') === 0) || '').slice(7)""")
        self._tap("prompt:" + key)
        self.page.wait_for_selector("#eg-sheet.is-open", timeout=4000)
        self.page.click('#eg-sheet .eg-acts button:has-text("Full editor")')
        self.page.wait_for_selector("#we-modal.open", timeout=4000)
        self.assertTrue(len(self.page.input_value("#wem-text")) > 50)
        # Esc cancels the modal, and the graph is still where you left it.
        self.page.keyboard.press("Escape")
        self.page.wait_for_selector("#we-modal.open", state="hidden", timeout=4000)
        self._settle()
        self.assertEqual(self._trail(), ["The Game", "Engine"])

    def test_saving_from_the_window_persists_and_clears_the_badge(self):
        self._dive("layer:engine")
        key = self.page.evaluate(
            """() => (Array.from(document.querySelectorAll('#eg-world .eg-node.is-child'))
                       .map(g => g.getAttribute('data-id'))
                       .find(id => id.indexOf('prompt:') === 0) || '').slice(7)""")
        before = self._studio_content()["prompts"][key]
        try:
            self._tap("prompt:" + key)
            self.page.wait_for_selector("#eg-sheet.is-open", timeout=4000)
            self.page.wait_for_timeout(500)
            self.page.fill("#eg-sheet .eg-text", before + "\n\nSAVED FROM THE GRAPH.")
            self.page.click('#eg-sheet .eg-acts button:has-text("Save")')
            self.page.wait_for_selector("#we-dirty", state="hidden", timeout=6000)
            self.assertIn("SAVED FROM THE GRAPH", self._studio_content()["prompts"][key])
        finally:
            self._put_prompt(key, before)
        self.assertEqual(self._studio_content()["prompts"][key], before)

    def test_control_vertex_is_its_own_toggle(self):
        """A runtime knob has nothing to configure, so the tap IS the switch —
        and it drives the same live button the old icon rail held."""
        self._dive("layer:engine")
        self._dive("group:system")
        was_on = self.page.evaluate(
            "() => document.getElementById('btn-vhs').classList.contains('on')")
        self._tap("control:btn-vhs")
        self.page.wait_for_function(
            """(was) => document.getElementById('btn-vhs').classList.contains('on') !== was""",
            arg=was_on, timeout=4000)
        # No window opens for a control, and the cell reflects the new state.
        self.assertTrue(self.page.is_hidden("#eg-sheet .eg-text"))
        self.page.wait_for_function(
            """(was) => {
                 const g = document.querySelector('#eg-world .eg-node[data-id="control:btn-vhs"]');
                 return !!g && g.classList.contains('is-on') !== was;
               }""", arg=was_on, timeout=4000)

    def test_spec_vertex_mounts_the_real_form(self):
        """The window hosts the actual cast form, not a second implementation."""
        self._dive("layer:game")
        block = self.page.evaluate(
            """() => Array.from(document.querySelectorAll('#eg-world .eg-node.is-child'))
                          .map(g => g.getAttribute('data-id'))
                          .find(id => id.indexOf('spec:') === 0)""")
        self.assertTrue(block, "the game layer owns spec sheets")
        self._tap(block)
        self.page.wait_for_selector("#eg-sheet.is-open .eg-spec .we-block", timeout=4000)
        self.assertTrue(self.page.is_visible("#eg-sheet .eg-spec .we-cast-toggle"))
        # One frame, not two: the window names the sheet, so the block's own
        # header is suppressed.
        self.assertEqual(
            self.page.evaluate(
                """() => getComputedStyle(document.querySelector(
                     '#eg-sheet .eg-spec .we-block-head')).display"""), "none")

    def test_levels_group_offers_saving_this_place(self):
        self._dive("layer:level")
        self.assertIn("group:levels", self._children())
        self._dive("group:levels")
        self.assertIn("new:level", self._children())
        self._tap("new:level")
        self.page.wait_for_selector("#eg-sheet.is-open .eg-name-form input", timeout=4000)
        self.assertEqual(self.page.text_content("#eg-sheet-title").strip(), "Save this level")

    def test_the_flat_list_is_still_one_click_away(self):
        """Two renderings of one state: the list keeps working, and the choice
        survives a reload."""
        self.page.click("#we-view")
        self.page.wait_for_selector('#we-tabs [data-tab="engine"]', state="visible", timeout=4000)
        self.assertEqual(
            self.page.evaluate(
                "getComputedStyle(document.getElementById('we-graph')).display"), "none")
        self.page.click('#we-tabs [data-tab="engine"]')
        self.page.wait_for_selector("#btn-model", state="visible", timeout=4000)

        self.page.click("#we-view")
        self.page.wait_for_selector("#we-graph", state="visible", timeout=4000)
        self.assertEqual(
            self.page.evaluate("() => localStorage.getItem('we_view')"), "graph")


if __name__ == "__main__":
    unittest.main()
