"""
test_run_isolation.py — a new run must inherit NOTHING from the last one, and a
journey must not inherit the light of the place it left.

Two defects, reported together from one playthrough.

THE POLLUTION. "when I run the game the current run is always polluted by
previous runs. when I switch worlds I get artifacts from the last one... sometimes
I see images of a character appear in my new world."

The run's own DATA was never wrong: `_perform_game_reset` rebuilds state.json and
history.json correctly. What survived was everything held in PROCESS memory
beside them — module-level dicts keyed by session_id, where a single-player boot's
session id is always the literal string "default". So the new run asked each of
them a question the old run had already answered, and got the old run's answer: a
face from the previous world for a subject with the same label, the previous
world's campfire, an interact close-up still inside its 90-second TTL and
therefore composited into the new run's first frame under the heading "THE SUBJECT
YOU JUST LOOKED AT CLOSELY", and a palette gloss that was then handed to the new
world's first tone call as "PREVIOUS TONE (keep matching this)".

THE JOURNEY. A filed bug: the player typed "travel to antartica" and did not go.
Every stage upstream worked — the consequence model wrote the flight and the
arrival, world evolution recorded the Antarctic ice shelf, the cut was detected,
and the scene text handed to the renderer read "a vast, featureless Antarctic ice
sheet". The frame that came back was the Utah desert at golden hour with a
helicopter parked in it, and the next choice slate offered "Heave open the truck
door".

Nothing overruled the cut. What overruled it was everything a cut politely keeps
hold of, all of which is written for the next ROOM rather than the next continent:
the carry-the-light camera block, the "Maintain the same lighting, time of day and
color palette" img2img clause, a `time_of_day` string pinned at reset off the level
plate's own palette ("weather: golden hour, rust, red dust"), a tone gloss anchored
to the previous one, and `seen_elements` still holding the desert's props.

What matters here:

  * Every run-scoped cache is registered for purging, or exempt WITH A REASON.
    The guard below walks the module, so the next cache somebody adds cannot
    quietly join the ones that used to leak.
  * A purge is scoped to one session. Two people on one server must not clear
    each other's run.
  * Both reset paths purge, and so does the mid-run world stitch — which is the
    moment the complaint is loudest and which previously cleared only the open
    encounter.
  * A region change releases the previous place's light in ALL of the places that
    were pinning it, because the failure was that four of five agreed and the
    fifth was outvoted. Fixing one of them would have changed nothing.
  * An ordinary hard cut still carries the light over. A doorway does not change
    the sun, and relighting every threshold would be the original continuity bug
    wearing this fix's clothes.

No network: every assertion is over pure helpers, module state, or source text.

Run with:
    python3 -m unittest test_run_isolation -v
"""

import ast
import os
import unittest
from pathlib import Path

os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("ANTHROPIC_API_KEY", "")

import engine

ROOT = Path(__file__).parent.resolve()
ENGINE_SRC = (ROOT / "engine.py").read_text(encoding="utf-8")


