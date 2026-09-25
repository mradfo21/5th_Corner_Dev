"""Unit tests for Conversation Moment server helpers (no network)."""
from __future__ import annotations

from pathlib import Path

import scene_audio
import engine


def test_conversation_music_profile_is_intimate():
    prompts, cfg = scene_audio._scene_to_music_prompt(
        "a frightened radio operator in a basement", mode="conversation",
        direction="",
    )
    joined = " ".join(p["text"] for p in prompts).lower()
    assert "intimate" in joined or "conversation" in joined or "radio" in joined
    assert 60 <= cfg["bpm"] <= 100
    # Cache names diverge by mode so scene + conversation beds don't collide.
    scene_name = scene_audio._cache_name("a quiet hallway", 12, mode="scene")
    convo_name = scene_audio._cache_name("a quiet hallway", 12, mode="conversation")
    assert scene_name.startswith("scene_")
    assert convo_name.startswith("convo_")
    assert scene_name != convo_name


def test_authored_music_direction_is_mixed_into_every_scene():
    """Typing a score in the editor used to do nothing unless Generate
    succeeded and locked a loop. The direction has to ride on the next
    scene — and the next run — without that."""
    prompts, _cfg = scene_audio._scene_to_music_prompt(
        "a sunlit kitchen", mode="scene",
        direction="slow detuned piano, tape hiss, no drums",
    )
    joined = " ".join(p["text"] for p in prompts)
    assert "detuned piano" in joined
    assert "sunlit kitchen" in joined
    empty, _ = scene_audio._scene_to_music_prompt(
        "a sunlit kitchen", mode="scene", direction="")
    assert "detuned piano" not in " ".join(p["text"] for p in empty)


def test_saving_a_direction_clears_a_generated_lock(tmp_path, monkeypatch):
    """A leftover generated loop hid every later prompt. Saving a new
    direction has to take the lock off so the next run can hear it."""
    music = tmp_path / "music"
    music.mkdir()
    monkeypatch.setattr(scene_audio, "MUSIC_DIR", music)
    monkeypatch.setattr(scene_audio, "_DIRECTION_PATH", music / "direction.json")
    monkeypatch.setattr(scene_audio, "_LOOP_META", music / "loop.json")
    (music / "loop.wav").write_bytes(b"RIFF")
    (music / "loop.json").write_text(
        '{"file": "loop.wav", "source": "generated", "prompt": "old", "created_at": 1}',
        encoding="utf-8")
    assert scene_audio.custom_loop() is not None
    scene_audio.set_music_direction("new uneasy drones")
    assert scene_audio.get_music_direction() == "new uneasy drones"
    assert scene_audio.custom_loop() is None


def test_scene_text_uses_the_shot_not_the_style_stamp():
    """Image prompts open with a stable style stamp. Scoring the first
    240 characters made every turn the same piece of music."""
    stamp = (
        "cinematic 35mm film still, analog grain, muted palette, "
        "wide establishing shot of the authored location, "
    ) * 8
    shot = "a rusted radio on a kitchen table under a bare bulb"
    text = stamp + shot
    assert "rusted radio" not in text[:240]
    cleaned = scene_audio._clean_scene_text(text)
    assert "rusted radio" in cleaned
    assert cleaned == text[-240:]


def test_menu_loop_does_not_steal_the_match(tmp_path, monkeypatch):
    """A title-screen lock must not become the in-match bed."""
    music = tmp_path / "music"
    music.mkdir()
    monkeypatch.setattr(scene_audio, "MUSIC_DIR", music)
    monkeypatch.setattr(scene_audio, "_DIRECTION_PATH", music / "direction.json")
    monkeypatch.setattr(scene_audio, "_LOOP_META", music / "loop.json")
    monkeypatch.setattr(scene_audio, "_MENU_META", music / "menu.json")
    monkeypatch.setattr(scene_audio, "_MENU_DIRECTION_PATH", music / "menu_direction.json")
    (music / "menu.wav").write_bytes(b"RIFF")
    (music / "menu.json").write_text(
        '{"file": "menu.wav", "source": "generated", "prompt": "title", "created_at": 1}',
        encoding="utf-8")
    menu = scene_audio.menu_loop()
    assert menu is not None
    assert menu["file"] == "menu.wav"
    assert scene_audio.custom_loop() is None


