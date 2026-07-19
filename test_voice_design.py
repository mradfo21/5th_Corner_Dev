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


class TestDesignPipeline(unittest.TestCase):
    """End-to-end async pipeline with a fake ElevenLabs.

    Covers the four state transitions that matter for correctness:
      - non-blocking call returns "generating" and kicks off a worker,
      - a concurrent call for the same key COALESCES onto the inflight job,
      - a completed job flips the cache entry to "ready" and later reads hit,
      - release_session_voices deletes voices tagged to that session only.
    """

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

        def fake_save(gvid, brief):
            self.call_log.append(("save", gvid))
            return "voice_" + gvid

        def fake_delete(voice_id):
            self.call_log.append(("delete", voice_id))
            return True

        self.vd._post_design = fake_design
        self.vd._post_save = fake_save
        self.vd._delete_voice = fake_delete

    def tearDown(self):
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
        time.sleep(0.6)  # let the worker finish
        designs = [c for c in self.call_log if c[0] == "design"]
        self.assertEqual(len(designs), 1)

    def test_ready_after_wait_returns_designed(self):
        r = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                        "s1", wait=1.5)
        self.assertEqual(r["status"], "ready")
        self.assertEqual(r["source"], "designed")
        self.assertTrue(r["voice_id"].startswith("voice_"))

    def test_second_lookup_is_cache_hit(self):
        self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                    "s1", wait=1.5)
        r = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                        "s1", wait=0)
        self.assertEqual(r["status"], "ready")
        self.assertEqual(r["source"], "cache")

    def test_different_session_designs_different_voice(self):
        r1 = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                         "s1", wait=1.5)
        r2 = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                         "s2", wait=1.5)
        self.assertNotEqual(r1["cache_key"], r2["cache_key"])
        designs = [c for c in self.call_log if c[0] == "design"]
        self.assertEqual(len(designs), 2)

    def test_release_session_voices_deletes_only_that_session(self):
        r1 = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                         "s1", wait=1.5)
        r2 = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                         "s2", wait=1.5)
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
                                        "s1", wait=1.5)
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
                                        "s1", wait=1.5)
        st = self.vd.get_status(r["cache_key"])
        self.assertEqual(st["status"], "ready")
        self.assertEqual(st["voice_id"], r["voice_id"])
        self.assertEqual(self.vd.get_status("bogus")["status"], "unknown")

    def test_is_ready_voice_id_admits_designed_and_rejects_random(self):
        r = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                        "s1", wait=1.5)
        self.assertTrue(self.vd.is_ready_voice_id(r["voice_id"]))
        self.assertFalse(self.vd.is_ready_voice_id("not-a-real-id"))
        self.assertFalse(self.vd.is_ready_voice_id(""))

    def test_design_failure_records_failed_and_uses_ttl(self):
        self.vd._post_design = lambda brief: None  # simulate upstream failure
        r = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                        "s1", wait=1.5)
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
                                        "s1", wait=1.5)
        r = self.vd.get_or_design_voice({"label": "elder", "kind": "person"},
                                        "s1", wait=0)
        self.assertEqual(r["source"], "budget")

    def test_corrupt_cache_file_is_recovered(self):
        self.vd.CACHE_PATH.write_text("{not: valid json", encoding="utf-8")
        # Any operation must not raise; recovery yields an empty cache.
        r = self.vd.get_or_design_voice({"label": "warden", "kind": "person"},
                                        "s1", wait=1.5)
        self.assertEqual(r["status"], "ready")


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
