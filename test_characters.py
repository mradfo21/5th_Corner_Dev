"""Characters (characters.py): the player as a thing the game owns.

    python -m unittest test_characters -v

Offline throughout — SOMEWHERE_CHARACTERS_OFFLINE makes every render a
stand-in, so this never spends a key even on a machine that has one, and the
authoring sandbox points CHARACTERS_DIR at an empty folder, so nothing here
lands on the player's roster.
"""
import json
import os
import shutil
import unittest
from pathlib import Path

os.environ["SOMEWHERE_CHARACTERS_OFFLINE"] = "1"

import authoring_sandbox  # noqa: E402

authoring_sandbox.guard()

import characters  # noqa: E402
import game_identity  # noqa: E402


def _fresh_dir():
    d = Path(characters.CHARACTERS_DIR)
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)
    characters._CACHE.clear()
    characters._RUN_BIND.clear()


def _make(concept="A salvage diver in her forties, patched olive canvas suit.", **kw):
    return characters.create(concept, wait=True, **kw)


class ATestNeverTouchesTheRoster(unittest.TestCase):
    def test_the_store_is_sandboxed(self):
        root = Path(authoring_sandbox.engaged_at() or "")
        self.assertTrue(str(characters.CHARACTERS_DIR).startswith(str(root)),
                        f"{characters.CHARACTERS_DIR} is not inside the sandbox {root}")
        self.assertNotEqual(Path(characters.CHARACTERS_DIR), characters.ROOT / "characters")


class ACharacterIsMadeFromOneLine(unittest.TestCase):
    def setUp(self):
        _fresh_dir()

    def test_create_draws_every_file_the_screens_and_the_sim_need(self):
        rec = _make()
        self.assertEqual(rec["status"], characters.STATUS_READY, rec.get("error"))
        look = rec["current_look"]
        self.assertTrue(look and look == rec["base_look"])
        d = characters.look_dir(rec["id"], look)
        for f in ("turnaround_key.png", "turnaround.png", "turnaround_ref.jpg",
                  "idle.png", "thumb.png", "face_ref.jpg", "look.json"):
            self.assertTrue((d / f).is_file(), f)
        self.assertTrue(rec["name"])

    def test_it_is_on_disk_and_on_the_roster(self):
        rec = _make()
        again = characters.load(rec["id"])
        self.assertEqual(again["id"], rec["id"])
        self.assertIn(rec["id"], [r["id"] for r in characters.list_all()])

    def test_the_card_carries_urls_not_paths(self):
        c = characters.card(_make(), full=True)
        self.assertTrue(c["idle"].startswith("/api/characters/"))
        self.assertTrue(c["thumb"].startswith("/api/characters/"))
        self.assertNotIn(str(characters.CHARACTERS_DIR), json.dumps(c))

    def test_pictures_are_kept_as_sources(self):
        import base64, io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (200, 10, 10)).save(buf, format="PNG")
        url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        rec = _make("the person in the picture", pictures=[url])
        self.assertEqual(rec["sources"], ["source_1.png"])
        self.assertIsNotNone(characters.file_path(rec["id"], "sources/source_1.png"))

    def test_nothing_else_leaves_by_the_file_door(self):
        rec = _make()
        for bad in ("character.json", "../x", "looks/../character.json", "looks/a/b/c.png"):
            self.assertIsNone(characters.file_path(rec["id"], bad), bad)

    def test_a_deleted_character_leaves_the_roster_but_not_the_disk(self):
        rec = _make()
        self.assertTrue(characters.delete(rec["id"]))
        self.assertNotIn(rec["id"], [r["id"] for r in characters.list_all()])
        self.assertTrue(any((Path(characters.CHARACTERS_DIR) / "_deleted").iterdir()))


