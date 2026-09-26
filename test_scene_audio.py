"""Offline tests for scene music + world sound (no network).

Nothing is generated today (see scene_audio.is_available); these hold the
prompt builders, the lanes' contracts through the generator seam, and every
path that plays a file already on disk."""
from __future__ import annotations

import re
from pathlib import Path

import scene_audio


def test_flatten_music_prompt_is_instrumental():
    text = scene_audio.flatten_music_prompt(
        "a collapsing grate and a miner with a pipe",
        mode="encounter",
        direction="",
    ).lower()
    assert "instrumental" in text or "no vocal" in text
    assert "tense" in text or "danger" in text or "confrontation" in text


def test_authored_direction_survives_flatten():
    text = scene_audio.flatten_music_prompt(
        "a sunlit kitchen", mode="scene",
        direction="slow detuned piano, tape hiss, no drums",
    )
    assert "detuned piano" in text
    assert "sunlit kitchen" in text


def test_ambience_kind_reads_the_place():
    assert scene_audio._ambience_kind("rain on a concrete roof", "scene") == "rain"
    assert scene_audio._ambience_kind("a flooded cave tunnel", "scene") == "cave"
    assert scene_audio._ambience_kind("neon street market", "scene") == "urban"
    assert scene_audio._ambience_kind("a quiet kitchen", "conversation") == "room"
    assert scene_audio._ambience_kind("an unknown place", "encounter") == "industrial"


def test_sfx_prompt_is_foley_not_a_score():
    prompt = scene_audio._scene_to_sfx_prompt("rain on a concrete roof", "scene")
    low = prompt.lower()
    assert "loop" in low
    assert "rain" in low
    assert "no music" in low or "no melody" in low


def test_stock_catalog_covers_encounter_hits():
    for key in ("encounter_enter", "encounter_lock", "encounter_resolve",
                "encounter_exit", "encounter_hitch", "encounter_die",
                "encounter_title", "encounter_survive"):
        spec = scene_audio.STOCK_STINGERS[key]
        assert spec["file"].startswith("sting_")
        assert spec["file"].endswith(".mp3")
        assert spec["seconds"] <= 4
        assert spec["loop"] is False
    for key in ("industrial", "rain", "cave", "wind", "room", "urban", "forest"):
        spec = scene_audio.STOCK_AMBIENCE[key]
        assert spec["loop"] is True
        assert spec["seconds"] >= 8


def test_cache_names_split_music_and_sfx():
    music = scene_audio._cache_name("a quiet hallway", 20, mode="scene")
    sfx = scene_audio._sfx_cache_name("a quiet hallway", 14, mode="scene")
    assert music.startswith("scene_") and music.endswith(".mp3")
    assert sfx.startswith("amb_") and sfx.endswith(".mp3")
    assert music != sfx
    enc = scene_audio._cache_name("a quiet hallway", 20, mode="encounter")
    assert enc.startswith("enc_")
    assert enc != music


def _clear_mock(monkeypatch):
    monkeypatch.delenv("MOCK_MODE", raising=False)
    monkeypatch.delenv("STORYGEN_BACKEND", raising=False)
    try:
        import keys_store
        monkeypatch.setattr(keys_store, "_explicit_mock", False)
    except Exception:
        pass


