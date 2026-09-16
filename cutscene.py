"""
cutscene.py — 4-shot cinematic montage from a plate.

Revives the flipbook idea (one Gemini grid → sliced panels) as a *cutscene*:
a 2×2 of cinematic camera angles derived from an existing still / encounter
plate / world frame, played as a montage rather than an animated GIF.

Two ways in:
  1. Experience-graph node — World A → Cutscene → World B
  2. Programmatic — POST /api/cutscene/play from the current plate

Generation prefers one Gemini img2img call (2×2 grid, place-locked to the
source plate). When Gemini is off, the key is missing, or ``offline=True``,
we fall back to an optical montage: four cinematic crops of the same plate
so the Moment can still be prototyped and tested without a network.
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).parent.resolve()

MOODS: Dict[str, Dict[str, Any]] = {
    "threshold": {
        "label": "Threshold",
        "hint": "Crossing from one place into the next.",
        "shots": (
            ("wide", "Wide establishing",
             "cinematic wide establishing shot of this exact place, player small in frame, environment readable"),
            ("ots", "Over-shoulder approach",
             "over-shoulder follow, player approaching the threshold of this place"),
            ("insert", "Insert / detail",
             "tight insert on the thing that matters here — a lock, a wound, a sign, a hand on a gate"),
            ("reveal", "Reverse / reveal",
             "reverse angle looking into the next space, or the consequence of crossing"),
        ),
    },
    "aftermath": {
        "label": "Aftermath",
        "hint": "What is left after something happens.",
        "shots": (
            ("wide", "Wreckage wide",
             "wide wreckage of this exact place, aftermath still hanging in the air"),
            ("medium", "Medium on the player",
             "medium shot of the player standing in the wreckage, body readable"),
            ("insert", "Close evidence",
             "close insert on evidence — blood, a dropped thing, a mark on the wall"),
            ("dutch", "Dutch / threat",
             "dutch or low angle, the remaining threat or the empty space it left"),
        ),
    },
    # The opening of a level. Unlike every other mood here, this one is NOT a
    # restaging of the moment the plate shows — it is the shots that come BEFORE
    # it, so the plate is where the montage is heading rather than where the
    # camera stands (see ``plate_role`` in build_cutscene_prompt). The goal stays
    # unreached in all four: the point is to make the player want to walk to it.
    #
    # The wording is deliberately distance-neutral — "the approach", never "from
    # outside" or "open ground". An indoor level also gets an approach (down the
    # length of the space toward the far door), and naming the outdoors here
    # contradicted the indoor/outdoor clause the prompt adds below it.
    "approach": {
        "label": "Approach",
        "hint": "Arriving at a level, with what you came for still in the distance.",
        "shots": (
            ("establish", "Establishing wide",
             "extreme wide establishing shot taking in the whole approach to this "
             "place, the destination small and far off at the end of it, the "
             "distance between here and there readable"),
            ("approach", "The approach",
             "from behind and well back, the walk in toward the destination — it is "
             "ahead in frame and still a long way off"),
            ("distant", "The goal, distant",
             "long lens down the length of the approach onto the destination itself "
             "— small in frame, unreached, the distance still to be crossed readable"),
            ("threshold", "Threshold",
             "arrived at the edge of it, looking in, the way forward open — a "
             "settled, held composition, the frame the story starts from"),
        ),
    },
    "arrival": {
        "label": "Arrival",
        "hint": "Landing in a new space.",
        "shots": (
            ("crane", "Crane in",
             "high / crane-in on this exact place, arriving from above"),
            ("medium", "Medium landing",
             "medium shot of the player arriving, feet on this ground"),
            ("close", "Close reaction",
             "close on the player's face or hands taking the new place in"),
            ("wide", "Wide new space",
             "wide of the new space as it is, now that they are in it"),
        ),
    },
    "departure": {
        "label": "Departure",
        "hint": "Leaving this place behind.",
        "shots": (
            ("close", "Last look",
             "close last look at something in this place — a face, a mark, a door"),
            ("medium", "Walking away",
             "medium of the player walking away through this exact place"),
            ("wide", "Wide leaving",
             "wide of the place being left, player smaller now"),
            ("silhouette", "Silhouette",
             "silhouette against the light at the edge of this place"),
        ),
    },
    "encounter": {
        "label": "Encounter",
        "hint": "Restage a confrontation as four angles.",
        "shots": (
            ("wide", "Two-shot wide",
             "wide two-shot of this confrontation in this exact place"),
            ("ots", "Over-shoulder",
             "over-shoulder from the player onto the other body"),
            ("close", "Close on them",
             "close on the other face or the danger, large and readable"),
            ("reverse", "Reverse on the player",
             "reverse on the player, what this moment is doing to them"),
        ),
    },
}
MOOD_IDS = tuple(MOODS.keys())
DEFAULT_MOOD = "threshold"

# How long each shot holds. This is where the cutscene grammar parts ways with
# the flipbook's: a flipbook panel is one frame of motion and flicks past in
# under half a second, but these are four different cameras on one moment and
# each is a photograph you are meant to look at. At 1600ms the montage read as a
# slideshow on fast-forward — nobody had time to see what a shot was of before
# it cut. Four seconds is a held shot. Overridden by the cutscene_hold_ms
# tunable (see tunables.py), which is why nothing reads the constant directly.
DEFAULT_DURATION_MS = 4000
HOLD_MS = DEFAULT_DURATION_MS
GRID_COLS = 2
GRID_ROWS = 2

# Optical crops (left, top, right, bottom) as fractions of the source plate.
# Wide / medium / close / offset — a cheap stand-in for four camera moves.
_OPTICAL_BOXES = (
    (0.00, 0.00, 1.00, 1.00),
    (0.14, 0.12, 0.86, 0.88),
    (0.28, 0.10, 0.72, 0.62),
    (0.36, 0.30, 0.98, 0.94),
)


def normalize_mood(raw: Any) -> str:
    mood = str(raw or "").strip().lower()
    return mood if mood in MOODS else DEFAULT_MOOD


def mood_catalog() -> List[Dict[str, Any]]:
    out = []
    for mid, row in MOODS.items():
        out.append({
            "id": mid,
            "label": row["label"],
            "hint": row.get("hint") or "",
            "shots": [
                {"camera": cam, "label": label}
                for cam, label, _ in row["shots"]
            ],
        })
    return out


def shot_labels(mood: str) -> List[Dict[str, str]]:
    row = MOODS[normalize_mood(mood)]
    return [{"camera": cam, "label": label} for cam, label, _ in row["shots"]]


def extract_grid_panels(
    grid_image_path: Path,
    output_dir: Path,
    *,
    cols: int = GRID_COLS,
    rows: int = GRID_ROWS,
    stem: str = "shot",
) -> List[Path]:
    """Slice an even grid into individual panel files. Reading order L→R, T→B."""
    from PIL import Image

    grid_image_path = Path(grid_image_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    grid = Image.open(grid_image_path)
    width, height = grid.size
    if width < cols or height < rows or width % cols != 0 or height % rows != 0:
        # Tolerate near-even Gemini output by flooring the cell size.
        cell_w = width // cols
        cell_h = height // rows
        if cell_w < 8 or cell_h < 8:
            return []
    else:
        cell_w = width // cols
        cell_h = height // rows
    paths: List[Path] = []
    for row in range(rows):
        for col in range(cols):
            x = col * cell_w
            y = row * cell_h
            panel = grid.crop((x, y, x + cell_w, y + cell_h))
            n = row * cols + col + 1
            dest = output_dir / f"{stem}_{n:02d}.png"
            panel.save(dest)
            paths.append(dest)
    return paths


def _crop_frac(im, box: Tuple[float, float, float, float]):
    w, h = im.size
    l = int(max(0.0, min(1.0, box[0])) * w)
    t = int(max(0.0, min(1.0, box[1])) * h)
    r = int(max(0.0, min(1.0, box[2])) * w)
    b = int(max(0.0, min(1.0, box[3])) * h)
    if r <= l + 8 or b <= t + 8:
        return im.copy()
    return im.crop((l, t, r, b))


def optical_montage(
    source_path: Path,
    output_dir: Path,
    *,
    stem: str = "cutscene",
    mood: str = DEFAULT_MOOD,
) -> List[Dict[str, Any]]:
    """Four cinematic crops of one plate. No network. The prototype path."""
    from PIL import Image

    source_path = Path(source_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    im = Image.open(source_path).convert("RGB")
    labels = shot_labels(mood)
    shots: List[Dict[str, Any]] = []
    for i, box in enumerate(_OPTICAL_BOXES):
        crop = _crop_frac(im, box)
        if i == 3:
            # A slight rotate-and-recrop so the last beat does not feel like
            # the same photograph zoomed. Optical only — Gemini restages.
            crop = crop.rotate(-7, resample=Image.BICUBIC, expand=True)
            cw, ch = crop.size
            inset = 0.08
            crop = crop.crop((
                int(cw * inset), int(ch * inset),
                int(cw * (1 - inset)), int(ch * (1 - inset)),
            ))
        dest = output_dir / f"{stem}_{i + 1:02d}.png"
        crop.save(dest)
        meta = labels[i] if i < len(labels) else {"camera": "shot", "label": f"Shot {i + 1}"}
        shots.append({
            "path": dest,
            "camera": meta["camera"],
            "label": meta["label"],
        })
    return shots


def build_cutscene_prompt(
    mood: str = DEFAULT_MOOD,
    *,
    shot_brief: str = "",
    setting: str = "",
    name: str = "",
    goal: str = "",
    plate_role: str = "anchor",
) -> str:
    """One 2×2 grid instruction. Place-locked. No captions, no borders.

    ``plate_role`` says what the reference photograph IS to this montage:

    · ``anchor`` — the camera is standing in it. Every shot is another angle on
      the moment the plate shows. This is what restaging a beat wants.
    · ``destination`` — the montage is heading TOWARD it and has not arrived.
      The plate is the look, the light and the cast, but not the framing; the
      shots are further out than it. The level's opening needs this, and given
      the anchor wording ("the current photograph of this exact place") the
      model otherwise redraws the plate four times from where it already stands.
    """
    mood = normalize_mood(mood)
    pack = MOODS[mood]
    bits: List[str] = []
    try:
        import game_identity
        anchor = game_identity.world_anchor(
            "1993 analog photograph, cinematic game cutscene, practical light.",
            include_character=True,
            include_vantage=False,
        )
        if anchor:
            bits.append(anchor.rstrip(". ") + ".")
    except Exception:
        bits.append(
            "1993 analog photograph, cinematic game cutscene, practical light."
        )
    bits.append(
        "MUST be a 2×2 grid of FOUR stills. Same resolution per panel. "
        "No panel borders, numbers, captions, letterbox, HUD, or game UI. "
        "Reading order left-to-right, top-to-bottom. Each panel is a different "
        "cinematic camera on the SAME moment and the SAME place."
    )
    if plate_role == "destination":
        bits.append(
            "PLACE LOCK — HARD. The reference photograph is this place as it "
            "looks once you are standing in it: keep its location, architecture, "
            "materials, ground, sky, light, and the people and wardrobe already "
            "in it. But the camera has NOT ARRIVED YET. All four shots sit "
            "FURTHER BACK than the reference and look toward it across the "
            "distance still to be crossed. Do not reproduce the reference's "
            "framing in any panel, and do not invent a different place."
        )
    else:
        bits.append(
            "PLACE LOCK — HARD. The reference is the current photograph of this "
            "exact place. Keep the same location, architecture, materials, ground, "
            "sky, and light. Do not teleport. Do not invent a new set."
        )
    if goal:
        bits.append(
            f"WHAT THE PLAYER CAME HERE FOR: {goal.rstrip('. ')}. It is visible "
            "in these shots and is NOT REACHED in any of them — keep it far off, "
            "small in frame, across distance the player still has to cross. Never "
            "cut to it up close, never show it entered or opened."
        )
    if (setting or "").lower().startswith("outdoor"):
        bits.append("This frame is OUTDOORS. Stay outdoors. Same sky, same ground.")
    elif (setting or "").lower().startswith("indoor"):
        bits.append("This frame is INDOORS. Stay in this same room.")
    if name:
        bits.append(f"This cutscene is called '{name}'.")
    if shot_brief:
        bits.append(f"Author direction: {shot_brief.strip()}")
    bits.append(f"Mood: {pack['label']}. {pack.get('hint') or ''}".strip())
    for i, (_cam, label, instruction) in enumerate(pack["shots"], start=1):
        bits.append(f"Panel {i} ({label}): {instruction}.")
    bits.append(
        "A finished 1993 photograph in each panel. Empty hands, no text, "
        "no watermarks. Preserve the people and wardrobe already in the reference."
    )
    prompt = " ".join(bits)
    try:
        import engine
        if hasattr(engine, "_sanitize_for_image_generation"):
            prompt = engine._sanitize_for_image_generation(prompt)
    except Exception:
        pass
    return prompt


def _new_stem() -> str:
    return "cutscene_" + uuid.uuid4().hex[:10]


def _to_web(path: Path, session_id: str) -> str:
    try:
        import engine
        url = engine._to_web_image_url(str(path), session_id)
        if url:
            return url
    except Exception:
        pass
    return "/images/" + path.name


def environment_type(session_id: str = "default") -> str:
    """"indoor-…" / "outdoor-…" for the place this session is in right now.

    This decides the indoor/outdoor clause in the grid prompt, and it used to be
    read from ``setting_reference.setting`` — a field that has never existed on
    the Level sheet, so it was always "" and the clause never fired. Nothing
    stopped an indoor level's montage from being drawn under an open sky.

    Vision already labels every turn's environment as ``setting_type``, so walk
    the history back for it and fall back to classifying the render base, the
    same way the world drift does.
    """
    import engine

    try:
        for entry in reversed(engine._load_history(session_id) or []):
            label = str(entry.get("setting_type") or "").strip()
            if label:
                return label
        st = engine._load_state(session_id) or {}
        return engine.classify_setting(st.get("current_render_base") or "") or ""
    except Exception:
        logging.exception("[CUTSCENE] environment lookup failed")
        return ""


def resolve_source_path(
    session_id: str = "default",
    *,
    source_url: str = "",
    source: str = "incoming",
    dest_world_id: str = "",
) -> Optional[Path]:
    """Find the plate this cutscene restages."""
    import engine

    if source_url:
        p = engine._resolve_image_path(source_url, session_id)
        if p is not None and Path(p).exists():
            return Path(p)
        raw = Path(source_url)
        if raw.exists():
            return raw

    if source == "dest_world" and dest_world_id:
        try:
            import experience_store
            import world_frames
            exp = experience_store.get_experience()
            world = experience_store.world_by_id(exp, dest_world_id)
            slug = (world or {}).get("slug") or ""
            if slug:
                rec = world_frames.record(slug)
                frame = rec.get("path") or ""
                if frame and Path(frame).is_file():
                    return Path(frame)
        except Exception:
            logging.exception("[CUTSCENE] dest_world frame lookup failed")

    try:
        st = engine.get_state(session_id) or {}
        url = st.get("current_image_url") or ""
        if url:
            p = engine._resolve_image_path(url, session_id)
            if p is not None and Path(p).exists():
                return Path(p)
    except Exception:
        logging.exception("[CUTSCENE] current plate lookup failed")
    return None


def generate_shots(
    source_path: Path,
    *,
    session_id: str = "default",
    mood: str = DEFAULT_MOOD,
    name: str = "",
    shot_brief: str = "",
    setting: str = "",
    goal: str = "",
    plate_role: str = "anchor",
    offline: bool = False,
    output_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Produce four shot files from a source plate.

    ``source`` on the result is ``gemini`` or ``optical``. Optical is the
    prototype / fallback so a missing key never blocks the Moment.
    """
    import engine

    mood = normalize_mood(mood)
    source_path = Path(source_path)
    if output_dir is None:
        output_dir = Path(engine._get_image_dir(session_id))
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _new_stem()
    t0 = time.time()
    used = "optical"
    grid_path: Optional[Path] = None
    shots: List[Dict[str, Any]] = []

    want_gemini = (
        not offline
        and bool(getattr(engine, "IMAGE_ENABLED", True))
    )
    if want_gemini:
        try:
            from gemini_image_utils import GEMINI_API_KEY
            if not (GEMINI_API_KEY and str(GEMINI_API_KEY).strip()):
                want_gemini = False
        except Exception:
            want_gemini = False
    if want_gemini:
        try:
            from gemini_image_utils import generate_gemini_img2img
            prompt = build_cutscene_prompt(
                mood, shot_brief=shot_brief, setting=setting, name=name,
                goal=goal, plate_role=plate_role,
            )
            tod = ""
            try:
                tod = str((engine.get_state(session_id) or {}).get("time_of_day") or "")
            except Exception:
                tod = ""
            grid_file = generate_gemini_img2img(
                prompt=prompt,
                caption=stem + "_grid",
                reference_image_path=str(source_path),
                strength=0.42,
                world_prompt=(shot_brief or "")[:200] or None,
                time_of_day=tod,
                hd_mode=False,
                output_dir=output_dir,
                is_flipbook=False,
                include_people=True,
            )
            if grid_file and Path(grid_file).exists():
                grid_path = Path(grid_file)
                panels = extract_grid_panels(
                    grid_path, output_dir, cols=GRID_COLS, rows=GRID_ROWS,
                    stem=stem,
                )
                labels = shot_labels(mood)
                if len(panels) >= 4:
                    used = "gemini"
                    for i, panel in enumerate(panels[:4]):
                        meta = labels[i]
                        shots.append({
                            "path": panel,
                            "camera": meta["camera"],
                            "label": meta["label"],
                        })
        except Exception:
            logging.exception("[CUTSCENE] Gemini 2×2 failed — optical fallback")

    if not shots:
        shots = optical_montage(
            source_path, output_dir, stem=stem, mood=mood,
        )
        used = "optical"

    payload_shots = []
    for shot in shots:
        path = Path(shot["path"])
        payload_shots.append({
            "url": _to_web(path, session_id),
            "path": str(path),
            "camera": shot["camera"],
            "label": shot["label"],
        })
    return {
        "source": used,
        "mood": mood,
        "name": name or "",
        "shots": payload_shots,
        "grid_url": _to_web(grid_path, session_id) if grid_path else "",
        "duration_ms": int(HOLD_MS or DEFAULT_DURATION_MS),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "source_plate": _to_web(source_path, session_id),
    }


