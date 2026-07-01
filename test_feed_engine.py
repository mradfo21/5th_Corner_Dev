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


if __name__ == "__main__":
    unittest.main()
