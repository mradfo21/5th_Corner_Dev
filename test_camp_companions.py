"""Regression tests: CAMP must feed every companion screenshot + jeep into img2img."""
from __future__ import annotations

from pathlib import Path

import engine


def _write_tiny_png(path: Path) -> None:
    # Minimal valid 1x1 PNG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
        )
    )


def test_resolve_image_path_finds_session_companion():
    sid = "test_camp_resolve"
    img_dir = Path(engine._get_image_dir(sid))
    portrait = img_dir / "companion_kane.png"
    _write_tiny_png(portrait)

    # Legacy bug: /images/... resolved only against ROOT/images and missed this.
    resolved = engine._resolve_image_path("/images/companion_kane.png", sid)
    assert resolved is not None
    assert resolved.exists()
    assert resolved.resolve() == portrait.resolve()

    # Query-param session hint also works without an explicit session_id arg.
    resolved_qs = engine._resolve_image_path(
        f"/images/companion_kane.png?session={sid}"
    )
    assert resolved_qs is not None and resolved_qs.exists()


def test_collect_camp_companions_uses_state_and_disk():
    sid = "test_camp_roster"
    img_dir = Path(engine._get_image_dir(sid))
    for name in ("companion_kane.png", "companion_maya.png", "companion_orphan.png"):
        _write_tiny_png(img_dir / name)
    # Downsample must be ignored as a separate roster entry.
    _write_tiny_png(img_dir / "companion_kane_small.png")

    st = {
        "companions": {
            "kane": {
                "label": "Kane",
                "kind": "person",
                "portrait_url": "/images/companion_kane.png",
                "last_seen_turn": 9,
            },
            "maya": {
                "label": "Maya",
                "kind": "person",
                "portrait_url": "/images/companion_maya.png",
                "last_seen_turn": 3,
            },
            # Missing file on disk — must be skipped, not crash.
            "ghost": {
                "label": "Ghost",
                "kind": "person",
                "portrait_url": "/images/companion_ghost.png",
                "last_seen_turn": 1,
            },
        }
    }
    roster = engine._collect_camp_companions(sid, st)
    labels = [r["label"] for r in roster]
    assert "Kane" in labels
    assert "Maya" in labels
    assert "Ghost" not in labels
    # Disk-only companion still included.
    assert any(l.lower().startswith("orphan") for l in labels)
    # Most recently seen first.
    assert labels[0] == "Kane"
    for r in roster:
        assert Path(r["portrait_path"]).exists()
        assert not r["portrait_path"].endswith("_small.png")


def test_normalize_camp_leave_choice_forces_on_foot():
    raw_drive = "Leave camp and drive the red jeep into a new location across the desert."
    normalized = engine._normalize_camp_leave_choice(raw_drive, "camp_leave")
    low = normalized.lower()
    assert "leave camp" in low
    assert "new" in low and "location" in low
    assert "walk" in low or "on foot" in low
    assert "drive" not in low
    assert "jeep" not in low
    assert "truck" not in low
    # Non-camp sources keep the caller's text.
    assert engine._normalize_camp_leave_choice(raw_drive, "scan_move") == raw_drive


def test_camp_leave_image_prompt_forbids_cabin_pov():
    prompt = engine.build_image_prompt(
        player_choice=engine._CAMP_LEAVE_CHOICE,
        dispatch="Dusty desert track toward distant mesas under haze.",
        narrative_dispatch="You leave the fire behind and set out.",
        hard_transition=True,
    )
    low = prompt.lower()
    assert "not inside a vehicle" in low or "no vehicle interior" in low or "dashboard" in low
    assert "steering wheel" in low or "cabin" in low


def test_build_camp_prompt_maps_every_reference():
    attendees = [
        {"label": "Kane"},
        {"label": "Maya"},
    ]
    ref_map = [
        {"index": 1, "role": "jeep", "label": "red jeep"},
        {"index": 2, "role": "companion", "label": "Kane"},
        {"index": 3, "role": "companion", "label": "Maya"},
    ]
    prompt = engine._build_camp_prompt(attendees, jeep_included=True, ref_map=ref_map)
    low = prompt.lower()
    assert "reference image 1" in low and "jeep" in low
    assert "reference image 2" in low and "kane" in low
    assert "reference image 3" in low and "maya" in low
    assert "every named companion" in low or "none missing" in low


if __name__ == "__main__":
    test_resolve_image_path_finds_session_companion()
    test_collect_camp_companions_uses_state_and_disk()
    test_normalize_camp_leave_choice_forces_on_foot()
    test_camp_leave_image_prompt_forbids_cabin_pov()
    test_build_camp_prompt_maps_every_reference()
    print("ok")
