#!/usr/bin/env python3
"""Flipbook sequences: the grid has to come back apart the way it went together.

Every bug this file guards against actually shipped. The grid was hardcoded 4x4
in three places that didn't know about each other (the splitter's `// 4`, the
prompt's sixteen-cell ASCII diagram, and a `panel_16.png` filename), so the only
count that worked was the one nobody wanted — sixteen panels of a 1K generation
are 256px each. And playback went out as a 256-colour GIF, which threw away the
resolution that splitting a big grid was for in the first place.

So: the count is a setting, the shape follows from the count, the prompt's
diagram is generated from the same table the splitter reads, and what comes out
is lossless PNG.
"""

import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from PIL import Image

import flipbook


def _grid(width=800, height=600, path=None):
    """A grid image whose every pixel encodes where in the frame it came from,
    so a mis-ordered or mis-cropped split is provable rather than eyeballed."""
    img = Image.new("RGB", (width, height))
    for x in range(width):
        for y in range(height):
            img.putpixel((x, y), (x * 255 // max(1, width - 1),
                                  y * 255 // max(1, height - 1), 128))
    if path:
        img.save(path)
    return img


class TestTheCountIsASetting(unittest.TestCase):

    def test_the_supported_counts_are_2_4_8_16(self):
        self.assertEqual(flipbook.FRAME_COUNTS, (2, 4, 8, 16))

    def test_four_is_the_default_not_sixteen(self):
        # 16 panels of one generation is the old bootleg-quality behaviour.
        self.assertEqual(flipbook.DEFAULT_FRAMES, 4)
        self.assertEqual(flipbook.normalize_frames(None), 4)
        self.assertEqual(flipbook.normalize_frames(""), 4)

    def test_a_config_string_is_a_count(self):
        # tunables/enum values and form posts arrive as strings.
        self.assertEqual(flipbook.normalize_frames("8"), 8)
        self.assertEqual(flipbook.normalize_frames(" 16 "), 16)

    def test_an_unsupported_count_downgrades_instead_of_raising(self):
        # A stale setting must not be able to break a turn.
        self.assertEqual(flipbook.normalize_frames(1), 2)
        self.assertEqual(flipbook.normalize_frames(3), 2)
        self.assertEqual(flipbook.normalize_frames(9), 8)
        self.assertEqual(flipbook.normalize_frames(999), 16)
        self.assertEqual(flipbook.normalize_frames("garbage"), 4)

    def test_every_count_has_a_shape_that_holds_its_frames(self):
        for n in flipbook.FRAME_COUNTS:
            rows, cols = flipbook.shape_for(n)
            self.assertEqual(rows * cols, n, n)

    def test_the_square_counts_keep_the_canvas_aspect(self):
        # A square split is the only one that hands each panel the ratio the
        # grid was generated at, which is why 4 and 16 are the exact ones.
        for n in (4, 16):
            rows, cols = flipbook.shape_for(n)
            self.assertEqual(rows, cols, n)

    def test_the_uneven_counts_stay_as_near_square_as_possible(self):
        # 8 as 4x2 would hand back ultrawide slivers; 2x4 is the near-square one.
        self.assertEqual(flipbook.shape_for(8), (2, 4))
        self.assertEqual(flipbook.shape_for(2), (1, 2))


class TestSplitting(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.grid = self.tmp / "grid.png"
        _grid(path=self.grid)

    def test_every_count_yields_exactly_that_many_panels(self):
        for n in flipbook.FRAME_COUNTS:
            out = self.tmp / f"out{n}"
            panels = flipbook.split_grid(self.grid, n, out_dir=out)
            self.assertEqual(len(panels), n, n)
            self.assertTrue(all(p.is_file() for p in panels), n)

    def test_panels_are_lossless_png_not_gif(self):
        # The whole reason for splitting a high-res grid.
        panels = flipbook.split_grid(self.grid, 4, out_dir=self.tmp / "png")
        for p in panels:
            self.assertEqual(p.suffix, ".png")
            with Image.open(p) as img:
                self.assertEqual(img.format, "PNG")
                # A GIF would be palettised; these must carry full colour.
                self.assertEqual(img.mode, "RGB")

    def test_panels_are_named_in_playback_order(self):
        panels = flipbook.split_grid(self.grid, 8, out_dir=self.tmp / "order")
        self.assertEqual([p.name for p in panels],
                         [f"grid_f{i:02d}.png" for i in range(1, 9)])

    def test_panels_are_written_flat_so_the_web_layer_can_serve_them(self):
        # Generated images are served by BASENAME out of the session's images
        # dir; a subdirectory 404s.
        out = self.tmp / "flat"
        panels = flipbook.split_grid(self.grid, 4, out_dir=out)
        for p in panels:
            self.assertEqual(p.parent, out)

    def test_reading_order_is_left_to_right_then_down(self):
        # Panel 1 comes from the top-left of the grid and the last from the
        # bottom-right. The gradient makes that checkable: red rises with x,
        # green with y.
        panels = flipbook.split_grid(self.grid, 4, out_dir=self.tmp / "read")
        first = Image.open(panels[0]).getpixel((0, 0))
        last = Image.open(panels[-1]).getpixel((0, 0))
        self.assertLess(first[0], last[0])   # further right
        self.assertLess(first[1], last[1])   # further down
        top_right = Image.open(panels[1]).getpixel((0, 0))
        self.assertGreater(top_right[0], first[0])   # right of panel 1
        self.assertEqual(top_right[1], first[1])     # same row

    def test_each_panel_is_a_real_fraction_of_the_grid(self):
        # 4 panels of a given grid are twice the width and height of 16 — the
        # entire quality argument for making the count a setting.
        four = flipbook.split_grid(self.grid, 4, out_dir=self.tmp / "q4")
        sixteen = flipbook.split_grid(self.grid, 16, out_dir=self.tmp / "q16")
        w4, h4 = Image.open(four[0]).size
        w16, h16 = Image.open(sixteen[0]).size
        self.assertAlmostEqual(w4 / w16, 2.0, delta=0.1)
        self.assertAlmostEqual(h4 / h16, 2.0, delta=0.1)

    def test_the_divider_sliver_is_trimmed_off(self):
        # An even crop keeps a piece of the grid line on the panel edge, which
        # flickers as a border on every frame of playback.
        panels = flipbook.split_grid(self.grid, 4, out_dir=self.tmp / "inset")
        w, h = Image.open(panels[0]).size
        self.assertLess(w, 800 // 2)
        self.assertLess(h, 600 // 2)

    def test_a_grid_too_small_to_split_is_refused_not_crashed(self):
        tiny = self.tmp / "tiny.png"
        _grid(width=8, height=8, path=tiny)
        self.assertEqual(flipbook.split_grid(tiny, 16, out_dir=self.tmp / "t"), [])


class TestTheSequenceDescriptor(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.grid = self.tmp / "shot.png"
        _grid(path=self.grid)

    def test_the_still_is_the_last_panel(self):
        # Everything that only understands one image (SCAN, the vision pass,
        # the next turn's img2img reference) has to get the frame where the
        # action ENDED, not a mid-action blur.
        seq = flipbook.sequence_from_grid(self.grid, 4, out_dir=self.tmp / "s")
        self.assertEqual(seq["still_path"], seq["frame_paths"][-1])
        self.assertEqual(seq["first_path"], seq["frame_paths"][0])

    def test_it_carries_what_a_player_needs_to_play_it(self):
        seq = flipbook.sequence_from_grid(self.grid, 8, out_dir=self.tmp / "p",
                                          frame_ms=250)
        self.assertEqual(seq["frame_count"], 8)
        self.assertEqual(len(seq["frame_paths"]), 8)
        self.assertEqual(seq["frame_ms"], 250)
        self.assertEqual(seq["grid"], "2x4")

    def test_a_failed_split_is_none_rather_than_a_half_sequence(self):
        tiny = self.tmp / "tiny.png"
        _grid(width=4, height=4, path=tiny)
        self.assertIsNone(
            flipbook.sequence_from_grid(tiny, 16, out_dir=self.tmp / "n"))


class TestGuideImages(unittest.TestCase):
    """The old code attached prompts/flipbook_blank_grid_template.png, which was
    never in the repo — so every flipbook generation ran with no layout
    reference at all. Generating them means a new count brings its own."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_a_guide_is_built_for_every_supported_count(self):
        built = flipbook.build_all_guides(root=self.tmp)
        self.assertEqual(len(built), len(flipbook.FRAME_COUNTS))
        for path in built:
            self.assertTrue(path.is_file(), path)

    def test_a_guide_has_the_shape_it_is_named_for(self):
        for n in flipbook.FRAME_COUNTS:
            path = flipbook.build_guide(n, root=self.tmp)
            rows, cols = flipbook.shape_for(n)
            self.assertIn(f"{rows}x{cols}", path.name)
            with Image.open(path) as img:
                w, h = img.size
            # Same cell size across the grid, so the guide's own aspect is the
            # shape's aspect — what the model is being asked to reproduce.
            self.assertAlmostEqual(w / h, (cols * 384) / (rows * 216), delta=0.01)

    def test_the_guide_lookup_reports_a_missing_guide_rather_than_a_bad_path(self):
        # This is the bug being fixed: a path that doesn't exist was silently
        # treated as "no layout hint needed".
        self.assertIsNone(flipbook.find_guide(4, root=self.tmp / "empty"))
        flipbook.build_guide(4, root=self.tmp)
        self.assertIsNotNone(flipbook.find_guide(4, root=self.tmp))

    def test_the_attached_guide_carries_no_text_to_copy(self):
        # Labels in a reference image come back burned into the panels, so the
        # numbered variant exists for humans only and is never the default.
        blank = flipbook.build_guide(4, out_path=self.tmp / "blank.png")
        numbered = flipbook.build_guide(4, out_path=self.tmp / "num.png",
                                        numbered=True)
        self.assertNotEqual(Image.open(blank).tobytes(),
                            Image.open(numbered).tobytes())
        self.assertEqual(flipbook.build_guide(4, out_path=self.tmp / "d.png").stat().st_size,
                         blank.stat().st_size)


class TestThePromptFollowsTheShape(unittest.TestCase):
    """The prompt used to be prose hand-written for 4x4, including a sixteen
    cell ASCII diagram. Any other count contradicted the picture it drew."""

    def test_the_no_text_rule_comes_before_the_diagram(self):
        """Panels came back with "T=0s" burned into the corner.

        The instruction talks about time advancing and then hands the model a
        diagram with numbers sitting inside cells; it read both as things to draw.
        A ban thousands of characters later did not survive that, so it is stated
        up front and the diagram is explained as a key immediately after it.
        """
        text = flipbook.grid_prompt(4)
        ban = text.index("NO TEXT IS DRAWN IN THIS IMAGE")
        diagram = text.index("+---")
        self.assertLess(ban, diagram, "the ban must precede the diagram")
        self.assertIn("not \"T=0s\"", text)
        self.assertIn("NOT part of the picture", text)

    def test_the_diagram_names_every_frame_exactly_once(self):
        for n in flipbook.FRAME_COUNTS:
            text = flipbook.grid_prompt(n)
            for i in range(1, n + 1):
                self.assertIn(str(i), text, (n, i))
            self.assertIn(flipbook.grid_label(n), text, n)

    def test_the_diagram_has_a_row_per_row_of_the_shape(self):
        for n in flipbook.FRAME_COUNTS:
            rows, _cols = flipbook.shape_for(n)
            diagram_rows = [l for l in flipbook.grid_prompt(n).splitlines()
                            if l.startswith("|")]
            self.assertEqual(len(diagram_rows), rows, n)

    def test_the_layout_table_and_the_splitter_agree(self):
        # One table drives both, so the order the model is shown can never
        # drift from the order the frames are read back in.
        for n in flipbook.FRAME_COUNTS:
            flat = [i for row in flipbook.panel_grid(n) for i in row]
            self.assertEqual(flat, list(range(1, n + 1)), n)

    def test_a_single_row_shape_does_not_talk_about_dropping_down(self):
        self.assertNotIn("DOWN", flipbook.grid_prompt(2))
        self.assertIn("DOWN", flipbook.grid_prompt(4))

    def test_the_first_and_last_panel_contracts_are_stated(self):
        text = flipbook.grid_prompt(4)
        self.assertIn("PANEL 1:", text)
        self.assertIn("PANEL 4:", text)
        self.assertIn("no captions", text.lower())


class TestStalePromptsAreNotArguedWith(unittest.TestCase):
    """Worlds carry their own copy of the flipbook prompt, and every copy written
    before the count was a setting says "THE RENDER MUST BE A 4×4 GRID" and walks
    through sixteen numbered frames. Handed that with a request for a 2x2 grid,
    the model has two contradictory instructions."""

    OLD = ("**THE RENDER MUST BE A 4×4 GRID OF IMAGES.**\n"
           "These 16 frames form one continuous action sequence.")

    def test_the_old_sixteen_frame_prompt_is_stale_at_every_other_shape(self):
        for n in (2, 4, 8):
            self.assertTrue(flipbook.prefix_is_stale(self.OLD, n), n)

    def test_it_is_not_stale_at_the_shape_it_was_written_for(self):
        self.assertFalse(flipbook.prefix_is_stale(self.OLD, 16))

    def test_a_shape_agnostic_prompt_is_never_stale(self):
        # The shipped text: no grid, no count, so it survives any shape.
        shipped = json.loads(
            (Path(__file__).parent / "prompts" / "simulation_prompts.defaults.json")
            .read_text(encoding="utf-8")).get("gemini_flipbook_4panel_prefix") or ""
        self.assertTrue(shipped, "the authored flipbook prompt still has to exist")
        for n in flipbook.FRAME_COUNTS:
            self.assertFalse(flipbook.prefix_is_stale(shipped, n), n)

    def test_nothing_and_empty_are_not_stale(self):
        self.assertFalse(flipbook.prefix_is_stale("", 4))
        self.assertFalse(flipbook.prefix_is_stale(None, 4))

    def test_panel_1_is_not_read_as_a_frame_count(self):
        # "Panel 1 is the earliest moment" is true at every shape.
        self.assertFalse(flipbook.prefix_is_stale("Panel 1 is the earliest moment.", 8))


class TestTheEngineDecidesPerSession(unittest.TestCase):
    """Flipbook has to be switchable at runtime AND per session: a Watch render
    turning it on for itself must not flip it on for whoever is playing."""

    def setUp(self):
        import engine
        self.engine = engine
        self._saved = (engine.FLIPBOOK_ENABLED, engine.FLIPBOOK_FRAMES,
                       engine.FLIPBOOK_FRAME_MS)

    def tearDown(self):
        (self.engine.FLIPBOOK_ENABLED, self.engine.FLIPBOOK_FRAMES,
         self.engine.FLIPBOOK_FRAME_MS) = self._saved

    def test_a_session_with_no_opinion_follows_the_global_knob(self):
        self.engine.FLIPBOOK_ENABLED = True
        self.engine.FLIPBOOK_FRAMES = 8
        self.assertEqual(self.engine.flipbook_settings({}),
                         {"enabled": True, "frames": 8, "frame_ms": self.engine.FLIPBOOK_FRAME_MS})

    def test_a_session_can_override_the_global_either_way(self):
        self.engine.FLIPBOOK_ENABLED = False
        self.assertTrue(self.engine.flipbook_active({"flipbook_mode": True}))
        self.engine.FLIPBOOK_ENABLED = True
        self.assertFalse(self.engine.flipbook_active({"flipbook_mode": False}))

    def test_a_session_can_pin_its_own_frame_count(self):
        self.engine.FLIPBOOK_FRAMES = 4
        self.assertEqual(self.engine.flipbook_settings({"flipbook_frames": 16})["frames"], 16)

    def test_the_viewfinder_is_never_a_flipbook(self):
        # CAMERA frames are a composed plate with their own contract; a grid of
        # in-betweens would paint over the viewfinder.
        import game_identity
        self.engine.FLIPBOOK_ENABLED = True
        with unittest.mock.patch.object(game_identity, "is_viewfinder_spec",
                                        return_value=True):
            self.assertFalse(self.engine.flipbook_active({}, {"camera": "viewfinder"}))
        self.assertTrue(self.engine.flipbook_active({}, {"camera": "first_person"}))

    def test_the_prompt_asks_for_the_shape_the_session_is_set_to(self):
        for n in flipbook.FRAME_COUNTS:
            block = self.engine._flipbook_action_block("Run", "You run.", False, n)
            self.assertIn(str(n), block, n)

    def test_playback_length_is_the_count_times_the_hold(self):
        # The prompt asks for an action that fits the time it will be on screen,
        # which used to be the literal "4 seconds" at every shape.
        self.assertEqual(self.engine._flipbook_seconds(4, 500), 2.0)
        self.assertEqual(self.engine._flipbook_seconds(16, 250), 4.0)


class TestTheSequenceRidesTheBeat(unittest.TestCase):
    """The turn loop hands back ONE still because history, SCAN, the vision pass
    and the next turn's reference are all written for a single image. The frames
    travel beside it, on the feed item — and the still is the sequence's LAST
    frame, so a client that ignores sequences shows the right picture."""

    def setUp(self):
        import api
        import engine
        self.api, self.engine = api, engine
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_world_image = engine.WORLD_IMAGE_ENABLED
        self._orig_gen = engine._gen_image
        engine.WORLD_IMAGE_ENABLED = True
        self.frames = [self.tmp / f"beat_f{i:02d}.png" for i in (1, 2, 3, 4)]
        for f in self.frames:
            Image.new("RGB", (16, 16)).save(f)

    def tearDown(self):
        self.engine.WORLD_IMAGE_ENABLED = self._orig_world_image
        self.engine._gen_image = self._orig_gen
        self.engine.take_flipbook_sequence("default")

    def _run_turn(self):
        return self.engine._generate_and_append_scene_image(
            caption="a quiet ridge at dusk", dispatch="You crest the ridge.",
            choice="Move forward", frame_idx=1, world_prompt="",
            session_id="default", write_history=False)

    def _fake_flipbook_turn(self):
        """A generation that produced a sequence, as _flipbook_generate would."""
        def gen(*a, **k):
            self.engine._FLIPBOOK_SEQUENCES["default"] = {
                "frame_paths": [str(p) for p in self.frames],
                "frame_count": 4, "frame_ms": 300, "grid": "2x2",
                "still_path": str(self.frames[-1]),
            }
            return (str(self.frames[-1]), "a prompt", None)
        self.engine._gen_image = gen

    def test_the_beat_carries_the_frames_and_the_still_is_the_last_one(self):
        self.api.app.test_client().post("/api/reset")
        self._fake_flipbook_turn()
        self._run_turn()
        beat = (self.engine._load_state("default").get("feed_log") or [])[-1]
        seq = (beat.get("metadata") or {}).get("sequence") or {}
        self.assertEqual(seq.get("frame_count"), 4)
        self.assertEqual(len(seq.get("frames") or []), 4)
        self.assertEqual(seq.get("frame_ms"), 300)
        self.assertEqual(beat.get("image_url"), seq["frames"][-1],
                         "the still a flipbook turn reports is its last frame")

    def test_the_frames_are_pngs_and_not_a_gif(self):
        self.api.app.test_client().post("/api/reset")
        self._fake_flipbook_turn()
        self._run_turn()
        beat = (self.engine._load_state("default").get("feed_log") or [])[-1]
        for url in ((beat.get("metadata") or {}).get("sequence") or {})["frames"]:
            self.assertIn(".png", url)
            self.assertNotIn(".gif", url)

    def test_status_reports_the_current_sequence_for_a_client_that_rejoined(self):
        self.api.app.test_client().post("/api/reset")
        self._fake_flipbook_turn()
        self._run_turn()
        data = self.api.app.test_client().get("/api/status").get_json()
        payload = data.get("data") or data
        self.assertEqual(len(payload["current_sequence"]["frames"]), 4)
        self.assertIn("frames", payload["flipbook"])

    def test_a_still_only_turn_does_not_inherit_the_last_sequence(self):
        # The bug this prevents: motion from the previous turn replaying under a
        # new dispatch, animating an action that isn't happening.
        self.api.app.test_client().post("/api/reset")
        self._fake_flipbook_turn()
        self._run_turn()
        still = self.tmp / "plain.png"
        Image.new("RGB", (16, 16)).save(still)
        self.engine._gen_image = lambda *a, **k: (str(still), "a prompt", None)
        self._run_turn()
        st = self.engine._load_state("default")
        self.assertIsNone(st.get("current_sequence"))
        self.assertIsNone(((st["feed_log"][-1].get("metadata")) or {}).get("sequence"))

    def test_a_blocked_turn_keeps_the_last_still_but_drops_its_motion(self):
        self.api.app.test_client().post("/api/reset")
        self._fake_flipbook_turn()
        self._run_turn()
        held = self.engine._load_state("default").get("current_image_url")
        self.engine._gen_image = lambda *a, **k: None
        self._run_turn()
        st = self.engine._load_state("default")
        self.assertEqual(st.get("current_image_url"), held)
        self.assertIsNone(st.get("current_sequence"))

    def test_the_sequence_is_handed_over_once(self):
        self.engine._FLIPBOOK_SEQUENCES["default"] = {"frame_paths": [], "frame_count": 0}
        self.assertIsNotNone(self.engine.take_flipbook_sequence("default"))
        self.assertIsNone(self.engine.take_flipbook_sequence("default"))

    def test_a_one_frame_sequence_is_not_a_sequence(self):
        # Nothing to animate; the client would flash a single frame and stop.
        payload = self.engine.flipbook_web_payload(
            {"frame_paths": [str(self.frames[0])], "frame_count": 1}, "default")
        self.assertIsNone(payload)


class TestGeneratingAFlipbookTurn(unittest.TestCase):
    """The generation runs INLINE and returns the still.

    It used to run in a daemon thread beside the still, writing its result to
    state for a Discord client to poll, while the turn itself set the image path
    to None — which in the web app is the "signal lost" beat. Flipbook mode did
    not render a sequence, it rendered nothing.
    """

    SESSION = "flipbook_gen_test"

    def setUp(self):
        import engine
        import gemini_image_utils
        self.engine = engine
        self.gemini = gemini_image_utils
        self.tmp = Path(tempfile.mkdtemp())
        self.calls = []
        self._orig = gemini_image_utils.generate_gemini_img2img

    def tearDown(self):
        self.gemini.generate_gemini_img2img = self._orig
        self.engine.take_flipbook_sequence(self.SESSION)

    def _model_returns_a_grid(self, ok=True):
        def fake(**kwargs):
            self.calls.append(kwargs)
            if not ok:
                return None
            grid = self.tmp / "grid.png"
            _grid(width=640, height=480, path=grid)
            return str(grid)
        self.gemini.generate_gemini_img2img = fake

    def _generate(self, st=None, frames=4, refs=None, ref_is_anchor=False):
        return self.engine._flipbook_generate(
            prompt_str="A ridge at dusk.", caption="ridge", choice="Move forward",
            dispatch="You crest the ridge.", world_prompt="", time_of_day="dusk",
            img_dir=self.tmp, session_id=self.SESSION,
            st=dict({"flipbook_mode": True, "flipbook_frames": frames}, **(st or {})),
            refs=refs if refs is not None else [],
            ref_is_anchor=ref_is_anchor)

    def test_it_returns_the_sequence_and_the_still_is_the_last_frame(self):
        self._model_returns_a_grid()
        seq = self._generate()
        self.assertEqual(seq["frame_count"], 4)
        self.assertEqual(seq["still_path"], seq["frame_paths"][-1])
        self.assertTrue(Path(seq["still_path"]).is_file())

    def test_the_frames_land_in_the_session_image_dir_the_web_layer_serves(self):
        self._model_returns_a_grid()
        seq = self._generate()
        for p in seq["frame_paths"]:
            self.assertEqual(Path(p).parent, self.tmp)

    def test_the_prompt_and_the_wire_request_agree_on_the_shape(self):
        self._model_returns_a_grid()
        self._generate(frames=8)
        call = self.calls[-1]
        self.assertEqual(call["flipbook_grid"], (2, 4))
        self.assertIn("2x4", call["prompt"])
        self.assertNotIn("4x4 grid", call["prompt"].lower())

    def test_the_layout_guide_goes_last_where_it_carries_least_weight(self):
        self._model_returns_a_grid()
        self._generate()
        refs = self.calls[-1]["reference_image_path"]
        self.assertIn("flipbook_guide_2x2.png", refs[-1])

    def test_the_previous_sequences_last_frame_leads_the_references(self):
        # Gemini weights the first reference most heavily, and panel 1 has to be
        # the next instant after wherever the camera actually is.
        anchor = self.tmp / "anchor.png"
        _grid(width=32, height=32, path=anchor)
        self._model_returns_a_grid()
        self._generate(st={"flipbook_last_frame": str(anchor)})
        self.assertEqual(self.calls[-1]["reference_image_path"][0], str(anchor))

    def test_an_anchored_caller_is_not_handed_the_last_turns_opening_frame(self):
        """The reported "it warped me back in time".

        An anchored caller (the encounter plate) has said its own frame is the
        truth. `prev_last` was already excluded for it; `prev_first` was not, so a
        confrontation staged straight after the player set a truck on fire got a
        clean photograph of that truck NOT on fire, from one panel before the
        flames. The model believed the photograph: the encounter opened on the
        pre-fire scene, and the antagonist the brief had written never made it
        into the frame because two of the three references showed an empty place.
        """
        anchor = self.tmp / "captured.png"
        stale_first = self.tmp / "before_the_fire_f01.png"
        stale_last = self.tmp / "after_the_fire_f04.png"
        for p in (anchor, stale_first, stale_last):
            _grid(width=32, height=32, path=p)
        self._model_returns_a_grid()

        self._generate(st={"flipbook_first_frame": str(stale_first),
                           "flipbook_last_frame": str(stale_last)},
                       refs=[str(anchor)], ref_is_anchor=True)

        refs = self.calls[-1]["reference_image_path"]
        self.assertEqual(refs[0], str(anchor), "the caller's frame must lead")
        self.assertNotIn(str(stale_first), refs,
                         "the previous sequence's opening frame is time travel here")
        self.assertNotIn(str(stale_last), refs)

    def test_an_ordinary_turn_still_gets_the_opening_frame_as_context(self):
        """It is only wrong for an anchored caller; a normal turn wants the width."""
        stale_first = self.tmp / "turn_open_f01.png"
        _grid(width=32, height=32, path=stale_first)
        self._model_returns_a_grid()
        self._generate(st={"flipbook_first_frame": str(stale_first)})
        self.assertIn(str(stale_first), self.calls[-1]["reference_image_path"])

    def test_it_leaves_the_anchors_the_next_turn_needs(self):
        self._model_returns_a_grid()
        seq = self._generate()
        st = self.engine._load_state(self.SESSION)
        self.assertEqual(st["flipbook_last_frame"], seq["still_path"])
        self.assertEqual(st["flipbook_first_frame"], seq["frame_paths"][0])
        self.assertEqual(st["flipbook_last_grid"], seq["grid_path"])
        self.assertEqual(len(st["current_sequence"]["frames"]), 4)

    def test_a_generation_that_never_came_back_is_a_fallback_not_a_dead_turn(self):
        self._model_returns_a_grid(ok=False)
        self.assertIsNone(self._generate())

    def test_a_stale_authored_prompt_is_dropped_rather_than_contradicted(self):
        self._model_returns_a_grid()
        with unittest.mock.patch.dict(
                self.engine.PROMPTS,
                {"gemini_flipbook_4panel_prefix": "MUST BE A 4x4 GRID of 16 frames."},
                clear=False):
            self._generate(frames=4)
        self.assertNotIn("16 frames", self.calls[-1]["prompt"])


class TestTheApiTogglesOneSession(unittest.TestCase):

    def setUp(self):
        import api
        import engine
        self.engine = engine
        self.client = api.app.test_client()

    def tearDown(self):
        self.client.post("/api/flipbook", json={"enabled": None, "session": "flipbook_test"})

    def post(self, **body):
        body.setdefault("session", "flipbook_test")
        return self.client.post("/api/flipbook", json=body).get_json()["data"]

    def test_it_turns_flipbook_on_for_that_session_only(self):
        # Pin the global OFF while checking isolation. An untouched session
        # inherits the global by design, so leaving this to whatever the
        # process booted with tested the ambient tunable, not the isolation.
        saved = self.engine.FLIPBOOK_ENABLED
        try:
            self.engine.FLIPBOOK_ENABLED = False
            data = self.post(enabled=True, frames=8)
            self.assertTrue(data["settings"]["enabled"])
            self.assertEqual(data["settings"]["frames"], 8)
            self.assertFalse(
                self.engine.flipbook_active(self.engine._load_state("default")))
        finally:
            self.engine.FLIPBOOK_ENABLED = saved

    def test_an_unsupported_count_is_taken_to_the_nearest_real_one(self):
        self.assertEqual(self.post(enabled=True, frames=5)["settings"]["frames"], 4)
        self.assertEqual(self.post(frames=99)["settings"]["frames"], 16)

    def test_the_frame_hold_is_clamped_to_something_watchable(self):
        self.assertEqual(self.post(frame_ms=5)["settings"]["frame_ms"], 80)
        self.assertEqual(self.post(frame_ms=99999)["settings"]["frame_ms"], 1000)

    def test_clearing_it_drops_the_session_back_to_the_global_setting(self):
        self.post(enabled=True)
        saved = self.engine.FLIPBOOK_ENABLED
        try:
            self.engine.FLIPBOOK_ENABLED = True
            self.assertTrue(self.post(enabled=None)["settings"]["enabled"])
            self.engine.FLIPBOOK_ENABLED = False
            self.assertFalse(self.post()["settings"]["enabled"])
        finally:
            self.engine.FLIPBOOK_ENABLED = saved

    def test_it_advertises_the_counts_a_client_may_offer(self):
        self.assertEqual(self.post()["frame_counts"], list(flipbook.FRAME_COUNTS))


class TestThePlaybackPolicies(unittest.TestCase):
    """Play watches the motion once and holds where it ended; Watch loops it
    while the next turn renders. Same frames, one argument apart — and neither
    goes through a GIF, because 256 colours is what the split was avoiding."""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).parent
        cls.js = (root / "static" / "js" / "standalone.js").read_text(encoding="utf-8")
        cls.engine_src = (root / "engine.py").read_text(encoding="utf-8")

    def test_there_is_one_player_driving_both_modes(self):
        self.assertEqual(self.js.count("function createSequencePlayer"), 1)

    def test_play_runs_it_once(self):
        play = self.js.split("function playSceneSequence", 1)[1].split("\n  }", 1)[0]
        self.assertIn("loop: false", play)

    def test_watch_loops_it(self):
        stage = self.js.split("function paintStageFrame", 1)[1].split("\n    }", 1)[0]
        self.assertIn("loop: true", stage)

    def test_the_in_between_frames_skip_the_scene_change_ceremony(self):
        # setScene crossfades for 1.5s and fires a flash and a VCR glitch. Per
        # frame that is a strobe, and the crossfade outlasts the frame.
        # In-between frames go through the double-buffered swap: it paints the
        # HIDDEN A/B layer and flips which one shows, because repainting the
        # visible layer in place left it unrasterized for a beat and the black
        # underlay showed through as dropped frames.
        self.assertIn("function paintSequenceFrame", self.js)
        painter = self.js.split("const sceneSequence = createSequencePlayer", 1)[1] \
                         .split("function playSceneSequence", 1)[0]
        self.assertIn("paintSequenceFrame", painter)
        self.assertIn("info.first", painter)
        # The swap must not re-announce the scene: markScenePainted is the boot
        # gate AND the interact dive's hand-off, so once per scene, not per frame.
        swap = self.js.split("function paintSequenceFrame", 1)[1].split("\n  }", 1)[0]
        self.assertNotIn("markScenePainted", swap)
        self.assertIn("scene-active", swap)

    def test_a_still_arriving_from_anywhere_else_stops_playback(self):
        # A fallback still, a camera plate or the next turn must not have last
        # turn's frames painting over the top of it.
        set_scene = self.js.split("function setScene(imageUrl, opts)", 1)[1] \
                           .split("function paintActiveScene", 1)[0]
        self.assertIn("sceneSequence.stop()", set_scene)

    def test_frames_are_decoded_before_the_motion_starts(self):
        player = self.js.split("function createSequencePlayer", 1)[1].split("\n  }", 1)[0]
        self.assertIn("preload", player)

    def test_reduced_motion_gets_the_outcome_and_no_animation(self):
        player = self.js.split("function createSequencePlayer", 1)[1]
        self.assertIn("prefersReducedMotion()", player.split("return { play", 1)[0])

    def test_scan_captures_the_frame_the_turn_ended_on(self):
        play = self.js.split("function playSceneSequence", 1)[1].split("\n  }", 1)[0]
        self.assertIn("state.currentStillUrl = stillUrl", play)

    def test_nothing_in_the_playback_path_builds_a_gif(self):
        self.assertNotIn("create_flipbook_gif", self.engine_src)
        self.assertNotIn("grid_to_flipbook_gif", self.engine_src)
        self.assertFalse((Path(__file__).parent / "create_flipbook_gif.py").exists(),
                         "the 4x4-hardcoded GIF builder is superseded by flipbook.py")


if __name__ == "__main__":
    unittest.main()
