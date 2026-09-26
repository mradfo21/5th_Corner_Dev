"""
test_speech.py — one spoken line, on either provider, never on a second account.

speech.py builds a Gemini TTS request; provider_bridge answers it with
OpenAI's /v1/audio/speech when the player chose OpenAI. The two agree on one
convention — the delivery rides in the prompt as "<style>:\\n<line>" — and
these tests hold both ends of it, plus the things a player would notice:
mock mode never calls out, a line is paid for once, and a streamed (headerless
PCM) answer still plays in a browser.

Run with:
    python -m unittest test_speech -v
"""

import base64
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")

import provider_bridge
import speech


def _resp(status=200, body=None, content=b""):
    r = mock.Mock()
    r.status_code = status
    r.ok = status == 200
    r.json.return_value = body or {}
    r.text = str(body or "")
    r.content = content
    return r


def _gemini_audio(raw: bytes, mime="audio/wav"):
    """A generateContent answer (what the bridge hands back on OpenAI)."""
    return {"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": mime, "data": base64.b64encode(raw).decode("ascii")}}]}}]}


def _interaction(raw: bytes, mime="audio/wav"):
    """An Interactions API answer (Gemini's own path)."""
    return {"status": "completed",
            "usage": {"total_input_tokens": 4, "total_output_tokens": 43},
            "steps": [{"type": "model_output", "content": [
                {"type": "audio", "mime_type": mime,
                 "data": base64.b64encode(raw).decode("ascii")}]}]}


def _wav(seconds: float) -> bytes:
    return b"RIFF" + bytes(1) * (40 + int(seconds * 48000))


