"""Where the player's things live, apart from where the program lives.

A packaged build used to write everything into its own folder: every module
finds its data with `Path(__file__).parent`, and the one-folder build puts that
beside the exe (SOMEWHERE.spec, `contents_directory="."`). The installer
(Velopack, M4) replaces that folder on every update, so every save, run, World
and tape would go with it. (docs/plans/DISTRIBUTION_MVP_PLAN.md, M2.)

So there are two roots:

- `install_root()` — the program and its factory content (templates, static,
  the shipped Worlds, the factory prompt file). Replaced by updates; may be
  read-only.
- `data_root()` — everything the game writes. %APPDATA%\\ABYSS in a packaged
  build; the repo itself when run from source, so development is unchanged.
  SOMEWHERE_DATA_ROOT overrides it (tests, tools/smoke_exe.py).

`prepare()` runs once at launch, before `api` is imported: it moves a player's
old %APPDATA%\\SOMEWHERE folder over (once), seeds the factory files the game
also rewrites, and points the stores' existing overrides at the data root.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Dict, Iterable, Optional

import app_identity

FROZEN = bool(getattr(sys, "frozen", False))
_INSTALL = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent
ENV = "SOMEWHERE_DATA_ROOT"

# What the game rewrites that the build also ships a factory copy of. Seeded
# into the data root; refreshed on update only while the player's copy is
# still exactly the factory copy it was seeded from (FACTORY_MANIFEST).
FACTORY_FILES = (
    "prompts/simulation_prompts.json",
    "tunables.json",
    "ai_config.json",
    "pricing.json",
)
FACTORY_TREES = (
    "worlds",
    "experiences",
)
FACTORY_MANIFEST = ".factory.json"

# Folders the game writes into, created at the data root.
WRITABLE = ("sessions", "logs", "archives", "worlds", "experiences", "levels",
            "assets/references", "assets/music", "playtest_results",
            "characters", "bugs", "images")


def install_root() -> Path:
    return _INSTALL


def data_root() -> Path:
    raw = (os.environ.get(ENV) or "").strip()
    if raw:
        return Path(raw)
    if FROZEN:
        return app_identity.data_dir()
    return _INSTALL


def data(*parts: str) -> Path:
    return data_root().joinpath(*parts)


# ── seeding ─────────────────────────────────────────────────────────────

def _sha(p: Path) -> Optional[str]:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def _factory_files(src_root: Path) -> Iterable[str]:
    for rel in FACTORY_FILES:
        if (src_root / rel).is_file():
            yield rel
    for tree in FACTORY_TREES:
        base = src_root / tree
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*")):
            if f.is_file() and f.name != ".active":
                yield f.relative_to(src_root).as_posix()


def seed_factory(src_root: Path, dest_root: Path) -> Dict[str, str]:
    """Copy factory files the game rewrites into the data root.

    Missing → copied. Present and still byte-identical to the factory copy we
    last put there → replaced by the new factory copy (an update's fixes land).
    Present and changed by the player → left alone. Returns {rel: action}.
    """
    if src_root.resolve() == dest_root.resolve():
        return {}
    man_path = dest_root / FACTORY_MANIFEST
    try:
        manifest = json.loads(man_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    actions: Dict[str, str] = {}
    for rel in _factory_files(src_root):
        src, dst = src_root / rel, dest_root / rel
        new = _sha(src)
        have = _sha(dst)
        if have is None:
            action = "seeded"
        elif have == new:
            manifest[rel] = new
            continue
        elif manifest.get(rel) == have:
            action = "refreshed"
        else:
            continue  # the player's own
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".seed")
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        manifest[rel] = new
        actions[rel] = action
    dest_root.mkdir(parents=True, exist_ok=True)
    tmp = man_path.with_name(man_path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, man_path)
    return actions


# ── the stores' own overrides ───────────────────────────────────────────

def store_env(root: Path) -> Dict[str, str]:
    """The env overrides the stores already honour, pointed at `root`."""
    return {
        "SESSIONS_DIR": str(root / "sessions"),
        "SOMEWHERE_WORLDS_DIR": str(root / "worlds"),
        "SOMEWHERE_EXPERIENCES_DIR": str(root / "experiences"),
        "SOMEWHERE_CHARACTERS_DIR": str(root / "characters"),
        "SOMEWHERE_PROMPTS_PATH": str(root / "prompts" / "simulation_prompts.json"),
        "SOMEWHERE_TUNABLES_PATH": str(root / "tunables.json"),
        "REFERENCES_DIR": str(root / "assets" / "references"),
        "SOMEWHERE_ANALYTICS_DIR": str(root / "sessions" / "_analytics"),
    }


def prepare() -> Path:
    """Once per launch, before `api` is imported. Returns the data root."""
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    for rel in WRITABLE:
        (root / rel).mkdir(parents=True, exist_ok=True)
    if root.resolve() != install_root().resolve():
        try:
            done = seed_factory(install_root(), root)
            for rel, action in sorted(done.items()):
                print(f"[paths] {action} {rel}")
        except Exception as e:  # noqa: BLE001 — a failed seed must not stop the game
            print(f"[paths] factory seed failed: {e}")
        for k, v in store_env(root).items():
            os.environ.setdefault(k, v)
    return root
