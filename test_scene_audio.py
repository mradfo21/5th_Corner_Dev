"""Offline tests for scene music + world sound (no network).

Since 2026-09-25 nothing here is generated: every lane plays the shipped
sound library (static/audio/library/, sound_library.py), made once on
ElevenLabs by tools/build_sound_library.py. These hold the wiring — what each
endpoint answers, from which words, under which switch — and every path that
still plays a file a person put on disk (a locked loop, a designer one-shot).
The library's own invariants are test_sound_library's.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import scene_audio
import sound_library


def _no_run(monkeypatch):
    """No session state: phase normal, no world, no vision read."""
    monkeypatch.setattr(scene_audio, "_run_context",
                        lambda session_id="default": {"phase": "normal", "world": "", "vision": ""})


def _music_dir(monkeypatch, tmp_path):
    music = tmp_path / "music"
    music.mkdir(exist_ok=True)
    monkeypatch.setattr(scene_audio, "MUSIC_DIR", music)
    monkeypatch.setattr(scene_audio, "_LOOP_META", music / "loop.json")
    monkeypatch.setattr(scene_audio, "_DIRECTION_PATH", music / "direction.json")
    monkeypatch.setattr(scene_audio, "_SFX_DIRECTION_PATH", music / "sfx_direction.json")
    monkeypatch.setattr(scene_audio, "_MENU_META", music / "menu.json")
    monkeypatch.setattr(scene_audio, "_MENU_DIRECTION_PATH", music / "menu_direction.json")
    return music


def test_an_encounter_is_scored_as_a_confrontation(monkeypatch, tmp_path):
    _no_run(monkeypatch)
    _music_dir(monkeypatch, tmp_path)
    rec = scene_audio.get_scene_audio("hostile — person — a miner with a pipe", mode="encounter")
    assert rec["music_id"].startswith("mus_enc_")
    assert sound_library.get(rec["music_id"])["instrumental"] is True


def test_the_authored_direction_steers_the_score(monkeypatch, tmp_path):
    """Typing a score in the editor has to change what plays on the next scene
    without anything being locked. With a library that means the words choose
    the track: the direction is read into the pick, and into the world's
    flavour, so a neon direction on a desert World scores it neon."""
    _no_run(monkeypatch)
    _music_dir(monkeypatch, tmp_path)
    plain = scene_audio.get_scene_audio("a sunlit kitchen")["music_id"]
    scene_audio.set_music_direction("neon synthwave, rain on chrome, cyberpunk")
    steered = scene_audio.get_scene_audio("a sunlit kitchen")["music_id"]
    assert "cyber" not in plain
    assert "cyber" in steered, steered


def test_the_place_decides_the_bed(monkeypatch, tmp_path):
    _no_run(monkeypatch)
    _music_dir(monkeypatch, tmp_path)
    bed = lambda text, mode="scene": scene_audio.inspect_scene(text, mode)["ambience_id"]
    assert bed("rain on a corrugated tin roof") in ("amb_rain_metal", "amb_rain")
    assert bed("a flooded cave tunnel, water dripping") in ("amb_cave", "amb_sewer", "amb_pump_station")
    assert bed("a neon street market, holographic signs") == "amb_neon_rain"
    assert bed("an unknown place") == "amb_room"


def test_every_ambience_was_asked_for_without_music():
    for e in sound_library.entries("ambience"):
        low = e["prompt"].lower()
        assert "no music" in low, e["id"]
        assert e["loop"] is True
        assert "loop" in low, e["id"]


def test_stock_catalog_covers_encounter_hits():
    status = scene_audio.stock_status()
    for key in ("encounter_enter", "encounter_lock", "encounter_resolve",
                "encounter_exit", "encounter_hitch", "encounter_die",
                "encounter_title", "encounter_survive"):
        rec = status[key]
        assert rec["ready"] and rec["url"].startswith(sound_library.URL_BASE + "stinger/")
        assert rec["seconds"] <= 4
        assert rec["loop"] is False
    for key in ("industrial", "rain", "cave", "wind", "room", "urban", "forest"):
        rec = status[key]
        assert rec["ready"] and rec["loop"] is True
        assert rec["seconds"] >= 8


def test_scene_conversation_and_encounter_get_different_beds(monkeypatch, tmp_path):
    _no_run(monkeypatch)
    _music_dir(monkeypatch, tmp_path)
    ids = {mode: scene_audio.get_scene_audio("a quiet hallway", mode=mode)["music_id"]
           for mode in ("scene", "conversation", "encounter")}
    assert ids["scene"].startswith("mus_scene_")
    assert ids["conversation"].startswith("mus_convo_")
    assert ids["encounter"].startswith("mus_enc_")


def _clear_mock(monkeypatch):
    monkeypatch.delenv("MOCK_MODE", raising=False)
    monkeypatch.delenv("STORYGEN_BACKEND", raising=False)
    try:
        import keys_store
        monkeypatch.setattr(keys_store, "_explicit_mock", False)
    except Exception:
        pass


def test_a_key_changes_nothing(monkeypatch):
    """No key makes sound and none is needed: the library plays the same with
    a Gemini key, an OpenAI key, or nothing at all."""
    _clear_mock(monkeypatch)
    for env in ("GEMINI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    assert scene_audio.is_available() is True
    for env in ("GEMINI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(env, "a-perfectly-good-key-1234")
    assert scene_audio.is_available() is True
    assert scene_audio.unavailable_reason() is None


def test_playing_the_library_writes_nothing(monkeypatch, tmp_path):
    """Previews, test clips, foley and beds are the library's own files: a
    preview is a URL under /static/, not a sample written to disk."""
    _no_run(monkeypatch)
    music = _music_dir(monkeypatch, tmp_path)
    prev = scene_audio.library_preview("a slow drone")
    assert prev["url"].startswith(sound_library.URL_BASE + "music/")
    for layer in ("music", "sfx", "stinger"):
        clip = scene_audio.library_test_clip("a quiet yard", layer=layer)
        assert clip["url"].startswith(sound_library.URL_BASE), layer
    assert scene_audio.action_foley("Vault the fence", session_id="t")["url"].startswith(
        sound_library.URL_BASE + "foley/")
    assert scene_audio.consequence_bed(
        "The hinges tear out of the frame and it swings wide", session_id="t")["url"].startswith(
        sound_library.URL_BASE + "consequence/")
    assert list(music.iterdir()) == []


def test_locking_a_track_copies_it_into_the_loop_slot(monkeypatch, tmp_path):
    """"Lock as the only track" chooses the closest library track and copies it
    in, so every path that serves and clears a lock keeps working, and a
    direction change takes a derived lock off like it always did."""
    music = _music_dir(monkeypatch, tmp_path)
    loop = scene_audio.library_loop("a slow uneasy drone")
    assert loop["source"] == "library"
    assert (music / "loop.mp3").stat().st_size > 1000
    assert scene_audio.custom_loop()["url"].startswith("/audio/loop.mp3?v=")
    scene_audio.set_music_direction("something else entirely")
    assert scene_audio.custom_loop() is None


def test_no_sound_provider_is_called_from_here():
    """The third provider is gone, key and wire both. A request library or an
    API host creeping back into this module would be a second key again."""
    src = Path(scene_audio.__file__).read_text(encoding="utf-8")
    low = src.lower()
    assert "import requests" not in src
    assert "api.elevenlabs.io" not in low
    assert "xi-api-key" not in low
    assert "elevenlabs_api_key" not in low


def test_a_missing_library_is_silence_with_a_reason(monkeypatch, tmp_path):
    _no_run(monkeypatch)
    _music_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(sound_library, "CATALOG_PATH", tmp_path / "nope" / "catalog.json")
    assert scene_audio.is_available() is False
    assert scene_audio.unavailable_reason() == scene_audio.MISSING_REASON
    assert scene_audio.get_scene_audio("a quiet forest at dawn") is None
    assert scene_audio.action_foley("Vault the fence") is None


def test_an_encounter_opens_on_its_stinger(monkeypatch, tmp_path):
    _no_run(monkeypatch)
    _music_dir(monkeypatch, tmp_path)
    rec = scene_audio.get_scene_audio("a collapsing grate", mode="encounter")
    assert rec["stinger_url"].endswith("stinger/encounter_enter.mp3")
    assert rec["sfx_url"].startswith(sound_library.URL_BASE + "ambience/")
    assert len(rec["stingers"]) >= 12


def test_resolve_audio_path_serves_session_clips_and_rejects_traversal(tmp_path, monkeypatch):
    audio = tmp_path / "audio"
    audio.mkdir()
    (audio / "scene_abc.mp3").write_bytes(b"ID3" + b"\x00" * 80)
    monkeypatch.setattr(scene_audio, "MUSIC_DIR", tmp_path)
    monkeypatch.setattr(scene_audio, "_session_audio_dir",
                        lambda session_id="default", create=True: audio)
    path = scene_audio.resolve_audio_path("scene_abc.mp3")
    assert path is not None and path.name == "scene_abc.mp3"
    assert scene_audio.resolve_audio_path("../secrets.txt") is None
    assert scene_audio.resolve_audio_path("audio/../scene_abc.mp3") is None


def test_inspect_scene_says_what_would_play_and_why(monkeypatch, tmp_path):
    _no_run(monkeypatch)
    _music_dir(monkeypatch, tmp_path)
    scene_audio.set_music_direction("slow detuned piano")
    scene_audio.set_sfx_direction("quiet")
    rec = scene_audio.inspect_scene("a limestone cavern, stalactites dripping", mode="scene")
    assert rec["mode"] == "scene"
    assert rec["direction"] == "slow detuned piano"
    assert rec["sfx_direction"] == "quiet"
    assert rec["music_prompt"] and rec["sfx_prompt"]
    assert rec["music_url"].startswith(sound_library.URL_BASE)
    assert rec["ambience_id"] == "amb_cave"
    assert "cavern" in rec["ambience_matched"]
    assert rec["stinger_id"] is None
    enc = scene_audio.inspect_scene("a collapsing grate", mode="encounter")
    assert enc["stinger_id"] == "encounter_enter"
    assert enc["music_id"].startswith("mus_enc_")


def test_the_ambience_direction_steers_the_bed(monkeypatch, tmp_path):
    _no_run(monkeypatch)
    _music_dir(monkeypatch, tmp_path)
    assert scene_audio.inspect_scene("an unknown place")["ambience_id"] == "amb_room"
    scene_audio.set_sfx_direction("server racks and cooling fans")
    assert scene_audio.inspect_scene("an unknown place")["ambience_id"] == "amb_server_room"


def test_stock_status_includes_the_prompt():
    rec = scene_audio.stock_status()["encounter_enter"]
    assert rec["kind"] == "stinger"
    assert rec["prompt"]
    assert rec["loop"] is False


def test_list_and_clear_generated_cache(tmp_path, monkeypatch):
    audio = tmp_path / "audio"
    audio.mkdir()
    (audio / "scene_abc.mp3").write_bytes(b"ID3" + b"\x00" * 40)
    (audio / "amb_xyz.mp3").write_bytes(b"ID3" + b"\x00" * 40)
    (audio / "keep_me.txt").write_text("nope")
    monkeypatch.setattr(scene_audio, "_get_audio_dir", lambda session_id="default": audio)
    listed = scene_audio.list_generated_cache("default")
    names = {c["file"] for c in listed}
    assert "scene_abc.mp3" in names
    assert "amb_xyz.mp3" in names
    assert scene_audio.clear_generated_cache("default") == 2
    assert scene_audio.list_generated_cache("default") == []
    assert (audio / "keep_me.txt").exists()


def test_resolve_audio_path_finds_another_sessions_clip(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    other = sessions / "other" / "audio"
    other.mkdir(parents=True)
    (other / "scene_abc.mp3").write_bytes(b"ID3" + b"\x00" * 40)
    monkeypatch.setattr(scene_audio, "ROOT", tmp_path)
    monkeypatch.setattr(scene_audio, "MUSIC_DIR", tmp_path / "music")
    monkeypatch.setattr(
        scene_audio, "_session_audio_dir",
        lambda session_id="default", create=True: sessions / session_id / "audio",
    )
    path = scene_audio.resolve_audio_path("scene_abc.mp3", "default")
    assert path is not None and path.name == "scene_abc.mp3"


def test_resolve_audio_path_serves_editor_test_clips(tmp_path, monkeypatch):
    music = tmp_path / "music"
    music.mkdir()
    (music / "test_music.mp3").write_bytes(b"ID3" + b"\x00" * 40)
    monkeypatch.setattr(scene_audio, "MUSIC_DIR", music)
    path = scene_audio.resolve_audio_path("test_music.mp3")
    assert path is not None and path.name == "test_music.mp3"


def test_library_urls_carry_no_session(monkeypatch, tmp_path):
    """They are static files every session shares; only a clip generated into
    a session's own folder ever needed ?session= to be found."""
    _no_run(monkeypatch)
    _music_dir(monkeypatch, tmp_path)
    rec = scene_audio.get_scene_audio("a quiet hallway", session_id="abc")
    assert "session=" not in rec["audio_url"] and "session=" not in rec["sfx_url"]