class TestEveryRunScopedCacheIsAccountedFor(unittest.TestCase):
    """The guard. Without it this file only proves that the caches which leaked
    ONCE have been fixed, which is the weaker half of the ask ("this needs to be
    bullet proof").

    The rule it leans on is a real convention in engine.py rather than a
    naming one: a CACHE is declared empty and filled at runtime, while static
    configuration is declared with its contents. So every module-level dict
    whose initialiser is `{}` is run state, and has to be either purged or
    exempt with a stated reason.
    """

    def _module_level_empty_dicts(self):
        found = {}
        for node in ast.parse(ENGINE_SRC).body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets, value = [node.target.id], node.value
            elif isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                value = node.value
            else:
                continue
            if isinstance(value, ast.Dict) and not value.keys:
                for name in targets:
                    found[name] = node.lineno
        return found

    def test_no_run_scoped_cache_is_unaccounted_for(self):
        registered = (
            set(engine._RUN_CACHES_BY_SESSION)
            | set(engine._RUN_CACHES_BY_SESSION_TUPLE)
            | set(engine._RUN_CACHES_PROCESS_WIDE)
            | set(engine._RUN_CACHES_EXEMPT)
        )
        orphans = {
            name: line for name, line in self._module_level_empty_dicts().items()
            if name not in registered
        }
        self.assertEqual(
            orphans, {},
            "engine.py grew a module-level cache that purge_run_caches() has "
            "never heard of. Every one of these is a place the previous run can "
            "hand its answer to the new one — which is the bug this file exists "
            "for. Add it to _RUN_CACHES_BY_SESSION (keyed by session id), "
            "_RUN_CACHES_BY_SESSION_TUPLE (keyed by a tuple starting with the "
            "session id), _RUN_CACHES_PROCESS_WIDE (no session in the key), or "
            "_RUN_CACHES_EXEMPT with the reason it must survive a reset.",
        )

    def test_every_exemption_states_a_reason(self):
        for name, reason in engine._RUN_CACHES_EXEMPT.items():
            self.assertGreater(
                len(reason.strip()), 20,
                f"{name} is exempt from the purge without saying why. The next "
                f"reader has to be able to tell a deliberate exemption from a "
                f"forgotten one.",
            )

    def test_the_registered_names_actually_exist(self):
        """A rename that misses the tuples would leave a purge that silently
        clears nothing, which reads exactly like a purge that works."""
        for name in (engine._RUN_CACHES_BY_SESSION
                     + engine._RUN_CACHES_BY_SESSION_TUPLE
                     + engine._RUN_CACHES_PROCESS_WIDE):
            self.assertTrue(
                hasattr(engine, name),
                f"{name} is registered for purging but no longer exists in "
                f"engine.py",
            )

    def test_the_tuple_keyed_caches_are_listed_as_tuple_keyed(self):
        """Popping a tuple-keyed dict by a bare session id is a no-op that looks
        like success, so the two kinds are listed apart and must stay apart."""
        for name in engine._RUN_CACHES_BY_SESSION_TUPLE:
            self.assertNotIn(name, engine._RUN_CACHES_BY_SESSION)


