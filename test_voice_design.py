"""
test_voice_design.py — offline unit tests for voice_design.py.

The unit classes fake the Gemini voices endpoint at the ``requests`` layer
(``FakeGemini``: POST/DELETE/GET on ``/v1beta/voices``), so the suite runs
with no network and no key, and the wire format itself — the body the create
sends, the gender retry, the paging of the listing — is under test, not only
the pipeline above it. ``is_available`` is pinned on by replacing
``_gemini_playing``; the classes that test availability itself patch
``provider_bridge`` instead.

The integration class at the bottom is skipped unless a Gemini key is set
AND ``SOMEWHERE_LIVE_VOICE_TEST=1``: it designs one real voice, checks it is
listed, and deletes it.

Run with:

    python -m unittest test_voice_design -v
"""

import os
import threading
import time
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock


def _reload_with_env(**env):
    """Reload voice_design under a fresh env so module-level config picks up
    new values (mirrors how gunicorn workers boot)."""
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    import importlib
    import voice_design
    importlib.reload(voice_design)
    return voice_design


def _pin_available(vd, key: str = "test-key"):
    """Make the reloaded module believe Gemini is playing on ``key``."""
    vd._gemini_playing = lambda: True
    vd._api_key = lambda: key
    return vd


class FakeResp:
    def __init__(self, status: int, body: Any = None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = str(self._body)

    def json(self):
        return self._body


class FakeGemini:
    """The three calls voice_design makes, with a record of each."""

    def __init__(self, delay: float = 0.15):
        self.delay = delay
        self.calls: List[tuple] = []
        self.stored: Dict[str, Dict[str, Any]] = {}
        self._n = 0
        self._lock = threading.Lock()
        self.fail_create = False
        self.reject_gender = False
        self.page_size = 0  # 0 = one page

    # POST /v1beta/voices
    def post(self, url, headers=None, json=None, timeout=None, **_kw):
        voice = (json or {}).get("voice") or {}
        with self._lock:
            self.calls.append(("create", url, headers, json))
        time.sleep(self.delay)  # simulate network
        if self.fail_create:
            return FakeResp(500, {"error": {"message": "boom"}})
        if self.reject_gender and "gender" in voice:
            return FakeResp(400, {"error": {"message": "bad gender"}})
        with self._lock:
            self._n += 1
            vid = f"voice_fake{self._n:04d}"
        body = {
            "id": vid, "model": voice.get("model"), "type": "prompted",
            "expire_time": "2027-09-25T12:00:00.123456789Z",
            "display_name": voice.get("display_name"),
            "prompted": voice.get("prompted"),
            "language_code": voice.get("language_code"),
            "gender": voice.get("gender"),
            "usage": {"total_input_tokens": 40, "total_output_tokens": 300,
                      "total_thought_tokens": 12},
            "sample_audio": {"mime_type": "audio/wav", "data": "UklGRg=="},
        }
        self.stored[vid] = body
        return FakeResp(200, body)

    # DELETE /v1beta/voices/<id>
    def delete(self, url, headers=None, timeout=None, **_kw):
        vid = url.rsplit("/", 1)[-1]
        with self._lock:
            self.calls.append(("delete", vid))
        self.stored.pop(vid, None)
        return FakeResp(200, {})

    # GET /v1beta/voices — as it answered live: unfiltered, the prebuilt
    # catalogue with the stored voices among it; `type=prompted`, only the
    # stored ones. Snake-case `next_page_token`.
    PREBUILT = [{"id": "achernar", "type": "prebuilt", "display_name": "Achernar"},
                {"id": "algenib", "type": "prebuilt", "display_name": "Algenib"}]

    def get(self, url, headers=None, params=None, timeout=None, **_kw):
        params = dict(params or {})
        with self._lock:
            self.calls.append(("list", params))
        voices = [dict(v, type="prompted") for v in self.stored.values()]
        if params.get("type") != "prompted":
            voices = self.PREBUILT + voices
        if not self.page_size:
            return FakeResp(200, {"voices": voices})
        start = int(params.get("pageToken") or 0)
        page = voices[start:start + self.page_size]
        body: Dict[str, Any] = {"voices": page}
        if start + self.page_size < len(voices):
            body["next_page_token"] = str(start + self.page_size)
        return FakeResp(200, body)

    def of(self, kind: str) -> List[tuple]:
        return [c for c in self.calls if c[0] == kind]


class _CostRecorder:
    def __init__(self):
        self.events: List[tuple] = []

    def record_usage(self, *args, **kwargs):
        self.events.append((args, kwargs))


# An upper bound, not a pause: get_or_design_voice returns the moment the
# design lands. 1.5 s was enough on a dev machine and not on a busy CI runner,
# where these tests read "generating" (#162, #168; fixed on main by #169).
READY_WAIT = 10.0


class _FakeGeminiCase(unittest.TestCase):
    """A fresh module, a fresh cache file and a fake Gemini per test."""

    env: Dict[str, Optional[str]] = {}

    def setUp(self):
        self.vd = _pin_available(_reload_with_env(**{
            "SOMEWHERE_DYNAMIC_VOICES": "1",
            "SOMEWHERE_DESIGN_BUDGET_PER_SESSION": "8",
            "SOMEWHERE_DESIGN_CONCURRENCY": "3",
            "SOMEWHERE_VOICE_SOFT_CAP": None,
            **self.env,
        }))
        if self.vd.CACHE_PATH.exists():
            self.vd.CACHE_PATH.unlink()
        self.fake = FakeGemini()
        self.cost = _CostRecorder()
        self.vd.cost_tracker = self.cost
        import requests
        self._patches = [
            mock.patch.object(requests, "post", self.fake.post),
            mock.patch.object(requests, "delete", self.fake.delete),
            mock.patch.object(requests, "get", self.fake.get),
        ]
        for p in self._patches:
            p.start()

    def _drain(self, seconds: float = 10.0) -> None:
        """Wait until every background design has finished (no sleeps that
        guess: a worker on a cold or busy machine is slower than any fixed
        pause)."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            with self.vd._INFLIGHT_LOCK:
                pending = list(self.vd._INFLIGHT_EVENTS.values())
            if not pending:
                return
            for ev in pending:
                ev.wait(timeout=max(0.0, deadline - time.time()))
            time.sleep(0.01)

    def tearDown(self):
        # Drain the designs a test started and did not wait for (wait=0).
        # _reload_with_env reloads this module IN PLACE, so a worker left
        # running outlives its test: it calls the NEXT test's fake designer
        # (a third design where two were expected) and holds that cache key in
        # flight, so the next test's request coalesces onto it and reads
        # "generating". It only showed on a slow CI runner (2026-09-25, #162).
        self._drain()
        for p in self._patches:
            p.stop()
        if self.vd.CACHE_PATH.exists():
            self.vd.CACHE_PATH.unlink()

    def design(self, label="warden", session="s1", wait=READY_WAIT, kind="person", **kw):
        return self.vd.get_or_design_voice({"label": label, "kind": kind},
                                           session, wait=wait, **kw)


class TestBriefBuilder(unittest.TestCase):
    """The brief builder is pure / deterministic — no I/O, no import-time
    side effects beyond reading env. These lock in the classifier outputs
    so a future refactor can't silently drift the prompt shape."""

    @classmethod
    def setUpClass(cls):
        cls.vd = _reload_with_env(SOMEWHERE_DYNAMIC_VOICES="1")

    def _brief(self, label, kind, **context_kw):
        ctx = {"situation": {"chaos": 0, "phase": "normal"}, "recent": [],
               "opening_line": "", "premise": ""}
        for k, v in context_kw.items():
            if k in ("chaos", "phase", "time_of_day", "scene"):
                ctx["situation"][k] = v
            else:
                ctx[k] = v
        return self.vd.brief_for_subject({"label": label, "kind": kind}, ctx)

    def test_person_default_reads_naturally(self):
        b = self._brief("warden", "person")
        self.assertIn("adult male voice", b["description"])
        self.assertIn("warden", b["description"])
        self.assertIn("hard-edged", b["description"])
        self.assertEqual(b["gender"], "male")
        self.assertGreater(len(b["sample_text"]), 10)

    def test_machine_uses_synthetic_environment(self):
        b = self._brief("rusted intercom", "machine")
        self.assertIn("synthetic voice", b["description"])
        self.assertIn("PA / intercom", b["description"])
        # "neutral" is not a gender the create call is known to accept.
        self.assertEqual(b["gender"], "")

    def test_creature_uses_uncanny_timbre(self):
        b = self._brief("shape in the doorway", "creature")
        self.assertIn("uncanny", b["description"])

    def test_the_moment_is_style_not_the_voice(self):
        """A designed voice keeps what it was designed with. Chaos 9 must not
        bake "frayed" into the character for every line it ever says; it
        comes back as the line's style instead."""
        calm = self._brief("courier", "person")
        b = self._brief("courier", "person", chaos=9, phase="climax",
                        recent=["a scream in the tunnel"],
                        scene="the drainage tunnel", premise="a quarantine")
        self.assertEqual(b["description"], calm["description"])
        for situational in ("frayed", "halting", "scream", "tunnel",
                            "quarantine", "Emotion", "Speak the sample"):
            self.assertNotIn(situational, b["description"])
        self.assertIn("frayed", b["emotion"])
        self.assertIn("halting", b["delivery"])

    def test_female_hint_from_label(self):
        b = self._brief("old woman", "person")
        self.assertIn("elder female", b["description"])
        self.assertEqual(b["gender"], "female")

    def test_young_hint_from_label(self):
        b = self._brief("young girl", "person")
        self.assertIn("young female", b["description"])

    def test_unknown_gender_is_left_out(self):
        b = self._brief("figure", "person")
        self.assertEqual(b["gender"], "")
        self.assertNotIn("unspecified", b["description"])

    def test_opening_line_becomes_sample_when_reasonable(self):
        opening = "The warden's close. Keep low, don't say my name."
        b = self._brief("warden", "person", opening_line=opening)
        self.assertEqual(b["sample_text"], opening)

    def test_short_or_empty_opening_falls_back_to_neutral(self):
        b = self._brief("figure", "person", opening_line="hi")
        self.assertNotEqual(b["sample_text"], "hi")

    def test_labels_kept_as_metadata(self):
        b = self._brief("warden", "person")
        self.assertEqual(b["labels"]["source"], self.vd.NAME_PREFIX)
        self.assertIn("subject_label", b["labels"])
        self.assertIn("subject_kind", b["labels"])
        self.assertIn("created_at", b["labels"])

    def test_description_is_compact(self):
        long_label = "wounded cold hostile " + "x" * 3000
        b = self._brief(long_label, "person", scene="y" * 3000)
        self.assertGreaterEqual(len(b["description"]), 20)
        self.assertLessEqual(len(b["description"]), 400)

    def test_voice_name_is_short_and_prefixed(self):
        b = self._brief("warden", "person")
        self.assertTrue(b["voice_name"].startswith("[dyn]"))
        self.assertLessEqual(len(b["voice_name"]), 100)

    def test_elevenlabs_era_seed_is_compacted(self):
        """Companions saved before the switch carry the ElevenLabs brief as
        their regen seed. Redesigned as-is, "Emotion: frayed" would be in the
        voice for good and the tail would be read as a description."""
        old = ('An adult male voice for a person known as "kane" in a 1993 '
               'analog-horror world. Timbre: gravelly, worn. Delivery: measured. '
               'Emotion: frayed, breath-short, urgent. Register: hushed. '
               'Environment: close-mic\'d, natural room. Character notes: Currently '
               'in: the tunnel. World premise: a quarantine. Speak the sample line '
               'as this character would speak it, once, cleanly. Do NOT include '
               'music, background sound effects, singing, or non-speech noises.')
        c = self.vd._compact_description(old)
        self.assertIn("gravelly", c)
        self.assertIn("Register: hushed", c)
        for gone in ("Emotion", "World premise", "Character notes",
                     "Speak the sample", "Do NOT include"):
            self.assertNotIn(gone, c)
        self.assertLessEqual(len(self.vd._compact_description("z " * 2000)),
                             self.vd.DESCRIPTION_MAX)


class TestCacheKey(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vd = _reload_with_env(SOMEWHERE_DYNAMIC_VOICES="1")

    def test_normalizes_case_and_whitespace(self):
        k1 = self.vd.cache_key({"label": "Warden", "kind": "PERSON"}, "s1")
        k2 = self.vd.cache_key({"label": "warden", "kind": "person"}, "s1")
        k3 = self.vd.cache_key({"label": "  warden  ", "kind": "person"}, "s1")
        self.assertEqual(k1, k2)
        self.assertEqual(k1, k3)

    def test_session_scoped(self):
        k1 = self.vd.cache_key({"label": "warden", "kind": "person"}, "s1")
        k2 = self.vd.cache_key({"label": "warden", "kind": "person"}, "s2")
        self.assertNotEqual(k1, k2)

    def test_world_prompt_changes_key(self):
        k1 = self.vd.cache_key({"label": "warden", "kind": "person"}, "s1",
                                world_prompt="a snowy town")
        k2 = self.vd.cache_key({"label": "warden", "kind": "person"}, "s1",
                                world_prompt="a rusted refinery")
        self.assertNotEqual(k1, k2)


class TestWireFormat(_FakeGeminiCase):
    """What actually goes to Google, and what is kept from the answer."""

    def test_create_body_is_the_verified_shape(self):
        r = self.design("warden")
        self.assertEqual(r["status"], "ready")
        (_, url, headers, body), = self.fake.of("create")
        self.assertEqual(url, "https://generativelanguage.googleapis.com/v1beta/voices")
        self.assertEqual(headers["x-goog-api-key"], "test-key")
        self.assertIs(body["store"], True)
        v = body["voice"]
        self.assertEqual(v["model"], "gemini-3.8-flash-tts")
        self.assertEqual(v["type"], "prompted")
        self.assertEqual(v["language_code"], "en-US")
        self.assertEqual(v["gender"], "male")
        self.assertTrue(v["display_name"].startswith("[dyn] warden"))
        self.assertEqual(v["prompted"]["input"], r["description"])

    def test_ready_id_is_the_voice_id_gemini_answered(self):
        r = self.design("warden")
        self.assertTrue(r["voice_id"].startswith("voice_"))
        self.assertIn(r["voice_id"], self.fake.stored)
        # The sample audio is not hoarded in the cache file.
        self.assertNotIn("UklGRg", self.vd.CACHE_PATH.read_text(encoding="utf-8"))

    def test_unknown_gender_sends_no_gender_field(self):
        self.design("figure")
        (_, _, _, body), = self.fake.of("create")
        self.assertNotIn("gender", body["voice"])

    def test_a_gender_400_retries_once_without_it(self):
        self.fake.reject_gender = True
        r = self.design("warden")
        self.assertEqual(r["status"], "ready")
        creates = self.fake.of("create")
        self.assertEqual(len(creates), 2)
        self.assertIn("gender", creates[0][3]["voice"])
        self.assertNotIn("gender", creates[1][3]["voice"])

    def test_one_usage_event_per_create_in_tokens(self):
        self.design("warden")
        (args, kw), = self.cost.events
        self.assertEqual(args, ("s1", "voice", "gemini",
                                "gemini-3.8-flash-tts:voice_design"))
        self.assertEqual(kw["operation"], "design")
        self.assertEqual(kw["unit_type"], "tokens")
        self.assertEqual(kw["input_units"], 40)
        self.assertEqual(kw["output_units"], 312)  # output + thought
        self.assertTrue(kw["success"])

    def test_a_failed_create_is_recorded_as_failed(self):
        self.fake.fail_create = True
        self.design("warden")
        (_args, kw), = self.cost.events
        self.assertFalse(kw["success"])

    def test_delete_hits_the_voice_url(self):
        r = self.design("warden")
        self.vd.release_session_voices("s1")
        self.assertIn(("delete", r["voice_id"]), self.fake.calls)
        self.assertNotIn(r["voice_id"], self.fake.stored)

    def test_listing_follows_next_page_token(self):
        for lbl in ("a1", "a2", "a3", "a4", "a5"):
            self.design(lbl)
        self.fake.page_size = 2
        voices, reason = self.vd._list_voices()
        self.assertEqual(reason, "ok")
        self.assertEqual(len(voices), 5)
        lists = self.fake.of("list")
        self.assertEqual(len(lists), 3)
        self.assertEqual([c[1].get("pageToken") for c in lists], [None, "2", "4"])
        self.assertTrue(all(v["display_name"].startswith("[dyn]") for v in voices))

    def test_listing_asks_for_stored_voices_not_the_catalogue(self):
        """Unfiltered, the listing is Google's 2,089 prebuilt voices; the
        first live run walked twenty pages of them and gave up."""
        self.design("warden")
        voices, reason = self.vd._list_voices()
        self.assertEqual(reason, "ok")
        self.assertEqual(self.fake.of("list")[0][1].get("type"), "prompted")
        self.assertEqual(len(voices), 1)
        # And a prebuilt entry is never taken for ours, filter or not.
        self.fake.get = lambda *a, **k: FakeResp(200, {"voices": FakeGemini.PREBUILT})
        import requests
        with mock.patch.object(requests, "get", self.fake.get):
            voices, reason = self.vd._list_voices()
        self.assertEqual((voices, reason), ([], "empty"))

    def test_an_elevenlabs_cache_file_is_not_trusted(self):
        """A version-1 cache holds ElevenLabs ids. Gemini TTS cannot speak
        them and this key cannot delete them; served as 'ready' they would
        be a silent TALK."""
        import json
        key = self.vd.cache_key({"label": "warden", "kind": "person"}, "s1")
        self.vd.CACHE_PATH.write_text(json.dumps({"version": 1, "voices": {key: {
            "voice_id": "cjVigY5qzO86Huf0OWal", "session_id": "s1",
            "status": "ready"}}}), encoding="utf-8")
        self.assertFalse(self.vd.is_ready_voice_id("cjVigY5qzO86Huf0OWal"))
        r = self.design("warden")
        self.assertTrue(r["voice_id"].startswith("voice_"))
        self.assertEqual(r["source"], "designed")


class TestDesignPipeline(_FakeGeminiCase):
    """End-to-end async pipeline with a fake Gemini.

    Covers the state transitions that matter for correctness:
      - non-blocking call returns "generating" and kicks off a worker,
      - a concurrent call for the same key COALESCES onto the inflight job,
      - a completed job flips the cache entry to "ready" and later reads hit,
      - release_session_voices deletes voices tagged to that session only.
    """

    def test_wait_zero_returns_generating(self):
        r = self.design(wait=0)
        self.assertEqual(r["status"], "generating")
        self.assertIsNone(r["voice_id"])
        # A cache_key must be present so the client can poll status.
        self.assertTrue(r["cache_key"])

    def test_concurrent_identical_calls_coalesce(self):
        r1 = self.design(wait=0)
        r2 = self.design(wait=0)
        # Second caller must NOT trigger a duplicate design call.
        self.assertEqual(r1["cache_key"], r2["cache_key"])
        self._drain()
        self.assertEqual(len(self.fake.of("create")), 1)

    def test_ready_after_wait_returns_designed(self):
        r = self.design()
        self.assertEqual(r["status"], "ready")
        self.assertEqual(r["source"], "designed")
        self.assertTrue(r["voice_id"].startswith("voice_"))

    def test_second_lookup_is_cache_hit(self):
        self.design()
        r = self.design(wait=0)
        self.assertEqual(r["status"], "ready")
        self.assertEqual(r["source"], "cache")
        self.assertEqual(len(self.fake.of("create")), 1)

    def test_regenerate_voice_uses_stored_description_and_redesigns(self):
        # First design lands a voice; regenerate must DROP the cache entry,
        # spend another design call, and keep the caller's description seed.
        first = self.design("kane")
        self.assertEqual(first["status"], "ready")
        seed = "a low gravelly wary male voice mid-40s tired, analog horror radio."
        creates_before = len(self.fake.of("create"))
        regen = self.vd.regenerate_voice(
            {"label": "kane", "kind": "person"}, "s1", seed,
            old_voice_id=first["voice_id"], wait=READY_WAIT,
        )
        self.assertEqual(regen["status"], "ready")
        self.assertEqual(regen["description"], seed)
        self.assertNotEqual(regen["voice_id"], first["voice_id"])
        self.assertEqual(len(self.fake.of("create")), creates_before + 1)
        self.assertEqual(self.fake.of("create")[-1][3]["voice"]["prompted"]["input"], seed)
        # Old voice freed when refcount is zero.
        self.assertIn(("delete", first["voice_id"]), self.fake.calls)

    def test_regenerate_keeps_a_held_voice(self):
        first = self.design("kane")
        self.vd.acquire(first["voice_id"])
        try:
            self.vd.regenerate_voice(
                {"label": "kane", "kind": "person"}, "s1",
                "a low gravelly wary male voice, tired.",
                old_voice_id=first["voice_id"], wait=READY_WAIT)
            self.assertNotIn(("delete", first["voice_id"]), self.fake.calls)
        finally:
            self.vd.release(first["voice_id"])

    def test_description_override_skips_brief_builder(self):
        seed = "a thin metallic whisper for a machine called rusted intercom."
        r = self.design("rusted intercom", kind="machine", description_override=seed)
        self.assertEqual(r["status"], "ready")
        self.assertEqual(r["description"], seed)

    def test_different_session_designs_different_voice(self):
        r1 = self.design(session="s1")
        r2 = self.design(session="s2")
        self.assertNotEqual(r1["cache_key"], r2["cache_key"])
        self.assertNotEqual(r1["voice_id"], r2["voice_id"])
        self.assertEqual(len(self.fake.of("create")), 2)

    def test_release_session_voices_deletes_only_that_session(self):
        r1 = self.design(session="s1")
        r2 = self.design(session="s2")
        res = self.vd.release_session_voices("s1")
        self.assertEqual(res["deleted"], 1)
        self.assertIn(r1["voice_id"], res["voice_ids"])
        # s2's voice still resolves from cache.
        r2b = self.design(session="s2", wait=0)
        self.assertEqual(r2b["voice_id"], r2["voice_id"])
        self.assertEqual(r2b["source"], "cache")

    def test_refcount_blocks_release_then_allows_after_release(self):
        r = self.design()
        self.vd.acquire(r["voice_id"])
        res1 = self.vd.release_session_voices("s1")
        self.assertEqual(res1["deleted"], 0)
        self.assertEqual(res1["skipped"], 1)
        self.assertEqual(self.fake.of("delete"), [])
        # After the caller signals end-of-call, cleanup succeeds.
        self.vd.release(r["voice_id"])
        res2 = self.vd.release_session_voices("s1")
        self.assertEqual(res2["deleted"], 1)

    def test_get_status_reports_ready_and_unknown(self):
        r = self.design()
        st = self.vd.get_status(r["cache_key"])
        self.assertEqual(st["status"], "ready")
        self.assertEqual(st["voice_id"], r["voice_id"])
        self.assertEqual(self.vd.get_status("bogus")["status"], "unknown")

    def test_is_ready_voice_id_admits_designed_and_rejects_random(self):
        r = self.design()
        self.assertTrue(self.vd.is_ready_voice_id(r["voice_id"]))
        self.assertFalse(self.vd.is_ready_voice_id("voice_not_ours"))
        self.assertFalse(self.vd.is_ready_voice_id("Charon"))
        self.assertFalse(self.vd.is_ready_voice_id(""))

    def test_design_failure_records_failed_and_uses_ttl(self):
        self.fake.fail_create = True
        r = self.design()
        self.assertEqual(r["status"], "failed")
        # Immediately re-asking must NOT re-attempt (TTL guards paid calls).
        r2 = self.design(wait=0)
        self.assertEqual(r2["status"], "failed")
        self.assertEqual(len(self.fake.of("create")), 1)

    def test_failure_retries_after_the_ttl(self):
        self.fake.fail_create = True
        self.design()
        self.fake.fail_create = False
        key = self.vd.cache_key({"label": "warden", "kind": "person"}, "s1")
        entry = self.vd._get_entry(key)
        entry["expires_at"] = time.time() - 1
        self.vd._put_entry(key, entry)
        r = self.design()
        self.assertEqual(r["status"], "ready")

    def test_per_session_budget_falls_back_after_cap(self):
        self.vd.DESIGN_BUDGET_PER_SESSION = 2
        for lbl in ("warden", "courier"):
            self.design(lbl)
        r = self.design("elder", wait=0)
        self.assertEqual(r["source"], "budget")
        self.assertEqual(len(self.fake.of("create")), 2)

    def test_corrupt_cache_file_is_recovered(self):
        self.vd.CACHE_PATH.write_text("{not: valid json", encoding="utf-8")
        # Any operation must not raise; recovery yields an empty cache.
        r = self.design()
        self.assertEqual(r["status"], "ready")


class TestSoftCap(_FakeGeminiCase):
    """Google stores 200 voices per project; we evict before we get there."""

    def test_default_cap_leaves_headroom_under_200(self):
        self.assertEqual(self.vd._soft_cap(), 180)
        self.assertLess(self.vd._soft_cap(), self.vd.PROJECT_VOICE_LIMIT)
        snap = self.vd.cache_snapshot()
        self.assertEqual(snap["project_slots"]["limit"], 200)
        self.assertEqual(snap["config"]["model"], "gemini-3.8-flash-tts")

    def test_lru_evicts_the_least_recently_used_at_the_cap(self):
        self.vd.VOICE_SOFT_CAP_OVERRIDE = 2
        a = self.design("alpha")
        b = self.design("bravo")
        # Touch alpha so bravo is the least recently used.
        time.sleep(1.1)
        self.design("alpha", wait=0)
        c = self.design("charlie")
        self.assertEqual(c["status"], "ready")
        self.assertIn(("delete", b["voice_id"]), self.fake.calls)
        self.assertNotIn(("delete", a["voice_id"]), self.fake.calls)
        self.assertFalse(self.vd.is_ready_voice_id(b["voice_id"]))
        self.assertEqual(self.vd.cache_snapshot()["project_slots"]["used"], 2)

    def test_lru_never_evicts_a_held_voice(self):
        self.vd.VOICE_SOFT_CAP_OVERRIDE = 1
        a = self.design("alpha")
        self.vd.acquire(a["voice_id"])
        try:
            self.design("bravo")
            self.assertNotIn(("delete", a["voice_id"]), self.fake.calls)
        finally:
            self.vd.release(a["voice_id"])


class TestSweep(_FakeGeminiCase):
    """The sweep reaps our orphans and nothing else."""

    def _plant(self, vid, name, hours_old):
        created = time.time() - hours_old * 3600
        expires = created + 365 * 24 * 3600
        from datetime import datetime, timezone
        self.fake.stored[vid] = {
            "id": vid, "display_name": name,
            "expire_time": datetime.fromtimestamp(expires, timezone.utc)
                                   .strftime("%Y-%m-%dT%H:%M:%S.123456789Z"),
        }

    def test_reaps_old_orphans_with_our_prefix_only(self):
        self._plant("voice_orphan", "[dyn] warden (person)", hours_old=3)
        self._plant("voice_handmade", "Narrator I made", hours_old=3)
        res = self.vd.sweep_orphans(active_session_ids=[])
        self.assertEqual(res["deleted"], 1)
        self.assertIn(("delete", "voice_orphan"), self.fake.calls)
        self.assertNotIn(("delete", "voice_handmade"), self.fake.calls)

    def test_a_fresh_orphan_is_given_grace(self):
        """Not in this cache and minutes old: a design in flight, or a
        second checkout's voice on the same key."""
        self._plant("voice_new", "[dyn] courier (person)", hours_old=0.1)
        res = self.vd.sweep_orphans(active_session_ids=[])
        self.assertEqual(res["deleted"], 0)
        self.assertEqual(self.fake.of("delete"), [])

    def test_keeps_cached_voices_of_live_sessions(self):
        r = self.design(session="s1")
        res = self.vd.sweep_orphans(max_age_hours=0, active_session_ids=["s1"])
        self.assertEqual(res["deleted"], 0)
        self.assertTrue(self.vd.is_ready_voice_id(r["voice_id"]))

    def test_reaps_stale_cached_voices_of_ended_sessions(self):
        r = self.design(session="s1")
        res = self.vd.sweep_orphans(max_age_hours=0, active_session_ids=["s2"])
        self.assertEqual(res["deleted"], 1)
        self.assertFalse(self.vd.is_ready_voice_id(r["voice_id"]))

    def test_never_reaps_a_held_voice(self):
        r = self.design(session="s1")
        self.vd.acquire(r["voice_id"])
        try:
            self.vd.sweep_orphans(max_age_hours=0, active_session_ids=[])
            self.assertEqual(self.fake.of("delete"), [])
        finally:
            self.vd.release(r["voice_id"])

    def test_drops_cache_entries_whose_voice_is_gone(self):
        r = self.design(session="s1")
        self.fake.stored.pop(r["voice_id"])
        res = self.vd.sweep_orphans(active_session_ids=["s1"])
        self.assertEqual(res["dropped_cache"], 1)
        self.assertFalse(self.vd.is_ready_voice_id(r["voice_id"]))

    def test_a_failed_listing_changes_nothing(self):
        r = self.design(session="s1")
        self.fake.get = lambda *a, **k: FakeResp(503, {"error": "down"})
        import requests
        with mock.patch.object(requests, "get", self.fake.get):
            res = self.vd.sweep_orphans(max_age_hours=0, active_session_ids=[])
        self.assertEqual(res["reason"], "http_503")
        self.assertTrue(self.vd.is_ready_voice_id(r["voice_id"]))
        self.assertEqual(self.fake.of("delete"), [])


class TestNoLibrary(unittest.TestCase):
    """ElevenLabs' "your voices" library is gone; the callers that listed it
    must get an empty answer, not an exception, and fall to the roster."""

    def test_library_is_empty_and_nothing_is_a_library_voice(self):
        vd = _reload_with_env(SOMEWHERE_DYNAMIC_VOICES="1")
        with mock.patch("requests.get") as get:
            lib = vd.voice_library(force=True)
            self.assertFalse(vd.is_library_voice_id("Charon"))
        self.assertEqual(lib, {"ok": False, "reason": "no_library", "voices": []})
        get.assert_not_called()


class TestFeatureDisabled(unittest.TestCase):
    """Availability is Gemini actually playing — and when it is not, nothing
    reaches the network."""

    def _vd(self, **pb):
        vd = _reload_with_env(SOMEWHERE_DYNAMIC_VOICES="1")
        self._patches = [mock.patch(f"provider_bridge.{k}", return_value=v)
                         for k, v in pb.items()]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])
        return vd

    def _assert_silent(self, vd):
        self.assertFalse(vd.is_available())
        self.assertTrue(vd.unavailable_reason())
        with mock.patch("requests.post") as post, \
                mock.patch("requests.get") as get, \
                mock.patch("requests.delete") as delete:
            self.assertIsNone(vd.get_or_design_voice(
                {"label": "warden", "kind": "person"}, "s1", wait=0.2))
            self.assertIsNone(vd.regenerate_voice(
                {"label": "warden", "kind": "person"}, "s1",
                "a gravelly adult male voice, measured.", wait=0.2))
            self.assertEqual(vd.sweep_orphans()["deleted"], 0)
        post.assert_not_called()
        get.assert_not_called()
        delete.assert_not_called()

    def test_gemini_with_a_key_is_available(self):
        vd = self._vd(_mock_forced=False, effective_provider="gemini",
                      gemini_key="AIza-test")
        self.assertTrue(vd.is_available())
        self.assertEqual(vd.unavailable_reason(), "")

    def test_no_gemini_key(self):
        self._assert_silent(self._vd(_mock_forced=False, effective_provider="",
                                     gemini_key=""))

    def test_an_openai_player_has_no_voice_design(self):
        vd = self._vd(_mock_forced=False, effective_provider="openai",
                      gemini_key="AIza-test")
        self.assertIn("OpenAI", vd.unavailable_reason())
        self._assert_silent(vd)

    def test_mock_mode(self):
        vd = self._vd(_mock_forced=True, effective_provider="gemini",
                      gemini_key="AIza-test")
        self.assertEqual(vd.unavailable_reason(), "mock mode")
        self._assert_silent(vd)

    def test_disabled_flag_returns_none_from_resolver(self):
        vd = _reload_with_env(SOMEWHERE_DYNAMIC_VOICES="0")
        self.assertFalse(vd.is_available())
        r = vd.get_or_design_voice({"label": "warden", "kind": "person"}, "s1")
        self.assertIsNone(r)


