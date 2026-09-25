"""
test_voice_design.py — offline unit tests for voice_design.py.

Every test in ``TestVoiceDesignUnit`` monkeypatches the three ElevenLabs
HTTP wrappers (``_post_design``, ``_post_save``, ``_delete_voice``) so the
suite runs with no network and no API key. The integration class at the
bottom is auto-skipped unless ``ELEVENLABS_API_KEY`` is set — it exercises
a full design -> save -> tts-round-trip -> delete cycle against the real
ElevenLabs API.

Run with:

    python3 -m unittest test_voice_design -v
"""

import json
import os
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List


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


class TestBriefBuilder(unittest.TestCase):
    """The brief builder is pure / deterministic — no I/O, no import-time
    side effects beyond reading env. These lock in the classifier outputs
    so a future refactor can't silently drift the prompt shape."""

    @classmethod
    def setUpClass(cls):
        cls.vd = _reload_with_env(ELEVENLABS_API_KEY="test-key",
                                  ELEVENLABS_DYNAMIC_VOICES="1")

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
        self.assertIn("1993 analog-horror", b["description"])
        # Sample lines are the ONLY thing ElevenLabs synthesizes into a
        # preview, so a shape regression here would degrade voice quality.
        self.assertGreater(len(b["sample_text"]), 10)

    def test_machine_uses_synthetic_environment(self):
        b = self._brief("rusted intercom", "machine")
        self.assertIn("synthetic voice", b["description"])
        self.assertIn("PA / intercom", b["description"])

    def test_creature_uses_uncanny_timbre(self):
        b = self._brief("shape in the doorway", "creature")
        self.assertIn("uncanny", b["description"])

    def test_high_chaos_frays_delivery(self):
        b = self._brief("courier", "person", chaos=9, phase="climax")
        self.assertIn("frayed", b["description"])
        self.assertIn("halting", b["description"])

    def test_female_hint_from_label(self):
        b = self._brief("old woman", "person")
        self.assertIn("elder female", b["description"])

    def test_young_hint_from_label(self):
        b = self._brief("young girl", "person")
        self.assertIn("young female", b["description"])

    def test_opening_line_becomes_sample_when_reasonable(self):
        opening = "The warden's close. Keep low, don't say my name."
        b = self._brief("warden", "person", opening_line=opening)
        self.assertEqual(b["sample_text"], opening)

    def test_short_or_empty_opening_falls_back_to_neutral(self):
        b = self._brief("figure", "person", opening_line="hi")
        self.assertNotEqual(b["sample_text"], "hi")

    def test_labels_include_source_tag_for_sweeper(self):
        b = self._brief("warden", "person")
        self.assertEqual(b["labels"]["source"], self.vd.LABEL_TAG)
        self.assertIn("subject_label", b["labels"])
        self.assertIn("subject_kind", b["labels"])
        self.assertIn("created_at", b["labels"])

    def test_description_within_elevenlabs_bounds(self):
        # ElevenLabs Voice Design requires 20 <= len <= 1000.
        long_scene = "x" * 3000
        b = self._brief("warden", "person", scene=long_scene)
        self.assertGreaterEqual(len(b["description"]), 20)
        self.assertLessEqual(len(b["description"]), 1000)

    def test_voice_name_is_short_and_prefixed(self):
        b = self._brief("warden", "person")
        self.assertTrue(b["voice_name"].startswith("[dyn]"))
        self.assertLessEqual(len(b["voice_name"]), 100)


