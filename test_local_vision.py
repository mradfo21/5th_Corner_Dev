"""
test_local_vision.py — unit tests for the on-device SCAN detector
(local_vision.py) and its wiring into engine._detect_objects.

The fusion logic is tested against a STUBBED MediaPipe, not the real model:
these assertions are about our rules — which COCO classes are allowed to exist
in this world, when a class needs the scene prompt to corroborate it, that the
camera operator's own hand never becomes a talkable figure — and they should
hold whatever the weights happen to say on a given frame. One integration test
at the end does run the real model, and is skipped when it isn't installed.

Run with:
    python3 -m pytest test_local_vision.py -v
"""

import io
import time
import unittest
from unittest.mock import patch

from PIL import Image

import engine
import local_vision


def frame_bytes(width=640, height=360, color=(40, 45, 50)):
    """A real (if boring) JPEG, so the decode path runs for real."""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="JPEG")
    return buf.getvalue()


def stub_boxes(*detections):
    """Patch the detector out. Each detection is (coco_label, score, box)."""
    return patch.object(local_vision, "_mediapipe_boxes", return_value=list(detections))


class TestPromptNouns(unittest.TestCase):
    """Reading this world's open vocabulary out of the scene prompt."""

    def test_finds_lexicon_nouns(self):
        nouns = local_vision.prompt_nouns(
            "A rusted silo stands beyond the chain-link fence. A floodlight burns.")
        phrases = [n.phrase for n in nouns]
        self.assertIn("rusted silo", phrases)
        self.assertIn("chain-link fence", phrases)
        self.assertIn("floodlight", phrases)

    def test_keeps_adjective_modifiers_for_specificity(self):
        nouns = local_vision.prompt_nouns("An abandoned armored personnel carrier sits there.")
        self.assertEqual([n.phrase for n in nouns], ["abandoned armored personnel carrier"])
        self.assertEqual(nouns[0].category, "vehicle")

    def test_ignores_proper_place_names(self):
        """"Hollis Ridge" is a place, not a ridge you can walk up to."""
        nouns = local_vision.prompt_nouns(
            "You approach the Hollis Ridge processing plant from the east.")
        phrases = [n.phrase for n in nouns]
        self.assertIn("processing plant", phrases)
        self.assertNotIn("ridge", phrases)

    def test_sentence_initial_capital_is_not_a_proper_name(self):
        nouns = local_vision.prompt_nouns("Silos loom over the yard.")
        self.assertIn("silos", [n.phrase for n in nouns])

    def test_ignores_the_players_own_body_and_gear(self):
        nouns = local_vision.prompt_nouns(
            "Your flashlight beam shakes in a gloved hand as you grip the steering wheel.")
        self.assertEqual(nouns, [])

    def test_ignores_pure_atmosphere(self):
        """Sunset/haze are in nearly every prompt and are never a point of interest."""
        nouns = local_vision.prompt_nouns(
            "A bruised sunset bleeds into haze over the overcast valley.")
        self.assertEqual([n.phrase for n in nouns], [])

    def test_deduplicates_on_the_head_noun(self):
        nouns = local_vision.prompt_nouns(
            "A silo ahead. Past it, another rusted silo, and a third silo beyond.")
        self.assertEqual(len([n for n in nouns if n.head == "silo"]), 1)

    def test_ranks_story_weight_over_word_order(self):
        """A padlock mentioned first must not outrank the figure mentioned last."""
        nouns = local_vision.prompt_nouns(
            "A padlock hangs on the gate. Beyond it, a figure watches.")
        self.assertEqual(nouns[0].head, "figure")

    def test_reads_spatial_hints(self):
        nouns = local_vision.prompt_nouns(
            "A barn sits to your left. A distant water tower breaks the horizon "
            "on the right.")
        by_head = {n.head: n for n in nouns}
        self.assertEqual(by_head["barn"].horizontal, "left")
        self.assertEqual(by_head["water tower"].horizontal, "right")
        self.assertEqual(by_head["water tower"].depth, "far")

    def test_empty_prompt_is_safe(self):
        self.assertEqual(local_vision.prompt_nouns(""), [])
        self.assertEqual(local_vision.prompt_nouns(None), [])


