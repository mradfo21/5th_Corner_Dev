"""
End-to-end tests for the editor's dots — the whole authoring surface.

The editor used to be 37 nodes deep in a mirror of the prompt file. It is now
one red dot: double-tap it and four dots bloom around it — Level, Character,
Game, Controls — and tapping one brings up the handful of fields that steer it.
Everything else (the engine's contract prompts, the runtime knobs, saved levels
and builds) moved behind the header's List toggle.

These tests hold that shape:
  · it opens as ONE dot, and the engine is genuinely not in here
  · the dot opens into exactly four, and empty paper closes it again
  · a window carries the essentials only — no switch, no ⓘ, no disclosures
  · typing turns the sheet on, which is what makes it reach a model
  · Controls hosts the REAL movement strip, and gives it back
  · the dots never sit still, and you can still hit one that is moving
  · the flat list still has the whole surface

Run: python3 -m unittest test_editor_graph_e2e
The two tests that persist anything capture the block first and restore it in a
`finally`, so a run leaves prompts.json exactly as it found it.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.request

try:
    from playwright.sync_api import sync_playwright
    HAVE_PW = True
except ImportError:  # pragma: no cover
    HAVE_PW = False

PHONE = {"width": 430, "height": 932}
# The four dots, in ring order starting at 12 o'clock.
DOTS = ["dot:level", "dot:character", "dot:game", "dot:controls"]


def _find_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@unittest.skipUnless(HAVE_PW, "playwright not installed")
class TestEditorDots(unittest.TestCase):
    """Drive the dots in a real browser, on a phone-sized viewport."""

    @classmethod
    def setUpClass(cls):
        cls.port = _find_free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        env = dict(os.environ)
        env["MOCK_MODE"] = "1"
        cls.proc = subprocess.Popen(
            [sys.executable, "run_local.py", "--mock", "--no-browser",
             "--port", str(cls.port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        )
        for _ in range(120):
            try:
                urllib.request.urlopen(cls.base_url + "/", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        else:  # pragma: no cover
            raise RuntimeError("server did not start")
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close()
            cls.playwright.stop()
        finally:
            cls.proc.terminate()
            cls.proc.wait(timeout=10)

    # ---- server helpers --------------------------------------------------
    def _studio_content(self) -> dict:
        with urllib.request.urlopen(self.base_url + "/api/admin/studio/content") as r:
            body = json.loads(r.read().decode())
        return body.get("data", body)

    def _identity(self, block: str) -> dict:
        return (self._studio_content().get("identity") or {}).get(block) or {}

    def _put_identity(self, block: str, patch: dict) -> None:
        req = urllib.request.Request(
            self.base_url + "/api/admin/studio/identity",
            data=json.dumps({block: patch}).encode(),
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        urllib.request.urlopen(req).read()

    # ---- browser helpers -------------------------------------------------
    def setUp(self):
        self.errors = []
        self.page = self.browser.new_page(
            viewport=PHONE, is_mobile=True, has_touch=True)
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.add_init_script(
            "try { localStorage.setItem('scan_tutorial_seen_v1', '1'); } catch (e) {}")
        self.page.goto(f"{self.base_url}/standalone")
        self.page.keyboard.press("r")
        self.page.wait_for_selector(".choice-btn", state="attached", timeout=20000)
        # ` opens the editor (the EDIT rail button is behind the collapsed menu).
        self.page.keyboard.press("`")
        self.page.wait_for_selector("#we-graph", state="visible", timeout=10000)
        self.page.wait_for_function(
            "() => document.querySelectorAll('#eg-world .eg-node').length > 0",
            timeout=10000)
        self._settle()

    def tearDown(self):
        try:
            self.assertEqual(self.errors, [], f"page errors: {self.errors}")
        finally:
            self.page.close()

    def _settle(self, ms: int = 900):
        """Let the open/close animation (620ms) finish."""
        self.page.wait_for_timeout(ms)

    def _dot(self, node_id: str):
        """Live centre of a dot in client pixels — they move, so ask now."""
        pt = self.page.evaluate("(id) => window.EditorGraph.dotAt(id)", node_id)
        self.assertIsNotNone(pt, f"no dot drawn for {node_id}")
        return pt

    def _tap(self, node_id: str):
        pt = self._dot(node_id)
        self.page.mouse.click(pt["x"], pt["y"])

    def _open_ring(self):
        pt = self._dot("game")
        self.page.mouse.dblclick(pt["x"], pt["y"])
        self._settle()

    def _tap_paper(self):
        """Somewhere with no dot on it. The corner is always empty."""
        box = self.page.evaluate(
            """() => { const r = document.getElementById('eg-canvas').getBoundingClientRect();
                       return {x: r.x, y: r.y, w: r.width, h: r.height}; }""")
        x, y = box["x"] + 14, box["y"] + 14
        self.assertEqual(
            self.page.evaluate("([x, y]) => window.EditorGraph.probe(x, y).where", [x, y]),
            "empty", "expected the top-left corner to be empty paper")
        self.page.mouse.click(x, y)
        self._settle()

    def _unlock_machine_room(self):
        """Shift+` reveals the door to the flat list. A player never sees it."""
        self.page.keyboard.press("~")
        self.page.wait_for_timeout(250)
        self.page.wait_for_selector("#we-view", state="visible", timeout=3000)

    def _shown(self):
        return self.page.evaluate(
            """() => Array.from(document.querySelectorAll('#eg-world .eg-node'))
                          .filter(g => g.style.display !== 'none')
                          .map(g => g.getAttribute('data-id'))""")

    def _labels(self):
        return self.page.evaluate(
            """() => Array.from(document.querySelectorAll('#eg-world .eg-node'))
                          .filter(g => g.style.display !== 'none' &&
                                       !g.classList.contains('is-core'))
                          .map(g => g.querySelector('.eg-name').textContent)""")

    # ---- tests -----------------------------------------------------------
    def test_the_editor_opens_as_one_red_dot(self):
        """One dot, named, filled. Nothing else on the sheet."""
        self.assertEqual(self._shown(), ["game"])
        self.assertEqual(self.page.text_content("#eg-caption-name").strip(), "Game")
        self.assertEqual(
            self.page.evaluate(
                """() => document.querySelector('#eg-world .eg-node[data-id="game"] .eg-name')
                                 .textContent"""), "Game")
        # Filled in the editor's one red, not outlined like the rest.
        fill = self.page.evaluate(
            """() => getComputedStyle(document.querySelector(
                 '#eg-world .eg-node[data-id="game"] .eg-cell')).fill""")
        self.assertEqual(fill, "rgb(224, 48, 28)")
        self.assertTrue(self.page.evaluate(
            """() => document.querySelector('#eg-world .eg-node[data-id="game"]')
                             .classList.contains('is-alone')"""))
        # The flat column isn't rendered behind it.
        self.assertEqual(
            self.page.evaluate(
                "getComputedStyle(document.querySelector('.we-scroll')).display"), "none")

    def test_the_dot_opens_into_exactly_four(self):
        """Level, Character, Game, Controls — the four things you direct."""
        self._open_ring()
        self.assertEqual(sorted(self._shown()), sorted(["game"] + DOTS))
        self.assertEqual(self._labels(), ["Level", "Character", "Game", "Controls"])
        # The nucleus drops its name once the ring is out, so "Game" appears once.
        self.assertEqual(
            self.page.evaluate(
                """() => getComputedStyle(document.querySelector(
                     '#eg-world .eg-node.is-core .eg-name')).display"""), "none")

    def test_the_engine_is_not_in_here_any_more(self):
        """The rulebook, the runtime knobs and the galleries are gone from the
        dots. They are still in the flat list; they are just not what you meet."""
        self._open_ring()
        ids = self.page.evaluate(
            """() => Array.from(document.querySelectorAll('#eg-world .eg-node'))
                          .map(g => g.getAttribute('data-id'))""")
        self.assertEqual(sorted(ids), sorted(["game"] + DOTS))
        for gone in ("prompt:", "control:", "build:", "level:", "group:", "layer:", "new:"):
            self.assertFalse([i for i in ids if i.startswith(gone)],
                             f"{gone}* should not be in the dots any more")

    def test_empty_paper_closes_the_ring(self):
        """With dots this small there is more paper than dot, which makes
        leaving easier than arriving — the right way round."""
        self._open_ring()
        self.assertEqual(len(self._shown()), 5)
        self._tap_paper()
        self.assertEqual(self._shown(), ["game"])

    def test_escape_closes_the_ring_then_the_editor(self):
        self._open_ring()
        self.page.keyboard.press("Escape")
        self._settle()
        self.assertEqual(self._shown(), ["game"])
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(500)
        self.assertFalse(self.page.evaluate(
            "document.body.classList.contains('world-editor-on')"),
            "Escape at the top should close the editor")

    def test_a_window_carries_the_essentials_and_nothing_to_read(self):
        """Four fields for a place. No switch, no ⓘ, no advanced disclosure, no
        compiled-output pane — all of which the full list still has."""
        self._open_ring()
        self._tap("dot:level")
        self.page.wait_for_selector("#eg-sheet.is-open", timeout=4000)
        self._settle(500)
        self.assertEqual(self.page.text_content("#eg-sheet-title").strip(), "Level")
        self.assertEqual(
            self.page.eval_on_selector_all(
                "#eg-sheet-body .we-cast-label", "els => els.map(e => e.textContent)"),
            ["Name", "What it is", "Landmarks", "Opening shot"])
        body = "#eg-sheet-body "
        self.assertEqual(self.page.eval_on_selector_all(body + ".we-info", "e => e.length"), 0)
        self.assertEqual(self.page.eval_on_selector_all(body + ".we-more", "e => e.length"), 0)
        self.assertEqual(
            self.page.eval_on_selector_all(body + ".we-more-compiled", "e => e.length"), 0)
        self.assertEqual(
            self.page.eval_on_selector_all(body + ".we-block-head", "e => e.length"), 0)
        # The "Use this level" switch is gone: typing is the opt-in.
        self.assertEqual(
            self.page.eval_on_selector_all(
                body + '.we-cast-toggle input[type="checkbox"]', "e => e.length"), 0)

    def test_typing_turns_the_sheet_on(self):
        """The sheets ship blank AND off, so filling one in used to change
        nothing at all. Words in a field now carry the switch with them."""
        before = self._identity("setting_reference")
        try:
            self._put_identity("setting_reference", {"enabled": False, "name": ""})
            self.page.reload()
            self.page.keyboard.press("r")
            self.page.wait_for_selector(".choice-btn", state="attached", timeout=20000)
            self.page.keyboard.press("`")
            self.page.wait_for_selector("#we-graph", state="visible", timeout=10000)
            self._settle()
            self._open_ring()
            self._tap("dot:level")
            self.page.wait_for_selector("#eg-sheet.is-open", timeout=4000)
            self._settle(500)

            box = self.page.query_selector("#eg-sheet-body input[type='text']")
            box.click()
            box.fill("The Kettle Yard")
            self.page.keyboard.press("Enter")      # blur commits
            self.page.wait_for_timeout(1200)

            saved = self._identity("setting_reference")
            self.assertEqual(saved.get("name"), "The Kettle Yard")
            self.assertTrue(saved.get("enabled"),
                            "writing into a minimal sheet should switch it on")
        finally:
            self._put_identity("setting_reference", {
                "enabled": bool(before.get("enabled")),
                "name": before.get("name", ""),
            })

    def test_a_dot_with_words_in_it_is_marked(self):
        """The one piece of state a dot carries: is this steering the game?"""
        before = self._identity("game_design")
        try:
            self._put_identity("game_design", {"genre": "", "tone": "",
                                               "threat_model": "", "enabled": False})
            self.page.reload()
            self.page.keyboard.press("r")
            self.page.wait_for_selector(".choice-btn", state="attached", timeout=20000)
            self.page.keyboard.press("`")
            self.page.wait_for_selector("#we-graph", state="visible", timeout=10000)
            self._settle()
            self._open_ring()
            self.assertFalse(self.page.evaluate(
                """() => document.querySelector('#eg-world .eg-node[data-id="dot:game"]')
                                 .classList.contains('is-set')"""),
                "an empty sheet should not be marked")

            # Through the window, the way a person would: the mark has to appear
            # off the back of the save, without a reload.
            self._tap("dot:game")
            self.page.wait_for_selector("#eg-sheet.is-open", timeout=4000)
            self._settle(500)
            box = self.page.query_selector("#eg-sheet-body input[type='text']")
            box.click()
            box.fill("found-footage horror")
            self.page.keyboard.press("Enter")
            self.page.wait_for_timeout(1200)
            self.page.keyboard.press("Escape")
            self._settle(600)
            self.assertTrue(self.page.evaluate(
                """() => document.querySelector('#eg-world .eg-node[data-id="dot:game"]')
                                 .classList.contains('is-set')"""),
                "a sheet with words in it should be marked")
        finally:
            self._put_identity("game_design", {
                "genre": before.get("genre", ""),
                "tone": before.get("tone", ""),
                "threat_model": before.get("threat_model", ""),
                "enabled": bool(before.get("enabled")),
            })

    def test_controls_holds_the_camera_and_the_real_movement_strip(self):
        """How you see it and how you move, in one window. The CONTROLS strip is
        the panel's own wired element on loan, not a second copy of it."""
        self._open_ring()
        self._tap("dot:controls")
        self.page.wait_for_selector("#eg-sheet.is-open", timeout=4000)
        self._settle(500)
        # Four perspectives, by name only — the taglines are teaching copy.
        self.assertEqual(
            self.page.eval_on_selector_all(
                "#eg-sheet-body .we-mode-name", "els => els.map(e => e.textContent)"),
            ["First person", "Over the shoulder", "Third person", "Fixed cinematic"])
        self.assertEqual(
            self.page.eval_on_selector_all("#eg-sheet-body .we-mode-tag", "e => e.length"), 0)
        # The live strip itself, moved in.
        self.assertTrue(self.page.evaluate(
            "!!document.querySelector('#eg-sheet-body #we-input-opts')"))

        self.page.click("#eg-sheet-body #we-input-profile button:nth-child(2)")
        self.page.wait_for_timeout(300)
        self.assertEqual(
            self.page.evaluate("localStorage.getItem('input_profile')"), "fps")

        # And it has to go home, or its listeners leave with the innerHTML.
        self.page.keyboard.press("Escape")
        self._settle(600)
        self.assertTrue(self.page.evaluate(
            "!!document.querySelector('#world-editor > #we-input-opts')"),
            "the strip should be back in the panel after the window closes")
        self.page.evaluate("localStorage.setItem('input_profile', 'doom')")

    def test_the_header_carries_only_the_way_out(self):
        """Text size, width and the machine-room door all moved into Controls.
        A settings row you have to read past is the interface apologising."""
        tools = self.page.eval_on_selector_all(
            "#world-editor .we-head-tools button",
            """els => els.filter(e => e.offsetParent !== null)
                        .map(e => e.id)""")
        self.assertEqual(tools, ["we-close"])
        # The footer's Revert / Apply Live / Save & Restart is the prompt
        # pipeline's control panel. Nothing in the dots needs it.
        self.assertFalse(self.page.is_visible("#we-foot"))
        # And they are all still there, inside Controls.
        self._open_ring()
        self._tap("dot:controls")
        self.page.wait_for_selector("#eg-sheet.is-open", timeout=4000)
        self._settle(500)
        self.assertTrue(self.page.evaluate(
            "!!document.querySelector('#eg-sheet-body #we-panel-opts')"))
        self.assertEqual(
            self.page.eval_on_selector_all(
                "#eg-sheet-body .eg-group .we-cast-label",
                "els => els.map(e => e.textContent)"),
            ["Movement", "Panel"])

    def test_the_machine_room_is_not_offered_to_a_player(self):
        """The flat list holds the engine's contract prompts and the runtime
        knobs. It stays reachable for us, and undrawn for everyone else."""
        self.assertFalse(self.page.is_visible("#we-view"),
                         "the List door should not be drawn by default")
        self._unlock_machine_room()
        self.assertTrue(self.page.is_visible("#we-view"))
        self._unlock_machine_room_off()
        self.assertFalse(self.page.is_visible("#we-view"))

    def _unlock_machine_room_off(self):
        self.page.keyboard.press("~")
        self.page.wait_for_timeout(250)

    def test_the_dots_never_sit_still(self):
        """Sprung to a slot, drifting, pushing off each other — and still
        hittable, because the hit test reads live positions rather than slots."""
        self._open_ring()
        first = self._dot("dot:level")
        self.page.wait_for_timeout(1400)
        later = self._dot("dot:level")
        moved = ((first["x"] - later["x"]) ** 2 + (first["y"] - later["y"]) ** 2) ** 0.5
        self.assertGreater(moved, 0.4, "the dots should drift")
        # ...but not far enough to lose the composition.
        self.assertLess(moved, later["r"], "drift should stay inside a dot's own radius")
        # Nothing may end up sitting on the nucleus.
        core = self._dot("game")
        for node_id in DOTS:
            d = self._dot(node_id)
            gap = ((core["x"] - d["x"]) ** 2 + (core["y"] - d["y"]) ** 2) ** 0.5
            self.assertGreater(gap, core["r"] + d["r"],
                               f"{node_id} should clear the nucleus")
        # A moving target is still a target.
        pt = self._dot("dot:character")
        self.assertEqual(
            self.page.evaluate("([x, y]) => window.EditorGraph.probe(x, y).id",
                               [pt["x"], pt["y"]]), "dot:character")

    def test_the_flat_list_still_has_the_whole_surface(self):
        """Nothing was deleted, only demoted: with the machine room unlocked the
        engine's prompts are one click away, and the dots come back the same."""
        self._unlock_machine_room()
        self.page.click("#we-view")
        self.page.wait_for_timeout(400)
        self.assertFalse(self.page.evaluate(
            "document.body.classList.contains('we-graph-mode')"))
        self.page.click('#we-tabs [data-tab="engine"]')
        self.page.wait_for_timeout(400)
        cards = self.page.eval_on_selector_all(
            "#we-fields .we-card", "els => els.length")
        self.assertGreater(cards, 0, "the engine's prompts should still be editable")
        self.assertTrue(self.page.is_visible("#btn-model"),
                        "the runtime controls should still be reachable")
        self.page.click("#we-view")
        self.page.wait_for_timeout(500)
        self.assertEqual(self._shown(), ["game"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