def test_nothing_generates_on_either_key(monkeypatch):
    """The game runs on one key, Gemini or OpenAI, and neither makes sound
    effects. A key being present must not make the module claim it can; the
    reason it gives is shown to the player, so it has to be the true one."""
    _clear_mock(monkeypatch)
    for env in ("GEMINI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(env, "a-perfectly-good-key-1234")
    assert scene_audio.is_available() is False
    why = scene_audio.unavailable_reason()
    assert why == scene_audio.NO_GENERATOR_REASON
    assert "sound generator" in why


def test_every_generator_answers_none(monkeypatch, tmp_path):
    """The seam is where Lyria plugs in. Until it does, nothing reaches the
    disk and nothing claims to be pending, whichever lane asks."""
    _clear_mock(monkeypatch)
    monkeypatch.setattr(scene_audio, "MUSIC_DIR", tmp_path)
    monkeypatch.setattr(scene_audio, "STOCK_DIR", tmp_path / "stock")
    assert scene_audio._generate_music("a slow drone", 8, mode="scene") is None
    assert scene_audio._generate_sfx("rain on a roof", 8, loop=True) is None
    assert scene_audio.generate_preview("a slow drone") is None
    assert scene_audio.generate_loop("a slow drone") is None
    assert scene_audio.generate_test_clip("a quiet yard", layer="music") is None
    assert scene_audio.generate_test_clip("a quiet yard", layer="sfx") is None
    stinger = scene_audio.generate_test_clip("", layer="stinger")
    assert stinger["url"] is None and stinger["error"] == "no_key"
    assert stinger["reason"] == scene_audio.NO_GENERATOR_REASON
    assert scene_audio.action_foley("Vault the fence", session_id="t") is None
    assert scene_audio.consequence_bed(
        "The hinges tear out of the frame and it swings wide", session_id="t") is None
    assert list(tmp_path.iterdir()) == []


def test_the_seam_is_what_the_music_lanes_call(monkeypatch, tmp_path):
    """One place for a generator to go: the preview and the locked loop both
    come back the moment _generate_music answers."""
    monkeypatch.setattr(scene_audio, "MUSIC_DIR", tmp_path)
    monkeypatch.setattr(scene_audio, "_LOOP_META", tmp_path / "loop.json")
    monkeypatch.setattr(scene_audio, "is_available", lambda: True)
    seen = []
    monkeypatch.setattr(scene_audio, "_generate_music",
                        lambda prompt, secs, mode="scene", session_id="default": (
                            seen.append(mode) or b"ID3" + b"\x00" * 80))
    assert scene_audio.generate_preview("a slow drone")["file"] == "preview.mp3"
    assert scene_audio.generate_loop("a slow drone")["source"] == "generated"
    assert seen == ["verbatim", "verbatim"]


def test_no_sound_provider_is_called_from_here():
    """The third provider is gone, key and wire both. A request library or an
    API host creeping back into this module would be a second key again."""
    src = Path(scene_audio.__file__).read_text(encoding="utf-8")
    low = src.lower()
    assert "import requests" not in src
    assert "api.elevenlabs.io" not in low
    assert "xi-api-key" not in low
    assert "elevenlabs_api_key" not in low


def test_get_scene_audio_is_none_with_nothing_on_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(scene_audio, "STOCK_DIR", tmp_path / "stock")
    monkeypatch.setattr(scene_audio, "MUSIC_DIR", tmp_path)
    monkeypatch.setattr(scene_audio, "_LOOP_META", tmp_path / "loop.json")
    (tmp_path / "stock").mkdir()
    assert scene_audio.get_scene_audio("a quiet forest at dawn") is None


def test_encounter_uses_stock_stinger_when_present(monkeypatch, tmp_path):
    stock = tmp_path / "stock"
    stock.mkdir()
    audio = tmp_path / "audio"
    audio.mkdir()
    monkeypatch.setattr(scene_audio, "STOCK_DIR", stock)
    monkeypatch.setattr(scene_audio, "MUSIC_DIR", tmp_path)
    monkeypatch.setattr(scene_audio, "_LOOP_META", tmp_path / "loop.json")
    monkeypatch.setattr(scene_audio, "_session_audio_dir",
                        lambda session_id="default", create=True: audio)
    monkeypatch.setattr(scene_audio, "_get_audio_dir", lambda session_id="default": audio)
    sting = stock / "sting_encounter_enter.mp3"
    sting.write_bytes(b"ID3" + b"\x00" * 80)
    amb = stock / "amb_industrial.mp3"
    amb.write_bytes(b"ID3" + b"\x00" * 80)
    rec = scene_audio.get_scene_audio("a collapsing grate", mode="encounter")
    assert rec is not None
    assert rec["audio_url"] is None
    assert rec["stinger_url"] and "sting_encounter_enter.mp3" in rec["stinger_url"]
    assert rec["sfx_url"] and "amb_industrial.mp3" in rec["sfx_url"]


def test_resolve_audio_path_serves_stock_and_rejects_traversal(tmp_path, monkeypatch):
    stock = tmp_path / "stock"
    stock.mkdir()
    (stock / "sting_encounter_enter.mp3").write_bytes(b"ID3" + b"\x00" * 80)
    monkeypatch.setattr(scene_audio, "STOCK_DIR", stock)
    monkeypatch.setattr(scene_audio, "MUSIC_DIR", tmp_path)
    path = scene_audio.resolve_audio_path("sting_encounter_enter.mp3")
    assert path is not None and path.name == "sting_encounter_enter.mp3"
    assert scene_audio.resolve_audio_path("../secrets.txt") is None
    assert scene_audio.resolve_audio_path("stock/../secrets.txt") is None


def test_inspect_scene_exposes_both_prompts(monkeypatch, tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    monkeypatch.setattr(scene_audio, "MUSIC_DIR", music)
    monkeypatch.setattr(scene_audio, "_DIRECTION_PATH", music / "direction.json")
    monkeypatch.setattr(scene_audio, "_SFX_DIRECTION_PATH", music / "sfx_direction.json")
    scene_audio.set_music_direction("slow detuned piano")
    scene_audio.set_sfx_direction("wet steam pipes")
    rec = scene_audio.inspect_scene("a flooded cave tunnel", mode="scene")
    assert rec["mode"] == "scene"
    assert "detuned piano" in rec["music_prompt"]
    assert "wet steam pipes" in rec["sfx_prompt"]
    assert rec["ambience_kind"] == "cave"
    assert rec["stinger_id"] is None
    enc = scene_audio.inspect_scene("a collapsing grate", mode="encounter")
    assert enc["stinger_id"] == "encounter_enter"
    assert "tense" in enc["music_prompt"].lower() or "danger" in enc["music_prompt"].lower()


def test_sfx_direction_is_mixed_into_the_foley_prompt(monkeypatch, tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    monkeypatch.setattr(scene_audio, "_SFX_DIRECTION_PATH", music / "sfx_direction.json")
    empty = scene_audio._scene_to_sfx_prompt("rain on concrete", "scene", direction="")
    mixed = scene_audio._scene_to_sfx_prompt(
        "rain on concrete", "scene", direction="close-mic dripping pipes")
    assert "close-mic dripping pipes" in mixed
    assert "rain" in mixed.lower()
    assert "close-mic dripping pipes" not in empty


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
    monkeypatch.setattr(scene_audio, "STOCK_DIR", tmp_path / "stock")
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
    monkeypatch.setattr(scene_audio, "STOCK_DIR", music / "stock")
    path = scene_audio.resolve_audio_path("test_music.mp3")
    assert path is not None and path.name == "test_music.mp3"


def test_sessionize_url_skips_stock_and_stamps_generated():
    assert scene_audio._sessionize_url("/audio/amb_rain.mp3", "abc") == "/audio/amb_rain.mp3"
    assert scene_audio._sessionize_url("/audio/sting_encounter_enter.mp3", "abc") == (
        "/audio/sting_encounter_enter.mp3")
    stamped = scene_audio._sessionize_url("/audio/scene_0123456789abcdef.mp3", "abc")
    assert stamped.endswith("session=abc")
    assert scene_audio._sessionize_url("/audio/scene_x.mp3", "default") == "/audio/scene_x.mp3"


def test_inspect_endpoint_returns_prompts():
    import api
    client = api.app.test_client()
    rec = client.post("/api/music/inspect", json={
        "prompt": "a quiet forest at dawn", "mode": "scene",
    })
    assert rec.status_code == 200
    data = rec.get_json()["data"]
    assert "forest" in (data.get("music_prompt") or "").lower() or data.get("ambience_kind") == "forest"
    assert data.get("sfx_prompt")
    assert data.get("mode") == "scene"


def test_health_and_scene_audio_say_why_there_is_no_sound(monkeypatch, tmp_path):
    import api
    _clear_mock(monkeypatch)
    monkeypatch.setattr(scene_audio, "STOCK_DIR", tmp_path / "stock")
    monkeypatch.setattr(scene_audio, "MUSIC_DIR", tmp_path)
    monkeypatch.setattr(scene_audio, "_LOOP_META", tmp_path / "loop.json")
    client = api.app.test_client()
    health = client.get("/api/health").get_json()
    assert "music" in health
    assert health["music"]["can_generate"] is False
    assert scene_audio.NO_GENERATOR_REASON in (health["music"].get("reason") or "")
    rec = client.post("/api/scene_audio", json={"prompt": "a quiet forest"})
    body = rec.get_json()
    assert body.get("audio_url") is None
    assert scene_audio.NO_GENERATOR_REASON in (body.get("reason") or "")


def test_mock_mode_does_not_generate(monkeypatch, tmp_path):
    _clear_mock(monkeypatch)
    monkeypatch.setenv("STORYGEN_BACKEND", "mock")
    monkeypatch.setattr(scene_audio, "STOCK_DIR", tmp_path / "stock")
    monkeypatch.setattr(scene_audio, "MUSIC_DIR", tmp_path)
    monkeypatch.setattr(scene_audio, "_LOOP_META", tmp_path / "loop.json")
    (tmp_path / "stock").mkdir()
    called = []
    monkeypatch.setattr(scene_audio, "_generate_music",
                        lambda *a, **k: called.append("music") or b"x")
    monkeypatch.setattr(scene_audio, "_generate_sfx",
                        lambda *a, **k: called.append("sfx") or b"x")
    assert scene_audio.is_available() is False
    assert "offline mock" in scene_audio.unavailable_reason()
    rec = scene_audio.get_scene_audio("a quiet forest at dawn")
    assert rec is None or rec.get("audio_url") is None
    assert rec is None or rec.get("pending_music") is not True
    assert called == []


def test_uncached_music_returns_immediately_and_marks_pending(monkeypatch, tmp_path):
    import threading
    import time
    _clear_mock(monkeypatch)
    # A generator that works (the day Lyria lands): the first scene must still
    # not wait on it.
    monkeypatch.setattr(scene_audio, "is_available", lambda: True)
    audio = tmp_path / "audio"
    stock = tmp_path / "stock"
    audio.mkdir()
    stock.mkdir()
    (stock / "amb_forest.mp3").write_bytes(b"ID3" + b"\x00" * 80)
    monkeypatch.setattr(scene_audio, "STOCK_DIR", stock)
    monkeypatch.setattr(scene_audio, "MUSIC_DIR", tmp_path)
    monkeypatch.setattr(scene_audio, "_LOOP_META", tmp_path / "loop.json")
    monkeypatch.setattr(scene_audio, "_session_audio_dir",
                        lambda session_id="default", create=True: audio)
    monkeypatch.setattr(scene_audio, "_get_audio_dir", lambda session_id="default": audio)
    started = threading.Event()
    release = threading.Event()

    def slow_music(*a, **k):
        started.set()
        release.wait(2)
        return b"ID3" + b"\x00" * 80

    monkeypatch.setattr(scene_audio, "_generate_music", slow_music)
    monkeypatch.setattr(scene_audio, "_generate_sfx",
                        lambda *a, **k: b"ID3" + b"\x00" * 80)
    t0 = time.time()
    rec = scene_audio.get_scene_audio("a quiet forest at dawn")
    elapsed = time.time() - t0
    assert elapsed < 1.0
    assert rec is not None
    assert rec["audio_url"] is None
    assert rec.get("pending_music") is True
    assert rec.get("sfx_url")
    started.wait(1)
    release.set()


def test_ensure_stock_without_a_generator_reports_missing(monkeypatch, tmp_path):
    _clear_mock(monkeypatch)
    monkeypatch.setattr(scene_audio, "STOCK_DIR", tmp_path)
    rec = scene_audio.ensure_stock_sounds()
    assert rec["ok"] is False
    assert rec["reason"] == "no_key"   # the token api.py maps to "unavailable"
    assert rec["why"] == scene_audio.NO_GENERATOR_REASON
    assert rec["files"]["encounter_enter"]["ready"] is False


def test_encounter_does_not_adopt_the_explore_loop(monkeypatch, tmp_path):
    music = tmp_path / "music"
    stock = music / "stock"
    music.mkdir()
    stock.mkdir()
    (music / "loop.wav").write_bytes(b"RIFF" + b"\x00" * 80)
    (music / "loop.json").write_text(
        '{"file": "loop.wav", "source": "generated", "prompt": "old", "created_at": 1}',
        encoding="utf-8")
    monkeypatch.setattr(scene_audio, "MUSIC_DIR", music)
    monkeypatch.setattr(scene_audio, "STOCK_DIR", stock)
    monkeypatch.setattr(scene_audio, "_LOOP_META", music / "loop.json")
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


def test_nothing_generates_a_title_bed_behind_your_back():
    """Reading what is scoring the game must not commission new audio.

    /api/music used to warm a 10-second sample off the menu direction text, and
    the title screen played it. Writing "horror action score" in the editor and
    never locking anything therefore put ten seconds of clanking metal on loop
    under the main menu — billed, unasked for, and with no control that turned
    it off. The title bed is the LOCKED track now, so there is nothing for a GET
    to warm.
    """
    src = Path(scene_audio.__file__).read_text(encoding="utf-8")
    assert "def kick_menu_preview" not in src
    api_src = (Path(scene_audio.__file__).parent / "api.py").read_text(encoding="utf-8")
    assert "kick_menu_preview" not in api_src
    client = (Path(scene_audio.__file__).parent
              / "static" / "js" / "standalone.js").read_text(encoding="utf-8")
    menu = client.split("async enterMenu()", 1)[1].split("leaveMenu()", 1)[0]
    assert "menu_loop" in menu
    assert "menu_preview" not in menu
    assert "/api/music/preview" not in menu


def test_the_bed_lane_respects_the_api_text_limit():
    """The sound model this was built against rejected over 450 characters with
    a 400, not a truncation, so an overlong scene descriptor made no ambience
    at all - silently. Both SFX lanes used to clip at 500."""
    long_scene = "a dripping flooded corridor. " * 100
    assert len(scene_audio._scene_to_sfx_prompt(long_scene, "scene")) \
        <= scene_audio.SFX_TEXT_MAX
    assert scene_audio.SFX_TEXT_MAX == 450

def test_two_different_scenes_get_two_different_loops():
    """The whole point of scene ambience. If the cache key collapsed, every
    location would share one loop and the world would sound like one room."""
    a = scene_audio._sfx_cache_name("a flooded pump house, water to the ankles", 14)
    b = scene_audio._sfx_cache_name("a dry scrapyard under a red mesa at dusk", 14)
    assert a != b
    assert a == scene_audio._sfx_cache_name(
        "a flooded pump house, water to the ankles", 14), "must be stable"


def test_the_loop_prompt_names_the_scene_and_asks_to_tile():
    prompt = scene_audio._scene_to_sfx_prompt(
        "a flooded pump house, water to the ankles", "scene").lower()
    assert "flooded pump house" in prompt
    assert "loop" in prompt
    assert "no music" in prompt or "no melody" in prompt


def test_scene_ambience_can_be_switched_off_to_stock(monkeypatch):
    monkeypatch.setattr(scene_audio, "scene_ambience_enabled", lambda: False)
    monkeypatch.setattr(scene_audio, "is_available", lambda: True)
    monkeypatch.setattr(scene_audio, "stock_ambience_url", lambda kind: "/audio/stock.wav")
    monkeypatch.setattr(scene_audio, "_kick",
                        lambda key, fn: (_ for _ in ()).throw(
                            AssertionError("generated a scene loop while off")))
    url, cached, pending = scene_audio._resolve_sfx("a yard", "t", 14, "scene")
    assert url == "/audio/stock.wav"
    assert pending is False


def test_a_missing_scene_loop_is_reported_pending_so_the_client_comes_back():
    """Stock plays immediately so there is something to hear, and the scene's
    own loop generates behind it. The client MUST keep asking or it never
    arrives - which is exactly the bug that made every place sound alike."""
    import types
    kicked = []
    real_exists = scene_audio.Path.exists
    url, cached, pending = None, None, None
    import unittest.mock as m
    with m.patch.object(scene_audio, "is_available", lambda: True), \
         m.patch.object(scene_audio, "scene_ambience_enabled", lambda: True), \
         m.patch.object(scene_audio, "stock_ambience_url", lambda kind: "/audio/stock.wav"), \
         m.patch.object(scene_audio, "_kick", lambda key, fn: kicked.append(key)):
        url, cached, pending = scene_audio._resolve_sfx(
            "a never-before-seen place %s" % id(kicked), "t", 14, "scene")
    assert pending is True, "a miss must be pending so the client retries"
    assert len(kicked) == 1, "and must be generating in the background"


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


def test_foley_prompt_is_short_and_concrete():
    """The earlier attempt fed the render prompt in - camera rig, film stock,
    "the back of the head toward the lens" - and got mush, because none of
    that describes a sound. The choice text is four concrete words."""
    p = scene_audio._action_foley_prompt("Sprint toward the utility truck")
    assert "Sprint toward the utility truck" in p
    assert len(p) <= scene_audio.SFX_TEXT_MAX
    low = p.lower()
    assert "foley" in low
    assert "no music" in low and "not a loop" in low
    for camera_noise in ("follow-cam", "film stock", "lens", "composition"):
        assert camera_noise not in low


def test_the_slate_numbering_is_not_part_of_the_sound():
    assert scene_audio._clean_action("1. Kick open the truck door.") == \
        "Kick open the truck door"
    assert scene_audio._clean_action("  3)  Vault the fence  ") == "Vault the fence"


def test_each_action_gets_its_own_clip():
    a = scene_audio._foley_cache_name("Sprint toward the utility truck")
    b = scene_audio._foley_cache_name("Vault over the chain link fence")
    assert a != b
    assert a == scene_audio._foley_cache_name("Sprint toward the utility truck")
    assert a.startswith("foley_")


def test_foley_is_short():
    assert 0.5 <= scene_audio.ACTION_FOLEY_SECONDS <= 4.0


def test_foley_is_not_generated_as_a_loop(monkeypatch):
    seen = {}
    monkeypatch.setattr(scene_audio, "is_available", lambda: True)
    monkeypatch.setattr(scene_audio, "_generate_sfx",
                        lambda prompt, secs, loop=True, session_id="d": (
                            seen.update(loop=loop, secs=secs, prompt=prompt) or b"x"))
    monkeypatch.setattr(scene_audio, "_kick", lambda key, fn: fn())
    scene_audio.action_foley("Kick open the rusted door", session_id="foleytest")
    assert seen["loop"] is False
    assert seen["secs"] == scene_audio.ACTION_FOLEY_SECONDS


def test_foley_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(scene_audio, "action_foley_enabled", lambda: False)
    assert scene_audio.action_foley("Vault the fence", session_id="t") is None


def test_an_empty_or_junk_action_makes_no_sound(monkeypatch):
    monkeypatch.setattr(scene_audio, "is_available", lambda: True)
    for junk in ("", "   ", "1.", "a"):
        assert scene_audio.action_foley(junk, session_id="t") is None


# ─────────────── Consequence bed: the sound the flipbook plays over ─────────


def test_consequence_bed_prompt_leads_with_what_happened():
    """Same lesson foley learned: the concrete thing first, instruction after.
    The outcome is what the frames are drawing, so it is what the bed is of."""
    p = scene_audio._consequence_bed_prompt(
        "The hinges tear out of the frame and the door swings wide into the dark.")
    assert p.startswith("The hinges tear out of the frame")
    assert len(p) <= scene_audio.SFX_TEXT_MAX
    low = p.lower()
    assert "not a loop" in low
    assert "no music" in low and "no voice" in low


def test_long_consequence_prose_is_cut_at_a_word():
    """A consequence is prose and can run for paragraphs. Handing all of it to
    a sound model buys nothing and blows the 450-character API limit."""
    long_text = "The gantry gives way and " + "steel screams against steel " * 40
    what = scene_audio._clean_consequence(long_text)
    assert len(what) <= scene_audio.CONSEQUENCE_TEXT_MAX
    assert not what.endswith(" ")
    assert what in long_text, "truncation must not invent words"
    assert len(scene_audio._consequence_bed_prompt(long_text)) <= scene_audio.SFX_TEXT_MAX


def test_the_consequence_sound_is_a_one_shot_not_a_loop():
    """It looped once, holding until the next action was committed. Played
    that way an 18-second gesture repeats under someone who is still reading,
    and repetition is exactly what stops a sound reading as the world
    answering. It plays through and stops; the scene ambience underneath is
    the lane that is built to loop."""
    seen = {}
    monkey = scene_audio
    orig_avail, orig_sfx, orig_kick = (
        monkey.is_available, monkey._generate_sfx, monkey._kick)
    try:
        monkey.is_available = lambda: True
        monkey._generate_sfx = lambda prompt, secs, loop=True, session_id="d": (
            seen.update(loop=loop, secs=secs) or b"x")
        monkey._kick = lambda key, fn: fn()
        scene_audio.consequence_bed("The floor gives out beneath the crate",
                                    session_id="bedtest")
    finally:
        monkey.is_available, monkey._generate_sfx, monkey._kick = (
            orig_avail, orig_sfx, orig_kick)
    assert seen["loop"] is False
    assert seen["secs"] == scene_audio.CONSEQUENCE_BED_SECONDS
    assert "not a loop" in scene_audio._consequence_bed_prompt("The floor gives out")
    # and the client must not loop the node or re-fire it once it has played
    js = (Path(__file__).resolve().parent / "static" / "js" / "standalone.js").read_text(
        encoding="utf-8")
    start = js.split("function startBeat(buf, key) {", 1)[1].split("\n    }", 1)[0]
    assert "s.loop = false;" in start
    assert "loopable(" not in start, "seam-blending is for a loop point it no longer has"
    assert "s.onended" in start, "a finished one-shot has to release the node"
    assert "beatPlayed" in js


def test_a_fragment_makes_no_bed(monkeypatch):
    """Error strings and one-word beats are not a scene to record."""
    monkeypatch.setattr(scene_audio, "is_available", lambda: True)
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
    """Measured: scoring off metadata.base sent the sound model "seamless looping
    environmental ambience of s grit across the pad. The place is the Four
    Corners fence..." for a desert well pad - the pump jack, the shed and the
    standing water sliced out by _clean_scene_text's last-240 rule, because
    build_realtime_base puts the style anchor first and the place line last.
    The keyword matcher then defaulted to indoor room tone. The bed is scored
    from vision's read of the rendered frame now."""
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


def test_a_desert_scored_from_the_render_base_came_out_indoors():
    """The regression this replaced, kept as a live demonstration: feed the
    render base and the scene is gone and the stock kind is wrong; feed the
    frame description and both are right."""
    import engine as _engine
    visual = ("A rusted pump jack squats in the foreground, chain slapping "
              "against its counterweight. Beyond it a collapsed equipment shed, "
              "corrugated sheeting peeled back, standing water in the ruts. "
              "Wind moves grit across the pad.")
    base = _engine.build_realtime_base(visual_scene=visual, narrative="")
    from_base = scene_audio._scene_to_sfx_prompt(base, mode="scene")
    from_frame = scene_audio._scene_to_sfx_prompt(visual, mode="scene")
    # the picture's audible contents survive one route and not the other
    assert "pump jack" not in from_base and "shed" not in from_base
    assert "pump jack" in from_frame and "shed" in from_frame
    # and the stock bed picked underneath is outdoors rather than a room
    assert scene_audio._ambience_kind(base) == "room", "the regression"
    assert scene_audio._ambience_kind(visual) != "room", "the fix"


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
    fixed gain is always wrong for something."""
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
