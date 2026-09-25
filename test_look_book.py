"""The run's look book: what rides into a frame, where, and when it must not.

look_book.py shoots a contact sheet per run (world sheet: cast / conflicts /
sets) and a casting sheet with one plate per roster entry. The probe that
justified it (CHANGELOG, "The run gets a look book") also found the ways it
goes wrong, and each one is pinned here:

  * the sheet is DESIGN, not continuity — it rides after the previous frame,
    never ahead of it, and never displaces the player's own sheet;
  * on a flipbook turn the blank layout guide stays last, behind the sheet;
  * a nine-panel sheet is captioned as a sheet, or the model returns a grid;
  * a roster plate is captioned as a DESIGN of who arrives, not as a close-up
    the player "has just been looking at";
  * a plate is only attached for a roster entry the text actually names, never
    for a word that could describe the player ("journalist" inside
    "photojournalist"), and never for a word two entries share;
  * a book shot for another World is never served;
  * the fight remembers which roster entry it is through every rebuild of the
    brief, and draws from the book's roster rather than a private one.

Run with:
    python -m unittest test_look_book -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import engine  # noqa: E402
import encounter  # noqa: E402
import gemini_image_utils as giu  # noqa: E402
import look_book  # noqa: E402

SID = "_test_look_book"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

ROSTER = [
    "A Horizon security guard in black tactical gear and red goggles",
    "A desperate freelance photojournalist trying to steal the story first",
    "A desert coyote warped by the red biome",
    "A scavenger hauling scrap from a Horizon research outpost",
]


def _fake_book(key=None, status="ready"):
    d = look_book.book_dir(SID)
    for n in ("world_sheet_ref.jpg", "plate_01.jpg", "plate_02.jpg", "plate_03.jpg", "plate_04.jpg"):
        (d / n).write_bytes(PNG)
    book = {
        "world_key": key or look_book.world_key(),
        "status": status,
        "roster": ROSTER,
        "world_sheet": "world_sheet.png",
        "world_sheet_ref": "world_sheet_ref.jpg",
        "roster_looks": [
            {"kind": ROSTER[0], "look": "black PASGT helmet, twin red goggles",
             "terms": ["guard", "sentry"], "plate": "plate_01.jpg"},
            {"kind": ROSTER[1], "look": "khaki vest, camcorder",
             "terms": ["journalist", "photographer", "rival"], "plate": "plate_02.jpg"},
            {"kind": ROSTER[2], "look": "hairless blistered hide",
             "terms": ["coyote", "scavenger"], "plate": "plate_03.jpg"},
            {"kind": ROSTER[3], "look": "welding goggles, wheelbarrow",
             "terms": ["scavenger", "looter"], "plate": "plate_04.jpg"},
        ],
    }
    (d / look_book.BOOK_FILE).write_text(json.dumps(book), encoding="utf-8")
    return d


class _BookCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: shutil.rmtree(engine._get_session_root(SID), ignore_errors=True))
        self.d = _fake_book()
        # A photojournalist protagonist, so the player-word filter has teeth.
        self.player = mock.patch.object(
            look_book, "_player_words",
            return_value={"jason", "fleece", "freelance", "photojournalist", "press", "camera"})
        self.player.start()
        self.addCleanup(self.player.stop)
        for flag in ("LOOK_BOOK_ENABLED", "LOOK_BOOK_SHEET_ON_TURNS", "LOOK_BOOK_ROSTER_PLATES"):
            p = mock.patch.object(look_book, flag, True)
            p.start()
            self.addCleanup(p.stop)
        # Nothing here may start a real build.
        sp = mock.patch.object(look_book, "spawn", return_value=False)
        sp.start()
        self.addCleanup(sp.stop)


class WhatTheBookHandsOver(_BookCase):

    def test_a_named_roster_entry_brings_its_plate(self):
        plates = look_book.plates_named_in(SID, "A guard steps out from behind the truck.")
        self.assertEqual([os.path.basename(p) for p in plates], ["plate_01.jpg"])

    def test_plurals_count(self):
        plates = look_book.plates_named_in(SID, "Two sentries sweep the fence line.")
        self.assertEqual([os.path.basename(p) for p in plates], ["plate_01.jpg"])

    def test_a_word_inside_the_players_own_description_never_matches(self):
        """The protagonist is a photojournalist; "journalist" and "photographer"
        describe him doing his job, and would pull the rival's plate into a
        frame with nobody else in it."""
        for text in ("You raise your camera like any journalist would.",
                     "The photographer crouches by the wire."):
            self.assertEqual(look_book.plates_named_in(SID, text), [], text)
        # Its own distinct term still works.
        plates = look_book.plates_named_in(SID, "Your rival steps out, camcorder up.")
        self.assertEqual([os.path.basename(p) for p in plates], ["plate_02.jpg"])

    def test_a_word_two_entries_claim_is_not_acted_on(self):
        self.assertEqual(look_book.plates_named_in(SID, "A scavenger picks at the wreck."), [])
        self.assertEqual(
            [os.path.basename(p) for p in look_book.plates_named_in(SID, "A looter picks at the wreck.")],
            ["plate_04.jpg"])

    def test_a_compounds_everyday_noun_names_it(self):
        d = look_book.book_dir(SID)
        b = json.loads((d / look_book.BOOK_FILE).read_text())
        b["roster_looks"][2]["terms"] = ["jackrabbit", "hare"]
        (d / look_book.BOOK_FILE).write_text(json.dumps(b))
        plates = look_book.plates_named_in(SID, "The mutated rabbit lies still in the dust.")
        self.assertEqual([os.path.basename(p) for p in plates], ["plate_03.jpg"])

    def test_a_word_for_any_body_is_not_a_name(self):
        """"carcass" matched a different creature's plate over the jackrabbit
        the player had just killed, for three turns."""
        d = look_book.book_dir(SID)
        b = json.loads((d / look_book.BOOK_FILE).read_text())
        b["roster_looks"][2]["terms"] = ["carcass", "beast"]
        (d / look_book.BOOK_FILE).write_text(json.dumps(b))
        self.assertEqual(look_book.plates_named_in(SID, "You stand over the carcass."), [])

    def test_substrings_are_not_words(self):
        self.assertEqual(look_book.plates_named_in(SID, "The vanguard of the storm."), [])

    def test_the_earliest_mention_wins(self):
        plates = look_book.plates_named_in(SID, "A coyote circles while a guard watches.")
        self.assertEqual([os.path.basename(p) for p in plates], ["plate_03.jpg"])

    def test_a_book_for_another_world_is_never_served(self):
        _fake_book(key="not-this-world")
        self.assertIsNone(look_book.world_sheet(SID))
        self.assertEqual(look_book.plates_named_in(SID, "A guard steps out."), [])
        self.assertIsNone(look_book.plate_for(SID, ROSTER[0]))

    def test_an_unfinished_book_hands_over_nothing(self):
        _fake_book(status="shooting")
        self.assertIsNone(look_book.world_sheet(SID))
        self.assertEqual(look_book.plates_named_in(SID, "A guard steps out."), [])

    def test_the_switches_cut_their_own_part_only(self):
        with mock.patch.object(look_book, "LOOK_BOOK_SHEET_ON_TURNS", False):
            self.assertIsNone(look_book.world_sheet(SID))
            self.assertIsNotNone(look_book.plate_for(SID, ROSTER[0]))
        with mock.patch.object(look_book, "LOOK_BOOK_ROSTER_PLATES", False):
            self.assertIsNotNone(look_book.world_sheet(SID))
            self.assertIsNone(look_book.plate_for(SID, ROSTER[0]))
            self.assertEqual(look_book.plates_named_in(SID, "A guard steps out."), [])

    def test_labels_only_for_book_files(self):
        self.assertIn("CONTACT SHEET", look_book.label_for(self.d / "world_sheet_ref.jpg"))
        self.assertIn("ROSTER PLATE", look_book.label_for(self.d / "plate_01.jpg"))
        self.assertIn("security guard", look_book.label_for(self.d / "plate_01.jpg"))
        self.assertIsNone(look_book.label_for(ROOT / "prompts" / "flipbook_guide_2x2.png"))
        self.assertIsNone(look_book.label_for(engine._get_session_root(SID) / "images" / "plate_01.jpg"))

    def test_the_editor_can_only_fetch_book_files(self):
        self.assertIsNotNone(look_book.file_path(SID, "plate_01.jpg"))
        for bad in ("../state.json", "book.json", "plate_01.jpg/../../x", ""):
            self.assertIsNone(look_book.file_path(SID, bad), bad)


class WhereItRidesInTheRequest(_BookCase):
    """The real parts assembly in generate_gemini_img2img, captured on the wire."""

    def setUp(self):
        super().setUp()
        self.tmp = ROOT / "_test_look_book_payload"
        self.tmp.mkdir(exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.prev = self.tmp / "prev_frame.png"
        self.sheet = self.tmp / "character_sheet.png"
        self.guide = self.tmp / "flipbook_guide_2x2.png"
        for p in (self.prev, self.sheet, self.guide):
            p.write_bytes(PNG)
        self.book_sheet = str(self.d / "world_sheet_ref.jpg")
        self.plate = str(self.d / "plate_01.jpg")

    def _payload(self, refs, **kw):
        seen = {}

        def fake_post(url, headers=None, json=None, timeout=None, **_):
            seen["payload"] = json
            raise RuntimeError("stop — the payload is what we came for")

        with mock.patch.object(giu, "GEMINI_API_KEY", "test-key"), \
             mock.patch.object(giu.requests, "post", fake_post):
            giu.generate_gemini_img2img(
                prompt="A guard stands by the fence twenty feet ahead.",
                caption="scene", reference_image_path=refs, output_dir=self.tmp, **kw)
        parts = seen["payload"]["contents"][0]["parts"]
        labels = []
        for i, part in enumerate(parts):
            if "inlineData" in part:
                labels.append(parts[i - 1].get("text", "") if i else "")
        return parts, labels, parts[-1].get("text", "")

    def test_the_sheet_rides_last_and_is_captioned_as_a_sheet(self):
        _parts, labels, _ = self._payload([str(self.prev)], design_refs=[self.book_sheet])
        self.assertEqual(len(labels), 2)
        self.assertIn("CONTACT SHEET", labels[-1])
        self.assertIn("not a grid", labels[-1])

    def test_the_players_sheet_and_the_previous_frame_stay_ahead_of_it(self):
        _parts, labels, _ = self._payload(
            [str(self.prev)], identity_paths=[str(self.sheet)], design_refs=[self.book_sheet])
        self.assertEqual(len(labels), 3)
        self.assertNotIn("CONTACT SHEET", labels[0])
        self.assertNotIn("CONTACT SHEET", labels[1])
        self.assertIn("CONTACT SHEET", labels[2])

    def test_the_flipbook_layout_guide_stays_last(self):
        guide_label = "LAYOUT TEMPLATE — a blank 2x2 grid."
        _parts, labels, _ = self._payload(
            [str(self.prev), str(self.guide)], design_refs=[self.book_sheet],
            is_flipbook=True, flipbook_grid=(2, 2),
            reference_labels={str(self.guide): guide_label})
        self.assertEqual(labels[-1], guide_label)
        self.assertIn("CONTACT SHEET", labels[-2])

    def test_the_sheet_never_pushes_the_request_past_six(self):
        many = []
        for i in range(6):
            p = self.tmp / f"ctx_{i}.png"
            p.write_bytes(PNG)
            many.append(str(p))
        _parts, labels, _ = self._payload(many, design_refs=[self.book_sheet])
        self.assertEqual(len(labels), 6)
        self.assertIn("CONTACT SHEET", labels[-1])

    def test_a_roster_plate_is_a_design_not_a_close_up(self):
        _parts, labels, prompt = self._payload([str(self.prev)], cast_plates=[self.plate])
        self.assertTrue(any("ROSTER PLATE" in l for l in labels))
        self.assertFalse(any("CLOSE-UP OF A SUBJECT ALREADY IN THIS SCENE" in l for l in labels))
        self.assertIn("DESIGNED LOOK OF WHO ARRIVES", prompt)
        self.assertNotIn("THE SUBJECT FROM THE CLOSE-UP IS IN THIS SCENE", prompt)

    def test_no_book_no_change(self):
        _parts, labels, prompt = self._payload([str(self.prev)])
        self.assertEqual(len(labels), 1)
        self.assertNotIn("ROSTER PLATE", prompt)


class TheFightKnowsWhoItIs(_BookCase):

    def test_the_roster_kind_survives_the_brief_rebuild(self):
        brief = encounter.normalize_encounter_brief({
            "character": {"label": "Horizon guard", "look": "black helmet", "kind": "person"},
            "danger": "rifle up", "stakes": "you lose the film",
            "roster_kind": ROSTER[0],
        })
        self.assertEqual(brief.get("roster_kind"), ROSTER[0])
        again = encounter.normalize_encounter_brief(brief)
        self.assertEqual(again.get("roster_kind"), ROSTER[0])

    def test_the_rolled_entry_is_drawn_from_its_plate(self):
        plates = encounter._book_cast_plates(SID, {"roster_kind": ROSTER[0]})
        self.assertEqual([os.path.basename(p) for p in plates], ["plate_01.jpg"])
        self.assertEqual(encounter._book_cast_plates(SID, {}), [])

    def test_the_fight_rolls_from_the_books_roster(self):
        with mock.patch.object(engine, "_load_state", return_value={}), \
             mock.patch.object(engine, "_save_state"), \
             mock.patch.object(encounter, "build_encounter_roster",
                               side_effect=AssertionError("a private roster was built")):
            self.assertEqual(encounter.encounter_roster(SID), ROSTER)

    def test_a_designed_fighter_keeps_its_name_after_the_plate(self):
        """Vision reads a designed guard as "a figure in tactical gear"; the
        plate was drawn FROM the roster entry, so the name stays the entry's.
        First live run with the book: every designed fighter became "A figure"."""
        brief = encounter.normalize_encounter_brief({
            "character": {"label": "Horizon security guard", "look": "black helmet", "kind": "person"},
            "danger": "rifle up", "stakes": "you lose the film",
            "roster_kind": ROSTER[0], "roster_plate": "plate_01.jpg",
        })
        seen = "A figure in black tactical gear and red goggles raises a rifle at the photographer."
        encounter.adopt_plate_look(brief, seen)
        out = encounter.align_brief_to_plate(brief, {"description": seen})
        self.assertEqual(out["character"]["label"], "Horizon security guard")

    def test_a_clothing_scrap_is_not_kept_as_a_name(self):
        brief = encounter.normalize_encounter_brief({
            "character": {"label": "Charcoal-black nylon tactical rig over", "look": "rig", "kind": "person"},
            "danger": "rifle up", "stakes": "you lose the film",
            "roster_kind": ROSTER[0], "roster_plate": "plate_01.jpg",
        })
        self.assertFalse(encounter._label_names_roster_entry(brief))

    def test_without_a_plate_the_photograph_still_names_it(self):
        brief = encounter.normalize_encounter_brief({
            "character": {"label": "Horizon security guard", "look": "black helmet", "kind": "person"},
            "danger": "rifle up", "stakes": "you lose the film",
        })
        seen = "A rancher in a straw hat holds a shotgun at the photographer."
        out = encounter.align_brief_to_plate(brief, {"description": seen})
        self.assertNotEqual(out["character"]["label"], "Horizon security guard")

    def test_the_designed_look_beats_a_stock_stranger(self):
        """"camcorder" trips the camera-language net, which used to swap the
        designed activist for "a sentry in unmarked fatigues" while his plate
        drew the activist."""
        designed = ("Olive M-65 field coat with hand-stenciled red protest symbols, "
                    "woolen watch cap, carrying an analog camcorder wrapped in tape.")
        raw = json.dumps({"character": {"label": "Horizon protest activist", "look": designed,
                                        "kind": "person", "stance": "desperate"},
                          "motive": "the footage", "danger": "he swings the camcorder",
                          "stakes": "you lose the film", "place_hold": "fence"})
        with mock.patch.object(encounter, "roll_encounter_kind", return_value=ROSTER[1]), \
             mock.patch.object(look_book, "look_for", return_value=designed), \
             mock.patch.object(encounter, "encounter_lore_context", return_value=""), \
             mock.patch.object(engine, "_load_state", return_value={}), \
             mock.patch.object(engine, "_ask", return_value=raw):
            brief = encounter.build_encounter_brief(SID)
        self.assertEqual(brief.get("roster_kind"), ROSTER[1])
        self.assertIn("M-65", brief["character"]["look"])

    def test_api_begin_attaches_the_plate_only_without_a_sighting(self):
        src = (ROOT / "encounter.py").read_text(encoding="utf-8")
        self.assertIn("if not cast_plates:\n        cast_plates = _book_cast_plates(session_id, brief)", src)


class TheBookOutlivesTheSweep(unittest.TestCase):

    def test_the_disk_sweep_never_takes_the_book(self):
        """Every book file is the oldest thing in its session, so an
        oldest-first sweep took book.json first and the book rebuilt itself
        mid-run (seen live: twice in five turns)."""
        src = (ROOT / "engine.py").read_text(encoding="utf-8")
        start = src.index("def _ensure_disk_headroom")
        body = src[start:src.index("\ndef ", start + 10)]
        self.assertIn('if "look_book" in p.parts:', body)


class WithNoKeyNothingHappens(unittest.TestCase):

    def test_mock_mode_never_builds(self):
        with mock.patch.object(look_book, "_api_key", return_value=""):
            self.assertFalse(look_book.enabled())
            self.assertFalse(look_book.spawn(SID))

    def test_the_off_switch_is_off(self):
        with mock.patch.object(look_book, "LOOK_BOOK_ENABLED", False):
            self.assertFalse(look_book.enabled())


class TheEditorDoesNotLeakIntoTheBook(unittest.TestCase):
    """The live prompt file is shared with the editor, which binds whatever
    World the author opens. Reported: GENERATE on THE FIFTH CORNER while a
    SWAT run was loading painted the SWAT run over the desk, and the book
    kept rebuilding for Worlds nobody was playing."""

    def setUp(self):
        self.addCleanup(lambda: shutil.rmtree(engine._get_session_root(SID), ignore_errors=True))

    def test_reading_the_sheet_never_starts_a_build(self):
        """GENERATE draws through wf-<world>, which has no book; reading used to
        start a whole book for it on every GENERATE."""
        shutil.rmtree(engine._get_session_root(SID) / "look_book", ignore_errors=True)
        with mock.patch.object(look_book, "LOOK_BOOK_SHEET_ON_TURNS", True), \
                mock.patch.object(look_book, "enabled", return_value=True), \
                mock.patch.object(look_book, "spawn") as sp:
            self.assertIsNone(look_book.world_sheet(SID))
            sp.assert_not_called()

    def test_the_frame_sessions_never_get_a_book(self):
        with mock.patch.object(look_book, "enabled", return_value=True), \
                mock.patch.object(look_book.threading, "Thread") as th:
            self.assertFalse(look_book.spawn("wf-somewhere"))
            th.assert_not_called()

    def test_a_build_shoots_the_world_it_started_with(self):
        """The World is frozen at spawn and carried onto the sheet threads."""
        swat = {"setting_reference": {"name": "World"}, "player_character": {"name": "Ghost"}}
        fifth = {"setting_reference": {"name": "Horizon perimeter"}, "player_character": {"name": "Jason"}}
        seen = {}
        with mock.patch.object(look_book, "_live_prompts", return_value=fifth):
            look_book._SNAP.prompts = json.loads(json.dumps(swat))
            try:
                def on_thread():
                    seen["name"] = look_book._prompts()["setting_reference"]["name"]
                t = look_book.threading.Thread(target=look_book._carry(on_thread))
                t.start()
                t.join()
                self.assertEqual(look_book._prompts()["player_character"]["name"], "Ghost")
                self.assertEqual(look_book._world_name(look_book._prompts()), "World · Ghost")
            finally:
                look_book._SNAP.prompts = None
            self.assertEqual(seen["name"], "World")
            # Outside a build, the live file.
            self.assertEqual(look_book._prompts()["player_character"]["name"], "Jason")

    def test_a_book_finished_under_the_editor_is_still_ready(self):
        book = {"world_sheet": "world_sheet.png", "roster_looks": [], "timings": {}, "log": [],
                "started": 0}
        with mock.patch.object(look_book, "world_key", return_value="other"), \
                mock.patch.object(look_book, "_save"):
            look_book._finish(SID, book, "mine")
        self.assertEqual(book["status"], "ready")

    def test_the_desk_names_the_world(self):
        d = _fake_book()
        book = json.loads((d / look_book.BOOK_FILE).read_text(encoding="utf-8"))
        book["world_name"] = "World · Ghost"
        (d / look_book.BOOK_FILE).write_text(json.dumps(book), encoding="utf-8")
        self.assertEqual(look_book.summary(SID)["world_name"], "World · Ghost")


class TheEditorStopsTheCutscene(unittest.TestCase):
    """Client wiring, checked in the source (there is no browser here)."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "static" / "js" / "standalone.js").read_text(encoding="utf-8")
        cls.graph = (ROOT / "static" / "js" / "editor_graph.js").read_text(encoding="utf-8")
        cls.html = (ROOT / "templates" / "standalone.html").read_text(encoding="utf-8")

    def _body(self, src, head):
        start = src.index(head)
        return src[start:start + 6000]

    def test_opening_the_editor_takes_the_cutscene_down(self):
        body = self._body(self.js, "    async function open(opts) {")
        self.assertLess(body.index("pauseRun();"), body.index("Cutscene.holdForEditor()"))

    def test_a_montage_never_completes_under_the_editor(self):
        body = self._body(self.js, "    async function finish() {")
        self.assertLess(body.index("if (editorOpen()) { holdForEditor(); return; }"),
                        body.index('postJSON("/api/cutscene/complete"'))

    def test_closing_puts_the_held_cutscene_back(self):
        body = self._body(self.js, "    async function resumeRun(dest, opts) {")
        self.assertIn("Cutscene.takeHeld()", body)
        self.assertIn('if (held === "opening") booted = false;', body)
        self.assertIn("Cutscene.unstickGraph()", body)

    def test_the_look_book_is_reachable_from_the_experience_desk(self):
        self.assertIn('id="we-lookbook"', self.html)
        self.assertIn('getElementById("we-lookbook")', self.js)
        body = self._body(self.graph, "  function sheetWorldNode(n, body) {")
        self.assertIn('fillLookBook(group(body, "Look book"), n);', body)