def test_inspect_endpoint_returns_prompts():
    import api
    client = api.app.test_client()
    rec = client.post("/api/music/inspect", json={
        "prompt": "a quiet forest at dawn", "mode": "scene",
    })
    assert rec.status_code == 200
    data = rec.get_json()["data"]
    assert data.get("ambience_kind") in ("amb_forest", "amb_forest_day")
    assert data.get("sfx_prompt")
    assert data.get("music_prompt")
    assert data.get("mode") == "scene"


def test_health_and_scene_audio_say_the_library_is_playing(monkeypatch, tmp_path):
    import api
    _clear_mock(monkeypatch)
    _music_dir(monkeypatch, tmp_path)
    client = api.app.test_client()
    health = client.get("/api/health").get_json()
    assert health["music"]["can_generate"] is True
    assert health["music"]["reason"] == "ready"
    rec = client.post("/api/scene_audio", json={"prompt": "a quiet forest"})
    body = rec.get_json()
    assert body.get("audio_url", "").startswith(sound_library.URL_BASE)
    assert "reason" not in body


def test_mock_mode_plays_the_library_too(monkeypatch, tmp_path):
    """The mock gate existed so a "fully offline" run could not bill music on
    every scene. The library bills nothing and calls nothing, so an offline run
    has sound now."""
    _clear_mock(monkeypatch)
    _no_run(monkeypatch)
    _music_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("STORYGEN_BACKEND", "mock")
    assert scene_audio.is_available() is True
    rec = scene_audio.get_scene_audio("a quiet forest at dawn")
    assert rec["audio_url"].startswith(sound_library.URL_BASE)


