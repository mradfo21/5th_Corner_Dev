"""
test_world_drift.py — coverage for the ambient world-drift tick: the text-only
simulation step that runs BETWEEN turns so the realtime world model keeps
receiving updates instead of holding the prompt from the last choice.

What matters here (and what broke before it existed):

  * A drift is a PROMPT-ONLY re-steer. If its feed item ever carries an
    image_url — or the client routes it through the normal scene path — a
    seed-locked model (LingBot) re-stages the entire world, and a Happy Oyster
    adventure world gets rebuilt, for an atmospheric beat. That's a black
    re-anchor every 20 seconds instead of a world that breathes.
  * A drift must never race a real turn for the world prompt.
  * The pacing/budget must be enforced on the SERVER: the client asks on a
    timer, so a client that asks too often (or two tabs on one session) must
    not be able to buy extra LLM calls.
  * Drift beats must be folded into the next world-evolution rewrite, or the
    world state silently contradicts changes the player already watched happen.

No network: `engine._ask` is stubbed, so these run offline.

Run with:
    python3 -m unittest test_world_drift -v
"""

import os
import shutil
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("ANTHROPIC_API_KEY", "")

import api
import engine

ROOT = Path(__file__).parent.resolve()

SESSION_ID = "drift-test"


def _discard_test_session():
    shutil.rmtree(engine._get_session_root(SESSION_ID), ignore_errors=True)


BASE_PROMPT = (
    "Handheld 1993 VHS first-person view. A cracked concrete service corridor "
    "lit by one failing fluorescent tube, sand drifted across the floor."
)


