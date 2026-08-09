"""
levels_store.py — named, saveable LEVELS.

A *world* (see ``worlds_store``) snapshots every editable prompt key: the
engine's contracts, the game's identity, the level, and the cast, all at once.
That is the right unit for "save the whole build", and the wrong unit for "make
a level" — loading one replaces your engine tuning and your character along with
the place, so there was no way to build three rooms for the same game.

A *level* is the LEVEL LAYER only (``prompt_layers.LEVEL_KEYS``): the level brief
and the setting plate. Saving captures just those; loading applies just those.
Everything else — how a turn resolves, what genre this is, who you play — is left
exactly as it was, so you can author a set of levels and walk the same character
through each of them.

Stored as ``levels/<slug>.json``: human-readable, diffable, shareable.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List

import prompt_layers
import prompts_store

ROOT = Path(__file__).parent.resolve()
LEVELS_DIR = ROOT / "levels"


def _slug(name: str) -> str:
    """A filesystem-safe, stable id for a level name."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s[:64] or "level"


def _ensure_dir() -> None:
    LEVELS_DIR.mkdir(parents=True, exist_ok=True)


def level_keys() -> List[str]:
    """The keys a level owns — intersected with what's actually editable now, so
    a level file can never carry a key the current build doesn't recognise."""
    known = set(prompts_store.editable_keys())
    return [k for k in prompt_layers.LEVEL_KEYS if k in known]


def _snapshot() -> Dict[str, Any]:
    prompts = dict(prompts_store.PROMPTS)
    return {k: prompts.get(k) for k in level_keys()}


def _summarize(fields: Dict[str, Any]) -> Dict[str, Any]:
    """The at-a-glance card for a level: what place is this, and is it armed.

    Read from the setting plate rather than the brief because the plate is the
    structured half — a name and a one-line summary make a browsable gallery,
    where the first 80 characters of a 1200-word brief do not.
    """
    setting = fields.get("setting_reference")
    setting = setting if isinstance(setting, dict) else {}
    brief = fields.get("world_initial_state")
    return {
        "place": (setting.get("name") or "").strip(),
        "summary": (setting.get("summary") or "").strip()[:180],
        "era": (setting.get("era") or "").strip(),
        "enabled": bool(setting.get("enabled")),
        "has_opening_shot": bool((setting.get("opening_shot") or "").strip()),
        "brief_chars": len(brief) if isinstance(brief, str) else 0,
        "plate_count": len(setting.get("reference_images") or []),
    }


def list_levels() -> List[Dict[str, Any]]:
    """Metadata for every saved level, newest-touched first."""
    _ensure_dir()
    out: List[Dict[str, Any]] = []
    for p in LEVELS_DIR.glob("*.json"):
        try:
            with p.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        entry = {
            "slug": p.stem,
            "name": data.get("name", p.stem),
            "note": data.get("note", ""),
            "created": data.get("created", 0),
            "updated": data.get("updated", 0),
        }
        entry.update(_summarize(data.get("level") or {}))
        out.append(entry)
    out.sort(key=lambda w: w.get("updated", 0), reverse=True)
    return out


def save_level(name: str, note: str = "") -> Dict[str, Any]:
    """Snapshot the current LEVEL layer as a named level.

    Overwrites a level of the same slug, preserving its original created-at.
    """
    if not (name or "").strip():
        raise ValueError("A level name is required.")
    _ensure_dir()
    slug = _slug(name)
    path = LEVELS_DIR / f"{slug}.json"
    now = time.time()
    created = now
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                created = json.load(f).get("created", now)
        except Exception:
            pass
    payload = {
        "name": name.strip(),
        "note": (note or "").strip(),
        "created": created,
        "updated": now,
        # Named `level` rather than `prompts` so a level file can never be
        # mistaken for a world snapshot by a loader that only checks shape.
        "level": _snapshot(),
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    result = {"slug": slug, "name": payload["name"]}
    result.update(_summarize(payload["level"]))
    return result


def _read_level(slug: str) -> Dict[str, Any]:
    path = LEVELS_DIR / f"{_slug(slug)}.json"
    if not path.exists():
        raise KeyError(f"Level '{slug}' not found.")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def get_level(slug: str) -> Dict[str, Any]:
    """Full stored payload for one level (metadata + level-layer values)."""
    data = _read_level(slug)
    data["slug"] = _slug(slug)
    return data


def load_level(slug: str) -> Dict[str, Any]:
    """Apply a saved level to the live prompt file, touching ONLY level keys.

    Hot-reloads into the running engine, so the next fresh run opens in this
    level. Keys outside the level layer are ignored even if a hand-edited file
    contains them — that guarantee is the whole reason levels exist separately
    from worlds.
    """
    data = _read_level(slug)
    stored = data.get("level") or {}
    allowed = set(level_keys())
    fields = {k: v for k, v in stored.items() if k in allowed}
    if fields:
        prompts_store.save_prompts_bulk(fields)
    return {
        "slug": _slug(slug),
        "name": data.get("name", slug),
        "applied": sorted(fields.keys()),
    }


def delete_level(slug: str) -> bool:
    path = LEVELS_DIR / f"{_slug(slug)}.json"
    if path.exists():
        path.unlink()
        return True
    return False