def test_every_answer_is_immediate_and_never_pending(monkeypatch, tmp_path):
    _no_run(monkeypatch)
    _music_dir(monkeypatch, tmp_path)
    t0 = time.time()
    rec = scene_audio.get_scene_audio("a quiet forest at dawn")
    assert time.time() - t0 < 1.0
    assert rec["cached"] is True
    assert rec["pending_music"] is False and rec["pending_sfx"] is False
    for got in (scene_audio.action_foley("Vault the fence"),
                scene_audio.consequence_bed("The floor gives way beneath the crate")):
        assert got["pending"] is False and got["cached"] is True


def test_the_stock_box_has_nothing_to_generate():
    rec = scene_audio.stock_record("encounter_enter")
    assert rec["url"] and rec["cached"] is True
    assert scene_audio.stock_record("rain")["kind"] == "ambience"
    assert scene_audio.stock_record("no_such_thing") is None


def test_encounter_does_not_adopt_the_explore_loop(monkeypatch, tmp_path):
    _no_run(monkeypatch)
    music = _music_dir(monkeypatch, tmp_path)
    (music / "loop.wav").write_bytes(b"RIFF" + b"\x00" * 80)
    (music / "loop.json").write_text(
        '{"file": "loop.wav", "source": "generated", "prompt": "old", "created_at": 1}',
        encoding="utf-8")
    assert scene_audio.custom_loop() is not None
    scene = scene_audio.get_scene_audio("a quiet forest", mode="scene")
    assert scene and "loop.wav" in (scene.get("audio_url") or "")
    enc = scene_audio.get_scene_audio("hostile — person — Kane", mode="encounter")
    assert not enc or "loop.wav" not in (enc.get("audio_url") or "")
    assert not enc or enc.get("source") != "generated"