def test_saving_menu_direction_clears_a_generated_menu_lock(tmp_path, monkeypatch):
    music = tmp_path / "music"
    music.mkdir()
    monkeypatch.setattr(scene_audio, "MUSIC_DIR", music)
    monkeypatch.setattr(scene_audio, "_MENU_META", music / "menu.json")
    monkeypatch.setattr(scene_audio, "_MENU_DIRECTION_PATH", music / "menu_direction.json")
    (music / "menu.wav").write_bytes(b"RIFF")
    (music / "menu.json").write_text(
        '{"file": "menu.wav", "source": "generated", "prompt": "old", "created_at": 1}',
        encoding="utf-8")
    assert scene_audio.menu_loop() is not None
    scene_audio.set_menu_direction("warm analog title theme")
    assert scene_audio.get_menu_direction() == "warm analog title theme"
    assert scene_audio.menu_loop() is None


def test_last_preview_returns_a_cached_sample(tmp_path, monkeypatch):
    music = tmp_path / "music"
    music.mkdir()
    monkeypatch.setattr(scene_audio, "MUSIC_DIR", music)
    assert scene_audio.last_preview("menu_preview") is None
    (music / "menu_preview.wav").write_bytes(b"RIFF" + b"\x00" * 40)
    rec = scene_audio.last_preview("menu_preview")
    assert rec is not None
    assert rec["file"] == "menu_preview.wav"
    assert rec["url"].startswith("/audio/menu_preview.wav?v=")


def test_saving_menu_direction_clears_a_stale_preview(tmp_path, monkeypatch):
    music = tmp_path / "music"
    music.mkdir()
    monkeypatch.setattr(scene_audio, "MUSIC_DIR", music)
    monkeypatch.setattr(scene_audio, "_MENU_DIRECTION_PATH", music / "menu_direction.json")
    monkeypatch.setattr(scene_audio, "_MENU_META", music / "menu.json")
    (music / "menu_preview.wav").write_bytes(b"RIFF" + b"\x00" * 40)
    scene_audio.set_menu_direction("warm analog title theme")
    assert not (music / "menu_preview.wav").exists()
    (music / "menu_preview.wav").write_bytes(b"RIFF" + b"\x00" * 40)
    scene_audio.set_menu_direction("warm analog title theme")
    assert (music / "menu_preview.wav").exists()


def test_an_old_elevenlabs_key_does_not_turn_generation_back_on(monkeypatch):
    """Music and SFX were ElevenLabs; nothing on the player's key makes them
    now (docs/plans/ONE_KEY_AUDIO_PLAN.md). A keys.env that still carries
    the old key must not bring back a Generate button that cannot work."""
    monkeypatch.delenv("MOCK_MODE", raising=False)
    monkeypatch.delenv("STORYGEN_BACKEND", raising=False)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_" + "a" * 40)
    assert not scene_audio.is_available()
    assert scene_audio.unavailable_reason()


def test_build_portrait_prompt_uses_cinematic_anchor():
    ctx = {
        "subject": {"label": "Kane", "kind": "person", "speaks": True},
        "situation": {
            "scene": "a flooded subway platform lit by a single red bulb",
            "time_of_day": "night",
            "location": "subway",
        },
    }
    prompt = engine.build_portrait_prompt(ctx)
    low = prompt.lower()
    assert "kane" in low
    assert "medium shot" in low or "cinematic" in low
    assert "subway" in low or "flooded" in low


def test_portrait_cache_key_stable_per_scene():
    a = engine._portrait_cache_key("s1", "Kane", "a dark hallway")
    b = engine._portrait_cache_key("s1", "kane", "a dark hallway")
    c = engine._portrait_cache_key("s1", "Kane", "a bright room")
    assert a == b
    assert a != c


