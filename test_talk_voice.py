"""
test_talk_voice.py — TALK and the narrator speak on the player's one key.

Until 2026-09-25 both were ElevenLabs Convai agents opened from the page, and
the engine shipped a PUBLIC default agent id so voice "worked out of the box
with no secret at all". It worked on whoever owned the agent: every keyless
install would have talked, and been billed, on one account. The suite that
used to live here checked that a bad ElevenLabs key was reported instead of
hanging "establishing channel…" — a whole class of failure that is gone with
the websocket.

What must hold now:
  * nothing in the game can reach api.elevenlabs.io, and no agent id ships
  * a conversation is "voice" only when this key can actually speak
    (speech.can_speak); otherwise it is text, not a dead channel
  * a line's caption waits for its voice, and the greeting is on screen
    before anything is spoken
  * a voice id from the ElevenLabs era (a saved companion, localStorage's
    talk_voice_id) names nothing now and falls back to the roster

Run with:
    python -m unittest test_talk_voice -v
"""

import os
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("ANTHROPIC_API_KEY", "")

import engine

ROOT = Path(__file__).parent.resolve()
RUNTIME = ("engine.py", "api.py", "speech.py", "voice_design.py", "scene_audio.py",
           "provider_bridge.py", "static/js/standalone.js")


class TestNothingReachesElevenLabs(unittest.TestCase):
    def test_no_runtime_file_calls_elevenlabs(self):
        for rel in RUNTIME:
            src = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("api.elevenlabs.io", src, rel)
            self.assertNotIn("xi-api-key", src, rel)
            self.assertNotIn("@elevenlabs/client", src, rel)

    def test_no_default_agent_ships(self):
        """The public agent id is what every keyless install talked on."""
        for rel in RUNTIME:
            src = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("agent_1601kxh3rz2hej9swfs75dv33q78", src, rel)
        self.assertFalse(hasattr(engine, "ELEVENLABS_AGENT_ID"))

    def test_the_talk_session_hands_out_no_agent(self):
        fn = (ROOT / "engine.py").read_text(encoding="utf-8").split(
            "def api_talk_session():", 1)[1].split("\ndef ", 1)[0]
        for field in ('"agent_id"', '"signed_url"', '"overrides"'):
            self.assertNotIn(field, fn)
        self.assertIn('"voice" if _speech_available() else "text"', fn)


class TestVoiceModeMeansTheKeyCanSpeak(unittest.TestCase):
    def test_mock_mode_is_text(self):
        with mock.patch("provider_bridge._mock_forced", return_value=True):
            self.assertFalse(engine._speech_available())

    def test_a_gemini_key_speaks(self):
        with mock.patch("provider_bridge._mock_forced", return_value=False), \
                mock.patch("provider_bridge.active", return_value=False), \
                mock.patch("provider_bridge.can_call_gemini_api", return_value=True):
            self.assertTrue(engine._speech_available())

    def test_no_key_is_text(self):
        with mock.patch("provider_bridge._mock_forced", return_value=False), \
                mock.patch("provider_bridge.active", return_value=False), \
                mock.patch("provider_bridge.can_call_gemini_api", return_value=False):
            self.assertFalse(engine._speech_available())


class TestVoiceIds(unittest.TestCase):
    def test_an_elevenlabs_era_id_names_nothing(self):
        # "Eric", the old default, as saved on companions and in localStorage.
        self.assertEqual(engine._valid_voice_id("cjVigY5qzO86Huf0OWal"), "")

    def test_every_gemini_voice_is_accepted(self):
        for name in ("Charon", "Kore", "Algenib", "Sulafat", "Pulcherrima"):
            self.assertEqual(engine._valid_voice_id(name), name)

    def test_a_subject_always_gets_a_voice(self):
        for subj in ({"label": "old woman", "kind": "person"},
                     {"label": "intercom", "kind": "machine"},
                     {"label": "thing in the dark", "kind": "creature"},
                     {}):
            vid = engine.resolve_fallback_voice_for_subject(subj)
            self.assertTrue(engine._valid_voice_id(vid), (subj, vid))

    def test_the_narrator_pace_is_said_in_words(self):
        """ElevenLabs took `speed` as a number; a TTS model directed in words
        needs it in the direction, or the editor's pace knob does nothing."""
        with mock.patch.object(engine, "NARRATOR_SPEED", 0.8):
            cast = engine.resolve_cast("narrator")
        self.assertIn("slowly", cast.get("style", ""))
        self.assertTrue(engine._valid_voice_id(cast["voice_id"]))