def play_for_session(
    session_id: str,
    body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Generate a cutscene payload for this session. Does not hop Worlds."""
    import engine

    body = body or {}
    cutscene_id = str(body.get("cutscene_id") or "").strip()
    mood = normalize_mood(body.get("mood"))
    name = str(body.get("name") or "").strip()
    shot_brief = str(body.get("shot_brief") or "").strip()
    source = str(body.get("source") or "incoming").strip() or "incoming"
    source_url = str(body.get("source_url") or body.get("frame") or "").strip()
    dest_world = str(body.get("to_world") or body.get("dest_world_id") or "").strip()
    offline = bool(body.get("offline"))

    node = None
    if cutscene_id:
        try:
            import experience_store
            exp = experience_store.get_experience()
            node = experience_store.cutscene_by_id(exp, cutscene_id)
        except Exception:
            node = None
        if node:
            mood = normalize_mood(node.get("mood") or mood)
            name = name or str(node.get("name") or "")
            shot_brief = shot_brief or str(node.get("shot_brief") or "")
            source = str(node.get("source") or source) or "incoming"
            if not dest_world:
                dest_world = _outgoing_world_id(cutscene_id)

    # A montage staged by the server (the level's opening) already knows its
    # plate as a file. Prefer it: that plate is a World frame, whose web URL is
    # /api/worlds/<slug>/frame, and resolving that back to a path is something
    # resolve_source_path cannot do — so routing it through the client would
    # leave the opening with no reference at all.
    staged = engine.get_state(session_id) or {}
    staged = staged.get("pending_cutscene") or {}
    if staged.get("cutscene_id") == cutscene_id:
        source_url = str(staged.get("source_path") or "") or source_url

    plate = resolve_source_path(
        session_id,
        source_url=source_url,
        source=source,
        dest_world_id=dest_world,
    )
    if plate is None:
        return {"ok": False, "error": "no_plate", "shots": []}

    goal = ""
    try:
        import game_identity
        goal = game_identity.level_goal()
    except Exception:
        logging.exception("[CUTSCENE] level goal lookup failed")

    try:
        generated = generate_shots(
            plate,
            session_id=session_id,
            mood=mood,
            name=name,
            shot_brief=shot_brief,
            setting=environment_type(session_id),
            goal=goal,
            plate_role="destination" if mood == "approach" else "anchor",
            offline=offline,
        )
    except Exception:
        logging.exception("[CUTSCENE] generate_shots failed")
        return {"ok": False, "error": "generate_failed", "shots": []}
    play_id = cutscene_id or ("play-" + uuid.uuid4().hex[:8])
    payload = {
        "ok": True,
        "cutscene_id": play_id,
        "name": name or MOODS[mood]["label"],
        "mood": mood,
        "graph": bool(node),
        "to_world": dest_world,
        **generated,
    }
    try:
        with engine.WORLD_STATE_LOCK:
            st = engine._load_state(session_id) or {}
            if node:
                st["experience_cutscene_id"] = play_id
            # MERGED onto what is already pending, not replacing it. The opening
            # montage is stamped at reset with the fields only reset knows — that
            # it IS the opening, its goal line, its prologue — and generating the
            # shots must not wipe them, or the hand-off back to turn one has no
            # idea it is the one that owns the parked choice slate.
            prev = st.get("pending_cutscene") or {}
            keep = prev if prev.get("cutscene_id") == play_id else {}
            st["pending_cutscene"] = {
                **keep,
                "cutscene_id": play_id,
                "name": payload["name"],
                "mood": mood,
                "status": "ready",
                "to_world": dest_world,
                "shots": payload["shots"],
                "duration_ms": payload["duration_ms"],
                "source": payload["source"],
                "graph": bool(node),
            }
            engine._save_state(st, session_id)
        # Saving to disk is not enough. The module-global `state` mirror still
        # holds the copy from BEFORE the shots existed, and the next request that
        # persists it — a status poll, a feed poll — writes that copy straight
        # back over this one. Nothing noticed while the shots were only ever read
        # from this function's own HTTP response; the opening montage reads them
        # back on a later request to find the frame it ended on, and got none.
        engine._sync_ambient_state(st, session_id)
    except Exception:
        logging.exception("[CUTSCENE] failed to stamp pending_cutscene")
    return payload


def _outgoing_world_id(cutscene_id: str) -> str:
    try:
        import experience_store
        exp = experience_store.get_experience()
        for t in experience_store.transitions_from(exp, cutscene_id):
            dest = experience_store.world_by_id(exp, t.get("to") or "")
            if dest:
                return dest["id"]
    except Exception:
        pass
    return ""