def test_portrait_cache_key_includes_bbox():
    a = engine._portrait_cache_key(
        "s1", "Kane", "hall", {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
    )
    b = engine._portrait_cache_key(
        "s1", "Kane", "hall", {"x": 0.7, "y": 0.2, "w": 0.3, "h": 0.4},
    )
    assert a != b


def test_norm_box_from_scan_subject():
    box = engine._norm_box_from_subject({"cx": 0.5, "cy": 0.4, "w": 0.2, "h": 0.3})
    assert box is not None
    assert box["w"] > 0.2  # padded
    assert box["h"] > 0.3
    assert abs((box["x"] + box["w"] / 2.0) - 0.5) < 0.03
    assert engine._norm_box_from_subject({"cx": 0.5, "cy": 0.5, "w": 1.0, "h": 1.0}) is None
    assert engine._norm_box_from_subject({"label": "figure"}) is None


def test_crop_image_to_norm_box_keeps_the_tagged_pixels(tmp_path):
    from PIL import Image
    im = Image.new("RGB", (100, 100), (0, 0, 255))
    for x in range(20, 50):
        for y in range(10, 40):
            im.putpixel((x, y), (255, 0, 0))
    path = tmp_path / "scene.png"
    im.save(path)
    out = engine._crop_image_to_norm_box(
        str(path), {"x": 0.20, "y": 0.10, "w": 0.30, "h": 0.30},
    )
    cropped = Image.open(out)
    assert cropped.size == (30, 30)
    # Center of the crop was the red square in the source.
    r, g, b = cropped.getpixel((15, 15))
    assert r > 200 and b < 50


def test_build_portrait_prompt_img2img_uses_the_crop():
    ctx = {
        "subject": {"label": "Kane", "kind": "person", "speaks": True},
        "situation": {
            "scene": "a flooded subway platform lit by a single red bulb",
            "time_of_day": "night",
            "location": "subway",
        },
    }
    prompt = engine.build_portrait_prompt(ctx, img2img=True).lower()
    assert "crop" in prompt
    assert "exact person" in prompt or "this exact" in prompt
    assert "turn the camera" not in prompt


def test_the_close_up_is_told_about_the_wide_reference():
    """The crop says WHAT the subject is; the wide says WHERE it is.

    Generated from the crop alone, the model had nothing to build a background
    out of, so it invented one and the dive read as a cut to another location
    rather than a close look at something in this place. The second reference
    fixes that only if the prompt names it — told just about "the crop", the
    model treated the wide as another subject and sometimes drew that instead.
    """
    for kind in ("person", "object"):
        ctx = {
            "subject": {"label": "Kane" if kind == "person" else "truck door",
                        "kind": kind, "speaks": kind == "person"},
            "situation": {"scene": "a flooded yard", "time_of_day": "dusk",
                          "location": "yard"},
        }
        wide = engine.build_portrait_prompt(ctx, img2img=True, with_wide=True)
        low = wide.lower()
        assert "first reference" in low, kind
        assert "second reference" in low, kind
        assert "wide shot" in low, kind
        # It is context, not the composition to copy.
        assert "not the subject" in low, kind
        assert ("framing must not" in low) or ("must not be reproduced" in low), kind

        # And without a wide frame the prompt must not promise one.
        alone = engine.build_portrait_prompt(ctx, img2img=True).lower()
        assert "second reference" not in alone, kind
        assert "softly out of focus" in alone, kind


def test_talk_subject_is_figure_for_beings_not_machines():
    assert engine._talk_subject_is_figure({"label": "Kane", "kind": "person"})
    assert engine._talk_subject_is_figure({"label": "guard", "kind": "character"})
    assert not engine._talk_subject_is_figure(
        {"label": "computer monitor", "kind": "machine"}
    )
    assert not engine._talk_subject_is_figure({"label": "rusted radio", "kind": "object"})
    # Label backstop when detect omitted kind — this was the "talk to the
    # monitor, get a random guy" path.
    assert not engine._talk_subject_is_figure({"label": "computer monitor"})
    assert engine._talk_subject_is_figure({"label": "security guard"})


def test_talk_portrait_object_without_crop_does_not_invent_a_person():
    """No live crop + a machine must not fall through to character text2img."""
    import api
    from unittest import mock

    c = api.app.test_client()
    with mock.patch("engine._rate_limited", return_value=False), \
         mock.patch("engine.IMAGE_ENABLED", True), \
         mock.patch("engine.CONVERSATION_PORTRAIT_BUDGET", 99), \
         mock.patch("api._spend_blocked", return_value=None):
        r = c.post("/api/talk/portrait", json={
            "subject": {"label": "computer monitor", "kind": "machine"},
            "session_id": "test_monitor_nocrop",
        })
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data.get("image_url") is None
    assert data.get("reason") == "no_crop"


def test_client_pins_the_live_crop_and_does_not_rebuild_objects_as_people():
    from pathlib import Path
    root = Path(__file__).resolve().parent
    js = (root / "static" / "js" / "standalone.js").read_text(encoding="utf-8")
    assert "function subjectIsFigure" in js
    assert "if (!subjectIsFigure(subj)) return;" in js
    assert "setPortrait(referenceFrame)" in js
    assert "function subjectTalkCropBox" in js
    assert "pinnedCrop" in js
    # Full-frame fallback painted the third-person player as the subject.
    start = js.split("async function start(subj)", 1)[1].split("\n    function ", 1)[0]
    assert "captureScanFrame" not in start
    assert "Do NOT fall back to a full-frame capture" in start
    # World-model re-anchor is opt-in; default mirrored the follow-cam player.
    animate = js.split("function animateCharacter(", 1)[1].split("\n    function ", 1)[0]
    assert "__CONVERSATION_ANIMATE__ !== true" in animate
    reactor = (root / "static" / "js" / "reactor_renderer.js").read_text(encoding="utf-8")
    assert "function captureSource" in reactor


def test_talk_portrait_figure_without_crop_does_not_invent_the_player():
    """No live crop + a person must not text2img the player-centric world."""
    import api
    from unittest import mock

    c = api.app.test_client()
    with mock.patch("engine._rate_limited", return_value=False), \
         mock.patch("engine.IMAGE_ENABLED", True), \
         mock.patch("engine.CONVERSATION_PORTRAIT_BUDGET", 99), \
         mock.patch("api._spend_blocked", return_value=None):
        r = c.post("/api/talk/portrait", json={
            "subject": {"label": "The Watcher", "kind": "person"},
            "session_id": "test_watcher_nocrop",
        })
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data.get("image_url") is None
    assert data.get("reason") == "no_crop"


def test_talk_portrait_img2img_uses_the_crop():
    """SCAN crop is the likeness reference; Gemini reframes it to a portrait."""
    import api
    import base64
    import io
    from pathlib import Path
    from unittest import mock
    from PIL import Image

    im = Image.new("RGB", (32, 32), (180, 20, 20))
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    data_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

    c = api.app.test_client()
    sid = "test_watcher_crop"
    out = Path(engine._get_image_dir(sid)) / "talk_portrait_the_watcher.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), (20, 20, 180)).save(out, "PNG")
    with mock.patch("engine._rate_limited", return_value=False), \
         mock.patch("engine.IMAGE_ENABLED", True), \
         mock.patch("engine.CONVERSATION_PORTRAIT_BUDGET", 99), \
         mock.patch("api._spend_blocked", return_value=None), \
         mock.patch("gemini_image_utils.generate_gemini_img2img", return_value=str(out)) as gen:
        r = c.post("/api/talk/portrait", json={
            "subject": {
                "label": "The Watcher", "kind": "person",
                "cx": 0.7, "cy": 0.4, "w": 0.2, "h": 0.45,
            },
            "reference_image": data_url,
            "reference_cropped": True,
            "session_id": sid,
        })
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data.get("mode") == "img2img"
    assert data.get("image_url")
    gen.assert_called_once()
    kwargs = gen.call_args.kwargs
    assert kwargs.get("portrait_mode") is True
    assert kwargs.get("subject_crop") is True
    assert kwargs.get("object_subject") is False
    assert not kwargs.get("identity_paths")
    assert kwargs.get("world_prompt") in (None, "")
    st = engine._load_state(sid)
    watcher = (st.get("companions") or {}).get("the watcher") or {}
    assert watcher.get("portrait_source") == "img2img"


def test_talk_session_does_not_block_on_vision():
    src = Path(__file__).resolve().parent.joinpath("engine.py").read_text(encoding="utf-8")
    session = src.split("def api_talk_session(", 1)[1].split("\ndef ", 1)[0]
    assert "include_vision=False" in session
    assert "_talk_opening_fallback" in session
    assert "_is_canned_talk_fallback" in session
    assert "or _talk_opening_fallback(_label, _kind)" not in session
    message = src.split("def api_talk_message(", 1)[1].split("\ndef ", 1)[0]
    assert "include_vision=False" in message


def test_build_portrait_prompt_object_keeps_the_object():
    ctx = {
        "subject": {"label": "computer monitor", "kind": "machine", "speaks": True},
        "situation": {
            "scene": "a dusty control room",
            "time_of_day": "dusk",
            "location": "control room",
        },
    }
    prompt = engine.build_portrait_prompt(ctx, img2img=True).lower()
    assert "crop" in prompt
    assert "exact object" in prompt or "this exact object" in prompt
    assert "exact person" not in prompt
    assert "do not invent a person" in prompt
    assert "face, hair, clothes" not in prompt
    t2i = engine.build_portrait_prompt(ctx, img2img=False).lower()
    assert "computer monitor" in t2i
    assert "no person" in t2i
    assert "mid-torso" not in t2i


def test_record_character_memory_upserts():
    sid = "test_moment_mem"
    # Isolate: wipe any prior characters for this synthetic session.
    try:
        st = engine._load_state(sid) or {}
        st["characters"] = {}
        st["turn_count"] = 3
        engine._save_state(st, sid)
    except Exception:
        pass
    entry = engine._record_character_memory(
        sid, {"label": "Kane", "kind": "person"}, note="Seemed wary."
    )
    assert entry["label"] == "Kane"  # display casing preserved
    assert entry["talk_count"] == 1
    assert entry["notes"][-1] == "Seemed wary."
    entry2 = engine._record_character_memory(
        sid, {"label": "Kane", "kind": "person"}, note="Mentioned the basement."
    )
    assert entry2["talk_count"] == 2
    assert len(entry2["notes"]) == 2
    # Lookup key is lowercased so "Kane" / "kane" collide on one record.
    st = engine._load_state(sid)
    assert "kane" in (st.get("characters") or {})


def test_record_companion_stores_portrait():
    sid = "test_companion_roster"
    try:
        st = engine._load_state(sid) or {}
        st["companions"] = {}
        st["characters"] = {}
        st["turn_count"] = 5
        engine._save_state(st, sid)
    except Exception:
        pass
    entry = engine._record_companion(
        sid, {"label": "Security Guard", "kind": "person"},
        "/images/companion_security_guard.png", prompt="a guard", scene="a warehouse",
    )
    assert entry["label"] == "Security Guard"
    assert entry["portrait_url"] == "/images/companion_security_guard.png"
    assert entry["seen_count"] == 1
    assert entry["first_seen_turn"] == 5
    # Re-seeing bumps the count, keeps first_seen.
    entry2 = engine._record_companion(
        sid, {"label": "Security Guard", "kind": "person"},
        "/images/companion_security_guard.png",
    )
    assert entry2["seen_count"] == 2
    assert entry2["first_seen_turn"] == 5
    # Roster persisted + keyed lowercased.
    st = engine._load_state(sid)
    assert "security guard" in (st.get("companions") or {})


def test_companion_slug_is_filesystem_safe():
    assert engine._companion_slug("Security Guard!") == "security_guard"
    assert engine._companion_slug("  Kane / Fleece  ") == "kane_fleece"
    assert engine._companion_slug("") == "figure"


def test_companion_voice_stored_and_preserved():
    sid = "test_companion_voice"
    st = engine._load_state(sid) or {}
    st["companions"] = {}
    st["characters"] = {}
    st["turn_count"] = 4
    engine._save_state(st, sid)
    # Voice resolved first (as in api_talk_session), before the portrait lands.
    engine._record_companion_voice(sid, {"label": "Kane", "kind": "person"}, {
        "voice_id": "vox_kane_123",
        "description": "a low, gravelly wary male voice, mid-40s, tired",
        "source": "designed",
        "status": "ready",
        "cache_key": "abc123",
        "model": "eleven_ttv_v3",
    })
    st = engine._load_state(sid)
    v = (st.get("companions") or {}).get("kane", {}).get("voice") or {}
    assert v.get("voice_id") == "vox_kane_123"
    assert "gravelly" in v.get("description", "")
    assert v.get("model") == "eleven_ttv_v3"
    # Portrait lands AFTER — must NOT drop the voice block.
    engine._record_companion(sid, {"label": "Kane", "kind": "person"},
                             "/images/companion_kane.png")
    st = engine._load_state(sid)
    comp = (st.get("companions") or {}).get("kane", {})
    assert comp.get("portrait_url") == "/images/companion_kane.png"
    assert (comp.get("voice") or {}).get("voice_id") == "vox_kane_123"  # preserved
    # A later preset override (empty description) must NOT erase the regen seed.
    engine._record_companion_voice(sid, {"label": "Kane", "kind": "person"}, {
        "voice_id": "preset_xyz", "description": "", "source": "override",
        "status": "override", "cache_key": None, "model": "",
    })
    st = engine._load_state(sid)
    v2 = (st.get("companions") or {}).get("kane", {}).get("voice") or {}
    assert v2.get("voice_id") == "preset_xyz"
    assert "gravelly" in v2.get("description", "")  # regen description retained


def test_resolve_image_path_is_session_aware():
    # Regression: companion/camp images live in sessions/<id>/images, and their
    # web URLs carry a ?session=<id> hint. _resolve_image_path must find them
    # there (not only the legacy root dir) or the camp roster comes up empty.
    import os
    from pathlib import Path
    sid = "test_resolve_session"
    img_dir = Path(engine._get_image_dir(sid))
    fpath = img_dir / "companion_zzz.png"
    fpath.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 600)
    try:
        url = engine._to_web_image_url("companion_zzz.png", sid)
        assert "?session=" + sid in url
        # Resolve via the URL's own ?session= hint (no explicit id).
        p1 = engine._resolve_image_path(url)
        assert p1 and p1.exists() and str(p1).endswith("companion_zzz.png")
        # And via an explicit session_id.
        p2 = engine._resolve_image_path(url, sid)
        assert p2 and p2.exists()
        # A default-session web URL still resolves (no query).
        d_dir = Path(engine._get_image_dir("default"))
        dfile = d_dir / "companion_def.png"
        dfile.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 600)
        pd = engine._resolve_image_path("/images/companion_def.png")
        assert pd and pd.exists()
        dfile.unlink(missing_ok=True)
    finally:
        fpath.unlink(missing_ok=True)