class TestPurgeClearsTheRunAndOnlyTheRun(unittest.TestCase):

    SID = "isolation_probe"
    OTHER = "isolation_bystander"

    def setUp(self):
        import encounter
        import gemini_image_utils
        self.encounter = encounter
        self.gemini = gemini_image_utils
        for sid in (self.SID, self.OTHER):
            self.addCleanup(engine.purge_run_caches, sid, reason="test cleanup")

    def _dirty_everything(self, sid):
        engine._VISUAL_TONE_CACHE[sid] = f"{sid} golden hour, red dust"
        engine._PORTRAIT_SPEND[sid] = 7
        engine._FLIPBOOK_SEQUENCES[sid] = {"frames": ["a", "b"]}
        engine._OPENING_PREFETCH[sid] = {"url": "/old/opening.png"}
        engine._INTERACT_PLATES[sid] = {"label": "The Watcher",
                                        "path": "/old/watcher.png"}
        engine._PORTRAIT_CACHE[(sid, "the watcher", "scenehash", "")] = \
            "/old/world/watcher.png"
        engine._CAMP_CACHE[(sid, "v4", "world", "labels", "jeep")] = "/old/camp.png"
        self.encounter._LOOK_READ_CACHE[sid] = (0.0, "a man in a blue vest")

    def _residue(self, sid):
        residue = []
        for name in engine._RUN_CACHES_BY_SESSION:
            if sid in getattr(engine, name):
                residue.append(name)
        for name in engine._RUN_CACHES_BY_SESSION_TUPLE:
            if any(k[0] == sid for k in getattr(engine, name)):
                residue.append(name)
        if sid in self.encounter._LOOK_READ_CACHE:
            residue.append("encounter._LOOK_READ_CACHE")
        return residue

    def test_a_purge_leaves_nothing_of_the_run(self):
        self._dirty_everything(self.SID)
        engine.purge_run_caches(self.SID, reason="test")
        self.assertEqual(
            self._residue(self.SID), [],
            "these caches would have answered the next run with the last one's "
            "data — the portrait and interact-plate ones by handing it a picture "
            "of a character from a world the player has left",
        )

    def test_a_purge_leaves_another_session_alone(self):
        """Two people play one server. A reset is not a licence to clear
        somebody else's run."""
        self._dirty_everything(self.SID)
        self._dirty_everything(self.OTHER)
        engine.purge_run_caches(self.SID, reason="test")
        self.assertEqual(self._residue(self.SID), [])
        self.assertEqual(
            sorted(self._residue(self.OTHER)),
            sorted(list(engine._RUN_CACHES_BY_SESSION)
                   + list(engine._RUN_CACHES_BY_SESSION_TUPLE)
                   + ["encounter._LOOK_READ_CACHE"]),
            "purging one session took another session's run with it",
        )

    def test_the_img2img_continuity_handle_is_dropped(self):
        """A module global in gemini_image_utils holding the last frame this
        PROCESS corrected, with no session in it at all — so the first render of
        a new world could continue from the last render of the old one."""
        self.gemini._last_corrected_image = "a PIL image of the desert"
        self.addCleanup(setattr, self.gemini, "_last_corrected_image", None)
        engine.purge_run_caches(self.SID, reason="test")
        self.assertIsNone(self.gemini._last_corrected_image)

    def test_the_frame_we_continue_from_is_dropped(self):
        """`_last_image_path` is what a render reaches for when history is empty,
        which is precisely the state a reset leaves history in."""
        engine._last_image_path = "/sessions/default/images/last_night.png"
        engine.purge_run_caches(self.SID, reason="test")
        self.assertIsNone(engine._last_image_path)

    def test_purging_a_session_that_never_played_is_harmless(self):
        engine.purge_run_caches("a_session_that_does_not_exist", reason="test")
        engine.purge_run_caches(self.SID, reason="test")  # and twice over

    def test_the_media_purge_only_deletes_regenerable_frames(self):
        sid = "isolation_media_probe"
        img_dir = Path(engine._get_image_dir(sid))
        keep = img_dir / "notes.json"
        frames = [img_dir / "turn_1.png", img_dir / "turn_2.jpg",
                  img_dir / "prop_jeep.png", img_dir / "companion_watcher.png"]
        for f in frames:
            f.write_bytes(b"not really a png")
        keep.write_text("{}", encoding="utf-8")
        try:
            removed = engine.purge_run_media(sid)
            self.assertEqual(removed, len(frames))
            for f in frames:
                self.assertFalse(f.exists(), f"{f.name} survived the purge")
            self.assertTrue(
                keep.exists(),
                "the media purge is for frames, not for everything in the folder",
            )
        finally:
            import shutil
            shutil.rmtree(engine._get_session_root(sid), ignore_errors=True)

    def test_a_companion_plate_does_not_outlive_the_run(self):
        """The literal complaint: "sometimes I see images of a character appear
        in my new world". Companion and prop plates are written to stable
        sweep-protected filenames precisely so they can be re-referenced
        forever, which is right within a run and wrong across one."""
        sid = "isolation_plate_probe"
        img_dir = Path(engine._get_image_dir(sid))
        plate = img_dir / "companion_the_watcher.png"
        plate.write_bytes(b"a face from the last world")
        try:
            engine.purge_run_media(sid)
            self.assertFalse(plate.exists())
        finally:
            import shutil
            shutil.rmtree(engine._get_session_root(sid), ignore_errors=True)


