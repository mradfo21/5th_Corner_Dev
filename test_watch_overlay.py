"""Watch overlay state is derived from recorded choices / scan — not empty.

The painter lives in the browser (WatchOverlay in standalone.js). The
contract it paints from is render_jobs.overlay_state / overlay_state_at_time,
so these tests lock that derivation without a browser.
"""

import unittest

import render_jobs


def _choice_beat(**extra):
    beat = {
        "turn": 1,
        "kind": "choice",
        "phase": "choosing",
        "choice": "Run",
        "choices": ["Run", "Hide", "Wait"],
        "boxes": [],
        "pick": None,
        "narrative": "",
        "subject": None,
    }
    beat.update(extra)
    return beat


def _scan_beat(**extra):
    beat = {
        "turn": 2,
        "kind": "scan_move",
        "phase": "scanned",
        "choice": "Move to the crate.",
        "subject": "crate",
        "choices": ["Wait", "Look around"],
        "narrative": "You close on the crate.",
        "boxes": [
            {"label": "crate", "cx": 0.4, "cy": 0.6, "w": 0.2, "h": 0.15},
            {"label": "lamp", "cx": 0.7, "cy": 0.3, "w": 0.1, "h": 0.2},
        ],
        "pick": {"label": "crate", "cx": 0.4, "cy": 0.6, "w": 0.2, "h": 0.15},
    }
    beat.update(extra)
    return beat


class TestOverlayStateFromRecordedBeats(unittest.TestCase):

    def test_missing_beat_is_an_empty_overlay(self):
        state = render_jobs.overlay_state(None)
        self.assertEqual(state["boxes"], [])
        self.assertEqual(state["choices"], [])
        self.assertEqual(state["selected"], "")
        self.assertEqual(state["caption"], "")

    def test_choosing_shows_the_slate_and_no_pick(self):
        state = render_jobs.overlay_state(_choice_beat())
        self.assertEqual(state["choices"], ["Run", "Hide", "Wait"])
        self.assertEqual(state["selected"], "")
        self.assertIsNone(state["pick"])
        self.assertEqual(state["boxes"], [])

    def test_chosen_locks_the_picked_line(self):
        state = render_jobs.overlay_state(_choice_beat(phase="chosen"))
        self.assertEqual(state["choices"], ["Run", "Hide", "Wait"])
        self.assertEqual(state["selected"], "Run")

    def test_scan_shows_boxes_not_the_button_slate(self):
        state = render_jobs.overlay_state(_scan_beat())
        self.assertEqual(len(state["boxes"]), 2)
        self.assertEqual(state["boxes"][0]["label"], "crate")
        self.assertEqual(state["choices"], [])
        self.assertEqual(state["selected"], "")
        self.assertEqual(state["pick"]["label"], "crate")

    def test_scan_pick_flares_when_chosen(self):
        state = render_jobs.overlay_state(_scan_beat(phase="chosen"))
        self.assertEqual(state["pick"]["label"], "crate")
        self.assertEqual(state["selected"], "Move to the crate.")
        self.assertIn("crate", state["caption"].lower())

    def test_resolved_narrator_line_is_a_caption(self):
        state = render_jobs.overlay_state(_choice_beat(
            phase="resolved", narrative="Dust lifts off the road."))
        self.assertEqual(state["caption_kind"], "narrator")
        self.assertEqual(state["caption"], "Dust lifts off the road.")

    def test_camp_kind_is_a_caption_not_a_new_mode(self):
        state = render_jobs.overlay_state(_choice_beat(
            kind="camp", phase="chosen", choice="Sit by the fire",
            subject="fire"))
        self.assertEqual(state["caption_kind"], "camp")


class TestOverlayPlaybackTimeline(unittest.TestCase):

    def setUp(self):
        self.beats = [
            _choice_beat(turn=1, phase="resolved"),
            _scan_beat(turn=2, phase="resolved"),
        ]

    def test_review_view_page_offers_choices(self):
        state = render_jobs.overlay_state_at_page(self.beats, 0, "review_video")
        self.assertEqual(state["choices"], ["Run", "Hide", "Wait"])
        self.assertEqual(state["selected"], "")

    def test_review_selected_page_locks_the_pick(self):
        state = render_jobs.overlay_state_at_page(self.beats, 2, "review_video")
        self.assertEqual(state["selected"], "Run")

    def test_review_skips_burned_in_choice_pages(self):
        state = render_jobs.overlay_state_at_page(
            self.beats, 1, "review_video", burned_in=True)
        self.assertEqual(state["choices"], [])
        self.assertEqual(state["selected"], "")

    def test_review_view_page_still_overlays_when_later_pages_are_burned_in(self):
        state = render_jobs.overlay_state_at_page(
            self.beats, 0, "review_video", burned_in=True)
        self.assertEqual(state["choices"], ["Run", "Hide", "Wait"])

    def test_view_video_plays_offer_then_pick(self):
        offer = render_jobs.overlay_state_at_time(
            self.beats, 0.1, variant="view_video", flipbook_page_s=0.5)
        pick = render_jobs.overlay_state_at_time(
            self.beats, 0.3, variant="view_video", flipbook_page_s=0.5)
        self.assertEqual(offer["choices"], ["Run", "Hide", "Wait"])
        self.assertEqual(offer["selected"], "")
        self.assertEqual(pick["selected"], "Run")

    def test_scan_turn_on_view_video_uses_boxes(self):
        state = render_jobs.overlay_state_at_time(
            self.beats, 0.6, variant="view_video", flipbook_page_s=0.5)
        self.assertEqual(len(state["boxes"]), 2)
        self.assertEqual(state["choices"], [])

    def test_empty_beats_stay_empty(self):
        state = render_jobs.overlay_state_at_time([], 1.0, variant="review_video")
        self.assertEqual(state["choices"], [])
        self.assertEqual(state["boxes"], [])


class TestClientWiresTheOverlay(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        root = Path(__file__).parent
        cls.js = (root / "static" / "js" / "standalone.js").read_text(
            encoding="utf-8", errors="replace")
        cls.html = (root / "templates" / "standalone.html").read_text(
            encoding="utf-8", errors="replace")
        cls.css = (root / "static" / "css" / "standalone.css").read_text(
            encoding="utf-8", errors="replace")

    def test_watch_player_paints_from_recorded_beats(self):
        self.assertIn("const WatchOverlay", self.js)
        self.assertIn("function stateFromBeat", self.js)
        self.assertIn("function stateAtTime", self.js)
        self.assertIn("WatchOverlay.stateAtTime(beats, t, spec)", self.js)
        self.assertIn("run.beats", self.js)
        self.assertIn('id="wp-overlay"', self.html)

    def test_choices_are_type_not_mint_slabs(self):
        self.assertIn(".watch-choice", self.css)
        self.assertIn("watch-choice-flare", self.css)
        # Spatial scan boxes stay; choice lines must not grow a 1px mint box.
        self.assertNotIn(".watch-choice {\n  border: 1px solid", self.css)
        self.assertNotIn("scan-tutorial", self.js)
        self.assertNotIn('id="scan-tutorial"', self.html)


if __name__ == "__main__":
    unittest.main()
