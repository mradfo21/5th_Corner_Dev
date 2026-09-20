"""
test_scene_objects.py — coverage for on-screen object grounding: the things SCAN
named in the current frame have to reach the turn the player commits from it.

The defect this exists for: SCAN names up to a dozen interactable things in the
frame, and every one of them used to be discarded except the single label the
player tapped (which survived only as the permanence `subject`). The consequence
call was handed the frame as a multimodal part and its pixels as img2img
references, but never a LIST of what was in it — so a beat could only ever be
about the one thing that got poked, while the other five things on screen might
as well not have existed. Detection had already run and been paid for; the names
were simply thrown away.

What matters here:

  * The cache is stamped with the turn it describes, and expires by itself. A
    label list from two rooms ago would ask the consequence to feature things
    that are no longer in front of the player — the exact ungrounded/teleporting
    failure the spatial rules exist to prevent.
  * `turn_count` only increments once a turn has fully resolved, so a SCAN and
    the action committed from it share a stamp. If that ever stops being true
    the whole feature silently no-ops, so it is asserted here directly.
  * The directive asks for TWO things to be woven. Below two labels there is
    nothing to pair, and the prompt must not carry an impossible instruction.
  * On-screen labels LEAD the choice-grounding list. `seen_elements` is written
    by the async world-evolution rewrite and therefore lags a turn; what the
    player can physically act on right now outranks world memory.

No network: every assertion here is over pure helpers or source text.

Run with:
    python3 -m unittest test_scene_objects -v
"""

import os
import unittest
from pathlib import Path

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("ANTHROPIC_API_KEY", "")

import engine

ROOT = Path(__file__).parent.resolve()


def _dets(*labels):
    return [{"label": l} for l in labels]


class TestRecordSceneObjects(unittest.TestCase):
    """What SCAN saw, reduced to a comparable list of names."""

    def test_records_labels_with_the_current_turn(self):
        st = {"turn_count": 4}
        labels = engine.record_scene_objects(st, _dets("blast door", "mercenary"))
        self.assertEqual(labels, ["blast door", "mercenary"])
        self.assertEqual(st["scene_objects"], ["blast door", "mercenary"])
        self.assertEqual(st["scene_objects_turn"], 4)

    def test_deduplicates_case_insensitively(self):
        st = {"turn_count": 0}
        labels = engine.record_scene_objects(st, _dets("Blast Door", "blast door", "vent"))
        self.assertEqual(labels, ["Blast Door", "vent"])

    def test_drops_blank_and_malformed_entries(self):
        st = {"turn_count": 0}
        labels = engine.record_scene_objects(
            st, [{"label": "vent"}, {"label": "  "}, {}, None, {"label": None}])
        self.assertEqual(labels, ["vent"])

    def test_is_capped_so_the_prompt_stays_grounded_not_diluted(self):
        st = {"turn_count": 0}
        labels = engine.record_scene_objects(st, _dets(*[f"thing {i}" for i in range(20)]))
        self.assertEqual(len(labels), engine.SCENE_OBJECTS_MAX)
        # The cap keeps the FRONT of the pass: the tail of a detection is its
        # least salient part.
        self.assertEqual(labels[0], "thing 0")

    def test_an_empty_scan_still_stamps_the_turn(self):
        # Otherwise a scan that found nothing would leave the PREVIOUS turn's
        # labels in place, still stamped and still readable.
        st = {"turn_count": 2, "scene_objects": ["old pipe"], "scene_objects_turn": 1}
        self.assertEqual(engine.record_scene_objects(st, []), [])
        self.assertEqual(st["scene_objects"], [])
        self.assertEqual(st["scene_objects_turn"], 2)


class TestSceneObjectsExpire(unittest.TestCase):
    """Stale nouns are worse than none."""

    def test_labels_from_this_turn_are_honored(self):
        st = {"turn_count": 3, "scene_objects": ["vent", "creature"], "scene_objects_turn": 3}
        self.assertEqual(engine.scene_objects_for_turn(st), ["vent", "creature"])

    def test_labels_from_a_previous_turn_are_dropped(self):
        st = {"turn_count": 4, "scene_objects": ["vent", "creature"], "scene_objects_turn": 3}
        self.assertEqual(engine.scene_objects_for_turn(st), [])

    def test_turn_zero_is_a_real_turn_not_a_falsy_miss(self):
        st = {"turn_count": 0, "scene_objects": ["jeep"], "scene_objects_turn": 0}
        self.assertEqual(engine.scene_objects_for_turn(st), ["jeep"])

    def test_a_state_that_never_scanned_is_empty(self):
        self.assertEqual(engine.scene_objects_for_turn({}), [])
        self.assertEqual(engine.scene_objects_for_turn({"turn_count": 2}), [])

    def test_an_unstamped_cache_is_not_trusted(self):
        st = {"turn_count": 0, "scene_objects": ["jeep"]}
        self.assertEqual(engine.scene_objects_for_turn(st), [])

    def test_garbage_stamps_degrade_to_empty(self):
        st = {"turn_count": 1, "scene_objects": ["jeep"], "scene_objects_turn": "soon"}
        self.assertEqual(engine.scene_objects_for_turn(st), [])

    def test_a_reset_run_cannot_inherit_the_last_frame(self):
        # _perform_game_reset writes turn_count 0 alongside these two keys. The
        # stamp has to be a value turn 0 can never match.
        src = (ROOT / "engine.py").read_text(encoding="utf-8", errors="replace")
        self.assertIn('"scene_objects": [],', src)
        self.assertIn('"scene_objects_turn": -1,', src)
        st = {"turn_count": 0, "scene_objects": [], "scene_objects_turn": -1}
        self.assertEqual(engine.scene_objects_for_turn(st), [])