class GenerateShootsTheBook(unittest.TestCase):
    """GENERATE starts the book for the World it binds; the run that closing
    the editor starts takes that book instead of rolling a second one."""

    def setUp(self):
        self.addCleanup(lambda: shutil.rmtree(engine._get_session_root(SID), ignore_errors=True))
        self.addCleanup(lambda: (look_book._PREBUILT.pop(SID, None), look_book._BUILDING.pop(look_book._slot(SID), None)))
        for name, val in (("enabled", True), ("world_key", "fifth")):
            p = mock.patch.object(look_book, name, return_value=val)
            p.start()
            self.addCleanup(p.stop)

    def test_generate_starts_the_book_for_the_run(self):
        with mock.patch.object(look_book, "spawn", return_value=True) as sp:
            self.assertTrue(look_book.prebuild_for_generate(SID))
        sp.assert_called_once_with(SID, reason="generate")
        self.assertEqual(look_book._PREBUILT[SID], "fifth")

    def test_generate_again_keeps_the_build_under_way(self):
        look_book._BUILDING[look_book._slot(SID)] = "fifth"
        with mock.patch.object(look_book, "spawn") as sp:
            self.assertTrue(look_book.prebuild_for_generate(SID))
        sp.assert_not_called()

    def test_the_frame_session_is_not_a_run(self):
        with mock.patch.object(look_book, "spawn") as sp:
            self.assertFalse(look_book.prebuild_for_generate("wf-somewhere"))
        sp.assert_not_called()

    def test_the_run_takes_the_book_generate_is_shooting(self):
        look_book._PREBUILT[SID] = "fifth"
        look_book._BUILDING[look_book._slot(SID)] = "fifth"
        with mock.patch.object(look_book, "spawn") as sp, \
                mock.patch.object(look_book.shutil, "rmtree") as rm:
            look_book.reset_for_new_run(SID)
        sp.assert_not_called()
        rm.assert_not_called()
        self.assertNotIn(SID, look_book._PREBUILT)  # claimed once

    def test_the_run_takes_the_finished_book(self):
        _fake_book(key="fifth")
        look_book._PREBUILT[SID] = "fifth"
        with mock.patch.object(look_book, "spawn") as sp:
            look_book.reset_for_new_run(SID)
        sp.assert_not_called()

    def test_the_next_run_rolls_its_own(self):
        """Claimed once: a later New Game gets a new roster, as before."""
        _fake_book(key="fifth")
        with mock.patch.object(look_book, "spawn") as sp:
            look_book.reset_for_new_run(SID)
        sp.assert_called_once_with(SID, reason="new run")

    def test_a_book_for_another_world_is_not_taken(self):
        _fake_book(key="swat")
        look_book._PREBUILT[SID] = "swat"
        with mock.patch.object(look_book, "spawn") as sp:
            look_book.reset_for_new_run(SID)
        sp.assert_called_once_with(SID, reason="new run")

    def test_a_superseded_build_stops_at_its_next_save(self):
        look_book._TICKET[look_book._slot(SID)] = 7
        look_book._SNAP.ticket = 6
        try:
            with self.assertRaises(look_book._Superseded):
                look_book._save(SID, {"world_key": "swat"})
        finally:
            look_book._SNAP.ticket = None

    def test_the_new_book_says_so_at_once(self):
        """The progress line reads the placeholder, not the old World's 5/5."""
        look_book._placeholder(SID, "fifth", "generate")
        book = look_book.load(SID)
        self.assertEqual((book["status"], book["reason"], book["log"][0]["stage"]),
                         ("roster", "generate", "start"))

    def test_generate_asks_for_the_book(self):
        src = (ROOT / "api.py").read_text(encoding="utf-8")
        start = src.index("def admin_studio_world_frames_reset")
        body = src[start:src.index("\n@app.route", start)]
        self.assertIn("look_book.prebuild_for_generate(_engine._resolve_request_session_id())", body)
        self.assertIn('"look_book": look_book_started', body)

    def test_the_editor_shows_it_being_made(self):
        js = (ROOT / "static" / "js" / "standalone.js").read_text(encoding="utf-8")
        html = (ROOT / "templates" / "standalone.html").read_text(encoding="utf-8")
        self.assertIn('id="we-lb-progress"', html)
        self.assertIn("{ id: wid, session_id: SESSION_ID }", js)
        self.assertIn("if (payload && payload.look_book) watchLookBook();", js)
        self.assertIn("LookBookStatus.peek()", js)


