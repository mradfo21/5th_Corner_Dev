"""Unit tests for Conversation Moment server helpers (no network)."""
from __future__ import annotations

import scene_audio
import engine


def test_conversation_music_profile_is_intimate():
    prompts, cfg = scene_audio._scene_to_music_prompt(
        "a frightened radio operator in a basement", mode="conversation"
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
    # ElevenLabs voice_id instead of designing a fresh one every scene.
    sid = "test_companion_voice_reuse"
    st = engine._load_state(sid) or {}
    st["companions"] = {}
    st["characters"] = {}
    st["world_prompt"] = "a flooded subway"
    engine._save_state(st, sid)
    preset = "cjVigY5qzO86Huf0OWal"  # Eric — known voices.json id
    seed = "a low gravelly wary male voice mid-40s tired analog horror."
    engine._record_companion_voice(sid, {"label": "Kane", "kind": "person"}, {
        "voice_id": preset,
        "description": seed,
        "source": "designed",
        "status": "ready",
        "cache_key": "deadbeefdeadbeef",
        "model": "eleven_ttv_v3",
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


if __name__ == "__main__":
    test_conversation_music_profile_is_intimate()
    test_build_portrait_prompt_uses_cinematic_anchor()
    test_portrait_cache_key_stable_per_scene()
    test_record_character_memory_upserts()
    test_record_companion_stores_portrait()
    test_companion_slug_is_filesystem_safe()
    test_companion_voice_stored_and_preserved()
    test_resolve_image_path_is_session_aware()
    test_companions_endpoint_lists_roster()
    print("ok")
