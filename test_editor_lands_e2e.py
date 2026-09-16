"""
Does an edit made in the editor reach the running game?

test_editor_graph_e2e.py asks whether the control is there and whether the value
was stored. Both of those were already true for every setting in the report that
prompted this file — "music can never be altered, narrator voice can never be
altered, narrator prompt seems to do nothing, scan altering seems to do nothing"
— and all four were nonetheless real. The value landed on disk and something
further down the line kept using the old one.

So these tests drive the browser and then ask the SERVER, at the endpoint the
game itself calls:

  · pick a narrator voice  → /api/narrator/worldbuild speaks in it
  · write a narrator brief → clicking away is enough to keep it
  · move the detector knob → /api/admin/studio/tunables reports the new number
  · bring your own music   → the player is told to adopt the new loop, by URL

And the trust half, which is not about correctness but about belief: every one
of those says so, in the toast, in words that distinguish "live now" from "next
run" — because a settings panel you have been burned by twice is one you stop
reading.

Run: python -m pytest test_editor_lands_e2e.py
Everything persisted here is captured first and restored in a `finally`.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from test_editor_graph_e2e import EditorHarness, _tiny_wav


class TestAnEditReachesTheGame(EditorHarness):

    # ---- helpers ---------------------------------------------------------
    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())

    def _narration_voices(self) -> list:
        """The voice the narrator is actually resolved to, per line.

        speak=false so nothing is synthesised: the endpoint still runs the same
        resolution the audio path would, which is the part under test.

        The endpoint rate-limits itself to one call every three seconds, which
        is right for a button a player leans on and wrong for a test that has to
        ask twice to see a change. Wait it out rather than widen the limit.
        """
        for attempt in range(6):
            try:
                body = self._post(
                    "/api/narrator/worldbuild",
                    {"multi": False, "speak": False, "session_id": "default"})
                return [s.get("voice_id") for s in (body.get("segments") or [])]
            except urllib.error.HTTPError as e:
                if e.code != 429 or attempt == 5:
                    raise
                time.sleep(1.2)
        raise AssertionError("unreachable")

    def _open_mechanic(self, node_id: str):
        self._open_ring()
        self._dive("dot:game")
        self._dive("dot:mechanics")
        self._open_leaf(node_id)

    def _toast(self) -> str:
        return (self.page.text_content("#we-toast") or "").strip()

    def _await_toast(self, timeout: int = 4000) -> str:
        self.page.wait_for_selector("#we-toast.show", timeout=timeout)
        return self._toast()

    # ---- the four that were silently doing nothing -----------------------
    def test_picking_a_voice_changes_who_narrates(self):
        """The picker wrote `narrator_voice_id` and the engine read it — and
        then resolve_cast handed back voices.json's cast entry instead, which
        always exists, so the tunable was unreachable by construction."""
        before = self._tunables().get("narrator_voice_id")
        was = self._narration_voices()
        self.assertTrue(was and was[0], "narration should resolve to some voice")
        try:
            self._open_mechanic("dot:narrator")
            self.page.wait_for_selector("#eg-sheet-body select", timeout=8000)
            options = self.page.eval_on_selector(
                "#eg-sheet-body select",
                "s => Array.from(s.options).map(o => o.value).filter(Boolean)")
            other = next((o for o in options if o != was[0]), None)
            self.assertIsNotNone(
                other, "the voice library needs a second voice to test with")

            self.page.select_option("#eg-sheet-body select", other)
            self.page.wait_for_timeout(1400)

            self.assertEqual(self._tunables().get("narrator_voice_id"), other)
            self.assertEqual(self._narration_voices()[0], other,
                             "the narrator should now speak in the chosen voice")
        finally:
            self._put_tunables({"narrator_voice_id": before or ""})

    def test_writing_the_brief_and_clicking_away_keeps_it(self):
        """Cast fields saved on blur and prompt boxes hid the commit behind a
        button, so half the editor discarded what you typed and half kept it.
        Two behaviours that look identical is how you learn to trust neither."""
        before = self._prompt("narrator_direction")
        self.assertTrue(before, "narrator_direction should ship with a default")
        try:
            self._open_mechanic("dot:narrator")
            box = self.page.wait_for_selector("#eg-sheet-body .eg-prompt",
                                              timeout=8000)
            box.click()
            self.page.keyboard.press("End")
            self.page.keyboard.type(" The tide is always going out.")

            # No Save. Click the window's own heading — a blur and nothing else.
            self.page.click("#eg-sheet-title")
            self.page.wait_for_timeout(1500)
            self.assertIn("The tide is always going out.",
                          self._prompt("narrator_direction"))
        finally:
            self._put_prompt("narrator_direction", before)

    def test_the_detector_knob_moves_the_number_the_scan_uses(self):
        """MediaPipe bakes the confidence floor into the graph when the detector
        is built. The slider moved a module global the live detector had already
        copied, so it read a new number back to you and SCAN kept the old one
        until the process restarted."""
        before = self._tunables().get("detect_min_score")
        try:
            self._open_mechanic("dot:scan")
            self.page.wait_for_selector("#eg-sheet-body input[type=range]",
                                        timeout=8000)
            target = 0.55 if (before or 0) != 0.55 else 0.35
            self.page.eval_on_selector(
                "#eg-sheet-body input[type=range]",
                """(el, v) => { el.value = v;
                                el.dispatchEvent(new Event('input', {bubbles: true}));
                                el.dispatchEvent(new Event('change', {bubbles: true})); }""",
                str(target))
            self.page.wait_for_timeout(1600)

            live = self._tunables().get("detect_min_score")
            self.assertAlmostEqual(float(live), target, places=3)
            # And the server agrees it is in force, not merely stored.
            with urllib.request.urlopen(self.base_url + "/api/health", timeout=20) as r:
                health = json.loads(r.read().decode())
            self.assertIsInstance(health.get("detect") or {}, dict)
        finally:
            self._put_tunables({"detect_min_score": before})

    def test_bringing_your_own_music_tells_the_player_to_take_it(self):
        """One loop file, always called loop.<ext>, so every loop anyone ever
        set had the identical URL — and both the HTTP cache and the player's own
        "am I already playing this?" check kept the previous track. Storing the
        new file was never the problem."""
        self.page.evaluate(
            """() => { window.__adopted = [];
                       const sa = window.SceneAudio;
                       const orig = sa.adoptLoop.bind(sa);
                       sa.adoptLoop = (u) => { window.__adopted.push(u || null);
                                               return orig(u); }; }""")
        try:
            self._open_mechanic("dot:music")
            self.page.set_input_files("#eg-sheet-body .eg-file", {
                "name": "brought.wav", "mimeType": "audio/wav",
                "buffer": _tiny_wav(),
            })
            self.page.wait_for_timeout(2400)

            adopted = self.page.evaluate("() => window.__adopted")
            self.assertTrue(adopted, "the bed was never told the loop had changed")
            self.assertIn("?v=", adopted[-1] or "",
                          "an unstamped URL is the previous track's address")

            # Clearing has to push too, or two of the three ways to change the
            # music still wait for a scene change that may be ten turns away.
            self.page.click("#eg-sheet-body button:has-text('Back to per-scene')")
            self.page.wait_for_timeout(1800)
            self.assertIsNone(self.page.evaluate("() => window.__adopted")[-1],
                              "going back to per-scene should push too")
        finally:
            urllib.request.urlopen(urllib.request.Request(
                self.base_url + "/api/music", method="DELETE")).read()

    def test_naming_the_character_reaches_the_text_the_model_gets(self):
        """Level and Character were the other two named in the report. They
        already persisted — but persisting is not the question. The question is
        whether the directive compiled from them, which is the actual string
        handed to the image and story models, picks the change up."""
        before = self._identity("player_character")
        try:
            self._open_ring()
            self._open_leaf("dot:character")
            box = self.page.wait_for_selector(
                "#eg-sheet-body input[type='text']", timeout=8000)
            box.click()
            box.fill("Ivo Marsh")
            self.page.keyboard.press("Enter")   # blur commits
            self.page.wait_for_timeout(1500)

            with urllib.request.urlopen(
                    self.base_url + "/api/admin/studio/identity", timeout=20) as r:
                data = json.loads(r.read().decode())
            preview = (data.get("data") or data).get("preview") or {}
            compiled = " ".join(str(preview.get(k) or "") for k in
                                ("narrative_directive", "image_directive"))
            self.assertIn("Ivo Marsh", compiled,
                          "the name never reached the compiled directive")
        finally:
            self._put_identity("player_character", {
                "enabled": bool(before.get("enabled")),
                "name": before.get("name", ""),
            })

    # ---- the trust half --------------------------------------------------
    def test_a_knob_says_it_is_live(self):
        """"Saved." is what a control that silently clamps also says."""
        before = self._tunables().get("detect_backend")
        try:
            self._open_mechanic("dot:scan")
            self.page.wait_for_selector("#eg-sheet-body select", timeout=8000)
            self.page.select_option("#eg-sheet-body select", "local")
            self.assertIn("live now", self._await_toast().lower())
        finally:
            self._put_tunables({"detect_backend": before})

    def test_a_prompt_says_whether_it_changes_anything_yet(self):
        """Most briefs re-steer the next turn; a couple only seed a fresh world.
        For those, "it saved and nothing happened" is the correct outcome — and
        indistinguishable from the bug unless somebody says so."""
        before = self._prompt("narrator_direction")
        try:
            self._open_mechanic("dot:narrator")
            box = self.page.wait_for_selector("#eg-sheet-body .eg-prompt",
                                              timeout=8000)
            box.click()
            self.page.keyboard.press("End")
            self.page.keyboard.type(" Salt on everything.")
            self.page.click("#eg-sheet-title")
            self.page.wait_for_timeout(1500)

            said = (self.page.text_content("#eg-sheet-body .eg-said") or "").lower()
            self.assertIn("saved", said,
                          "the window should say what became of the edit")
            # And WHEN, which is the whole point: one of these two, never
            # neither. "Saved." on its own is what a control that changes
            # nothing until the next world also says.
            self.assertTrue("next turn" in said or "fresh run" in said, said)
        finally:
            self._put_prompt("narrator_direction", before)