class TestCacheKey(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vd = _reload_with_env(ELEVENLABS_API_KEY="test-key",
                                  ELEVENLABS_DYNAMIC_VOICES="1")

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


# An upper bound, not a pause: get_or_design_voice returns the moment the
# design lands. 1.5 s was enough on a dev machine and not on a busy CI runner,
# where these tests read "generating" (#162, #168).
READY_WAIT = 10.0


class TestDesignPipeline(unittest.TestCase):
    """End-to-end async pipeline with a fake ElevenLabs.

    Covers the four state transitions that matter for correctness:
      - non-blocking call returns "generating" and kicks off a worker,
      - a concurrent call for the same key COALESCES onto the inflight job,
      - a completed job flips the cache entry to "ready" and later reads hit,
      - release_session_voices deletes voices tagged to that session only.
    """

    @classmethod
    def setUpClass(cls):
        # A design records its cost through cost_tracker, whose first call
        # imports the whole engine — seconds on a cold CI runner, longer than
        # the wait=READY_WAIT these tests allow. Pay it once, here, not inside a
        # timed assertion ("'generating' != 'ready'" on #162's run).
        import cost_tracker  # noqa: F401
        import engine  # noqa: F401

    def setUp(self):
        # Fresh module + fresh cache file per test.
        self.vd = _reload_with_env(
            ELEVENLABS_API_KEY="test-key",
            ELEVENLABS_DYNAMIC_VOICES="1",
            ELEVENLABS_DESIGN_BUDGET_PER_SESSION="8",
            ELEVENLABS_DESIGN_CONCURRENCY="3",
        )
        if self.vd.CACHE_PATH.exists():
            self.vd.CACHE_PATH.unlink()

        self.call_log: List[tuple] = []

        def fake_design(brief):
            self.call_log.append(("design", brief["voice_name"]))
            time.sleep(0.15)  # simulate network
            return {"previews": [
                {"generated_voice_id": "gv_primary"},
                {"generated_voice_id": "gv_alt1"},
                {"generated_voice_id": "gv_alt2"},
            ]}

        self._save_n = 0

        def fake_save(gvid, brief):
            self._save_n += 1
            vid = f"voice_{gvid}_{self._save_n}"
            self.call_log.append(("save", gvid, vid))
            return vid

        def fake_delete(voice_id):
            self.call_log.append(("delete", voice_id))
            return True

        self.vd._post_design = fake_design
        self.vd._post_save = fake_save
        self.vd._delete_voice = fake_delete

    def _drain(self, seconds: float = 10.0) -> None:
        """Wait until every background design has finished (no sleeps that
        guess: a worker's first job imports the engine, which on a cold or
        busy machine takes longer than any fixed pause)."""
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
        if self.vd.CACHE_PATH.exists():
            self.vd.CACHE_PATH.unlink()

    def test_wait_zero_returns_generating(self):
        r = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                        "s1", wait=0)
        self.assertEqual(r["status"], "generating")
        self.assertIsNone(r["voice_id"])
        # A cache_key must be present so the client can poll status.
        self.assertTrue(r["cache_key"])

    def test_concurrent_identical_calls_coalesce(self):
        r1 = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                         "s1", wait=0)
        r2 = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                         "s1", wait=0)
        # Second caller must NOT trigger a duplicate design call.
        self.assertEqual(r1["cache_key"], r2["cache_key"])
        self._drain()  # let the worker finish, however long its first import takes
        designs = [c for c in self.call_log if c[0] == "design"]
        self.assertEqual(len(designs), 1)

    def test_ready_after_wait_returns_designed(self):
        r = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                        "s1", wait=READY_WAIT)
        self.assertEqual(r["status"], "ready")
        self.assertEqual(r["source"], "designed")
        self.assertTrue(r["voice_id"].startswith("voice_"))

    def test_second_lookup_is_cache_hit(self):
        self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                    "s1", wait=READY_WAIT)
        r = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                        "s1", wait=0)
        self.assertEqual(r["status"], "ready")
        self.assertEqual(r["source"], "cache")

    def test_regenerate_voice_uses_stored_description_and_redesigns(self):
        # First design lands a voice; regenerate must DROP the cache entry,
        # spend another design credit, and keep the caller's description seed.
        first = self.vd.get_or_design_voice(
            {"label": "kane", "kind": "person"}, "s1", wait=READY_WAIT
        )
        self.assertEqual(first["status"], "ready")
        seed = "a low gravelly wary male voice mid-40s tired, analog horror radio."
        self.assertGreaterEqual(len(seed), 20)
        designs_before = len([c for c in self.call_log if c[0] == "design"])
        regen = self.vd.regenerate_voice(
            {"label": "kane", "kind": "person"}, "s1", seed,
            old_voice_id=first["voice_id"], wait=READY_WAIT,
        )
        self.assertEqual(regen["status"], "ready")
        self.assertEqual(regen["description"], seed)
        self.assertNotEqual(regen["voice_id"], first["voice_id"])
        designs_after = len([c for c in self.call_log if c[0] == "design"])
        self.assertEqual(designs_after, designs_before + 1)
        # Old slot freed when refcount is zero.
        self.assertIn(("delete", first["voice_id"]), self.call_log)

    def test_description_override_skips_brief_builder(self):
        seed = "a thin metallic whisper for a machine called rusted intercom."
        r = self.vd.get_or_design_voice(
            {"label": "rusted intercom", "kind": "machine"}, "s1",
            wait=READY_WAIT, description_override=seed,
        )
        self.assertEqual(r["status"], "ready")
        self.assertEqual(r["description"], seed)

    def test_different_session_designs_different_voice(self):
        r1 = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                         "s1", wait=READY_WAIT)
        r2 = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                         "s2", wait=READY_WAIT)
        self.assertNotEqual(r1["cache_key"], r2["cache_key"])
        designs = [c for c in self.call_log if c[0] == "design"]
        self.assertEqual(len(designs), 2)

    def test_release_session_voices_deletes_only_that_session(self):
        r1 = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                         "s1", wait=READY_WAIT)
        r2 = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                         "s2", wait=READY_WAIT)
        res = self.vd.release_session_voices("s1")
        self.assertEqual(res["deleted"], 1)
        self.assertIn(r1["voice_id"], res["voice_ids"])
        # s2's voice still resolves from cache.
        r2b = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                          "s2", wait=0)
        self.assertEqual(r2b["voice_id"], r2["voice_id"])
        self.assertEqual(r2b["source"], "cache")

    def test_refcount_blocks_release_then_allows_after_release(self):
        r = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                        "s1", wait=READY_WAIT)
        self.vd.acquire(r["voice_id"])
        res1 = self.vd.release_session_voices("s1")
        self.assertEqual(res1["deleted"], 0)
        self.assertEqual(res1["skipped"], 1)
        # After the caller signals end-of-call, cleanup succeeds.
        self.vd.release(r["voice_id"])
        res2 = self.vd.release_session_voices("s1")
        self.assertEqual(res2["deleted"], 1)

    def test_get_status_reports_ready_and_unknown(self):
        r = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                        "s1", wait=READY_WAIT)
        st = self.vd.get_status(r["cache_key"])
        self.assertEqual(st["status"], "ready")
        self.assertEqual(st["voice_id"], r["voice_id"])
        self.assertEqual(self.vd.get_status("bogus")["status"], "unknown")

    def test_is_ready_voice_id_admits_designed_and_rejects_random(self):
        r = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                        "s1", wait=READY_WAIT)
        self.assertTrue(self.vd.is_ready_voice_id(r["voice_id"]))
        self.assertFalse(self.vd.is_ready_voice_id("not-a-real-id"))
        self.assertFalse(self.vd.is_ready_voice_id(""))

    def test_design_failure_records_failed_and_uses_ttl(self):
        self.vd._post_design = lambda brief: None  # simulate upstream failure
        r = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                        "s1", wait=READY_WAIT)
        self.assertEqual(r["status"], "failed")
        # Immediately re-asking must NOT re-attempt (TTL guards paid calls).
        r2 = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                         "s1", wait=0)
        self.assertEqual(r2["status"], "failed")

    def test_per_session_budget_falls_back_after_cap(self):
        self.vd = _reload_with_env(
            ELEVENLABS_API_KEY="test-key",
            ELEVENLABS_DYNAMIC_VOICES="1",
            ELEVENLABS_DESIGN_BUDGET_PER_SESSION="2",
        )
        # Re-monkeypatch after reload.
        self.vd._post_design = lambda brief: {"previews": [
            {"generated_voice_id": "gv_x"}]}
        self.vd._post_save = lambda gvid, brief: "voice_" + gvid
        self.vd._delete_voice = lambda vid: True
        for lbl in ("warden", "courier"):
            self.vd.get_or_design_voice({"label": lbl, "kind": "person"},
                                        "s1", wait=READY_WAIT)
        r = self.vd.get_or_design_voice({"label": "elder", "kind": "person"},
                                        "s1", wait=0)
        self.assertEqual(r["source"], "budget")

    def test_corrupt_cache_file_is_recovered(self):
        self.vd.CACHE_PATH.write_text("{not: valid json", encoding="utf-8")
        # Any operation must not raise; recovery yields an empty cache.
        r = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                        "s1", wait=READY_WAIT)
        self.assertEqual(r["status"], "ready")


