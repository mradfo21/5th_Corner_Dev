"""ACCOUNT's provider choice, and OpenAI answering the game's Gemini calls.

    python -m unittest test_provider_bridge -v

No network: OpenAI is a fake that records what it was sent. The invariants:

  * with OpenAI chosen (and its key), a Gemini generateContent POST from any
    module never reaches Google — text, vision, JSON-with-schema, pictures
    with reference images, dictation — and comes back Gemini-shaped;
  * with Gemini chosen, nothing is intercepted;
  * ai_config's wire getters say "gemini" under OpenAI whatever the config
    names (Krea pictures, a Claude narrator), so every call site takes the
    Gemini path the bridge understands; the player-facing label says openai;
  * a key pasted under the wrong provider is refused with the right name;
  * a parameter a model refuses is dropped and the call retried, once learned;
  * the cost ledger books OpenAI, not the Gemini the caller thinks it called.
"""

from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests
from PIL import Image

import ai_provider_manager
import keys_store
import provider_bridge


def _png(w: int, h: int, colour=(10, 120, 60)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(out, format="PNG")
    return out.getvalue()


class _FakeResp:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body
        self.ok = 200 <= status < 300
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeOpenAI:
    """Stands in for provider_bridge._post_openai."""

    def __init__(self):
        self.calls = []
        self.refuse = {}          # path -> param refused once with a 400

    def __call__(self, key, path, *, json_body=None, data=None, files=None, timeout=60.0):
        self.calls.append({"key": key, "path": path, "json": json_body, "data": data,
                           "files": files, "timeout": timeout})
        sent = json_body or data or {}
        param = self.refuse.get(path)
        if param and param in sent:
            return _FakeResp(400, {"error": {"message": f"Unsupported parameter: '{param}' is not supported with this model.",
                                             "param": param, "code": "unsupported_parameter"}})
        if path == "/chat/completions":
            wants_json = (json_body or {}).get("response_format", {}).get("type") == "json_object"
            content = '{"dispatch": "The fence gives.", "relocated": false}' if wants_json else "The fence gives."
            return _FakeResp(200, {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}})
        if path in ("/images/generations", "/images/edits"):
            size = (json_body or data or {}).get("size", "1536x1024")
            w, h = (int(x) for x in str(size).split("x"))
            return _FakeResp(200, {"data": [{"b64_json": base64.b64encode(_png(w, h)).decode()}],
                                   "usage": {"input_tokens": 300, "output_tokens": 1000,
                                             "input_tokens_details": {"text_tokens": 100, "image_tokens": 200}}})
        if path == "/audio/transcriptions":
            return _FakeResp(200, {"text": " go left "})
        return _FakeResp(404, {"error": {"message": "no such path"}})


GEMINI_TEXT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"
GEMINI_IMAGE = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite-image:generateContent"


class BridgeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        keys_store.set_store_path(Path(self.tmp.name) / "keys.env")
        provider_bridge._settings_cache = None
        provider_bridge._unsupported.clear()
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "CUSTOM_TEXT_MODEL", "STORYGEN_BACKEND"):
            os.environ.pop(k, None)
        ai_provider_manager.set_backend_override(None)
        # A custom key rewrites the narrator in ai_config.json: on a copy.
        cfg = Path(self.tmp.name) / "ai_config.json"
        cfg.write_bytes(Path(ai_provider_manager.AI_CONFIG_PATH).read_bytes())
        self.cfg = mock.patch.object(ai_provider_manager, "AI_CONFIG_PATH", cfg)
        self.cfg.start()
        ai_provider_manager._cached_config = None
        self.fake = FakeOpenAI()
        self.post = mock.patch.object(provider_bridge, "_post_openai", self.fake)
        self.post.start()
        # engine may be imported by other suites; its cached key must not count.
        self.engine_key = mock.patch("provider_bridge.gemini_key", lambda: (os.environ.get("GEMINI_API_KEY") or "").strip())
        self.engine_key.start()

    def tearDown(self):
        self.engine_key.stop()
        self.post.stop()
        self.cfg.stop()
        ai_provider_manager._cached_config = None
        self.env.stop()
        keys_store.set_store_path(None)
        provider_bridge._settings_cache = None
        self.tmp.cleanup()

    def _choose_openai(self):
        os.environ["OPENAI_API_KEY"] = "sk-test-0000000000000000"
        provider_bridge.set_provider("openai")

    # ── which provider plays ───────────────────────────────────────────

    def test_effective_provider_follows_the_choice_and_the_keys(self):
        self.assertEqual(provider_bridge.effective_provider(), "")
        os.environ["GEMINI_API_KEY"] = "AIza-test-000000000000"
        self.assertEqual(provider_bridge.effective_provider(), "gemini")
        provider_bridge.set_provider("openai")
        # Chosen but no OpenAI key: keep playing on the key that exists.
        self.assertEqual(provider_bridge.effective_provider(), "gemini")
        os.environ["OPENAI_API_KEY"] = "sk-test-0000000000000000"
        self.assertEqual(provider_bridge.effective_provider(), "openai")
        self.assertTrue(provider_bridge.active())
        provider_bridge.set_provider("gemini")
        self.assertFalse(provider_bridge.active())

    def test_gemini_is_the_default_and_openai_only_plays_when_chosen(self):
        # Both keys in a .env and nothing chosen: Gemini, exactly as before.
        os.environ["GEMINI_API_KEY"] = "AIza-test-000000000000"
        os.environ["OPENAI_API_KEY"] = "sk-test-0000000000000000"
        self.assertEqual(provider_bridge.effective_provider(), "gemini")
        self.assertFalse(provider_bridge.active())
        # An OpenAI key alone is not a choice: nothing is bridged.
        os.environ.pop("GEMINI_API_KEY")
        self.assertEqual(provider_bridge.effective_provider(), "")
        self.assertFalse(provider_bridge.active())
        self.assertFalse(provider_bridge.can_call_gemini_api())
        provider_bridge.set_provider("openai")
        self.assertEqual(provider_bridge.effective_provider(), "openai")
        self.assertTrue(provider_bridge.can_call_gemini_api())

    def test_mock_mode_is_never_bridged(self):
        self._choose_openai()
        ai_provider_manager.set_backend_override("mock")
        try:
            self.assertFalse(provider_bridge.active())
            self.assertEqual(provider_bridge.backend_label(), "mock")
        finally:
            ai_provider_manager.set_backend_override(None)

    def test_the_choice_is_stored_beside_the_keys_not_in_the_install(self):
        provider_bridge.set_provider("openai")
        path = Path(self.tmp.name) / "account.json"
        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(path.read_text())["provider"], "openai")
        self.assertNotIn("sk-", path.read_text())

    def test_wire_getters_say_gemini_under_openai(self):
        cfg = {"text_provider": "anthropic", "text_model": "claude-sonnet-4-5",
               "image_provider": "krea", "image_model": "krea-2/medium"}
        with mock.patch.object(ai_provider_manager, "load_ai_config", return_value=cfg):
            self.assertEqual(ai_provider_manager.get_text_provider(), "anthropic")
            self._choose_openai()
            self.assertEqual(ai_provider_manager.get_text_provider(), "gemini")
            self.assertEqual(ai_provider_manager.get_image_provider(), "gemini")
            self.assertTrue(ai_provider_manager.get_text_model().startswith("gemini"))
            self.assertTrue(ai_provider_manager.get_image_model().startswith("gemini"))
            self.assertEqual(provider_bridge.backend_label(), "openai")

    def test_backend_label_names_mixed_gemini_setups(self):
        os.environ["GEMINI_API_KEY"] = "AIza-test-000000000000"
        cfg = {"text_provider": "gemini", "text_model": "gemini-3.1-flash-lite",
               "image_provider": "krea", "image_model": "krea-2/medium"}
        with mock.patch.object(ai_provider_manager, "load_ai_config", return_value=cfg):
            self.assertEqual(provider_bridge.backend_label(), "gemini + krea medium")
        cfg["image_provider"] = "gemini"
        with mock.patch.object(ai_provider_manager, "load_ai_config", return_value=cfg):
            self.assertEqual(provider_bridge.backend_label(), "gemini")

    # ── ACCOUNT's save path ───────────────────────────────────────────

    def test_a_key_under_the_wrong_provider_is_named(self):
        with self.assertRaises(ValueError) as e:
            keys_store.choose_provider("gemini", "sk-proj-abcdefghijklmnop")
        self.assertIn("OpenAI", str(e.exception))
        with self.assertRaises(ValueError) as e:
            keys_store.choose_provider("openai", "AIzaSyabcdefghijklmnop")
        self.assertIn("Gemini", str(e.exception))

    def test_choosing_openai_with_a_key_saves_both_and_never_echoes(self):
        status = keys_store.choose_provider("openai", "sk-proj-abcdefghijklmnop1234")
        self.assertEqual(status["ai"]["provider"], "openai")
        self.assertNotIn("sk-proj-abcdefghijklmnop1234", json.dumps(status))
        self.assertEqual(os.environ.get("OPENAI_API_KEY"), "sk-proj-abcdefghijklmnop1234")
        self.assertIn("OPENAI_API_KEY=", (Path(self.tmp.name) / "keys.env").read_text())

    def test_choosing_openai_over_a_custom_key_needs_a_real_one(self):
        keys_store.set_custom("http://localhost:11434/v1", "llama3.1", "")
        with self.assertRaises(ValueError):
            keys_store.choose_provider("openai", "")
        status = keys_store.choose_provider("openai", "sk-proj-abcdefghijklmnop1234")
        self.assertFalse(status["custom"]["set"])
        self.assertEqual(provider_bridge.effective_provider(), "openai")

    # ── the calls themselves ──────────────────────────────────────────

    def test_nothing_is_intercepted_on_gemini(self):
        os.environ["GEMINI_API_KEY"] = "AIza-test-000000000000"
        os.environ["OPENAI_API_KEY"] = "sk-test-0000000000000000"
        provider_bridge.set_provider("gemini")
        wire = []

        def send(adapter, req, **kw):
            wire.append(req.url)
            r = requests.models.Response()
            r.status_code, r._content, r.url = 200, b'{"candidates": []}', req.url
            return r
        with mock.patch("requests.adapters.HTTPAdapter.send", send):
            r = requests.post(GEMINI_TEXT, json={"contents": [{"parts": [{"text": "hi"}]}]}, timeout=5)
        self.assertEqual(wire, [GEMINI_TEXT])
        self.assertFalse(getattr(r, "_bridged", False))
        self.assertEqual(self.fake.calls, [])

    def test_story_with_a_schema_and_an_image_becomes_a_json_chat(self):
        self._choose_openai()
        img = base64.b64encode(_png(64, 48)).decode()
        payload = {
            "contents": [{"parts": [{"inlineData": {"mimeType": "image/jpeg", "data": img}},
                                    {"text": "The player cuts the fence."}]}],
            "generationConfig": {"temperature": 0.8, "maxOutputTokens": 900,
                                 "responseMimeType": "application/json",
                                 "responseSchema": {"type": "OBJECT", "properties": {
                                     "dispatch": {"type": "STRING"}, "relocated": {"type": "BOOLEAN", "nullable": True}},
                                     "required": ["dispatch"], "propertyOrdering": ["dispatch", "relocated"]}},
            "safetySettings": [{"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"}],
        }
        status, body = provider_bridge.answer_gemini_request(GEMINI_TEXT, payload, timeout=25)
        self.assertEqual(status, 200)
        text = body["candidates"][0]["content"]["parts"][0]["text"]
        self.assertEqual(json.loads(text)["dispatch"], "The fence gives.")
        sent = self.fake.calls[-1]["json"]
        self.assertEqual(sent["model"], provider_bridge.openai_models()["text"])
        self.assertEqual(sent["response_format"], {"type": "json_object"})
        system = sent["messages"][0]["content"]
        self.assertIn('"type":"object"', system)
        self.assertIn('"type":["boolean","null"]', system)
        self.assertNotIn("propertyOrdering", system)
        user = sent["messages"][1]["content"]
        self.assertTrue(user[0]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertGreaterEqual(self.fake.calls[-1]["timeout"], provider_bridge.TEXT_TIMEOUT_FLOOR)
        self.assertEqual(body["usageMetadata"]["candidatesTokenCount"], 30)

    def _picture_payload(self, ar="16:9"):
        ref = base64.b64encode(_png(320, 180, (200, 10, 10))).decode()
        return {
            "contents": [{"parts": [{"text": "Reference 1: the previous moment."},
                                    {"inlineData": {"mimeType": "image/png", "data": ref}},
                                    {"text": "Reference 2: who you play."},
                                    {"inlineData": {"mimeType": "image/png", "data": ref}},
                                    {"text": "Draw the player climbing the fence."}]}],
            "generationConfig": {"responseModalities": ["IMAGE"],
                                 "imageConfig": {"aspectRatio": ar, "imageSize": "1K"}},
        }

    def _size_of(self, body):
        part = body["candidates"][0]["content"]["parts"][0]["inlineData"]
        return Image.open(io.BytesIO(base64.b64decode(part["data"]))).size

    def test_a_picture_with_references_is_an_edit_at_the_exact_ratio(self):
        self._choose_openai()
        status, body = provider_bridge.answer_gemini_request(GEMINI_IMAGE, self._picture_payload(), timeout=30)
        self.assertEqual(status, 200)
        call = self.fake.calls[-1]
        self.assertEqual(call["path"], "/images/edits")
        sent = call["json"]
        self.assertEqual(len(sent["images"]), 2)
        self.assertTrue(sent["images"][0]["image_url"].startswith("data:image/png;base64,"))
        self.assertEqual(sent["size"], "1536x864")
        self.assertIn("(input image 2)", sent["prompt"])
        self.assertNotIn("cropped", sent["prompt"])
        self.assertEqual(sent["input_fidelity"], "high")
        self.assertGreaterEqual(call["timeout"], provider_bridge.IMAGE_TIMEOUT_FLOOR)
        self.assertEqual(self._size_of(body), (1536, 864))

    def test_a_model_without_custom_sizes_gets_a_classic_one_cropped(self):
        self._choose_openai()
        provider_bridge._std_sizes_only.clear()
        self.fake.refuse["/images/edits"] = "size"
        real = self.fake.__call__

        def once(key, path, **kw):
            body = kw.get("json_body") or kw.get("data") or {}
            if path == "/images/edits" and body.get("size") != "1536x1024":
                return _FakeResp(400, {"error": {"message": "Invalid value for 'size'.", "param": "size",
                                                 "code": "invalid_value"}})
            self.fake.refuse.pop("/images/edits", None)
            return real(key, path, **kw)
        with mock.patch.object(provider_bridge, "_post_openai", once):
            status, body = provider_bridge.answer_gemini_request(GEMINI_IMAGE, self._picture_payload(), timeout=30)
        self.assertEqual(status, 200)
        sent = self.fake.calls[-1]["json"]
        self.assertEqual(sent["size"], "1536x1024")
        self.assertIn("cropped to 16:9", sent["prompt"])
        w, h = self._size_of(body)
        self.assertAlmostEqual(w / h, 16 / 9, places=2)
        provider_bridge._std_sizes_only.clear()

    def test_edits_fall_back_to_multipart_when_json_images_are_refused(self):
        self._choose_openai()
        provider_bridge._multipart_only.clear()
        self.fake.refuse["/images/edits"] = "images"
        status, _ = provider_bridge.answer_gemini_request(GEMINI_IMAGE, self._picture_payload(), timeout=30)
        self.assertEqual(status, 200)
        call = self.fake.calls[-1]
        self.assertIsNone(call["json"])
        self.assertEqual([f[0] for f in call["files"]], ["image[]", "image[]"])
        provider_bridge._multipart_only.clear()

    def test_a_picture_without_references_is_a_generation(self):
        self._choose_openai()
        payload = {"contents": [{"parts": [{"text": "A desert fence at dusk."}]}],
                   "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "4:3"}}}
        status, body = provider_bridge.answer_gemini_request(GEMINI_IMAGE, payload)
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.calls[-1]["path"], "/images/generations")
        w, h = self._size_of(body)
        self.assertAlmostEqual(w / h, 4 / 3, places=2)
        self.assertEqual(self.fake.calls[-1]["json"]["size"], "1536x1152")

    def test_dictation_goes_to_transcription(self):
        self._choose_openai()
        payload = {"contents": [{"parts": [{"text": "Return ONLY the words actually spoken."},
                                           {"inlineData": {"mimeType": "audio/webm;codecs=opus",
                                                           "data": base64.b64encode(b"\x1a\x45" * 400).decode()}}]}]}
        status, body = provider_bridge.answer_gemini_request(GEMINI_TEXT, payload)
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.calls[-1]["path"], "/audio/transcriptions")
        self.assertEqual(body["candidates"][0]["content"]["parts"][0]["text"], "go left")

    def test_a_refused_parameter_is_dropped_and_remembered(self):
        self._choose_openai()
        self.fake.refuse["/chat/completions"] = "temperature"
        payload = {"contents": [{"parts": [{"text": "hello"}]}], "generationConfig": {"temperature": 0.7}}
        status, _ = provider_bridge.answer_gemini_request(GEMINI_TEXT, payload)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.fake.calls), 2)
        self.assertNotIn("temperature", self.fake.calls[-1]["json"])
        provider_bridge.answer_gemini_request(GEMINI_TEXT, payload)
        self.assertEqual(len(self.fake.calls), 3)   # learned: no second 400

    def test_reasoning_effort_steps_down_until_the_model_takes_it(self):
        self._choose_openai()
        provider_bridge._effort.clear()
        seen = []
        real = self.fake.__call__

        def picky(key, path, **kw):
            body = kw.get("json_body") or {}
            seen.append(body.get("reasoning_effort"))
            if body.get("reasoning_effort") == "none":
                return _FakeResp(400, {"error": {"message": "Unsupported value: 'reasoning_effort' does not support 'none' with this model.",
                                                 "param": "reasoning_effort", "code": "unsupported_value"}})
            return real(key, path, **kw)
        with mock.patch.object(provider_bridge, "_post_openai", picky):
            status, _ = provider_bridge.answer_gemini_request(GEMINI_TEXT, {"contents": [{"parts": [{"text": "hi"}]}]})
            provider_bridge.answer_gemini_request(GEMINI_TEXT, {"contents": [{"parts": [{"text": "hi"}]}]})
        self.assertEqual(status, 200)
        self.assertEqual(seen, ["none", "minimal", "minimal"])
        provider_bridge._effort.clear()

    def test_the_hook_answers_requests_post_from_any_module(self):
        self._choose_openai()
        r = requests.post(GEMINI_TEXT, headers={"x-goog-api-key": ""},
                          json={"contents": [{"parts": [{"text": "hello"}]}]}, timeout=20)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(getattr(r, "_bridged", False))
        self.assertEqual(r.json()["candidates"][0]["content"]["parts"][0]["text"], "The fence gives.")
        self.assertEqual(self.fake.calls[-1]["path"], "/chat/completions")

    def test_the_lore_cache_is_refused_not_sent_to_google(self):
        self._choose_openai()
        r = requests.post("https://generativelanguage.googleapis.com/v1beta/cachedContents",
                          json={"model": "x"}, timeout=5)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.fake.calls, [])

    def test_the_ledger_books_openai_and_drops_the_callers_gemini_line(self):
        import cost_tracker
        self._choose_openai()
        booked = []
        real = cost_tracker.record_usage

        def spy(sid, service, provider, model, **kw):
            booked.append((service, provider, model))
            return real(sid, service, provider, model, **kw)
        with mock.patch.object(cost_tracker, "record_usage", spy), \
             mock.patch.object(cost_tracker, "init_db", side_effect=AssertionError("gemini line was booked")):
            # What engine._record_text_usage does after a "Gemini" call:
            self.assertIsNone(real("s", "text", "gemini", "gemini-3.1-flash-lite", operation="ask",
                                   input_units=10, output_units=10, unit_type="tokens"))
        with mock.patch.object(cost_tracker, "record_usage", spy):
            provider_bridge.answer_gemini_request(GEMINI_TEXT, {"contents": [{"parts": [{"text": "hi"}]}]})
        self.assertIn(("text", "openai", provider_bridge.openai_models()["text"]), booked)


