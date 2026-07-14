"""
test_feed_engine.py — regression coverage for the legacy feed-based turn
loop (api_reset/api_feed/api_choose/api_regenerate_choices in engine.py)
that powers the standalone UI (/standalone).

Deliberately runs with LLM_ENABLED left at its real production default
(True) and ai_provider_manager NOT overridden to "mock" — this is the
exact code path `run_local.py --mock` / test_providers.py do NOT exercise
(--mock sets engine.LLM_ENABLED = False, which short-circuits before ever
reaching the bug this test guards against). No API key is required: with
an empty/missing GEMINI_API_KEY, the real network calls fail fast (auth
error, not a timeout) and the engine's own existing "Signal interrupted"
fallbacks kick in — which is sufficient to exercise the state-management
code path end to end.

This specifically guards against a regression where the world-evolution
step accidentally reloaded and replaced the ENTIRE in-memory turn state
from an unrelated session file, silently discarding the feed_log,
chaos_level, and last_choice accumulated earlier in the same turn. That
bug was invisible under --mock (LLM_ENABLED=False skips the code path
entirely) and only appeared once wired up against a real backend -
exactly what happens when this is deployed to Render.

Run with:
    python3 -m unittest test_feed_engine -v
"""

import os
import time
import unittest

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("ANTHROPIC_API_KEY", "")

import ai_provider_manager
import engine
import api


class TestFeedEngineContinuity(unittest.TestCase):
    def setUp(self):
        self.client = api.app.test_client()
        # Explicitly exercise the REAL (non-mock) code path: no backend
        # override, LLM_ENABLED left True (production default). Image
        # generation is disabled purely to keep the test fast/offline;
        # that flag is orthogonal to the bug this test guards against.
        ai_provider_manager.set_backend_override(None)
        os.environ.pop("STORYGEN_BACKEND", None)
        engine.LLM_ENABLED = True
        engine.IMAGE_ENABLED = False
        engine.WORLD_IMAGE_ENABLED = False

    def _reset(self):
        resp = self.client.post("/api/reset")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def _choose(self, choice_text, context_id):
        resp = self.client.post("/api/choose", json={"choice": choice_text, "context_item_id": context_id})
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def _feed(self, since_id=0):
        resp = self.client.get(f"/api/feed?since_id={since_id}")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def _status(self):
        resp = self.client.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def _wait_for_next_prompt(self, since_id, timeout_s=25):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            items = self._feed(since_id)
            if any(i.get("type") == "player_choice_prompt" for i in items):
                return items
            if any(i.get("type") == "error_event" for i in items):
                # Still a valid terminal state for this test's purposes —
                # the turn resolved (with an error) rather than hanging.
                return items
            time.sleep(0.2)
        self.fail(f"Timed out waiting for turn resolution (since_id={since_id})")

    def _advance_turn(self, choice_text, since_id, timeout_s=25):
        """POST /api/choose and wait for the turn to FULLY resolve.

        The background thread appends the next player_choice_prompt to the
        feed before its own trailing steps finish (death check, history.json
        write, turn_count increment + final save) — waiting only for the
        prompt to appear is not enough to guarantee those trailing steps are
        done, which matters both for assertions immediately after this call
        and for test isolation (a lingering thread finishing after the next
        test's reset() would stomp the fresh reset with stale data).
        """
        turn_before = self._status().get("turn", 0)
        self._choose(choice_text, since_id)
        items = self._wait_for_next_prompt(since_id, timeout_s=timeout_s)
        deadline = time.time() + 10
        while time.time() < deadline:
            if self._status().get("turn", 0) > turn_before:
                break
            time.sleep(0.15)
        return items

    def test_feed_history_persists_across_two_turns(self):
        """The exact regression: feed_log must accumulate, not get replaced."""
        initial = self._reset()
        self.assertTrue(any(i["type"] == "player_choice_prompt" for i in initial))
        last_id = initial[-1]["id"]

        self._advance_turn("Move forward carefully", last_id)

        full_feed = self._feed(0)
        self.assertGreater(len(full_feed), len(initial), "turn 1 must ADD to the feed, not replace it")
        ids_seen = [i["id"] for i in full_feed]
        self.assertEqual(ids_seen, sorted(ids_seen), "feed ids must stay monotonically increasing")
        self.assertEqual(len(ids_seen), len(set(ids_seen)), "feed ids must stay unique (no id-counter reset)")
        # The original reset items must still be present verbatim.
        for original_item in initial:
            self.assertIn(original_item["id"], ids_seen)

        last_id = full_feed[-1]["id"]
        self._advance_turn("Crouch low and scan the area", last_id)

        full_feed_2 = self._feed(0)
        self.assertGreater(len(full_feed_2), len(full_feed), "turn 2 must ADD to the feed, not replace it")

    def test_turn_count_advances(self):
        initial = self._reset()
        last_id = initial[-1]["id"]
        self.assertEqual(self._status()["turn"], 0)

        self._advance_turn("Move forward carefully", last_id)
        self.assertEqual(self._status()["turn"], 1)

    def test_chaos_level_persists_not_reverted(self):
        """generate_and_apply_choice() bumps chaos_level; the subsequent
        world-evolution step must not silently revert it to 0."""
        initial = self._reset()
        last_id = initial[-1]["id"]
        self.assertEqual(self._status()["chaos"], 0)

        self._advance_turn("Move forward carefully", last_id)
        self.assertGreaterEqual(self._status()["chaos"], 1)

    def test_realtime_concurrent_write_not_clobbered_by_turn(self):
        """Regression for the realtime SCAN/observe freeze.

        A concurrent state write that lands DURING a turn — e.g. the realtime
        /api/observe fast-path saving the live video frame as current_image_url,
        or a scene_image feed item — must NOT be clobbered by the turn thread
        appending its feed items from a state snapshot it loaded BEFORE taking
        the lock. That lost-update race made realtime turns silently drop feed
        items, so the browser polled an empty feed forever (freeze) and the
        video never re-anchored (went black). SCAN made it reproducible because
        it keeps the live video active, so /api/observe + /api/detect fire
        during the turn."""
        initial = self._reset()
        last_id = initial[-1]["id"]

        import items as items_mod
        orig_detect = items_mod.detect_item_pickups
        injected = {"done": False}

        def racing_detect(dispatch, inventory):
            # Runs inside _process_turn_background, in the exact window between
            # its out-of-lock state load and its feed_log append. Simulate a
            # realtime writer saving state concurrently (a scene_image feed item
            # + current_image_url), exactly as /api/observe / the scene-image
            # thread do in realtime mode.
            if not injected["done"]:
                injected["done"] = True
                with engine.WORLD_STATE_LOCK:
                    st = engine._load_state("default")
                    st["current_image_url"] = "/images/RACE_MARKER.png"
                    st.setdefault("feed_log", []).append(
                        engine.create_feed_item(
                            type="scene_image", content="",
                            image_url="/images/RACE_MARKER.png",
                        )
                    )
                    engine._save_state(st, "default")
                    engine.state = st
            return orig_detect(dispatch, inventory)

        items_mod.detect_item_pickups = racing_detect
        try:
            self._advance_turn("Move forward carefully", last_id)
        finally:
            items_mod.detect_item_pickups = orig_detect

        feed = self._feed(0)
        # The concurrent realtime write survived the turn's feed appends...
        self.assertTrue(
            any(i.get("image_url") == "/images/RACE_MARKER.png" for i in feed),
            "a concurrent realtime state write was clobbered by the turn thread",
        )
        # ...and the turn still resolved with a narrative + a fresh choice prompt
        # (i.e. the client would NOT freeze on an empty feed).
        types = [i.get("type") for i in feed]
        self.assertIn("narrative_event", types)
        self.assertIn("player_choice_prompt", types)
        # ids remain unique + monotonic (no lost/duplicated ids from the race).
        ids = [i["id"] for i in feed]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), len(set(ids)))