class EachWorldHasItsOwnBook(unittest.TestCase):
    """Reported: "im not seeing the look books change when i navigate between
    worlds". There was one book per run; now there is one per World in it."""

    def setUp(self):
        self.addCleanup(lambda: shutil.rmtree(engine._get_session_root(SID), ignore_errors=True))
        self.world = "somewhere"
        p = mock.patch.object(look_book, "world_slug", side_effect=lambda *a, **k: self.world)
        p.start()
        self.addCleanup(p.stop)

    def test_navigating_shows_each_worlds_book(self):
        _fake_book()
        self.assertEqual(len(look_book.load(SID)["roster"]), len(ROSTER))
        self.world = "world"
        self.assertEqual(look_book.load(SID), {})
        self.world = "somewhere"
        self.assertEqual(len(look_book.load(SID)["roster"]), len(ROSTER))

    def test_a_new_run_clears_only_its_own_world(self):
        _fake_book()
        self.world = "world"
        _fake_book()
        with mock.patch.object(look_book, "spawn"):
            look_book.reset_for_new_run(SID)
        self.assertEqual(look_book.load(SID), {})
        self.world = "somewhere"
        self.assertTrue(look_book.load(SID))

    def test_building_one_world_does_not_block_another(self):
        look_book._BUILDING[look_book._slot(SID)] = look_book.world_key()
        self.addCleanup(lambda: look_book._BUILDING.pop(look_book._slot(SID, "somewhere"), None))
        self.world = "world"
        self.assertFalse(look_book.building(SID))

    def test_the_files_carry_their_world(self):
        _fake_book()
        s = look_book.summary(SID)
        self.assertEqual(s["world"], "somewhere")
        self.assertTrue(all("&w=somewhere" in r["plate"] for r in s["roster"] if r.get("plate")))
        self.assertIsNotNone(look_book.file_path(SID, "plate_01.jpg", "somewhere"))
        self.assertIsNone(look_book.file_path(SID, "plate_01.jpg", "../x"))
        self.assertIsNone(look_book.file_path(SID, "plate_01.jpg", "world"))

    def test_a_shelf_file_is_still_labelled(self):
        path = look_book.book_dir(SID) / "world_sheet_ref.jpg"
        self.assertEqual(look_book.label_for(path), look_book.SHEET_LABEL)

    def test_the_editor_follows_the_world(self):
        js = (ROOT / "static" / "js" / "standalone.js").read_text(encoding="utf-8")
        start = js.index("    async function enterWorld(worldId) {")
        self.assertIn("LookBookStatus.peek()", js[start:start + 3000])
        self.assertIn("NO LOOK BOOK FOR THIS WORLD YET", js)