class TestPixelDetections(unittest.TestCase):
    """What MediaPipe says, and how much of it we believe."""

    def test_trusted_class_becomes_a_tag_on_its_own(self):
        with stub_boxes(("person", 0.7, (0.4, 0.3, 0.5, 0.7))):
            out = local_vision.detect(frame_bytes(), scene_prompt="")
        self.assertEqual([o["label"] for o in out], ["figure"])
        self.assertEqual(out[0]["kind"], "person")
        self.assertEqual(out[0]["source"], "pixels")

    def test_prompt_lends_its_specific_name_to_a_measured_box(self):
        """The box is COCO's "car"; the label should be the prompt's own words."""
        with stub_boxes(("car", 0.6, (0.05, 0.5, 0.35, 0.8))):
            out = local_vision.detect(
                frame_bytes(),
                scene_prompt="An abandoned armored personnel carrier sits to your left.")
        self.assertEqual([o["label"] for o in out], ["abandoned armored personnel carrier"])
        self.assertEqual(out[0]["source"], "pixels")

    def test_corroborate_class_is_dropped_without_prompt_support(self):
        """A "tv" in the middle of a forest is the classic dark-frame hallucination."""
        with stub_boxes(("tv", 0.5, (0.3, 0.3, 0.45, 0.45))):
            out = local_vision.detect(frame_bytes(), scene_prompt="Pines crowd the trail.")
        self.assertNotIn("television", [o["label"] for o in out])

    def test_corroborate_class_survives_when_the_prompt_backs_it_up(self):
        with stub_boxes(("tv", 0.5, (0.3, 0.3, 0.45, 0.45))):
            out = local_vision.detect(
                frame_bytes(),
                scene_prompt="A television flickers in the corner of the motel room.")
        self.assertIn("television", [o["label"] for o in out])

    def test_classes_that_cannot_exist_here_are_dropped(self):
        with stub_boxes(("surfboard", 0.9, (0.3, 0.3, 0.6, 0.6)),
                        ("giraffe", 0.9, (0.1, 0.1, 0.3, 0.3)),
                        ("broccoli", 0.9, (0.5, 0.5, 0.7, 0.7))):
            out = local_vision.detect(frame_bytes(), scene_prompt="")
        self.assertEqual(out, [])

    def test_operator_foreground_is_never_a_figure(self):
        """The regression this whole guard exists for.

        In the game's flagship exterior frame the highest-confidence COCO
        detection is the player's own hand holding a flashlight, at 0.66. Left
        alone it becomes a "figure" the UI offers to TALK to.
        """
        hand = (0.662, 0.616, 0.992, 0.993)
        with stub_boxes(("person", 0.66, hand)):
            out = local_vision.detect(frame_bytes(), scene_prompt="")
        self.assertEqual(out, [], "the operator's own hand must not become a tag")

    def test_a_figure_out_in_the_world_still_tags(self):
        """The guard must not swallow real subjects: same class, clear of the edge."""
        with stub_boxes(("person", 0.5, (0.45, 0.40, 0.55, 0.72))):
            out = local_vision.detect(frame_bytes(), scene_prompt="")
        self.assertEqual([o["label"] for o in out], ["figure"])

    def test_pinpoint_boxes_are_rejected_as_tape_noise(self):
        with stub_boxes(("person", 0.23, (0.783, 0.981, 0.800, 0.998))):
            out = local_vision.detect(frame_bytes(), scene_prompt="")
        self.assertEqual(out, [])