def test_encounter_designer_urls_ignore_json():
    urls = scene_audio.encounter_designer_urls()
    assert "cues" not in urls
    assert all(not str(k).endswith(".json") for k in urls)


def test_nothing_plays_an_audition_under_the_title():
    """Reading what is scoring the game must not commission new audio, and the
    title screen must not loop an audition.

    /api/music used to warm a 10-second sample off the menu direction text, and
    the title screen played it: writing "horror action score" in the editor and
    never locking anything put ten seconds of clanking metal on loop under the
    main menu — billed, unasked for, and with no control that turned it off.
    The title plays the LOCKED track, or the library's title theme (a track
    written as one), and never a preview.
    """
    src = Path(scene_audio.__file__).read_text(encoding="utf-8")
    assert "def kick_menu_preview" not in src
    api_src = (Path(scene_audio.__file__).parent / "api.py").read_text(encoding="utf-8")
    assert "kick_menu_preview" not in api_src
    client = (Path(scene_audio.__file__).parent
              / "static" / "js" / "standalone.js").read_text(encoding="utf-8")
    menu = client.split("async enterMenu()", 1)[1].split("leaveMenu()", 1)[0]
    assert "menu_loop" in menu
    assert "menu_library" in menu
    assert "menu_preview" not in menu
    assert "/api/music/preview" not in menu
    assert scene_audio.menu_library()["id"].startswith("mus_menu_")


