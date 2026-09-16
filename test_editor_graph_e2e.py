"""
End-to-end tests for the editor's dots — the whole authoring surface.

The editor used to be 37 nodes deep in a mirror of the prompt file. It is now
one red dot: double-tap it and three dots bloom around it — Level, Character,
World — and tapping one brings up the handful of fields that steer it.
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
# The top ring, in order starting at 12 o'clock.
DOTS = ["dot:level", "dot:character", "dot:world"]
HARNESS_LOOP = ["h:dot:choices", "h:dot:actions", "h:dot:picture", "h:dot:state"]
SOUND_DOTS = [
    "s:dot:palette", "s:dot:clicks", "s:dot:chrome", "s:dot:turn",
    "s:dot:lens", "s:dot:body", "s:dot:voice",
]
HARNESS_PICTURE = [
    "h:dot:system", "h:dot:framing", "h:dot:still", "h:dot:edit", "h:dot:film",
]
HARNESS_STATE = ["h:dot:evolve", "h:dot:scene"]
# Inside GAME, and inside its two containers.
GAME_RING = ["dot:mechanics", "dot:models", "dot:controls"]
MECHANICS = ["dot:camera", "dot:scan", "dot:camp", "dot:narrator", "dot:music"]
MODELS = ["dot:live", "dot:text", "dot:image", "dot:voice"]


def _tiny_wav() -> bytes:
    """Two seconds of a sine, as a real WAV — the upload path checks the type."""
    import math
    import struct
    import wave
    from io import BytesIO
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(
            struct.pack("<h", int(3000 * math.sin(i / 18))) for i in range(16000)))
    return buf.getvalue()


def _find_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TheNucleusIsAMarkerNotAStackOfRings(unittest.TestCase):
    """The origin shrinks; its siblings (pick, HERE, the render spinner)
    used to stay at full coin size and draw a nest of circles around it."""

    def test_core_chrome_is_stripped(self):
        from pathlib import Path
        css = (Path(__file__).resolve().parent / "static" / "css" / "standalone.css").read_text(
            encoding="utf-8", errors="replace")
        self.assertIn("#world-editor .eg-node.is-core .eg-here", css)
        self.assertIn("#world-editor .eg-node.is-core .eg-pick", css)
        self.assertIn("#world-editor .eg-node.is-core .eg-render-mark", css)

    def test_diving_clears_the_pick(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parent / "static" / "js" / "editor_graph.js").read_text(
            encoding="utf-8", errors="replace")
        dive = js.split("function diveWorld(n)", 1)[1][:400]
        self.assertIn("selectNode(null)", dive)


@unittest.skipUnless(HAVE_PW, "playwright not installed")
class EditorHarness(unittest.TestCase):
    """A mock server, a phone-sized browser, and the editor already open.

    Split out from the tests below so a second suite can drive the same surface
    without a second copy of the boot sequence — see test_editor_lands_e2e.py,
    which asks the other half of the question: not "is the control there" but
    "did the server take what it wrote".
    """

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        exp_path = Path("experiences") / "default.json"
        cls._exp_path = exp_path
        cls._exp_backup = exp_path.read_text(encoding="utf-8") if exp_path.exists() else None
        cls.port = _find_free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        env = dict(os.environ)
        env["MOCK_MODE"] = "1"
        env["ELEVENLABS_API_KEY"] = ""
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
            try:
                if cls._exp_backup is not None:
                    cls._exp_path.parent.mkdir(parents=True, exist_ok=True)
                    cls._exp_path.write_text(cls._exp_backup, encoding="utf-8")
            except Exception:
                pass

    # ---- server helpers --------------------------------------------------
    def _studio_content(self) -> dict:
        with urllib.request.urlopen(self.base_url + "/api/admin/studio/content") as r:
            body = json.loads(r.read().decode())
        return body.get("data", body)

    def _identity(self, block: str) -> dict:
        return (self._studio_content().get("identity") or {}).get(block) or {}

    def _scene_audio(self, prompt: str) -> dict:
        req = urllib.request.Request(
            self.base_url + "/api/scene_audio",
            data=json.dumps({"prompt": prompt, "session": "default"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read().decode())

    def _prompt(self, key: str) -> str:
        return (self._studio_content().get("prompts") or {}).get(key) or ""

    def _put_prompt(self, key: str, value: str) -> None:
        # The endpoint takes {"key","value"} or {"data": {...}} — not a bare map.
        req = urllib.request.Request(
            self.base_url + "/api/admin/studio/prompts",
            data=json.dumps({"key": key, "value": value}).encode(),
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        urllib.request.urlopen(req).read()

    def _tunables(self) -> dict:
        with urllib.request.urlopen(
                self.base_url + "/api/admin/studio/tunables") as r:
            body = json.loads(r.read().decode())
        return (body.get("data") or body).get("values") or {}

    def _put_tunables(self, patch: dict) -> None:
        req = urllib.request.Request(
            self.base_url + "/api/admin/studio/tunables",
            data=json.dumps(patch).encode(),
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        urllib.request.urlopen(req).read()

    def _put_identity(self, block: str, patch: dict) -> None:
        req = urllib.request.Request(
            self.base_url + "/api/admin/studio/identity",
            data=json.dumps({block: patch}).encode(),
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        urllib.request.urlopen(req).read()

    def _seed_experience(self) -> None:
        """One World, no links — so the graph tests don't inherit live authoring."""
        import experience_store
        req = urllib.request.Request(
            self.base_url + "/api/admin/studio/experience",
            data=json.dumps(experience_store.default_experience()).encode(),
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        urllib.request.urlopen(req).read()

    # ---- browser helpers -------------------------------------------------
    def setUp(self):
        self.errors = []
        self._seed_experience()
        self.page = self.browser.new_page(
            viewport=PHONE, is_mobile=True, has_touch=True)
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.goto(f"{self.base_url}/standalone?mode=play")
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
            noise = [e for e in self.errors
                     if "Identifier 'v' has already been declared" not in e]
            self.assertEqual(noise, [], f"page errors: {self.errors}")
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
        """Dive into the start World so Level / Character / World are on stage."""
        self._open_experience()
        wid = self._world_dot_id()
        self.assertIsNotNone(wid, "Experience should hold at least one World")
        self.page.evaluate("(id) => window.EditorGraph.enter(id)", wid)
        self._settle()

    def _open_experience(self):
        if self.page.evaluate("() => window.EditorGraph.openId()") is not None:
            return
        pt = self._dot("experience")
        self.page.mouse.click(pt["x"], pt["y"])
        self._settle()

    def _world_dot_id(self):
        return self.page.evaluate(
            """() => {
              const g = document.querySelector('#eg-world .eg-kind-world-node');
              return g && g.getAttribute('data-id');
            }""")

    def _core_id(self):
        return self.page.evaluate(
            """() => {
              const g = document.querySelector('#eg-world .eg-node.is-core, #eg-world .eg-node.is-alone');
              return g && g.getAttribute('data-id');
            }""")

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

    def _dive(self, node_id: str):
        """Into a container. One click is enough; a double is the same gesture."""
        self._tap(node_id)
        self._settle()

    def _shown(self):
        """On stage — excluding anything mid-wilt on its way off."""
        return self.page.evaluate(
            """() => Array.from(document.querySelectorAll('#eg-world .eg-node'))
                          .filter(g => g.style.display !== 'none' &&
                                       !g.classList.contains('is-leaving'))
                          .map(g => g.getAttribute('data-id'))""")

    def _open_leaf(self, node_id: str):
        opened = self.page.evaluate("(id) => window.EditorGraph.activate(id)", node_id)
        self.assertTrue(opened, f"could not activate {node_id}")
        self.page.wait_for_selector("#eg-sheet.is-open", timeout=4000)
        self._settle(700)

    def _rows(self):
        return self.page.eval_on_selector_all(
            "#eg-sheet-body .eg-stat .eg-stat-k", "els => els.map(e => e.textContent)")

    def _labels(self):
        return self.page.evaluate(
            """() => Array.from(document.querySelectorAll('#eg-world .eg-node'))
                          .filter(g => g.style.display !== 'none' &&
                                       !g.classList.contains('is-core'))
                          .map(g => g.querySelector('.eg-name').textContent)""")


class TestEditorDots(EditorHarness):
    """Drive the dots in a real browser, on a phone-sized viewport."""

    # ---- tests -----------------------------------------------------------
    def test_the_editor_opens_as_one_red_dot(self):
        """One nucleus, labeled with the Experience's name. Worlds live inside it once opened."""
        import experience_store
        title = (experience_store.default_experience().get("name") or "Experience").strip()
        clipped = title if len(title) <= 9 else title[:9].strip()
        self.assertEqual(self._shown(), ["experience"])
        self.assertEqual(self.page.text_content("#eg-caption-name").strip(), title)
        self.assertEqual(
            self.page.evaluate(
                """() => document.querySelector('#eg-world .eg-node[data-id="experience"] .eg-name')
                                 .textContent"""), clipped)
        self.assertTrue(self.page.evaluate(
            """() => document.querySelector('#eg-world .eg-node[data-id="experience"]')
                             .classList.contains('is-alone')"""))
        # The flat column isn't rendered behind it.
        self.assertEqual(
            self.page.evaluate(
                "getComputedStyle(document.querySelector('.we-scroll')).display"), "none")
        self.assertTrue(self.page.is_visible("#eg-toolkit"),
                        "the Experience toolkit sits on the bubble canvas")
        self.assertTrue(self.page.is_visible("#we-tab-experience"))
        self.assertTrue(self.page.is_visible("#we-tab-harness"))
        self.assertTrue(self.page.is_visible("#we-tab-sound"))

    def test_experience_tab_opens_the_top_row(self):
        """EXPERIENCE is a picker: the tab surfaces a strip of Experiences
        on disk, and choosing one loads that graph. A second click still
        focuses the row. Harness hides it."""
        import experience_store as xs
        from pathlib import Path

        self.page.wait_for_selector("#we-xp-track .we-xp-cell", timeout=4000)
        self.assertTrue(self.page.is_visible("#we-xp-row"),
                        "Experience surface shows the top row of Experiences")
        self.assertTrue(self.page.is_visible("#we-xp-new"),
                        "+ NEW starts another Experience from the carousel")
        self.assertTrue(
            self.page.evaluate(
                """() => !!document.querySelector('#we-xp-track .we-xp-cell.is-on')"""),
            "the active Experience is marked in the row")
        self.assertTrue(
            self.page.evaluate(
                """() => {
                  const on = document.querySelector('#we-xp-track .we-xp-cell.is-on');
                  return !!(on && on.querySelector('input.we-xp-name'));
                }"""),
            "the selected tile is named in place")
        self.assertTrue(self.page.is_visible("#eg-toolkit"),
                        "SELECT / + WORLD stay on the worlds graph")

        self.page.click("#we-tab-harness")
        self._settle()
        self.assertEqual(
            self.page.evaluate(
                "getComputedStyle(document.getElementById('we-xp-row')).display"),
            "none",
            "Harness keeps the engine graph; the Experience row is for Experience")

        self.page.evaluate("() => document.getElementById('we-tab-sound').click()")
        self._settle()
        self.assertEqual(
            self.page.evaluate(
                "getComputedStyle(document.getElementById('we-xp-row')).display"),
            "none",
            "Sound is a surface of its own; the Experience row stays put")
        self.assertEqual(self._shown(), ["sound"])

        self.page.click("#we-tab-experience")
        self.page.wait_for_function(
            "() => document.getElementById('we-xp-row').classList.contains('is-lit')",
            timeout=2000)
        self.assertTrue(self.page.is_visible("#we-xp-row"))
        self.assertEqual(self._shown(), ["experience"])

        mesa = Path("experiences") / "night-mesa.json"
        try:
            xs.save_experience({
                "name": "Night Mesa",
                "worlds": [{"id": "w-yard", "name": "Yard", "slug": ""}],
                "start_world": "w-yard",
                "transitions": [],
            }, slug="night-mesa")
            self.page.click("#we-tab-experience")
            self.page.wait_for_selector('#we-xp-track [data-slug="night-mesa"]', timeout=4000)
            self.page.click('#we-xp-track [data-slug="night-mesa"]')
            self.page.wait_for_function(
                """() => {
                  const c = document.querySelector('#we-xp-track [data-slug="night-mesa"]');
                  return !!(c && c.classList.contains('is-on'));
                }""",
                timeout=8000)
            self._settle()
            self._open_experience()
            names = self.page.evaluate(
                """() => Array.from(document.querySelectorAll(
                      '#eg-world .eg-kind-world-node .eg-name'))
                      .map(e => e.textContent)""")
            self.assertIn("Yard", names,
                          "picking an Experience loads its worlds into the graph")
            self.assertTrue(self.page.is_visible("#eg-toolkit"))
        finally:
            try:
                xs.set_active("default")
            except Exception:
                pass
            req = urllib.request.Request(
                self.base_url + "/api/experiences/activate",
                data=json.dumps({"slug": "default"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(req).read()
            except Exception:
                pass
            try:
                mesa.unlink()
            except Exception:
                pass

    def test_experience_carousel_names_and_creates(self):
        """+ NEW writes a file, typing the selected name saves it, and
        clicking another tile loads that Experience."""
        import experience_store as xs
        from pathlib import Path

        self.page.wait_for_selector("#we-xp-new", timeout=4000)
        self.assertTrue(self.page.is_visible("#we-xp-new"))
        created = None
        try:
            self.page.click("#we-xp-new")
            self.page.wait_for_function(
                """() => {
                  const on = document.querySelector('#we-xp-track .we-xp-cell.is-on');
                  const slug = on && on.getAttribute('data-slug');
                  return !!(slug && slug !== 'default'
                            && on.querySelector('input.we-xp-name'));
                }""",
                timeout=8000)
            slug = self.page.evaluate(
                """() => document.querySelector('#we-xp-track .we-xp-cell.is-on')
                                 .getAttribute('data-slug')""")
            created = Path("experiences") / f"{slug}.json"
            name = "#we-xp-track .we-xp-cell.is-on input.we-xp-name"
            self.page.fill(name, "Carousel Mesa")
            self.page.locator(name).blur()
            self.page.wait_for_function(
                """() => {
                  const el = document.querySelector(
                    '#we-xp-track .we-xp-cell.is-on input.we-xp-name');
                  return el && el.value.trim() === 'Carousel Mesa';
                }""",
                timeout=4000)
            self.assertTrue(created.exists(), " + NEW writes an Experience file")
            saved = json.loads(created.read_text(encoding="utf-8"))
            self.assertEqual(saved["id"], slug)
            self.assertEqual(saved["name"], "Carousel Mesa")

            self.page.click('#we-xp-track [data-slug="default"]')
            self.page.wait_for_function(
                """() => {
                  const c = document.querySelector('#we-xp-track [data-slug="default"]');
                  return !!(c && c.classList.contains('is-on'));
                }""",
                timeout=8000)
            self.page.click(f'#we-xp-track [data-slug="{slug}"]')
            self.page.wait_for_function(
                f"""() => {{
                  const c = document.querySelector('#we-xp-track [data-slug="{slug}"]');
                  return !!(c && c.classList.contains('is-on'));
                }}""",
                timeout=8000)
            self.assertEqual(
                self.page.input_value("#we-xp-track .we-xp-cell.is-on input.we-xp-name"),
                "Carousel Mesa")
        finally:
            try:
                xs.set_active("default")
            except Exception:
                pass
            req = urllib.request.Request(
                self.base_url + "/api/experiences/activate",
                data=json.dumps({"slug": "default"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(req).read()
            except Exception:
                pass
            try:
                if created and created.exists():
                    created.unlink()
            except Exception:
                pass

    def test_harness_is_an_editable_dot_graph(self):
        """The Harness tab is the engine as dots, not a 'not in this tab' card."""
        self.page.click("#we-tab-harness")
        self._settle()
        self.assertEqual(self._shown(), ["harness"])
        self.assertEqual(self.page.text_content("#eg-caption-name").strip(), "Harness")
        self.assertEqual(
            self.page.evaluate(
                "getComputedStyle(document.getElementById('eg-toolkit')).display"),
            "none",
            "Add World / Link belong to Experience, not the machine room")
        pt = self._dot("harness")
        self.page.mouse.dblclick(pt["x"], pt["y"])
        self._settle()
        self.assertEqual(
            sorted(self._shown()),
            sorted(HARNESS_LOOP),
            "the overview is the turn loop — the Harness nucleus stays off stage")
        self.assertEqual(self._labels(), ["Choices", "Actions", "Picture", "State"])
        self.assertEqual(
            self.page.text_content("#eg-caption-name").strip(),
            "1 Choices → 2 Actions → 3 Picture → 4 State")
        self.assertEqual(
            self.page.evaluate(
                """() => Array.from(document.querySelectorAll('#eg-world .eg-node:not(.is-core) .eg-beat'))
                              .map(t => t.textContent)"""),
            ["1", "2", "3", "4"],
            "beats are numbered so the loop has a start")
        self.assertEqual(
            self.page.evaluate(
                "() => document.querySelector('#eg-world .eg-node[data-id=\"h:dot:picture\"] .eg-wait').textContent"),
            "WAIT")
        self.assertFalse(
            any(i in self._shown() for i in HARNESS_PICTURE + HARNESS_STATE),
            "Picture / State knobs stay inside those nodes")
        self.assertEqual(
            self.page.evaluate("() => document.querySelectorAll('#eg-edges .eg-flow').length"),
            4,
            "the harness draws the turn as one four-beat loop")
        vb0 = self.page.evaluate(
            """() => { const v = document.getElementById('eg-canvas').viewBox.baseVal;
                       return {w: v.width, h: v.height}; }""")
        pt = self._dot("h:dot:actions")
        self.page.mouse.move(pt["x"], pt["y"])
        self.page.mouse.wheel(0, -240)
        self._settle(200)
        vb1 = self.page.evaluate(
            """() => { const v = document.getElementById('eg-canvas').viewBox.baseVal;
                       return {w: v.width, h: v.height}; }""")
        self.assertLess(vb1["w"], vb0["w"], "scroll zooms the harness in")
        self.page.mouse.wheel(0, 240)
        self._settle(200)
        actions_r, pic_r = self.page.evaluate(
            """() => {
              const a = document.querySelector('#eg-world .eg-node[data-id="h:dot:actions"] .eg-cell');
              const p = document.querySelector('#eg-world .eg-node[data-id="h:dot:picture"] .eg-cell');
              return [+a.getAttribute('r'), +p.getAttribute('r')];
            }""")
        self.assertGreater(pic_r, actions_r, "Picture is the wait, so it is the biggest bubble")
        self._dive("h:dot:picture")
        self.assertEqual(self._labels(), ["System", "Framing", "Still", "Edit", "Film"])
        core_x, mid = self.page.evaluate(
            """() => {
              const c = document.getElementById('eg-canvas').getBoundingClientRect();
              const id = document.querySelector('#eg-world .eg-node.is-core')
                .getAttribute('data-id');
              const d = window.EditorGraph.dotAt(id);
              return [d.x, c.x + c.width * 0.42];
            }""")
        self.assertLess(
            core_x, mid,
            "Picture knobs start on the left wash, not over the still")
        self.assertEqual(
            self.page.evaluate(
                """() => [...document.querySelectorAll('#eg-edges .eg-edge-g')]
                     .map(g => g.getAttribute('data-id'))"""),
            ["ring:start", "ring:0", "ring:1", "ring:2", "ring:3", "ring:back"],
            "Picture starts at the hub, runs the knobs in order, then returns")
        self._open_leaf("h:dot:system")
        self.assertTrue(self.page.is_visible("#eg-sheet.is-open"))
        body = self.page.text_content("#eg-sheet-body") or ""
        self.assertIn("How this session draws", body)
        self.page.wait_for_selector("#eg-sheet-body .we-mode-name", timeout=4000)
        names = self.page.eval_on_selector_all(
            "#eg-sheet-body .we-mode-name", "els => els.map(e => e.textContent)")
        self.assertIn("Live video", names,
                      "System offers live video; stills are the floor, not a mode")
        self.assertNotIn("Still images", names)
        self.assertIsNone(
            self.page.query_selector("#eg-sheet-body #btn-renderer"),
            "play-rail buttons do not belong in the Harness")
        for toy in ("Auto-play", "Tape", "Debug log", "VHS overlay"):
            self.assertNotIn(toy, body)
        self.page.keyboard.press("Escape")
        self._settle()
        self.page.keyboard.press("Escape")
        self._settle()

        self._open_leaf("h:dot:actions")
        self.assertTrue(self.page.is_visible("#eg-sheet.is-open"))
        self.assertTrue(
            self.page.query_selector("#eg-sheet-body textarea.eg-prompt"),
            "Actions opens the contract prompt for editing")
        self.page.keyboard.press("Escape")
        self._settle()

        self._dive("h:dot:state")
        self.assertEqual(self._labels(), ["Evolve", "Scene"])
        self.page.keyboard.press("Escape")
        self._settle()

        self.page.click("#we-tab-experience")
        self._settle()
        self.assertEqual(self._shown(), ["experience"])

    def test_sound_is_a_surface_of_families(self):
        """Sound sits next to Experience and Harness: palette plus muteable families."""
        self.page.evaluate("() => document.getElementById('we-tab-sound').click()")
        self._settle()
        self.assertEqual(self._shown(), ["sound"])
        self.assertEqual(self.page.text_content("#eg-caption-name").strip(), "Sound")
        self.assertEqual(
            self.page.evaluate(
                "getComputedStyle(document.getElementById('eg-toolkit')).display"),
            "none",
            "Add World belongs to Experience, not the synth")
        pt = self._dot("sound")
        self.page.mouse.dblclick(pt["x"], pt["y"])
        self._settle()
        self.assertEqual(
            sorted(i for i in self._shown() if i.startswith("s:dot:")),
            sorted(SOUND_DOTS))
        self.assertEqual(
            self._labels(),
            ["Palette", "Clicks", "Chrome", "Turn", "Lens", "Body", "Voice"])
        self._open_leaf("s:dot:palette")
        self.assertTrue(self.page.is_visible("#eg-sheet.is-open"))
        body = self.page.text_content("#eg-sheet-body") or ""
        self.assertIn("Tape", body)
        self.assertIn("Quiet", body)
        self.assertIn("Silent", body)
        self.page.keyboard.press("Escape")
        self._settle()
        self.page.click("#we-tab-experience")
        self._settle()
        self.assertEqual(self._shown(), ["experience"])

    def test_experience_holds_worlds_you_can_spawn_and_link(self):
        """The Experience is a state machine: every World is a cell, Add World
        drops another, and Link draws a directed transition between them."""
        self._open_experience()
        before = [i for i in self._shown() if i.startswith("world:")]
        self.assertEqual(len(before), 1, "a new Experience starts with one World")
        self.page.click('#eg-toolkit [data-tool="add-world"]')
        self.page.wait_for_timeout(1200)
        worlds = [i for i in self._shown() if i.startswith("world:")]
        self.assertEqual(len(worlds), 2, "Add World should drop a second cell on the Experience")
        self.assertIn("experience", self._shown())
        self.assertTrue(
            self.page.evaluate(
                """() => !!document.querySelector('#eg-world .eg-kind-world-node.is-selected')"""),
            "the new World should be selected")
        self.assertTrue(
            self.page.evaluate(
                """() => {
                  const g = document.querySelector('#eg-world .eg-kind-world-node.is-selected');
                  const p = g && g.querySelector('.eg-pick');
                  return !!(p && getComputedStyle(p).strokeOpacity !== '0');
                }"""),
            "the selected World wears a pick ring outside the coin")
        a, b = worlds[0], worlds[1]
        pa = self._dot(a)
        pb = self._dot(b)
        self.page.mouse.move(pa["x"], pa["y"])
        self.page.wait_for_timeout(250)
        port = self.page.evaluate(
            """() => {
              const d = document.querySelector('#eg-world .eg-port-dot');
              if (!d) return null;
              const r = d.getBoundingClientRect();
              return {x: r.x + r.width / 2, y: r.y + r.height / 2};
            }""")
        self.assertIsNotNone(port, "hovering a World should show its satellite")
        self.page.mouse.move(port["x"], port["y"])
        self.page.mouse.down()
        self.page.mouse.move(pb["x"], pb["y"], steps=12)
        self.page.mouse.up()
        self.page.wait_for_timeout(900)
        self.assertTrue(
            self.page.evaluate("() => document.querySelectorAll('#eg-edges .eg-edge').length > 0"),
            "dragging a satellite onto another World should draw a transition")
        self.assertTrue(
            self.page.evaluate(
                """() => document.querySelectorAll('#eg-edges .eg-bead').length > 0
                    && document.getElementById('eg-inspector').classList.contains('is-open')"""),
            "a new link should show a bead and a small inspector for when it fires")
        type_ids = self.page.evaluate(
            """() => {
              const sel = document.querySelector('#eg-inspector select.eg-select');
              return sel ? Array.from(sel.options).map(o => o.value) : [];
            }""")
        self.assertIn("turn_count", type_ids)
        self.assertIn("game_over", type_ids)
        self.assertFalse(
            self.page.evaluate("() => !!document.querySelector('#eg-inspector .eg-insp-mode')"),
            "Type must be a dropdown, not a pair of mode buttons")

    def test_a_world_cycle_rides_the_ring(self):
        """Right-out / left-in noodles U-turned around a cycle and crossed.
        Three Worlds in a loop have to be three outward arcs."""
        req = urllib.request.Request(
            self.base_url + "/api/admin/studio/experience",
            data=json.dumps({
                "id": "default",
                "name": "Cycle",
                "start_world": "wa",
                "worlds": [
                    {"id": "wa", "name": "A", "x": -400, "y": 40},
                    {"id": "wb", "name": "B", "x": 0, "y": -520},
                    {"id": "wc", "name": "C", "x": 440, "y": 40},
                ],
                "transitions": [
                    {"id": "t1", "from": "wa", "to": "wb",
                     "condition": {"type": "turn_count", "turns": 8}},
                    {"id": "t2", "from": "wb", "to": "wc",
                     "condition": {"type": "turn_count", "turns": 8}},
                    {"id": "t3", "from": "wc", "to": "wa",
                     "condition": {"type": "turn_count", "turns": 8}},
                ],
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        urllib.request.urlopen(req).read()
        self.page.reload()
        self.page.keyboard.press("r")
        self.page.wait_for_selector(".choice-btn", state="attached", timeout=20000)
        self.page.keyboard.press("`")
        self.page.wait_for_selector("#we-graph", state="visible", timeout=10000)
        self.page.wait_for_function(
            "() => document.querySelectorAll('#eg-world .eg-node').length > 0",
            timeout=10000)
        self._settle()
        self._open_experience()
        self._settle(900)
        paths = self.page.evaluate(
            """() => Array.from(document.querySelectorAll('#eg-edges .eg-edge'))
              .map(p => p.getAttribute('d'))
              .map(d => {
                const n = d.match(/-?\\d+(?:\\.\\d+)?/g).map(Number);
                return {x1:n[0], y1:n[1], c1x:n[2], c1y:n[3], c2x:n[4], c2y:n[5], x2:n[6], y2:n[7]};
              })""")
        self.assertEqual(len(paths), 3, "a 3-world cycle draws three links")
        for i, p in enumerate(paths):
            self.assertGreater(
                abs(p["c1y"] - p["y1"]) + abs(p["c2y"] - p["y2"]),
                20,
                "link %s still uses a flat horizontal noodle" % i)

    def test_delete_key_removes_a_world(self):
        """Delete / Backspace destroy the selected World, not just the toolkit."""
        self._open_experience()
        self.page.click('#eg-toolkit [data-tool="add-world"]')
        self.page.wait_for_timeout(1200)
        worlds = [i for i in self._shown() if i.startswith("world:")]
        self.assertEqual(len(worlds), 2)
        extra = next(i for i in worlds if i != "world:w-live")
        self._tap(extra)
        self._settle(200)
        self.page.evaluate("() => { if (document.activeElement) document.activeElement.blur(); }")
        self.page.keyboard.press("Delete")
        self.page.wait_for_timeout(900)
        left = [i for i in self._shown() if i.startswith("world:")]
        self.assertEqual(len(left), 1, "Delete should remove the selected World")
        self.assertNotIn(extra, left)

    def test_spawned_world_asks_to_be_named(self):
        """A new World is a level-in-waiting: the card to name it opens immediately."""
        self._open_experience()
        self.page.click('#eg-toolkit [data-tool="add-world"]')
        self.page.wait_for_timeout(1200)
        self.assertTrue(
            self.page.evaluate(
                """() => {
                  const d = document.getElementById('eg-inspector');
                  return !!(d && d.classList.contains('is-open')
                    && d.querySelector('.eg-insp-name')
                    && d.querySelector('.eg-insp-design'));
                }"""),
            "adding a World should open a naming card with Edit")
        name = self.page.query_selector("#eg-inspector .eg-insp-name")
        self.assertIsNotNone(name)
        name.fill("The Four Corners")
        self.page.evaluate("() => { if (document.activeElement) document.activeElement.blur(); }")
        self.page.wait_for_timeout(900)
        self.assertTrue(
            self.page.evaluate(
                """() => Array.from(document.querySelectorAll('#eg-world .eg-world-cap'))
                      .some(t => t.textContent === 'The Four Corners')"""),
            "the cell should wear the name you gave it")
        extra = next(i for i in self._shown() if i.startswith("world:") and i != "world:w-live")
        self.page.click("#eg-inspector .eg-insp-design")
        self.page.wait_for_function(
            """() => {
              const g = document.querySelector('#eg-world .eg-node[data-id="dot:level"]');
              return !!(g && g.style.display !== 'none' && !g.classList.contains('is-leaving'));
            }""",
            timeout=8000)
        shown = self._shown()
        self.assertIn("dot:level", shown, "Edit should dive into the World's interior")
        self.assertIn(extra, shown)

    def test_world_circle_shows_a_take_in_progress(self):
        """Changing the level plate should feel like a take: the cell says
        RENDERING, then the still swaps. markRendering is the optimistic
        state the editor paints the moment you change the art."""
        self._open_experience()
        self.assertTrue(
            self.page.evaluate(
                """() => {
                  const id = document.querySelector('#eg-world .eg-kind-world-node')
                    && document.querySelector('#eg-world .eg-kind-world-node').getAttribute('data-id');
                  const worldId = id && id.indexOf('world:') === 0 ? id.slice(6) : '';
                  window.EditorGraph.markRendering(worldId);
                  const g = document.querySelector('#eg-world .eg-kind-world-node.is-rendering');
                  const label = g && g.querySelector('.eg-render-label');
                  return !!(g && label && label.textContent === 'RENDERING');
                }"""),
            "a World take should spin RENDERING on the cell")

    def test_world_x_removes_it(self):
        """Each World carries its own × — no Delete tool on the sidebar."""
        self._open_experience()
        self.page.click('#eg-toolkit [data-tool="add-world"]')
        self.page.wait_for_timeout(1200)
        self.assertFalse(self.page.query_selector('#eg-toolkit [data-tool="connect"]'))
        self.assertFalse(self.page.query_selector('#eg-toolkit [data-tool="delete"]'))
        worlds = [i for i in self._shown() if i.startswith("world:")]
        self.assertEqual(len(worlds), 2)
        extra = next(i for i in worlds if i != "world:w-live")
        pt = self.page.evaluate(
            """(id) => {
              const g = document.querySelector('#eg-world .eg-node[data-id="' + id + '"] .eg-kill-disk');
              if (!g) return null;
              const r = g.getBoundingClientRect();
              return {x: r.x + r.width / 2, y: r.y + r.height / 2};
            }""", extra)
        self.assertIsNotNone(pt, "a World should show an × once there is more than one")
        self.page.mouse.click(pt["x"], pt["y"])
        self.page.wait_for_timeout(900)
        left = [i for i in self._shown() if i.startswith("world:")]
        self.assertEqual(len(left), 1)
        self.assertNotIn(extra, left)

    def test_the_dot_opens_into_the_three_things_you_author(self):
        """A place, a person, and the gameplay — inside a World cell."""
        self._open_ring()
        wid = self._world_dot_id()
        self.assertEqual(sorted(self._shown()), sorted([wid] + DOTS))
        self.assertEqual(self._labels(), ["Level", "Character", "Gameplay"])
        # The nucleus drops its name once the ring is out, so "Gameplay" appears once.
        self.assertEqual(
            self.page.evaluate(
                """() => getComputedStyle(document.querySelector(
                     '#eg-world .eg-node.is-core .eg-name')).display"""), "none")

    def test_one_size_per_level_and_smaller_as_you_go_in(self):
        """Within a ring every circle matches — sizing each to its own word made a
        bag of different coins. Between rings they step down, which is the only
        thing on screen carrying depth."""
        self._open_ring()

        def radii():
            # is-orbit is exactly "a satellite of the ring that is open", which
            # display alone is not: a node that has never been painted has an
            # empty style.display, not "none".
            return self.page.evaluate(
                """() => Array.from(document.querySelectorAll('#eg-world .eg-node.is-orbit'))
                              .filter(g => !g.classList.contains('is-leaving'))
                              .map(g => Number(g.querySelector('circle').getAttribute('r')))""")

        top = radii()
        self.assertGreater(len(top), 2)
        self.assertEqual(len(set(top)), 1, f"circles in one ring differ: {set(top)}")
        self._dive("dot:world")
        mid = radii()
        self.assertEqual(len(set(mid)), 1, f"circles in one ring differ: {set(mid)}")
        self.assertLess(mid[0], top[0], "a level in should be a size smaller")
        self._dive("dot:mechanics")
        deep = radii()
        self.assertLess(deep[0], mid[0], "and smaller again")
        # And the wall of whatever you're inside is drawn.
        self.assertEqual(
            self.page.eval_on_selector_all("#eg-world .eg-shell", "els => els.length"), 1)
        # Type scales with the level, so one size per ring — not one size in the
        # whole tree, which is what it was before depth existed.
        sizes = self.page.evaluate(
            """() => Array.from(document.querySelectorAll('#eg-world .eg-node.is-orbit'))
                          .map(g => g.querySelector('.eg-name').getAttribute('font-size'))""")
        self.assertEqual(len(set(sizes)), 1, f"type differs inside one ring: {set(sizes)}")
        self.assertEqual(
            self.page.evaluate(
                """() => getComputedStyle(document.querySelector(
                     '#eg-world .eg-name')).textTransform"""), "uppercase")
        # Every label, at every depth, still sits inside its own circle.
        worst = self.page.evaluate(
            """() => Math.max(...Array.from(
                 document.querySelectorAll('#eg-world .eg-node')).map(g => {
                   const t = g.querySelector('.eg-name');
                   const r = Number(g.querySelector('circle').getAttribute('r'));
                   return t.getComputedTextLength() / (r * 2);
                 }))""")
        self.assertLess(worst, 0.86,
                        "a label should sit inside its circle with room to spare")

    def test_the_nucleus_frames_its_ring_instead_of_joining_it(self):
        """The centre dot is WHERE YOU ARE, not another thing to pick. Drawn at
        the same weight as its own children it read as a sixth option in a ring
        of five, and the one piece of hierarchy on the page went with it.

        Measured on screen, not in the SVG: the shrink is a transform, so the
        `r` attribute the test above reads is unchanged by design.
        """
        def widths():
            return self.page.evaluate(
                """() => { const out = {core: null, ring: []};
                     document.querySelectorAll('#eg-world .eg-node').forEach((g) => {
                       if (g.style.display === 'none' ||
                           g.classList.contains('is-leaving')) return;
                       const w = g.querySelector('.eg-cell').getBoundingClientRect().width;
                       if (g.classList.contains('is-core')) out.core = w;
                       else out.ring.push(w);
                     });
                     return out; }""")

        # Alone, the Experience is a real coin — not a marker the size of
        # a nucleus. The shrink is for an opened ring's origin.
        alone = widths()
        self.assertIsNone(alone["core"], "a lone dot is not a nucleus")
        self.assertGreater(alone["ring"][0], 70,
                           "the collapsed Experience should read as a node")

        self._open_ring()
        for step in ("dot:world", "dot:mechanics"):
            self._dive(step)
            w = widths()
            self.assertIsNotNone(w["core"], f"no nucleus inside {step}")
            ratio = w["core"] / max(w["ring"])
            self.assertLess(ratio, 0.7,
                            f"the nucleus inside {step} is competing with its "
                            f"ring ({ratio:.2f} of a satellite)")
            self.assertGreater(ratio, 0.35,
                               f"the nucleus inside {step} has vanished "
                               f"({ratio:.2f} of a satellite)")

    def test_the_experience_sits_beside_the_nucleus(self):
        """Opening the Experience keeps a small origin and a full-size
        Experience coin. Worlds are cells on that graph; later Experiences
        can sit there too."""
        self._open_experience()
        shown = self._shown()
        self.assertIn("experience", shown)
        self.assertIn("xp", shown)
        self.assertTrue(any(i.startswith("world:") for i in shown))
        sizes = self.page.evaluate(
            """() => {
              const out = {core: 0, xp: 0, world: 0};
              document.querySelectorAll('#eg-world .eg-node').forEach((g) => {
                if (g.style.display === 'none' ||
                    g.classList.contains('is-leaving')) return;
                const w = g.querySelector('.eg-cell').getBoundingClientRect().width;
                const id = g.getAttribute('data-id');
                if (g.classList.contains('is-core')) out.core = w;
                else if (id === 'xp') out.xp = w;
                else if (g.classList.contains('eg-kind-world-node')) out.world = w;
              });
              return out;
            }""")
        self.assertGreater(sizes["xp"], 90, "the Experience coin should match a World")
        self.assertGreater(sizes["world"], 80)
        self.assertLess(sizes["core"] / sizes["xp"], 0.7,
                        "the origin should stay a nucleus, not a second Experience")
        self.assertGreater(sizes["xp"] / sizes["world"], 0.85)
        self.assertLess(sizes["xp"] / sizes["world"], 1.2)

    def test_no_spokes_to_the_middle(self):
        """Lines from the nucleus to every satellite drew the one relationship
        you can already see, and turned a constellation into a wheel."""
        self._open_ring()
        self.assertEqual(
            self.page.eval_on_selector_all("#eg-world line", "els => els.length"), 0)

    def test_game_holds_mechanics_models_and_controls(self):
        """The depth: inside GAME are the mechanics, the models that
        generate it, and how you drive."""
        self._open_ring()
        self._dive("dot:world")
        self.assertEqual(sorted(self._shown()), sorted(["dot:world"] + GAME_RING))
        self.assertEqual(self._labels(), ["Mechanics", "Models", "Controls"])

        self._dive("dot:mechanics")
        self.assertEqual(sorted(self._shown()), sorted(["dot:mechanics"] + MECHANICS))
        self.assertEqual(self._labels(),
                         ["Camera", "Scan", "Camp", "Narrator", "Music"])

        # Back up one level at a time, not straight to the top.
        self.page.keyboard.press("Escape")
        self._settle()
        self.assertEqual(sorted(self._shown()), sorted(["dot:world"] + GAME_RING))

        self._dive("dot:models")
        self.assertEqual(sorted(self._shown()), sorted(["dot:models"] + MODELS))
        self.assertEqual(self._labels(), ["World", "Image", "Voice"])

    def test_the_engine_is_not_in_here_any_more(self):
        """The contract prompts, the runtime knob grid and the galleries are gone
        from the dots. They are still in the flat list; just not what you meet."""
        self._open_ring()
        ids = self.page.evaluate(
            """() => Array.from(document.querySelectorAll('#eg-world .eg-node'))
                          .map(g => g.getAttribute('data-id'))""")
        for gone in ("prompt:", "control:", "build:", "level:", "group:", "layer:", "new:"):
            self.assertFalse([i for i in ids if i.startswith(gone)],
                             f"{gone}* should not be in the dots any more")

    def test_empty_paper_closes_the_ring(self):
        """Inside a World, empty paper surfaces. Outside the enclosure on
        the Experience canvas closes the Worlds back into the Experience."""
        self._open_ring()
        self.assertEqual(len(self._shown()), 4)
        self._tap_paper()
        self.assertIn("experience", self._shown())
        self.assertTrue(any(i.startswith("world:") for i in self._shown()))
        self._tap_paper()
        self.assertEqual(self._shown(), ["experience"],
                         "outside the enclosure should close the Worlds")

    def test_escape_closes_the_ring_then_the_editor(self):
        self._open_ring()
        self.page.keyboard.press("Escape")
        self._settle()
        self.assertIn("experience", self._shown())
        self.page.keyboard.press("Escape")
        self._settle()
        self.assertEqual(self._shown(), ["experience"])
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(500)
        self.assertFalse(self.page.evaluate(
            "document.body.classList.contains('world-editor-on')"),
            "Escape at the top should close the editor")

    def test_back_arrow_closes_a_sheet_then_the_editor(self):
        """Top-left arrow: a nested sheet returns to the graph; at the root
        it closes the editor to Watch/Play."""
        self.assertTrue(self.page.is_visible("#we-back"))
        self._open_ring()
        self._tap("dot:level")
        self.page.wait_for_selector("#eg-sheet.is-open", timeout=4000)
        self.page.click("#we-back")
        self._settle()
        self.assertFalse(self.page.evaluate(
            "document.getElementById('eg-sheet').classList.contains('is-open')"),
            "back from a sheet should return to the graph")
        self.assertEqual(len(self._shown()), 4)
        self.page.click("#we-back")
        self._settle()
        self.assertIn("experience", self._shown())
        self.page.click("#we-back")
        self._settle()
        self.assertEqual(self._shown(), ["experience"])
        self.page.click("#we-back")
        self.page.wait_for_timeout(500)
        self.assertFalse(self.page.evaluate(
            "document.body.classList.contains('world-editor-on')"),
            "back at the root should close the editor")

    def test_a_window_carries_the_essentials_and_nothing_to_read(self):
        """Four fields for a place. No switch, no ⓘ, no advanced disclosure.
        The compiled prompt stays visible — hiding it is how a save looked dead."""
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
        self.assertGreater(
            self.page.eval_on_selector_all(body + ".we-compiled", "e => e.length"), 0,
            "the compiled prompt has to stay on the sheet")
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

    def test_character_look_compiles_on_the_open_sheet(self):
        """Typing Look used to save and remount. The compiled prompt now
        updates in place, and the cursor stays in the field."""
        before = self._identity("player_character")
        try:
            self._open_ring()
            self._tap("dot:character")
            self.page.wait_for_selector("#eg-sheet.is-open", timeout=4000)
            self._settle(400)
            look = self.page.query_selector(
                '#eg-sheet-body [data-identity-field="appearance"]')
            self.assertIsNotNone(look, "Character must show a Look field")
            look.click()
            look.fill("neon pink mohawk proof")
            self.page.wait_for_timeout(900)
            compiled = self.page.text_content("#eg-sheet-body .we-compiled") or ""
            self.assertIn("neon pink mohawk proof", compiled)
            self.assertEqual(
                self.page.evaluate(
                    "document.activeElement.getAttribute('data-identity-field')"),
                "appearance",
                "a save must not remount the sheet and steal the cursor")
            self.assertIn(
                "neon pink mohawk",
                (self._identity("player_character").get("appearance") or ""))
        finally:
            self._put_identity("player_character", {
                "appearance": before.get("appearance", ""),
            })

    def test_spec_windows_have_a_clear_button(self):
        """Character and Level used to ship with no way to empty the sheet —
        the overlay could warn that a blank character was switched on, and
        the only reset lived on a different window."""
        self._open_ring()
        for node_id, title in (("dot:level", "Level"), ("dot:character", "Character")):
            self._tap(node_id)
            self.page.wait_for_selector("#eg-sheet.is-open", timeout=4000)
            self._settle(400)
            self.assertEqual(self.page.text_content("#eg-sheet-title").strip(), title)
            btn = self.page.query_selector("#eg-sheet-body [data-action='clear-block']")
            self.assertIsNotNone(btn, f"{title} should have a Clear button")
            self.assertEqual(btn.text_content().strip(), "Clear")
            self.page.keyboard.press("Escape")
            self._settle(400)
        self._dive("dot:world")
        self._dive("dot:mechanics")
        self._open_leaf("dot:camera")
        cam = self.page.query_selector("#eg-sheet-body [data-action='clear-block']")
        self.assertIsNotNone(cam, "Camera should have a Clear button")
        self.page.keyboard.press("Escape")
        self._settle(400)
        self._open_leaf("dot:camp")
        self.assertIsNotNone(
            self.page.query_selector("#eg-sheet-body [data-action='clear-prompt']"),
            "Camp's shot prompt should have a Clear button")
        self.page.keyboard.press("Escape")
        self._settle(400)
        self._open_leaf("dot:narrator")
        self.assertIsNotNone(
            self.page.query_selector("#eg-sheet-body [data-action='clear-prompt']"),
            "Narrator should have a Clear button")

    def test_clearing_the_level_sheet_persists(self):
        """Clear empties the fields, switches the block off, and that is what
        the game reads on the next turn — not a client-only wipe."""
        before = self._identity("setting_reference")
        try:
            self._open_ring()
            self._tap("dot:level")
            self.page.wait_for_selector("#eg-sheet.is-open", timeout=4000)
            self._settle(500)

            box = self.page.query_selector("#eg-sheet-body input[type='text']")
            box.click()
            box.fill("The Kettle Yard")
            self.page.keyboard.press("Enter")
            self.page.wait_for_timeout(1200)
            self.assertTrue(self._identity("setting_reference").get("enabled"),
                            "setup: typing should have switched the level on")

            btn = self.page.query_selector("#eg-sheet-body [data-action='clear-block']")
            self.assertIsNotNone(btn)
            btn.click()
            self.page.wait_for_timeout(1200)

            saved = self._identity("setting_reference")
            self.assertFalse(saved.get("enabled"),
                             "Clear should switch the level off")
            self.assertEqual(saved.get("name") or "", "")
            self.assertEqual(saved.get("summary") or "", "")
            self.assertEqual(saved.get("landmarks") or "", "")
            self.assertEqual(saved.get("opening_shot") or "", "")
        finally:
            self._put_identity("setting_reference", {
                "enabled": bool(before.get("enabled")),
                "name": before.get("name", ""),
                "summary": before.get("summary", ""),
                "landmarks": before.get("landmarks", ""),
                "opening_shot": before.get("opening_shot", ""),
            })

    def test_the_glow_means_you_changed_it(self):
        """Not "has content" — the shipped character sheet HAS content, so that
        rule lit Character up on a game nobody had touched. The glow is yours."""
        def glows(node_id):
            return self.page.evaluate(
                """(id) => document.querySelector(
                     '#eg-world .eg-node[data-id="' + id + '"]')
                       .classList.contains('is-changed')""", node_id)

        before = self._identity("camera_perspective")
        try:
            # Untouched: nothing on the top ring is claiming to be yours.
            self._open_ring()
            for node_id in DOTS:
                self.assertFalse(glows(node_id),
                                 f"{node_id} glows on an untouched game")

            # Change one field, the way a person would, and the mark appears off
            # the back of the save with no reload.
            self._dive("dot:world")
            self._dive("dot:mechanics")
            self._open_leaf("dot:camera")
            modes = self.page.query_selector_all("#eg-sheet-body .we-mode")
            other = next(m for m in modes if "active" not in (m.get_attribute("class") or ""))
            other.click()
            self.page.wait_for_timeout(1200)
            self.page.keyboard.press("Escape")
            self._settle(600)
            self.assertTrue(glows("dot:camera"), "an edited sheet should glow")
            # And a container wears what is inside it, so you can see from the
            # top that something in there is yours — two levels up, through
            # Mechanics into Game.
            self.page.keyboard.press("Escape")
            self._settle()
            self.assertTrue(glows("dot:mechanics"),
                            "a container should inherit the glow from its children")
            self.page.keyboard.press("Escape")
            self._settle()
            self.assertTrue(glows("dot:world"),
                            "a container should inherit the glow from its grandchildren")
        finally:
            self._put_identity("camera_perspective", {
                "mode": before.get("mode", "first_person"),
            })

    def test_camera_is_a_mechanic_with_four_perspectives(self):
        """Where the camera stands is a mechanic, not a preference."""
        self._open_ring()
        self._dive("dot:world")
        self._dive("dot:mechanics")
        self._open_leaf("dot:camera")
        # By name only — the taglines are teaching copy.
        self.assertEqual(
            self.page.eval_on_selector_all(
                "#eg-sheet-body .we-mode-name", "els => els.map(e => e.textContent)"),
            ["First person", "Over the shoulder", "Third person", "Fixed cinematic"])
        self.assertEqual(
            self.page.eval_on_selector_all("#eg-sheet-body .we-mode-tag", "e => e.length"), 0)

    def test_scan_and_npc_report_what_is_actually_wrong(self):
        """Neither has a knob to turn: SCAN's backend is set on the server and
        conversation needs an agent id in the environment. What they DO have is
        the answer to "why isn't this working", which until now lived in a boot
        log nobody reads."""
        self._open_ring()
        self._dive("dot:world")
        self._dive("dot:mechanics")

        self._open_leaf("dot:scan")
        rows = self._rows()
        self.assertIn("Answering", rows)
        self.assertIn("On device", rows)
        self.page.keyboard.press("Escape")
        self._settle(600)

        self._open_leaf("dot:narrator")
        self.assertIn("Out loud", self._rows())
        # A machine token is the server talking to itself, never the UI's words.
        values = self.page.eval_on_selector_all(
            "#eg-sheet-body .eg-stat-v", "els => els.map(e => e.textContent)")
        self.assertFalse([v for v in values if "_" in v and v.islower()],
                         f"raw reason codes leaked into the panel: {values}")

    def test_the_narrator_is_writable_and_castable(self):
        """The voice that talks straight to the player had neither: its wording
        was three f-strings in engine.py and its voice was an environment
        variable, so the panel was facts and a button you couldn't influence."""
        before = self._prompt("narrator_direction")
        self.assertTrue(before, "narrator_direction should ship with a default")
        try:
            self._open_ring()
            self._dive("dot:world")
            self._dive("dot:mechanics")
            self._open_leaf("dot:narrator")

            # Who reads it: a real menu over the voice library.
            self.assertEqual(
                self.page.eval_on_selector_all(
                    "#eg-sheet-body .eg-field-k", "els => els.map(e => e.textContent)"),
                ["Reads the story"])
            self.assertGreater(
                self.page.eval_on_selector("#eg-sheet-body select",
                                           "s => s.options.length"), 1)

            # What it says: the template, with its placeholders intact.
            box = self.page.query_selector("#eg-sheet-body .eg-prompt")
            self.assertIsNotNone(box, "the narrator should have a prompt to edit")
            for var in ("{world}", "{premise}", "{focus}"):
                self.assertIn(var, box.input_value())
            box.click()
            self.page.keyboard.press("End")
            self.page.keyboard.type(" Never mention the weather.")
            self.page.click("#eg-sheet-body .eg-acts .we-btn-primary")
            self.page.wait_for_timeout(1500)
            self.assertIn("Never mention the weather.",
                          self._prompt("narrator_direction"))
        finally:
            self._put_prompt("narrator_direction", before)
            self.assertEqual(self._prompt("narrator_direction"), before)

    def test_models_pick_from_what_the_server_advertises(self):
        """The world and image pickers are the real lists, and the world panel
        says whether realtime can actually connect right now."""
        self._open_ring()
        self._dive("dot:world")
        self._dive("dot:models")

        self._open_leaf("dot:world")
        picks = self.page.eval_on_selector_all(
            "#eg-sheet-body .we-mode-name", "els => els.map(e => e.textContent)")
        self.assertGreater(len(picks), 1, "the world models should be listed")
        self.assertIn("Realtime", self._rows())
        self.page.keyboard.press("Escape")
        self._settle(600)

        self._open_leaf("dot:text")
        picks = self.page.eval_on_selector_all(
            "#eg-sheet-body .we-mode-name", "els => els.map(e => e.textContent)")
        self.assertGreaterEqual(len(picks), 1, "the narrator should be listed")
        self.page.keyboard.press("Escape")
        self._settle(600)

        self._open_leaf("dot:image")
        picks = self.page.eval_on_selector_all(
            "#eg-sheet-body .we-mode-name", "els => els.map(e => e.textContent)")
        self.assertGreater(len(picks), 1, "the image presets should be listed")
        # Exactly one of them is current.
        self.assertEqual(
            self.page.eval_on_selector_all(
                "#eg-sheet-body .we-mode.active", "els => els.length"), 1)
        self.page.keyboard.press("Escape")
        self._settle(600)

        # Voice CASTS, it doesn't just report: two dropdowns over the whole
        # library, falling back to the shipped registry with no key on the box.
        self._open_leaf("dot:voice")
        self.assertEqual(
            self.page.eval_on_selector_all(
                "#eg-sheet-body .eg-field-k", "els => els.map(e => e.textContent)"),
            ["Default voice", "Narrator"])
        opts = self.page.eval_on_selector_all(
            "#eg-sheet-body select", "els => els.map(s => s.options.length)")
        self.assertEqual(len(opts), 2)
        self.assertTrue(all(n > 1 for n in opts), f"voice menus look empty: {opts}")

    def test_controls_holds_the_movement_strip_and_a_key_card(self):
        """The CONTROLS strip is the panel's own wired element on loan, not a
        second copy of it. Schemes are per camera; Tank / Look are starting
        layouts, and every action can be rebound."""
        self._open_ring()
        self._dive("dot:world")
        self._open_leaf("dot:controls")
        self.assertEqual(
            self.page.eval_on_selector_all(
                "#eg-sheet-body .eg-group > .we-cast-label",
                "els => els.map(e => e.textContent)"),
            ["Movement", "Panel", "Start over"])
        self.assertTrue(self.page.evaluate(
            "!!document.querySelector('#eg-sheet-body #we-input-opts')"))
        self.assertTrue(self.page.evaluate(
            "!!document.querySelector('#we-input-schemes button[data-value=\"third_person\"]')"))
        self.assertTrue(self.page.evaluate(
            "!!document.querySelector('#we-input-keys')"))
        self.assertIn("Forward", self.page.evaluate(
            "() => document.getElementById('we-input-keys').innerText"))

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
        # And they are all still there, inside Game > Controls.
        self._open_ring()
        self._dive("dot:world")
        self._open_leaf("dot:controls")
        self.assertTrue(self.page.evaluate(
            "!!document.querySelector('#eg-sheet-body #we-panel-opts')"))

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
        core = self._dot(self._core_id())
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

    def test_a_setting_is_a_control_not_a_paragraph(self):
        """Scan and Camp were panels of facts you could read and not touch, which
        is indistinguishable from a broken control. They now carry a real select,
        a real slider and a real switch, and each one persists through
        /api/admin/studio/tunables."""
        before = self._tunables()
        try:
            self._open_ring()
            self._dive("dot:world")
            self._dive("dot:mechanics")

            self._open_leaf("dot:scan")
            # The knobs must not wait on /api/health. They used to share a
            # Promise.all with it, and on the first open after a boot that meant
            # waiting for MediaPipe to import — so the detector panel was empty
            # for seconds, which is the whole "settings don't work" complaint in
            # miniature. Generous timeout, tight expectation: it should be there
            # almost immediately, but the failure worth catching is "never".
            self.page.wait_for_selector("#eg-sheet-body select", timeout=8000)
            self.assertEqual(
                self.page.eval_on_selector_all("#eg-sheet-body select", "e => e.length"), 1)
            self.assertEqual(
                self.page.eval_on_selector_all(
                    "#eg-sheet-body input[type=range]", "e => e.length"), 1)
            # Picking the on-device detector actually lands on the server.
            self.page.select_option("#eg-sheet-body select", "local")
            self.page.wait_for_timeout(1400)
            self.assertEqual(self._tunables().get("detect_backend"), "local")

            self.page.keyboard.press("Escape")
            self._settle(700)
            self._open_leaf("dot:camp")
            self.assertEqual(
                self.page.eval_on_selector_all("#eg-sheet-body .eg-switch", "e => e.length"), 1)
            self.page.click("#eg-sheet-body .eg-switch")
            self.page.wait_for_timeout(1400)
            self.assertFalse(self._tunables().get("camp_include_jeep"),
                             "the jeep switch should reach the server")
        finally:
            self._put_tunables({"_clear": True})
            self.assertEqual(self._tunables().get("detect_backend"),
                             before.get("detect_backend"))

    def test_camp_is_directable_not_hardcoded(self):
        """Camp's establishing shot used to be a wall of strings in engine.py —
        the one scene the game composes for you was the one you couldn't direct.
        It's a prompt key now, editable in place, and an edit reaches the file."""
        before = self._prompt("camp_scene_prompt")
        self.assertTrue(before, "camp_scene_prompt should ship with a default")
        try:
            self._open_ring()
            self._dive("dot:world")
            self._dive("dot:mechanics")
            self._open_leaf("dot:camp")
            box = self.page.query_selector("#eg-sheet-body .eg-prompt")
            self.assertIsNotNone(box, "camp should have a prompt to edit")
            # The runtime facts are placeholders, not baked strings.
            for var in ("{vantage}", "{terrain}", "{who}"):
                self.assertIn(var, box.input_value())
            box.click()
            self.page.keyboard.press("End")
            self.page.keyboard.type(" A dog sleeps by the fire.")
            self.page.click(
                "#eg-sheet-body .eg-acts .we-btn-primary")
            self.page.wait_for_timeout(1500)
            self.assertIn("A dog sleeps by the fire.",
                          self._prompt("camp_scene_prompt"))
        finally:
            self._put_prompt("camp_scene_prompt", before)
            self.assertEqual(self._prompt("camp_scene_prompt"), before)

    def test_music_can_be_written_or_brought(self):
        """The score was the one part of the soundtrack you couldn't touch: it
        derived itself from each scene and that was that. Now a loop you upload
        or generate takes over, and it takes over for EVERY caller — the
        override lives in get_scene_audio, not in one player."""
        before = json.loads(urllib.request.urlopen(
            self.base_url + "/api/music").read().decode())
        before = before.get("data") or before

        def _put_music(prompt, menu_prompt):
            req = urllib.request.Request(
                self.base_url + "/api/music",
                data=json.dumps({
                    "prompt": prompt, "menu_prompt": menu_prompt,
                }).encode(),
                headers={"Content-Type": "application/json"},
                method="PUT")
            urllib.request.urlopen(req).read()

        def _clear_loops():
            urllib.request.urlopen(urllib.request.Request(
                self.base_url + "/api/music", method="DELETE")).read()
            urllib.request.urlopen(urllib.request.Request(
                self.base_url + "/api/music?for=menu", method="DELETE")).read()

        _put_music("", "")
        _clear_loops()
        try:
            self._open_ring()
            self._dive("dot:world")
            self._dive("dot:mechanics")
            opened = self.page.evaluate(
                "(id) => window.EditorGraph.activate(id)", "dot:music")
            self.assertTrue(opened, "Music should still be on the mechanics ring")
            self.page.wait_for_selector("#eg-sheet.is-open", timeout=4000)
            self.page.wait_for_function(
                """() => {
                  const t = document.getElementById('eg-sheet-title');
                  return t && t.textContent === 'Music';
                }""",
                timeout=4000)
            labels = self.page.eval_on_selector_all(
                "#eg-sheet-body .eg-group > .we-cast-label",
                "els => els.map(e => e.textContent)")
            self.assertEqual(
                labels[:3],
                ["How it sounds", "Playing", "Or bring your own"])
            self.assertIn("Title screen", labels)
            self.assertIn("Ambience", labels)
            self.assertIn("Test a scene", labels)
            self.assertIn("Stock", labels)
            self.assertIn("Cache", labels)
            body = self.page.text_content("#eg-sheet-body")
            self.assertIn("Play loop", body)
            self.assertIn("Play menu", body)
            self.assertIn("Play music", body)
            self.assertIn("Generate missing", body)
            # Both ways in are actually here.
            self.assertTrue(self.page.evaluate(
                "!!document.querySelector('#eg-sheet-body .eg-prompt')"))
            self.assertTrue(self.page.evaluate(
                "!!document.querySelector('#eg-sheet-body .eg-file')"))
            self.assertIn("follows each scene", body)

            # Upload a real WAV the way a person would.
            self.page.set_input_files("#eg-sheet-body .eg-file", {
                "name": "my-loop.wav",
                "mimeType": "audio/wav",
                "buffer": _tiny_wav(),
            })
            self.page.wait_for_timeout(2200)
            body = self.page.text_content("#eg-sheet-body")
            self.assertIn("your upload", body)
            self.assertIn("my-loop.wav", body)
            self.assertTrue(self.page.evaluate(
                "!!document.querySelector('#eg-sheet-body .eg-audio')"),
                "you should be able to hear what you just chose")

            # And the scene endpoint every player already uses now hands that
            # loop back instead of scoring the scene.
            #
            # One loop, one filename — the extension follows the file you gave
            # it — but the URL is STAMPED with which loop it is. Without the
            # stamp every loop anyone ever sets has the identical address, so
            # the browser cache and the player's "am I already playing this?"
            # check both keep the previous track and choosing new music appears
            # to do nothing at all.
            url = self._scene_audio("a dark corridor").get("audio_url")
            self.assertTrue(url.startswith("/audio/loop.wav?v="), url)
            first = url

            # Replace it, and the address has to move with it.
            self.page.set_input_files("#eg-sheet-body .eg-file", {
                "name": "second.wav", "mimeType": "audio/wav",
                "buffer": _tiny_wav(),
            })
            self.page.wait_for_timeout(2200)
            self.assertNotEqual(
                self._scene_audio("a dark corridor").get("audio_url"), first,
                "a different loop must not be served from the same URL")

            # Back to per-scene, and the endpoint follows.
            self.page.click("#eg-sheet-body .we-btn-ghost")
            self.page.wait_for_timeout(1500)
            self.assertIn("follows each scene",
                          self.page.text_content("#eg-sheet-body"))
        finally:
            _clear_loops()
            _put_music(before.get("direction") or "",
                       before.get("menu_direction") or "")

    def test_clearing_everything_takes_two_taps(self):
        """One button to get out of a mess, and it asks first — it empties four
        sheets and every knob."""
        self._open_ring()
        self._dive("dot:world")
        self._open_leaf("dot:controls")
        btn = self.page.query_selector("#eg-sheet-body .eg-group:last-child .we-btn")
        self.assertEqual(btn.text_content().strip(), "Clear everything")
        btn.click()
        self.page.wait_for_timeout(300)
        self.assertIn("Sure?", btn.text_content(),
                      "the first tap should arm it, not fire it")

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
        self.assertEqual(self._shown(), ["experience"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