class TheLevelWaitsForItsBook(unittest.TestCase):
    """Asked: "make sure the look book generation is completed BEFORE starting
    the level. Even if it adds to the wait times." The client prepares the
    book and holds the opening black until it is done; the reset claims it;
    api_reset waits for it for any caller that skipped the prepare; a World
    arrived in mid-run gets its book and its first frame waits for it."""

    def setUp(self):
        self.addCleanup(lambda: shutil.rmtree(engine._get_session_root(SID), ignore_errors=True))
        self.addCleanup(lambda: (look_book._PREBUILT.pop(SID, None),
                                 look_book._BUILDING.pop(look_book._slot(SID), None)))
        for name, val in (("enabled", True), ("world_key", "fifth")):
            p = mock.patch.object(look_book, name, return_value=val)
            p.start()
            self.addCleanup(p.stop)

    def test_prepare_starts_the_runs_book_once(self):
        with mock.patch.object(look_book, "spawn", return_value=True) as sp:
            self.assertTrue(look_book.prepare_for_level(SID))
            look_book._BUILDING[look_book._slot(SID)] = "fifth"
            self.assertTrue(look_book.prepare_for_level(SID))   # the client retrying
        sp.assert_called_once_with(SID, reason="new run")
        with mock.patch.object(look_book, "spawn") as sp2:
            look_book.reset_for_new_run(SID)                    # ...and the reset claims it
        sp2.assert_not_called()

    def test_a_book_generate_is_shooting_is_the_one_it_waits_for(self):
        look_book._PREBUILT[SID] = "fifth"
        look_book._BUILDING[look_book._slot(SID)] = "fifth"
        with mock.patch.object(look_book, "spawn") as sp:
            self.assertTrue(look_book.prepare_for_level(SID))
        sp.assert_not_called()

    def test_a_new_run_after_that_gets_a_new_book(self):
        _fake_book(key="fifth")
        with mock.patch.object(look_book, "spawn", return_value=True) as sp:
            look_book.prepare_for_level(SID)
        sp.assert_called_once_with(SID, reason="new run")

    def test_the_wait_ends_when_the_build_does(self):
        _fake_book(key="fifth")
        self.assertEqual(look_book.wait_ready(SID, timeout=1), "ready")
        look_book._BUILDING[look_book._slot(SID)] = "fifth"
        self.assertEqual(look_book.wait_ready(SID, timeout=0.6), "timeout")
        with mock.patch.object(look_book, "enabled", return_value=False):
            self.assertEqual(look_book.wait_ready(SID, timeout=5), "off")

    def test_an_arrival_gets_its_worlds_book(self):
        with mock.patch.object(look_book, "spawn", return_value=True) as sp:
            look_book.ensure_for_level(SID)
        sp.assert_called_once_with(SID, reason="arrival")
        _fake_book(key="fifth")
        with mock.patch.object(look_book, "spawn") as sp2:
            look_book.ensure_for_level(SID)
        sp2.assert_not_called()

    def test_the_server_waits_outside_the_lock(self):
        src = (ROOT / "engine.py").read_text(encoding="utf-8")
        start = src.index("def prepare_level_look_book")
        body = src[start:src.index("\ndef ", start + 10)]
        self.assertIn("    with TURN_LOCK:\n        apply_experience_start({}, session_id, world_id=start_world_id)", body)
        self.assertIn("\n    if wait:\n        look_book.wait_ready(session_id)", body)
        reset = src[src.index("def api_reset"):]
        self.assertLess(reset.index("prepare_level_look_book(SID, start_world_id, wait=True)"),
                        reset.index("_perform_game_reset(start_world_id)"))
        done = src[src.index("def api_cutscene_complete"):]
        self.assertLess(done.index("look_book.wait_ready(sid)"), done.index("with WORLD_STATE_LOCK:"))
        self.assertIn("_look_book_for_arrival(session_id)", src)

    def test_the_client_holds_the_opening_until_the_book_is_done(self):
        js = (ROOT / "static" / "js" / "standalone.js").read_text(encoding="utf-8")
        html = (ROOT / "templates" / "standalone.html").read_text(encoding="utf-8")
        api = (ROOT / "api.py").read_text(encoding="utf-8")
        self.assertIn("@app.route('/api/look_book/prepare', methods=['POST'])", api)
        self.assertIn('id="opening-lookbook"', html)
        reset = js[js.index("  async function resetGame(opts) {"):]
        self.assertLess(reset.index("await LookBookStatus.gate(startWorldId);"),
                        reset.index('postJSON("/api/reset"'))
        self.assertIn("OpeningFade.extend()", js)

    def test_a_stale_montage_cannot_start_the_level(self):
        """The saved session's unfinished opening replays at launch. It kept
        playing through the wait, lifted the black with its first shot and
        completed onto the old run."""
        js = (ROOT / "static" / "js" / "standalone.js").read_text(encoding="utf-8")
        reset = js[js.index("  async function resetGame(opts) {"):]
        self.assertLess(reset.index("Cutscene.abandon()"), reset.index("await LookBookStatus.gate(startWorldId);"))
        self.assertIn('if (!pushed && opts.graph && seq === pushedAt) await unstickGraph();', js)
        ready = js[js.index("    function ready(reason) {"):][:600]
        self.assertIn('classList.contains("lookbook-gate") && reason !== "timeout"', ready)