class TestVoiceLibrary(unittest.TestCase):
    """The live ElevenLabs library — your voices, not the shipped vanilla
    roster — has to be what the game actually lists and accepts."""

    def setUp(self):
        self.vd = _reload_with_env(
            ELEVENLABS_API_KEY="sk_" + "a" * 40,
            ELEVENLABS_DYNAMIC_VOICES="1",
        )
        self.vd._LIBRARY_CACHE["voices"] = []
        self.vd._LIBRARY_CACHE["at"] = 0.0

    def tearDown(self):
        self.vd._LIBRARY_CACHE["voices"] = []
        self.vd._LIBRARY_CACHE["at"] = 0.0

    def test_a_dashboard_key_id_does_not_hit_the_network(self):
        vd = _reload_with_env(ELEVENLABS_API_KEY="6" * 64)
        called = []
        vd._page_v2_voices = lambda *a, **k: called.append("hit") or ([], 400)
        voices, reason = vd._list_workspace_voices(with_reason=True)
        self.assertEqual(reason, "bad_key")
        self.assertEqual(voices, [])
        self.assertFalse(called)
        self.assertEqual(vd.voice_library()["reason"], "bad_key")

    def test_listing_asks_for_personal_voices_first(self):
        seen = []

        def fake_page(headers, extra=None):
            seen.append((extra or {}).get("voice_type"))
            if (extra or {}).get("voice_type") == "non-community":
                return ([{"voice_id": "mine", "name": "Mine",
                          "category": "cloned"}], 200)
            return ([], 200)

        self.vd._page_v2_voices = fake_page
        voices, reason = self.vd._list_workspace_voices(with_reason=True)
        self.assertEqual(reason, "ok")
        self.assertEqual([v["voice_id"] for v in voices], ["mine"])
        self.assertEqual(seen[0], "non-community")
        self.assertNotIn(None, seen)

    def test_library_hides_premade_once_yours_exist(self):
        def fake_list(with_reason=False):
            raw = [
                {"voice_id": "custom_1", "name": "SW_Flesh",
                 "category": "generated", "description": "ours",
                 "labels": {"gender": "male"}},
                {"voice_id": "cjVigY5qzO86Huf0OWal", "name": "Eric",
                 "category": "premade", "description": "stock", "labels": {}},
            ]
            return (raw, "ok") if with_reason else raw

        self.vd._list_workspace_voices = fake_list
        lib = self.vd.voice_library(force=True)
        self.assertTrue(lib["ok"])
        self.assertEqual([v["id"] for v in lib["voices"]], ["custom_1"])
        self.assertEqual(lib["yours"], 1)
        self.assertTrue(self.vd.is_library_voice_id("custom_1"))
        self.assertFalse(self.vd.is_library_voice_id("cjVigY5qzO86Huf0OWal"))

    def test_library_hides_ephemeral_designed_voices(self):
        def fake_list(with_reason=False):
            raw = [
                {"voice_id": "keep_me", "name": "Clara (Drifter)",
                 "category": "generated", "labels": {}},
                {"voice_id": "tmp_dyn", "name": "[dyn] warden",
                 "category": "generated",
                 "labels": {"source": self.vd.LABEL_TAG}},
            ]
            return (raw, "ok") if with_reason else raw

        self.vd._list_workspace_voices = fake_list
        lib = self.vd.voice_library(force=True)
        self.assertEqual([v["id"] for v in lib["voices"]], ["keep_me"])