def test_every_sound_prompt_fit_the_sound_models_limit():
    """The sound model rejected over 450 characters with a 400, not a
    truncation — an overlong prompt once made no ambience at all, silently.
    tools/build_sound_library refuses one; the catalog shows none got through."""
    for lane in ("ambience", "foley", "consequence"):
        for e in sound_library.entries(lane):
            assert len(e["prompt"]) <= 450, e["id"]


def test_two_different_scenes_get_two_different_beds():
    """The whole point of scene ambience. If every place matched the same
    bed, the world would sound like one room."""
    a = sound_library.pick("ambience", "a flooded pump house, water to the ankles", seed="s")
    b = sound_library.pick("ambience", "a dry scrapyard under a red mesa at dusk", seed="s")
    assert a["id"] != b["id"]
    assert a["id"] == sound_library.pick(
        "ambience", "a flooded pump house, water to the ankles", seed="s")["id"], "must be stable"


def test_scene_ambience_can_be_switched_off_to_the_generic_beds(monkeypatch, tmp_path):
    _no_run(monkeypatch)
    _music_dir(monkeypatch, tmp_path)
    on = scene_audio.get_scene_audio("server racks with blinking lights")["sfx_id"]
    monkeypatch.setattr(scene_audio, "scene_ambience_enabled", lambda: False)
    off = scene_audio.get_scene_audio("server racks with blinking lights")["sfx_id"]
    assert on == "amb_server_room"
    assert sound_library.get(off).get("generic") is True


def test_a_scene_with_no_audible_words_still_has_a_room():
    got = sound_library.pick("ambience", "a figure in a long coat, facing away", seed="s")
    assert got["id"] == "amb_room" and got.get("fallback") is True


def test_the_client_retries_while_either_layer_is_pending():
    """`pending_sfx && !sfx_url` was false whenever stock had been handed over,
    so the retry stopped and the scene's own loop was never fetched."""
    js = (Path(__file__).resolve().parent / "static" / "js" / "standalone.js").read_text(
        encoding="utf-8")
    assert "const waiting = !!(res.pending_music || res.pending_sfx);" in js
    assert "res.pending_sfx && !res.sfx_url" not in js


def test_ambience_is_mixed_louder_than_the_score():
    """It is the atmosphere now. At 0.55 of a 0.12 bed it played at 0.066 gain
    - running, and inaudible."""
    js = (Path(__file__).resolve().parent / "static" / "js" / "standalone.js").read_text(
        encoding="utf-8")
    sfx = float(re.search(r"const SFX_BED = ([\d.]+);", js).group(1))
    music = float(re.search(r"const MUSIC_BED = ([\d.]+);", js).group(1))
    assert sfx > music, "the place should lead, not the score"
    assert sfx >= 1.0, "ambience must not be attenuated below the volume preset"


# ───────────────────────── Action foley: the player's own sound ─────────────


