"""The shipped sound library holds what the game needs, and the game only reads it.

    python -m unittest test_sound_library -v

Since 2026-09-25 no sound is generated while the game runs: the player brings
one key, nothing on it makes sound effects, and Matt's call was to "precache
them FROM my 11 labs and ship precached whatever you need". So the library in
static/audio/library/ IS the sound of the game, and these are its invariants:

  * the catalog and the folder agree, both ways (the prompts_store
    `unwired_keys` pattern: nothing listed that is missing, nothing shipped
    that nothing can pick);
  * every lane has clips, and every stinger id the client knows is one;
  * pick() is deterministic, always answers, and reads the words the game
    actually uses — a MOVE TO toward a blast door is footsteps, not the door;
  * music is instrumental;
  * no runtime module can reach a sound provider;
  * the endpoints hand back library files that are served, never pending.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import unittest
from pathlib import Path

os.environ.setdefault("SOMEWHERE_MOCK", "1")

import sound_library as sl

ROOT = Path(__file__).resolve().parent
LIB = ROOT / "static" / "audio" / "library"


def _catalog() -> dict:
    return json.loads((LIB / "catalog.json").read_text(encoding="utf-8"))


class TestTheCatalogIsTheFolder(unittest.TestCase):
    def test_every_catalog_file_exists_and_every_file_is_catalogued(self):
        cat = _catalog()
        listed = {e["file"] for e in cat["entries"]}
        on_disk = {p.relative_to(LIB).as_posix() for p in LIB.rglob("*")
                   if p.is_file() and p.name != "catalog.json"}
        self.assertEqual(sorted(listed - on_disk), [], "catalogued but missing")
        self.assertEqual(sorted(on_disk - listed), [], "shipped but nothing can pick it")
        for e in cat["entries"]:
            self.assertGreater((LIB / e["file"]).stat().st_size, 1000, e["id"])
            self.assertTrue(e["file"].endswith(".mp3"), e["id"])

    def test_ids_are_unique_and_lanes_known(self):
        ids = [e["id"] for e in _catalog()["entries"]]
        self.assertEqual(len(ids), len(set(ids)))
        for e in _catalog()["entries"]:
            self.assertIn(e["lane"], sl.LANES, e["id"])
            self.assertTrue(e.get("tags"), f"{e['id']} has no tags to be picked by")
            self.assertTrue(e.get("prompt"), f"{e['id']} does not say what made it")

    def test_every_lane_has_enough(self):
        counts = {lane: len(sl.entries(lane)) for lane in sl.LANES}
        self.assertGreaterEqual(counts["ambience"], 20, counts)
        self.assertGreaterEqual(counts["foley"], 60, counts)
        self.assertGreaterEqual(counts["consequence"], 12, counts)
        self.assertGreaterEqual(counts["stinger"], 12, counts)
        self.assertGreaterEqual(counts["music"], 16, counts)
        self.assertTrue(sl.available())

    def test_it_fits_in_the_installer(self):
        total = sum((LIB / e["file"]).stat().st_size for e in _catalog()["entries"])
        self.assertLess(total, 40 * 1024 * 1024, f"{total / 1e6:.1f} MB")

    def test_lengths_and_loops_fit_their_lane(self):
        for e in _catalog()["entries"]:
            lane, secs = e["lane"], float(e["seconds"])
            if lane in ("ambience", "music"):
                self.assertTrue(e["loop"], e["id"])
                self.assertGreaterEqual(secs, 12 if lane == "ambience" else 30, e["id"])
            else:
                self.assertFalse(e["loop"], e["id"])
            if lane == "foley":
                self.assertLessEqual(secs, 3.5, e["id"])
            if lane == "consequence":
                # A bed that plays under the whole wait; the first discovery
                # take came back 0.77 s of near-silence and was retaken.
                self.assertGreaterEqual(secs, 3.0, e["id"])
                self.assertLessEqual(secs, 12.5, e["id"])
            self.assertGreater(float(e.get("level_db", -99)), -45.0, f"{e['id']} is nearly silent")

    def test_every_stinger_the_client_knows_is_shipped(self):
        js = (ROOT / "static" / "js" / "standalone.js").read_text(encoding="utf-8")
        block = js.split("const STOCK_CUE = {", 1)[1].split("};", 1)[0]
        cues = re.findall(r"(\w+):", block)
        self.assertGreaterEqual(len(cues), 12)
        for cue in cues:
            entry = sl.get(cue)
            self.assertIsNotNone(entry, cue)
            self.assertEqual(entry["lane"], "stinger")


class TestMusicIsInstrumental(unittest.TestCase):
    def test_every_track_is_marked_and_was_asked_for_without_voices(self):
        for e in sl.entries("music"):
            self.assertIs(e.get("instrumental"), True, e["id"])
            low = e["prompt"].lower()
            self.assertIn("no vocals", low, e["id"])
            self.assertIn(e.get("mode"), sl.MODES, e["id"])
            self.assertIn(e.get("phase"), sl.PHASES + ("any",), e["id"])

    def test_every_mode_has_a_track_and_the_scene_has_every_phase(self):
        rows = sl.entries("music")
        for mode in sl.MODES:
            self.assertTrue([e for e in rows if e["mode"] == mode], mode)
        desert = {e["phase"] for e in rows if e["mode"] == "scene" and e.get("flavor") == "desert"}
        self.assertEqual(desert, set(sl.PHASES))

    def test_phase_and_world_choose_the_score(self):
        pick = lambda **kw: sl.pick("music", "a corridor", seed="s", **kw)["id"]
        self.assertEqual(pick(mode="scene", phase="normal"), "mus_scene_normal_desert")
        self.assertEqual(pick(mode="scene", phase="critical"), "mus_scene_critical_desert")
        self.assertEqual(pick(mode="scene", phase="escalating"), "mus_scene_escalating_desert")
        self.assertIn("cyber", pick(mode="scene", phase="critical", flavor="cyber"))
        self.assertEqual(sl.pick("music", "", mode="menu", seed="x")["mode"], "menu")
        self.assertEqual(sl.pick("music", "hostile — creature — the thing in the vat",
                                 mode="encounter", seed="x")["id"], "mus_enc_creature")
        # the camp level is scored like any scene; the fire makes it camp
        self.assertEqual(sl.pick("music", "companions sit round the campfire at night",
                                 mode="scene", seed="x")["mode"], "camp")

    def test_the_world_decides_the_flavour(self):
        self.assertEqual(sl.flavor_of("1993. The Four Corners desert, a quarantined facility"), "desert")
        self.assertEqual(sl.flavor_of("a neon megacity of holograms and chrome"), "cyber")
        self.assertEqual(sl.flavor_of("riot police hold the barricade on 5th street"), "riot")
        self.assertEqual(sl.flavor_of(""), "desert")


class TestPick(unittest.TestCase):
    TEXTS = ("", "   ", "a", "Kick open the rusted door",
             "Give the silhouette what they want", "zzzz qqqq", "1.")

    def test_it_always_answers_and_answers_the_same(self):
        for lane in sl.LANES:
            for text in self.TEXTS:
                a = sl.pick(lane, text, seed="one", mode="scene", phase="normal")
                b = sl.pick(lane, text, seed="one", mode="scene", phase="normal")
                self.assertIsNotNone(a, (lane, text))
                self.assertEqual(a["id"], b["id"], (lane, text))
                self.assertEqual(a["lane"], lane)

    def test_nothing_audible_gets_the_fallback_not_a_guess(self):
        for lane in ("foley", "consequence", "ambience"):
            got = sl.pick(lane, "zzzz qqqq", seed="s", context="a grated catwalk")
            if lane == "ambience":
                # the place is ambience's to read, so the catwalk may speak
                continue
            self.assertTrue(got.get("fallback"), (lane, got["id"]))

    def test_the_seed_spreads_ties_across_variants(self):
        seen = {sl.pick("foley", t, seed=t)["id"] for t in (
            "Kick open the rusted door", "Kick in the office door", "Kick the door open",
            "Boot the door", "Kick open the storeroom door", "Kick open the red door",
            "Kick the door in hard", "Kick the cabin door open")}
        self.assertTrue(seen <= {"door_kick_1", "door_kick_2"}, seen)
        self.assertEqual(seen, {"door_kick_1", "door_kick_2"},
                         "two different doors should not always sound identical")

    def test_it_reads_the_words_players_use(self):
        """Lines from real slates (sessions of 2026-09), each with what it must
        sound like."""
        cases = {
            "Kick open the rusted door": "door_kick",
            "Sprint toward the rusted truck": "run_",
            "Smash the laptop screen": "smash_",
            "Smash the white console": "smash_console",
            "Shatter the glass wall graffiti": "smash_glass",
            "Vault over the central desk": "vault_",
            "Scale the chain link fence": "climb_fence",
            "Crawl under the rusted tanks": "crawl",
            "Wear the Rig Welder Breastplate": "gear_strap",
            "Read the Embossed Leather Journal": "book_open",
            "Hack the vending machine until it spits out grenades": "keyboard_typing",
            "Take a photograph of the tower": "camera_shutter",
            "Rip the power cable loose": "rip_metal",
            "Turn the rusted valve wheel": "valve_turn",
            "Heave the heavy blast door open": "blast_door",
            "Swing baton at the Sentinel": "baton_hit",
            "Insert the Uplink Interface Port": "plug_insert",
        }
        for text, want in cases.items():
            got = sl.pick("foley", text, seed=text)["id"]
            self.assertTrue(got.startswith(want), f"{text!r} -> {got}, wanted {want}*")

    def test_a_move_to_toward_a_door_is_footsteps_not_the_door(self):
        """`where` exists for this: "blast door" as a foley tag made "Head for
        the Reinforced Blast Door" play a blast door grinding open."""
        for text in ("Head for the Reinforced Blast Door",
                     "Reach the Sigil Blast Door and go in",
                     "Head for the Chemical Filtration Vat"):
            got = sl.pick("foley", text, seed=text,
                          context="a circular blast door at the end of a corridor")
            self.assertTrue(got["id"].startswith("steps_"), (text, got["id"]))

    def test_the_place_chooses_the_surface(self):
        run = lambda where: sl.pick("foley", "Sprint for the exit", seed="x", context=where)["id"]
        self.assertEqual(run("a metal grate catwalk over a pit"), "run_metal")
        self.assertIn(run("open desert, a dirt road, sagebrush"), ("run_gravel_1", "run_gravel_2"))

    def test_the_frame_chooses_the_place(self):
        cases = {
            ("A rusted pump jack squats in the foreground, chain slapping. Wind moves "
             "grit across the well pad."): "amb_oilfield",
            "A chain-link fence topped with razor wire under a sodium floodlight": "amb_fence_night",
            "A neon megacity street at night, holographic signs over wet asphalt": "amb_neon_rain",
            "Server racks with blinking lights fill a cold data center": "amb_server_room",
            "Companions sit round a campfire, embers drifting": "amb_campfire",
            "A long fluorescent-lit hallway with linoleum tiles": "amb_fluorescent_corridor",
        }
        for text, want in cases.items():
            self.assertEqual(sl.pick("ambience", text, seed="s")["id"], want, text)

    def test_hysteresis_keeps_a_bed_that_is_still_good(self):
        text = "a dim industrial corridor with a blast door"
        best = sl.pick("ambience", text, seed="s")["id"]
        scored = {e["id"]: s for s, e in sl.score_all("ambience", text)}
        near = [i for i, s in scored.items()
                if i != best and 0 < s >= scored[best] * sl.PREFER_RATIO]
        for other in near:
            self.assertEqual(sl.pick("ambience", text, seed="s", prefer=other)["id"], other)
        self.assertEqual(sl.pick("ambience", text, seed="s", prefer="amb_campfire")["id"], best)

    def test_the_consequence_reads_what_happened(self):
        cases = {
            "Muzzle flash lights the corridor as the guard opens fire": "csq_gunfire",
            "The catwalk gives way and the gantry collapses into the pit": "csq_structural_collapse",
            "A klaxon alarm sounds and the lockdown is triggered": "csq_alarm",
            "The pipe bursts and water floods across the floor": "csq_water_rush",
        }
        for text, want in cases.items():
            self.assertEqual(sl.pick("consequence", text, seed=text)["id"], want, text)

    def test_the_stemmer_agrees_with_itself(self):
        for forms in (("kick", "kicks", "kicked", "kicking"), ("slide", "slides", "sliding"),
                      ("door", "doors"), ("run", "running", "runs", "ran"),
                      ("smash", "smashes", "smashed"), ("body", "bodies")):
            self.assertEqual(len({sl.stem(f) for f in forms}), 1, forms)
        self.assertNotEqual(sl.stem("deserted"), sl.stem("desert"))


