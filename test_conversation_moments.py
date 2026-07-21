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


if __name__ == "__main__":
    test_conversation_music_profile_is_intimate()
    test_build_portrait_prompt_uses_cinematic_anchor()
    test_portrait_cache_key_stable_per_scene()
    test_record_character_memory_upserts()
    print("ok")
