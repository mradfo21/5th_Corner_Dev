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
        """A session parked at a decision point with a rendered scene.

        Starts from a discarded session: a drift worker from an earlier test
        can still be mid-write when that test tears down, and inheriting its
        leftovers made this suite flake.
        """
        _discard_test_session()
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

    def test_drift_survives_the_prompt_being_pruned_from_the_editor(self):
        """The editable prompt surface is deliberately kept small, and this
        instruction block was once pruned from it as a dead knob — which
        silently turned every drift into a no-op. The feature carries its own
        default so tidying the editor can't disable it."""
        from prompts_store import PROMPTS
        self.assertNotIn("world_tick_micro_change_instructions", PROMPTS,
                         "if this key is back, this test's premise needs revisiting")
        result = self._tick()
        self.assertTrue(result["ok"], result)
        self.assertIn("TIME PASSES", self.ask_calls[0])

    def test_an_authored_prompt_overrides_the_default(self):
        """…but if someone does expose it again, theirs wins."""
        real = engine.PROMPTS
        engine.PROMPTS = dict(real)
        engine.PROMPTS["world_tick_micro_change_instructions"] = "AUTHORED DRIFT RULES {environment_type}"
        try:
            self.assertTrue(self._tick()["ok"])
        finally:
            engine.PROMPTS = real
        self.assertIn("AUTHORED DRIFT RULES", self.ask_calls[0])

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
        drifts = []
        while time.time() < deadline:
            feed = self.client.get(f"/api/feed?since_id=0&session_id={SESSION_ID}").get_json()
            drifts = [i for i in feed if i.get("type") == "world_drift"]
            if drifts:
                break
            time.sleep(0.05)
        # The worker publishes the feed item BEFORE clearing its in-flight
        # flag, so the item appearing doesn't mean the tick is finished. A test
        # that asks again in that window gets a perfectly valid "busy" instead
        # of the refusal it was checking for.
        while engine._drift_worker_active and time.time() < deadline:
            time.sleep(0.02)
        return drifts

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

    def test_reground_attaches_the_live_frame_not_the_web_url(self):
        src = (ROOT / "engine.py").read_text(encoding="utf-8")
        worker = src.split("def _spawn_observe_reground(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("image_url=fpath", worker)
        self.assertNotIn("image_url=web", worker)
        self.assertIn("last_dispatch=vision", worker)


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
        # SHAPE / turn-steer / fallback movement must refuse the same way
        # drift does, or they rebuild an Adventure world on a keystroke.
        steer = self.standalone.split("steerRealtime(text, where)", 1)[1]
        steer = steer.split("applyDrift(meta)", 1)[0]
        self.assertIn("supportsLiveSteer", steer)
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


class TestChoicesAreGroundedOnTheRenderedFrame(unittest.TestCase):
    """The slate drifted off the picture because every text-grounding step in
    the pipeline compared choices against the render REQUEST — the caption the
    image model was asked to draw — and nothing ever looked at what came back.
    A frame of barrels was therefore offered a crate to kick open."""

    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "engine.py").read_text(encoding="utf-8")
        cls.choices_src = (ROOT / "choices.py").read_text(encoding="utf-8")

    def _phase2(self):
        return self.src.split("def _advance_turn_choices_deferred_impl(", 1)[1] \
                       .split("\ndef ", 1)[0]

    def test_the_turn_looks_at_the_frame_it_just_rendered(self):
        # The read now runs on a background thread in parallel with the choice
        # call, but it must still LOOK at the frame it rendered — gated on the
        # frame existing (not the old inverted `not analysis_img_url`) — and it
        # must be joined before the history entry is written.
        phase2 = self._phase2()
        self.assertIn("_vision_analyze_all(analysis_img_url)", phase2)
        self.assertIn("analysis_img_url and VISION_ENABLED", phase2)
        self.assertNotIn("if (not analysis_img_url) and VISION_ENABLED:", phase2)
        self.assertIn("_vision_thread.join(", phase2)

    def test_a_failed_vision_read_keeps_the_render_caption(self):
        # Blanking it left the slate with no scene text at all, which is worse
        # than grounding on the request. The read caption is the initial value
        # and is only displaced once a real description comes back.
        phase2 = self._phase2()
        self.assertNotIn('vision_analysis_text  = ""', phase2)
        self.assertIn('if _vision_holder["description"]:', phase2)

    def test_the_rendered_description_reaches_the_next_turn(self):
        phase2 = self._phase2()
        self.assertIn('"vision_analysis":   vision_analysis_text', phase2)
        self.assertIn('"spatial_compass":   _spatial_compass_turn', phase2)

    def test_the_opening_slate_is_generated_from_the_opening_frame(self):
        intro = self.src.split("def generate_intro_turn_feed_items(", 1)[1] \
                        .split("\ndef ", 1)[0]
        self.assertIn("frame_path", intro)
        self.assertIn("image_url=frame_path or None", intro)
        self.assertIn("intro_frame_vision or intro_image_description", intro)

    def test_reset_resolves_the_opening_frame_before_building_the_slate(self):
        """On the no-montage path the run opens on a still, so that still has
        to exist before the slate is written from it."""
        reset = self.src.split("def _perform_game_reset(", 1)[1] \
                        .split("\ndef api_reset", 1)[0]
        branch = reset.split("_open_on_montage(new_state)", 1)[1] \
                      .split("else:", 1)[1]
        resolve_at = branch.index("_cached_opening_frame(new_state)")
        intro_at = branch.index("initial_items, intro_image_kwargs = generate_intro_turn_feed_items(")
        self.assertLess(resolve_at, intro_at,
                        "the opening still must be resolved before the intro slate")
        self.assertIn("frame_path=opening_rec.get(\"path\")", branch)

    def test_the_montage_path_regrounds_its_slate_on_the_frame_it_lands_on(self):
        """There is deliberately no frame at reset on the montage path — the
        montage IS the first render. So the slate is written from the level's
        prose and has to be rewritten once the player has somewhere to stand,
        or it describes a room they are not in (the 2026-09-17 report)."""
        handoff = self.src.split("def _finish_opening_montage(", 1)[1] \
                          .split("\ndef ", 1)[0]
        self.assertIn("slate_id=choices_item.get(\"id\")", handoff)
        append_at = handoff.index("_feed_append(st, choices_item)")
        reground_at = handoff.index("_spawn_cached_opening_vision(")
        self.assertLess(append_at, reground_at,
                        "the slate needs an id before it can be revised")

    def test_a_cold_start_regrounds_the_slate_once_the_render_lands(self):
        # With no cached still there is nothing to look at, so the opening slate
        # comes from the shot description. It must not stay that way.
        reset = self.src.split("def _perform_game_reset(", 1)[1] \
                        .split("\ndef api_reset", 1)[0]
        self.assertIn("_spawn_scene_choices_reground(", reset)
        spawn_at = reset.index("_spawn_scene_image_async(**intro_image_kwargs)")
        reground_at = reset.index("_spawn_scene_choices_reground(")
        self.assertLess(spawn_at, reground_at)

    def test_text_gates_are_skipped_when_the_model_saw_the_frame(self):
        gen = self.choices_src.split("def generate_choices(", 1)[1] \
                              .split("\ndef ", 1)[0]
        self.assertIn("frame_attached = True", gen)
        self.assertIn("if not frame_attached:\n        opts = filter_choices(", gen)
        self.assertIn("frame_attached=frame_attached", gen)

    def test_the_critic_is_shown_the_frame_it_is_judging(self):
        critic = self.choices_src.split("def choice_critic(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("image_path=frame_path", critic)
        self.assertIn("if not frame_attached:", critic)


class TestChoiceCriticKeepsWhatIsOnScreen(unittest.TestCase):
    """The critic's noun-overlap gate is the step that actually deleted
    image-grounded options: it kept only choices whose nouns appeared in the
    dispatch, so the one choice matching the render request survived and the
    rest were replaced with more of the same."""

    def setUp(self):
        import choices
        self.choices = choices
        # The critic's own LLM pass is not under test here; returning nothing
        # makes it fall through to the filtered list, which is what we assert on.
        self._real_ask = engine._ask
        engine._ask = lambda *a, **k: ""

    def tearDown(self):
        engine._ask = self._real_ask

    # A canyon of barrels, described by a dispatch that promised a crate.
    DISPATCH = "You drop into the canyon. The rusted crate sits where the slope ends."
    SLATE = ["Kick the rusted crate open",   # matches the text
             "Shoulder the oil barrel over",  # matches the FRAME only
             "Squeeze past the boulder"]      # matches the FRAME only

    def test_frame_grounded_choices_survive_when_the_frame_was_attached(self):
        out = self.choices.choice_critic(
            self.DISPATCH, "", list(self.SLATE), "", frame_attached=True)
        self.assertIn("Shoulder the oil barrel over", out)
        self.assertIn("Squeeze past the boulder", out)

    def test_without_a_frame_the_noun_gate_still_applies(self):
        # Text-only turns have nothing better to check against, so the old
        # behaviour must remain: only the text-matching option survives.
        out = self.choices.choice_critic(
            self.DISPATCH, "", list(self.SLATE), "", frame_attached=False)
        self.assertEqual(["Kick the rusted crate open"], out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
