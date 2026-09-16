#!/usr/bin/env python3
"""Pare the default style back to one medium statement. Run from repo root.

Three things reached the image model and argued with each other: an art
direction block whose only mention of the year sat on a line the recast filter
deleted, a carried-gear list naming a VHS camcorder, and 268 words of negative
prompt that the OpenAI path appends as plain text. This rewrites all three
across the shipped defaults, the live prompt file, the blank-world harness, and
every world snapshot.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import prompt_layers  # noqa: E402

# Engine-owned prompts a world snapshot froze an older copy of. `prompt_layers`
# calls these ENGINE, meaning the engine owns them and a world has no business
# holding its own version — but `load_world` copies a snapshot's whole prompt map
# over the live file, so a stale snapshot silently reinstates retired text. The
# active world was still handing the encounter plate "restage this from a new
# lens", which is the register break that made walking into a fight look like a
# cut to a different film.
RESYNC_FROM_DEFAULTS = (
    "encounter_plate_anchor",
    "encounter_choice_overlay",
    "encounter_choice_instructions",
)

# No place name anywhere in here. `game_identity.strip_shipped_place` removes
# shipped-biome sentences from a recast level, and the year has to survive that
# — it is the only thing in the payload that fixes the medium.
ART_DIRECTION = """MEDIUM
A photograph taken in 1993 on consumer colour film. Available light only, muted colour, fine grain, soft contrast. Framing is a little imperfect, the way a single frame taken by someone who was standing there is imperfect. A photograph of a real place.

WORLD
Period technology only: CRT monitors, fluorescent tubes, chain-link, rusted plant, 1990s trucks. Threats are human or biological, never mechanical. Touchstones: The X-Files, Twin Peaks, The Thing.

HORROR
Physical effects only: prosthetics, latex, stage blood. Injuries look like injuries."""

# Was 268 words, and 60 of them banned bodies and perspectives — which is
# `PERSPECTIVE_MODES[mode]["negative_add"]`'s job. Static perspective bans have
# to be stripped back out by `game_identity.negative_prompt` on every
# third-person call, and anything the strip misses argues with the camera the
# player selected. Medium and content only here.
NEGATIVE_PROMPT = (
    "ABSOLUTELY NOT: 3D render, CGI, video game screenshot, digital illustration, "
    "cartoon, painting, stylized art, studio lighting, professional retouching. "
    "NEVER: robots, androids, sci-fi or futuristic technology, holograms, neon, "
    "glowing screens. "
    "NO GRAPHICS: captions, subtitles, timecode, timestamps, watermarks, logos, "
    "HUD, crosshair, letterbox, pillarbox, black borders."
)

SIGNATURE_GEAR = "a battered 35mm stills camera on a neck strap"

# Substring rewrites for prose that still hands the image model a tape deck.
REPLACEMENTS = (
    (
        "'raise/lift the camcorder', 'bring the Panasonic / Handycam / AG-450 to "
        "your eye', 'get it on tape'",
        "'raise the camera', 'get a shot of it'",
    ),
    ("'tuck the camera to your chest', ", ""),
    (
        "Write how the world should photograph \u2014 camcorder / viewfinder / "
        "tape-HUD language burns interface into the frame.",
        "Write how the world should photograph. Never name the recording device: "
        "'camcorder', 'VHS' and 'viewfinder' burn tape timestamps and interface "
        "into the frame.",
    ),
)

FLAT = (
    "prompts/simulation_prompts.defaults.json",
    "prompts/simulation_prompts.json",
    "prompts/harness.generic.json",
)
NESTED = (
    "worlds/somewhere.json",
    "worlds/somewhere-fp.json",
    "worlds/world.json",
)


def resync(prompts: dict, defaults: dict) -> list[str]:
    """Pull engine-owned keys back to the shipped text where a world froze them."""
    changed: list[str] = []
    for key in RESYNC_FROM_DEFAULTS:
        assert prompt_layers.layer_of(key) == prompt_layers.ENGINE, key
        want = defaults.get(key)
        if isinstance(want, str) and key in prompts and prompts[key] != want:
            prompts[key] = want
            changed.append(key)
    return changed


def patch(prompts: dict) -> list[str]:
    changed: list[str] = []
    if "image_art_direction" in prompts and prompts["image_art_direction"] != ART_DIRECTION:
        prompts["image_art_direction"] = ART_DIRECTION
        changed.append("image_art_direction")
    if "image_negative_prompt" in prompts and prompts["image_negative_prompt"] != NEGATIVE_PROMPT:
        prompts["image_negative_prompt"] = NEGATIVE_PROMPT
        changed.append("image_negative_prompt")

    who = prompts.get("player_character")
    if isinstance(who, dict) and "camcorder" in (who.get("signature_gear") or "").lower():
        who["signature_gear"] = SIGNATURE_GEAR
        changed.append("player_character.signature_gear")

    for key, value in list(prompts.items()):
        if not isinstance(value, str):
            continue
        out = value
        for old, new in REPLACEMENTS:
            out = out.replace(old, new)
        if out != value:
            prompts[key] = out
            changed.append(key)
    return changed


def main() -> int:
    defaults_path = ROOT / "prompts/simulation_prompts.defaults.json"
    for rel, nested in [(p, False) for p in FLAT] + [(p, True) for p in NESTED]:
        path = ROOT / rel
        if not path.is_file():
            print(f"skip (missing) {rel}")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        target = data.get("prompts") if nested else data
        if not isinstance(target, dict):
            print(f"skip (no prompt map) {rel}")
            continue
        changed = patch(target)
        # Read the defaults back off disk each time: this loop patches them
        # first, so the resync source is already the new text.
        if path != defaults_path:
            changed += resync(target, json.loads(defaults_path.read_text(encoding="utf-8")))
        if changed:
            path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        print(f"{rel}: {', '.join(changed) if changed else 'no change'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