class TestEveryPlaceARunBeginsPurges(unittest.TestCase):
    """Source assertions. The purge is only as good as its call sites, and the
    two reset paths had already drifted into two different ideas of what a reset
    means — one archived and released voices, the other cleared the feed, and
    neither touched the caches.
    """

    def _body(self, name, until):
        return ENGINE_SRC.split(f"def {name}(", 1)[1].split(until, 1)[0]

    def test_the_new_game_reset_purges_before_it_builds(self):
        body = self._body("_perform_game_reset", "\ndef api_reset")
        self.assertIn("purge_run_caches(", body)
        self.assertIn("purge_run_media(", body)
        # Order matters and is the whole reason this is asserted on source: the
        # reset renders an opening frame, reads it, and mints a tone gloss, and
        # each of those consults a cache. A purge at the END of the reset would
        # arrive after the very frames it exists to protect. Matched on the CALL,
        # not the name — the function's opening comment mentions it too.
        self.assertLess(
            body.index("purge_run_caches("),
            body.index("= generate_intro_turn_feed_items("),
            "the purge runs after the intro has already been generated, so the "
            "intro is still built from the previous run's caches",
        )

    def test_the_hard_reset_purges(self):
        body = self._body("reset_state", "\ndef ")
        self.assertIn("purge_run_caches(", body)
        self.assertIn("purge_run_media(", body)

    def test_deleting_a_session_purges(self):
        body = self._body("delete_session", "\n# Legacy constants")
        self.assertIn("purge_run_caches(", body)

    def test_a_world_stitch_purges_and_forgets_the_old_props(self):
        """Switching worlds mid-run is where the complaint was loudest, and it
        used to clear only the open encounter."""
        body = self._body("apply_experience_world", "\ndef maybe_apply_world_transition")
        self.assertIn("purge_run_caches(", body)
        # The forgetting grew past three keys and moved into one helper (the
        # goal, the heat, the story clock, the roster, the flipbook keyframe —
        # see test_world_stitch); the stitch has to call it, and the helper
        # still has to forget the props.
        self.assertIn("_clear_world_scoped_state(state)", body)
        clear = self._body("_clear_world_scoped_state", "\ndef ")
        self.assertIn('state["seen_elements"] = []', clear)
        self.assertIn('state["scene_objects"] = []', clear)
        self.assertIn('state["scene_objects_turn"] = -1', clear)


class TestARegionChangeIsNotJustAnotherRoom(unittest.TestCase):

    def test_the_action_from_the_filed_bug_is_a_region_change(self):
        for phrase in ("travel to antartica",
                       "a helicopter appears and transports you to antartica",
                       "fly to the city",
                       "drive to the big building",
                       "teleport to the facility",
                       "sail for the mainland",
                       "you wake up in a white room"):
            self.assertTrue(engine.is_region_change(phrase), phrase)

    def test_ordinary_movement_is_not(self):
        """Every one of these is from the same capture as the Antarctica turn.
        They are moves WITHIN the desert, and the desert's light is theirs."""
        for phrase in ("Move to the mesa.",
                       "Scramble up the red scree",
                       "Sprint toward the industrial tanks",
                       "Heave open the truck door",
                       "Vault the chain link fence",
                       "get to the top",
                       "climb onto the roof",
                       "Enter the doorway",
                       "Photograph the gauge",
                       ""):
            self.assertFalse(engine.is_region_change(phrase), phrase)

    def test_an_elevator_is_a_new_room_and_keeps_the_light(self):
        """"Ride the elevator to the top floor" is a long-haul verb and a
        destination by the plain rule, and it is also the same building — which
        keeps its weather and its hour. Relighting it would be the continuity bug
        this fix exists to avoid, wearing the fix's clothes."""
        for phrase in ("ride the elevator to the top floor",
                       "take the stairs down to the basement",
                       "ride the freight lift to the loading bay"):
            self.assertFalse(engine.is_region_change(phrase), phrase)

    def test_an_observation_never_relocates_however_it_is_worded(self):
        self.assertFalse(engine.is_region_change(
            "follow the access road toward the city with your eyes"))

    def test_the_clock_survives_a_region_change_and_the_weather_does_not(self):
        """The capture had "7:14pm | weather: golden hour, rust, red dust,
        chain-link steel" going into a render of an Antarctic ice sheet. Red dust
        is what came back. The hour is the story's and travels; the weather was
        the desert's and does not."""
        pinned = ("7:14pm | weather: golden hour, rust, red dust, chain-link "
                  "steel | mood: oppressive suffocating dread")
        self.assertEqual(engine._clock_only(pinned), "7:14pm")
        self.assertEqual(engine._clock_only(""), "")
        self.assertEqual(engine._clock_only("9:02am"), "9:02am")