class CheckCase(unittest.TestCase):
    """check(): one real call proves the key; the result is kept, not the key."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        keys_store.set_store_path(Path(self.tmp.name) / "keys.env")
        provider_bridge._settings_cache = None
        self.env = mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-0000000000000000"}, clear=False)
        self.env.start()
        os.environ.pop("OPENAI_BASE_URL", None)

    def tearDown(self):
        self.env.stop()
        keys_store.set_store_path(None)
        provider_bridge._settings_cache = None
        self.tmp.cleanup()

    def test_models_are_picked_from_what_the_key_can_see(self):
        ids = ["gpt-4o-mini", "gpt-5-mini", "gpt-image-1", "gpt-image-2", "whisper-1"]
        listing = _FakeResp(200, {"data": [{"id": i} for i in ids]})
        with mock.patch("requests.get", return_value=listing), \
             mock.patch.object(provider_bridge, "_post_openai", FakeOpenAI()):
            result = provider_bridge.check("openai")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["text_model"], "gpt-5-mini")
        self.assertEqual(result["image_model"], "gpt-image-2")
        self.assertEqual(provider_bridge.openai_models()["transcribe"], "whisper-1")
        saved = (Path(self.tmp.name) / "account.json").read_text()
        self.assertIn('"ok": true', saved)
        self.assertNotIn("sk-test", saved)

    def test_a_bad_key_says_so_in_words(self):
        with mock.patch("requests.get", return_value=_FakeResp(401, {"error": {"message": "Incorrect API key"}})):
            result = provider_bridge.check("openai")
        self.assertFalse(result["ok"])
        self.assertIn("turned this key down", result["message"])


if __name__ == "__main__":
    unittest.main()