class TestWorldDriftTick(unittest.TestCase):
    def setUp(self):
        engine.LLM_ENABLED = True
        # Drift ships OFF by default (production runs one gunicorn worker with a
        # handful of threads); these tests exercise it explicitly enabled.
        engine.WORLD_DRIFT_ENABLED = True
        engine._drift_worker_active = False
        self._real_ask = engine._ask
        self.ask_calls = []

        def fake_ask(prompt, **kwargs):
            self.ask_calls.append(prompt)
            return "The fluorescent tube dies and dust settles across the floor."

        engine._ask = fake_ask
        self._seed_state()

    def tearDown(self):
        engine._ask = self._real_ask
        _discard_test_session()

    def _seed_state(self, **overrides):
        """A session parked at a decision point with a rendered scene."""
        st = engine._load_state(SESSION_ID)
        st.update({
            "turn_count": 3,
            "current_render_base": BASE_PROMPT,
            "current_render_prompt": BASE_PROMPT + " Motion: the view shifts as you step forward.",
            "current_phase": "escalating",
            "threat_level": 4,
            "last_choice": "Step deeper into the corridor",
            "player_state": {"alive": True, "health": 90},
            "feed_log": [],
            "ambient_beats": [],
            "drift_count": 0,
            "last_drift_ts": 0,
        })
        st.update(overrides)
        engine._save_state(st, SESSION_ID)
        return st

    def _tick(self):
        return engine.world_drift_tick(SESSION_ID)

    # ── the happy path ────────────────────────────────────────────────────────

    def test_tick_appends_a_prompt_only_drift_item(self):
        result = self._tick()
        self.assertTrue(result["ok"], result)

        st = engine._load_state(SESSION_ID)
        drifts = [i for i in st["feed_log"] if i.get("type") == "world_drift"]
        self.assertEqual(len(drifts), 1)
        item = drifts[0]

        # PROMPT-ONLY: no image, no transition. An image here re-stages the
        # whole live world for an ambient beat.
        self.assertIsNone(item.get("image_url"))
        self.assertFalse(item["metadata"]["hard_transition"])
        self.assertTrue(item["metadata"]["drift"])

        # The steer prompt is the CURRENT scene plus the new beat, so the world
        # drifts from where it is instead of being replaced.
        self.assertTrue(item["metadata"]["prompt"].startswith(BASE_PROMPT))
        self.assertIn(result["beat"], item["metadata"]["prompt"])
        self.assertEqual(item["metadata"]["base"], BASE_PROMPT)

    def test_tick_records_the_beat_as_world_state(self):
        result = self._tick()
        st = engine._load_state(SESSION_ID)
        self.assertEqual(st["ambient_beats"], [result["beat"]])
        self.assertEqual(st["drift_count"], 1)
        self.assertEqual(st["current_render_prompt"], st["feed_log"][-1]["metadata"]["prompt"])

    def test_beat_prompt_carries_the_scene_and_the_phase(self):
        self._tick()
        self.assertEqual(len(self.ask_calls), 1)
        prompt = self.ask_calls[0]
        # Grounded in the place the player is actually looking at, and in the
        # story dials — the world_tick prompt's phase rules are the whole reason
        # a 'critical' drift is allowed to be bigger than a 'normal' one.
        self.assertIn("concrete service corridor", prompt)
        self.assertIn("escalating", prompt)
        self.assertIn("Step deeper into the corridor", prompt)

    def test_earlier_beats_are_fed_back_so_drift_accumulates(self):
        self._seed_state(ambient_beats=["Sand hisses under the door."])
        self._tick()
        self.assertIn("Sand hisses under the door.", self.ask_calls[0])

    def test_beats_are_capped(self):
        self._seed_state(ambient_beats=[f"beat {n}" for n in range(20)])
        self._tick()
        st = engine._load_state(SESSION_ID)
        self.assertEqual(len(st["ambient_beats"]), engine.WORLD_DRIFT_BEATS_KEPT)
        self.assertEqual(st["ambient_beats"][-1], "The fluorescent tube dies and dust settles across the floor.")

    # ── refusals: cost + correctness gates, all enforced server-side ─────────

    def test_disabled_by_flag(self):
        engine.WORLD_DRIFT_ENABLED = False
        self.assertEqual(self._tick()["skipped"], "disabled")
        self.assertEqual(self.ask_calls, [])

    def test_no_drift_before_a_scene_exists(self):
        self._seed_state(current_render_base="")
        self.assertEqual(self._tick()["skipped"], "no_scene")
        self.assertEqual(self.ask_calls, [])

    def test_no_drift_before_the_first_turn(self):
        self._seed_state(turn_count=0)
        self.assertEqual(self._tick()["skipped"], "no_turns_yet")

    def test_no_drift_when_dead(self):
        self._seed_state(player_state={"alive": False, "health": 0})
        self.assertEqual(self._tick()["skipped"], "dead")

    def test_no_drift_while_a_turn_is_in_flight(self):
        """A running turn owns the world prompt; drifting into it would fight
        the consequence the player is waiting for.

        The lock is held from ANOTHER thread on purpose: that's where a real turn
        runs, and TURN_LOCK is reentrant, so holding it on this thread would let
        the probe straight through.
        """
        holding = threading.Event()
        release = threading.Event()

        def hold_the_turn_lock():
            with engine.TURN_LOCK:
                holding.set()
                release.wait(5)

        t = threading.Thread(target=hold_the_turn_lock, daemon=True)
        t.start()
        self.assertTrue(holding.wait(5))
        try:
            self.assertEqual(self._tick()["skipped"], "turn_in_flight")
        finally:
            release.set()
            t.join(5)
        self.assertEqual(self.ask_calls, [])

    def test_second_tick_is_refused_until_the_interval_elapses(self):
        self.assertTrue(self._tick()["ok"])
        self.assertEqual(self._tick()["skipped"], "too_soon")
        # The refused ask must not have cost an LLM call.
        self.assertEqual(len(self.ask_calls), 1)

    def test_budget_per_decision_point_is_finite(self):
        self._seed_state(drift_count=engine.WORLD_DRIFT_MAX_PER_TURN)
        self.assertEqual(self._tick()["skipped"], "budget_spent")
        self.assertEqual(self.ask_calls, [])

    def test_empty_beat_is_not_published(self):
        engine._ask = lambda prompt, **kw: "   "
        self.assertEqual(self._tick()["skipped"], "no_beat")
        st = engine._load_state(SESSION_ID)
        self.assertEqual([i for i in st["feed_log"] if i.get("type") == "world_drift"], [])


class TestWorldTickEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = api.app.test_client()
        engine.LLM_ENABLED = True
        engine.WORLD_DRIFT_ENABLED = True
        engine._drift_worker_active = False
        self._real_ask = engine._ask
        engine._ask = lambda prompt, **kw: "A door slams somewhere deeper in the facility."
        st = engine._load_state(SESSION_ID)
        st.update({
            "turn_count": 2,
            "current_render_base": BASE_PROMPT,
            "player_state": {"alive": True, "health": 100},
            "feed_log": [],
            "ambient_beats": [],
            "drift_count": 0,
            "last_drift_ts": 0,
        })
        engine._save_state(st, SESSION_ID)

    def tearDown(self):
        # Let any queued worker land BEFORE we discard the session, or it
        # recreates the file mid-teardown and leaks a drift into the next test.
        deadline = time.time() + 5
        while engine._drift_worker_active and time.time() < deadline:
            time.sleep(0.02)
        engine._ask = self._real_ask
        engine._drift_worker_active = False
        _discard_test_session()

    def _wait_for_drift(self, timeout_s=5.0):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            feed = self.client.get(f"/api/feed?since_id=0&session_id={SESSION_ID}").get_json()
            drifts = [i for i in feed if i.get("type") == "world_drift"]
            if drifts:
                return drifts
            time.sleep(0.05)
        return []

    def test_endpoint_never_waits_on_the_model(self):
        """Production serves the whole game from one worker with a few threads.
        The tick endpoint is polled by every client, so it must hand the model
        call to a worker and return immediately."""
        resp = self.client.post("/api/world_tick", json={"session_id": SESSION_ID})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"], body)
        self.assertTrue(body.get("queued"), "the endpoint must queue, not block")

    def test_tick_lands_on_the_requested_session_feed(self):
        resp = self.client.post("/api/world_tick", json={"session_id": SESSION_ID})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"], resp.get_json())

        # The client learns about the drift through the SAME feed poll it already
        # runs — no second channel.
        drifts = self._wait_for_drift()
        self.assertEqual(len(drifts), 1)
        self.assertIn("metadata", drifts[0])
        self.assertIsNone(drifts[0].get("image_url"))

    def test_refusal_is_a_200_with_a_reason(self):
        """The client asks optimistically on a timer; a refusal is normal
        operation, not an error it should log or back off from."""
        self.client.post("/api/world_tick", json={"session_id": SESSION_ID})
        self._wait_for_drift()
        resp = self.client.post("/api/world_tick", json={"session_id": SESSION_ID})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["skipped"], "too_soon")

    def test_only_one_drift_runs_at_a_time(self):
        """Several players (or tabs) polling must not each buy an LLM call and
        exhaust the thread budget the actual game needs."""
        engine._drift_worker_active = True
        try:
            body = self.client.post("/api/world_tick", json={"session_id": SESSION_ID}).get_json()
        finally:
            engine._drift_worker_active = False
        self.assertFalse(body["ok"])
        self.assertEqual(body["skipped"], "busy")


class TestResetFallbackCannotBrickTheFeed(unittest.TestCase):
    """api_reset's "no choices came back" recovery used to append an item with a
    hardcoded id of 999999 that was never persisted.

    The client tracks the highest id it has seen and then polls
    /api/feed?since_id=<that>, so one appearance of that fallback pinned it at
    999999 for the life of the page: every real item (ids in the tens) was
    filtered out server-side and the feed went silent forever. A recovery path
    must not be able to brick the session it is recovering.
    """

    def setUp(self):
        self.src = (ROOT / "engine.py").read_text(encoding="utf-8")

    def test_fallback_choice_prompt_has_no_hardcoded_id(self):
        self.assertNotIn('"id": 999999', self.src)

    def test_fallback_choice_prompt_goes_through_the_shared_counter(self):
        block = self.src.split("No player_choice_prompt found in initial_items", 1)[1][:1400]
        self.assertIn("create_feed_item(", block)
        # …and is actually persisted, so the server's counter stays ahead of it.
        self.assertIn("_feed_append(st, fallback_item)", block)


class TestTurnThreadHygiene(unittest.TestCase):
    """The /api/choose spawn used to be a leftover debug harness: a non-daemon
    thread per turn, a 200ms sleep on the request thread, and a scratch file
    that leaked whenever the worker didn't write it inside that window."""

    def setUp(self):
        self.src = (ROOT / "engine.py").read_text(encoding="utf-8")
        self.choose = self.src.split("def api_choose(", 1)[1].split("\ndef ", 1)[0]

    def test_turn_thread_is_a_daemon(self):
        self.assertIn("daemon=True", self.choose)
        self.assertNotIn("# thread.daemon = True", self.src)

    def test_request_thread_does_not_sleep_waiting_on_the_worker(self):
        self.assertNotIn("time.sleep(0.2)", self.choose)

    def test_scratch_markers_are_swept(self):
        self.assertIn("_sweep_thread_signals", self.choose)
        self.assertIn("def _sweep_thread_signals", self.src)

    def test_sweeper_only_removes_stale_markers(self):
        engine._sweep_thread_signals()  # must never raise
        fresh = engine.ROOT / "thread_signal_test_fresh.tmp"
        fresh.write_text("x", encoding="utf-8")
        try:
            engine._sweep_thread_signals()
            self.assertTrue(fresh.exists(), "a marker for a running turn must survive")
        finally:
            fresh.unlink(missing_ok=True)