class TestEngineResolver(unittest.TestCase):
    """Engine-level resolver: proves the wiring in engine.py always returns a
    usable voice_id even when the dynamic module is disabled — so
    api_talk_session can never end up with an empty voice."""

    def test_resolver_falls_back_when_module_disabled(self):
        _reload_with_env(SOMEWHERE_DYNAMIC_VOICES="0")
        import importlib
        import engine
        importlib.reload(engine)
        r = engine.resolve_voice_for_subject({"label": "warden", "kind": "person"},
                                             "default")
        self.assertTrue(r["voice_id"])
        self.assertEqual(r["status"], "disabled")


class TestFallbackVoiceForSubject(unittest.TestCase):
    """The smart fallback voice picker: hashes subject label into a
    gender/kind-filtered pool of roster voices so different characters
    sound different even before a per-character voice is designed."""

    @classmethod
    def setUpClass(cls):
        cls.vd = _reload_with_env(SOMEWHERE_DYNAMIC_VOICES="1")
        import importlib
        import engine
        importlib.reload(engine)
        cls.engine = engine

    def _name_of(self, vid):
        return next((v.get("name") for v in self.engine.VOICES_CONFIG["voices"]
                     if v.get("id") == vid), None)

    def test_returns_a_registered_voice(self):
        registered = {v["id"] for v in self.engine.VOICES_CONFIG["voices"]}
        for lbl, kind in [("warden", "person"), ("creature", "creature"),
                          ("intercom", "machine"), ("figure", "person")]:
            vid = self.engine.resolve_fallback_voice_for_subject(
                {"label": lbl, "kind": kind})
            self.assertIn(vid, registered,
                          f"{lbl}/{kind} returned unregistered {vid!r}")

    def test_female_label_picks_female_voice(self):
        female_ids = {v["id"] for v in self.engine.VOICES_CONFIG["voices"]
                      if v.get("gender") == "female"}
        for lbl in ("woman", "old woman", "mother", "sister", "young girl"):
            vid = self.engine.resolve_fallback_voice_for_subject(
                {"label": lbl, "kind": "person"})
            self.assertIn(vid, female_ids,
                          f"{lbl!r} should map to a female voice, got {self._name_of(vid)!r}")

    def test_male_label_picks_male_voice(self):
        male_ids = {v["id"] for v in self.engine.VOICES_CONFIG["voices"]
                    if v.get("gender") == "male"}
        for lbl in ("warden", "sheriff", "priest", "father", "old man"):
            vid = self.engine.resolve_fallback_voice_for_subject(
                {"label": lbl, "kind": "person"})
            self.assertIn(vid, male_ids,
                          f"{lbl!r} should map to a male voice, got {self._name_of(vid)!r}")

    def test_machine_picks_neutral(self):
        neutral_ids = {v["id"] for v in self.engine.VOICES_CONFIG["voices"]
                       if v.get("gender") == "neutral"}
        for lbl in ("rusted intercom", "static-filled radio", "PA system"):
            vid = self.engine.resolve_fallback_voice_for_subject(
                {"label": lbl, "kind": "machine"})
            self.assertIn(vid, neutral_ids)

    def test_never_picks_the_narrator(self):
        # The narrator is "the archive voice" and would break the fiction if
        # a random character got it.
        narrator_id = self.engine.VOICES_CONFIG.get("narrator_voice")
        for lbl in ["figure", "shape", "watcher", "voice", "silhouette",
                    "presence", "man", "woman", "creature", "child"]:
            vid = self.engine.resolve_fallback_voice_for_subject(
                {"label": lbl, "kind": "person"})
            self.assertNotEqual(vid, narrator_id)

    def test_same_label_picks_same_voice_deterministically(self):
        v1 = self.engine.resolve_fallback_voice_for_subject(
            {"label": "warden", "kind": "person"})
        v2 = self.engine.resolve_fallback_voice_for_subject(
            {"label": "warden", "kind": "person"})
        v3 = self.engine.resolve_fallback_voice_for_subject(
            {"label": "WARDEN", "kind": "person"})
        self.assertEqual(v1, v2)
        self.assertEqual(v1, v3)

    def test_different_labels_spread_across_multiple_voices(self):
        # 12 distinct people labels should produce at least 4 distinct
        # voices — proves the hash distributes and we're not accidentally
        # collapsing everyone to the same fallback.
        labels = ["warden", "sheriff", "priest", "detective", "watcher",
                  "figure", "man", "operator", "hunter", "guard",
                  "captain", "cowboy"]
        chosen = {self.engine.resolve_fallback_voice_for_subject(
            {"label": lbl, "kind": "person"}) for lbl in labels}
        self.assertGreaterEqual(len(chosen), 4,
                                f"Only {len(chosen)} distinct voices for "
                                f"{len(labels)} distinct labels: {chosen}")

    def test_malformed_subject_still_returns_a_voice(self):
        for bad in [None, {}, {"label": None, "kind": None},
                    {"label": "", "kind": ""}]:
            vid = self.engine.resolve_fallback_voice_for_subject(bad)
            self.assertTrue(vid, f"empty subject {bad!r} returned {vid!r}")


