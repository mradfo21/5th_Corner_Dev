"""
worlds_store.py — named, saveable "worlds": snapshots of the editable
simulation prompts you can fork, switch between, and re-instance live.

A "world" is just the current values of every editable prompt key (see
``prompts_store.editable_keys``) plus a little metadata. Saving captures the
live prompts; loading bulk-applies a world's prompts back into the live prompt
file — which ``prompts_store`` hot-reloads into the running engine, so the very
next turn (or a fresh run) plays under that world. Worlds are stored as
``worlds/<slug>.json`` so they're human-readable, diffable, and shareable.

This is the persistence layer behind the in-game WORLD EDITOR ("save our
world"): fork the desert-horror premise into "Sunken Station" or "Neon Bazaar",
name it, switch between them in seconds.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List

import prompts_store

ROOT = Path(__file__).parent.resolve()
WORLDS_DIR = ROOT / "worlds"


def _slug(name: str) -> str:
    """A filesystem-safe, stable id for a world name."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s[:64] or "world"


def _ensure_dir() -> None:
    WORLDS_DIR.mkdir(parents=True, exist_ok=True)


def _editable_snapshot() -> Dict[str, Any]:
    """The current LIVE values for every editable prompt key (everything the
    editor can touch), so a saved world round-trips the full prompt set — not
    just the handful the UI happens to surface."""
    prompts = dict(prompts_store.PROMPTS)
    return {k: prompts.get(k) for k in prompts_store.editable_keys(prompts)}


def list_worlds() -> List[Dict[str, Any]]:
    """Metadata for every saved world, newest-touched first."""
    _ensure_dir()
    out: List[Dict[str, Any]] = []
    for p in WORLDS_DIR.glob("*.json"):
        try:
            with p.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        out.append({
            "slug": p.stem,
            "name": data.get("name", p.stem),
            "note": data.get("note", ""),
            "created": data.get("created", 0),
            "updated": data.get("updated", 0),
            "field_count": len(data.get("prompts", {})),
        })
    out.sort(key=lambda w: w.get("updated", 0), reverse=True)
    return out


def save_world(name: str, note: str = "") -> Dict[str, Any]:
    """Snapshot the current live prompts as a named world (overwrites a world
    of the same slug, preserving its original created-at)."""
    if not (name or "").strip():
        raise ValueError("A world name is required.")
    _ensure_dir()
    slug = _slug(name)
    path = WORLDS_DIR / f"{slug}.json"
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
        "prompts": _editable_snapshot(),
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return {"slug": slug, "name": payload["name"]}


def _read_world(slug: str) -> Dict[str, Any]:
    path = WORLDS_DIR / f"{_slug(slug)}.json"
    if not path.exists():
        raise KeyError(f"World '{slug}' not found.")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def get_world(slug: str) -> Dict[str, Any]:
    """Full stored payload for one world (metadata + prompts)."""
    data = _read_world(slug)
    data["slug"] = _slug(slug)
    return data


def load_world(slug: str) -> Dict[str, Any]:
    """Apply a saved world's prompts to the live prompt file (hot-reloads into
    the running engine). Only keys that are still editable are applied, so a
    stale world can never inject unknown keys."""
    data = _read_world(slug)
    stored = data.get("prompts") or {}
    known = set(prompts_store.editable_keys())
    fields = {k: v for k, v in stored.items() if k in known}
    if fields:
        prompts_store.save_prompts_bulk(fields)
    return {"slug": _slug(slug), "name": data.get("name", slug), "applied": len(fields)}


def delete_world(slug: str) -> bool:
    path = WORLDS_DIR / f"{_slug(slug)}.json"
    if path.exists():
        path.unlink()
        return True
    return False