class TestPromptAnchoring(unittest.TestCase):
    """Placing labels the detector is structurally blind to."""

    def test_structures_are_anchored_from_the_prompt_alone(self):
        with stub_boxes():
            out = local_vision.detect(
                frame_bytes(),
                scene_prompt="A rusted silo rises past the gate of the processing plant.")
        labels = [o["label"] for o in out]
        self.assertIn("rusted silo", labels)
        self.assertTrue(all(o["source"] == "prompt" for o in out))

    def test_people_are_never_anchored_without_pixels(self):
        """A guessed person tag would offer a conversation with empty ground."""
        with stub_boxes():
            out = local_vision.detect(
                frame_bytes(),
                scene_prompt="A figure stands motionless near the treeline, watching.")
        labels = [o["label"] for o in out]
        self.assertNotIn("figure", labels)
        self.assertIn("treeline", labels)

    def test_anchored_boxes_stay_inside_the_frame(self):
        with stub_boxes():
            out = local_vision.detect(
                frame_bytes(),
                scene_prompt="A barn, a silo, a water tower, a gate, a fence, "
                             "a floodlight, a generator and a ladder.")
        self.assertTrue(out)
        for o in out:
            ymin, xmin, ymax, xmax = o["box_2d"]
            self.assertGreaterEqual(min(ymin, xmin), 0)
            self.assertLessEqual(max(ymax, xmax), 1000)
            self.assertLess(ymin, ymax)
            self.assertLess(xmin, xmax)

    def test_anchoring_can_be_switched_off(self):
        with stub_boxes(), patch.object(local_vision, "ANCHOR_PROMPT_NOUNS", False):
            out = local_vision.detect(frame_bytes(), scene_prompt="A rusted silo rises.")
        self.assertEqual(out, [])

    def test_respects_max_items(self):
        with stub_boxes():
            out = local_vision.detect(
                frame_bytes(),
                max_items=3,
                scene_prompt="A barn, a silo, a water tower, a gate, a fence, "
                             "a floodlight, a generator and a ladder.")
        self.assertLessEqual(len(out), 3)

    def test_no_prompt_and_no_boxes_yields_nothing(self):
        with stub_boxes():
            self.assertEqual(local_vision.detect(frame_bytes(), scene_prompt=""), [])


class TestFailureModes(unittest.TestCase):
    """detect() sits on a request path and must never raise."""

    def test_garbage_bytes_return_empty(self):
        self.assertEqual(local_vision.detect(b"not an image", scene_prompt="A silo."), [])

    def test_empty_bytes_return_empty(self):
        self.assertEqual(local_vision.detect(b"", scene_prompt="A silo."), [])

    def test_detector_explosion_returns_empty(self):
        with patch.object(local_vision, "_mediapipe_boxes", side_effect=RuntimeError("boom")):
            out = local_vision.detect(frame_bytes(), scene_prompt="")
        self.assertEqual(out, [])

    def test_detector_explosion_still_allows_prompt_tags(self):
        """A dead detector should degrade to prompt-anchored tags, not to nothing."""
        with patch.object(local_vision, "_mediapipe_boxes", side_effect=RuntimeError("boom")):
            out = local_vision.detect(frame_bytes(), scene_prompt="A rusted silo rises.")
        self.assertIn("rusted silo", [o["label"] for o in out])


