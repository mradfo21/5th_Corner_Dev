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
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List

import prompts_store

ROOT = Path(__file__).parent.resolve()
import authoring_sandbox as _sandbox
_sandbox.guard()  # before the path below is computed — see authoring_sandbox
# Overridable so a test run writes to a copy — see prompts_store.PROMPTS_PATH.
WORLDS_DIR = Path(os.getenv("SOMEWHERE_WORLDS_DIR") or (ROOT / "worlds"))
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
    # A World born from the harness carries a gutted rulebook (see DOCTRINE_KEYS).
    # Binding it used to stamp that over the live prompt file, on every bind and
    # every reset, so the pollution reinstalled itself each run. Substitute the
    # factory copy instead of dropping the key: a World the editor created has no
    # other source for these, and leaving the live value alone would make what the
    # game plays depend on what was loaded before it.
    gutted = harness_doctrine_in(fields)
    if gutted:
        factory = prompts_store.load_defaults()
        for key in gutted:
            replacement = factory.get(key)
            if isinstance(replacement, str) and replacement.strip():
                fields[key] = replacement
            else:
                fields.pop(key, None)
        print(f"[WORLDS] '{_slug(slug)}' carries the generic harness copy of "
              f"{', '.join(gutted)} — restored from the factory defaults rather "
              f"than playing the test fixture. Author these in the editor to "
              f"make the substitution stop.", flush=True)
    if fields:
        prompts_store.save_prompts_bulk(fields)
    return {"slug": _slug(slug), "name": data.get("name", slug),
            "applied": len(fields), "doctrine_restored": gutted}


# THE CAST IS THE RUN'S, NOT THE ROOM'S.
#
# Every World froze its own copy of `player_character`, and binding one writes
# that copy over the live sheet. So a character recast in the editor only ever
# lived in whichever World was snapshotted afterwards: `somewhere` and `yard`
# still held the shipped Jason with no plate while `world` held the authored
# photojournalist with one — and an Experience that hops World A -> B swapped
# protagonist mid-run. Reported as "sometimes I see Kelsey for a frame, then
# Jason from the editor", and as "it worked, sometimes".
#
# Snapshotting still wins over unsaved scratch (that is deliberate — see
# test_a_saved_look_survives_the_play_reset). What changes is the SCOPE of the
# save: a World is a place, the person walking through it belongs to the run,
# so saving the cast saves it into every room at once.
CAST_KEYS = ("player_character",)


# THE RULEBOOK IS THE GAME'S, NOT THE ROOM'S — AND NEVER THE TEST HARNESS'S.
#
# Reported as *"it feels random sometimes like the features are either trying to
# work and break or the prompts get randomly generated incorrectly"*, and it was
# not randomness. Measured on the live prompt file during that report:
#
#   action_consequence_instructions           451 chars   (authored: 15,461)
#   player_choice_generation_instructions      364 chars   (authored:  4,528)
#   narrator_direction                         533 chars   (authored:  3,681)
#
# Byte-identical to `prompts/harness.generic.json`. The game was playing the test
# fixture: no fairness doctrine, no death rules, no tension rhythm, no stillness
# beats, no choice-slate doctrine.
#
# It was not a test run that did it, and that is the part worth understanding.
# `_blank_prompts` deliberately seeds a NEW World from the harness so it does not
# inherit whatever happens to be loaded into Play — which is the right instinct
# about the wrong set of keys. `world_initial_state`, the cast, the setting and
# the camera genuinely belong to a World and should start blank. These three do
# not. They are how the GAME works, identical in every world that ever ships, and
# a blank one is not an empty room waiting to be authored — it is the rulebook
# deleted.
#
# So every World made in the editor was born holding a gutted rulebook, and
# `load_world` stamps a World's prompts onto the live file on every bind and
# every reset. The pollution therefore reinstalled itself on each new run, which
# is exactly why it read as intermittent rather than as a broken file.
#
# Two guards, because the born-wrong worlds already exist on disk:
#   * `_blank_prompts` takes these from the factory defaults.
#   * `load_world` refuses a stored value that is byte-identical to the harness
#     fixture and says so. Exact-match only — a World that genuinely authors its
#     own consequence doctrine (somewhere.json and world.json both do, at ~15,000
#     chars) is untouched.
DOCTRINE_KEYS = (
    "action_consequence_instructions",
    "player_choice_generation_instructions",
    "narrator_direction",
)


