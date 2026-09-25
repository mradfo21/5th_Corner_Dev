"""A character's own voice: designed when they are made, heard at the reveal,
and the voice their runs are narrated in.

    python -m unittest test_character_voice -v

The narrator is "you, speaking into a tape" (narrator_direction), so a run
played as a character is narrated in the voice the character creator
designed for them. What must hold:

  * the brief that names them also says how they sound, in Google's shape
    for a designed voice (one sentence, archetype first, permanent traits)
  * the voice is designed from it, stored on the record with the key it was
    made on, and its line is spoken into voice.wav for the screens
  * a voice made on another key, or about to expire, is not used — it is
    designed again from the description, which is what survives
  * the narrator speaks in it: an editor's explicit pick first, then the
    run's character, then the roster
  * where no voice can be designed (mock mode, an OpenAI key) nothing is
    tried and nothing is shown

Offline: every provider call is mocked; the authoring sandbox points the
roster at an empty folder.
"""
import json
import os
import shutil
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ["SOMEWHERE_CHARACTERS_OFFLINE"] = "1"
import authoring_sandbox  # noqa: E402
authoring_sandbox.guard()
import characters  # noqa: E402
import voice_design  # noqa: E402

ROOT = Path(__file__).resolve().parent


def _fresh():
    d = Path(characters.CHARACTERS_DIR)
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)
    characters._CACHE.clear()
    characters._VOICE_TRIES.clear()


def _person(**voice):
    rec = characters._new_record("Mara Quill", "A salvage diver in her forties.", "created")
    rec.update(status=characters.STATUS_READY, pronouns="she/her", role="Salvage Diver")
    if voice:
        rec["voice"] = voice
    characters.save(rec)
    return rec["id"]


DESC = ("A dry-humoured salvage diver in her late forties with a low, slightly rasped alto, "
        "a coastal Maine accent, and clipped, unhurried pacing.")


class TheBriefSaysHowTheySound(unittest.TestCase):
    def test_the_brief_asks_for_a_voice_and_a_line(self):
        brief = characters._BRIEF.format(concept="x", with_pics="", voice_rule=characters.VOICE_RULE,
                                         line_rule=characters.LINE_RULE)
        self.assertIn('"voice":', brief)
        self.assertIn('"voice_line":', brief)
        # Google's shape: one sentence, archetype first; never a real person.
        self.assertIn("ONE sentence, archetype first", characters.VOICE_RULE)
        self.assertIn("never a celebrity or real person", characters.VOICE_RULE)
        self.assertIn("no emotion of the moment", characters.VOICE_RULE)

    def test_expand_keeps_the_voice(self):
        out = {"name": "Mara Quill", "pronouns": "she/her", "role": "Salvage Diver", "tagline": "Diver.",
               "demeanor": "dry", "who": "...", "held": "empty hands", "voice": DESC,
               "voice_line": "Nobody sends a diver down twice."}
        with mock.patch.object(characters, "have_key", return_value=True), \
                mock.patch.object(characters, "_text_json", return_value=out):
            got = characters._expand({"concept": "a diver"}, [])
        self.assertEqual(got["voice"], DESC)
        self.assertEqual(got["voice_line"], "Nobody sends a diver down twice.")

    def test_the_delivery_is_short_and_the_same_as_the_narrators(self):
        """Extra direction makes a designed voice drift (Google), and what the
        player hears at the reveal should be what their run sounds like."""
        reg = json.loads((ROOT / "voices.json").read_text(encoding="utf-8"))
        self.assertEqual(reg["cast"]["narrator"]["style"], characters.VOICE_LINE_STYLE)
        self.assertLess(len(characters.VOICE_LINE_STYLE), 80)