class TestTheRegionChangeReachesTheRenderer(unittest.TestCase):
    """The Antarctica frame was not lost to one bad instruction. It was lost to
    four concrete statements that the light was red desert dusk, against one
    sentence naming Antarctica. Each is asserted separately, because fixing any
    single one of them would have changed nothing.
    """

    def setUp(self):
        # build_image_prompt classifies the action, and the classifier falls
        # through to the network for wordings its keyword lists do not cover.
        # A prompt-shape assertion must not depend on an API key.
        orig = engine._ask
        engine._ask = lambda prompt, **kw: "FORWARD_MOVEMENT"
        self.addCleanup(setattr, engine, "_ask", orig)

    def _prompt(self, **kw):
        kw.setdefault("player_choice", "travel to antartica")
        kw.setdefault("dispatch", "A vast, featureless Antarctic ice sheet.")
        return engine.build_image_prompt(**kw)

    def test_a_journey_is_classified_without_a_round_trip(self):
        """`_detect_movement_type` had no entry for long-haul travel, so the
        commonest typed journey in the game paid for an LLM call and then took
        whatever came back. On a failure it defaults to 'exploration' — a change
        of continent rendered as a slight pan from the same spot, which a live
        capture caught happening on a 403."""
        asked = []
        orig = engine._ask
        engine._ask = lambda prompt, **kw: (asked.append(prompt), "STATIONARY")[1]
        self.addCleanup(setattr, engine, "_ask", orig)
        self.assertEqual(
            engine._detect_movement_type("travel to antartica"), "forward_movement")
        self.assertEqual(asked, [], "a journey should not need asking about")

    def test_a_region_change_withdraws_the_carry_the_light_instruction(self):
        relocated = self._prompt(hard_transition=True, region_change=True)
        self.assertNotIn("Carry over the light", relocated)
        self.assertIn("DIFFERENT PLACE ON THE MAP", relocated)
        # Withdrawing an instruction the model never hears is indistinguishable
        # from leaving it in place, so the release is stated outright.
        for released in ("weather", "palette", "light"):
            self.assertIn(released, relocated.lower())

    def test_an_ordinary_hard_cut_still_carries_the_light(self):
        room = self._prompt(player_choice="step inside the pump house",
                            hard_transition=True, region_change=False)
        self.assertIn("Carry over the light", room)
        self.assertNotIn("DIFFERENT PLACE ON THE MAP", room)

    def test_the_reference_frame_is_demoted_to_film_stock(self):
        """The img2img clause is the most CONCRETE line in the payload — it names
        lighting, time of day and palette in one breath and sits at the very end.
        That is why the desert won outright."""
        src = ENGINE_SRC.split("if prev_img_paths_list:", 1)[1][:2000]
        self.assertIn("if region_change:", src)
        region = src.split("if region_change:", 1)[1].split("elif hard_transition:", 1)[0]
        self.assertIn("FILM STOCK", region)
        self.assertIn("do not carry any of them into this frame", region)
        self.assertNotIn("Maintain the same lighting", region)

    def test_the_tone_gloss_stops_anchoring_on_the_place_just_left(self):
        """A hard transition re-asks for the tone but is deliberately handed the
        old gloss as "PREVIOUS TONE (keep matching this)", because a doorway does
        not change the sun. A continent does."""
        sid = "tone_region_probe"
        engine._VISUAL_TONE_CACHE.pop(sid, None)
        self.addCleanup(engine._VISUAL_TONE_CACHE.pop, sid, None)
        asked = []
        orig = engine._ask
        engine._ask = lambda prompt, **kw: (asked.append(prompt), "cold blue tone")[1]
        self.addCleanup(setattr, engine, "_ask", orig)

        engine._VISUAL_TONE_CACHE[sid] = "golden hour, rust, red dust"
        engine.summarize_world_prompt_for_image(
            "an Antarctic ice shelf", session_id=sid,
            hard_transition=True, frame_idx=4, relight=True,
        )
        self.assertNotIn("PREVIOUS TONE", asked[-1])
        self.assertNotIn("red dust", asked[-1])
        self.assertIn("DIFFERENT PLACE", asked[-1])

    def test_a_doorway_still_anchors_on_the_previous_tone(self):
        sid = "tone_room_probe"
        engine._VISUAL_TONE_CACHE.pop(sid, None)
        self.addCleanup(engine._VISUAL_TONE_CACHE.pop, sid, None)
        asked = []
        orig = engine._ask
        engine._ask = lambda prompt, **kw: (asked.append(prompt), "same tone")[1]
        self.addCleanup(setattr, engine, "_ask", orig)

        engine._VISUAL_TONE_CACHE[sid] = "golden hour, rust, red dust"
        engine.summarize_world_prompt_for_image(
            "a dark interior", session_id=sid,
            hard_transition=True, frame_idx=4, relight=False,
        )
        self.assertIn("PREVIOUS TONE", asked[-1])
        self.assertIn("red dust", asked[-1])

    def test_a_failed_tone_call_still_keeps_the_last_good_one(self):
        """Dropping the ANCHOR must not also drop the FALLBACK. One transient
        403 used to render the rest of a run "in the style of" an error message,
        and the guard against that is returning the cached gloss — which a
        relight must still be able to do."""
        sid = "tone_failure_probe"
        engine._VISUAL_TONE_CACHE.pop(sid, None)
        self.addCleanup(engine._VISUAL_TONE_CACHE.pop, sid, None)
        orig = engine._ask
        engine._ask = lambda prompt, **kw: "Signal interrupted due to API error."
        self.addCleanup(setattr, engine, "_ask", orig)

        engine._VISUAL_TONE_CACHE[sid] = "golden hour, rust, red dust"
        got = engine.summarize_world_prompt_for_image(
            "an Antarctic ice shelf", session_id=sid,
            hard_transition=True, frame_idx=4, relight=True,
        )
        self.assertEqual(got, "golden hour, rust, red dust")

    def test_the_turn_pipeline_forgets_the_old_worlds_props(self):
        """The other half of the filed bug. The frame is only half of what the
        player sees: the slate after the Antarctica turn offered "Heave open the
        truck door", because `seen_elements` feeds the choice generator through
        grounded_entities and nothing ever pruned it on a relocation."""
        body = ENGINE_SRC.split("def advance_turn_image_fast(", 1)[1] \
                         .split("\ndef ", 1)[0]
        self.assertIn("is_region_change(choice)", body)
        guard = body.split("if hard_transition and not is_timeout_penalty", 1)[1][:600]
        self.assertIn('state["seen_elements"] = []', guard)
        self.assertIn('state["scene_objects_turn"] = -1', guard)

    def test_a_timeout_penalty_never_relocates_anything(self):
        """A timeout is the world acting while the camera holds still. It must
        not be able to reach the relight path however the last action was
        worded."""
        body = ENGINE_SRC.split("def advance_turn_image_fast(", 1)[1] \
                         .split("\ndef ", 1)[0]
        self.assertIn("if hard_transition and not is_timeout_penalty "
                      "and is_region_change(choice):", body)


