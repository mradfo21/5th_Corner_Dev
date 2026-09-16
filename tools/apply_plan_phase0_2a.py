#!/usr/bin/env python3
"""Phase 0 residuals + Phase 2a camera strip. Run from repo root."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import experience_store as xs
import worlds_store as ws

ROOT = Path(__file__).resolve().parent.parent

CAMERA_KEYS = (
    "action_consequence_instructions",
    "image_camera_rules",
    "image_negative_prompt",
    "gemini_flipbook_4panel_prefix",
    "gemini_text_to_image_instructions",
    "gemini_image_to_image_instructions",
    "player_choice_generation_instructions",
)

REPLACEMENTS = (
    ("16-FRAME CONTINUOUS FIRST-PERSON SEQUENCE", "16-FRAME CONTINUOUS SEQUENCE"),
    ("continuous first-person action sequence", "continuous action sequence"),
    ("first-person camera", "the active camera"),
    ("first-person feel", "embodied feel"),
    ("FIRST-PERSON SEQUENCE", "CONTINUOUS SEQUENCE"),
    (
        "third person perspective, over shoulder view, behind character, following someone. ",
        "",
    ),
    (
        "third person perspective, over shoulder view, behind character, following someone.",
        "",
    ),
)


def _strip_camera(text: str) -> str:
    out = text
    for old, new in REPLACEMENTS:
        out = out.replace(old, new)
    return out


def _patch_prompt_map(data: dict) -> int:
    n = 0
    for key in CAMERA_KEYS:
        raw = data.get(key)
        if not isinstance(raw, str):
            continue
        nxt = _strip_camera(raw)
        if nxt != raw:
            data[key] = nxt
            n += 1
    return n


def snapshot_fp_rollback() -> Path:
    dest = ROOT / "worlds" / "somewhere-fp.json"
    if dest.exists():
        print("FP rollback already exists:", dest)
        return dest
    info = ws.save_world("SOMEWHERE FP", note="Phase 0 first-person rollback", slug="somewhere-fp")
    print("wrote FP rollback", info)
    return dest


def lock_play_to_somewhere() -> None:
    xs.set_active("somewhere")
    applied = ws.load_world("somewhere")
    print("Play .active ->", xs.get_active_slug(), "applied", applied)


def snapshot_opening_frame() -> None:
    src = ROOT / "worlds" / "new-level.frame.png"
    dst = ROOT / "worlds" / "somewhere.frame.png"
    if dst.exists():
        print("opening frame already present")
        return
    if src.is_file():
        shutil.copy2(src, dst)
        side = ROOT / "worlds" / "new-level.frame.json"
        if side.is_file():
            shutil.copy2(side, ROOT / "worlds" / "somewhere.frame.json")
        print("copied opening frame from new-level plate")
    else:
        print("no new-level.frame.png to copy")


def patch_json_file(path: Path, *, world: bool) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    target = data.get("prompts") if world else data
    if not isinstance(target, dict):
        return 0
    n = _patch_prompt_map(target)
    if n:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return n


def main() -> int:
    snapshot_fp_rollback()
    snapshot_opening_frame()
    for rel, world in (
        ("prompts/simulation_prompts.defaults.json", False),
        ("worlds/somewhere.json", True),
    ):
        n = patch_json_file(ROOT / rel, world=world)
        print(f"stripped {n} camera keys in {rel}")
    lock_play_to_somewhere()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
