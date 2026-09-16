"""Regenerate whatever the world has not been told, and say what you did.

The editor's promise is that a world fills itself in: supply nothing and it drafts
defaults, attach a reference image and it drafts from that, press re-draw and it
does it again. The promise held per-field, in one place, for one source — a plate
attached to a character or level sheet — and nowhere else. Everything the schema
gained since then stayed blank on any world authored in words:
`setting_reference.goal` was "", so the opening montage told the player they had
come here to reach a landmark they were standing at; `camera_perspective.lens` and
`.notes` were blank and could not be filled by any path at all, because every fill
route was gated on supporting images and a camera has no photograph to read.

So this walks the whole surface at once and reports every field it touched and
where the answer came from. The reporting is the point as much as the filling: a
regeneration you cannot see is indistinguishable from one that did not happen, and
that is exactly how the world stayed hollow through a dozen runs.

    python tools/regenerate_world.py            # show what is missing
    python tools/regenerate_world.py --apply    # draft it
    python tools/regenerate_world.py --apply --overwrite   # redraw everything
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# Music is not part of the identity spec — it lives in scene_audio as one string —
# but it is authored in the same editor and has the same contract, so it is
# regenerated here rather than being the one field nobody covers.
MUSIC_PROMPT = (
    "In one short phrase, name the score for this world: instrumentation, era and "
    "mood, as a composer brief. No sentences, no explanation, at most 12 words. "
    "Example shape: 'sparse analog synth drones, 1993, dread under silence'."
)


def _blocks() -> List[str]:
    try:
        import game_identity
        return list(game_identity.text_fillable_blocks())
    except Exception:
        return []


def plan() -> Dict[str, Any]:
    """What is currently unauthored, without changing anything."""
    out: Dict[str, Any] = {"blocks": {}, "music": {}, "missing": 0}
    try:
        import game_identity
        for block in _blocks():
            empty = game_identity.empty_fill_fields(block)
            has_plate = bool((game_identity.get_spec().get(block) or {})
                             .get("reference_images"))
            out["blocks"][block] = {"empty": empty, "has_plate": has_plate}
            out["missing"] += len(empty)
    except Exception as err:
        out["error"] = str(err)

    try:
        import scene_audio
        current = str(scene_audio.get_music_direction() or "").strip()
        out["music"] = {"direction": current, "empty": not current}
        if not current:
            out["missing"] += 1
    except Exception as err:
        out["music"] = {"error": str(err)}
    return out


def _regen_music(overwrite: bool) -> Dict[str, Any]:
    try:
        import scene_audio
    except Exception as err:
        return {"skipped": True, "reason": f"unavailable: {err}"}
    try:
        current = str(scene_audio.get_music_direction() or "").strip()
    except Exception:
        current = ""
    if current and not overwrite:
        return {"skipped": True, "reason": "already_written", "direction": current}

    bible = ""
    try:
        import prompts_store
        bible = str(prompts_store.PROMPTS.get("world_initial_state") or "")
    except Exception:
        bible = ""
    if len(bible.strip()) < 200:
        return {"skipped": True, "reason": "no_world_to_read"}

    try:
        import engine
        answer = engine._ask(
            f"{MUSIC_PROMPT}\n\nTHE WORLD:\n{bible[:4000]}",
            temp=0.9, tokens=60, use_lore=False,
        )
    except Exception as err:
        return {"skipped": True, "reason": f"ask_failed: {err}"}

    answer = " ".join(str(answer or "").split()).strip().strip('"').rstrip(".")
    # A model that answers with a paragraph has misunderstood the brief; a score
    # note that long is not usable as one and would be worse than the default.
    if not answer or len(answer) > 160:
        return {"skipped": True, "reason": "nothing_usable"}
    try:
        scene_audio.set_music_direction(answer)
    except Exception as err:
        return {"skipped": True, "reason": f"save_failed: {err}"}
    return {"skipped": False, "direction": answer, "was": current, "source": "text"}


def regenerate(*, overwrite: bool = False,
               blocks: Optional[List[str]] = None) -> Dict[str, Any]:
    """Draft everything unauthored. Returns a report; never raises.

    ``overwrite`` redraws fields that already have text — what a re-draw button
    means. Without it only blanks are filled, so authored work is never lost.
    """
    report: Dict[str, Any] = {"blocks": {}, "music": {}, "filled": 0,
                              "overwrite": bool(overwrite)}
    targets = blocks if blocks is not None else _blocks()

    try:
        import game_identity
    except Exception as err:
        report["error"] = str(err)
        return report

    for block in targets:
        try:
            spec = game_identity.get_spec().get(block) or {}
            has_plate = bool(spec.get("reference_images"))
            if has_plate and block in game_identity.image_fillable_blocks():
                result = game_identity.apply_image_fill(block, overwrite=overwrite)
            else:
                result = game_identity.apply_text_fill(block, overwrite=overwrite)
        except Exception as err:
            result = {"filled": {}, "skipped": True, "reason": f"raised: {err}"}
        report["blocks"][block] = result
        report["filled"] += len(result.get("filled") or {})

    report["music"] = _regen_music(overwrite)
    if not report["music"].get("skipped"):
        report["filled"] += 1
    return report


def describe(report: Dict[str, Any]) -> List[str]:
    """The report as lines a person can read. Used by the tool and the log."""
    lines: List[str] = []
    for block, result in (report.get("blocks") or {}).items():
        filled = result.get("filled") or {}
        if filled:
            lines.append(f"{block}: drafted {len(filled)} field(s) "
                         f"from {result.get('source') or '?'}")
            for key, value in filled.items():
                lines.append(f"    {key} = {str(value)[:110]}")
        else:
            lines.append(f"{block}: nothing drafted "
                         f"({result.get('reason') or 'unknown'})")
    music = report.get("music") or {}
    if music.get("skipped"):
        lines.append(f"music: nothing drafted ({music.get('reason') or 'unknown'})")
    else:
        lines.append(f"music: {music.get('direction')}")
    lines.append(f"TOTAL fields drafted: {report.get('filled', 0)}")
    return lines
