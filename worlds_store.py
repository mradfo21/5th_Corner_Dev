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
HARNESS_PATH = ROOT / "prompts" / "harness.generic.json"


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
        # Sidecars from world_frames (world-3.frame.json) are not worlds.
        if p.name.endswith(".frame.json"):
            continue
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


def save_world(name: str, note: str = "", slug: str = "") -> Dict[str, Any]:
    """Snapshot the current live prompts as a named world (overwrites a world
    of the same slug, preserving its original created-at).

    ``slug`` keeps the write on the file this World already owns. Deriving
    it from the display name is how a rename forked a second file and left
    REDRAW reading the old one.
    """
    if not (name or "").strip():
        raise ValueError("A world name is required.")
    _ensure_dir()
    slug = _slug(slug) if str(slug or "").strip() else _slug(name)
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


def _blank_prompts() -> Dict[str, Any]:
    """Mechanical loop + empty place. Never the live Play file."""
    if HARNESS_PATH.is_file():
        try:
            data = json.loads(HARNESS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("world_initial_state"):
                return {k: v for k, v in data.items() if k in prompts_store.editable_keys(data) or k in data}
        except Exception:
            pass
    prompts = _editable_snapshot()
    prompts["world_initial_state"] = (
        "A playable place in third person. The camera follows a person through space. "
        "Each action moves the body or changes the room."
    )
    prompts["player_character"] = {
        "enabled": True,
        "name": "the traveler",
        "pronouns": "they/them",
        "role": "someone arriving",
        "appearance": "adult, short dark hair, unremarkable face",
        "wardrobe": "dark jacket, trousers, boots, a rust-red bandana at the throat",
        "signature_gear": "a scuffed 35mm stills camera on a frayed strap",
        "demeanor": "",
        "backstory": "",
        "reference_images": [],
    }
    prompts["setting_reference"] = {
        "enabled": True,
        "name": "an open place",
        "summary": "A navigable space you can walk through.",
        "era": "",
        "palette": "",
        "landmarks": "",
        "opening_shot": "A person stands in an open place. The camera sees their whole body.",
        "reference_images": [],
    }
    prompts["camera_perspective"] = {
        "mode": "third_person",
        "show_hands": False,
        "lens": "",
        "notes": "",
    }
    return prompts


def create_blank_world(name: str, note: str = "") -> Dict[str, Any]:
    """A new World from the generic harness. Does not snapshot Play."""
    if not (name or "").strip():
        raise ValueError("A world name is required.")
    _ensure_dir()
    slug = _slug(name)
    path = WORLDS_DIR / f"{slug}.json"
    now = time.time()
    payload = {
        "name": name.strip(),
        "note": (note or "").strip() or "Blank third-person harness",
        "created": now,
        "updated": now,
        "prompts": _blank_prompts(),
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return {"slug": slug, "name": payload["name"]}


def clone_world(source_slug: str, new_name: str) -> Dict[str, Any]:
    """Copy an existing world's prompt snapshot under a new name.

    Does not touch the live prompt file — the Experience graph forks identities
    without loading them into the editor.
    """
    if not (new_name or "").strip():
        raise ValueError("A world name is required.")
    data = _read_world(source_slug)
    _ensure_dir()
    slug = _slug(new_name)
    path = WORLDS_DIR / f"{slug}.json"
    now = time.time()
    payload = {
        "name": new_name.strip(),
        "note": data.get("note", ""),
        "created": now,
        "updated": now,
        "prompts": dict(data.get("prompts") or {}),
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return {"slug": slug, "name": payload["name"]}