class TestHangProtection(unittest.TestCase):
    """A hung inference must never hold a request thread.

    Observed in production: inference that takes ~16 ms locally never returned at
    all on the deploy target. /api/detect is polled every ~2.5 s by photo
    targeting against a worker with four threads, so a call that never returns
    doesn't degrade SCAN — it takes the whole service down.
    """

    def setUp(self):
        self._saved = (local_vision._timeouts, local_vision._breaker_open)
        local_vision._timeouts = 0
        local_vision._breaker_open = False

    def tearDown(self):
        local_vision._timeouts, local_vision._breaker_open = self._saved

    def test_a_hung_inference_gives_up_instead_of_blocking(self):
        def never_returns(_image):
            time.sleep(30)

        with patch.object(local_vision, "_run_detect", never_returns), \
             patch.object(local_vision, "INFERENCE_TIMEOUT_S", 0.3):
            t0 = time.time()
            out = local_vision.detect(frame_bytes(), scene_prompt="A rusted silo rises.")
            elapsed = time.time() - t0

        self.assertLess(elapsed, 5.0,
                        "detect() must abandon a hung inference, not wait on it")
        # The prompt-anchored half still works: a wedged detector costs pixel
        # accuracy, not the whole feature.
        self.assertIn("rusted silo", [o["label"] for o in out])

    def test_repeated_hangs_trip_the_breaker(self):
        def never_returns(_image):
            time.sleep(30)

        with patch.object(local_vision, "_run_detect", never_returns), \
             patch.object(local_vision, "INFERENCE_TIMEOUT_S", 0.2), \
             patch.object(local_vision, "_MAX_TIMEOUTS", 2):
            for _ in range(2):
                local_vision.detect(frame_bytes(), scene_prompt="A silo.")

        self.assertTrue(local_vision._breaker_open,
                        "consecutive hangs must stop us leaking a thread per scan")
        self.assertFalse(local_vision.available(),
                         "a tripped breaker must report unavailable so "
                         "DETECT_BACKEND=auto can fall back to Gemini")

    def test_the_breaker_short_circuits_further_inference(self):
        calls = []
        local_vision._breaker_open = True
        with patch.object(local_vision, "_run_detect",
                          lambda image: calls.append(1)):
            local_vision.detect(frame_bytes(), scene_prompt="A silo.")
        self.assertEqual(calls, [], "no inference should be attempted once tripped")


class TestEngineIntegration(unittest.TestCase):
    """The local backend has to come out of engine._detect_objects in exactly the
    shape the client already consumes."""

    def test_local_backend_produces_the_wire_contract(self):
        with patch.object(engine, "DETECT_BACKEND", "local"), \
             patch.object(engine, "VISION_ENABLED", True), \
             stub_boxes(("person", 0.5, (0.45, 0.40, 0.55, 0.72))):
            objects = engine._detect_objects(
                image_bytes=frame_bytes(), mime_type="image/jpeg",
                scene_prompt="A guard watches the gate.")

        self.assertTrue(objects)
        for o in objects:
            self.assertEqual(set(o), {"label", "cx", "cy", "w", "h", "kind", "speaks"})
            for key in ("cx", "cy", "w", "h"):
                self.assertGreaterEqual(o[key], 0.0)
                self.assertLessEqual(o[key], 1.0)

    def test_a_person_from_pixels_is_talkable(self):
        with patch.object(engine, "DETECT_BACKEND", "local"), \
             patch.object(engine, "VISION_ENABLED", True), \
             stub_boxes(("person", 0.5, (0.45, 0.40, 0.55, 0.72))):
            objects = engine._detect_objects(
                image_bytes=frame_bytes(), mime_type="image/jpeg",
                scene_prompt="A guard watches the gate.")
        self.assertEqual(objects[0]["label"], "guard")
        self.assertTrue(objects[0]["speaks"],
                        "a person detected in the pixels should offer TALK")

    def test_engines_underwhelming_filter_still_applies_to_local_output(self):
        """Both backends share _normalize_detections, so the filter is one copy."""
        with patch.object(engine, "DETECT_BACKEND", "local"), \
             patch.object(engine, "VISION_ENABLED", True), \
             patch.object(local_vision, "detect", return_value=[
                 {"label": "gloved hands", "box_2d": [400, 400, 600, 600], "kind": "object"},
                 {"label": "steel door", "box_2d": [300, 300, 700, 500], "kind": "object"},
             ]):
            objects = engine._detect_objects(
                image_bytes=frame_bytes(), mime_type="image/jpeg")
        self.assertEqual([o["label"] for o in objects], ["steel door"])

    def test_vision_disabled_short_circuits(self):
        with patch.object(engine, "VISION_ENABLED", False):
            self.assertEqual(engine._detect_objects(image_bytes=frame_bytes()), [])

    def test_local_backend_needs_no_gemini_key(self):
        """The point of the whole exercise: SCAN works with no API key at all."""
        with patch.object(engine, "DETECT_BACKEND", "local"), \
             patch.object(engine, "VISION_ENABLED", True), \
             patch.object(engine, "LLM_ENABLED", False), \
             patch.object(engine, "GEMINI_API_KEY", ""), \
             stub_boxes(("person", 0.5, (0.45, 0.40, 0.55, 0.72))):
            objects = engine._detect_objects(
                image_bytes=frame_bytes(), mime_type="image/jpeg")
        self.assertTrue(objects, "local detection must not depend on GEMINI_API_KEY")

    def test_auto_falls_back_to_gemini_when_local_is_unavailable(self):
        with patch.object(engine, "DETECT_BACKEND", "auto"), \
             patch.object(engine, "VISION_ENABLED", True), \
             patch.object(local_vision, "available", return_value=False), \
             patch.object(engine, "_detect_objects_gemini", return_value=[
                 {"label": "steel door", "box_2d": [300, 300, 700, 500]},
             ]) as gemini:
            objects = engine._detect_objects(
                image_bytes=frame_bytes(), mime_type="image/jpeg")
        gemini.assert_called_once()
        self.assertEqual([o["label"] for o in objects], ["steel door"])

    def test_local_only_does_not_silently_fall_back_to_a_paid_backend(self):
        with patch.object(engine, "DETECT_BACKEND", "local"), \
             patch.object(engine, "VISION_ENABLED", True), \
             patch.object(local_vision, "available", return_value=False), \
             patch.object(engine, "_detect_objects_gemini") as gemini:
            objects = engine._detect_objects(
                image_bytes=frame_bytes(), mime_type="image/jpeg")
        gemini.assert_not_called()
        self.assertEqual(objects, [])