class TestTheClientSpeaksLinesNotSessions(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")

    def test_one_path_for_every_spoken_line(self):
        self.assertIn("const VoiceOut = (function () {", self.src)
        vo = self.src.split("const VoiceOut = (function () {", 1)[1].split("})();", 1)[0]
        self.assertIn('"/api/narrator/say"', vo)
        self.assertIn('"ended"', vo)
        self.assertNotIn("ElevenSDK", self.src)

    def test_the_caption_waits_for_its_voice(self):
        """Read the line, then hear it, was the bug with agent sessions."""
        nar = self.src.split("function speakSegment(seg, myGen, pending)", 1)[1].split(
            "\n    async function play(", 1)[0]
        self.assertIn("onStart: reveal", nar)

    def test_the_next_line_is_fetched_while_this_one_plays(self):
        play = self.src.split("async function play(segments, myGen)", 1)[1].split(
            "\n    async function narrate(", 1)[0]
        self.assertIn("next = voiced && lines[i + 1] ? VoiceOut.fetchLine(lines[i + 1])", play)

    def test_the_greeting_is_on_screen_before_it_is_spoken(self):
        start = self.src.split("async function start(subj)", 1)[1].split("\n    function ", 1)[0]
        session_at = start.index('postJSON("/api/talk/session"')
        greet_at = start.index('addLine("assistant", opening)')
        speak_at = start.index("speakLine(opening)")
        self.assertLess(session_at, greet_at)
        self.assertLess(greet_at, speak_at)
        self.assertIn("fallbackOpening", start)
        self.assertNotIn("opening_line: firstLine", start)

    def test_every_answer_is_spoken_in_voice_mode(self):
        send = self.src.split("async function send(text)", 1)[1].split("\n    function ", 1)[0]
        self.assertIn('postJSON("/api/talk/message"', send)
        self.assertIn("speakLine(reply)", send)

    def test_a_conversation_counts_only_if_the_player_said_something(self):
        fn = self.src.split("function worthATurn()", 1)[1].split("\n    }", 1)[0]
        self.assertIn('ln.role === "user"', fn)
        self.assertNotIn("seconds", fn)


class TestTalkOpeningIsNotAStockNpc(unittest.TestCase):
    def test_fallback_is_not_the_sentry_line(self):
        line = engine._talk_opening_fallback("guard", "person")
        self.assertNotIn("shouldn't be here", line.lower())
        self.assertNotIn("what do you want", line.lower())
        self.assertNotIn("who are you", line.lower())

    def test_the_old_canned_opener_is_detected(self):
        self.assertTrue(engine._is_canned_talk_fallback(
            "You. You shouldn't be here. What do you want?"))
        self.assertTrue(engine._is_canned_talk_fallback(
            "[the radio crackles]… is someone there? Say something."))
        self.assertFalse(engine._is_canned_talk_fallback("Don't. Not yet."))

    def test_the_client_fallback_is_not_the_sentry_line(self):
        src = (ROOT / "static/js/standalone.js").read_text(encoding="utf-8")
        fn = src.split("function fallbackOpening(subj)", 1)[1].split(
            "\n    function ", 1)[0]
        self.assertNotIn("You shouldn't be here", fn)
        self.assertNotIn("is someone there", fn)


if __name__ == "__main__":
    unittest.main(verbosity=2)
