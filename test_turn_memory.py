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
        # The block is finished history, and has to say so. Headed "PREVIOUS
        # BEAT" with no framing, the consequence model read it as a cue and
        # continued it instead of playing the choice.
        self.assertIn("ALREADY PLAYED", block)
        self.assertIn("Do not re-narrate", block)
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

    def test_the_slate_waits_for_the_frame_and_for_the_read_of_it(self):
        """The slate does not run until there is a frame to run it off.

        This used to assert the opposite — that the vision read overlapped the
        choice call — on the reasoning that the slate has the picture attached
        and so does not need the vision TEXT. bugs/20260917_153517 is that
        reasoning failing: choices came back at 15:35:11 and the read of the same
        frame completed at 15:35:11, after them, so the slate ran with
        `image_description=""` and its only scene text was the cumulative entity
        list — which still held "rusted truck" two turns after the player vaulted
        the fence and left it on the far side. It offered the truck.
        """
        src = inspect.getsource(engine._advance_turn_choices_deferred_impl)
        start_at = src.index("_vision_thread.start()")
        frame_wait_at = src.index("_await_frame_on_disk(analysis_img_url")
        absorb_at = src.index("_absorb_vision(\n                CHOICE_VISION_WAIT")
        choices_at = src.index("next_choices = generate_choices(")
        hist_at = src.index("history_entry = {")

        # Started early so the read overlaps the render bookkeeping, but WAITED
        # ON before the slate is written.
        self.assertLess(start_at, frame_wait_at)
        self.assertLess(frame_wait_at, absorb_at)
        self.assertLess(absorb_at, choices_at)
        self.assertLess(choices_at, hist_at)

    def test_the_slate_is_handed_what_the_frame_actually_shows(self):
        """`image_description=""` was the bug. The read is in hand by the time the
        slate runs, so it goes in — with the spatial compass, which is the line
        that makes an option about somewhere the player has left obviously wrong."""
        src = inspect.getsource(engine._advance_turn_choices_deferred_impl)
        # The generate_choices CALL, not the whole function: the comment above it
        # quotes the old `image_description=""` to explain what went wrong.
        call = src.split("next_choices = generate_choices(", 1)[1].split("\n        )", 1)[0]
        self.assertIn("image_description=scene_text_for_slate", call)
        self.assertNotIn('image_description=""', call)
        # The compass is part of that text, not just the prose description.
        scene_text = src.split("scene_text_for_slate = ", 1)[1][:400]
        self.assertIn("_spatial_compass_turn", scene_text)

    def test_a_frame_that_never_arrives_does_not_wedge_the_turn(self):
        """Both waits are bounded, and a slate is still produced. A player left
        looking at a picture with no buttons is worse than an imperfect slate."""
        self.assertGreater(engine.CHOICE_FRAME_WAIT, 0)
        self.assertGreater(engine.CHOICE_VISION_WAIT, 0)
        # The player feels this one, so it must not be the 35s history budget.
        self.assertLess(engine.CHOICE_VISION_WAIT, engine.VISION_JOIN_TIMEOUT)

    def test_a_truncated_frame_is_not_treated_as_readable(self):
        """A file that exists but is still being written attaches as a corrupt
        image, which is worse than waiting for it."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "half-written.png"
            empty.write_bytes(b"")
            self.assertEqual(engine._await_frame_on_disk(str(empty), 0.2), "")
            empty.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
            self.assertTrue(engine._await_frame_on_disk(str(empty), 0.2))

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
            dispatch, vision, alive, extras, _relocated = engine._generate_combined_dispatches(
                "Force the door",
                {"world_prompt": "A yard.", "recent_events": ["Vault -> rusted chain-link rattles once"]},
            )
        self.assertTrue(alive)
        self.assertIn("Chain-link immediately ahead.", vision)
        self.assertEqual(extras, [])
        prompt, kw = sent[0]
        self.assertIn("ALREADY PLAYED", prompt)
        self.assertIn("rusted chain-link rattles once", prompt)
        self.assertNotIn("fourth field `next_choices`", prompt)
        self.assertIn("THE ACTION COMPLETED", prompt)
        self.assertIn("Force the door", prompt)
        self.assertEqual(kw.get("response_schema"), engine._CONSEQUENCE_RESPONSE_SCHEMA)

    def test_finished_beats_are_read_before_the_choice_not_after(self):
        """Ordering is the whole fix. With the played-out beats sitting last —
        immediately above the output contract — the model continued them:
        "Kick through the fence mesh" came back as "Your kick buckles the
        hood", which was the previous turn's action. The choice has to be the
        last thing it reads before it writes."""
        sent = []

        def fake_ask(prompt, **kw):
            sent.append(prompt)
            return '{"dispatch": "d", "visual_scene": "v", "player_alive": true}'

        with patch.object(engine, "_ask", fake_ask):
            engine._generate_combined_dispatches(
                "Force the door",
                {"world_prompt": "A yard.", "recent_events": ["Vault -> rusted chain-link rattles once"]},
            )
        prompt = sent[0]
        self.assertLess(
            prompt.index("ALREADY PLAYED"), prompt.index("PLAYER CHOICE"),
            "finished beats must be framed as history ABOVE the choice, not read last",
        )

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
            dispatch, vision, alive, _, _relocated = engine._generate_combined_dispatches(
                "Force the door", {"world_prompt": "A yard."},
            )
        self.assertIn("drop answers back", dispatch)
        self.assertIn("Chain-link immediately ahead.", vision)
        self.assertNotEqual(dispatch, vision)

    def test_narrative_falls_back_to_the_caption_when_withheld(self):
        def fake_ask(prompt, **kw):
            return '{"visual_scene": "Chain-link immediately ahead.", "player_alive": true}'

        with patch.object(engine, "_ask", fake_ask):
            dispatch, vision, _, _, _relocated = engine._generate_combined_dispatches(
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