class TestNothingAtRuntimeReachesAProvider(unittest.TestCase):
    PROVIDER_MARKS = ("api.elevenlabs.io", "xi-api-key", "sound-generation", "/v1/music",
                      "generativelanguage.googleapis.com", "import requests", "urllib.request")

    def test_the_sound_modules_only_read_files(self):
        for rel in ("sound_library.py", "scene_audio.py"):
            src = (ROOT / rel).read_text(encoding="utf-8")
            for mark in self.PROVIDER_MARKS:
                self.assertNotIn(mark, src, f"{rel} contains {mark!r}")
            self.assertNotRegex(src, r"def _generate_", rel)

    def test_elevenlabs_lives_only_in_the_build_tool(self):
        """The one place that may call it is tools/build_sound_library.py,
        which we run; the game never does."""
        callers = []
        for p in list(ROOT.glob("*.py")) + list((ROOT / "static" / "js").glob("*.js")):
            if p.name.startswith("test_") or p.name.startswith("_"):
                continue
            if "api.elevenlabs.io" in p.read_text(encoding="utf-8", errors="replace"):
                callers.append(p.name)
        self.assertEqual(callers, [])
        self.assertIn("api.elevenlabs.io",
                      (ROOT / "tools" / "build_sound_library.py").read_text(encoding="utf-8"))

    def test_the_build_tool_never_writes_a_key(self):
        src = (ROOT / "tools" / "build_sound_library.py").read_text(encoding="utf-8")
        cat = (LIB / "catalog.json").read_text(encoding="utf-8")
        self.assertNotRegex(cat, r"sk_[A-Za-z0-9]{20,}")
        self.assertNotRegex(cat, r"AIza[0-9A-Za-z_\-]{20,}")
        self.assertIn("never printed", src)