class TestFeatureDisabled(unittest.TestCase):
    def test_no_api_key_makes_is_available_false(self):
        vd = _reload_with_env(ELEVENLABS_API_KEY=None,
                              ELEVENLABS_DYNAMIC_VOICES="1")
        self.assertFalse(vd.is_available())

    def test_disabled_flag_returns_none_from_resolver(self):
        vd = _reload_with_env(ELEVENLABS_API_KEY="test-key",
                              ELEVENLABS_DYNAMIC_VOICES="0")
        r = vd.get_or_design_voice({"label": "warden", "kind": "person"}, "s1")
        self.assertIsNone(r)


class TestEngineResolver(unittest.TestCase):
    """Engine-level resolver: proves the wiring in engine.py always returns a
    usable voice_id even when the dynamic module fails / is disabled — so
    api_talk_session can never end up with an empty tts.voice_id."""

    def test_resolver_falls_back_when_module_disabled(self):
        _reload_with_env(ELEVENLABS_API_KEY=None,
                         ELEVENLABS_DYNAMIC_VOICES="1")
        import importlib
        import engine
        importlib.reload(engine)
        r = engine.resolve_voice_for_subject({"label": "warden", "kind": "person"},
                                             "default")
        self.assertTrue(r["voice_id"])
        self.assertEqual(r["status"], "disabled")

    def test_a_library_voice_is_valid_and_is_what_the_registry_shows(self):
        """Picking one of our ElevenLabs voices used to be thrown away by
        _valid_voice_id, so TALK always fell back to Eric from voices.json."""
        vd = _reload_with_env(ELEVENLABS_API_KEY="sk_" + "a" * 40,
                              ELEVENLABS_DYNAMIC_VOICES="1")
        vd._LIBRARY_CACHE["voices"] = [{
            "id": "custom_voice_abc", "name": "Ours", "category": "cloned",
            "tag": "yours", "gender": "male",
        }]
        vd._LIBRARY_CACHE["at"] = time.time()
        import importlib
        import engine
        importlib.reload(engine)
        self.assertEqual(engine._valid_voice_id("custom_voice_abc"),
                         "custom_voice_abc")
        self.assertEqual(engine._valid_voice_id("not-a-voice"), "")
        reg = engine.get_voice_registry()
        self.assertEqual(reg["source"], "library")
        self.assertEqual([v["id"] for v in reg["voices"]], ["custom_voice_abc"])
        self.assertEqual(reg["default"], "custom_voice_abc")
        self.assertEqual(engine._valid_voice_id("cjVigY5qzO86Huf0OWal"), "",
                         "a leftover stock id must not win once the library is live")
        vd._LIBRARY_CACHE["voices"] = []
        vd._LIBRARY_CACHE["at"] = 0.0

    def test_narrator_prefers_a_library_voice_named_narrator(self):
        vd = _reload_with_env(ELEVENLABS_API_KEY="sk_" + "a" * 40,
                              ELEVENLABS_DYNAMIC_VOICES="1")
        vd._LIBRARY_CACHE["voices"] = [
            {"id": "aaa_bread", "name": "Baking Bread", "category": "cloned"},
            {"id": "narr_ours", "name": "Defect - Narrator", "category": "generated"},
            {"id": "clara_1", "name": "Clara (Drifter)", "category": "generated"},
        ]
        vd._LIBRARY_CACHE["at"] = time.time()
        import importlib
        import engine
        importlib.reload(engine)
        # Read from the registry rather than hardcoded: the shipped narrator id
        # changes when the voice is recast, and a hardcoded id that is no longer
        # in voices.json is not a stock leftover — it is an id the guard is
        # supposed to honour, so the test would pass its own premise by mistake.
        engine.ELEVENLABS_NARRATOR_VOICE_ID = engine.VOICES_CONFIG["narrator_voice"]
        engine.ELEVENLABS_VOICE_ID = ""
        self.assertEqual(engine._narrator_voice_id(), "narr_ours")
        self.assertEqual(engine._default_voice_id(), "clara_1")
        vd._LIBRARY_CACHE["voices"] = []
        vd._LIBRARY_CACHE["at"] = 0.0