def test_foley_is_chosen_by_the_choice_text_not_the_camera():
    """The earlier attempt fed the render prompt in - camera rig, film stock,
    "the back of the head toward the lens" - and got mush, because none of
    that describes a sound. The choice text is four concrete words."""
    assert scene_audio.action_foley("Sprint toward the utility truck")["id"].startswith("run_")
    # a slate line that names nothing audible gets the quiet fallback, not a guess
    vague = scene_audio.action_foley("Give the silhouette what they want")
    assert sound_library.get(vague["id"]).get("fallback") is True


def test_the_slate_numbering_is_not_part_of_the_sound():
    assert scene_audio._clean_action("1. Kick open the truck door.") == \
        "Kick open the truck door"
    assert scene_audio._clean_action("  3)  Vault the fence  ") == "Vault the fence"


def test_each_action_gets_its_own_clip():
    a = scene_audio.action_foley("Sprint toward the utility truck")["id"]
    b = scene_audio.action_foley("Vault over the chain link fence")["id"]
    assert a != b
    assert a == scene_audio.action_foley("Sprint toward the utility truck")["id"]


def test_foley_is_short_and_not_a_loop():
    for e in sound_library.entries("foley"):
        assert e["seconds"] <= 3.5, e["id"]
        assert e["loop"] is False, e["id"]


def test_foley_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(scene_audio, "action_foley_enabled", lambda: False)
    assert scene_audio.action_foley("Vault the fence", session_id="t") is None


def test_an_empty_or_junk_action_makes_no_sound():
    for junk in ("", "   ", "1.", "a"):
        assert scene_audio.action_foley(junk, session_id="t") is None


# ─────────────── Consequence bed: the sound the flipbook plays over ─────────


def test_the_bed_is_chosen_by_what_happened():
    rec = scene_audio.consequence_bed(
        "The hinges tear out of the frame and the door swings wide into the dark.")
    assert rec["id"] == "csq_door_breach"


def test_long_consequence_prose_still_chooses_quickly():
    """A consequence is prose and can run for paragraphs; the pick reads all of
    it and must not hold the turn up."""
    long_text = "The gantry gives way and " + "steel screams against steel " * 40
    t0 = time.time()
    rec = scene_audio.consequence_bed(long_text)
    assert time.time() - t0 < 0.5
    assert rec["id"] == "csq_structural_collapse"


def test_the_consequence_sound_is_a_one_shot_not_a_loop():
    """It looped once, holding until the next action was committed. Played
    that way a gesture repeats under someone who is still reading, and
    repetition is exactly what stops a sound reading as the world answering.
    It plays through and stops; the scene ambience underneath is the lane that
    is built to loop."""
    for e in sound_library.entries("consequence"):
        assert e["loop"] is False, e["id"]
    # and the client must not loop the node or re-fire it once it has played
    js = (Path(__file__).resolve().parent / "static" / "js" / "standalone.js").read_text(
        encoding="utf-8")
    start = js.split("function startBeat(buf, key) {", 1)[1].split("\n    }", 1)[0]
    assert "s.loop = false;" in start
    assert "loopable(" not in start, "seam-blending is for a loop point it no longer has"
    assert "s.onended" in start, "a finished one-shot has to release the node"
    assert "beatPlayed" in js


def test_a_fragment_makes_no_bed():
    """Error strings and one-word beats are not a scene to choose a sound by."""
    for junk in ("", "   ", "ok", "1.", "He runs."):
        assert scene_audio.consequence_bed(junk, session_id="t") is None


def test_consequence_bed_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(scene_audio, "consequence_bed_enabled", lambda: False)
    assert scene_audio.consequence_bed(
        "The hinges tear out of the frame and it swings wide", session_id="t") is None


def test_the_bed_is_armed_on_the_consequence_not_on_the_picture():
    """The whole feature rests on this ordering: the consequence lands five
    pipeline steps before guide_image, so the clip is generated during a wait
    the player is already having. Arm it on the picture instead and it shows
    up after the flipbook it was supposed to play under."""
    js = (Path(__file__).resolve().parent / "static" / "js" / "standalone.js").read_text(
        encoding="utf-8")
    assert "SceneAudio.armConsequence(" in js
    armed_at = js.index("SceneAudio.armConsequence(")
    assert 'Ceremony.reach("consequence")' in js[armed_at - 1600:armed_at]
    # There is no consequence_event feed type - the dispatch prose arrives as a
    # narrative_event. Gating on a type the engine never emits is silent death.
    engine_src = (Path(__file__).resolve().parent / "engine.py").read_text(encoding="utf-8")
    assert "consequence_event" not in engine_src
    assert 'item.type === "consequence_event"' not in js
    # started by the frames, dropped by the next commit
    assert "SceneAudio.playConsequence(key)" in js
    assert "SceneAudio.endConsequence()" in js
    play = js.split("function playSceneSequence(", 1)[1].split("\n  }", 1)[0]
    assert "playConsequence" in play