class ACharacterIsPlayableBeforeThePortraitIsFinished(unittest.TestCase):
    """The online path, with the models faked: playable the moment the
    turnaround lands (a stand-in idle cut off it), the hero idle after."""

    def setUp(self):
        _fresh_dir()

    def test_ready_on_the_turnaround_then_the_idle_crossfades_in(self):
        import io
        import threading
        import time
        from unittest import mock
        from PIL import Image

        def png(w, h, colour):
            b = io.BytesIO()
            Image.new("RGB", (w, h), colour).save(b, format="PNG")
            return b.getvalue()

        def sheet():
            im = Image.new("RGBA", (1600, 900), (0, 0, 0, 0))
            for i in range(4):
                im.paste((90, 60, 40, 255), (80 + i * 400, 100, 280 + i * 400, 820))
            return im

        idle_go = threading.Event()
        turn_go = threading.Event()

        def fake_image(parts, aspect, op):
            if op == "character_idle":
                idle_go.wait(40)
                return png(600, 800, (10, 200, 10))
            if op == "character_turnaround":
                turn_go.wait(40)
            return png(1600, 900, (10, 200, 10))

        def fake_cut(raw):
            im = Image.open(io.BytesIO(raw))
            if im.size == (600, 800):
                out = Image.new("RGBA", (600, 800), (0, 0, 0, 0))
                out.paste((200, 30, 30, 255), (150, 50, 450, 780))
                return out
            return sheet()

        with mock.patch.object(characters, "have_key", lambda: True), \
             mock.patch.object(characters, "_expand", lambda rec, src: {"name": "Wren Hale", "who": "a diver"}), \
             mock.patch.object(characters, "_image", fake_image), \
             mock.patch.object(characters, "_cut", fake_cut), \
             mock.patch.object(characters, "_mechanical_check", lambda cut: ""), \
             mock.patch.object(characters, "_tight", lambda c: c), \
             mock.patch.object(characters, "_text_json",
                               lambda *a, **k: {"body": "lean", "wardrobe_line": "olive canvas suit"}):
            rec = characters.create("A salvage diver.", wait=False)
            cid = rec["id"]
            # Who they are lands before what they look like: the brief's name
            # is on the record, and in the line, while the turnaround draws.
            deadline = time.time() + 30
            while time.time() < deadline and (characters.load(cid) or {}).get("name") != "Wren Hale":
                time.sleep(0.05)
            early = characters.load(cid)
            self.assertEqual(early["name"], "Wren Hale")
            self.assertEqual(early["status"], characters.STATUS_DRAWING)
            self.assertIn("Wren", early["job"]["line"])
            self.assertGreaterEqual(early["job"]["eta"], 8)
            turn_go.set()
            deadline = time.time() + 20
            c = {}
            while time.time() < deadline:
                c = characters.card(characters.load(cid) or {"id": cid})
                if c.get("finishing"):
                    break
                time.sleep(0.05)
            self.assertTrue(c.get("finishing"), c)
            rec = characters.load(cid)
            self.assertEqual(rec["status"], characters.STATUS_READY)
            self.assertEqual(rec["name"], "Wren Hale")
            self.assertEqual(rec["current_look"], rec["job"]["look"])
            d = characters.look_dir(cid, rec["current_look"])
            # Playable, and no portrait yet: the sheet's A-pose is never shown
            # as one ("why is he in an A pose", 2026-09-24). The roster square
            # is there; the screens wait for the hero pose.
            self.assertFalse((d / "idle.png").exists(), "no stand-in portrait cut off the sheet")
            self.assertFalse(characters.card(rec)["idle"])
            self.assertTrue((d / "thumb.png").is_file())
            self.assertTrue((d / "turnaround_ref.jpg").is_file())
            self.assertFalse((d / "idle_key.png").exists(), "the hero idle is still drawing")
            # a run can start now: the sim gets the turnaround and the brief's words
            characters.set_binding("finishing-test", cid, rec["current_look"])
            try:
                blk = characters.bound_block("finishing-test") or {}
            finally:
                characters.set_binding("finishing-test", "", "")
            self.assertTrue((blk.get("_character") or {}).get("turnaround_ref"), blk)
            self.assertIn("diver", json.dumps(blk))

            idle_go.set()
            deadline = time.time() + 20
            while time.time() < deadline and (characters.load(cid) or {}).get("job"):
                time.sleep(0.05)
            rec = characters.load(cid)
            self.assertFalse(rec.get("job"), rec.get("job"))
            self.assertFalse(characters.card(rec).get("finishing"))
            self.assertTrue((d / "idle_key.png").is_file())
            self.assertTrue(characters.card(rec)["idle"])
            look = characters.load_look(cid, rec["current_look"])
            self.assertEqual(look["wardrobe_line"], "olive canvas suit")
            self.assertLessEqual(look["ready_secs"], look["secs"])

    def test_a_fitting_shows_the_old_portrait_until_its_own_is_drawn(self):
        rec = _make()
        base = rec["base_look"]
        characters.save_look(rec["id"], {"id": "fit-wip", "kind": "fit", "parent": base,
                                         "worn": [], "status": "ready"})
        characters.look_dir(rec["id"], "fit-wip").mkdir(parents=True, exist_ok=True)
        characters._update(rec["id"], lambda r: r.update(current_look="fit-wip"))
        c = characters.card(characters.load(rec["id"]))
        self.assertIn(f"/looks/{base}/idle.png", c["idle"])

    def test_repose_draws_every_look_again_and_nothing_else(self):
        import io
        from unittest import mock
        from PIL import Image
        rec = _make()
        base = rec["base_look"]
        d = characters.look_dir(rec["id"], base)
        before_turn = (d / "turnaround_key.png").read_bytes()
        seen = []

        def fake_image(parts, aspect, op):
            seen.append((op, " ".join(p.get("text", "") for p in parts if isinstance(p, dict))))
            b = io.BytesIO(); Image.new("RGB", (600, 800), (1, 2, 3)).save(b, format="PNG")
            return b.getvalue()

        def fake_cut(raw):
            im = Image.new("RGBA", (600, 800), (0, 0, 0, 0))
            im.paste((200, 30, 30, 255), (150, 50, 450, 780))
            return im
        with mock.patch.object(characters, "have_key", lambda: True), \
             mock.patch.object(characters, "_image", fake_image), \
             mock.patch.object(characters, "_cut", fake_cut):
            out = characters.repose(rec["id"])
        self.assertEqual(out, {base: "ok"})
        self.assertEqual([op for op, _ in seen], ["character_idle"])
        self.assertIn("NOT the turnaround's A-pose", seen[0][1])
        self.assertEqual((d / "turnaround_key.png").read_bytes(), before_turn, "the sheet is untouched")

    def test_the_hero_pose_is_asked_for_attitude_not_the_sheet(self):
        self.assertIn("NOT the turnaround's A-pose", characters._IDLE)
        self.assertIn("{manner}", characters._IDLE)

    def test_the_countdown_is_learned_from_this_machine(self):
        self.assertEqual(characters._ready_eta(), characters.READY_ETA_S)
        for i, secs in enumerate((21.0, 24.0, 23.0)):
            d = Path(characters.CHARACTERS_DIR) / f"someone-{i}" / "looks" / "base-x"
            d.mkdir(parents=True)
            (d / "look.json").write_text(json.dumps({"ready_secs": secs}))
        self.assertEqual(characters._ready_eta(), 25)   # the median, and a breath