class TestObserveRegroundIsWiredUp(unittest.TestCase):
    """The realtime anti-drift loop called generate_choices without importing
    it, so every run raised NameError inside the worker. It was swallowed, so
    nothing 500'd — the feature had simply never worked, and the vision call it
    pays for was thrown away every time."""

    def test_reground_worker_imports_generate_choices(self):
        src = (ROOT / "engine.py").read_text(encoding="utf-8")
        worker = src.split("def _spawn_observe_reground(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("from choices import generate_choices", worker)

    def test_reground_guard_is_released_when_the_thread_cannot_start(self):
        src = (ROOT / "engine.py").read_text(encoding="utf-8")
        worker = src.split("def _spawn_observe_reground(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("could not spawn reground worker", worker)


class TestDriftSingleFlightIsAtomic(unittest.TestCase):
    """Two sessions polling /api/world_tick must not both spawn an LLM worker —
    that's the thread-budget exhaustion the guard exists to prevent."""

    def test_claim_is_taken_under_a_lock(self):
        src = (ROOT / "engine.py").read_text(encoding="utf-8")
        spawn = src.split("def _spawn_world_drift(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("with _DRIFT_LOCK:", spawn)
        self.assertIn("_DRIFT_LOCK = threading.Lock()", src)

    def test_a_refused_claim_is_reported_as_busy(self):
        engine._drift_worker_active = True
        try:
            self.assertFalse(engine._spawn_world_drift("whoever", {}))
        finally:
            engine._drift_worker_active = False


class TestDriftFeedsBackIntoTheSimulation(unittest.TestCase):
    """Drift beats have already been shown to the player and pushed to the live
    world model, so the world-evolution rewrite has to absorb them — otherwise
    the world state contradicts what the player watched."""

    def setUp(self):
        self.evolve_src = (ROOT / "evolve_prompt_file.py").read_text(encoding="utf-8")
        self.engine_src = (ROOT / "engine.py").read_text(encoding="utf-8")

    def test_evolution_prompt_includes_the_drift_beats(self):
        self.assertIn('state.get("ambient_beats")', self.evolve_src)
        self.assertIn("WHAT THE WORLD DID ON ITS OWN WHILE THE PLAYER DELIBERATED", self.evolve_src)

    def test_evolution_consumes_the_beats(self):
        self.assertIn('"ambient_beats": []', self.evolve_src)
        # Both merge sites (async feed path + inline path) must apply the clear,
        # or the same beats get replayed into every later rewrite.
        self.assertEqual(self.engine_src.count('"seen_elements", "ambient_beats"'), 2)

    def test_a_new_turn_resets_the_drift_budget(self):
        self.assertIn("st['drift_count'] = 0", self.engine_src)


class TestClientWiring(unittest.TestCase):
    """The browser is where "is anyone actually watching this?" is known, so the
    expensive gates live there. These are source assertions — the behavioural
    path is covered by the realtime e2e harness."""

    def setUp(self):
        self.standalone = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")
        self.reactor = (ROOT / "static/js/reactor_renderer.js").read_text(encoding="utf-8")

    def test_drift_items_bypass_the_scene_path(self):
        # renderItem's generic "has a prompt -> it's a new scene" branch fires
        # ceremony beats, scene sound and autoplay, and re-anchors the world.
        self.assertIn('if (item.type === "world_drift")', self.standalone)
        self.assertIn("Renderer.applyDrift", self.standalone)

    def test_drift_is_applied_without_an_image(self):
        self.assertIn("applyDrift(meta)", self.standalone)
        self.assertIn("RR.applyScene({ prompt: prompt, imageUrl: null, hardTransition: false })",
                      self.standalone)

    def test_drift_asks_only_while_a_steerable_world_is_on_screen(self):
        self.assertIn("supportsLiveSteer", self.standalone)
        self.assertIn("supportsLiveSteer", self.reactor)
        self.assertIn("/api/world_tick", self.standalone)
        # A Happy Oyster adventure world is fixed once built, so a prompt change
        # rebuilds it — never acceptable on a timer.
        self.assertIn('familyFor(rstate.modelId) !== "happy_oyster"', self.reactor)

    def test_drift_loop_stops_when_nobody_is_looking(self):
        idle = self.standalone.split("idle() {", 1)[1].split("},", 1)[0]
        self.assertIn("document.hidden", idle)
        self.assertIn("state.processing", idle)
        self.assertIn("isShowing", idle)
        self.assertIn("ambientContextAllowed()", idle)


if __name__ == "__main__":
    unittest.main(verbosity=2)