class TestBlockedImageGracefulRecovery(unittest.TestCase):
    """A safety-blocked / failed image must NOT silently drop the scene beat.

    Regression for the reported "reactor/lingbot freaks out, refusing to draw
    anything but black … went back to stills but selecting an action didn't
    change scenes." When the still is content-filtered, image generation returns
    None. Previously _generate_and_append_scene_image returned early and appended
    NOTHING — so the turn's ceremony parked on the guide-image step forever and
    stills mode showed no feedback. Now it emits a scene_image beat WITHOUT an
    image (metadata.blocked=True) so the client resolves the turn and realtime
    keeps steering off the prompt.
    """

    def setUp(self):
        ai_provider_manager.set_backend_override(None)
        os.environ.pop("STORYGEN_BACKEND", None)
        self._orig_world_image = engine.WORLD_IMAGE_ENABLED
        self._orig_gen_image = engine._gen_image
        engine.WORLD_IMAGE_ENABLED = True  # take the real append path...
        engine._gen_image = lambda *a, **k: None  # ...but force a "blocked" image

    def tearDown(self):
        engine.WORLD_IMAGE_ENABLED = self._orig_world_image
        engine._gen_image = self._orig_gen_image

    def test_blocked_image_emits_signal_lost_scene_beat(self):
        api.app.test_client().post("/api/reset")
        st = engine._load_state("default")
        before = len(st.get("feed_log", []))

        ret = engine._generate_and_append_scene_image(
            caption="a quiet ridge at dusk",
            dispatch="You crest the ridge.",
            choice="Move forward",
            frame_idx=1,
            world_prompt=st.get("world_prompt", ""),
            session_id="default",
            write_history=False,
        )
        # Returns None (no image), but must NOT have gone silent.
        self.assertIsNone(ret, "a blocked image still returns None to callers")

        st = engine._load_state("default")
        feed = st.get("feed_log", [])
        self.assertGreater(len(feed), before, "a blocked image must still append a scene beat")

        beat = feed[-1]
        self.assertEqual(beat.get("type"), "scene_image")
        self.assertIsNone(beat.get("image_url"), "blocked scene beat carries no image")
        meta = beat.get("metadata") or {}
        self.assertTrue(meta.get("blocked"), "blocked scene beat is flagged so the client can surface it")
        self.assertTrue((meta.get("prompt") or "").strip(),
                        "the render prompt rides along so realtime keeps steering")


if __name__ == "__main__":
    unittest.main()