class ACharacterIsNotLostToABadWrite(unittest.TestCase):
    """The roster is the player's: a torn or unreadable file must not take a
    character off it, and a packaged build must not keep it in the folder an
    update replaces."""

    def setUp(self):
        _fresh_dir()

    def test_an_unreadable_record_comes_back_from_its_last_good_copy(self):
        rec = _make()
        characters._update(rec["id"], lambda r: r.update(tagline="first"))
        characters._update(rec["id"], lambda r: r.update(tagline="second"))
        f = characters.char_dir(rec["id"]) / "character.json"
        f.write_text('{"id": "' + rec["id"] + '", "name": "Or', encoding="utf-8")   # torn mid-write
        characters._CACHE.clear()
        back = characters.load(rec["id"])
        self.assertIsNotNone(back)
        self.assertEqual(back["id"], rec["id"])
        self.assertIn(rec["id"], [r["id"] for r in characters.list_all()])
        self.assertTrue(json.loads(f.read_text(encoding="utf-8"))["id"] == rec["id"], "put back on disk")
        self.assertTrue(f.with_suffix(".json.broken").is_file(), "the torn file is kept to look at")

    def test_writes_leave_no_temp_files(self):
        rec = _make()
        for i in range(3):
            characters._update(rec["id"], lambda r: r.update(tagline=str(i)))
        left = [p.name for p in characters.char_dir(rec["id"]).rglob("*.tmp")]
        self.assertEqual(left, [])

    def test_a_packaged_build_keeps_the_roster_in_the_players_folder(self):
        import sys
        import tempfile
        from unittest import mock
        tmp = Path(tempfile.mkdtemp())
        game = tmp / "SOMEWHERE"
        (game / "characters" / "kept-0001").mkdir(parents=True)
        (game / "characters" / "kept-0001" / "character.json").write_text('{"id": "kept-0001"}')
        with mock.patch.object(characters, "ROOT", game), \
             mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.dict(os.environ, {"APPDATA": str(tmp / "appdata")}):
            d = characters._default_dir()
        self.assertEqual(d, tmp / "appdata" / "SOMEWHERE" / "characters")
        self.assertTrue((d / "kept-0001" / "character.json").is_file(), "an older build's roster comes along")
        shutil.rmtree(tmp, ignore_errors=True)

    def test_from_the_repo_it_is_beside_the_code(self):
        self.assertEqual(characters._default_dir(), characters.ROOT / "characters")