class TestTheCampfireKnowsWhichWorldItIsIn(unittest.TestCase):
    """Belt to the purge's braces. The purge is what actually stops the previous
    world's pictures reappearing, but a purge is a thing somebody has to remember
    to call, and the failure is silent and reads as the image model misbehaving.
    A world in the key means a missed purge can only cost a regeneration.
    """

    def test_the_camp_key_names_the_world(self):
        key = engine._camp_cache_key("default", ["The Watcher"], "/jeep.png")
        self.assertEqual(key[0], "default")
        self.assertIn(
            engine._active_world_sig("default"), key,
            "the camp cache key was the session, the roster and the jeep — "
            "nothing that changes when the player walks into a different world, "
            "so the same companions sat around the previous world's fire",
        )

    def test_two_worlds_cannot_share_a_camp_shot(self):
        orig = engine._active_world_sig
        engine._active_world_sig = lambda sid="default": "exp/world-a"
        self.addCleanup(setattr, engine, "_active_world_sig", orig)
        a = engine._camp_cache_key("default", ["The Watcher"], "/jeep.png")
        engine._active_world_sig = lambda sid="default": "exp/world-b"
        b = engine._camp_cache_key("default", ["The Watcher"], "/jeep.png")
        self.assertNotEqual(a, b)

    def test_the_world_signature_survives_a_missing_session(self):
        """It is read on a cache-key path, so it must never raise."""
        self.assertIsInstance(engine._active_world_sig("no_such_session"), str)