class TestScanAndItsTurnShareAStamp(unittest.TestCase):
    """The feature rests entirely on turn_count NOT moving between the scan and
    the consequence generated from it. If the increment ever migrates earlier in
    the pipeline, every cache read goes stale and this silently stops working."""

    def test_turn_count_increments_only_after_choices_are_appended(self):
        src = (ROOT / "engine.py").read_text(encoding="utf-8", errors="replace")
        bumps = src.count('st["turn_count"] = int(st.get("turn_count", 0)) + 1')
        # Three sites: the death turn, the held encounter round (added with
        # the multi-round fights on 09-18 — a round that keeps the fight open
        # is still a turn), and the ordinary turn. All in the turn loop.
        self.assertEqual(bumps, 3, "turn_count bump sites moved; re-check the "
                                   "scene-object cache staleness guard")
        # Both bumps live in _process_turn_background, after the phase that
        # generated the consequence — never inside advance_turn_image_fast.
        phase1 = src.index("def advance_turn_image_fast")
        phase1_end = src.index("def advance_turn_choices_deferred")
        self.assertNotIn('st["turn_count"] = int(st.get("turn_count", 0)) + 1',
                         src[phase1:phase1_end])


class TestOnscreenDirective(unittest.TestCase):
    """The prompt text that turns a list of nouns into a braided beat."""

    def test_names_the_objects_and_demands_a_pair(self):
        text = engine.onscreen_directive(["blast door", "mercenary", "vent"])
        self.assertIn("blast door, mercenary, vent", text)
        self.assertIn("AT LEAST TWO", text)
        self.assertIn("CHANGE", text)
        # Both surfaces have to show them, or the beat lands in the prose and
        # vanishes from the picture (the permanence failure, one level up).
        self.assertIn("`dispatch`", text)
        self.assertIn("`visual_scene`", text)

    def test_stays_out_of_the_prompt_when_there_is_nothing_to_pair(self):
        self.assertEqual(engine.onscreen_directive([]), "")
        self.assertEqual(engine.onscreen_directive(["lone pipe"]), "")

    def test_reaches_the_consequence_prompt(self):
        src = (ROOT / "engine.py").read_text(encoding="utf-8", errors="replace")
        grounding = src[src.index("grounding_block = ("):]
        self.assertIn("onscreen_directive(onscreen)", grounding[:600])


class TestGroundedEntities(unittest.TestCase):
    """The entity list handed to choice generation."""

    def test_onscreen_leads_and_memory_follows(self):
        st = {
            "turn_count": 1,
            "scene_objects": ["blast door"], "scene_objects_turn": 1,
            "seen_elements": ["red biome", "mercenary"],
        }
        # seen_elements is walked most-recent-first, matching the [-10:] intent
        # of the call site this replaced.
        self.assertEqual(engine.grounded_entities(st),
                         "blast door, mercenary, red biome")

    def test_falls_back_to_memory_alone_when_nothing_was_scanned(self):
        st = {"turn_count": 2, "seen_elements": ["red biome"]}
        self.assertEqual(engine.grounded_entities(st), "red biome")

    def test_a_label_in_both_appears_once(self):
        st = {
            "turn_count": 1,
            "scene_objects": ["Mercenary"], "scene_objects_turn": 1,
            "seen_elements": ["mercenary"],
        }
        self.assertEqual(engine.grounded_entities(st), "Mercenary")

    def test_is_bounded_and_truncates_from_memory_not_the_screen(self):
        st = {
            "turn_count": 1,
            "scene_objects": ["blast door", "vent"], "scene_objects_turn": 1,
            "seen_elements": [f"element {i}" for i in range(40)],
        }
        out = engine.grounded_entities(st, limit=5).split(", ")
        self.assertEqual(len(out), 5)
        self.assertEqual(out[:2], ["blast door", "vent"])

    def test_an_empty_state_is_an_empty_string(self):
        self.assertEqual(engine.grounded_entities({}), "")


