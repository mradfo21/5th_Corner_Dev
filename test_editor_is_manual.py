"""
The editor writes one World, and only GENERATE reaches the game.

Asked: "when I'm making changes to the world I notice it's STILL trying to run
the game, STILL trying to update the values, which makes it super muddy. I press
generate and it seems to create stale worlds built from a mish mash of data
pollinated from other worlds. The editor needs to be more manual so I can know
what I'm gonna get when I run the world. Make sure the editor is driven by
generate and that when I press generate it's going to restart / recache cleanly
to show me the current world."

What these pin, server side:
  * the live prompt file knows which World it holds (worlds_store.bound_slug),
    an editor write binds the World it names first, and a persist that would
    copy one World's sheet into another is refused;
  * a sheet save with no World named no longer writes into the START World;
  * saving never draws; GENERATE binds the World clean and draws it, lit from
    its own palette;
  * the editor holds the run's prompts and gives them back on a plain close;
  * a World missing its place gets the blank place, never SOMEWHERE's;
  * RESET on a World that isn't SOMEWHERE does not turn it into SOMEWHERE;
  * a New Game can start in the World GENERATE drew.
And client side: opening the editor pauses the run and boots none; saving does
not restage the video; closing restarts in the drawn World or hands the paused
run back.

Never touches the network. Redirects experiences/, worlds/, and the prompt file
into a temp dir.

Run: python -m unittest test_editor_is_manual -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import experience_store as xs
import worlds_store as ws
import prompts_store as ps
import game_identity as gi
import world_frames as wf
import engine
import api

ROOT = Path(__file__).resolve().parent


class _Isolated(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self._tmpdir.name)
        self._orig = {
            "exp": xs.EXPERIENCES_DIR,
            "sessions": xs.SESSIONS_DIR,
            "worlds": ws.WORLDS_DIR,
            "prompts": ps.PROMPTS_PATH,
            "defaults": ps.DEFAULTS_PATH,
            "refs": gi.REFERENCES_DIR,
            "gi_sessions": gi.SESSIONS_DIR,
            "bound": ws.bound_slug(),
        }
        xs.EXPERIENCES_DIR = tmp / "experiences"
        xs.SESSIONS_DIR = tmp / "sessions"
        ws.WORLDS_DIR = tmp / "worlds"
        ps.PROMPTS_PATH = tmp / "simulation_prompts.json"
        ps.DEFAULTS_PATH = tmp / "simulation_prompts.defaults.json"
        gi.REFERENCES_DIR = tmp / "references"
        gi.SESSIONS_DIR = tmp / "gi_sessions"
        gi.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        # The factory IS SOMEWHERE, as it is in the shipped defaults.
        factory = {
            "world_initial_state": "SOMEWHERE: 1993, the Horizon fence, a photojournalist.",
            "action_consequence_instructions": "Return JSON.",
        }
        factory.update(gi.default_spec())
        for path in (ps.PROMPTS_PATH, ps.DEFAULTS_PATH):
            path.write_text(json.dumps(factory), encoding="utf-8")
        ps.PROMPTS._mtime = None
        ps.PROMPTS._last_check = 0.0
        ps.PROMPTS._reload(force=True)
        ws._set_bound("")
        api._EDITOR_HOLD["prompts"] = None
        api._EDITOR_HOLD["bound"] = ""
        self.client = api.app.test_client()
        # Two Worlds, each with its own place.
        exp = xs.create_experience("Riot")
        self.a = exp["worlds"][0]
        exp = xs.add_world("Harbour")
        self.b = exp["worlds"][-1]
        self._write_place(self.a, "A rain-slicked street, a protest, robot police.")
        self._write_place(self.b, "A foggy harbour at dawn, cranes, a moored tanker.")

    def tearDown(self):
        xs.EXPERIENCES_DIR = self._orig["exp"]
        xs.SESSIONS_DIR = self._orig["sessions"]
        ws.WORLDS_DIR = self._orig["worlds"]
        ps.PROMPTS_PATH = self._orig["prompts"]
        ps.DEFAULTS_PATH = self._orig["defaults"]
        gi.REFERENCES_DIR = self._orig["refs"]
        gi.SESSIONS_DIR = self._orig["gi_sessions"]
        ws._set_bound(self._orig["bound"])
        api._EDITOR_HOLD["prompts"] = None
        api._EDITOR_HOLD["bound"] = ""
        ps.PROMPTS._mtime = None
        ps.PROMPTS._reload(force=True)
        self._tmpdir.cleanup()

    def _write_place(self, world, summary):
        data = ws.get_world(world["slug"])
        setting = dict(data["prompts"].get(gi.SETTING_KEY) or {})
        setting["summary"] = summary
        setting["enabled"] = True
        data["prompts"][gi.SETTING_KEY] = setting
        (ws.WORLDS_DIR / f"{world['slug']}.json").write_text(
            json.dumps(data), encoding="utf-8")

    def _summary_in(self, world):
        return ws.get_world(world["slug"])["prompts"][gi.SETTING_KEY]["summary"]

    def _live_summary(self):
        ps.PROMPTS._reload(force=True)
        return (ps.PROMPTS.get(gi.SETTING_KEY) or {}).get("summary")


class TestTheLiveFileKnowsWhichWorldItHolds(_Isolated):
    def test_bind_and_save_record_the_world(self):
        ws.load_world(self.a["slug"])
        self.assertEqual(ws.bound_slug(), self.a["slug"])
        ws.save_world(self.b["name"], slug=self.b["slug"])
        self.assertEqual(ws.bound_slug(), self.b["slug"])

    def test_a_persist_cannot_copy_one_world_into_another(self):
        """The run stitched into B (or New Game bound the start World); the
        editor, open on A, saves. That used to write B's sheet into A."""
        ws.load_world(self.b["slug"])
        r = self.client.post("/api/admin/studio/experience/persist", json={"id": self.a["id"]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self._summary_in(self.a),
                         "A rain-slicked street, a protest, robot police.")

    def test_an_edit_binds_the_world_it_names_first(self):
        ws.load_world(self.b["slug"])
        r = self.client.put("/api/admin/studio/identity", json={
            gi.SETTING_KEY: {"era": "2088"}, "world_id": self.a["id"]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(ws.bound_slug(), self.a["slug"])
        saved = ws.get_world(self.a["slug"])["prompts"][gi.SETTING_KEY]
        self.assertEqual(saved["era"], "2088")
        self.assertEqual(saved["summary"], "A rain-slicked street, a protest, robot police.")
        self.assertEqual(self._summary_in(self.b),
                         "A foggy harbour at dawn, cranes, a moored tanker.",
                         "the World the live file held before is untouched")

    def test_a_sheet_save_naming_no_world_does_not_write_the_start_world(self):
        start_before = json.dumps(ws.get_world(self.a["slug"])["prompts"], sort_keys=True)
        ws.load_world(self.b["slug"])
        r = self.client.put("/api/admin/studio/identity", json={
            gi.SETTING_KEY: {"era": "a stray edit"}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            json.dumps(ws.get_world(self.a["slug"])["prompts"], sort_keys=True),
            start_before)

    def test_a_prompt_save_binds_the_world_it_names(self):
        ws.load_world(self.b["slug"])
        r = self.client.put("/api/admin/studio/prompts", json={
            "data": {"world_initial_state": "A riot bible."}, "world_id": self.a["id"]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(ws.bound_slug(), self.a["slug"])


class TestOnlyGenerateDraws(_Isolated):
    def test_saving_does_not_draw(self):
        ws.load_world(self.a["slug"])
        with patch.object(wf, "schedule_ensure") as sched, \
             patch.object(wf, "ensure") as ens, \
             patch.object(wf, "force_reset") as force:
            r = self.client.post("/api/admin/studio/experience/persist", json={"id": self.a["id"]})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(sched.called or ens.called or force.called)

    def test_generate_binds_clean_and_draws_lit_from_its_own_roll(self):
        ws.load_world(self.a["slug"])
        # Something left in the live file that the World does not say.
        ps.save_prompts_bulk({"world_initial_state": "LEFTOVER from another World"})
        data = ws.get_world(self.a["slug"])
        data["prompts"]["world_initial_state"] = "A 2088 riot bible."
        (ws.WORLDS_DIR / f"{self.a['slug']}.json").write_text(json.dumps(data), encoding="utf-8")
        ws._set_bound("")   # unknown: the live file is not trusted into the World
        with patch.object(wf, "force_reset", return_value={}) as force, \
             patch.object(engine, "_generate_random_starting_time",
                          return_value="2:40am | weather: sodium rain"):
            r = self.client.post("/api/admin/studio/worlds/frames/reset", json={"id": self.a["id"]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        ps.PROMPTS._reload(force=True)
        self.assertEqual(ps.PROMPTS.get("world_initial_state"), "A 2088 riot bible.",
                         "GENERATE plays exactly what is saved in the World")
        self.assertEqual(ws.get_world(self.a["slug"])["prompts"]["world_initial_state"],
                         "A 2088 riot bible.", "the leftover was not saved into it")
        force.assert_called_once()
        self.assertEqual(force.call_args.kwargs.get("time_of_day"),
                         "2:40am | weather: sodium rain")
        body = r.get_json()["data"]
        self.assertEqual(body["prompts"]["world_initial_state"], "A 2088 riot bible.")

    def test_generate_on_another_worlds_card_draws_it_from_its_own_file(self):
        ws.load_world(self.a["slug"])
        with patch.object(wf, "force_reset", return_value={}):
            r = self.client.post("/api/admin/studio/worlds/frames/reset", json={"id": self.b["id"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._summary_in(self.b),
                         "A foggy harbour at dawn, cranes, a moored tanker.")
        self.assertEqual(self._live_summary(),
                         "A foggy harbour at dawn, cranes, a moored tanker.")


class TestClosingHandsTheRunBack(_Isolated):
    def test_a_plain_close_restores_the_runs_prompts(self):
        ws.load_world(self.b["slug"])   # the run is in the harbour
        self.client.post("/api/admin/studio/session/hold", json={})
        self.client.post("/api/admin/studio/experience/enter", json={"id": self.a["id"]})
        self.client.put("/api/admin/studio/identity", json={
            gi.SETTING_KEY: {"era": "2088"}, "world_id": self.a["id"]})
        self.assertEqual(self._live_summary(), "A rain-slicked street, a protest, robot police.")
        r = self.client.post("/api/admin/studio/session/release", json={"restore": True})
        self.assertTrue(r.get_json()["data"]["restored"])
        self.assertEqual(self._live_summary(), "A foggy harbour at dawn, cranes, a moored tanker.")
        self.assertEqual(ws.bound_slug(), self.b["slug"])
        self.assertEqual(ws.get_world(self.a["slug"])["prompts"][gi.SETTING_KEY]["era"], "2088",
                         "the edit is kept — in the World, not in the run")

    def test_after_generate_nothing_is_put_back(self):
        ws.load_world(self.b["slug"])
        self.client.post("/api/admin/studio/session/hold", json={})
        self.client.post("/api/admin/studio/experience/enter", json={"id": self.a["id"]})
        r = self.client.post("/api/admin/studio/session/release", json={"restore": False})
        self.assertFalse(r.get_json()["data"]["restored"])
        self.assertEqual(self._live_summary(), "A rain-slicked street, a protest, robot police.")


class TestNoWorldIsBornSomewhere(_Isolated):
    def test_a_world_missing_its_place_gets_the_blank_place(self):
        data = ws.get_world(self.a["slug"])
        data["prompts"].pop("world_initial_state", None)
        (ws.WORLDS_DIR / f"{self.a['slug']}.json").write_text(json.dumps(data), encoding="utf-8")
        ws.load_world(self.a["slug"])
        ps.PROMPTS._reload(force=True)
        bible = ps.PROMPTS.get("world_initial_state") or ""
        self.assertNotIn("SOMEWHERE", bible)
        self.assertEqual(bible, ws.blank_place()["world_initial_state"])

    def test_reset_on_another_world_keeps_it_out_of_somewhere(self):
        r = self.client.post("/api/admin/studio/prompts/reset",
                             json={"all": True, "world_id": self.a["id"]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        ps.PROMPTS._reload(force=True)
        self.assertNotIn("SOMEWHERE", ps.PROMPTS.get("world_initial_state") or "")
        self.assertEqual(ps.PROMPTS.get("action_consequence_instructions"), "Return JSON.",
                         "the rulebook still comes back as shipped")


class TestANewGameCanStartInTheWorldGenerateDrew(_Isolated):
    def test_the_override_lands_in_that_world(self):
        st = engine.apply_experience_start({}, "t", world_id=self.b["id"])
        self.assertEqual(st["experience_world_id"], self.b["id"])
        self.assertEqual(ws.bound_slug(), self.b["slug"])
        self.assertFalse(st.get("pending_cutscene"))

    def test_without_it_the_start_world(self):
        st = engine.apply_experience_start({}, "t")
        self.assertEqual(st["experience_world_id"], self.a["id"])


class TestTheClientPausesAndGenerateDrives(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = (ROOT / "static" / "js" / "standalone.js").read_text(encoding="utf-8")

    def _fn(self, start, end):
        return self.app.split(start, 1)[1].split(end, 1)[0]

    def test_opening_the_editor_pauses_the_run_and_boots_none(self):
        body = self._fn("    async function open(opts) {", "    function finishClose(opts)")
        self.assertIn("pauseRun()", body)
        self.assertIn("/api/admin/studio/session/hold", body)
        self.assertIn("boot: false", body)
        self.assertIn("await enterWorld(editingWorldId)", body)
        self.assertLess(body.index("session/hold"), body.index("enterWorld(editingWorldId)"),
                        "the run's prompts are held before the World is bound over them")
        pause = self._fn("    function pauseRun() {", "\n    }\n")
        for call in ("stopPolling()", "clearTurnWatchdog()", "clearTimeout(state.autoTimer)"):
            self.assertIn(call, pause)
        viewport = self._fn("    function ensurePlayViewport(opts) {", "    function isBooted()")
        self.assertIn("if (!boot) return Promise.resolve();", viewport)

    def test_nothing_ambient_runs_under_the_editor(self):
        gate = self._fn("  function ambientContextAllowed() {", "\n  }\n")
        self.assertIn('"world-editor-on"', gate)
        auto = self._fn("  function scheduleAutoAdvance(delay) {", "  function setAutoPlay(on)")
        self.assertIn('"world-editor-on"', auto)

    def test_saves_name_the_world_and_do_not_restage(self):
        save = self._fn("    async function saveFields(fields) {", "    function dirtyFields()")
        self.assertIn("world_id: resolveEditingWorldId()", save)
        self.assertNotIn("resteerLiveFromSheet(", save)
        self.assertNotIn("Camera.reload(", save)
        ident = self._fn("    async function saveIdentity(blockId, patch, opts) {",
                         "    async function clearIdentityBlock")
        self.assertIn("world_id: resolveEditingWorldId()", ident)
        apply_ = self._fn("    function applyIdentityPayload(data, opts) {",
                          "    async function saveIdentity")
        self.assertIn("opts && opts.resteer", apply_)

    def test_a_changed_world_is_not_reported_as_drawing(self):
        busy = self._fn("    function anyFrameBusy() {", "\n    }\n")
        self.assertNotIn('"dirty"', busy)

    def test_generate_restarts_the_run_in_the_world_it_drew(self):
        gen = self._fn("    async function resetWorldPicture(worldId) {",
                       "    // Art direction lives in the prompt file")
        self.assertIn("generatedWorldId = wid", gen)
        self.assertLess(gen.index("/api/admin/studio/worlds/frames/reset"),
                        gen.index("applySheetToLiveScene()"),
                        "the live scene is restaged from the World GENERATE bound")
        resume = self._fn("    async function resumeRun(dest, opts) {", "\n    }\n")
        self.assertIn("resetGame(drawn ? { worldId: drawn } : undefined)", resume)
        self.assertIn('{ restore: true }', resume)
        self.assertIn('{ restore: false }', resume)
        reset = self._fn("  async function resetGame(opts) {", "  // Turn watchdog")
        self.assertIn("world_id: startWorldId", reset)


if __name__ == "__main__":
    unittest.main()