class TestFallbackVoiceForSubject(unittest.TestCase):
    """The smart fallback voice picker: hashes subject label into a
    gender/kind-filtered pool of roster voices so different characters
    sound different even before a per-character voice is designed."""

    @classmethod
    def setUpClass(cls):
        # A leftover sk_ key + library cache from TestVoiceLibrary would
        # replace the shipped roster and break the gender-pool assertions.
        cls.vd = _reload_with_env(ELEVENLABS_API_KEY="test-key")
        cls.vd._LIBRARY_CACHE["voices"] = []
        cls.vd._LIBRARY_CACHE["at"] = 0.0
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
        # Narrator voice is "the archive voice" flavor and would break the
        # fiction if a random character got it.
        narrator_ids = {v["id"] for v in self.engine.VOICES_CONFIG["voices"]
                        if (v.get("name") or "").lower() == "narrator"}
        # Sample a broad label matrix to make an accidental narrator pick
        # extremely unlikely to slip through unnoticed.
        for lbl in ["figure", "shape", "watcher", "voice", "silhouette",
                    "presence", "man", "woman", "creature", "child"]:
            vid = self.engine.resolve_fallback_voice_for_subject(
                {"label": lbl, "kind": "person"})
            self.assertNotIn(vid, narrator_ids)

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
        # collapsing everyone to the same fallback (which would defeat the
        # whole point of this function).
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


@unittest.skipUnless(
    os.getenv("ELEVENLABS_API_KEY"),
    "no ELEVENLABS_API_KEY — skipping live integration test",
)
class TestElevenLabsIntegration(unittest.TestCase):
    """Live round-trip: design -> save -> confirm listed -> delete -> gone.

    Only runs when ``ELEVENLABS_API_KEY`` is set. Costs a small amount of
    Voice Design credit per run + occupies a slot momentarily. Safe on any
    paid tier (fails cleanly and cleans up on error paths)."""

    def setUp(self):
        self.vd = _reload_with_env(
            ELEVENLABS_DYNAMIC_VOICES="1",
            ELEVENLABS_DESIGN_BUDGET_PER_SESSION="8",
        )
        # Force a unique session so we don't collide with any live cache.
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
        self.assertTrue(r["voice_id"])
        # The voice should be listed in the workspace under our tag.
        listed = self.vd._list_workspace_voices()
        ids = [v.get("voice_id") for v in listed if isinstance(v, dict)]
        self.assertIn(r["voice_id"], ids)
        # Release + confirm the slot is reclaimed.
        res = self.vd.release_session_voices(self.session_id)
        self.assertGreaterEqual(res["deleted"], 1)


if __name__ == "__main__":
    unittest.main()