class TestTheTestFixtureIsNeverTheRulebook(unittest.TestCase):
    """The third thing that playthrough found, and the one that was permanent.

    Reported as "it feels random sometimes like the features are either trying to
    work and break or the prompts get randomly generated incorrectly". It was not
    randomness: `action_consequence_instructions` in the live prompt file was 451
    characters, byte-identical to `prompts/harness.generic.json`. The authored one
    is 15,461. The game was playing the test fixture — no fairness doctrine, no
    death rules, no tension rhythm, no stillness beats.

    No test run did it. `worlds_store._blank_prompts` seeds a NEW World from the
    harness so it cannot inherit whatever is loaded into Play, which is the right
    instinct about the wrong set of keys: the place, the cast and the camera
    belong to a World and should start blank, but the rulebook is how the GAME
    works and a blank one is the rules deleted. Every world made in the editor
    was born that way, and `load_world` stamps a World's prompts onto the live
    file on every bind and every reset — so the pollution reinstalled itself each
    run, which is why it read as intermittent rather than as a broken file.
    """

    def setUp(self):
        import worlds_store
        self.ws = worlds_store

    def test_a_world_created_today_ships_a_real_rulebook(self):
        blank = self.ws._blank_prompts()
        self.assertEqual(
            self.ws.harness_doctrine_in(blank), [],
            "a world created in the editor is born holding the test fixture's "
            "rulebook, so playing it deletes the game's rules",
        )
        # A number, not just "not the fixture": the failure mode is a block that
        # is present and far too short to contain any doctrine at all.
        self.assertGreater(
            len(str(blank.get("action_consequence_instructions", ""))), 5000,
            "the consequence doctrine is too short to contain the fairness "
            "rules, the death rules or the pacing",
        )

    def test_the_place_is_still_blank_in_a_new_world(self):
        """The fix must not turn "new world" into "a copy of the shipped one".
        An unauthored PLACE is the point; an unauthored GAME is the bug."""
        blank = self.ws._blank_prompts()
        self.assertLess(
            len(str(blank.get("world_initial_state", ""))), 2000,
            "a new world now arrives pre-authored, which defeats creating one",
        )

    def test_the_detector_is_exact_match_only(self):
        """Authored prose must never trip this, however short somebody writes
        it — worlds legitimately author their own consequence doctrine."""
        harness = self.ws._harness_prompts()
        fixture = harness["action_consequence_instructions"]
        self.assertEqual(
            self.ws.harness_doctrine_in(
                {"action_consequence_instructions": fixture}),
            ["action_consequence_instructions"])
        self.assertEqual(
            self.ws.harness_doctrine_in(
                {"action_consequence_instructions": fixture + "\nAlso: be kind."}),
            [], "a world that edited the fixture is authoring, not polluted")
        self.assertEqual(self.ws.harness_doctrine_in({}), [])
        self.assertEqual(
            self.ws.harness_doctrine_in({"action_consequence_instructions": ""}), [])

    def test_the_authored_worlds_are_left_alone(self):
        """somewhere.json and world.json both author ~15,000 characters of
        consequence doctrine. The guard must not touch either."""
        import json
        for name in ("somewhere.json", "world.json"):
            path = ROOT / "worlds" / name
            if not path.exists():
                continue
            blob = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                self.ws.harness_doctrine_in(blob.get("prompts") or {}), [],
                f"{name} was misread as polluted",
            )

    def test_binding_a_gutted_world_substitutes_rather_than_drops(self):
        """Dropping the key would leave the live value in place, which makes what
        the game plays depend on what was loaded before it — the same class of
        bug as the run pollution above."""
        src = (ROOT / "worlds_store.py").read_text(encoding="utf-8")
        body = src.split("def load_world(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("harness_doctrine_in(fields)", body)
        self.assertIn("prompts_store.load_defaults()", body)
        self.assertIn("fields[key] = replacement", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