class _TempCache(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._p = mock.patch.object(speech, "cache_dir", return_value=self.tmp)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestTheRequest(unittest.TestCase):
    def test_a_prebuilt_voice_and_a_designed_one(self):
        self.assertEqual(speech.voice_config("Charon"),
                         {"prebuiltVoiceConfig": {"voiceName": "Charon"}})
        self.assertEqual(speech.voice_config("voice_ibcqlcg3wndv"),
                         {"voice": "voice_ibcqlcg3wndv"})

    def test_it_asks_for_audio(self):
        body = speech.build_request("Hello.", "Kore", "quietly")
        self.assertEqual(body["generationConfig"]["responseModalities"], ["AUDIO"])

    def test_the_style_convention_round_trips(self):
        prompt = speech.build_prompt("The fence hums.", "low, close to the microphone")
        self.assertEqual(speech.split_prompt(prompt),
                         ("low, close to the microphone", "The fence hums."))

    def test_the_bridged_direction_is_under_a_heading_not_in_front_of_the_line(self):
        """`<style>:<newline><line>` got the style READ ALOUD (a live transcript
        began "Say this in character. An older voice for..."). The bridge's
        prompt keeps it headed so split_prompt can hand it to `instructions`."""
        prompt = speech.build_prompt("The fence hums.", "Say this in character, low")
        self.assertTrue(prompt.startswith("### DIRECTOR'S NOTES\n"))
        self.assertTrue(prompt.endswith("### TRANSCRIPT\nThe fence hums."))

    def test_a_line_with_no_style_is_left_alone(self):
        # A colon in the line itself must not be mistaken for a style.
        line = "He said: run.\nSo I ran."
        self.assertEqual(speech.split_prompt(speech.build_prompt(line)), ("", line))


class TestSynthesize(_TempCache):
    def setUp(self):
        super().setUp()
        self._g = mock.patch.object(provider_bridge, "active", return_value=False)
        self._g.start()
        self._k = mock.patch.object(provider_bridge, "gemini_key", return_value="AIza-test")
        self._k.start()

    def tearDown(self):
        self._g.stop()
        self._k.stop()
        super().tearDown()

    def test_mock_mode_never_calls_out(self):
        with mock.patch.object(speech, "can_speak", return_value=False),                 mock.patch("requests.post") as post:
            self.assertIsNone(speech.synthesize("Hello.", "Charon"))
        post.assert_not_called()

    def test_the_direction_rides_in_its_own_field_not_in_the_line(self):
        """In the prompt, the model read it aloud (see speech.py's docstring)."""
        wav = _wav(1)
        with mock.patch.object(speech, "can_speak", return_value=True),                 mock.patch("requests.post", return_value=_resp(body=_interaction(wav))) as post:
            speech.synthesize("Stay low.", "Kore", "whispered, frightened")
        url = post.call_args[0][0]
        sent = post.call_args[1]["json"]
        self.assertTrue(url.endswith("/interactions"))
        part = sent["input"][0]["content"][0]
        self.assertEqual(part["text"], "Stay low.")
        self.assertEqual(part["annotations"], [{"type": "speech_metadata",
                                                "style": "whispered, frightened"}])
        self.assertEqual(sent["generation_config"]["speech_config"], [{"voice": "Kore"}])

    def test_a_line_is_paid_for_once(self):
        wav = _wav(1)
        with mock.patch.object(speech, "can_speak", return_value=True),                 mock.patch("requests.post", return_value=_resp(body=_interaction(wav))) as post:
            a = speech.synthesize("Hello.", "Charon", "quietly")
            b = speech.synthesize("Hello.", "Charon", "quietly")
        self.assertEqual(a, wav)
        self.assertEqual(b, wav)
        self.assertEqual(post.call_count, 1)

    def test_a_different_voice_is_a_different_line(self):
        with mock.patch.object(speech, "can_speak", return_value=True),                 mock.patch("requests.post", return_value=_resp(body=_interaction(_wav(1)))) as post:
            speech.synthesize("Hello.", "Charon")
            speech.synthesize("Hello.", "Kore")
        self.assertEqual(post.call_count, 2)

    def test_headerless_pcm_is_wrapped_so_a_browser_plays_it(self):
        pcm = bytes([1, 0]) * 2400
        with mock.patch.object(speech, "can_speak", return_value=True),                 mock.patch("requests.post",
                           return_value=_resp(body=_interaction(pcm, "audio/L16;codec=pcm;rate=24000"))):
            out = speech.synthesize("Hello.", "Charon")
        self.assertEqual(out[:4], b"RIFF")
        self.assertIn(pcm, out)

    def test_a_line_that_recited_its_direction_is_said_again_plain(self):
        """Two words cannot take thirty seconds unless something else was said."""
        long_, short = _wav(30), _wav(1.5)
        answers = [_resp(body=_interaction(long_)), _resp(body=_interaction(short))]
        with mock.patch.object(speech, "can_speak", return_value=True),                 mock.patch("requests.post", side_effect=answers) as post:
            out = speech.synthesize("Stay low.", "Kore", "Read this as a frightened soldier")
        self.assertEqual(out, short)
        retry = post.call_args_list[1][1]["json"]["input"][0]["content"][0]
        self.assertNotIn("annotations", retry)

    def test_openai_takes_the_bridged_path(self):
        wav = _wav(1)
        with mock.patch.object(provider_bridge, "active", return_value=True),                 mock.patch.object(speech, "can_speak", return_value=True),                 mock.patch("requests.post", return_value=_resp(body=_gemini_audio(wav))) as post:
            out = speech.synthesize("Hello.", "Charon", "quietly")
        self.assertEqual(out, wav)
        self.assertIn(":generateContent", post.call_args[0][0])
        self.assertEqual(speech.split_prompt(
            post.call_args[1]["json"]["contents"][0]["parts"][0]["text"]), ("quietly", "Hello."))

    def test_a_busy_model_is_asked_once_more(self):
        """503 "high demand", measured on launch week. Asked twice, not forever."""
        wav = _wav(1)
        answers = [_resp(503, {"error": "high demand"}), _resp(body=_interaction(wav))]
        with mock.patch.object(speech, "can_speak", return_value=True),                 mock.patch.object(speech, "_RETRY_AFTER_S", 0),                 mock.patch("requests.post", side_effect=answers) as post:
            self.assertEqual(speech.synthesize("Hello.", "Charon"), wav)
        self.assertEqual(post.call_count, 2)

    def test_a_refusal_is_none_not_an_exception(self):
        with mock.patch.object(speech, "can_speak", return_value=True),                 mock.patch("requests.post", return_value=_resp(429, {"error": "quota"})):
            self.assertIsNone(speech.synthesize("Hello.", "Charon"))


class TestOpenAIAnswersTheSameRequest(unittest.TestCase):
    def _answer(self, voice, style="dry, slow"):
        payload = speech.build_request("The fence hums.", voice, style)
        wav = b"RIFF" + b"\x00" * 48044
        with mock.patch.object(provider_bridge, "openai_key", return_value="sk-test"), \
                mock.patch.object(provider_bridge, "_post_openai",
                                  return_value=_resp(content=wav)) as post:
            status, body = provider_bridge.answer_gemini_request(
                provider_bridge.GEMINI_API + "/models/gemini-3.8-flash-tts:generateContent",
                payload, 30)
        return status, body, post

    def test_it_goes_to_audio_speech_with_the_style_as_instructions(self):
        status, body, post = self._answer("Charon")
        self.assertEqual(status, 200)
        path = post.call_args[0][1]
        sent = post.call_args[1]["json_body"]
        self.assertEqual(path, "/audio/speech")
        self.assertEqual(sent["input"], "The fence hums.")
        self.assertEqual(sent["instructions"], "dry, slow")
        self.assertEqual(sent["voice"], "onyx")
        part = body["candidates"][0]["content"]["parts"][0]["inlineData"]
        self.assertEqual(part["mimeType"], "audio/wav")

    def test_every_gemini_voice_has_an_openai_one(self):
        valid = {"alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx",
                 "sage", "shimmer", "verse", "marin", "cedar"}
        self.assertEqual(len(provider_bridge.OPENAI_VOICE_FOR), 30)
        self.assertTrue(set(provider_bridge.OPENAI_VOICE_FOR.values()) <= valid)

    def test_a_designed_voice_still_speaks(self):
        status, _, post = self._answer("voice_ibcqlcg3wndv")
        self.assertEqual(status, 200)
        self.assertEqual(post.call_args[1]["json_body"]["voice"],
                         provider_bridge.OPENAI_DEFAULT_VOICE)

    def test_dictation_is_still_dictation(self):
        """A transcription request carries audio IN and asks for text; it must
        not be mistaken for a line to speak."""
        payload = {"contents": [{"parts": [
            {"text": "Transcribe."},
            {"inlineData": {"mimeType": "audio/webm", "data": "AAAA"}}]}]}
        self.assertFalse(provider_bridge._is_speech_request(payload))
        self.assertTrue(provider_bridge._is_audio_request(payload))

    def test_every_roster_voice_is_a_gemini_voice(self):
        import json
        reg = json.loads((Path(__file__).parent / "voices.json").read_text(encoding="utf-8"))
        ids = {v["id"] for v in reg["voices"]}
        ids |= {c["voice_id"] for c in reg["cast"].values()}
        ids |= set(reg["by_kind"].values()) | {reg["default_voice"], reg["narrator_voice"]}
        self.assertTrue(ids <= set(provider_bridge.OPENAI_VOICE_FOR), ids - set(provider_bridge.OPENAI_VOICE_FOR))


if __name__ == "__main__":
    unittest.main(verbosity=2)