class ABuildEndsInsideItsBudget(unittest.TestCase):
    """The first Fifth Corner harness run sat on "shooting the world sheet and
    the roster sheet" past five minutes: two sheet calls on a 300s socket
    timeout, a retry behind each, so the level's wait ran out first."""

    def test_no_image_call_can_outlast_the_levels_wait(self):
        self.assertLessEqual(look_book.SHEET_TIMEOUT_S, 150)
        self.assertLessEqual(look_book.PLATE_TIMEOUT_S, 90)
        self.assertLess(look_book.LOOK_BOOK_BUDGET_S, look_book.LOOK_BOOK_WAIT_S)
        src = (ROOT / "look_book.py").read_text(encoding="utf-8")
        self.assertNotIn("timeout=300", src)
        self.assertNotIn("timeout=180", src)

    def test_a_call_is_cut_to_what_the_build_has_left(self):
        import time as _t
        book = {"started": _t.time() - (look_book.LOOK_BOOK_BUDGET_S - 40)}
        self.assertLessEqual(look_book._cut(book, look_book.SHEET_TIMEOUT_S), 40)
        self.assertEqual(look_book._cut({"started": _t.time()}, look_book.SHEET_TIMEOUT_S),
                         look_book.SHEET_TIMEOUT_S)

    def test_no_second_ask_once_the_budget_is_spent(self):
        import time as _t
        calls = []
        book = {"started": _t.time() - look_book.LOOK_BOOK_BUDGET_S}
        got = look_book._shoot(SID, book, "world", lambda s, b: calls.append(1) or None)
        self.assertIsNone(got)
        self.assertEqual(len(calls), 1)

    def test_a_hung_call_is_abandoned_on_the_wall_clock(self):
        """requests' timeout is per socket read; a trickling answer never trips it."""
        import time as _t
        import requests

        def hang(*a, **k):
            _t.sleep(30)

        with mock.patch.object(requests, "post", side_effect=hang), \
                mock.patch.object(look_book, "_api_key", return_value="x"):
            t0 = _t.time()
            with self.assertRaises(RuntimeError):
                look_book._post("m", [{"text": "x"}], {}, timeout=1, operation="t", service="image")
            self.assertLess(_t.time() - t0, 10)