class TestDirectiveReachesTheModel(unittest.TestCase):
    """Source-level wiring is not proof. This drives the real consequence
    generator with the model call stubbed out and reads the prompt that would
    have gone over the wire."""

    def setUp(self):
        self.sent = []
        self._real_ask = engine._ask
        self._real_history = engine.history

        def fake_ask(prompt, **kw):
            self.sent.append(prompt)
            return ('{"dispatch": "d", "visual_scene": "v", '
                    '"player_alive": true, "next_choices": ["a", "b", "c"]}')

        engine._ask = fake_ask
        engine.history = []
        self.addCleanup(lambda: setattr(engine, "_ask", self._real_ask))
        self.addCleanup(lambda: setattr(engine, "history", self._real_history))

    def _prompt_for(self, state: dict) -> str:
        engine._generate_combined_dispatches("Force the door", state)
        self.assertEqual(len(self.sent), 1, "expected exactly one model call")
        return self.sent[0]

    def test_a_scanned_frame_puts_its_objects_in_the_prompt(self):
        prompt = self._prompt_for({
            "turn_count": 3,
            "world_prompt": "A dead mining facility in the red desert.",
            "scene_objects": ["blast door", "mercenary", "support beam"],
            "scene_objects_turn": 3,
        })
        self.assertIn("ON SCREEN RIGHT NOW", prompt)
        self.assertIn("blast door, mercenary, support beam", prompt)
        self.assertIn("AT LEAST TWO", prompt)

    def test_an_unscanned_turn_carries_no_such_instruction(self):
        prompt = self._prompt_for({
            "turn_count": 3,
            "world_prompt": "A dead mining facility in the red desert.",
        })
        self.assertNotIn("ON SCREEN RIGHT NOW", prompt)

    def test_a_stale_cache_carries_no_such_instruction(self):
        prompt = self._prompt_for({
            "turn_count": 4,
            "world_prompt": "A dead mining facility in the red desert.",
            "scene_objects": ["blast door", "mercenary"],
            "scene_objects_turn": 3,
        })
        self.assertNotIn("ON SCREEN RIGHT NOW", prompt)
        # The stale pair, not the bare noun: the director's sheet names the
        # live level's landmarks, and on a machine whose level has a blast
        # door among them the bare word is in every prompt.
        self.assertNotIn("blast door, mercenary", prompt)

    def test_the_existing_grounding_contract_still_ships(self):
        # The new block is additive: the fairness/phase rules the death doctrine
        # depends on must still be in the same prompt. (There was an INJURY
        # STATE line asserted here too, fed from a tracked wound list; both went
        # with the wound parser — see engine's "how a run ends".)
        prompt = self._prompt_for({
            "turn_count": 1,
            "world_prompt": "A dead mining facility in the red desert.",
            "scene_objects": ["vent", "creature"], "scene_objects_turn": 1,
            "seen_elements": ["red biome"],
            "current_phase": "escalating",
        })
        self.assertIn("DISCOVERED ENTITIES", prompt)
        self.assertIn("red biome", prompt)
        self.assertIn("DETECTION:", prompt)
        self.assertIn("STORY PHASE: escalating", prompt)
        self.assertIn("ON SCREEN RIGHT NOW", prompt)


class TestDetectCacheIsScanGated(unittest.TestCase):
    """Photo targeting polls /api/detect every ~2.5s while the camera is armed.
    Opting that loop into the cache would put a locked state read+write on a hot
    path for no benefit — and the codebase already records that hung calls on
    this endpoint can exhaust every gunicorn thread."""

    @staticmethod
    def _cache_block() -> str:
        """The executable region of api_detect from the purpose gate onward.

        Sliced off the `if` statement itself rather than the bare string, so the
        endpoint's docstring (which names both the gate and the helper, in the
        other order) can't satisfy these assertions.
        """
        src = (ROOT / "engine.py").read_text(encoding="utf-8", errors="replace")
        api = src[src.index("def api_detect"):src.index("def api_danger")]
        return api[api.index("if str(data.get('purpose')"):]

    def test_only_purpose_scan_writes_state(self):
        block = self._cache_block()
        self.assertIn('== "scan"', block.split("\n")[0])
        self.assertIn("record_scene_objects", block)
        self.assertIn("_save_state", block)

    def test_a_cache_failure_cannot_break_the_scan(self):
        block = self._cache_block()
        self.assertIn("except Exception", block)
        # The response still goes out after the cache attempt — and the
        # sighting that rides on the same pass is its own try/except too.
        self.assertIn('out = {"objects": objects or [], "anti_loop_suppressed": suppressed}', block)
        self.assertIn("return jsonify(out)", block)
        self.assertIn("_stage_encounter_sighting(", block)
        self.assertIn("except Exception as e_sight:", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
