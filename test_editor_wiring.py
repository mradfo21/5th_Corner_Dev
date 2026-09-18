#!/usr/bin/env python3
"""An edit in the editor has to reach the game.

Every setting in here was reachable, validated, persisted to disk and applied to
a live module — and four of them still did nothing, because something further
down the line quietly won the argument. That is the worst failure mode a
settings panel has: it is indistinguishable from a working one until you go
looking for the effect, and once you have been burned twice you stop believing
any of it.

  · the NARRATOR VOICE lost to a hardcoded entry in voices.json
  · the NARRATOR PROMPT was only read on a code path the button doesn't take
  · the MIN SCORE slider moved a number the detector had already copied
  · the MUSIC loop was served from a URL that never changed, so the browser
    cache and the player's own dedupe both kept the previous track

None of those are visible in the write path, which is why they survived. So
these tests do not check that a value was stored. They check the last mile: the
value the game actually uses, at the point it uses it.

Run: python -m pytest test_editor_wiring.py
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

import engine
import local_vision
import scene_audio
import tunables


class TestNarratorVoiceAnswersToTheEditor(unittest.TestCase):
    """`narrator_voice_id` is the editor's, and it has to win.

    voices.json ships a `cast.narrator` entry, and every caller resolved
    `cast["voice_id"] or ELEVENLABS_NARRATOR_VOICE_ID`. The cast entry is always
    present, so the second half of that expression was dead code and the picker
    in the Narrator window could never change anything.
    """

    def setUp(self):
        self.original = engine.ELEVENLABS_NARRATOR_VOICE_ID

    def tearDown(self):
        engine.ELEVENLABS_NARRATOR_VOICE_ID = self.original

    def test_the_shipped_registry_does_name_a_narrator(self):
        """If this ever stops being true the bug above cannot happen — and this
        test stops being about anything. Assert the precondition."""
        cast = (engine.VOICES_CONFIG.get("cast") or {}).get("narrator") or {}
        self.assertTrue(cast.get("voice_id"),
                        "voices.json is expected to cast a narrator")

    def test_changing_the_voice_changes_who_speaks(self):
        engine.ELEVENLABS_NARRATOR_VOICE_ID = "SOMEONEELSE"
        self.assertEqual(engine.resolve_cast("narrator")["voice_id"], "SOMEONEELSE")
        self.assertEqual(engine._segment_voice("narrator"), "SOMEONEELSE")

    def test_the_registry_still_supplies_the_delivery(self):
        """Only WHO is editor-owned. How they read — stability, speed — is still
        the cast sheet's, or picking a voice would silently flatten the
        performance as well."""
        engine.ELEVENLABS_NARRATOR_VOICE_ID = "SOMEONEELSE"
        entry = engine.resolve_cast("narrator")
        shipped = (engine.VOICES_CONFIG.get("cast") or {})["narrator"]
        for key in ("stability", "speed"):
            if key in shipped:
                self.assertEqual(entry.get(key), shipped[key])

    def test_nothing_moves_until_someone_moves_it(self):
        """The global is seeded from voices.json, so an untouched install has to
        behave exactly as it did before any of this."""
        engine.ELEVENLABS_NARRATOR_VOICE_ID = (
            engine.VOICES_CONFIG.get("narrator_voice") or "")
        self.assertEqual(engine.resolve_cast("narrator")["voice_id"],
                         (engine.VOICES_CONFIG.get("cast") or {})["narrator"]["voice_id"])

    def test_other_characters_are_not_hijacked(self):
        """Only the narrator is editor-cast. A warden must stay the warden."""
        engine.ELEVENLABS_NARRATOR_VOICE_ID = "SOMEONEELSE"
        warden = (engine.VOICES_CONFIG.get("cast") or {}).get("warden") or {}
        if warden.get("voice_id"):
            self.assertEqual(engine.resolve_cast("warden")["voice_id"],
                             warden["voice_id"])

    def test_an_unknown_character_falls_back_to_the_chosen_narrator(self):
        engine.ELEVENLABS_NARRATOR_VOICE_ID = "SOMEONEELSE"
        self.assertEqual(engine.resolve_cast("a passing stranger")["voice_id"],
                         "SOMEONEELSE")

    def test_the_registry_is_not_handed_out_to_be_mutated(self):
        """resolve_cast used to return the live dict out of VOICES_CONFIG."""
        engine.resolve_cast("narrator")["voice_id"] = "CLOBBERED"
        self.assertNotEqual(
            (engine.VOICES_CONFIG.get("cast") or {})["narrator"]["voice_id"],
            "CLOBBERED")

    def test_the_tunable_reaches_the_global_the_engine_reads(self):
        """The link between the store and the module. Both halves work in
        isolation; this is the join."""
        tunables._apply_one("narrator_voice_id", "FROMTHEEDITOR")
        try:
            self.assertEqual(engine.resolve_cast("narrator")["voice_id"],
                             "FROMTHEEDITOR")
        finally:
            engine.ELEVENLABS_NARRATOR_VOICE_ID = self.original


class TestNarratorPromptIsActuallyRead(unittest.TestCase):
    """`narrator_direction` was read on the single-voice path only, and the
    narrator button asks for the multi-voice one. So the editor's largest,
    most prominent narrator control governed a code path a player never took.
    """

    # A sentence no shipped prompt would ever contain, so finding it in what the
    # model was handed can only mean the editor's text got there.
    BRIEF = "SPEAK ONLY OF THE SALT MARSH AND THE NUMBER NINE."

    def _prompt_for(self, multi: bool) -> str:
        """Run the real narration path and return what the model was asked.

        Reading engine.py as a string was the previous test here, and it would
        have passed against a version that built the brief and then dropped it
        on the floor — which is exactly the bug. So: capture the argument.
        """
        import prompts_store
        seen = []
        before_ask, before_llm = engine._ask, engine.LLM_ENABLED
        before_prompt = prompts_store.PROMPTS.get("narrator_direction")
        try:
            prompts_store.PROMPTS["narrator_direction"] = self.BRIEF
            engine.LLM_ENABLED = True
            engine._ask = lambda prompt, **kw: (seen.append(prompt), "[]")[1]
            engine._narrator_script("", multi, "default")
        finally:
            engine._ask, engine.LLM_ENABLED = before_ask, before_llm
            if before_prompt is None:
                prompts_store.PROMPTS.pop("narrator_direction", None)
            else:
                prompts_store.PROMPTS["narrator_direction"] = before_prompt
        self.assertTrue(seen, "the narrator should have asked the model something")
        return seen[0]

    def test_the_multi_voice_path_asks_for_what_the_editor_wrote(self):
        """This is the button in the game. It was the one path that ignored the
        prompt, which is why editing it looked exactly like editing nothing."""
        self.assertIn(self.BRIEF, self._prompt_for(multi=True))

    def test_the_single_voice_path_still_does(self):
        self.assertIn(self.BRIEF, self._prompt_for(multi=False))

    def test_the_handoff_contract_survives_an_authored_brief(self):
        """Multi-voice narration is parsed as JSON. An authored brief replaces
        the wording, not the output shape — drop the contract and every
        narration falls back."""
        prompt = self._prompt_for(multi=True)
        self.assertIn("JSON array", prompt)
        self.assertLess(prompt.index(self.BRIEF), prompt.index("JSON array"),
                        "the format instruction has to come last to win")

    def test_a_template_with_a_bad_placeholder_does_not_crash(self):
        """Authoring mistakes are normal. Falling back to the shipped voice is
        the right answer; a 500 on the narrator button is not."""
        import prompts_store
        original = prompts_store.PROMPTS.get("narrator_direction")
        try:
            prompts_store.PROMPTS["narrator_direction"] = "Speak of {nonsense}."
            self.assertEqual(engine._authored_narrator_brief(world="w"), "")
        finally:
            if original is not None:
                prompts_store.PROMPTS["narrator_direction"] = original

    def test_the_placeholders_the_engine_supplies_all_render(self):
        """The shipped template has to survive the fill it is given, or the
        narrator silently drops to the fallback for everyone, for ever."""
        out = engine._authored_narrator_brief(
            world="a place", self="someone", premise="a premise",
            scene="\n\nCURRENT SCENE: a room", recent="", focus="", avoid="")
        self.assertTrue(out, "the shipped narrator_direction should format")
        self.assertIn("a place", out)
        self.assertNotIn("{", out.replace("{{", "").replace("}}", ""))


class TestMinScoreReachesTheDetector(unittest.TestCase):
    """MediaPipe bakes the confidence floor into the graph at construction, so
    moving the module global after the detector exists changes nothing. The
    slider reported a new number and SCAN kept using the old one."""

    def test_the_build_score_is_tracked(self):
        self.assertTrue(hasattr(local_vision, "_detector_score"))

    def test_a_stale_detector_is_retired_rather_than_reused(self):
        """Faked, because building the real thing costs a MediaPipe import: the
        contract under test is "a cached detector whose threshold no longer
        matches MIN_SCORE must not be handed back".

        The rebuild is stopped by pointing DETECT_MODEL_PATH at nothing, not by
        pre-setting _load_failed — the rebuild path clears that flag on purpose,
        because a detector that once existed proves the model is loadable.
        """
        class FakeDetector:
            closed = False

            def close(self):
                FakeDetector.closed = True

        before_score, before_det = local_vision._detector_score, local_vision._detector
        before_min = local_vision.MIN_SCORE
        before_failed, before_err = local_vision._load_failed, local_vision._load_error
        before_model = os.environ.get("DETECT_MODEL_PATH")
        try:
            local_vision._detector = FakeDetector()
            local_vision._detector_score = 0.15
            local_vision.MIN_SCORE = 0.15
            local_vision._load_failed = False
            self.assertIs(local_vision._load_detector(), local_vision._detector,
                          "an up-to-date detector should be reused")

            # The editor moves the slider.
            local_vision.MIN_SCORE = 0.55
            os.environ["DETECT_MODEL_PATH"] = str(
                Path(__file__).parent / "models" / "no-such-model.tflite")
            self.assertIsNone(local_vision._load_detector())
            self.assertTrue(FakeDetector.closed,
                            "the detector built on the old threshold must be closed")
            self.assertIsNone(local_vision._detector)
        finally:
            local_vision._detector = before_det
            local_vision._detector_score = before_score
            local_vision.MIN_SCORE = before_min
            local_vision._load_failed = before_failed
            local_vision._load_error = before_err
            if before_model is None:
                os.environ.pop("DETECT_MODEL_PATH", None)
            else:
                os.environ["DETECT_MODEL_PATH"] = before_model

    def test_the_tunable_moves_the_number_the_detector_reads(self):
        before = local_vision.MIN_SCORE
        try:
            tunables._apply_one("detect_min_score", 0.42)
            self.assertAlmostEqual(local_vision.MIN_SCORE, 0.42)
        finally:
            local_vision.MIN_SCORE = before


class TestEveryLoopGetsItsOwnAddress(unittest.TestCase):
    """There is one loop file and it is always called loop.<ext>, so without a
    stamp every loop ever chosen has the same URL — and both the HTTP cache and
    the player's "already playing this" check keep the previous track."""

    def test_the_url_carries_the_version(self):
        url = scene_audio._loop_url({"file": "loop.wav", "created_at": 1700000000.25})
        self.assertTrue(url.startswith("/audio/loop.wav?v="), url)

    def test_two_loops_a_moment_apart_differ(self):
        """Seconds resolution was not enough: a replacement is one disk write
        and one round trip after the original."""
        a = scene_audio._loop_url({"file": "loop.wav", "created_at": 1700000000.100})
        b = scene_audio._loop_url({"file": "loop.wav", "created_at": 1700000000.480})
        self.assertNotEqual(a, b)

    def test_the_same_loop_keeps_the_same_address(self):
        """Or every poll would re-download and re-decode the audio."""
        meta = {"file": "loop.wav", "created_at": 1700000000.25}
        self.assertEqual(scene_audio._loop_url(meta), scene_audio._loop_url(meta))

    def test_the_path_still_resolves_back_to_the_file(self):
        """The query is not part of the filename, and the server splits them —
        but the round trip is the thing that would break silently."""
        url = scene_audio._loop_url({"file": "loop.wav", "created_at": 1.0})
        name = url.rsplit("/", 1)[1].split("?", 1)[0]
        self.assertEqual(name, "loop.wav")

    def test_a_meta_with_no_timestamp_is_survivable(self):
        self.assertTrue(scene_audio._loop_url({"file": "loop.wav"}))