class TheWorldsTreasures(unittest.TestCase):
    """Six objects of extreme value, designed beside the cast and the sets and
    shot on their own sheet — the table goal rewards are drawn from.

    They live in the look book rather than being invented per goal because a
    reward is only worth crossing a level for if it is SCARCE and belongs to
    this world. The inventory this replaces was twelve hardcoded nouns with
    emoji, keyword-matched out of the narrator's prose: the one system in a
    game that generates every pixel that had no picture in it."""

    def setUp(self):
        import shutil as _sh
        self.tmp = ROOT / "_test_treasures"
        self.tmp.mkdir(exist_ok=True)
        self.addCleanup(lambda: _sh.rmtree(self.tmp, ignore_errors=True))

    def test_the_brief_is_asked_for_them(self):
        src = (ROOT / "look_book.py").read_text(encoding="utf-8")
        self.assertIn("NINE PIECES OF GEAR", src)
        # ...six treasures for the goals, three spoils for the fights.
        self.assertIn("SIX TREASURES", src)
        self.assertIn("THREE SPOILS", src)
        self.assertIn('"tier":"treasure|spoil"', src)
        # ...gear, with a spread: something to fight with, something that keeps
        # them alive, something that changes what they can do.
        for kind in ("weapon", "armor", "upgrade", "tool", "relic"):
            self.assertIn(kind, src)
        self.assertIn("they must cover at least one of each of the first three kinds",
                      src)
        self.assertIn("small enough for one person to carry out in their hands", src)
        self.assertIn('"kind":"weapon|armor|upgrade|tool|relic"', src)
        self.assertIn('"power":"..."', src)

    def test_a_row_without_a_name_or_a_picture_prompt_is_dropped(self):
        rows = look_book._clean_items([
            {"name": "Tidal Anchor", "look": "a barnacled iron weight",
             "use": "holds a door against the pull", "worth": "it stops the flood"},
            {"name": "tidal anchor", "look": "a duplicate"},   # same name
            {"name": "No Look"},                               # nothing to shoot
            {"look": "no name"},
        ])
        self.assertEqual([r["name"] for r in rows], ["Tidal Anchor"])
        self.assertEqual(rows[0]["use"], "holds a door against the pull")

    def test_the_table_is_capped_and_survives_junk(self):
        many = [{"name": f"Thing {i}", "look": "x"} for i in range(20)]
        self.assertEqual(len(look_book._clean_items(many)), look_book.ITEM_COUNT)
        self.assertEqual(look_book._clean_items(None), [])
        self.assertEqual(look_book._clean_items("nonsense"), [])

    def test_the_props_sheet_shoots_objects_not_scenes(self):
        book = {"items": [{"name": "Tidal Anchor", "look": "a barnacled iron weight"},
                          {"name": "Wet Ledger", "look": "a swollen book of names"}],
                "look_rules": {"palette": {"rust": "#7a3b1d"}}, "started": 0}
        seen = {}

        def fake_post(model, parts, cfg, timeout, operation, service):
            seen["op"] = operation
            seen["text"] = parts[-1]["text"]
            return {}

        with mock.patch.object(look_book, "_post", fake_post), \
             mock.patch.object(look_book, "_image_of", lambda d: b"PNG"):
            data = look_book._shoot_item_sheet("sid", book)
        self.assertEqual(data, b"PNG")
        self.assertEqual(seen["op"], "look_book_item_sheet")
        t = seen["text"]
        self.assertIn("PROPS sheet", t)
        self.assertIn("Tidal Anchor", t)
        self.assertIn("a barnacled iron weight", t)
        # one object, whole, alone, as cut-out item art on a flat key — so
        # the pack shows the THING and not a stock photo of a table
        self.assertIn("ONE object per frame, ALONE and WHOLE", t)
        self.assertIn("cut-out item art", t)
        self.assertIn("chroma-key colour", t)
        self.assertIn("No floor, no table, no surface", t)
        self.assertIn("No hand, no person, no second object", t)
        self.assertIn("Leave any frame beyond 2 solid black.", t)
        self.assertEqual(book["item_key"], "green")

    def test_the_sheet_is_cut_into_one_plate_per_treasure(self):
        from PIL import Image
        d = Path(self.tmp)
        sheet = Image.new("RGB", (800, 600), (0, 0, 0))
        for i, col in enumerate([(90, 70, 60), (60, 70, 90),
                                 (120, 110, 90), (80, 80, 80)]):
            x, y = (i % 2) * 404, (i // 2) * 304
            frame = Image.new("RGB", (392, 292), (0, 255, 0))
            frame.paste(Image.new("RGB", (160, 120), col), (116, 86))
            sheet.paste(frame, (x + 6, y + 6))
        sheet.save(d / "item_sheet.png")
        book = {"items": [{"name": "Tidal Anchor", "look": "a weight"},
                          {"name": "Wet Ledger", "look": "a book"}], "started": 0}
        with mock.patch.object(look_book, "book_dir", lambda sid, slug=None: d), \
             mock.patch.object(look_book, "_place_crops",
                               lambda dd, cells, its, **kw: {0: 0, 1: 1}):
            look_book._stage_item_sheet("sid", book, (d / "item_sheet.png").read_bytes())
        self.assertEqual(book["items"][0]["plate"], "item_01.png")
        self.assertEqual(book["items"][1]["plate"], "item_02.png")
        self.assertEqual(book["items"][0]["ref"], "item_01_ref.jpg")
        im = Image.open(d / "item_01.png")
        self.assertEqual(im.mode, "RGBA")
        # the key is gone and the object is not
        self.assertEqual(im.getpixel((0, 0))[3], 0)
        self.assertEqual(im.getpixel((im.width // 2, im.height // 2))[3], 255)
        self.assertTrue((d / "item_01_ref.jpg").exists())

    def test_a_plate_on_the_wrong_item_is_worse_than_none(self):
        # The crops are checked by eye, the same way the cast's are — a live
        # roster sheet once put a hazmat suit on the activist.
        src = (ROOT / "look_book.py").read_text(encoding="utf-8")
        stage = src.split("def _stage_item_sheet", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("_place_crops(", stage)
        self.assertIn('what="props"', stage)
        self.assertIn('key="name"', stage)

    def test_no_sheet_leaves_the_words_and_loses_the_pictures(self):
        book = {"items": [{"name": "Tidal Anchor", "look": "a weight"}], "started": 0}
        with mock.patch.object(look_book, "book_dir", lambda sid, slug=None: Path(self.tmp)):
            look_book._stage_item_sheet("sid", book, b"")
        self.assertNotIn("plate", book["items"][0])

    def test_the_treasures_ride_the_batch_that_is_already_parallel(self):
        # One image call, no extra wall clock on a loading screen the run is
        # already waiting through.
        src = (ROOT / "look_book.py").read_text(encoding="utf-8")
        build = src.split("def _build(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('jobs.append(("items", _shoot_item_sheet))', build)
        self.assertIn("_stage_item_sheet(session_id, book, results.get(\"items\")", build)

    def test_items_are_served_with_their_pictures_or_not_at_all(self):
        book = {"status": "ready",
                "items": [{"name": "Tidal Anchor", "look": "a weight",
                           "use": "holds a door", "plate": "item_01.png",
                           "ref": "item_01_ref.jpg"}]}
        with mock.patch.object(look_book, "current", lambda sid, **k: book), \
             mock.patch.object(look_book, "_file", lambda sid, n: f"/b/{n}" if n else None):
            rows = look_book.items("sid")
        self.assertEqual(rows[0]["plate"], "/b/item_01.png")
        self.assertEqual(rows[0]["ref"], "/b/item_01_ref.jpg")
        self.assertEqual(rows[0]["tier"], "treasure")   # an older book: all treasure
        self.assertEqual(rows[0]["use"], "holds a door")
        # A book that is not ready hands out nothing; the caller falls back.
        with mock.patch.object(look_book, "current", lambda sid, **k: {"status": "building"}):
            self.assertEqual(look_book.items("sid"), [])


if __name__ == "__main__":
    unittest.main()


class GearHasAKindAndAPower(unittest.TestCase):
    """Loot the player recognises: what it is for, in the language a game
    already uses. A run that hands out three relics has given them nothing to
    do with them, so the brief insists on a weapon, armour and an upgrade."""

    def test_the_kind_is_one_of_the_five_and_junk_becomes_a_relic(self):
        rows = look_book._clean_items([
            {"name": "Sump Cutter", "kind": "WEAPON", "look": "a shear",
             "power": "Cuts a sealed door in one pass"},
            {"name": "Odd One", "kind": "not-a-kind", "look": "a thing"},
            {"name": "Plain One", "look": "a thing"},
        ])
        self.assertEqual(rows[0]["kind"], "weapon")     # normalised
        self.assertEqual(rows[0]["power"], "Cuts a sealed door in one pass")
        self.assertEqual(rows[1]["kind"], "relic")      # unknown
        self.assertEqual(rows[2]["kind"], "relic")      # absent
        self.assertEqual(look_book.ITEM_KINDS,
                         ("weapon", "armor", "upgrade", "tool", "relic"))

    def test_the_props_sheet_is_told_what_each_thing_is(self):
        book = {"items": [{"name": "Sump Cutter", "kind": "weapon", "look": "a shear"}],
                "look_rules": {}, "started": 0}
        seen = {}
        with mock.patch.object(look_book, "_post",
                               lambda m, p, c, timeout, operation, service:
                                   (seen.update(t=p[-1]["text"]), {})[1]), \
             mock.patch.object(look_book, "_image_of", lambda d: b"PNG"):
            look_book._shoot_item_sheet("sid", book)
        self.assertIn("Sump Cutter (weapon)", seen["t"])

    def test_a_row_with_no_kind_does_not_break_the_sheet(self):
        # A book built before gear existed still has to shoot.
        book = {"items": [{"name": "Old Thing", "look": "a thing"}],
                "look_rules": {}, "started": 0}
        seen = {}
        with mock.patch.object(look_book, "_post",
                               lambda m, p, c, timeout, operation, service:
                                   (seen.update(t=p[-1]["text"]), {})[1]), \
             mock.patch.object(look_book, "_image_of", lambda d: b"PNG"):
            look_book._shoot_item_sheet("sid", book)
        self.assertIn("Old Thing", seen["t"])
        self.assertNotIn("(None)", seen["t"])


def _key_frame(key=(0, 255, 0), W=680, H=510):
    """A props frame the way the sheet model paints one: a flat key with
    grain, a soft shadow under the object, and an object with a dark strap,
    an olive panel and a hot highlight — the parts a naive key eats."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter
    rng = np.random.default_rng(7)
    sh = Image.new("L", (W, H), 0)
    ImageDraw.Draw(sh).ellipse((200, 380, 500, 430), fill=150)
    sh = sh.filter(ImageFilter.GaussianBlur(18))
    base = np.full((H, W, 3), key, float) * (1 - np.asarray(sh)[..., None] / 255 * 0.7)
    big = Image.fromarray(base.clip(0, 255).astype(np.uint8)).resize((W * 4, H * 4))
    d = ImageDraw.Draw(big)
    d.rounded_rectangle((880, 480, 1840, 1440), 160, fill=(120, 118, 112))
    d.rectangle((1000, 600, 1200, 1320), fill=(40, 38, 30))
    d.rectangle((1320, 800, 1720, 960), fill=(110, 115, 60))
    d.ellipse((1520, 560, 1680, 680), fill=(250, 250, 245))
    im = big.resize((W, H), Image.LANCZOS)
    a = np.asarray(im).astype(float) + rng.normal(0, 6, (H, W, 3))
    return Image.fromarray(a.clip(0, 255).astype(np.uint8))


class GearIsCutOut(unittest.TestCase):
    """The pack shows the THING — item art, the way a game shows loot — not a
    photograph of a table with a thing on it: "they need to be with a
    transparent background so it doesn't seem like a random ai stock photo.
    that look is awful" (Matt, 2026-09-22). The props sheet is shot on a flat
    key and each plate keyed off it."""

    def test_the_key_goes_and_the_object_stays(self):
        import numpy as np
        for key in ((0, 255, 0), (255, 0, 255), (40, 200, 60)):
            cut = look_book._cut_out(_key_frame(key))
            self.assertIsNotNone(cut, key)
            a = np.asarray(cut)
            self.assertEqual(cut.mode, "RGBA")
            h, w = a.shape[:2]
            self.assertEqual(a[0, 0, 3], 0)                  # the key
            self.assertEqual(a[h - 1, w - 1, 3], 0)
            self.assertEqual(a[h // 2, w // 2, 3], 255)      # the object
            # framed like inventory art: square, object centred with air round it
            self.assertEqual(h, w)

    def test_a_shadow_on_the_key_goes_with_the_key(self):
        import numpy as np
        cut = look_book._cut_out(_key_frame())
        a = np.asarray(cut)
        # below the object is where the shadow was: nothing opaque is left there
        h, w = a.shape[:2]
        self.assertLess(int(a[h - 3, :, 3].max()), 40)

    def test_dark_and_olive_parts_of_the_object_are_not_keyed(self):
        import numpy as np
        a = np.asarray(look_book._cut_out(_key_frame()))
        rgb = a[..., :3].astype(int)
        solid = a[..., 3] > 250
        dark = solid & (rgb.sum(-1) < 150)
        olive = solid & (abs(rgb[..., 0] - 110) < 20) & (abs(rgb[..., 1] - 115) < 20) \
            & (rgb[..., 2] < 90)
        self.assertGreater(int(dark.sum()), 500)
        self.assertGreater(int(olive.sum()), 500)

    def test_no_green_fringe(self):
        import numpy as np
        a = np.asarray(look_book._cut_out(_key_frame()))
        rgb = a[..., :3].astype(int)
        edge = (a[..., 3] > 10) & (a[..., 3] < 245)
        spill = rgb[..., 1] - np.maximum(rgb[..., 0], rgb[..., 2])
        self.assertLessEqual(int(spill[edge].max()), 6)

    def test_a_frame_not_on_a_key_is_refused_not_faked(self):
        import numpy as np
        from PIL import Image
        self.assertIsNone(look_book._cut_out(Image.new("RGB", (300, 200), (245, 245, 240))))
        room = Image.fromarray((np.random.default_rng(3).random((200, 300, 3)) * 255)
                               .astype(np.uint8))
        self.assertIsNone(look_book._cut_out(room))
        # all key, no object: nothing to show
        self.assertIsNone(look_book._cut_out(Image.new("RGB", (300, 200), (0, 255, 0))))

    def test_a_thing_run_off_the_frame_fades_out_rather_than_ending_in_a_cut(self):
        import numpy as np
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (400, 300), (255, 0, 255))
        d = ImageDraw.Draw(im)
        d.rectangle((150, 60, 250, 240), fill=(120, 110, 90))     # the thing
        d.rectangle((190, 0, 210, 60), fill=(30, 30, 30))         # its strap, off the top
        a = np.asarray(look_book._cut_out(im, feather=0))[..., 3]
        col = a[:, a.shape[1] // 2]
        top = int(np.nonzero(col)[0].min())
        # the strap is there, and it comes up from nothing, not from a hard edge
        self.assertLess(int(col[top]), 128)
        self.assertEqual(int(col[a.shape[0] // 2]), 255)

    def test_the_key_seen_through_clear_plastic_goes_but_red_stays_red(self):
        import numpy as np
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (400, 300), (255, 0, 255))
        d = ImageDraw.Draw(im)
        d.ellipse((100, 50, 300, 250), fill=(215, 175, 215))    # clear plastic over magenta
        d.ellipse((180, 130, 220, 170), fill=(190, 25, 25))     # a red wax seal
        a = np.asarray(look_book._cut_out(im, feather=0)).astype(int)
        h, w = a.shape[:2]
        solid = a[..., 3] > 250
        rgb = a[..., :3]
        pink = solid & ((np.minimum(rgb[..., 0], rgb[..., 2]) - rgb[..., 1]) > 12)
        self.assertEqual(int(pink.sum()), 0)
        seal = rgb[h // 2, w // 2]
        self.assertGreater(int(seal[0]), 150)
        self.assertLess(int(seal[1]), 60)

    def test_the_ref_is_flat_on_grey(self):
        cut = look_book._cut_out(_key_frame())
        ref = look_book._ref_of(cut)
        self.assertEqual(ref.mode, "RGB")
        self.assertEqual(ref.getpixel((0, 0)), (118, 118, 118))

    def test_a_green_world_is_shot_on_magenta(self):
        self.assertEqual(look_book._key_colour({"look_rules": {"palette": {
            "rust": "#7a3b1d", "bone": "#e8e6df"}}}), "green")
        self.assertEqual(look_book._key_colour({"look_rules": {"palette": {
            "sump": "#2f8a3a", "fog": "#6fbf5a"}}}), "magenta")
        self.assertEqual(look_book._key_colour({"items": [
            {"look": "an emerald glass visor"}]}), "magenta")

    def test_a_frame_that_will_not_cut_gets_no_plate(self):
        from PIL import Image
        d = Path(ROOT / "_test_cutfail")
        d.mkdir(exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        sheet = Image.new("RGB", (400, 300), (0, 0, 0))
        sheet.paste(Image.new("RGB", (388, 288), (240, 240, 235)), (6, 6))
        sheet.save(d / "item_sheet.png")
        book = {"items": [{"name": "Tidal Anchor", "look": "a weight"}], "started": 0}
        with mock.patch.object(look_book, "book_dir", lambda sid, slug=None: d), \
             mock.patch.object(look_book, "_place_crops",
                               lambda dd, cells, its, **kw: {0: 0}):
            look_book._stage_item_sheet("sid", book, (d / "item_sheet.png").read_bytes())
        self.assertNotIn("plate", book["items"][0])
        self.assertIn("no clean cut-out for Tidal Anchor",
                      " ".join(str(x) for x in book.get("log") or []))


class TheGearHasTiers(unittest.TestCase):
    """Six treasures for the goals and three spoils for the fights: winning an
    encounter pays out, and it pays out in this world's own gear."""

    def test_nine_by_default_six_of_them_treasures(self):
        self.assertEqual(look_book.ITEM_COUNT, 9)
        self.assertEqual(look_book.TREASURE_COUNT, 6)

    def test_the_tier_is_kept_and_a_missing_one_goes_by_position(self):
        rows = look_book._clean_items(
            [{"name": f"Thing {i}", "look": "x"} for i in range(8)]
            + [{"name": "Dropped Shiv", "look": "a shiv", "tier": "SPOIL"}])
        self.assertEqual([r["tier"] for r in rows],
                         ["treasure"] * 6 + ["spoil"] * 3)
        rows = look_book._clean_items([{"name": "A", "look": "x", "tier": "spoil"},
                                       {"name": "B", "look": "x", "tier": "junk"}])
        self.assertEqual([r["tier"] for r in rows], ["spoil", "treasure"])


class ThePlateIsServed(unittest.TestCase):
    """A plate on disk becomes the route the client can fetch — the old
    ?path=C:\\... URL went to a route that never existed, and every find card
    showed a broken image."""

    def test_a_book_path_becomes_the_file_route(self):
        p = ROOT / "sessions" / "run-1" / "look_book" / "the-world" / "item_02.png"
        url = look_book.plate_url(str(p))
        self.assertTrue(url.startswith("/api/look_book/run-1/file/item_02.png?w=the-world"), url)
        self.assertEqual(look_book.plate_url(""), "")
        self.assertEqual(look_book.plate_url("/tmp/elsewhere/item_02.png"), "")

    def test_the_route_serves_png_plates(self):
        d = look_book.book_dir(SID, "plates-test")
        (d / "item_03.png").write_bytes(PNG)
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        self.assertIsNotNone(look_book.file_path(SID, "item_03.png", "plates-test"))