class AVoiceIsDesigned(unittest.TestCase):
    def setUp(self):
        _fresh()
        self.p = [mock.patch.object(voice_design, "is_available", return_value=True),
                  mock.patch.object(voice_design, "key_fingerprint", return_value="fp-this-key")]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def _design(self, cid, made_id="voice_new1", change="", words=None):
        made = {"id": made_id, "expire_time": "2099-01-01T00:00:00Z", "fingerprint": "fp-this-key"}
        wav = b"RIFF" + bytes(4000)
        with mock.patch.object(voice_design, "design_character_voice", return_value=made) as design, \
                mock.patch.object(voice_design, "delete_character_voice") as delete, \
                mock.patch("speech.synthesize", return_value=wav) as say, \
                mock.patch.object(characters, "_voice_words",
                                  return_value=words or {"description": DESC, "line": "Down twice."}):
            characters.design_voice(cid, change=change, wait=True)
        return design, delete, say

    def test_from_the_brief(self):
        cid = _person(description=DESC, line="Nobody sends a diver down twice.")
        design, _, say = self._design(cid)
        self.assertEqual(design.call_args[0][1], DESC)
        self.assertEqual(design.call_args[0][2], "female")
        self.assertEqual(say.call_args[0][1], "voice_new1")
        self.assertEqual(say.call_args[0][2], characters.VOICE_LINE_STYLE)
        rec = characters.load(cid)
        v = rec["voice"]
        self.assertEqual((v["status"], v["id"], v["fingerprint"]), ("ready", "voice_new1", "fp-this-key"))
        self.assertTrue((characters.char_dir(cid) / "voice.wav").is_file())
        card = characters.card(rec)["voice"]
        self.assertTrue(card["url"].startswith(f"/api/characters/{cid}/file/voice.wav"))

    def test_a_character_from_before_voices_gets_words_written(self):
        cid = _person()
        with mock.patch.object(voice_design, "design_character_voice",
                               return_value={"id": "voice_a", "fingerprint": "fp-this-key"}), \
                mock.patch("speech.synthesize", return_value=None), \
                mock.patch.object(characters, "_voice_words",
                                  return_value={"description": DESC, "line": "x"}) as words:
            characters.design_voice(cid, wait=True)
        words.assert_called_once()
        self.assertEqual(characters.load(cid)["voice"]["description"], DESC)

    def test_a_new_voice_frees_the_old_one(self):
        cid = _person(description=DESC, line="x", id="voice_old", status="ready", fingerprint="fp-this-key")
        _, delete, _ = self._design(cid, made_id="voice_new2", change="a deeper voice",
                                    words={"description": DESC.replace("alto", "contralto"), "line": "x"})
        delete.assert_called_once_with("voice_old")
        self.assertEqual(characters.load(cid)["voice"]["id"], "voice_new2")

    def test_a_failed_design_says_so_and_keeps_the_description(self):
        cid = _person(description=DESC, line="x")
        with mock.patch.object(voice_design, "design_character_voice", return_value=None):
            characters.design_voice(cid, wait=True)
        v = characters.load(cid)["voice"]
        self.assertEqual(v["status"], "failed")
        self.assertEqual(v["description"], DESC)


class TwoTakesAndTheCloserOneKept(unittest.TestCase):
    """Voice design has no seed; one description can come back as different
    people. Two are designed, a listener picks, the other is deleted — it
    would hold one of the project's 200 slots for a year otherwise."""

    def test_the_judges_pick_is_kept_and_the_other_deleted(self):
        takes = iter([{"id": "voice_t1", "sample_audio": {"data": "AAAA"}},
                      {"id": "voice_t2", "sample_audio": {"data": "BBBB"}}])
        with mock.patch.object(voice_design, "is_available", return_value=True),                 mock.patch.object(voice_design, "_post_create", side_effect=lambda b: next(takes)),                 mock.patch.object(voice_design, "_record_design_cost"),                 mock.patch.object(voice_design, "_judge_takes", return_value=(1, "the older one")),                 mock.patch.object(voice_design, "_delete_voice") as delete,                 mock.patch.object(voice_design, "key_fingerprint", return_value="fp"):
            got = voice_design.design_character_voice("Mara", DESC, "female", takes=2)
        self.assertEqual(got["id"], "voice_t2")
        self.assertEqual((got["takes"], got["picked_because"]), (2, "the older one"))
        delete.assert_called_once_with("voice_t1")

    def test_a_judge_that_fails_keeps_the_first(self):
        with mock.patch("requests.post", side_effect=RuntimeError("offline")):
            best, why = voice_design._judge_takes(DESC, [{"sample_audio": {"data": "A"}},
                                                        {"sample_audio": {"data": "B"}}])
        self.assertEqual(best, 0)

    def test_our_voices_are_never_swept(self):
        self.assertNotEqual(voice_design.CHARACTER_PREFIX, voice_design.NAME_PREFIX)
        self.assertFalse(voice_design.CHARACTER_PREFIX.startswith(voice_design.NAME_PREFIX))