class TestTheGameServesIt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import api
        cls.client = api.app.test_client()

    def _get(self, url):
        r = self.client.get(url)
        try:
            return r.status_code, r.mimetype, len(r.data)
        finally:
            r.close()

    def test_scene_audio_answers_with_library_files_now(self):
        body = self.client.post("/api/scene_audio", json={
            "prompt": "A chain-link fence topped with razor wire under a floodlight at night",
            "session": "sound-library-test"}).get_json()
        self.assertTrue(body["audio_url"].startswith(sl.URL_BASE), body)
        self.assertTrue(body["sfx_url"].startswith(sl.URL_BASE), body)
        self.assertFalse(body["pending_music"])
        self.assertFalse(body["pending_sfx"])
        for url in (body["audio_url"], body["sfx_url"]):
            status, mime, size = self._get(url)
            self.assertEqual(status, 200, url)
            self.assertEqual(mime, "audio/mpeg", url)
            self.assertGreater(size, 1000)
        self.assertFalse((ROOT / "sessions" / "sound-library-test").exists(),
                         "asking what a session sounds like must not create it")

    def test_an_encounter_gets_its_stingers(self):
        body = self.client.post("/api/scene_audio", json={
            "prompt": "hostile — creature — the Calcified Watchman", "mode": "encounter",
            "session": "sound-library-test"}).get_json()
        self.assertTrue(body["stinger_url"].endswith("encounter_enter.mp3"))
        self.assertGreaterEqual(len(body["stingers"]), 12)
        self.assertEqual(body["music_id"], "mus_enc_creature")

    def test_foley_and_the_consequence_bed(self):
        f = self.client.post("/api/action_foley", json={
            "action": "Kick open the rusted door", "session": "sound-library-test"}).get_json()
        self.assertTrue(f["url"].startswith(sl.URL_BASE + "foley/door_kick_"), f)
        self.assertFalse(f["pending"])
        c = self.client.post("/api/consequence_audio", json={
            "text": "The pipe bursts and water floods across the concrete floor",
            "session": "sound-library-test"}).get_json()
        self.assertTrue(c["url"].endswith("csq_water_rush.mp3"), c)
        self.assertEqual(self._get(f["url"])[0], 200)

    def test_health_and_music_say_the_library_is_there(self):
        h = self.client.get("/api/health").get_json()
        self.assertIs(h["music"]["can_generate"], True)
        self.assertEqual(h["music"]["reason"], "ready")
        self.assertEqual(h["music"]["stock_ready"], h["music"]["stock_total"])
        m = self.client.get("/api/music").get_json()["data"]
        self.assertTrue(m["menu_library"]["url"].startswith(sl.URL_BASE + "music/mus_menu_"))

    def test_the_switches_still_switch(self):
        import engine
        import scene_audio
        saved = (engine.ACTION_FOLEY_ENABLED, engine.CONSEQUENCE_BED_ENABLED,
                 engine.SCENE_AMBIENCE_ENABLED)
        try:
            engine.ACTION_FOLEY_ENABLED = False
            engine.CONSEQUENCE_BED_ENABLED = False
            engine.SCENE_AMBIENCE_ENABLED = False
            self.assertIsNone(scene_audio.action_foley("Kick open the door"))
            self.assertIsNone(scene_audio.consequence_bed("The pipe bursts and water floods"))
            r = self.client.post("/api/action_foley", json={"action": "Kick the door"}).get_json()
            self.assertEqual(r["reason"], "off")
            rec = scene_audio.get_scene_audio(
                "Server racks with blinking lights fill a cold data center", session_id="x-off")
            self.assertTrue(sl.get(rec["sfx_id"]).get("generic"), rec["sfx_id"])
        finally:
            (engine.ACTION_FOLEY_ENABLED, engine.CONSEQUENCE_BED_ENABLED,
             engine.SCENE_AMBIENCE_ENABLED) = saved


class TestItShips(unittest.TestCase):
    def test_static_is_bundled_and_unguarded(self):
        import sys
        sys.path.insert(0, str(ROOT / "tools"))
        import ship_layout
        self.assertIn(("static", "static"), ship_layout.BUNDLE_TREES)
        self.assertIn("sound_library", ship_layout.RUNTIME_MODULES)
        import local_guard
        self.assertFalse(local_guard.is_guarded("/static/audio/library/catalog.json"))

    def test_git_does_not_ignore_the_files(self):
        try:
            p = subprocess.run(["git", "check-ignore", "static/audio/library/catalog.json",
                                "static/audio/library/foley/door_kick_1.mp3"],
                               cwd=ROOT, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            self.skipTest("no git")
        if p.returncode == 128:
            self.skipTest("not a git checkout")
        self.assertEqual(p.stdout.strip(), "", "the library must be committed, not ignored")


if __name__ == "__main__":
    unittest.main()