def _live_gemini_key() -> str:
    try:
        import provider_bridge
        return provider_bridge.gemini_key()
    except Exception:
        return os.getenv("GEMINI_API_KEY") or ""


@unittest.skipUnless(
    os.getenv("SOMEWHERE_LIVE_VOICE_TEST") == "1" and _live_gemini_key(),
    "set SOMEWHERE_LIVE_VOICE_TEST=1 with a Gemini key to design a real voice",
)
class TestGeminiVoiceDesignLive(unittest.TestCase):
    """Live round-trip: design -> listed -> delete -> gone.

    Spends one voice design on the key's project and holds one of its 200
    slots for a few seconds. Cleans up on every path."""

    def setUp(self):
        self.vd = _reload_with_env(
            SOMEWHERE_DYNAMIC_VOICES="1",
            SOMEWHERE_DESIGN_BUDGET_PER_SESSION="8",
        )
        # The live test needs the real key but not the rest of "Gemini is
        # what is playing" (an ACCOUNT set to OpenAI, a mock override).
        self.vd._gemini_playing = lambda: True
        # A unique session so we don't collide with any live cache entry.
        self.session_id = "vdtest-" + str(int(time.time()))

    def tearDown(self):
        try:
            self.vd.release_session_voices(self.session_id)
        except Exception:
            pass

    def test_live_roundtrip(self):
        r = self.vd.get_or_design_voice(
            {"label": "test warden", "kind": "person"},
            self.session_id,
            wait=self.vd.DESIGN_TIMEOUT_SECONDS,
        )
        self.assertEqual(r["status"], "ready", f"live design failed: {r}")
        self.assertTrue(r["voice_id"].startswith("voice_"))
        voices, reason = self.vd._list_voices()
        print(f"\n[live] designed {r['voice_id']}; listing: {reason}, "
              f"{len(voices)} voice(s)")
        self.assertEqual(reason, "ok")
        self.assertIn(r["voice_id"], [v["id"] for v in voices])
        mine = next(v for v in voices if v["id"] == r["voice_id"])
        self.assertTrue(mine["display_name"].startswith("[dyn]"))
        age = self.vd._remote_age_hours(mine)
        self.assertIsNotNone(age)
        self.assertLess(age, 1.0, "age read back from expire_time is off")
        res = self.vd.release_session_voices(self.session_id)
        self.assertEqual(res["deleted"], 1)
        after, _ = self.vd._list_voices()
        self.assertNotIn(r["voice_id"], [v["id"] for v in after])


if __name__ == "__main__":
    unittest.main()
