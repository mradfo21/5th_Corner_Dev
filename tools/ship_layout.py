"""What a distributable SOMEWHERE folder is allowed to contain.

The desktop build used to copy whatever was on the operator's machine
(``prompts/simulation_prompts.json``, every World in ``worlds/``) and omit
the shipped SOMEWHERE Experience. That is how a folder you can move to
another computer stops being the game.

This module is the single list. ``SOMEWHERE.spec`` and ``tools/build_exe.py``
both read it. Author Worlds, ``experiences/.active``, sessions, keys, and
playtest output never go in the folder.

Runtime modules stay at the repo root on purpose: every path in this app is
``Path(__file__).parent``. Do not move ``engine.py`` into a package without
rewriting that contract.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Iterable, List, Tuple

ROOT = Path(__file__).resolve().parent.parent

# Lazy imports the PyInstaller walker cannot see from play.py.
RUNTIME_MODULES: Tuple[str, ...] = (
    "ai_provider_manager",
    "api",
    "api_client",
    "autoplay",
    "billing",
    "choices",
    "coinop",
    "cost_tracker",
    "engine",
    "evolve_prompt_file",
    "experience_store",
    "fal_image_utils",
    "game_identity",
    "gemini_image_utils",
    "gemini_live_talk",
    "gemini_live_vision",
    "items",
    "keys_store",
    "krea_image_utils",
    "levels_store",
    "local_vision",
    "lore_cache_manager",
    "presence",
    "pricing",
    "prompt_layers",
    "prompts_store",
    "render_jobs",
    "run_local",
    "scene_audio",
    "tunables",
    "usage_limits",
    "veo_video_utils",
    "voice_design",
    "world_frames",
    "worlds_store",
)

# Folders Flask and the engine read by relative path. Never glob worlds/ or
# experiences/ here — those directories hold this machine's authoring.
BUNDLE_TREES: Tuple[Tuple[str, str], ...] = (
    ("templates", "templates"),
    ("static", "static"),
    ("prompts", "prompts"),
    ("models", "models"),
)

BUNDLE_FILES: Tuple[Tuple[str, str], ...] = (
    ("ai_config.json", "."),
    ("voices.json", "."),
    ("pricing.json", "."),
    ("automation.json", "."),
)

# The Phase 0 freeze. Play's factory door when experiences/.active is absent.
FACTORY_FILES: Tuple[str, ...] = (
    "worlds/somewhere.json",
    "worlds/somewhere.frame.png",
    "worlds/somewhere.frame.json",
    "experiences/somewhere.json",
    "experiences/.gitkeep",
    "worlds/.gitkeep",
)

WRITABLE_DIRS: Tuple[str, ...] = (
    "sessions",
    "logs",
    "archives",
    "worlds",
    "experiences",
    "levels",
    "lore/images",
    "lore/text",
    "assets/references",
    "assets/music",
    "playtest_results",
)


def _existing(rel: str) -> Path | None:
    path = ROOT / rel
    return path if path.exists() else None


def runtime_modules() -> List[str]:
    return [name for name in RUNTIME_MODULES if (ROOT / f"{name}.py").is_file()]


def bundle_datas() -> List[Tuple[str, str]]:
    """PyInstaller ``datas`` entries for content that is safe to copy as-is."""
    out: List[Tuple[str, str]] = []
    for src, dest in BUNDLE_TREES:
        if (ROOT / src).exists():
            out.append((src, dest))
    for src, dest in BUNDLE_FILES:
        if (ROOT / src).is_file():
            out.append((src, dest))
    for rel in FACTORY_FILES:
        path = _existing(rel)
        if path and path.is_file():
            out.append((rel.replace("\\", "/"), str(Path(rel).parent).replace("\\", "/")))
    return out


def factory_sources() -> List[Path]:
    return [p for rel in FACTORY_FILES if (p := _existing(rel)) and p.is_file()]


def write_factory_prompts(dest_root: Path) -> Path:
    """Live prompt file for a clean install: defaults + the SOMEWHERE snapshot.

    The operator's ``simulation_prompts.json`` is authoring state. Shipping it
    would put this machine's New Level leftovers in every build.
    """
    prompts_dir = dest_root / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    defaults_src = ROOT / "prompts" / "simulation_prompts.defaults.json"
    defaults = json.loads(defaults_src.read_text(encoding="utf-8")) if defaults_src.is_file() else {}
    world_src = ROOT / "worlds" / "somewhere.json"
    world = json.loads(world_src.read_text(encoding="utf-8")) if world_src.is_file() else {}
    merged = dict(defaults)
    merged.update(world.get("prompts") or {})
    dest = prompts_dir / "simulation_prompts.json"
    dest.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if defaults_src.is_file():
        shutil.copy2(defaults_src, prompts_dir / "simulation_prompts.defaults.json")
    return dest


def stamp_factory(dest_root: Path) -> List[str]:
    """Copy factory content into a built folder and strip author leftovers."""
    dest_root = Path(dest_root)
    copied: List[str] = []
    for src in factory_sources():
        rel = src.relative_to(ROOT)
        target = dest_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied.append(str(rel).replace("\\", "/"))
    write_factory_prompts(dest_root)
    copied.append("prompts/simulation_prompts.json")
    active = dest_root / "experiences" / ".active"
    if active.exists():
        active.unlink()
    for leftover in ("default.json", "untitled-experience.json"):
        junk = dest_root / "experiences" / leftover
        if junk.exists():
            junk.unlink()
    for rel in WRITABLE_DIRS:
        (dest_root / rel).mkdir(parents=True, exist_ok=True)
    return copied