def test_companions_endpoint_lists_roster():
    import api
    c = api.app.test_client()
    sid = "test_companion_api"
    # Seed a companion + a memory note.
    st = engine._load_state(sid) or {}
    st["companions"] = {}
    st["characters"] = {}
    st["turn_count"] = 2
    engine._save_state(st, sid)
    engine._record_companion(sid, {"label": "Kane", "kind": "person"},
                             "/images/companion_kane.png")
    engine._record_character_memory(sid, {"label": "Kane", "kind": "person"}, note="Wary.")
    r = c.get("/api/companions?session_id=" + sid)
    assert r.status_code == 200
    data = r.get_json()
    labels = [x["label"] for x in data["companions"]]
    assert "Kane" in labels
    kane = next(x for x in data["companions"] if x["label"] == "Kane")
    assert kane["portrait_url"] == "/images/companion_kane.png"


def test_resolve_voice_reuses_companion_voice_id():
    # Continuing-story: talking to a known companion must reuse their stored
    # voice_id instead of designing a fresh one every scene.
    sid = "test_companion_voice_reuse"
    st = engine._load_state(sid) or {}
    st["companions"] = {}
    st["characters"] = {}
    st["world_prompt"] = "a flooded subway"
    engine._save_state(st, sid)
    preset = "Algenib"  # a Gemini voice from voices.json
    seed = "a low gravelly wary male voice mid-40s tired analog horror."
    engine._record_companion_voice(sid, {"label": "Kane", "kind": "person"}, {
        "voice_id": preset,
        "description": seed,
        "source": "designed",
        "status": "ready",
        "cache_key": "deadbeefdeadbeef",
        "model": "gemini-3.8-flash-tts",
    })
    resolved = engine.resolve_voice_for_subject(
        {"label": "Kane", "kind": "person"}, sid, world_prompt="somewhere else"
    )
    assert resolved["voice_id"] == preset
    assert resolved["source"] == "companion"
    assert resolved["status"] == "ready"
    assert "gravelly" in resolved["description"]


