"""
test_talk_voice.py — TALK must never leave the player on a dead channel.

What went wrong in production: ELEVENLABS_API_KEY held a value that is not an
ElevenLabs key, so every attempt to mint a signed conversation URL came back
`invalid_api_key_prefix: API key must start with 'sk_'`. Nothing surfaced that:

  * boot logged "key=YES", because the key was merely PRESENT
  * /api/talk/session still answered "mode": "voice" with a null signed_url
  * the browser then opened a PRIVATE agent it had no signature for, which
    resolves fine and simply never connects
  * the player sat on "establishing channel…" with a dead mic, forever

The agent WAS being initialised — that was never the problem. The problem was
that an unusable key was indistinguishable from a working one at every layer.

Run with:
    python3 -m unittest test_talk_voice -v
"""

import os
import unittest
from pathlib import Path

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("ANTHROPIC_API_KEY", "")

import engine

ROOT = Path(__file__).parent.resolve()


class TestElevenLabsKeyValidation(unittest.TestCase):
    def setUp(self):
        self._real = engine.ELEVENLABS_API_KEY

    def tearDown(self):
        engine.ELEVENLABS_API_KEY = self._real

    def test_missing_key_is_reported(self):
        engine.ELEVENLABS_API_KEY = ""
        self.assertEqual(engine.elevenlabs_key_problem(), "not set")

    def test_a_valid_looking_key_is_accepted(self):
        engine.ELEVENLABS_API_KEY = "sk_" + "a" * 40
        self.assertIsNone(engine.elevenlabs_key_problem())

    def test_an_agent_id_pasted_into_the_key_is_caught(self):
        """The actual production value: an agent id in the API-key slot."""
        engine.ELEVENLABS_API_KEY = "agent_1601kxh3rz2hej9swfs75dv33q78"
        problem = engine.elevenlabs_key_problem()
        self.assertIsNotNone(problem)
        self.assertIn("sk_", problem)

    def test_the_problem_says_what_to_do_about_it(self):
        engine.ELEVENLABS_API_KEY = "nope"
        problem = engine.elevenlabs_key_problem()
        # Naming the expected prefix is the whole point — "invalid key" alone
        # doesn't tell anyone they pasted the wrong field.
        self.assertIn("sk_", problem)


class TestTalkSessionReportsWhyVoiceFailed(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / "engine.py").read_text(encoding="utf-8")
        self.session = self.src.split("def api_talk_session(", 1)[1].split("\ndef ", 1)[0]

    def test_response_carries_a_voice_error_field(self):
        self.assertIn('"voice_error": voice_error', self.session)

    def test_a_rejected_signing_request_is_reported_not_swallowed(self):
        self.assertIn("ElevenLabs rejected the signing request", self.session)

    def test_the_format_check_explains_failures_but_never_pre_empts_them(self):
        """The signing exchange is always attempted. The format check only
        annotates a failure, so a key shape we don't recognise but ElevenLabs
        accepts keeps working — being wrong about the format must not be able
        to take voice down."""
        self.assertIn("_ELEVENLABS_KEY_PROBLEM", self.session)
        self.assertNotIn("skipping signed-url", self.session)
        attempt_at = self.session.index("get-signed-url")
        annotate_at = self.session.index("_ELEVENLABS_KEY_PROBLEM")
        self.assertLess(attempt_at, annotate_at,
                        "the key-format hint must only be applied after a real failure")


class TestClientNeverHangsOnADeadChannel(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")

    def test_known_voice_failure_goes_straight_to_text(self):
        self.assertIn("session.voice_error", self.src)
        self.assertIn("voice unavailable", self.src)

    def test_a_channel_that_never_connects_falls_back(self):
        """startSession() can resolve and then never connect — an unauthorised
        private agent does exactly that."""
        self.assertIn("TALK_CONNECT_TIMEOUT_MS", self.src)
        begin = self.src.split("async function beginVoice(", 1)[1].split("\n    function ", 1)[0]
        self.assertIn("connectTimer", begin)
        self.assertIn("voice didn't connect", begin)

    def test_the_timeout_is_cancelled_once_connected(self):
        begin = self.src.split("async function beginVoice(", 1)[1].split("\n    function ", 1)[0]
        self.assertIn("connected = true", begin)
        self.assertIn("clearConnectTimer()", begin)

    def test_the_timeout_is_armed_only_after_the_session_starts(self):
        """Arming it earlier would count a slow SDK handshake against the
        connect budget and abandon a channel that was about to work."""
        begin = self.src.split("async function beginVoice(", 1)[1].split("\n    function ", 1)[0]
        start_at = begin.index("Conversation.startSession(opts)")
        arm_at = begin.index("connectTimer = setTimeout(")
        self.assertLess(start_at, arm_at)


if __name__ == "__main__":
    unittest.main(verbosity=2)
