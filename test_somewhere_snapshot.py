"""
Phase 0 freeze: Play is locked to the shipped SOMEWHERE World.

This is the demo that already works. The file may change when we improve
the game — that is allowed. The test fails if Play stops binding to it,
or if the snapshot stops being the Horizon/Jason game.

Run: python -m unittest test_somewhere_snapshot -v
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORLD_PATH = ROOT / "worlds" / "somewhere.json"
EXPERIENCE_PATH = ROOT / "experiences" / "somewhere.json"


def _load_world() -> dict:
    with WORLD_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


FP_PATH = ROOT / "worlds" / "somewhere-fp.json"
FRAME_PATH = ROOT / "worlds" / "somewhere.frame.png"


class TestSomewhereSnapshotOnDisk(unittest.TestCase):
    def test_world_file_is_checked_in(self):
        self.assertTrue(WORLD_PATH.is_file(), f"missing {WORLD_PATH}")

    def test_experience_file_is_checked_in(self):
        self.assertTrue(EXPERIENCE_PATH.is_file(), f"missing {EXPERIENCE_PATH}")

    def test_play_snapshot_is_third_person_with_jason(self):
        prompts = _load_world().get("prompts") or {}
        cam = prompts.get("camera_perspective") or {}
        self.assertEqual(cam.get("mode"), "third_person")
        who = prompts.get("player_character") or {}
        self.assertTrue(who.get("enabled"))
        self.assertIn("Jason", str(who.get("name") or ""))

    def test_fp_rollback_is_first_person(self):
        self.assertTrue(FP_PATH.is_file(), f"missing {FP_PATH}")
        data = json.loads(FP_PATH.read_text(encoding="utf-8"))
        cam = (data.get("prompts") or {}).get("camera_perspective") or {}
        self.assertEqual(cam.get("mode"), "first_person")

    def test_opening_frame_is_on_disk(self):
        self.assertTrue(FRAME_PATH.is_file(), f"missing {FRAME_PATH}")

    def test_shared_templates_do_not_hardcode_eyeline(self):
        prompts = _load_world().get("prompts") or {}
        consequence = prompts.get("action_consequence_instructions") or ""
        self.assertNotIn("first-person camera", consequence)
        negative = prompts.get("image_negative_prompt") or ""
        self.assertNotIn("third person perspective", negative)
        flip = prompts.get("gemini_flipbook_4panel_prefix") or ""
        self.assertNotIn("FIRST-PERSON SEQUENCE", flip)

    def test_snapshot_is_the_horizon_demo(self):
        brief = (_load_world().get("prompts") or {}).get("world_initial_state") or ""
        for marker in ("1993", "Four Corners", "Horizon", "Jason", "photojournalist"):
            self.assertIn(marker, brief, f"snapshot lost {marker!r}")

    def test_experience_binds_the_somewhere_world(self):
        exp = json.loads(EXPERIENCE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(exp.get("id"), "somewhere")
        worlds = exp.get("worlds") or []
        self.assertEqual(len(worlds), 1)
        self.assertEqual(worlds[0].get("slug"), "somewhere")
        self.assertEqual(exp.get("start_world"), worlds[0].get("id"))

    def test_experience_keeps_the_somewhere_clock(self):
        """SOMEWHERE wants a brisk arc: escalate around turn 2, peak around
        turn 5, so a short run still gets a shape instead of sitting at
        "normal" the whole way.

        That is the intent, and the marks are NOT those turn numbers. They are
        threat POINTS, and a turn adds one — except a SCAN tap (MOVE TO /
        INTERACT), which adds 1 + MAX_RISK_THREAT_BOOST = 2, and SCAN taps are
        how the game is mostly played. So the arc above is delivered by 4 / 9,
        which is exactly what experience_store.PRODUCT_* and the editor's own
        help text say SOMEWHERE uses.

        This test used to assert 2 / 5 — the turn numbers, entered into a
        points field. Live data had drifted further still, to 2 / 4. Played,
        that put a run at "escalating" on turn 1 and "critical" on turn 2, and
        then left it there: a 9-turn harness run finished with 8 turns at
        critical, UNLUCKY on most of them, and a turn-2 consequence that
        invented a gunshot wound "where you were grazed earlier" to satisfy the
        critical beat directive. The ladder was gone before the story started.
        """
        import experience_store as xs
        exp = json.loads(EXPERIENCE_PATH.read_text(encoding="utf-8"))
        threat = exp.get("threat") or {}
        self.assertEqual(threat.get("escalate_at"), xs.PRODUCT_ESCALATE_AT)
        self.assertEqual(threat.get("critical_at"), xs.PRODUCT_CRITICAL_AT)
        self.assertIn("BEAT:", str(threat.get("beat_normal") or ""))
        self.assertIn("BEAT:", str(threat.get("beat_escalating") or ""))
        self.assertIn("BEAT:", str(threat.get("beat_critical") or ""))

    def test_the_clock_delivers_the_arc_it_is_written_for(self):
        """The arithmetic, pinned — so the next person to retune the marks can
        see what they buy instead of guessing in turns."""
        import engine
        import experience_store as xs
        per_scan_turn = 1 + engine.MAX_RISK_THREAT_BOOST

        def arrives(mark):
            return -(-mark // per_scan_turn)  # ceil

        self.assertEqual(arrives(xs.PRODUCT_ESCALATE_AT), 2)
        self.assertEqual(arrives(xs.PRODUCT_CRITICAL_AT), 5)


class TestPlayBindsSomewhere(unittest.TestCase):
    def test_this_machine_play_is_somewhere(self):
        import experience_store as xs
        self.assertEqual(xs.get_active_slug(), "somewhere")

    def test_factory_slug_is_somewhere_when_shipped_file_exists(self):
        import experience_store as xs
        self.assertTrue(xs.shipped_experience_exists())
        self.assertEqual(xs.factory_slug(), xs.SHIPPED_SLUG)
        self.assertEqual(xs.SHIPPED_SLUG, "somewhere")

    def test_active_defaults_to_somewhere_without_pointer(self):
        import experience_store as xs
        active = xs.EXPERIENCES_DIR / ".active"
        saved = active.read_text(encoding="utf-8") if active.exists() else None
        try:
            if active.exists():
                active.unlink()
            self.assertEqual(xs.get_active_slug(), "somewhere")
            exp = xs.get_experience()
            start = xs.world_by_id(exp, exp.get("start_world") or "")
            self.assertIsNotNone(start)
            self.assertEqual(start.get("slug"), "somewhere")
        finally:
            if saved is None:
                if active.exists():
                    active.unlink()
            else:
                active.write_text(saved, encoding="utf-8")

    def test_world_store_can_read_the_snapshot(self):
        import worlds_store as ws
        data = ws.get_world("somewhere")
        self.assertEqual(data.get("name"), "SOMEWHERE")
        self.assertIn("world_initial_state", data.get("prompts") or {})

    def test_generic_harness_is_not_somewhere(self):
        import worlds_store as ws
        text = ws.HARNESS_PATH.read_text(encoding="utf-8")
        self.assertTrue(ws.HARNESS_PATH.is_file())
        self.assertNotIn("Horizon", text)
        self.assertNotIn("Four Corners", text)
        self.assertNotIn("Jason Fleece", text)
        blank = ws.create_blank_world("Harness Check")
        try:
            brief = (ws.get_world(blank["slug"]).get("prompts") or {}).get(
                "world_initial_state") or ""
            self.assertNotIn("Horizon", brief)
        finally:
            ws.delete_world(blank["slug"])

    def test_play_catalog_hides_leftover_default_tile(self):
        import experience_store as xs
        rows = xs.list_experiences(play_catalog=True)
        ids = {r["id"] for r in rows}
        self.assertIn("somewhere", ids)
        self.assertNotIn("default", ids)


class TestShipLayout(unittest.TestCase):
    """A player folder gets the freeze, not this machine's author Worlds."""

    def test_runtime_modules_are_on_disk(self):
        from tools.ship_layout import runtime_modules
        names = runtime_modules()
        self.assertIn("experience_store", names)
        self.assertIn("world_frames", names)
        self.assertIn("billing", names)
        self.assertIn("keys_store", names)

    def test_factory_sources_include_the_somewhere_freeze(self):
        from tools.ship_layout import factory_sources
        names = {p.name for p in factory_sources()}
        self.assertIn("somewhere.json", names)

    def test_stamp_factory_does_not_copy_author_leftovers(self):
        import tempfile
        from tools.ship_layout import stamp_factory
        with tempfile.TemporaryDirectory() as raw:
            dest = Path(raw)
            copied = stamp_factory(dest)
            self.assertTrue((dest / "experiences" / "somewhere.json").is_file())
            self.assertTrue((dest / "worlds" / "somewhere.json").is_file())
            self.assertFalse((dest / "experiences" / ".active").exists())
            self.assertFalse((dest / "experiences" / "default.json").exists())
            self.assertFalse((dest / "worlds" / "new-level.json").exists())
            live = (dest / "prompts" / "simulation_prompts.json").read_text(encoding="utf-8")
            self.assertIn("Horizon", live)
            self.assertIn("Four Corners", live)
            self.assertNotIn("experiences/default.json", copied)


if __name__ == "__main__":
    unittest.main()