def test_companion_regenerate_voice_endpoint():
    import api
    from unittest import mock

    sid = "test_companion_regen_api"
    st = engine._load_state(sid) or {}
    st["companions"] = {}
    st["characters"] = {}
    st["world_prompt"] = "campfire"
    engine._save_state(st, sid)
    seed = "a low gravelly wary male voice mid-40s tired analog horror."
    engine._record_companion(sid, {"label": "Kane", "kind": "person"},
                             "/images/companion_kane.png")
    engine._record_companion_voice(sid, {"label": "Kane", "kind": "person"}, {
        "voice_id": "cjVigY5qzO86Huf0OWal",
        "description": seed,
        "source": "designed",
        "status": "ready",
        "cache_key": "oldkeyoldkeyoldk",
        "model": "eleven_ttv_v3",
    })

    fake = {
        "voice_id": "voice_gv_regen",
        "cache_key": "newkeynewkeynewk",
        "source": "designed",
        "status": "ready",
        "description": seed,
    }
    c = api.app.test_client()
    with mock.patch("voice_design.regenerate_voice", return_value=fake), \
         mock.patch("voice_design.is_available", return_value=True):
        r = c.post("/api/companions/regenerate_voice", json={
            "label": "Kane", "session_id": sid, "wait": 0.5,
        })
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data["label"] == "Kane"
    assert data["voice"]["voice_id"] == "voice_gv_regen"
    assert data["voice"]["status"] == "ready"
    # Roster updated to the new id; description seed preserved.
    st = engine._load_state(sid)
    v = (st.get("companions") or {}).get("kane", {}).get("voice") or {}
    assert v.get("voice_id") == "voice_gv_regen"
    assert "gravelly" in (v.get("description") or "")

    # Missing description → no_description (don't burn a design credit).
    # Use a fresh session so the 2s rate-limit from the call above doesn't
    # 429 this assertion.
    sid2 = "test_companion_regen_nodesc"
    st2 = engine._load_state(sid2) or {}
    st2["companions"] = {}
    engine._save_state(st2, sid2)
    engine._record_companion_voice(sid2, {"label": "Mute", "kind": "person"}, {
        "voice_id": "cjVigY5qzO86Huf0OWal", "description": "", "source": "fallback",
        "status": "ready", "cache_key": None, "model": "",
    })
    with mock.patch("engine._rate_limited", return_value=False):
        r2 = c.post("/api/companions/regenerate_voice", json={
            "label": "Mute", "session_id": sid2,
        })
    assert r2.status_code == 200
    assert r2.get_json().get("reason") == "no_description"