def test_the_bed_opens_when_it_is_READY_not_when_the_picture_lands():
    """guide_image alone is 20-40s and the ceremony has six short blips to fill
    it with, so holding the bed for the frames left the longest silence in the
    turn exactly where the player is doing nothing but wait. It is armed on the
    prose and opened the moment the clip exists."""
    js = (Path(__file__).resolve().parent / "static" / "js" / "standalone.js").read_text(
        encoding="utf-8")
    arm = js.split("armConsequence(text) {", 1)[1].split("\n      },", 1)[0]
    assert "openBeat(url" in arm, "arming must open the bed itself"
    # and the frames must not restart what is already running
    play = js.split("async playConsequence(key) {", 1)[1].split("\n      },", 1)[0]
    assert "if (beatSrc) {" in play


def test_the_scene_bed_is_scored_from_the_frame_that_rendered():
    """Measured: scoring off metadata.base sent "seamless looping environmental
    ambience of s grit across the pad. The place is the Four Corners fence..."
    for a desert well pad - the pump jack, the shed and the standing water
    sliced out, because build_realtime_base puts the style anchor first and the
    place line last. The bed is scored from vision's read of the rendered frame
    now."""
    js = (Path(__file__).resolve().parent / "static" / "js" / "standalone.js").read_text(
        encoding="utf-8")
    api_src = (Path(__file__).resolve().parent / "api.py").read_text(encoding="utf-8")
    engine_src = (Path(__file__).resolve().parent / "engine.py").read_text(encoding="utf-8")
    assert '"current_vision": s.get("current_vision", "")' in api_src
    assert 'state["current_vision"] = vision_analysis_text' in engine_src
    assert "s.current_vision" in js
    # and the render base must no longer score anything
    restage = js.split("state.lastScenePrompt = scenePrompt;", 1)[1][:600]
    assert "SceneAudio.score(scenePrompt)" not in js
    assert "does NOT score" in restage


def test_a_desert_well_pad_sounds_like_one():
    """The regression the frame read fixed, in library terms: the frame's own
    words put a pump jack on the pad, not a room tone indoors."""
    visual = ("A rusted pump jack squats in the foreground, chain slapping "
              "against its counterweight. Beyond it a collapsed equipment shed, "
              "corrugated sheeting peeled back, standing water in the ruts. "
              "Wind moves grit across the pad.")
    got = sound_library.pick("ambience", visual, seed="s")
    assert got["id"] == "amb_oilfield", got["id"]
    assert not got.get("fallback")


def test_the_consequence_bed_takes_the_visual_scene_not_the_prose():
    """It is armed before the picture exists, so it cannot use vision. The
    visual_scene caption is the same kind of text and does exist by then."""
    js = (Path(__file__).resolve().parent / "static" / "js" / "standalone.js").read_text(
        encoding="utf-8")
    engine_src = (Path(__file__).resolve().parent / "engine.py").read_text(encoding="utf-8")
    assert '"visual": vision_dispatch_text' in engine_src
    assert "SceneAudio.armConsequence((meta.visual || \"\").trim() || item.content)" in js


def test_the_opening_of_a_run_is_not_a_consequence():
    """Boot resolves a turn too, so awaitingResolution is true and the run's
    scene-setting prose reaches the same branch the consequence does. Measured:
    turn one armed a bed from "1993. You are Jason Fleece, investigative
    photojournalist" - a setting dump with no sound in it. A bed answers
    something the PLAYER did, so it needs a commit first."""
    js = (Path(__file__).resolve().parent / "static" / "js" / "standalone.js").read_text(
        encoding="utf-8")
    arm = js.split("armConsequence(text) {", 1)[1].split("},", 1)[0]
    assert "if (!beatTurnLive) return;" in arm
    assert "if (beatArmed) return;" in arm, "one bed per turn, not one per prose beat"
    end = js.split("endConsequence() {", 1)[1].split("},", 1)[0]
    assert "beatTurnLive = true" in end
    assert "beatTurnLive = false" in js.split("function abandonBeat(", 1)[1][:300]