class OneArtStyleByDefaultAndTheirsIfTheyWantIt(unittest.TestCase):
    """Photoreal unless the player says otherwise (Luka came back as inked
    concept art, 2026-09-24): the style leads every picture of them, is kept
    per character, and STYLE redraws the same person in another one."""

    def setUp(self):
        _fresh_dir()

    def _online(self):
        import io
        from unittest import mock
        from PIL import Image
        said = []

        def png(w, h):
            b = io.BytesIO(); Image.new("RGB", (w, h), (10, 200, 10)).save(b, format="PNG"); return b.getvalue()

        def fake_image(parts, aspect, op):
            said.append((op, " ".join(p.get("text", "") for p in parts if isinstance(p, dict))))
            return png(600, 800) if op == "character_idle" else png(1600, 900)

        def fake_cut(raw):
            im = Image.open(io.BytesIO(raw))
            out = Image.new("RGBA", im.size, (0, 0, 0, 0))
            if im.size == (600, 800):
                out.paste((200, 30, 30, 255), (150, 50, 450, 780))
            else:
                for i in range(4):
                    out.paste((90, 60, 40, 255), (80 + i * 400, 100, 280 + i * 400, 820))
            return out
        patches = [mock.patch.object(characters, "have_key", lambda: True),
                   mock.patch.object(characters, "_expand", lambda rec, src: {"name": "Luka Novak", "who": "a gangster"}),
                   mock.patch.object(characters, "_image", fake_image),
                   mock.patch.object(characters, "_cut", fake_cut),
                   mock.patch.object(characters, "_mechanical_check", lambda cut: ""),
                   mock.patch.object(characters, "_tight", lambda c: c),
                   mock.patch.object(characters, "_text_json", lambda *a, **k: {"wardrobe_line": "coat"})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return said

    def test_the_default_style_leads_every_picture(self):
        said = self._online()
        rec = characters.create("a massive cyborg gangster", wait=True)
        self.assertEqual(rec["style"], "")
        ops = dict((op, txt) for op, txt in said)
        for op in ("character_turnaround", "character_idle", "character_face"):
            self.assertIn("ART STYLE", ops[op], op)
            self.assertIn("Photorealistic", ops[op])
            self.assertIn("NOT a drawing", ops[op])
        self.assertNotIn("MODEL SHEET", ops["character_turnaround"])
        self.assertEqual(characters.card(rec)["style"], "")

    def test_a_players_style_is_theirs(self):
        said = self._online()
        rec = characters.create("a massive cyborg gangster", style="Inked comic-book concept art, flat colours",
                                wait=True)
        self.assertEqual(rec["style"], "Inked comic-book concept art, flat colours")
        turn = next(t for op, t in said if op == "character_turnaround")
        self.assertIn("Inked comic-book", turn)
        self.assertNotIn("Photorealistic", turn)

    def test_style_redraws_the_same_person_and_back(self):
        said = self._online()
        rec = characters.create("a massive cyborg gangster", wait=True)
        base = rec["base_look"]
        del said[:]
        rec = characters.restyle(rec["id"], "Inked comic-book concept art", wait=True)
        self.assertEqual(rec["style"], "Inked comic-book concept art")
        self.assertNotEqual(rec["base_look"], base, "a new base, drawn off the old one")
        fit = next(t for op, t in said if op == "character_fitting")
        self.assertIn("EXCEPT the rendering", fit)
        self.assertIn("Inked comic-book concept art", fit)
        self.assertNotIn("_restyle", characters.load(rec["id"]))
        rec = characters.restyle(rec["id"], characters.DEFAULT_STYLE, wait=True)
        self.assertEqual(rec["style"], "", "back to the game's own style")
        with self.assertRaises(ValueError):
            characters.restyle(rec["id"], "")

    def test_one_drawn_before_styles_can_be_redrawn_in_the_default(self):
        self._online()
        rec = characters.create("a massive cyborg gangster", wait=True)
        characters._update(rec["id"], lambda r: r.pop("style", None))    # an older record
        self.assertFalse(characters.card(characters.load(rec["id"]))["style_known"])
        rec = characters.restyle(rec["id"], "", wait=True)
        self.assertEqual(rec["style"], "")
        self.assertTrue(characters.card(rec)["style_known"])

    def test_an_ordinary_fitting_keeps_their_style(self):
        said = self._online()
        rec = characters.create("a massive cyborg gangster", wait=True)
        it = characters.add_item(rec["id"], {"name": "Riot Helmet", "kind": "armor"})
        del said[:]
        characters.set_worn(rec["id"], it["id"], True, wait=True)
        fit = next(t for op, t in said if op == "character_fitting")
        self.assertIn("same art style as the attached turnaround.", fit)
        self.assertNotIn("EXCEPT the rendering", fit)


    def test_restyling_keeps_what_they_have_on(self):
        """Restyling Luka took his riot visor off: the new base is drawn bare
        and nothing put it back. It goes back on over the new base, and until
        it has he stays in the look he had."""
        import time as _t
        said = self._online()
        rec = characters.create("a massive cyborg gangster", wait=True)
        it = characters.add_item(rec["id"], {"name": "Shattered Riot Visor", "kind": "armor"})
        rec = characters.set_worn(rec["id"], it["id"], True, wait=True)
        fitted = rec["current_look"]
        del said[:]
        rec = characters.restyle(rec["id"], "Inked comic-book concept art", wait=True)
        new_base = rec["base_look"]
        deadline = _t.time() + 20
        while _t.time() < deadline:
            rec = characters.load(rec["id"])
            cur = rec["looks"].get(rec["current_look"], {})
            if rec["current_look"] not in (fitted, new_base) and not rec.get("job"):
                break
            _t.sleep(0.1)
        cur = characters.load_look(rec["id"], rec["current_look"])
        self.assertEqual(cur["parent"], new_base, "the visor goes back on over the new style")
        self.assertEqual(cur["worn"], [it["id"]])
        self.assertTrue(next(i for i in rec["inventory"] if i["id"] == it["id"])["worn"])
        self.assertEqual(sum(1 for op, _ in said if op == "character_fitting"), 2)


    def test_every_beat_that_draws_them_takes_the_current_look(self):
        """"confirm all other interactions use the current look": a fitting
        that lands between choices is worn by the goal cutscene, the fight,
        the photo — engine.adopt_current_look runs before each of them
        (api._DRAWS_THE_PLAYER), not only at the next choice turn."""
        from unittest import mock
        import engine
        import api
        self._online()
        rec = characters.create("a massive cyborg gangster", wait=True)
        it = characters.add_item(rec["id"], {"name": "Shattered Riot Visor", "kind": "armor"})
        store = {"s": {"character_id": rec["id"], "look_id": rec["current_look"], "turn_count": 4}}
        characters.set_binding("s", rec["id"], rec["current_look"])
        rec = characters.set_worn(rec["id"], it["id"], True, wait=True)
        with mock.patch.object(engine, "_load_state", lambda sid="default": dict(store[sid])), \
                mock.patch.object(engine, "_save_state", lambda st, sid="default": store.__setitem__(sid, dict(st))):
            line = engine.adopt_current_look("s")
        self.assertEqual(store["s"]["look_id"], rec["current_look"])
        self.assertEqual(characters.binding("s"), (rec["id"], rec["current_look"]))
        self.assertIn("Shattered Riot Visor", line)
        self.assertEqual(store["s"]["look_changed"]["put_on"], ["Shattered Riot Visor"])
        for path in ("/api/cutscene/play", "/api/encounter/begin", "/api/encounter/exchange",
                     "/api/encounter/resolve", "/api/photo"):
            self.assertIn(path, api._DRAWS_THE_PLAYER)
        # and the sheet every one of those draws from is that look
        block = characters.bound_block("s")
        self.assertTrue(block["_character"]["look"] == rec["current_look"])


class WhereAThingGoesOnTheBody(unittest.TestCase):
    CASES = [
        ({"name": "Riot Helmet", "kind": "armor"}, "head"),
        ({"name": "Cracked Gas Mask", "kind": "armor"}, "face"),
        ({"name": "Kevlar Vest", "kind": "armor"}, "outer"),
        ({"name": "Signal Shear", "kind": "weapon"}, "held"),
        ({"name": "Brass Knuckles", "kind": "weapon"}, "hands"),
        ({"name": "Lead Gloves", "kind": "armor"}, "hands"),
        ({"name": "Saint's Pendant", "kind": "relic"}, "neck"),
        ({"name": "Frequency Key", "kind": "relic"}, ""),
        ({"name": "Scope Module", "kind": "upgrade"}, ""),
        ({"name": "Hydraulic Pry Bar", "kind": "tool"}, "held"),
        ({"name": "Field Kit", "kind": "armor", "look": "a canvas backpack of dressings"}, "back"),
    ]

    def test_slots(self):
        for item, want in self.CASES:
            self.assertEqual(characters.slot_for(item), want, item)


class WearingSomethingChangesThem(unittest.TestCase):
    def setUp(self):
        _fresh_dir()
        self.rec = _make()
        self.cid = self.rec["id"]
        from PIL import Image
        self.plate = Path(characters.CHARACTERS_DIR) / "_plate.png"
        Image.new("RGBA", (64, 64), (20, 20, 20, 255)).save(self.plate)

    def test_an_item_is_copied_onto_the_character(self):
        it = characters.add_item(self.cid, {"name": "Riot Helmet", "kind": "armor",
                                            "plate": str(self.plate), "look": "black helmet"})
        self.assertEqual(it["slot"], "head")
        self.assertTrue((characters.char_dir(self.cid) / "items" / it["plate"]).is_file())
        self.assertTrue((characters.char_dir(self.cid) / "items" / it["ref"]).is_file())
        # the same thing twice is one thing
        again = characters.add_item(self.cid, {"name": "riot helmet", "kind": "armor"})
        self.assertEqual(again["id"], it["id"])
        self.assertEqual(len(characters.load(self.cid)["inventory"]), 1)

    def test_wearing_is_a_fitting_and_taking_it_off_costs_nothing(self):
        it = characters.add_item(self.cid, {"name": "Riot Helmet", "kind": "armor", "plate": str(self.plate)})
        base = characters.load(self.cid)["base_look"]
        rec = characters.set_worn(self.cid, it["id"], True, wait=True)
        fitted = rec["current_look"]
        self.assertNotEqual(fitted, base)
        self.assertEqual(characters.load_look(self.cid, fitted)["worn"], [it["id"]])
        self.assertTrue(characters.load(self.cid)["inventory"][0]["worn"])
        rec = characters.set_worn(self.cid, it["id"], False, wait=True)
        self.assertEqual(rec["current_look"], base, "taking it off goes back to the base look")
        n_looks = len(characters.load(self.cid)["looks"])
        rec = characters.set_worn(self.cid, it["id"], True, wait=True)
        self.assertEqual(rec["current_look"], fitted, "the outfit was cached")
        self.assertEqual(len(characters.load(self.cid)["looks"]), n_looks, "no new render")

    def test_one_thing_per_slot(self):
        a = characters.add_item(self.cid, {"name": "Riot Helmet", "kind": "armor"})
        b = characters.add_item(self.cid, {"name": "Welding Helmet", "kind": "armor"})
        characters.set_worn(self.cid, a["id"], True, wait=True)
        characters.set_worn(self.cid, b["id"], True, wait=True)
        worn = [i["name"] for i in characters.worn_items(characters.load(self.cid))]
        self.assertEqual(worn, ["Welding Helmet"])

    def test_a_carried_thing_cannot_be_worn(self):
        it = characters.add_item(self.cid, {"name": "Frequency Key", "kind": "relic"})
        with self.assertRaises(ValueError):
            characters.set_worn(self.cid, it["id"], True)

    def test_change_something_is_a_new_base(self):
        base = self.rec["base_look"]
        rec = characters.revise(self.cid, "make the suit a long waxed duster", wait=True)
        self.assertNotEqual(rec["base_look"], base)
        self.assertEqual(rec["current_look"], rec["base_look"])


class TheSimulationIsShownTheTurnaround(unittest.TestCase):
    """get_spec() lays the run's character over the cast sheet — every surface
    that asks game_identity who is on screen gets them."""

    def setUp(self):
        _fresh_dir()
        self.rec = _make("Oriel Vance, a salvage diver in her forties.", name="Oriel Vance")
        self.sid = characters._current_session_id() or "default"

    def tearDown(self):
        characters._RUN_BIND.clear()

    def _bind(self):
        characters.set_binding(self.sid, self.rec["id"], self.rec["current_look"])

    def test_unbound_is_the_cast_sheet(self):
        characters.set_binding(self.sid, "", "")
        self.assertEqual(game_identity.get_spec(), game_identity.raw_spec())

    def test_bound_is_the_character(self):
        self._bind()
        spec = game_identity.get_spec()
        self.assertEqual(game_identity.display_name(spec), "Oriel Vance")
        refs = game_identity.character_reference_paths(spec)
        self.assertEqual(len(refs), 1)
        self.assertTrue(refs[0].endswith("turnaround_ref.jpg"))
        self.assertEqual(game_identity.face_reference_paths(spec)[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1],
                         "face_ref.jpg")

    def test_the_label_says_turnaround_and_one_person(self):
        self._bind()
        spec = game_identity.get_spec()
        ref = game_identity.character_reference_paths(spec)[0]
        label = game_identity.reference_part_label(ref, spec)
        self.assertIn("CHARACTER SHEET", label)
        self.assertIn("TURNAROUND", label)
        self.assertIn("Draw ONE", label)
        self.assertIn("TURNAROUND", game_identity.reference_annotation([ref], spec))
        self.assertIn("TURNAROUND", game_identity.keep_character_instruction(spec, has_character_plate=True))

    def test_a_fitting_worn_before_its_words_land_says_what_was_put_on(self):
        # Playable at the turnaround: the read-back has not written the words
        # yet, so the sim is told the old look plus the new thing.
        base = characters.load_look(self.rec["id"], self.rec["base_look"])
        base.update({"garments": {"outer": "patched olive canvas suit"}, "body": "lean, forties",
                     "wardrobe_line": "patched olive canvas suit"})
        characters.save_look(self.rec["id"], base)
        it = characters.add_item(self.rec["id"], {"name": "Riot Helmet", "kind": "armor",
                                                  "look": "a black riot helmet, visor up"})
        characters.save_look(self.rec["id"], {"id": "fit-test", "kind": "fit", "parent": base["id"],
                                              "worn": [it["id"]], "status": "ready"})
        characters._update(self.rec["id"], lambda r: [i.update(worn=True) for i in r["inventory"]])
        characters.set_binding(self.sid, self.rec["id"], "fit-test")
        blk = characters.bound_block(self.sid)
        self.assertIn("olive canvas suit", blk["wardrobe"])
        self.assertIn("black riot helmet", blk["wardrobe"])
        self.assertEqual(blk["appearance"], "lean, forties")

    def test_the_editors_never_see_or_save_the_character(self):
        self._bind()
        raw = game_identity.raw_spec()
        self.assertNotIn("_character", raw[game_identity.CHARACTER_KEY])
        self.assertNotEqual(raw[game_identity.CHARACTER_KEY].get("name"), "Oriel Vance")

    def test_the_idle_never_reaches_the_sim(self):
        self._bind()
        spec = game_identity.get_spec()
        for p in game_identity.identity_reference_paths(spec=spec):
            self.assertNotIn("idle", Path(p).name)


class TheRunKeepsTheCharactersPack(unittest.TestCase):
    def setUp(self):
        _fresh_dir()
        self.rec = _make()
        self.cid = self.rec["id"]

    def test_bind_run_hands_over_the_pack_and_counts_the_run(self):
        it = characters.add_item(self.cid, {"name": "Riot Helmet", "kind": "armor"})
        st = {}
        characters.bind_run(st, self.cid)
        self.assertEqual(st["character_id"], self.cid)
        self.assertEqual([g["name"] for g in st["gear"]], ["Riot Helmet"])
        self.assertEqual(st["gear"][0]["character_item"], it["id"])
        self.assertEqual(characters.load(self.cid)["record"]["runs"], 1)

    def test_what_the_run_finds_goes_onto_the_character(self):
        import goal
        st = {}
        characters.bind_run(st, self.cid)
        st["gear"] = goal.held(st) + [{"name": "Signal Shear", "kind": "weapon", "plate": ""}]
        got = goal._stow(st, {"name": "Signal Shear"})
        self.assertTrue(got.get("character_item"))
        self.assertIn("Signal Shear", [i["name"] for i in characters.load(self.cid)["inventory"]])

    def test_only_what_is_worn_fights(self):
        import goal
        it = characters.add_item(self.cid, {"name": "Signal Shear", "kind": "weapon"})
        st = {}
        characters.bind_run(st, self.cid)
        self.assertEqual(goal.gear_edge(st), {})
        characters.set_worn(self.cid, it["id"], True, wait=True)
        characters.sync_run_look(st)
        self.assertEqual(goal.gear_edge(st), {"weapon": ["Signal Shear"]})

    def test_the_run_takes_a_new_look_at_the_turn_boundary_and_says_so(self):
        it = characters.add_item(self.cid, {"name": "Riot Helmet", "kind": "armor"})
        st = {}
        characters.bind_run(st, self.cid)
        before = st["look_id"]
        characters.set_worn(self.cid, it["id"], True, wait=True)
        self.assertEqual(st["look_id"], before, "not mid-turn")
        line = characters.sync_run_look(st, "t-sync")
        self.assertNotEqual(st["look_id"], before)
        self.assertIn("now wearing Riot Helmet", line)
        self.assertEqual(characters.binding("t-sync")[1], st["look_id"])

    def test_the_image_model_is_told_the_outfit_changed_for_two_turns(self):
        import engine
        it = characters.add_item(self.cid, {"name": "Riot Helmet", "kind": "armor"})
        st = {"turn_count": 4}
        characters.bind_run(st, self.cid)
        characters.set_worn(self.cid, it["id"], True, wait=True)
        characters.sync_run_look(st)
        line = engine._wardrobe_change_directive(st)
        self.assertIn("NOW WEARING Riot Helmet", line)
        self.assertIn("previous frame shows the old outfit", line)
        st["turn_count"] = 5
        self.assertTrue(engine._wardrobe_change_directive(st))
        st["turn_count"] = 6
        self.assertEqual(engine._wardrobe_change_directive(st), "")

    def test_the_new_thing_rides_as_its_own_plate_while_it_is_new(self):
        import engine
        from PIL import Image
        plate = Path(characters.CHARACTERS_DIR) / "_helmet.png"
        Image.new("RGBA", (32, 32), (10, 10, 10, 255)).save(plate)
        it = characters.add_item(self.cid, {"name": "Riot Helmet", "kind": "armor", "plate": str(plate)})
        st = {"turn_count": 2}
        characters.bind_run(st, self.cid)
        characters.set_worn(self.cid, it["id"], True, wait=True)
        characters.sync_run_look(st)
        plates = engine._wardrobe_change_plates(st)
        self.assertEqual(list(plates.values()), ["Riot Helmet"])
        self.assertTrue(list(plates)[0].endswith("_ref.jpg"))
        st["turn_count"] = 9
        self.assertEqual(engine._wardrobe_change_plates(st), {})

    def test_the_pack_card_names_the_item_and_whether_it_is_worn(self):
        import goal
        it = characters.add_item(self.cid, {"name": "Riot Helmet", "kind": "armor"})
        st = {}
        characters.bind_run(st, self.cid)
        card = goal.pack_cards(st)[0]
        self.assertEqual(card["id"], it["id"])
        self.assertEqual(card["slot"], "head")
        self.assertFalse(card["worn"])
        self.assertTrue(card["wearable"])


class TheOldCastSheetComesAlong(unittest.TestCase):
    def setUp(self):
        _fresh_dir()

    def test_a_recast_sheet_becomes_an_undrawn_character_once(self):
        raw = game_identity.raw_spec()
        block = dict(raw[game_identity.CHARACTER_KEY])
        block.update({"enabled": True, "name": "Simon Riley", "role": "Special operations operative",
                      "appearance": "tall, skull balaclava", "reference_images": []})
        raw[game_identity.CHARACTER_KEY] = block
        from unittest import mock
        with mock.patch.object(game_identity, "raw_spec", return_value=raw):
            characters.ensure_seeded()
            characters.ensure_seeded()
        rows = [r for r in characters.list_all() if r.get("origin") == "cast_sheet"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Simon Riley")
        self.assertEqual(rows[0]["status"], characters.STATUS_NEW)


class ANewRunIsTheCharacterPicked(unittest.TestCase):
    """PLAY → the character screen → the picker → /api/reset {character_id}:
    the run is that character from its first render, and says so."""

    SID = "characters-test"

    @classmethod
    def setUpClass(cls):
        _fresh_dir()
        cls.rec = characters.create("Oriel Vance, a salvage diver.", name="Oriel Vance", wait=True)
        characters.add_item(cls.rec["id"], {"name": "Riot Helmet", "kind": "armor"})
        import api
        cls.client = api.app.test_client()

    def tearDown(self):
        characters._RUN_BIND.pop(self.SID, None)

    @classmethod
    def tearDownClass(cls):
        # The reset made this the engine's active session; leave nothing bound
        # behind for a suite that runs after this one in the same process.
        import engine
        characters.set_binding(cls.SID, "", "")
        try:
            engine.set_active_session("default")
        except Exception:
            pass

    def test_the_reset_binds_the_character_and_hands_over_the_pack(self):
        import engine
        r = self.client.post("/api/reset", json={"session_id": self.SID, "character_id": self.rec["id"]})
        self.assertEqual(r.status_code, 200)
        st = engine._load_state(self.SID)
        self.assertEqual(st.get("character_id"), self.rec["id"])
        self.assertEqual(st.get("look_id"), self.rec["current_look"])
        self.assertEqual([g["name"] for g in st.get("gear") or []], ["Riot Helmet"])
        body = self.client.get(f"/api/character?session_id={self.SID}").get_json()
        self.assertEqual(body["character"]["name"], "Oriel Vance")
        self.assertEqual(body["pack"][0]["slot"], "head")

    def test_a_reset_without_one_is_the_cast_sheet(self):
        import engine
        self.client.post("/api/reset", json={"session_id": self.SID, "character_id": self.rec["id"]})
        self.client.post("/api/reset", json={"session_id": self.SID})
        st = engine._load_state(self.SID)
        self.assertFalse(st.get("character_id"))
        self.assertEqual(characters.binding(self.SID), ("", ""))

    def test_wear_over_http_flips_the_pack_and_fits(self):
        self.client.post("/api/reset", json={"session_id": self.SID, "character_id": self.rec["id"]})
        pack = self.client.get(f"/api/character?session_id={self.SID}").get_json()["pack"]
        r = self.client.post("/api/character/wear", json={"session_id": self.SID, "item": pack[0]["id"], "on": True})
        body = r.get_json()
        self.assertTrue(body["ok"], body)
        self.assertTrue(body["pack"][0]["worn"])
        for _ in range(50):
            if not characters.busy(self.rec["id"] + "-fit"):
                break
            import time
            time.sleep(0.1)
        c = characters.load(self.rec["id"])
        self.assertNotEqual(c["current_look"], c["base_look"])

    def test_the_roster_api(self):
        rows = self.client.get("/api/characters").get_json()["characters"]
        self.assertIn("Oriel Vance", [r["name"] for r in rows])
        one = self.client.get(f"/api/characters/{self.rec['id']}").get_json()["character"]
        self.assertTrue(one["idle"])
        f = self.client.get(one["idle"])
        self.assertEqual(f.status_code, 200)
        self.assertEqual(self.client.get(f"/api/characters/{self.rec['id']}/file/character.json").status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