# ---------------------------------------------------------------------------
# TALKING TO SOMEONE HAS TO DO SOMETHING
#
# A conversation used to be a closed loop: the transcript lived in the browser,
# /api/talk/end counted the conversation and threw the words away, and the next
# turn was generated as though the player had stood there in silence. INTERACT
# already worked the other way — the press commits a turn and the dive holds
# until the world answers — which is why reaching out felt like it landed and
# speaking did not.
# ---------------------------------------------------------------------------

def _wipe(sid, **state):
    st = engine._load_state(sid) or {}
    st.pop("last_conversation", None)
    st.update(state)
    engine._save_state(st, sid)
    return st


def test_a_finished_conversation_is_stamped_onto_the_turn_it_caused():
    sid = "test_talk_turn"
    _wipe(sid, turn_count=7)
    engine._record_turn_conversation(sid, "miner", [
        {"role": "user", "text": "Who else has been through here?"},
        {"role": "them", "text": "Two men in a white truck."},
    ])
    rec = (engine._load_state(sid) or {}).get("last_conversation")
    assert rec["with"] == "miner"
    assert rec["turn"] == 7          # the turn it is the consequence of
    assert [ln["role"] for ln in rec["lines"]] == ["you", "them"]
    assert rec["lines"][1]["text"] == "Two men in a white truck."


