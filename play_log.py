"""Append-only playtest log so a session can be judged after the fact.

Live play used to leave only stderr prints and a feed of prose. Encounter
cast swaps and invented hazards then had to be reverse-engineered from a
screenshot. Every begin / resolve / turn now writes one JSON line under
``logs/play/<session_id>.jsonl``.

Never raises into the game. If the disk is read-only, play continues.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
import paths as _paths  # where the game writes (M2): the repo from source, %APPDATA%/ABYSS built
PLAY_LOG_DIR = _paths.data_root() / "logs" / "play"


def play_log_path(session_id: str = "default") -> Path:
    safe = "".join(c for c in str(session_id or "default") if c.isalnum() or c in "-_") or "default"
    return PLAY_LOG_DIR / f"{safe}.jsonl"


def record(kind: str, session_id: str = "default", payload: Optional[dict] = None) -> Optional[Path]:
    """Append one event. Returns the file path, or None if write failed."""
    try:
        PLAY_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = play_log_path(session_id)
        event = {
            "ts": round(time.time(), 3),
            "kind": str(kind or "event"),
            "session_id": str(session_id or "default"),
        }
        if isinstance(payload, dict):
            event.update(payload)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        return path
    except Exception:
        return None


def diagnose_encounter(brief: Optional[dict], choices: Optional[list] = None) -> dict:
    """Flags a reviewer can scan without opening the stills."""
    flags = []
    brief = brief if isinstance(brief, dict) else {}
    char = brief.get("character") if isinstance(brief.get("character"), dict) else {}
    look = str(char.get("look") or "")
    label = str(char.get("label") or "")
    danger = str(brief.get("danger") or "")
    seen = str(brief.get("plate_seen") or "")
    try:
        import encounter
        if encounter.look_clones_player(look) or encounter.look_clones_player(label):
            flags.append("wardrobe_collision")
        if encounter.names_unseen_hazard(danger, seen):
            flags.append("unseen_hazard_in_danger")
        for item in choices or brief.get("choices") or []:
            text = item.get("text") if isinstance(item, dict) else item
            if encounter.names_unseen_hazard(str(text or ""), seen):
                flags.append("unseen_hazard_in_choices")
                break
    except Exception:
        pass
    return {"flags": flags, "label": label[:80], "look": look[:120], "danger": danger[:140]}
