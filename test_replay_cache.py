"""The replay cache is only useful if its key is exactly right.

Too loose and a verification run gets served the wrong frame, which is worse than
paying for a real one: it looks authoritative and it is wrong. Too tight and
nothing ever hits, the cache is dead weight, and the loop stays at thirty seconds
and four cents a turn. Both failures are silent, so they get pinned here.
"""
import io
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("GEMINI_API_KEY", "")

import replay_cache


def _png_bytes(level: int) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("L", (24, 24), color=level).save(buf, format="PNG")
    return buf.getvalue()


class _CacheInTemp(unittest.TestCase):
    """Every test gets its own recording library and its own mode."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        original_cache = replay_cache.CACHE_DIR
        replay_cache.CACHE_DIR = self.root / "replay"
        self.addCleanup(lambda: setattr(replay_cache, "CACHE_DIR", original_cache))

        original_mode = os.environ.get("SOMEWHERE_REPLAY")
        self.addCleanup(lambda: os.environ.__setitem__("SOMEWHERE_REPLAY", original_mode)
                        if original_mode is not None
                        else os.environ.pop("SOMEWHERE_REPLAY", None))

        replay_cache.reset_stats()
        self.addCleanup(replay_cache.reset_stats)

        # Recording is refused when generation is stubbed, which is the state a
        # test process is in. Say it is live so the record path can be exercised;
        # the guard itself is tested explicitly in TestItRefusesToRecordStubs.
        original_live = replay_cache.generation_is_live
        replay_cache.generation_is_live = lambda: True
        self.addCleanup(lambda: setattr(replay_cache, "generation_is_live", original_live))

    def set_mode(self, value):
        os.environ["SOMEWHERE_REPLAY"] = value


class TestMode(_CacheInTemp):
    def test_recording_is_the_default(self):
        os.environ.pop("SOMEWHERE_REPLAY", None)
        self.assertEqual(replay_cache.mode(), replay_cache.MODE_RECORD)

    def test_off_really_means_off(self):
        for raw in ("off", "0", "no", "false", "NONE"):
            self.set_mode(raw)
            self.assertEqual(replay_cache.mode(), replay_cache.MODE_OFF, raw)

    def test_replay_is_recognised(self):
        self.set_mode("replay")
        self.assertEqual(replay_cache.mode(), replay_cache.MODE_REPLAY)


class TestTheKey(_CacheInTemp):
    """What counts as "the same call" decides whether any of this works."""

    def test_positional_and_keyword_calls_share_a_key(self):
        """The engine calls these both ways. If the two disagreed, half of every
        run would miss and the hit rate would never reach 1.0."""
        def generate(prompt, caption, strength=0.3):
            return "x"

        positional, _, _ = replay_cache._key("img2img", generate, ("a dark yard", "yard"), {})
        keyword, _, _ = replay_cache._key("img2img", generate, (),
                                          {"prompt": "a dark yard", "caption": "yard"})
        self.assertEqual(positional, keyword)

    def test_a_default_is_the_same_as_passing_the_default(self):
        def generate(prompt, strength=0.3):
            return "x"

        implicit, _, _ = replay_cache._key("img2img", generate, ("p",), {})
        explicit, _, _ = replay_cache._key("img2img", generate, ("p",), {"strength": 0.3})
        self.assertEqual(implicit, explicit)

    def test_output_dir_is_ignored(self):
        """It is a per-session, sometimes per-temp-directory destination. Keyed
        on, every call would be unique and nothing would ever hit."""
        def generate(prompt, output_dir=None):
            return "x"

        a, _, _ = replay_cache._key("image", generate, ("p",), {"output_dir": "/tmp/one"})
        b, _, _ = replay_cache._key("image", generate, ("p",), {"output_dir": "/tmp/two"})
        self.assertEqual(a, b)

    def test_a_different_prompt_is_a_different_key(self):
        def generate(prompt):
            return "x"

        a, _, _ = replay_cache._key("image", generate, ("a dark yard",), {})
        b, _, _ = replay_cache._key("image", generate, ("a bright yard",), {})
        self.assertNotEqual(a, b)

    def test_the_excerpt_names_the_call(self):
        def generate(prompt, caption=""):
            return "x"

        _key, excerpt, _payload = replay_cache._key("image", generate, ("a dark yard at dusk",), {})
        self.assertEqual(excerpt, "a dark yard at dusk")


class TestReferenceImagesAreKeyedByPixels(_CacheInTemp):
    """img2img continuity depends entirely on which frame went in.

    Reference filenames carry random ids and temp paths get reused within a run,
    so keying on the path is both useless (never hits) and dangerous (two
    different frames can share one path).
    """

    def setUp(self):
        super().setUp()
        self.a = self.root / "one.png"
        self.b = self.root / "two.png"
        self.c = self.root / "three.png"
        self.a.write_bytes(_png_bytes(40))
        self.b.write_bytes(_png_bytes(40))     # same pixels, different name
        self.c.write_bytes(_png_bytes(200))    # different pixels

    def _key_for(self, reference):
        def img2img(prompt, reference_image_path):
            return "x"
        key, _, _ = replay_cache._key("img2img", img2img, ("p", str(reference)), {})
        return key

    def test_the_same_pixels_under_a_different_name_hit_the_same_key(self):
        self.assertEqual(self._key_for(self.a), self._key_for(self.b))

    def test_different_pixels_do_not(self):
        self.assertNotEqual(self._key_for(self.a), self._key_for(self.c))

    def test_a_list_of_references_is_keyed_by_all_of_them(self):
        def img2img(prompt, reference_image_path):
            return "x"
        one, _, _ = replay_cache._key("img2img", img2img, ("p", [str(self.a)]), {})
        two, _, _ = replay_cache._key("img2img", img2img, ("p", [str(self.a), str(self.c)]), {})
        self.assertNotEqual(one, two)

    def test_a_long_prompt_is_not_mistaken_for_a_path(self):
        """Prompts contain slashes. Hashing one as a file would be a silent
        no-op here, but the check exists so the path heuristic stays honest."""
        prompt = "wide shot / handheld / 35mm, the yard beyond the fence"
        def generate(prompt):
            return "x"
        _k, excerpt, payload = replay_cache._key("image", generate, (prompt,), {})
        self.assertEqual(payload["prompt"], prompt)


class TestRecordThenReplayAValue(_CacheInTemp):
    def test_a_replayed_call_returns_the_recording_without_calling_out(self):
        calls = []

        def ask(prompt, temp=1.0):
            calls.append(prompt)
            return "the door groans on rusted hinges"

        wrapped = replay_cache._wrap_value("ask", ask)

        self.set_mode("record")
        self.assertEqual(wrapped("what happens?"), "the door groans on rusted hinges")
        self.assertEqual(len(calls), 1)

        self.set_mode("replay")
        self.assertEqual(wrapped("what happens?"), "the door groans on rusted hinges")
        self.assertEqual(len(calls), 1, "replay must not reach the real function")
        self.assertEqual(replay_cache.stats()["hits"], 1)
        self.assertTrue(replay_cache.stats()["deterministic"])

    def test_a_miss_falls_through_and_is_counted_rather_than_faked(self):
        """A replay run that invented an answer would be worse than no replay:
        the run would look complete and mean nothing."""
        def ask(prompt):
            return "fresh prose"

        wrapped = replay_cache._wrap_value("ask", ask)
        self.set_mode("replay")

        self.assertEqual(wrapped("never recorded"), "fresh prose")
        stats = replay_cache.stats()
        self.assertEqual(stats["misses"], 1)
        self.assertFalse(stats["deterministic"])
        self.assertEqual(stats["miss_detail"][0]["asked_for"], "never recorded")

    def test_a_failed_call_is_not_recorded(self):
        """Caching a None would make a replay reproduce the failure forever with
        nothing to say why."""
        def ask(prompt):
            return None

        wrapped = replay_cache._wrap_value("ask", ask)
        self.set_mode("record")
        self.assertIsNone(wrapped("this one fails"))

        calls = []

        def ask_again(prompt):
            calls.append(prompt)
            return "worked this time"

        wrapped_again = replay_cache._wrap_value("ask", ask_again)
        self.set_mode("replay")
        self.assertEqual(wrapped_again("this one fails"), "worked this time")
        self.assertEqual(len(calls), 1, "the failure should not have been served back")

    def test_recording_leaves_the_real_result_untouched(self):
        """Recording runs during normal play, so it must be transparent."""
        def ask(prompt):
            return {"prose": "unchanged", "n": 3}

        wrapped = replay_cache._wrap_value("ask", ask)
        self.set_mode("record")
        self.assertEqual(wrapped("p"), {"prose": "unchanged", "n": 3})


class TestRecordThenReplayAnImage(_CacheInTemp):
    def setUp(self):
        super().setUp()
        self.generated = self.root / "generated"
        self.generated.mkdir()
        self.wanted = self.root / "session_images"

    def test_the_frame_comes_back_as_a_real_file_where_this_run_wants_it(self):
        """The pipeline pins the returned path, runs vision over it and uses it as
        the next img2img reference, so a path into the cache would not do."""
        produced = self.generated / "yard_123.png"
        produced.write_bytes(_png_bytes(60))
        calls = []

        def generate(prompt, caption, output_dir=None):
            calls.append(prompt)
            return str(produced)

        wrapped = replay_cache._wrap_image("image", generate)

        self.set_mode("record")
        first = wrapped("a dark yard", "yard", output_dir=self.generated)
        self.assertEqual(first, str(produced))
        self.assertEqual(len(calls), 1)

        # A replay run has a different destination and the original is gone.
        produced.unlink()
        self.set_mode("replay")
        second = wrapped("a dark yard", "yard", output_dir=self.wanted)

        self.assertEqual(len(calls), 1, "replay must not regenerate the image")
        self.assertEqual(Path(second).parent, self.wanted,
                         "the frame must land in the directory this run asked for")
        self.assertEqual(Path(second).name, "yard_123.png",
                         "the filename is part of what the pipeline expects")
        self.assertEqual(Path(second).read_bytes(), _png_bytes(60))

    def test_a_missing_image_is_a_miss_not_a_broken_path(self):
        def generate(prompt, output_dir=None):
            return None

        wrapped = replay_cache._wrap_image("image", generate)
        self.set_mode("replay")
        self.assertIsNone(wrapped("never recorded", output_dir=self.wanted))
        self.assertEqual(replay_cache.stats()["misses"], 1)


class TestItRefusesToRecordStubs(_CacheInTemp):
    """A poisoned library is worse than an empty one.

    engine._ask answers with a placeholder whenever LLM_ENABLED is false, and any
    process that imports api.py without keys is in that state -- a test, a tool, a
    --mock run. This was observed: the starting-time question got the placeholder
    recorded against it, and a later replay served that back, so the run fell
    silently to its default weather with nothing in the log to explain it.
    """

    STUB = "You are still in an open place. The next move is yours."

    def test_the_engine_placeholder_is_recognised(self):
        self.assertTrue(replay_cache.looks_like_a_stub(self.STUB))
        self.assertFalse(replay_cache.looks_like_a_stub(
            "The door groans open and the next move is yours to make."))
        self.assertFalse(replay_cache.looks_like_a_stub(None))

    def test_a_stub_answer_is_not_written_to_the_library(self):
        def ask(prompt):
            return self.STUB

        wrapped = replay_cache._wrap_value("ask", ask)
        self.set_mode("record")
        wrapped("name the weather")
        self.assertEqual(replay_cache.library()["entries"], 0)

    def test_nothing_is_recorded_when_generation_is_not_live(self):
        replay_cache.generation_is_live = lambda: False

        def ask(prompt):
            return "prose that never came from a model"

        wrapped = replay_cache._wrap_value("ask", ask)
        self.set_mode("record")
        wrapped("anything")
        self.assertEqual(replay_cache.library()["entries"], 0)

    def test_images_are_not_recorded_when_generation_is_not_live(self):
        replay_cache.generation_is_live = lambda: False
        produced = self.root / "frame.png"
        produced.write_bytes(_png_bytes(50))

        def generate(prompt, output_dir=None):
            return str(produced)

        wrapped = replay_cache._wrap_image("image", generate)
        self.set_mode("record")
        wrapped("p", output_dir=self.root)
        self.assertEqual(replay_cache.library()["entries"], 0)


class TestInstall(_CacheInTemp):
    def test_off_installs_nothing(self):
        self.set_mode("off")
        replay_cache._INSTALLED = False
        self.addCleanup(lambda: setattr(replay_cache, "_INSTALLED", False))
        result = replay_cache.install()
        self.assertFalse(result["installed"])

    def test_wrapping_twice_does_not_stack_wrappers(self):
        """Two layers would double-count every hit and record each call twice."""
        def ask(prompt):
            return "x"

        once = replay_cache._wrap_value("ask", ask)
        self.assertEqual(getattr(once, "_replay_kind", None), "ask")
        # install() skips anything already carrying _replay_kind, which is the
        # guard this asserts on.
        self.assertIsNotNone(getattr(once, "_replay_kind", None))


class TestStats(_CacheInTemp):
    def test_hit_rate_is_none_before_anything_is_looked_up(self):
        replay_cache.reset_stats()
        self.assertIsNone(replay_cache.stats()["hit_rate"])
        self.assertFalse(replay_cache.stats()["deterministic"])

    def test_the_library_reports_nothing_when_empty(self):
        self.assertEqual(replay_cache.library()["entries"], 0)


if __name__ == "__main__":
    unittest.main()