def test_a_conversation_only_colours_the_turn_it_belongs_to():
    """A stale exchange must not shape a beat three moves later."""
    sid = "test_talk_turn_stale"
    _wipe(sid, turn_count=7)
    engine._record_turn_conversation(sid, "miner", [
        {"role": "user", "text": "Show me the way down."},
    ])
    st = engine._load_state(sid)
    assert engine._conversation_for_turn(st) is not None
    st["turn_count"] = 9             # two turns later
    assert engine._conversation_for_turn(st) is None
    assert engine._conversation_directive(st) == ""


def test_an_empty_conversation_is_not_recorded():
    """Opening a channel and closing it without a word is not an action."""
    sid = "test_talk_turn_empty"
    _wipe(sid, turn_count=2)
    engine._record_turn_conversation(sid, "miner", [])
    engine._record_turn_conversation(sid, "miner", [{"role": "user", "text": "   "}])
    engine._record_turn_conversation(sid, "miner", ["not a line"])
    assert (engine._load_state(sid) or {}).get("last_conversation") is None


def test_the_consequence_is_told_to_make_the_conversation_land():
    sid = "test_talk_turn_directive"
    _wipe(sid, turn_count=4)
    engine._record_turn_conversation(sid, "miner", [
        {"role": "user", "text": "Can you show me the way down?"},
        {"role": "them", "text": "I'll unlock the stair gate."},
    ])
    d = engine._conversation_directive(engine._load_state(sid))
    # Both voices reach the prompt, attributed.
    assert "PLAYER: Can you show me the way down?" in d
    assert "MINER: I'll unlock the stair gate." in d
    # And the requirement that made it feel like an action rather than a scene.
    assert "IS the action of this turn" in d
    assert "Write what the conversation CHANGED" in d
    # The person does not evaporate on hang-up — this is talk's permanence.
    assert "still here" in d and "visual_scene" in d


def test_the_transcript_is_clipped_not_pasted_whole():
    """The exchange should inform the beat, not become it."""
    sid = "test_talk_turn_clip"
    _wipe(sid, turn_count=1)
    engine._record_turn_conversation(sid, "miner", [
        {"role": "user", "text": f"line {i} " + ("x" * 500)} for i in range(40)
    ])
    rec = (engine._load_state(sid) or {}).get("last_conversation")
    assert len(rec["lines"]) == engine.CONVERSATION_TURN_LINES
    assert all(len(ln["text"]) <= 300 for ln in rec["lines"])
    assert "line 39" in rec["lines"][-1]["text"]   # keeps the END of the talk


def test_the_conversation_reaches_the_consequence_prompt():
    """The directive is wired into the grounding block the consequence LLM
    actually reads — not built and dropped."""
    src = (Path(__file__).parent / "engine.py").read_text(encoding="utf-8")
    block = src[src.index("grounding_block = ("):]
    block = block[:block.index("\n        )")]
    assert "_conversation_directive(state)" in block


def test_talking_raises_the_stakes_without_counting_as_meddling():
    """Talk should be able to move the story and draw attention, but prying
    open a sealed drum is not the same act as asking someone a question."""
    src = (Path(__file__).parent / "engine.py").read_text(encoding="utf-8")
    assert 'is_talk = source == "talk"' in src
    assert 'risk_boost = 2 if is_interaction else (1 if is_talk else 0)' in src
    # Deliberately NOT an "interaction": that flag also picks the
    # "handling/entering a specific thing" directive, which is the wrong
    # sentence for a person the player just spoke to.
    assert 'is_interaction = source in ("scan_interact", "scan_move", "encounter")' in src