class TestTheClientTakesTheChange(unittest.TestCase):
    """The last mile is in the browser, and the failure there is the same shape:
    a change lands on the server and the thing playing never asks again."""

    @classmethod
    def setUpClass(cls):
        here = Path(__file__).parent
        cls.app = (here / "static" / "js" / "standalone.js").read_text(encoding="utf-8")
        cls.graph = (here / "static" / "js" / "editor_graph.js").read_text(encoding="utf-8")
        cls.html = (here / "templates" / "standalone.html").read_text(encoding="utf-8")

    def test_the_bed_can_be_told_to_take_a_new_loop(self):
        self.assertIn("async adoptLoop(url)", self.app)

    def test_the_music_window_tells_it(self):
        """Generating, uploading and clearing all have to push, or two of the
        three still wait for a scene change that may be ten turns away."""
        music = self.graph.split("function sheetMusic", 1)[1].split("\n  function ", 1)[0]
        self.assertIn("B.adoptLoop(url)", music)
        self.assertIn("B.adoptLoop(null)", music)
        self.assertGreaterEqual(music.count("playNow("), 3,
                                "generate, upload, and preview all have to push the bed")

    def test_unlocking_rescores_the_scene_not_the_hash(self):
        """requestedKey became a hash so two long image prompts with the same
        style stamp still re-score. Unlocking used to POST that hash as if it
        were the scene, which is how the next bed turned into garbage."""
        bed = self.app.split("async adoptLoop(url)", 1)[1].split("async enterMenu", 1)[0]
        self.assertIn("requestedPrompt", bed)
        self.assertIn("this.score(raw)", bed)

    def test_a_level_plate_does_not_become_the_scene(self):
        """Uploading a reference used to setScene the raw plate. The plate is
        input to a re-render; the viewport has to wait for the generated shot
        — which the author now asks for with GENERATE (see
        test_editing_saves_but_never_draws), so the upload only saves."""
        upload = self.app.split("function uploadPlate", 1)[1].split("async function deletePlate", 1)[0]
        self.assertNotIn("setScene(plateUrl", upload)
        self.assertIn("persistEditingWorld()", upload)
        self.assertIn("skipResteer", upload)
        self.assertIn("reading the image", upload)
        self.assertNotIn("willFill", upload)
        self.assertIn("world-frame-rendering", self.app)

    def test_the_editor_lets_you_drive_the_live_world(self):
        """WASD still reaches the live world while the desk is open — but only
        when the viewport is focused. Typing in a field never steers."""
        keys = self.app.split("if (WorldEditor.isOpen()) {", 1)[1]
        keys = keys.split("} else if (_isEditorToggleKey(e)", 1)[0]
        self.assertIn("if (_typing) return", keys)
        self.assertIn("Movement.enabled()", keys)
        self.assertIn("Movement.pressKey(mk)", keys)
        self.assertIn("VerbBar.onShift(true)", keys)
        self.assertNotIn("all other shortcuts are blocked behind the editor", keys)

    def test_play_focus_stops_look_while_the_desk_owns_the_pointer(self):
        """Opening the editor, or clicking it, parks look / WASD so moving the
        mouse across the live picture does not steer. Clicking the world
        takes play focus back — that click is not also a SCAN."""
        self.assertIn("const PlayFocus", self.app)
        self.assertIn("PlayFocus.toEditor()", self.app)
        self.assertIn("PlayFocus.toViewport()", self.app)
        self.assertIn("PlayFocus.isViewport()", self.app.split("function allowed()", 1)[1][:400])
        self.assertIn("PlayFocus.isViewport()", self.app.split("function enabled()", 1)[1][:400])
        tap = self.app.split("function onWorldTap(e)", 1)[1][:500]
        self.assertIn("PlayFocus.ateClick", tap)
        busy = self.app.split("function uiBusy()", 1)[1].split("function isUi", 1)[0]
        self.assertNotIn("el.worldEditor", busy)

    def test_mouse_look_is_a_bind_not_a_side_checkbox(self):
        """The mouse is the same remappable list as WASD: keys.mouse = look.
        A leftover checkbox next to Invert Y would be a second, competing control."""
        self.assertIn('DEVICE = "mouse"', self.app)
        self.assertIn("function paintMouseRow", self.app)
        self.assertIn("bindMouse", self.app)
        self.assertNotIn("we-input-mouse-wrap", self.html)
        self.assertNotIn("Mouse look</span>", self.html)

    def test_a_choice_slate_does_not_restage_the_leaving_world(self):
        """MOVE TO faded to black, then player_choice_prompt arrived with the
        previous still as image_url. applyScene rebuilt that world, lifted the
        fade, then the real scene_image faded again."""
        render = self.app.split("function renderItem(item)", 1)[1]
        render = render.split("function renderItems", 1)[0]
        self.assertIn("restageScene", render)
        self.assertIn('item.type === "scene_image"', render)
        self.assertIn('item.type !== "player_choice_prompt"', render)
        apply = render.split("Renderer.applyScene", 1)[0]
        self.assertIn("restageScene", apply)

    def test_a_live_stream_is_not_replaced_by_the_cached_still(self):
        """Frame poll used to setScene the opening still over a running
        Reactor / LingBot stream."""
        paint = self.app.split("function paintViewportFromFrame()", 1)[1]
        paint = paint.split("function keepLiveExperience", 1)[0]
        self.assertIn("Renderer.mode === \"reactor\"", paint)
        self.assertIn("instant: live", paint)
        self.assertIn("if (reactor && showing)", paint)
        self.assertIn("resteerLiveFromSheet()", paint)
        open_fn = self.app.split("async function open(opts)", 1)[1]
        open_fn = open_fn.split("function finishClose", 1)[0]
        self.assertIn("keepLiveExperience()", open_fn)
        keep = self.app.split("function keepLiveExperience()", 1)[1]
        keep = keep.split("function restoreReturn", 1)[0]
        self.assertIn("sceneFromWorld", keep)
        self.assertIn("next.prompt", keep)
        self.assertIn("ReactorRenderer.applyScene", keep)
        self.assertIn("ReactorRenderer.enable()", keep)
        self.assertIn("upgradeToLive", keep)
        self.assertIn("function seedKey", keep)
        self.assertIn("sameSeed", keep)
        self.assertIn("reactorAlreadyRunning", keep)
        self.assertIn("!known && !running", keep)
        self.assertNotIn("showing && next && keepLiveExperience._url", keep)
        self.assertIn("await StartMenu.ensurePlayViewport", open_fn)

    def test_the_editor_does_not_park_a_dummy_vital_bar(self):
        """present() used to force VITAL on whenever the desk was open, and
        CSS shoved it onto SAVE."""
        wants = self.app.split("function editorWantsHud()", 1)[1]
        wants = wants.split("function showHealthBar", 1)[0]
        self.assertIn("DAMAGE_SYSTEM_ENABLED", wants)

    def test_the_editor_seeds_lingbot_from_the_cached_frame(self):
        """LingBot will not start without a prompt. The desk used to hand it
        only the precached still, so applyScene no-op'd and the video stayed
        black."""
        self.assertIn("function sceneFromWorld", self.app)
        self.assertIn("function worldSteerPrompt", self.app)
        self.assertIn("function editorGuideUrl", self.app)
        self.assertIn("latestPlateUrl(\"setting\")", self.app)
        steer = self.app.split("function worldSteerPrompt", 1)[1][:1200]
        self.assertIn("frame_prompt", steer)
        self.assertIn("opening_shot", steer)
        self.assertNotIn("lastScene", steer)
        self.assertNotIn("prev.prompt", steer)
        self.assertIn("prepareLiveScene", self.app)
        self.assertIn("liveScene", self.app)
        self.assertIn("lastImagePrompt", self.app)
        seed = self.app.split("function usableFrameUrl", 1)[1].split("function worldSteerPrompt", 1)[0]
        self.assertIn('source === "placeholder"', seed)
        self.assertNotIn('source === "plate"', seed)
        frames = self.app.split("async function refreshWorldFrames()", 1)[1]
        frames = frames.split("async function flushSave()", 1)[0]
        self.assertIn("keepLiveExperience()", frames)
        live = self.graph.split("function fillPicturePicker", 1)[1].split("\n  function ", 1)[0]
        self.assertIn("prepareLiveScene", live)

    def test_the_harness_is_one_clockwise_turn(self):
        """Picture used to hang off Actions as a dashed side loop, so the
        wait that actually blocks a turn looked optional."""
        flow = self.graph.split("const HARNESS_FLOW = [", 1)[1].split("];", 1)[0]
        self.assertIn('from: "choices", to: "actions"', flow)
        self.assertIn('from: "actions", to: "picture"', flow)
        self.assertIn('from: "picture", to: "state"', flow)
        self.assertIn('from: "state", to: "choices"', flow)
        self.assertNotIn('from: "actions", to: "state"', flow)
        self.assertNotIn('from: "picture", to: "choices"', flow)
        homes = self.graph.split("function rememberHarnessHomes", 1)[1]
        homes = homes.split("function harnessLoop", 1)[0]
        self.assertIn("pinHome(choices, -COL, -ROW)", homes)
        self.assertIn("pinHome(actions, COL, -ROW)", homes)
        self.assertIn("pinHome(picture, COL, ROW)", homes)
        self.assertIn("pinHome(state, -COL, ROW)", homes)
        self.assertIn('"1 Choices → 2 Actions → 3 Picture → 4 State"', self.graph)

    def test_world_links_follow_the_ring(self):
        """Right-out / left-in noodles U-turned around a cycle and crossed
        themselves. Handles have to be the circular-arc tangent on the
        enclosure, or three Worlds look like a pretzel."""
        self.assertIn("function ringHandles", self.graph)
        self.assertIn("function ringCenter", self.graph)
        self.assertIn("(4 / 3)", self.graph)
        handles = self.graph.split("function edgeHandles", 1)[1]
        handles = handles.split("function edgePath", 1)[0]
        self.assertIn("ringHandles", handles)
        self.assertNotIn("a.cx + ar", handles)
        self.assertNotIn("b.cx - br", handles)

    def test_a_world_card_says_edit(self):
        """The caption used to coach a double-tap. The card is just Edit."""
        self.assertNotIn("double-tap to edit.", self.graph)
        self.assertNotIn("Double-tap to edit.", self.graph)
        self.assertNotIn("design this level", self.graph)
        self.assertNotIn("Design the Level", self.graph)
        card = self.graph.split("function openWorldInspector", 1)[1]
        card = card.split("\n  function ", 1)[0]
        self.assertIn('design.textContent = "Edit"', card)

    def test_start_here_marks_the_world_and_its_links(self):
        """'Start a run here' did not read, and choosing it left the graph
        looking the same because setStartWorld never told the dots."""
        self.assertIn('start.textContent = "Start here"', self.graph)
        self.assertNotIn("Start a run here", self.graph)
        self.assertIn('"eg-start"', self.graph)
        self.assertIn("is-from-start", self.graph)
        self.assertIn("notifyGraph()", self.app.split("async function setStartWorld", 1)[1][:500])

    def test_changed_is_yellow_and_start_is_green(self):
        """Red meant dirty AND start AND here, so the map could not be read."""
        css = Path(__file__).parent.joinpath(
            "static", "css", "standalone.css").read_text(encoding="utf-8")
        changed = css.split(".eg-node.is-orbit.is-changed .eg-cell", 1)[1]
        changed = changed.split("}", 1)[0]
        self.assertIn("var(--we-yellow)", changed)
        self.assertNotIn("var(--we-red)", changed)
        start = css.split(".eg-kind-world-node.is-start .eg-cell,", 1)[1]
        start = start.split("}", 1)[0]
        self.assertIn("var(--we-mint)", start)

    def test_the_experience_is_a_node_beside_the_nucleus(self):
        """The Experience used to BE the nucleus, so shrinking the origin
        shrank the only coin on the sheet. A placeable Experience node
        keeps the origin small without making the Experience tiny."""
        self.assertIn('kind: "experience-node"', self.graph)
        self.assertIn('id: "xp"', self.graph)
        self.assertIn("function defaultExperienceHome", self.graph)
        self.assertIn("function seedExperienceHome", self.graph)
        self.assertIn("B.moveExperience", self.graph)
        css = Path(__file__).parent.joinpath(
            "static", "css", "standalone.css").read_text(encoding="utf-8")
        alone = css.split("#world-editor .eg-node.is-alone .eg-cell,", 1)[1]
        alone = alone.split("}", 1)[0]
        self.assertNotIn("--eg-cell-scale: 0.42", alone)
        self.assertIn(".eg-kind-experience-node.is-orbit .eg-cell", css)
        core = css.split("#world-editor .eg-node.is-core .eg-cell {", 1)[1]
        core = core.split("}", 1)[0]
        self.assertIn("--eg-cell-scale: 0.42", core)

    def test_an_open_world_wears_its_ring_outside_the_still(self):
        """The nucleus shrink is for Game / Mechanics. On a World it left a
        mint circle inset in the photo."""
        css = Path(__file__).parent.joinpath(
            "static", "css", "standalone.css").read_text(encoding="utf-8")
        core = css.split(".eg-kind-world-node.is-core .eg-cell,", 1)[1]
        core = core.split("}", 1)[0]
        self.assertIn("--eg-cell-scale: 0.42", core)
        self.assertIn("n.r * 0.97", self.graph)

    def test_outside_the_enclosure_closes_the_worlds(self):
        """A miss inside the shell deselects. A miss outside it is the
        way back to the Experience coin — empty paper used to do nothing."""
        empty = self.graph.split("if (h.where === \"empty\")", 1)[1]
        empty = empty.split("if (h.where === \"core\")", 1)[0]
        self.assertIn("outsideShellAt", empty)
        self.assertIn("surface()", empty)
        self.assertIn("function outsideShellAt", self.graph)

    def test_a_click_puts_a_ring_around_the_dot(self):
        """HERE already owns the cell stroke. Selection has to be a second
        circle or clicking the live World looks like nothing happened."""
        self.assertIn('"eg-pick"', self.graph)
        self.assertIn(".eg-node.is-selected .eg-pick",
                      Path(__file__).parent.joinpath(
                          "static", "css", "standalone.css").read_text(
                              encoding="utf-8"))

    def test_the_desk_can_generate_a_world_picture(self):
        """GENERATE is the ONLY thing in the editor that draws.

        It used to be called REDRAW and it was not the only one: every
        identity field save, every plate upload / delete / clear, APPLY and
        SAVE all ran persistAndRender, so the editor kept drawing a character
        the author was still halfway through writing — and each draw resolved
        to a different half-finished person.
        """
        self.assertIn('id="we-reset"', self.html)
        self.assertIn('id="we-picture-bar"', self.html)
        generate = self.html.split('id="we-reset"', 1)[1].split("</button>", 1)[0]
        self.assertIn(">GENERATE", generate)
        self.assertNotIn(">REDRAW", generate)
        self.assertNotIn(">RESET", generate)
        self.assertIn("async function resetWorldPicture", self.app)
        self.assertIn("async function flushPendingWorldEdits", self.app)
        self.assertIn("data-identity-field", self.app)
        self.assertIn("skipPersist: true", self.app)
        generate = self.app.split("async function resetWorldPicture", 1)[1]
        generate = generate.split("    // Art direction lives in the prompt file", 1)[0]
        self.assertIn("flushPendingWorldEdits()", generate)
        self.assertIn("await persistWorld(wid)", generate)
        self.assertNotIn("catch (_) {}", generate)
        self.assertIn("/api/admin/studio/worlds/frames/reset", generate)
        self.assertIn("keepLiveExperience._url", generate)
        self.assertIn("applySheetToLiveScene()", generate)
        api = Path(__file__).parent.joinpath("api.py").read_text(encoding="utf-8")
        reset = api.split("def admin_studio_world_frames_reset", 1)[1][:900]
        self.assertIn("persist_world_snapshot", reset)
        persist = self.app.split("async function persistAndRender", 1)[1]
        persist = persist.split("let framePollTimer", 1)[0]
        self.assertLess(
            persist.index("persistEditingWorld"),
            persist.index("kickWorldFrame"),
            "a render started before the World was cached used yesterday's sheet")
        card = self.graph.split("function sheetWorldNode", 1)[1].split("\n  function ", 1)[0]
        self.assertIn("B.resetWorldPicture", card)

    def test_editing_saves_but_never_draws(self):
        """The author's complaint, pinned: "make the editor stop drawing and
        generating WHILE I'm editing it"."""
        for fn, end in (
            ("async function saveIdentity", "async function clearIdentityBlock"),
            ("async function clearIdentityBlock", "    function uploadPlate"),
            ("    function uploadPlate", "async function deletePlate"),
            ("async function deletePlate", "    // ── Worlds tab"),
            ("async function flushSave", "    function anyFrameBusy"),
            ("async function applyLive", "async function saveAndRestart"),
        ):
            body = self.app.split(fn, 1)[1].split(end, 1)[0]
            # The call, not the word — these bodies carry comments naming the
            # function they used to call and why they no longer do.
            self.assertNotIn("persistAndRender(", body,
                             f"{fn.strip()} must save, not draw")
            self.assertNotIn("kickWorldFrame(", body,
                             f"{fn.strip()} must save, not draw")

    def test_the_desk_can_roll_back_to_app_defaults(self):
        """RESET sits with REDRAW / SAVE and writes the shipped prompt file,
        not an empty sheet and not a new still."""
        self.assertIn('id="we-defaults"', self.html)
        self.assertIn(">RESET<", self.html)
        body = self.app.split("async function resetToAppDefaults", 1)[1]
        body = body.split("async function resetWorldPicture", 1)[0]
        self.assertIn("/api/admin/studio/prompts/reset", body)
        self.assertIn("all: true", body)
        self.assertNotIn("/api/admin/studio/worlds/frames/reset", body)
        self.assertIn("loadContent(true)", body)

    def test_lingbot_strafe_uses_the_official_enum(self):
        """LingBot World 2 rejects move_lateral:left. The wire has to send
        strafe_left / strafe_right or A/D never moves the camera."""
        reactor = (Path(__file__).parent / "static" / "js" / "reactor_renderer.js")
        src = reactor.read_text(encoding="utf-8")
        self.assertIn("function wireAxisValue", src)
        self.assertIn('value === "left") return "strafe_left"', src)
        self.assertIn('value === "right") return "strafe_right"', src)
        self.assertIn("wireAxisValue(axis, value)", src)

    def test_move_to_does_not_carry_look_onto_the_next_lingbot_stage(self):
        """MOVE TO restages LingBot. A leftover look/strafe hold is why the
        mouse started sliding like A/D after a cut."""
        move = self.app.split("function beginMoveTransition", 1)[1]
        move = move.split("function cancelMoveTransition", 1)[0]
        self.assertIn("Movement.releaseAll()", move)
        self.assertIn("onStageReset", self.app)
        reactor = (Path(__file__).parent / "static" / "js" / "reactor_renderer.js")
        src = reactor.read_text(encoding="utf-8")
        apply = src.split("async function applyRunningLingbot", 1)[1]
        apply = apply.split("async function establishHelios", 1)[0]
        self.assertIn("resetMoveState()", apply)
        oyster = src.split("async function applyRunningHappyOyster", 1)[1]
        oyster = oyster.split("const DRIVERS", 1)[0]
        self.assertIn("resetMoveState()", oyster)
        apply_move = src.split("function applyMoveState()", 1)[1]
        apply_move = apply_move.split("function setAxis", 1)[0]
        self.assertIn('m[k] || "idle"', apply_move)
        self.assertIn("yaws the camera", src)
        decorate = src.split("function decoratePrompt", 1)[1]
        decorate = decorate.split("async function loadConfig", 1)[0]
        self.assertNotIn('familyFor(rstate.modelId) === "seed_locked"', decorate)
        camp = self.app.split("async function openCamp()", 1)[1]
        camp = camp.split("async function leaveCamp", 1)[0]
        self.assertIn("Movement.releaseAll()", camp)

    def test_live_steer_does_not_rebuild_an_oyster_adventure(self):
        """SHAPE / turn-steer on Happy Oyster Adventure used to applyScene a
        new prompt and tear the world down. LingBot can take a live edit;
        Oyster Adventure cannot."""
        steer = self.app.split("steerRealtime(text, where)", 1)[1]
        steer = steer.split("applyDrift(meta)", 1)[0]
        self.assertIn("supportsLiveSteer", steer)
        self.assertIn('kind !== "motion"', steer)
        self.assertIn("Camera.motionClause", steer)
        move = self.app.split("steerMovement(beat)", 1)[1]
        move = move.split("try { window.__Renderer", 1)[0]
        self.assertIn("supportsLiveSteer", move)
        choose = self.app.split("IMMEDIATE WORLD STEER", 1)[1]
        choose = choose.split("If a captured specimen", 1)[0]
        self.assertIn('steerRealtime(steerVerb, { kind: "event" })', choose)
        persp = (Path(__file__).parent / "static" / "js" / "reactor_renderer.js").read_text(encoding="utf-8")
        self.assertIn(
            'getPerspective: () => (isHappyOyster() ? happyOysterPerspective() : null)',
            persp,
        )

    def test_the_title_bed_is_only_a_track_you_locked(self):
        """The menu played the editor's audition sample, and generated one if
        there wasn't one. An audition is not a decision: "horror action score"
        typed into the direction box became ten seconds of clanking metal
        looping under the main menu, which no control in the game turned off.
        The editor already tells you it is "silent until you set one"."""
        menu = self.app.split("async enterMenu()", 1)[1].split("leaveMenu()", 1)[0]
        self.assertIn("menu_loop", menu)
        self.assertNotIn("menu_preview", menu)
        self.assertNotIn("/api/music/preview", menu)
        leave = self.app.split("leaveMenu()", 1)[1].split("endConversation()", 1)[0]
        self.assertIn("bumpToken()", leave)

    def test_a_prompt_commits_when_you_leave_it(self):
        """Cast fields save on blur and prompt boxes hid the commit behind a
        button. Two behaviours that look identical is how you learn not to
        trust either."""
        editor = self.graph.split("function promptEditor", 1)[1]
        self.assertIn('addEventListener("blur"', editor)

    def test_closing_the_window_commits_too(self):
        """Removing a focused node does not reliably fire blur."""
        closer = self.graph.split("function closeSheet", 1)[1].split("\n  }", 1)[0]
        self.assertIn("_commit(false)", closer,
                      "the panel is going away, so the toast is the only place "
                      "left to confirm the edit was kept")

    def test_the_confirmation_outlives_the_save_that_writes_it(self):
        """A save re-renders, and the re-render replaces the paragraph the save
        is about to report into — so the outcome was written to a detached node
        and the visible one kept saying "Unsaved". The message therefore lives
        outside the DOM and is painted onto whichever element is current."""
        self.assertIn("function paintSaid", self.graph)
        editor = self.graph.split("function promptEditor", 1)[1]
        announce = editor.split("const announce =", 1)[1].split("\n", 1)[0]
        self.assertIn("remember(", announce)
        self.assertIn("paintSaid(", announce)
        # And a freshly built editor asks what it should already be saying.
        self.assertIn("paintSaid(key);", editor.split("const announce =", 1)[1])

    def test_the_confirmation_does_not_haunt_the_window(self):
        """Reopening a window much later to be told about an edit you have
        forgotten making is its own kind of noise."""
        self.assertIn("SAID_TTL_MS", self.graph)
        recall = self.graph.split("function recall", 1)[1].split("\n  }", 1)[0]
        self.assertIn("SAID_TTL_MS", recall)

    def test_a_knob_reports_the_effective_value(self):
        """The endpoint answers with what the game is now using. Printing
        "Saved." regardless is how a knob that silently clamps goes unnoticed."""
        setter = self.graph.split("async function setTunable", 1)[1].split("\n  }", 1)[0]
        self.assertIn("values", setter)

    def test_the_narrator_preview_is_the_thing_you_just_edited(self):
        """Proxying to the rail button asked for a radio play, which hands most
        of its lines to other members of the cast — demonstrating neither the
        voice nor the wording the window sets."""
        self.assertIn("multi: false", self.app.split("narratorPreview", 1)[1][:400])
        self.assertIn("B.narratorPreview()", self.graph)

    def test_the_detector_knobs_do_not_wait_on_the_health_probe(self):
        """They shared a Promise.all with /api/health, and the first call after
        a boot waits on MediaPipe importing.

        Comments are stripped before looking: the fix left a note behind that
        names the thing it removed, and a test that reads prose as code fails on
        the very change it is asking for."""
        scan = self.graph.split("function sheetScan", 1)[1].split("\n  function ", 1)[0]
        code = "\n".join(line for line in scan.splitlines()
                         if not line.lstrip().startswith("//"))
        self.assertNotIn("Promise.all", code)
        self.assertIn("loadTunables(true).then(", code)
        self.assertIn('getJson("/api/health").then(', code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
