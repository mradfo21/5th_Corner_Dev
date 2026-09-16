"""
test_talk_voice.py — TALK must never leave the player on a dead channel.

What went wrong in production: ELEVENLABS_API_KEY held a value that is not an
ElevenLabs key, so every attempt to mint a signed conversation URL came back
`invalid_api_key_prefix: API key must start with 'sk_'`. Nothing surfaced that:

  * boot logged "key=YES", because the key was merely PRESENT
  * /api/talk/session still answered "mode": "voice" with a null signed_url
    and a voice_error, after which the browser skipped the public agent
    entirely and either hung on "establishing channel…" or dropped to text.
  * the browser then opened a PRIVATE agent it had no signature for, which
    resolves fine and simply never connects
  * the player sat on "establishing channel…" with a dead mic, forever

The agent WAS being initialised — that was never the problem. The problem was
that an unusable key was indistinguishable from a working one at every layer,
and then the client treated any signing failure as "do not even try the
public agent".

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
        self.assertIn("agent id", problem)
        self.assertIn("sk_", problem)

    def test_an_api_key_ID_is_named_as_such(self):
        """The second production value: the key's ID, copied from the dashboard
        list. ElevenLabs rejects it with `api_key_id_used_as_api_key`. Calling it
        merely "malformed" sends you hunting for a truncated paste, so the message
        has to name the actual mistake and where the real key comes from."""
        engine.ELEVENLABS_API_KEY = "629614d8f9b6fc247ae55f47ee5c635845bf7fec5addb595d150613de9c4f44d"
        problem = engine.elevenlabs_key_problem()
        self.assertIsNotNone(problem)
        self.assertIn("ID", problem)
        self.assertIn("sk_", problem)
        self.assertIn("rotate", problem)
        # A 32-char key id is the other shape the dashboard shows.
        engine.ELEVENLABS_API_KEY = "0123456789abcdef0123456789abcdef"
        self.assertIn("ID", engine.elevenlabs_key_problem())

    def test_a_hex_looking_value_that_is_a_real_key_is_still_accepted(self):
        """Only the 'sk_' prefix decides acceptance — never length or charset."""
        engine.ELEVENLABS_API_KEY = "sk_" + "0123456789abcdef" * 4
        self.assertIsNone(engine.elevenlabs_key_problem())

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

    def test_a_known_bad_key_does_not_round_trip_to_elevenlabs(self):
        """A dashboard key-ID or an agent id in the secret slot can never sign.
        Hitting ElevenLabs with it just delayed TALK; skip the round trip."""
        self.assertIn("skipping signed-url", self.session)
        self.assertIn("elevenlabs_key_problem()", self.session)
        skip_at = self.session.index("skipping signed-url")
        attempt_at = self.session.index("get-signed-url")
        self.assertLess(self.session.index("not key_problem"), attempt_at,
                        "signing must be gated on a key that looks usable")
        self.assertGreater(skip_at, attempt_at)

    def test_a_rejected_signing_request_is_reported_not_swallowed(self):
        self.assertIn("ElevenLabs rejected the signing request", self.session)


class TestClientNeverHangsOnADeadChannel(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")

    def test_an_unusable_key_does_not_hang_on_voice(self):
        """A dashboard key-ID cannot sign. Trying the private agent by id
        left 'opening channel…' for seconds. Skip voice unless we have a
        signed URL or a public agent with no key error."""
        start = self.src.split("async function start(subj)", 1)[1].split("\n    function ", 1)[0]
        self.assertIn("canVoice", start)
        self.assertIn("signed_url", start)
        self.assertIn("!session.voice_error", start)
        self.assertIn("voice needs an sk_ key", start)
        self.assertIn("beginVoice(session, opening)", start)

    def test_the_opening_line_appears_before_the_voice_socket(self):
        """A hung startSession used to leave the speak screen silent."""
        start = self.src.split("async function start(subj)", 1)[1].split("\n    function ", 1)[0]
        greet_at = start.index('addLine("assistant", firstLine)')
        voice_at = start.index("beginVoice(session, opening)")
        self.assertLess(greet_at, voice_at)
        self.assertLess(greet_at, start.index("postJSON(\"/api/talk/session\""))
        self.assertIn("fallbackOpening", start)
        self.assertIn("greetingShown = true", start)

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

    def test_the_timeout_is_armed_before_the_session_starts(self):
        """startSession() hangs on a private agent with no signed URL.
        Arming only after it resolved left 'opening channel…' forever."""
        begin = self.src.split("async function beginVoice(", 1)[1].split("\n    function ", 1)[0]
        start_at = begin.index("Conversation.startSession(opts)")
        arm_at = begin.index("connectTimer = setTimeout(")
        self.assertLess(arm_at, start_at)
        self.assertLess(arm_at, begin.index("ensureSdk()"))
        self.assertIn("failToText", begin)
        self.assertIn("fallbackOpening", begin)
        # mode flips to voice only once the socket is up, so typing works
        # while the channel is still opening.
        self.assertIn('mode = "voice"', begin)
        self.assertLess(begin.index("onConnect"), begin.index('mode = "voice"'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
