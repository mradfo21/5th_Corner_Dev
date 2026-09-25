"""Local BYOK store — persist, mask, refuse leaks, local-only writes."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
import ai_provider_manager as apm
import keys_store


class KeysStoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "keys.env"
        keys_store.set_store_path(self.path)
        self.addCleanup(keys_store.set_store_path, None)
        self._saved = {env: os.environ.get(env) for env in keys_store._KNOWN_ENV}
        for env in keys_store._KNOWN_ENV:
            os.environ.pop(env, None)
        keys_store._applied_from_store.clear()
        keys_store._explicit_mock = False
        apm.set_backend_override(None)
        os.environ.pop("STORYGEN_BACKEND", None)

    def tearDown(self):
        for env, val in self._saved.items():
            if val is None:
                os.environ.pop(env, None)
            else:
                os.environ[env] = val
        keys_store._explicit_mock = False
        apm.set_backend_override(None)

    def test_set_and_reload_round_trip(self):
        keys_store.set_key("gemini", "AIzaSy-test-key-123456")
        self.assertEqual(os.environ.get("GEMINI_API_KEY"), "AIzaSy-test-key-123456")
        os.environ.pop("GEMINI_API_KEY", None)
        keys_store.load_into_environ()
        self.assertEqual(os.environ.get("GEMINI_API_KEY"), "AIzaSy-test-key-123456")

    def test_status_never_contains_the_secret(self):
        secret = "AIzaSy-super-secret-value"
        keys_store.set_key("gemini", secret)
        blob = json.dumps(keys_store.public_status(editable=True))
        self.assertNotIn(secret, blob)
        self.assertNotIn("super-secret", blob)
        gemini = next(p for p in keys_store.public_status(editable=True)["providers"]
                      if p["id"] == "gemini")
        self.assertTrue(gemini["set"])
        self.assertEqual(gemini["hint"], secret[-4:])

    def test_hosted_status_omits_even_the_hint(self):
        keys_store.set_key("krea", "krea-secret-key-value")
        hosted = keys_store.public_status(editable=False)
        krea = next(p for p in hosted["providers"] if p["id"] == "krea")
        self.assertTrue(krea["set"])
        self.assertEqual(krea["hint"], "")
        self.assertFalse(hosted["editable"])

    def test_clear_removes_from_file_and_environ(self):
        keys_store.set_key("openai", "sk-openai-test-key-1")
        keys_store.set_key("openai", "")
        self.assertNotIn("OPENAI_API_KEY", os.environ)
        self.assertNotIn("OPENAI_API_KEY", self.path.read_text(encoding="utf-8"))

    def test_newlines_are_rejected(self):
        with self.assertRaises(ValueError):
            keys_store.set_key("gemini", "AIza-good\nGEMINI_API_KEY=stolen")

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(KeyError):
            keys_store.set_key("stripe", "sk_test_not_a_provider")

    def test_refuses_to_write_inside_the_repo(self):
        repo_file = Path(__file__).resolve().parent / "keys.env"
        with self.assertRaises(ValueError):
            keys_store.set_key("gemini", "AIzaSy-should-not-land", path=repo_file)
        self.assertFalse(repo_file.exists())

    def test_refuses_playtest_results_path(self):
        dest = Path(__file__).resolve().parent / "playtest_results" / "keys.env"
        with self.assertRaises(ValueError):
            keys_store.set_key("gemini", "AIzaSy-should-not-land", path=dest)

    def test_catalogue_gains_krea_after_a_key_is_saved(self):
        apm.set_backend_override(None)
        os.environ.pop("STORYGEN_BACKEND", None)
        os.environ.pop("KREA_API_KEY", None)
        before = {e["id"] for e in apm.available_model_catalogue("image")}
        self.assertNotIn("krea-2/medium", before)
        keys_store.set_key("krea", "krea-live-key-abc")
        after = {e["id"] for e in apm.available_model_catalogue("image")}
        self.assertIn("krea-2/medium", after)

    def test_elevenlabs_is_not_a_provider_any_more(self):
        """The game runs on one key (docs/plans/ONE_KEY_AUDIO_PLAN.md). ACCOUNT's
        More keys is drawn from PROVIDERS, so a row here is a row on screen."""
        ids = {p["id"] for p in keys_store.public_status(editable=True)["providers"]}
        self.assertNotIn("elevenlabs", ids)
        self.assertNotIn("ELEVENLABS_API_KEY", keys_store._KNOWN_ENV)
        with self.assertRaises(KeyError):
            keys_store.set_key("elevenlabs", "sk_" + "a" * 40)
        self.assertFalse(any(env == "ELEVENLABS_API_KEY"
                             for _mod, _attr, env in keys_store._RUNTIME_ATTRS))

    def test_a_retired_key_in_keys_env_is_ignored_and_kept(self):
        """An existing player's keys.env still has the line. It must not crash,
        reach the process or ACCOUNT - and must not be deleted by the next
        unrelated save either: the secret is theirs and was shown once."""
        secret = "sk_" + "r" * 40
        prev = os.environ.pop("ELEVENLABS_API_KEY", None)
        self.addCleanup(lambda: prev is None or os.environ.__setitem__(
            "ELEVENLABS_API_KEY", prev))
        self.path.write_text(
            "GEMINI_API_KEY=AIzaSy-already-here-1234\n"
            f"ELEVENLABS_API_KEY={secret}\n", encoding="utf-8")
        keys_store.load_into_environ()
        self.assertEqual(os.environ.get("GEMINI_API_KEY"), "AIzaSy-already-here-1234")
        self.assertNotIn("ELEVENLABS_API_KEY", os.environ)
        blob = json.dumps(keys_store.public_status(editable=True))
        self.assertNotIn(secret, blob)
        self.assertNotIn("elevenlabs", blob.lower())
        keys_store.set_key("krea", "krea-secret-key-value")
        text = self.path.read_text(encoding="utf-8")
        self.assertIn(f"ELEVENLABS_API_KEY={secret}", text)
        self.assertIn("KREA_API_KEY=krea-secret-key-value", text)
        self.assertIn("GEMINI_API_KEY=AIzaSy-already-here-1234", text)
        self.assertNotIn("ELEVENLABS_API_KEY", os.environ)

    def test_saving_gemini_does_not_blank_another_runtime_key(self):
        """refresh_runtime_keys once wrote EVERY cached attr from getenv on each
        save, blanking keys that lived in config.json / the process env."""
        import engine
        prev = engine.OPENAI_API_KEY
        try:
            engine.OPENAI_API_KEY = "sk-keep-this-config-key-please"
            keys_store.set_key("gemini", "AIzaSy-other-provider-key")
            self.assertEqual(engine.OPENAI_API_KEY, "sk-keep-this-config-key-please")
        finally:
            engine.OPENAI_API_KEY = prev

    def test_explicit_mock_is_not_lifted(self):
        keys_store.mark_explicit_mock()
        apm.set_backend_override("mock")
        keys_store.set_key("gemini", "AIzaSy-live-enough")
        self.assertEqual(apm.get_backend_override(), "mock")

    def test_has_provider_key_rereads_the_store_when_environ_is_empty(self):
        keys_store.set_key("reactor", "reactor-secret-key-ok")
        os.environ.pop("REACTOR_API_KEY", None)
        self.assertTrue(keys_store.has_provider_key("reactor"))
        self.assertEqual(os.environ.get("REACTOR_API_KEY"), "reactor-secret-key-ok")

    def test_auto_mock_lifts_when_a_play_key_arrives(self):
        apm.set_backend_override("mock")
        os.environ["STORYGEN_BACKEND"] = "mock"
        status = keys_store.set_key("gemini", "AIzaSy-live-enough")
        self.assertTrue(status["lifted_mock"])
        self.assertIsNone(apm.get_backend_override())
        self.assertNotEqual(os.environ.get("STORYGEN_BACKEND"), "mock")


class KeysApiCase(unittest.TestCase):
    def setUp(self):
        import api
        self.api = api
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        keys_store.set_store_path(Path(self._tmp.name) / "keys.env")
        self.addCleanup(keys_store.set_store_path, None)
        self._saved = {env: os.environ.get(env) for env in keys_store._KNOWN_ENV}
        for env in keys_store._KNOWN_ENV:
            os.environ.pop(env, None)
        keys_store._applied_from_store.clear()
        keys_store._explicit_mock = False
        api._local_keys_armed = False
        self.client = api.app.test_client()

    def tearDown(self):
        self.api._local_keys_armed = False
        for env, val in self._saved.items():
            if val is None:
                os.environ.pop(env, None)
            else:
                os.environ[env] = val

    def test_unarmed_put_is_403(self):
        resp = self.client.put("/api/keys", json={"id": "gemini", "value": "AIzaSy-nope-1234"})
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("AIzaSy-nope-1234", resp.get_data(as_text=True))

    def test_remote_put_is_403_even_when_armed(self):
        self.api.enable_local_keys()
        resp = self.client.put(
            "/api/keys",
            json={"id": "gemini", "value": "AIzaSy-nope-1234"},
            environ_overrides={"REMOTE_ADDR": "10.0.0.7"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("local-only", resp.get_json()["error"])

    def test_local_put_saves_and_get_does_not_echo(self):
        self.api.enable_local_keys()
        secret = "AIzaSy-from-the-ui-ok"
        resp = self.client.put("/api/keys", json={"id": "gemini", "value": secret})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertNotIn(secret, body)
        got = self.client.get("/api/keys")
        self.assertEqual(got.status_code, 200)
        data = got.get_json()
        self.assertTrue(data["editable"])
        self.assertTrue(data["play_ready"])
        gemini = next(p for p in data["providers"] if p["id"] == "gemini")
        self.assertTrue(gemini["set"])
        self.assertEqual(gemini["hint"], secret[-4:])
        self.assertNotIn(secret, got.get_data(as_text=True))

    def test_reactor_config_is_enabled_when_the_key_is_only_in_the_store(self):
        keys_store.set_key("reactor", "reactor-from-store-key")
        os.environ.pop("REACTOR_API_KEY", None)
        resp = self.client.get("/api/reactor/config")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["enabled"])
        self.assertEqual(resp.headers.get("Cache-Control"), "no-store")

    def test_get_is_safe_when_unarmed(self):
        os.environ["GEMINI_API_KEY"] = "AIzaSy-host-secret-key"
        resp = self.client.get("/api/keys")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data["editable"])
        self.assertNotIn("AIzaSy-host-secret-key", resp.get_data(as_text=True))
        gemini = next(p for p in data["providers"] if p["id"] == "gemini")
        self.assertTrue(gemini["set"])
        self.assertEqual(gemini["hint"], "")


if __name__ == "__main__":
    unittest.main()
