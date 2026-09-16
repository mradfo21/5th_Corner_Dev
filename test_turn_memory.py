"""Play loop: world updates, then image, then choices from that image.

No live Gemini. Proves the tape, the evolve input, the wait, and the order.
"""
from __future__ import annotations

import hashlib
import inspect
import unittest
from pathlib import Path
from unittest.mock import patch

import engine

ROOT = Path(__file__).resolve().parent
WORLD_PATH = ROOT / "worlds" / "somewhere.json"


def _world_hash() -> str:
    return hashlib.sha256(WORLD_PATH.read_bytes()).hexdigest()


class TestTapeHelpers(unittest.TestCase):
    def test_record_then_previous_beat_keeps_the_camera_phrase(self):
        phrase = "rusted chain-link rattles once"
        st = {}
        engine._record_recent_event(st, "Vault the fence", phrase)
        block = engine._previous_beat_block(st)
        self.assertIn(phrase, block)
        self.assertIn("PREVIOUS BEAT", block)
        self.assertEqual(len(st["recent_events"]), 1)

    def test_tape_caps_at_ten(self):
        st = {"recent_events": [f"old {i}" for i in range(10)]}
        engine._record_recent_event(st, "Go", "new yard")
        self.assertEqual(len(st["recent_events"]), 10)
        self.assertTrue(st["recent_events"][-1].endswith("new yard"))
        self.assertNotIn("old 0", st["recent_events"])


class TestLoopWiring(unittest.TestCase):
    def test_web_turn_overlaps_evolve_with_the_render(self):
        # World evolution no longer blocks the frame. The web turn DEFERS it to a
        # thread that overlaps the scene render, then JOINS it before Phase 2 so
        # the choice generator and the history entry read the EVOLVED
        # world_prompt / seen_elements — not the pre-turn values. skip_evolve
        # stays False: this is deferred-but-joined, not fire-and-forget.
        src = inspect.getsource(engine._process_turn_background)
        self.assertIn("defer_evolve=True", src)
        self.assertIn("skip_evolve=False", src)
        image_at = src.index("scene = _generate_and_append_scene_image")
        join_at = src.index("evolve_thread.join")
        choices_at = src.index("p2 = advance_turn_choices_deferred")
        # Evolve is joined after the render starts and before choices are built.
        self.assertLess(image_at, join_at)
        self.assertLess(join_at, choices_at)

    def test_web_turn_renders_image_before_choices(self):
        src = inspect.getsource(engine._process_turn_background)
        image_at = src.index("scene = _generate_and_append_scene_image")
        choices_at = src.index("p2 = advance_turn_choices_deferred")
        self.assertLess(image_at, choices_at)
        after_choices = src[choices_at:]
        self.assertNotIn("_spawn_scene_image_async", after_choices)
        self.assertNotIn("_spawn_scene_choices_reground", after_choices)
        self.assertIn("img_path", src[image_at:choices_at + 400])

    def test_choices_attach_the_frame_and_vision_runs_in_parallel(self):
        # The slate is grounded on the attached frame (image_url=analysis_img_url),
        # and the vision READ runs on a background thread in PARALLEL with the
        # choice call rather than as a serial pre-pass — then is joined before the
        # history entry (next turn's spatial anchor) is written.
        src = inspect.getsource(engine._advance_turn_choices_deferred_impl)
        self.assertIn("image_url=analysis_img_url", src)
        self.assertIn("_vision_thread", src)
        start_at = src.index("_vision_thread.start()")
        choices_at = src.index("next_choices = generate_choices(")
        join_at = src.index("_vision_thread.join")
        hist_at = src.index("history_entry = {")
        self.assertLess(start_at, choices_at)
        self.assertLess(join_at, hist_at)

    def test_evolve_input_is_the_narrative_not_the_dummy_diff(self):
        # The evolve prompt has separate CONSEQUENCE OF ACTION and VISION
        # ANALYSIS slots. Handing the caption to both grew the world state out
        # of a shot list, so the consequence slot takes the narrative.
        src = inspect.getsource(engine.advance_turn_image_fast)
        self.assertIn("consequence_summary = dispatch or vision_dispatch", src)
        self.assertNotIn(
            "consequence_summary = summarize_world_state_diff", src
        )

    def test_play_helpers_do_not_rewrite_the_snapshot(self):
        before = _world_hash()
        st = {}
        engine._record_recent_event(st, "Climb", "mesa wall fills the frame")
        engine._previous_beat_block(st)
        self.assertEqual(_world_hash(), before)


class TestConsequenceCall(unittest.TestCase):
    def test_prompt_asks_for_visual_scene_not_next_choices(self):
        sent = []

        def fake_ask(prompt, **kw):
            sent.append((prompt, kw))
            return '{"visual_scene": "Chain-link immediately ahead.", "player_alive": true}'

        with patch.object(engine, "_ask", fake_ask):
            dispatch, vision, alive, extras = engine._generate_combined_dispatches(
                "Force the door",
                {"world_prompt": "A yard.", "recent_events": ["Vault -> rusted chain-link rattles once"]},
            )
        self.assertTrue(alive)
        self.assertIn("Chain-link immediately ahead.", vision)
        self.assertEqual(extras, [])
        prompt, kw = sent[0]
        self.assertIn("PREVIOUS BEAT", prompt)
        self.assertIn("rusted chain-link rattles once", prompt)
        self.assertNotIn("fourth field `next_choices`", prompt)
        self.assertIn("THE ACTION COMPLETED", prompt)
        self.assertIn("Force the door", prompt)
        self.assertEqual(kw.get("response_schema"), engine._CONSEQUENCE_RESPONSE_SCHEMA)

    def test_narrative_and_caption_are_two_channels(self):
        # The schema once declared only visual_scene, so structured output made
        # `dispatch` unanswerable and the parse aliased the two. The story log
        # then printed the image caption for the whole run.
        self.assertIn("dispatch", engine._CONSEQUENCE_RESPONSE_SCHEMA["required"])

        def fake_ask(prompt, **kw):
            return (
                '{"dispatch": "The frame gives under your boot and the drop '
                'answers back.", "visual_scene": "Chain-link immediately '
                'ahead.", "player_alive": true}'
            )

        with patch.object(engine, "_ask", fake_ask):
            dispatch, vision, alive, _ = engine._generate_combined_dispatches(
                "Force the door", {"world_prompt": "A yard."},
            )
        self.assertIn("drop answers back", dispatch)
        self.assertIn("Chain-link immediately ahead.", vision)
        self.assertNotEqual(dispatch, vision)

    def test_narrative_falls_back_to_the_caption_when_withheld(self):
        def fake_ask(prompt, **kw):
            return '{"visual_scene": "Chain-link immediately ahead.", "player_alive": true}'

        with patch.object(engine, "_ask", fake_ask):
            dispatch, vision, _, _ = engine._generate_combined_dispatches(
                "Force the door", {"world_prompt": "A yard."},
            )
        self.assertEqual(dispatch, vision)

    def test_sync_evolve_gets_the_narrative_and_the_camera_beat(self):
        src = inspect.getsource(engine.advance_turn_image_fast)
        after = src[src.index("consequence_summary = dispatch or vision_dispatch"):]
        self.assertIn("evolve_world_state", after)
        self.assertIn("vision_description=vision_dispatch", after)


class TestStudioRedirect(unittest.TestCase):
    def test_studio_opens_create(self):
        import api
        client = api.app.test_client()
        res = client.get("/studio", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertIn("mode=create", res.headers.get("Location", ""))


if __name__ == "__main__":
    unittest.main()