def _harness_prompts() -> Dict[str, Any]:
    """The generic test/authoring harness, or {} if it is missing."""
    if not HARNESS_PATH.is_file():
        return {}
    try:
        data = json.loads(HARNESS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _lines(text: str) -> List[str]:
    return [l.strip() for l in str(text or "").splitlines() if l.strip()]


def _is_fixture_plus_game_lines(stored: str, fixture: str, factory: str) -> bool:
    """The fixture, plus only lines the factory copy of the same block carries.

    Exact equality missed the second way a World ends up playing the fixture.
    `untitled-experience` (the active one on the machine this was found on)
    carried `player_choice_generation_instructions` at 754 chars: the 364-char
    harness fixture with the ALREADY DONE repeat-suppression paragraph spliced
    into it — the paragraph the game rolled out across every prompt copy on
    09-18. Not authored, not the fixture byte-for-byte, and bound onto the live
    file on every New Game, so the slate ran with no doctrine at all ("EVERY
    CHOICE MUST ADVANCE THE ACTION", the randomized slot, the whole 4,500
    chars) while `test_choice_slot_is_randomized` went red the moment the
    game was played.

    Still exact, line for line: every fixture line has to be present, and every
    line that is NOT the fixture has to appear verbatim in the factory defaults
    for that key — that is what "the game wrote it" looks like. A single line an
    author typed ("Also: be kind.") is in neither and keeps the block authored.
    """
    s_lines = _lines(stored)
    f_lines = _lines(fixture)
    if not f_lines or not s_lines:
        return False
    fset = set(f_lines)
    if not fset.issubset(set(s_lines)):
        return False
    factory_lines = set(_lines(factory))
    extras = [l for l in s_lines if l not in fset]
    return bool(extras) and all(l in factory_lines for l in extras)


def harness_doctrine_in(prompts: Dict[str, Any]) -> List[str]:
    """Which rulebook blocks in `prompts` are the harness fixture.

    Exact string equality against the shipped fixture, plus the one decoration
    the game itself applies to every copy (see _is_fixture_plus_game_lines), so
    it cannot misfire on authored prose however short somebody writes it.
    """
    harness = _harness_prompts()
    try:
        factory = prompts_store.load_defaults()
    except Exception:
        factory = {}
    out: List[str] = []
    for key in DOCTRINE_KEYS:
        fixture = harness.get(key)
        if not isinstance(fixture, str) or not fixture.strip():
            continue
        stored = prompts.get(key)
        if not isinstance(stored, str):
            continue
        if stored.strip() == fixture.strip():
            out.append(key)
            continue
        fac = factory.get(key) if isinstance(factory, dict) else None
        if isinstance(fac, str) and _is_fixture_plus_game_lines(stored, fixture, fac):
            out.append(key)
    return out


def patch_world_prompts(slug: str, fields: Dict[str, Any]) -> bool:
    """Overwrite a few prompt keys inside a stored World, leaving the rest.

    Not `save_world`: that captures the whole live prompt file, which would
    overwrite every other World's own place with this one's.
    """
    if not fields:
        return False
    path = WORLDS_DIR / f"{_slug(slug)}.json"
    if not path.exists():
        return False
    try:
        data = _read_world(slug)
    except KeyError:
        return False
    prompts = data.get("prompts")
    if not isinstance(prompts, dict):
        return False
    if all(prompts.get(k) == v for k, v in fields.items()):
        return False
    prompts.update(fields)
    data["prompts"] = prompts
    data["updated"] = time.time()
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return True


def sync_cast_to_worlds(slugs: List[str]) -> int:
    """Write the live cast sheet into each of these Worlds. Returns how many
    actually changed."""
    live = dict(prompts_store.PROMPTS)
    fields = {k: live[k] for k in CAST_KEYS if k in live}
    if not fields:
        return 0
    changed = 0
    for slug in slugs or []:
        if not slug:
            continue
        try:
            if patch_world_prompts(slug, fields):
                changed += 1
        except (OSError, ValueError):
            continue
    return changed


def delete_world(slug: str) -> bool:
    path = WORLDS_DIR / f"{_slug(slug)}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def _blank_prompts() -> Dict[str, Any]:
    """Mechanical loop + empty place. Never the live Play file.

    "Blank" means an unauthored PLACE, not an unauthored GAME. The harness blanks
    the rulebook along with the room (see DOCTRINE_KEYS), and a new World holding
    a 451-character consequence doctrine is not waiting to be filled in — it has
    had the fairness rules, the death rules and the pacing deleted. Those come
    from the factory so a world created in the editor is playable on the day it
    is created.
    """
    data = _harness_prompts()
    if data.get("world_initial_state"):
        blank = {k: v for k, v in data.items()
                 if k in prompts_store.editable_keys(data) or k in data}
        factory = prompts_store.load_defaults()
        for key in DOCTRINE_KEYS:
            replacement = factory.get(key)
            if isinstance(replacement, str) and replacement.strip():
                blank[key] = replacement
        return blank
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