def test_the_bed_is_its_own_layer_and_leads_the_room():
    """It is the event the player just caused; the ambience is the room that
    was already there. Under foley, which answers their hand on the button."""
    js = (Path(__file__).resolve().parent / "static" / "js" / "standalone.js").read_text(
        encoding="utf-8")
    beat = float(re.search(r"const BEAT_BED = ([\d.]+);", js).group(1))
    sfx = float(re.search(r"const SFX_BED = ([\d.]+);", js).group(1))
    foley = float(re.search(r"const FOLEY_LEVEL = ([\d.]+);", js).group(1))
    assert sfx < beat < foley
    # a stale generation must never open over the next turn
    assert "function abandonBeat(" in js
    assert "mine !== beatSeq" in js


def test_the_client_prewarms_the_slate_and_plays_on_commit():
    """A foley that lands eight seconds after the button is worse than none,
    so every choice is asked for when the slate renders and the click is a
    cache hit. makeChoice is the single funnel - choices, SCAN MOVE/INTERACT,
    typed actions and camp all pass through it."""
    js = (Path(__file__).resolve().parent / "static" / "js" / "standalone.js").read_text(
        encoding="utf-8")
    assert "SceneAudio.prewarmFoley(promptItem.choices.map" in js
    body = js.split("async function makeChoice(", 1)[1][:2000]
    assert "SceneAudio.foley(choiceText)" in body


def test_generated_audio_is_loudness_normalised():
    """Measured before this existed: a scene bed at RMS 0.007, about -43 dBFS.
    Running, and inaudible. The sound model did not normalise its output, so a
    fixed gain is always wrong for something. The library is normalised when it
    is built; the client still measures every buffer, so a loop someone
    uploads is levelled too."""
    js = (Path(__file__).resolve().parent / "static" / "js" / "standalone.js").read_text(
        encoding="utf-8")
    assert "function normalizeGain(buf)" in js
    target = float(re.search(r"const TARGET_RMS = ([\d.]+);", js).group(1))
    assert 0.05 <= target <= 0.25, "ambience wants roughly -22 dBFS"
    # every playback path must use it
    assert "normalizeGain(looped.buf || buf)" in js   # the beds
    assert "mult * normalizeGain(buf)" in js          # foley


def test_normalisation_uses_peak_as_well_as_loudness():
    """Measured off a real generated foley clip: rms 0.006, peak 0.060 - a raw
    peak of -24 dBFS. The source is that quiet, so the boost needed is large,
    and a peak term is what makes a large boost safe: it cannot by construction
    push the clip into clipping."""
    js = (Path(__file__).resolve().parent / "static" / "js" / "standalone.js").read_text(
        encoding="utf-8")
    assert "const PEAK_CEIL" in js
    assert "Math.min(TARGET_RMS / rms, PEAK_CEIL / peak)" in js
    ceiling = float(re.search(r"const GAIN_MIN = [\d.]+, GAIN_MAX = ([\d.]+);", js).group(1))
    assert ceiling >= 20, ("a 12x ceiling was itself keeping foley inaudible - "
                           "the clip needed ~15x")


def test_the_foley_cap_does_not_undo_the_normalisation():
    """The cap belongs on the multiplier applied to an ALREADY normalised clip.
    Clamping the gain itself turned a 14.9x boost into 0.85x and the hit came
    out quieter than the bed under it - measured peak 0.0145 against 0.169
    once the cap moved."""
    js = (Path(__file__).resolve().parent / "static" / "js" / "standalone.js").read_text(
        encoding="utf-8")
    body = js.split("function playFoley(", 1)[1][:1400]
    assert "const mult = Math.min(1, musicVol * FOLEY_LEVEL * duck);" in body
    assert "const target = mult * normalizeGain(buf);" in body
    # the old shape must not come back
    assert "FOLEY_MAX" not in js
