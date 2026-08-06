"""
test_concurrent_sessions.py — one player must never stall another.

The failure this guards against: scene-image generation held the GLOBAL
TURN_LOCK for its entire 10-30s render, and TURN_LOCK is also what /api/reset
and /api/choose hold. So one player's intro still blocked every other player's
reset — their HTTP request sat waiting on a lock held by a stranger's render.

The client `await`s /api/reset before it can draw anything, so a second player
opening the game saw the loader parked on its first step over a black screen
until someone else's image finished. With only a handful of gunicorn threads
(production runs one worker with 4), a few simultaneous opens wedged the
service entirely.

Run with:
    python3 -m unittest test_concurrent_sessions -v
"""

import os
import shutil
import threading
import time
import unittest

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("ANTHROPIC_API_KEY", "")

import engine

SESSION_A = "concurrency-a"
SESSION_B = "concurrency-b"
RENDER_S = 1.5


class TestSceneImageDoesNotBlockOtherPlayers(unittest.TestCase):
    def setUp(self):
        engine.WORLD_IMAGE_ENABLED = True
        self._real_gen = engine._gen_image
        self.started = threading.Event()
        self.release = threading.Event()

        def slow_gen_image(*a, **kw):
            self.started.set()
            self.release.wait(10)
            return None  # a blocked/failed render; we only care about locking

        engine._gen_image = slow_gen_image

        for sid in (SESSION_A, SESSION_B):
            st = engine._load_state(sid)
            st.update({"turn_count": 1, "feed_log": [],
                       "player_state": {"alive": True, "health": 100}})
            engine._save_state(st, sid)

    def tearDown(self):
        engine._gen_image = self._real_gen
        self.release.set()
        for sid in (SESSION_A, SESSION_B):
            shutil.rmtree(engine._get_session_root(sid), ignore_errors=True)

    def _render(self, session_id):
        return threading.Thread(
            target=engine._generate_and_append_scene_image,
            kwargs=dict(caption="c", dispatch="d", choice="ch", frame_idx=1,
                        world_prompt="w", session_id=session_id, write_history=False),
            daemon=True,
        )

    def test_a_render_does_not_hold_the_global_turn_lock(self):
        """A render in flight must leave TURN_LOCK free, or every other
        player's /api/reset and /api/choose blocks behind it."""
        t = self._render(SESSION_A)
        t.start()
        self.assertTrue(self.started.wait(5), "render never started")
        try:
            acquired = engine.TURN_LOCK.acquire(blocking=False)
            if acquired:
                engine.TURN_LOCK.release()
            self.assertTrue(
                acquired,
                "TURN_LOCK is held during a scene render — another player's "
                "reset/turn would block behind this image",
            )
        finally:
            self.release.set()
            t.join(10)

    def test_two_sessions_render_concurrently(self):
        """Two players opening at once must render in parallel, not in series."""
        started_b = threading.Event()
        real_gen = engine._gen_image
        first = {"sid": None}

        def gen(*a, **kw):
            sid = kw.get("session_id")
            if first["sid"] is None:
                first["sid"] = sid
                self.started.set()
                self.release.wait(10)
            else:
                started_b.set()
            return None

        engine._gen_image = gen
        ta, tb = self._render(SESSION_A), self._render(SESSION_B)
        try:
            ta.start()
            self.assertTrue(self.started.wait(5), "first render never started")
            tb.start()
            # B must get all the way into its own render while A is still stuck.
            self.assertTrue(
                started_b.wait(5),
                "a second session's render waited on the first — players are "
                "serialized behind each other's images",
            )
        finally:
            engine._gen_image = real_gen
            self.release.set()
            ta.join(10)
            tb.join(10)

    def test_same_session_renders_stay_serialized(self):
        """…but two renders for ONE session must still not interleave: they
        both write that session's history entry."""
        second_entered = threading.Event()
        real_gen = engine._gen_image
        calls = []

        def gen(*a, **kw):
            calls.append(1)
            if len(calls) == 1:
                self.started.set()
                self.release.wait(5)
            else:
                second_entered.set()
            return None

        engine._gen_image = gen
        t1, t2 = self._render(SESSION_A), self._render(SESSION_A)
        try:
            t1.start()
            self.assertTrue(self.started.wait(5))
            t2.start()
            self.assertFalse(
                second_entered.wait(1.0),
                "two renders for the SAME session ran concurrently",
            )
            self.release.set()
            self.assertTrue(second_entered.wait(5), "second render never ran")
        finally:
            engine._gen_image = real_gen
            self.release.set()
            t1.join(10)
            t2.join(10)


class TestIntroImageOrdering(unittest.TestCase):
    """The intro render must be spawned AFTER reset persists the new state.

    It appends its scene_image by reloading state under the lock, so spawning
    it before reset's own _save_state lets that save clobber the appended item
    — and losing the intro frame is a black screen the run never recovers from
    (a seed-locked world model can't start without it). This used to be masked
    by the global TURN_LOCK; per-session renders make the ordering explicit.
    """

    def setUp(self):
        from pathlib import Path
        self.src = Path(__file__).parent.joinpath("engine.py").read_text(encoding="utf-8")

    def test_intro_spawn_is_deferred_out_of_the_generator(self):
        gen = self.src.split("def generate_intro_turn_feed_items(", 1)[1].split("\n# --- Internal Reset", 1)[0]
        self.assertIn("spawn_image", gen)
        self.assertIn("return intro_items, intro_image_kwargs", gen)

    def test_reset_spawns_the_intro_image_after_saving(self):
        reset = self.src.split("def _perform_game_reset(", 1)[1].split("\ndef api_reset", 1)[0]
        self.assertIn("spawn_image=False", reset)
        save_at = reset.index("_save_state(new_state, SID)")
        spawn_at = reset.index("_spawn_scene_image_async(**intro_image_kwargs)")
        self.assertLess(save_at, spawn_at,
                        "the intro render must be spawned after the state save")


if __name__ == "__main__":
    unittest.main(verbosity=2)