class OnlyAVoiceOnThisKeyIsUsed(unittest.TestCase):
    def setUp(self):
        _fresh()

    def test_this_key_and_not_expiring(self):
        cid = _person(id="voice_a", status="ready", fingerprint="fp1", expires="2099-01-01T00:00:00Z")
        with mock.patch.object(voice_design, "key_fingerprint", return_value="fp1"):
            self.assertEqual(characters.voice_usable(characters.load(cid)), "voice_a")
        with mock.patch.object(voice_design, "key_fingerprint", return_value="fp-another-key"):
            self.assertEqual(characters.voice_usable(characters.load(cid)), "")

    def test_about_to_expire_is_not_used(self):
        soon = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(time.time() + 3600))
        cid = _person(id="voice_a", status="ready", fingerprint="fp1", expires=soon)
        with mock.patch.object(voice_design, "key_fingerprint", return_value="fp1"):
            self.assertEqual(characters.voice_usable(characters.load(cid)), "")

    def test_an_unusable_voice_is_designed_again_once_a_minute(self):
        cid = _person(description=DESC, id="voice_a", status="ready", fingerprint="fp-old")
        with mock.patch.object(voice_design, "key_fingerprint", return_value="fp-new"), \
                mock.patch.object(characters, "design_voice") as design:
            self.assertEqual(characters.ensure_voice(cid), "")
            self.assertEqual(characters.ensure_voice(cid), "")
        design.assert_called_once_with(cid)

    def test_nothing_is_tried_where_no_voice_can_be_designed(self):
        cid = _person(description=DESC)
        with mock.patch.object(voice_design, "is_available", return_value=False), \
                mock.patch.object(voice_design, "design_character_voice") as design:
            characters.design_voice(cid, wait=True)
        design.assert_not_called()
        self.assertFalse(characters.card(characters.load(cid))["voice"]["can"])


class TheNarratorSpeaksInIt(unittest.TestCase):
    def setUp(self):
        import engine
        self.engine = engine

    def test_the_run_character_before_the_roster(self):
        e = self.engine
        with mock.patch.object(e, "NARRATOR_VOICE_ID", ""), \
                mock.patch.object(e, "_run_character_voice", return_value="voice_mara"):
            self.assertEqual(e._narrator_voice_id(), "voice_mara")
            self.assertEqual(e.resolve_cast("narrator")["voice_id"], "voice_mara")
            self.assertEqual(e._valid_voice_id("voice_mara"), "voice_mara")
        with mock.patch.object(e, "NARRATOR_VOICE_ID", ""), \
                mock.patch.object(e, "_run_character_voice", return_value=""):
            self.assertEqual(e._narrator_voice_id(), "Charon")

    def test_an_editors_pick_wins(self):
        e = self.engine
        with mock.patch.object(e, "NARRATOR_VOICE_ID", "Algenib"), \
                mock.patch.object(e, "_run_character_voice", return_value="voice_mara"):
            self.assertEqual(e._narrator_voice_id(), "Algenib")

    def test_the_roster_default_is_not_mistaken_for_a_pick(self):
        """Seeded from voices.json, the roster's narrator looked like a choice
        and would have beaten every character's own voice."""
        src = (ROOT / "engine.py").read_text(encoding="utf-8")
        line = src.split("NARRATOR_VOICE_ID = ", 1)[1].split("\n", 1)[0]
        self.assertNotIn("VOICES_CONFIG", line)


class TheScreen(unittest.TestCase):
    def setUp(self):
        self.js = (ROOT / "static/js/characters.js").read_text(encoding="utf-8")

    def test_a_voice_line_under_the_style_line(self):
        self.assertIn('class="cs-voice-line"', self.js)
        self.assertLess(self.js.index('class="cs-style-line"'), self.js.index('class="cs-voice-line"'))

    def test_heard_once_by_itself_at_the_reveal(self):
        fn = self.js.split("function hearFresh()", 1)[1].split("\n  }", 1)[0]
        self.assertIn("c.id !== S.fresh", fn)
        self.assertIn("S.heard[c.id] === url", fn)

    def test_change_something_about_the_voice_changes_the_voice(self):
        sub = self.js.split("async function submitChange()", 1)[1].split("\n  }", 1)[0]
        self.assertIn("aboutTheVoice(change)", sub)
        self.assertIn("changeVoice(c, change)", sub)

    def test_the_file_door_lets_the_voice_out_and_nothing_else(self):
        _fresh()
        cid = _person()
        (characters.char_dir(cid) / "voice.wav").write_bytes(b"RIFF")
        (characters.char_dir(cid) / "character.json.bak").write_text("{}")
        self.assertIsNotNone(characters.file_path(cid, "voice.wav"))
        self.assertIsNone(characters.file_path(cid, "character.json.bak"))
        self.assertIsNone(characters.file_path(cid, "../voice.wav"))

    def test_the_route(self):
        src = (ROOT / "api.py").read_text(encoding="utf-8")
        self.assertIn("'/api/characters/<cid>/voice'", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
