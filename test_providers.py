"""
test_providers.py — offline unit tests for the ai_provider_manager.py
mock/offline backend extension (set_backend_override, active_backend,
MODEL_MAP, chat()/vision()/generate_image()).

These tests never touch the network: every case either explicitly forces
the "mock" backend, or only inspects pure functions (resolve_model,
active_backend with no override). Run with:

    python3 -m unittest test_providers -v
"""

import os
import unittest

import ai_provider_manager as apm


class TestBackendOverride(unittest.TestCase):
    def tearDown(self):
        apm.set_backend_override(None)
        os.environ.pop("STORYGEN_BACKEND", None)

    def test_no_override_returns_none(self):
        apm.set_backend_override(None)
        self.assertIsNone(apm.get_backend_override())

    def test_set_backend_override_mock(self):
        apm.set_backend_override("mock")
        self.assertEqual(apm.get_backend_override(), "mock")

    def test_set_backend_override_gemini(self):
        apm.set_backend_override("gemini")
        self.assertEqual(apm.get_backend_override(), "gemini")

    def test_clear_override(self):
        apm.set_backend_override("mock")
        apm.set_backend_override(None)
        self.assertIsNone(apm.get_backend_override())


class TestActiveBackend(unittest.TestCase):
    def tearDown(self):
        apm.set_backend_override(None)
        os.environ.pop("STORYGEN_BACKEND", None)

    def test_default_chat_backend_matches_config(self):
        apm.set_backend_override(None)
        os.environ.pop("STORYGEN_BACKEND", None)
        self.assertEqual(apm.active_backend("chat"), apm.get_text_provider())

    def test_default_image_backend_matches_config(self):
        apm.set_backend_override(None)
        os.environ.pop("STORYGEN_BACKEND", None)
        self.assertEqual(apm.active_backend("image"), apm.get_image_provider())

    def test_active_backend_respects_override(self):
        apm.set_backend_override("mock")
        self.assertEqual(apm.active_backend("chat"), "mock")
        self.assertEqual(apm.active_backend("vision"), "mock")
        self.assertEqual(apm.active_backend("image"), "mock")

    def test_override_takes_precedence_over_env(self):
        os.environ["STORYGEN_BACKEND"] = "openai"
        apm.set_backend_override("mock")
        self.assertEqual(apm.active_backend("chat"), "mock")

    def test_env_override_used_when_no_explicit_override(self):
        apm.set_backend_override(None)
        os.environ["STORYGEN_BACKEND"] = "openai"
        self.assertEqual(apm.active_backend("chat"), "openai")

    def test_is_mock_active_true_when_mock(self):
        apm.set_backend_override("mock")
        self.assertTrue(apm.is_mock_active("chat"))

    def test_is_mock_active_false_when_not_mock(self):
        apm.set_backend_override("gemini")
        self.assertFalse(apm.is_mock_active("chat"))


class TestModelMap(unittest.TestCase):
    def test_model_map_gpt4o(self):
        self.assertEqual(apm.resolve_model("gpt-4o"), "gemini-2.5-flash")

    def test_model_map_gpt4o_mini(self):
        self.assertEqual(apm.resolve_model("gpt-4o-mini"), "gemini-2.5-flash")

    def test_model_map_gpt_image_1(self):
        self.assertEqual(apm.resolve_model("gpt-image-1"), "gemini-2.5-flash-image")

    def test_unmapped_model_passes_through(self):
        self.assertEqual(apm.resolve_model("some-custom-model"), "some-custom-model")

    def test_no_model_falls_back_to_text_default(self):
        self.assertEqual(apm.resolve_model(None, "chat"), apm.get_text_model())

    def test_no_model_falls_back_to_image_default(self):
        self.assertEqual(apm.resolve_model(None, "image"), apm.get_image_model())


class TestMockChat(unittest.TestCase):
    def setUp(self):
        apm.set_backend_override("mock")

    def tearDown(self):
        apm.set_backend_override(None)

    def test_mock_chat_returns_string(self):
        result = apm.chat("Tell me a story")
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    def test_mock_chat_with_message_list(self):
        result = apm.chat([{"role": "user", "content": "Tell me a story"}])
        self.assertIsInstance(result, str)

    def test_mock_chat_choice_prompt_returns_choice_lines(self):
        result = apm.chat("Generate 3 choices for the player")
        lines = [l for l in result.splitlines() if l.strip()]
        self.assertGreaterEqual(len(lines), 2)

    def test_mock_chat_is_deterministic(self):
        a = apm.chat("Tell me a story")
        b = apm.chat("Tell me a story")
        self.assertEqual(a, b)

    def test_mock_chat_never_touches_network(self):
        # No GEMINI/OPENAI key set at all — if this tried real network calls
        # it would either raise or return a "Signal interrupted..." failure
        # string. Mock mode must short-circuit before any of that.
        old_gemini = os.environ.pop("GEMINI_API_KEY", None)
        old_openai = os.environ.pop("OPENAI_API_KEY", None)
        try:
            result = apm.chat("Tell me a story")
            self.assertNotIn("Signal interrupted", result)
        finally:
            if old_gemini is not None:
                os.environ["GEMINI_API_KEY"] = old_gemini
            if old_openai is not None:
                os.environ["OPENAI_API_KEY"] = old_openai


class TestMockVision(unittest.TestCase):
    def setUp(self):
        apm.set_backend_override("mock")

    def tearDown(self):
        apm.set_backend_override(None)

    def test_mock_vision_returns_string(self):
        result = apm.vision(prompt="Describe this scene")
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    def test_mock_vision_no_image_required(self):
        # Should not raise even with no image_path/image_data_b64 provided.
        result = apm.vision(image_path=None, image_data_b64=None, prompt="Describe")
        self.assertIsInstance(result, str)


class TestMockImage(unittest.TestCase):
    def setUp(self):
        apm.set_backend_override("mock")

    def tearDown(self):
        apm.set_backend_override(None)

    def test_mock_generate_image_returns_none(self):
        result = apm.generate_image("a dark hallway")
        self.assertIsNone(result)


class TestFlattenMessages(unittest.TestCase):
    def test_flatten_plain_string(self):
        self.assertEqual(apm._flatten_messages("hello"), "hello")

    def test_flatten_message_list(self):
        messages = [
            {"role": "system", "content": "You are a narrator."},
            {"role": "user", "content": "What happens next?"},
        ]
        flattened = apm._flatten_messages(messages)
        self.assertIn("narrator", flattened)
        self.assertIn("What happens next?", flattened)

    def test_flatten_multimodal_content_extracts_text(self):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "Describe this"}, {"type": "image_url", "image_url": "x"}]}
        ]
        flattened = apm._flatten_messages(messages)
        self.assertIn("Describe this", flattened)


if __name__ == "__main__":
    unittest.main()