def test_hanging_up_commits_a_turn_and_holds_the_moment():
    """The client half. Hanging up fires a real turn whose action is the
    conversation, and the Moment stays up until it lands — leaving early
    would drop the player back on the frame they were already looking at,
    which is the thing that made talking feel inert."""
    js = (Path(__file__).parent / "static/js/standalone.js").read_text(encoding="utf-8")
    settle = js[js.index("function settleAfterTalk("):]
    settle = settle[:settle.index("\n    }")]
    assert 'source: "talk"' in settle
    assert "conversation: transcript.slice(-12)" in settle
    assert "subject: label" in settle
    # Held until the turn is genuinely over, not merely painted.
    assert "Ceremony.isActive" in settle
    assert "state.awaitingResolution" in settle
    # …and never held for a turn that was refused, or forever.
    assert "if (!dispatched) { release(); return; }" in settle
    assert "SETTLE_MAX_MS" in settle
    # The payload reaches the server.
    assert "conversation: actionConversation," in js


def test_both_voice_and_text_conversations_are_remembered():
    """`messages` is the text-mode request body and stays empty for a whole
    voice call, where lines arrive through the SDK's onMessage. addLine is
    the one place every spoken line passes through."""
    js = (Path(__file__).parent / "static/js/standalone.js").read_text(encoding="utf-8")
    add = js[js.index("function addLine(role, content, opts) {"):]
    add = add[:add.index("\n    }")]
    assert "transcript.push(" in add
    # The client's own error notices are chrome, not something anybody said.
    assert "opts.record !== false" in add
    assert '"[the signal drops \\u2014 try again]", { record: false }' in js


def test_a_conversation_nobody_spoke_in_does_not_cost_a_turn():
    js = (Path(__file__).parent / "static/js/standalone.js").read_text(encoding="utf-8")
    worth = js[js.index("function worthATurn() {"):]
    worth = worth[:worth.index("\n    }")]
    assert 'transcript.some((ln) => ln.role === "user")' in worth
    # No exception for voice any more: every line the player says, typed or
    # dictated, passes through addLine (the ElevenLabs SDK's did not).
    assert 'mode === "voice"' not in worth


def test_speaking_from_inside_another_moment_does_not_double_spend():
    """SPEAK in an interact dive returns the player to the dive, which has
    already committed its own turn and is still waiting on it."""
    js = (Path(__file__).parent / "static/js/standalone.js").read_text(encoding="utf-8")
    assert "function nestedInAnotherMoment()" in js
    assert "window.Moments.depth() > 1" in js
    guard = js[js.index("if (worthATurn()"):]
    guard = guard[:guard.index("}")]
    assert "!nestedInAnotherMoment()" in guard
    assert "!state.awaitingResolution" in guard
    moments = (Path(__file__).parent / "static/js/moments.js").read_text(encoding="utf-8")
    assert "function depth() { return stack.length; }" in moments
    assert "\n    depth,\n" in moments


if __name__ == "__main__":
    test_conversation_music_profile_is_intimate()
    test_build_portrait_prompt_uses_cinematic_anchor()
    test_portrait_cache_key_stable_per_scene()
    test_portrait_cache_key_includes_bbox()
    test_norm_box_from_scan_subject()
    test_build_portrait_prompt_img2img_uses_the_crop()
    test_talk_subject_is_figure_for_beings_not_machines()
    test_build_portrait_prompt_object_keeps_the_object()
    test_talk_portrait_object_without_crop_does_not_invent_a_person()
    test_talk_portrait_figure_without_crop_does_not_invent_the_player()
    test_talk_portrait_img2img_uses_the_crop()
    test_talk_session_does_not_block_on_vision()
    test_client_pins_the_live_crop_and_does_not_rebuild_objects_as_people()
    test_record_character_memory_upserts()
    test_record_companion_stores_portrait()
    test_companion_slug_is_filesystem_safe()
    test_companion_voice_stored_and_preserved()
    test_resolve_image_path_is_session_aware()
    test_companions_endpoint_lists_roster()
    test_a_finished_conversation_is_stamped_onto_the_turn_it_caused()
    test_a_conversation_only_colours_the_turn_it_belongs_to()
    test_an_empty_conversation_is_not_recorded()
    test_the_consequence_is_told_to_make_the_conversation_land()
    test_the_transcript_is_clipped_not_pasted_whole()
    test_the_conversation_reaches_the_consequence_prompt()
    test_talking_raises_the_stakes_without_counting_as_meddling()
    test_hanging_up_commits_a_turn_and_holds_the_moment()
    test_both_voice_and_text_conversations_are_remembered()
    test_a_conversation_nobody_spoke_in_does_not_cost_a_turn()
    test_speaking_from_inside_another_moment_does_not_double_spend()
    print("ok")