@unittest.skipUnless(local_vision.available(),
                     "mediapipe / the .tflite model is not installed here")
class TestRealModel(unittest.TestCase):
    """One end-to-end pass with the actual weights on an actual game frame."""

    FRAME = "static/img/scene_exterior.png"

    def test_real_detection_returns_the_wire_contract(self):
        with open(self.FRAME, "rb") as f:
            data = f.read()
        out = local_vision.detect(
            data,
            scene_prompt="An abandoned armored personnel carrier sits to your left "
                         "beyond the chain-link fence of the processing plant.")
        self.assertTrue(out, "expected at least one detection on a real frame")
        for o in out:
            self.assertIn(o["source"], ("pixels", "prompt"))
            ymin, xmin, ymax, xmax = o["box_2d"]
            self.assertLess(ymin, ymax)
            self.assertLess(xmin, xmax)
            self.assertGreaterEqual(min(ymin, xmin), 0)
            self.assertLessEqual(max(ymax, xmax), 1000)

    def test_the_vehicle_is_located_from_pixels_not_guessed(self):
        """The carrier is the one thing COCO can see here; it should be measured."""
        with open(self.FRAME, "rb") as f:
            data = f.read()
        out = local_vision.detect(
            data, scene_prompt="An abandoned armored personnel carrier sits to your left.")
        carrier = [o for o in out if "carrier" in o["label"]]
        self.assertTrue(carrier, "expected the carrier to be detected")
        self.assertEqual(carrier[0]["source"], "pixels")

    def test_the_players_hand_is_not_offered_as_a_conversation(self):
        with open(self.FRAME, "rb") as f:
            data = f.read()
        objects = engine._normalize_detections(
            local_vision.detect(data, scene_prompt="You raise the flashlight."))
        self.assertEqual([o["label"] for o in objects if o["speaks"]], [])


if __name__ == "__main__":
    unittest.main()
