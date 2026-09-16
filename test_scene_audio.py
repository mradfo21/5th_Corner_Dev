"""Offline tests for ElevenLabs scene music + world SFX (no network)."""
from __future__ import annotations

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


def test_is_available_requires_an_sk_key(monkeypatch):
    _clear_mock(monkeypatch)
    monkeypatch.setattr(scene_audio, "ELEVENLABS_API_KEY", "")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert scene_audio.is_available() is False
    assert "not set" in scene_audio.unavailable_reason()
    monkeypatch.setenv("ELEVENLABS_API_KEY", "not-a-real-key")
    assert scene_audio.is_available() is False
    monkeypatch.setenv("ELEVENLABS_API_KEY", "ab" * 32)
    assert "key ID" in scene_audio.unavailable_reason()
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_" + "b" * 40)
    assert scene_audio.is_available() is True
    assert scene_audio.unavailable_reason() is None


def test_get_scene_audio_degrades_without_key(monkeypatch, tmp_path):
    monkeypatch.setattr(scene_audio, "ELEVENLABS_API_KEY", "")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr(scene_audio, "STOCK_DIR", tmp_path / "stock")
    monkeypatch.setattr(scene_audio, "MUSIC_DIR", tmp_path)
    monkeypatch.setattr(scene_audio, "_LOOP_META", tmp_path / "loop.json")
    (tmp_path / "stock").mkdir()
    assert scene_audio.get_scene_audio("a quiet forest at dawn") is None


def test_encounter_uses_stock_stinger_when_present(monkeypatch, tmp_path):
    monkeypatch.setattr(scene_audio, "ELEVENLABS_API_KEY", "")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
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


def test_health_and_scene_audio_name_a_bad_key(monkeypatch):
    import api
    _clear_mock(monkeypatch)
    monkeypatch.setattr(scene_audio, "ELEVENLABS_API_KEY", "")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "ab" * 32)
    client = api.app.test_client()
    health = client.get("/api/health").get_json()
    assert "music" in health
    assert health["music"]["can_generate"] is False
    assert "key ID" in (health["music"].get("reason") or "")
    rec = client.post("/api/scene_audio", json={"prompt": "a quiet forest"})
    body = rec.get_json()
    assert body.get("audio_url") is None
    assert "key ID" in (body.get("reason") or "")


def test_mock_mode_does_not_generate(monkeypatch, tmp_path):
    _clear_mock(monkeypatch)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_" + "b" * 40)
    monkeypatch.setenv("STORYGEN_BACKEND", "mock")
    monkeypatch.setattr(scene_audio, "STOCK_DIR", tmp_path / "stock")
    monkeypatch.setattr(scene_audio, "MUSIC_DIR", tmp_path)
    monkeypatch.setattr(scene_audio, "_LOOP_META", tmp_path / "loop.json")
    (tmp_path / "stock").mkdir()
    called = []
    monkeypatch.setattr(scene_audio, "_eleven_music",
                        lambda *a, **k: called.append("music") or b"x")
    monkeypatch.setattr(scene_audio, "_eleven_sfx",
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
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_" + "b" * 40)
    monkeypatch.setattr(scene_audio, "ELEVENLABS_API_KEY", "sk_" + "b" * 40)
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

    monkeypatch.setattr(scene_audio, "_eleven_music", slow_music)
    monkeypatch.setattr(scene_audio, "_eleven_sfx",
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


def test_ensure_stock_without_key_reports_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(scene_audio, "ELEVENLABS_API_KEY", "")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr(scene_audio, "STOCK_DIR", tmp_path)
    rec = scene_audio.ensure_stock_sounds()
    assert rec["ok"] is False
    assert rec["reason"] == "no_key"
    assert rec["files"]["encounter_enter"]["ready"] is False


def test_encounter_does_not_adopt_the_explore_loop(monkeypatch, tmp_path):
    monkeypatch.setattr(scene_audio, "ELEVENLABS_API_KEY", "")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
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


def test_kick_menu_preview_is_background_and_skips_without_direction(monkeypatch, tmp_path):
    import threading
    _clear_mock(monkeypatch)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_" + "b" * 40)
    monkeypatch.setattr(scene_audio, "ELEVENLABS_API_KEY", "sk_" + "b" * 40)
    monkeypatch.setattr(scene_audio, "MUSIC_DIR", tmp_path)
    monkeypatch.setattr(scene_audio, "_MENU_DIRECTION_PATH", tmp_path / "menu_direction.json")
    monkeypatch.setattr(scene_audio, "_MENU_WARMUP_STARTED", False)
    called = []
    ready = threading.Event()

    def fake_preview(*a, **k):
        called.append(1)
        ready.set()
        return {"url": "/audio/x"}

    monkeypatch.setattr(scene_audio, "generate_preview", fake_preview)
    scene_audio.kick_menu_preview()
    assert called == []
    scene_audio.set_menu_direction("warm analog title theme")
    monkeypatch.setattr(scene_audio, "_MENU_WARMUP_STARTED", False)
    scene_audio.kick_menu_preview()
    assert ready.wait(1.0)
    assert called
