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
    # The opening montage. ALL FOUR generated panels are empty of people, and the
    # montage then cuts to the level's own plate as a fifth and final beat — see
    # the tail of generate_shots.
    #
    # Two rounds of live renders got it here. Four angles on one continuous
    # walk-in (the original) played as the same shot repeated with the figure
    # sliding around inside it. Making three empty and asking for the character in
    # the fourth was better, but the model would not draw that fourth panel
    # correctly: it came back as a front-facing portrait, then as a side-on
    # medium, with the wardrobe drifting each time — and because the last panel is
    # the frame the run continues from, a wrong one poisons every later frame.
    #
    # So the montage stopped asking. The composition the game needs already
    # exists as the level plate, drawn by the pipeline that knows the follow-cam
    # rules, so the last beat IS that plate. The model does what it is reliably
    # good at (unpeopled establishing photographs) and nothing else.
    #
    # The grammar is the arthouse cold open: scale, texture, emptiness, absence —
    # unrelated distances and subjects, no implied continuity, nothing happening.
    "approach": {
        "label": "Approach",
        "hint": "A cold open on this place. Nobody until the last frame.",
        "shots": (
            # Deliberately distance-neutral and never named as outdoors: an
            # indoor level gets an approach too, and the prompt adds "stay in this
            # same room" from the environment label, so a brief that says
            # "outside" or "sky" contradicts it in the same payload.
            ("vista", "The widest view",
             "NO PEOPLE. The widest, emptiest view this place affords, held as a "
             "plate — the full depth and scale of it running away from the lens, "
             "weather and light doing the work. Nothing in particular is the "
             "subject; the scale is. No figure anywhere in frame"),
            ("detail", "A detail",
             "NO PEOPLE. A tight, patient close-up of one small worn thing that "
             "belongs to this place — rust bleeding down painted metal, wire, "
             "cracked ground, a stencilled number, dust on glass. Shallow focus, "
             "filling the frame, abstracted by how close it is. Not a wide, not a "
             "building, not a person"),
            ("structure", "The built thing",
             "NO PEOPLE. A static, symmetrical wide of the man-made structure of "
             "this place, standing empty — architecture as portrait, deadpan and "
             "frontal, nobody in it and nothing happening. The emptiness is the "
             "subject"),
            # The shot the montage exists to arrive at, and the frame the run
            # continues from. It is generated rather than taken from the level
            # plate: the plate is a 1K file and these panels are 4K, so ending on
            # it made the last and most important frame visibly the softest of the
            # five. Generated here it is the same resolution as everything else.
            #
            # Composition is spelled out because this is the shot that has to sell
            # the level: the character small in a wide, the goal readable in the
            # distance beyond her, and the distance between the two as the subject.
            ("threshold", "The character, and what she came for",
             "THE CHARACTER IS IN THIS PANEL, and this is the composed hero shot "
             "the montage has been building to. Third-person follow-cam FROM "
             "BEHIND: the camera sits back and a little above the player "
             "character, so we see the BACK of her head and shoulders and none of "
             "her face. She is small in a wide frame, standing at the near edge of "
             "the place, and the location opens away from her into depth. What she "
             "came here for is visible far off beyond her — small, unreached, with "
             "the whole distance she still has to cross lying between her and it. "
             "That distance is the subject of the shot. Compose it properly: put "
             "her off-centre on a third, lead the eye from her body to the "
             "far-off goal along a road, fence line, gully or shadow, build "
             "foreground, midground and distance, and leave the space she is about "
             "to walk into open. Still, level, held — beautiful and withholding, a "
             "frame that makes a viewer want to know what is out there. NOT a "
             "portrait, NOT facing the lens, NOT a close-up, NOT centred, NOT "
             "cropped tight on her"),
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

# The montage is ONE generation cut into four, so the panel resolution is the
# render resolution divided by two in each direction. At the play setting
# (gemini-3.1-flash-lite-image @ 1K) that is 672x376 a panel, and it shows: the
# unpeopled establishing shots came back soft, with the drifting invented detail
# a small render produces in landscape and macro subjects — exactly the frames
# that have to look photographic. It also matters more here than anywhere else,
# because the last panel is the img2img reference the whole run is generated
# from, so its softness compounds into every later frame.
#
# So the opening pays for the good model at 4K: 2048x1152 a panel. It is one
# generation per run, not one per turn. gemini-3.1-flash-lite-image tops out at
# 2K, hence the model override travelling with the size (see the sizes in
# ai_provider_manager.available_model_catalogue("image")); if the model is
# unavailable the call falls back and the montage still renders, just smaller.
GRID_RENDER_MODEL = "gemini-3-pro-image"
GRID_RENDER_SIZE = "4K"

# Optical crops (left, top, right, bottom) as fractions of the source plate.
# Wide / medium / close / offset — a cheap stand-in for four camera moves.
_OPTICAL_BOXES = (
    (0.00, 0.00, 1.00, 1.00),
    (0.14, 0.12, 0.86, 0.88),
    (0.28, 0.10, 0.72, 0.62),
    (0.36, 0.30, 0.98, 0.94),
)


def _cell_name(index: int, cols: int = GRID_COLS, rows: int = GRID_ROWS) -> str:
    """"BOTTOM-RIGHT" for panel 4 of a 2x2. Ordinals alone do not bind.

    A render obeyed all four shot briefs and then placed them in the wrong cells.
    That matters beyond tidiness: the LAST cell is sliced out as the frame the run
    continues from, so a detail landing there starts the game on a close-up.
    """
    i = max(1, int(index)) - 1
    row, col = divmod(i, max(1, cols))
    vertical = ("TOP", "BOTTOM") if rows == 2 else (f"ROW {row + 1}",) * rows
    horizontal = ("LEFT", "RIGHT") if cols == 2 else (f"COLUMN {col + 1}",) * cols
    v = vertical[row] if row < len(vertical) else f"ROW {row + 1}"
    h = horizontal[col] if col < len(horizontal) else f"COLUMN {col + 1}"
    return f"{v}-{h}"


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
    opening = (plate_role == "destination")
    bits.append(
        "MUST be a 2×2 grid of FOUR stills. Same resolution per panel. "
        "No panel borders, numbers, captions, letterbox, HUD, or game UI. "
        "Reading order left-to-right, top-to-bottom."
    )
    if opening:
        # The opening is NOT a beat seen four ways. It is a cold open: unrelated
        # photographs of one place, then the character. Saying "the SAME moment"
        # here (which is right for every other mood) is what made the montage
        # play as one shot repeated with the figure sliding around inside it.
        bits.append(
            "THESE FOUR PANELS ARE NOT THE SAME MOMENT AND NOT ONE CONTINUOUS SHOT. "
            "This is a title-sequence cold open: four separate photographs of one "
            "place, taken at different times, from unrelated distances, of "
            "unrelated subjects. Do NOT carry a pose, an action or a figure from "
            "one panel into the next. Do NOT make them read as a walk, a move, or "
            "a sequence of events. Nothing is happening in any of them — each is a "
            "held, static, patient frame with no motion blur and no action in "
            "progress. Vary the scale hard between panels: one of them is a "
            "landscape, one is a macro detail, one is a frontal architectural "
            "wide. They belong together because they are the same place and the "
            "same light, not because they are the same instant.\n"
            f"PANELS 1, 2 AND 3 ARE COMPLETELY EMPTY OF PEOPLE — no figure, no "
            f"face, no back, no hands, no legs, no feet, no boots, no silhouette, "
            f"no shadow of a person, nobody in the distance, nobody reflected in "
            f"anything. A cropped body part is still a person: a pair of legs at "
            f"the edge of frame fails this.\n"
            f"THE {_cell_name(len(pack['shots']))} PANEL IS THE ONLY ONE WITH THE "
            f"CHARACTER IN IT. It is the shot the montage arrives at and the frame "
            f"the game continues from, so it is the one that must be composed "
            f"most carefully of the four."
        )
    else:
        bits.append(
            "Each panel is a different cinematic camera on the SAME moment and "
            "the SAME place."
        )
    if plate_role == "destination":
        bits.append(
            "PLACE LOCK — HARD. The reference photograph is this place and this "
            "light: keep its location, architecture, materials, ground, sky, "
            "weather and palette, and the wardrobe of the person in it. It is the "
            "world these four photographs were taken in. Panels 1-3 look at parts "
            "of it the reference does not frame — other distances, other subjects, "
            "the land around it, a detail inside it — so do not reproduce the "
            "reference's framing in those. Do not invent a different place, a "
            "different era or a different climate."
        )
    else:
        bits.append(
            "PLACE LOCK — HARD. The reference is the current photograph of this "
            "exact place. Keep the same location, architecture, materials, ground, "
            "sky, and light. Do not teleport. Do not invent a new set."
        )
    if goal:
        if opening:
            bits.append(
                f"WHAT THE PLAYER CAME HERE FOR: {goal.rstrip('. ')}. It is NOT "
                "REACHED, NOT ENTERED and NOT OPENED in any panel. Where it appears "
                "at all it is far off and small — a thing on the horizon the player "
                "still has to walk to. It does not have to appear in every panel; "
                "a landscape or a detail that only implies it is better than four "
                "panels all pointing at the same building."
            )
        else:
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
    # Name the CELL, not just the ordinal. "Panel 4" was not binding: a render
    # put the character in the top-right and a signage detail in the bottom-right,
    # and because the bottom-right panel is the one handed to the game as its
    # opening frame, the run would have started on a close-up of a sign.
    for i, (_cam, label, instruction) in enumerate(pack["shots"], start=1):
        bits.append(f"Panel {i} — {_cell_name(i)} ({label}): {instruction}.")
    if opening:
        bits.append(
            "PANEL PLACEMENT IS NOT INTERCHANGEABLE. Each instruction belongs to "
            "the cell it names and nowhere else — a render that draws all four "
            "subjects correctly but puts them in the wrong cells is a failed "
            "render, because the panels are cut apart and shown in that order."
        )
    if opening:
        # The 4K renders came back as lavender-dusk landscape photography —
        # technically lovely and completely wrong for the material. The palette
        # already says 1993 and rust; what was missing was any instruction about
        # MOOD, so the model defaulted to a postcard.
        bits.append(
            "GRADE AND MOOD — THIS IS NOT A POSTCARD. Analog horror, not landscape "
            "photography. Desaturate: dirty, muted, slightly sickly colour on "
            "expired consumer film, with visible grain and a soft, low-contrast "
            "haze rather than clean digital clarity. No lavender or candy dusk "
            "skies, no glossy travel-magazine light, no lens flare, no vivid "
            "saturation, nothing picturesque. Every frame should feel a little "
            "wrong — too still, too empty, watched. Dread, quiet and specific."
        )
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

            # The character sheet's reference plate has to travel with this
            # request. The only other reference is the level plate, which is an
            # ENVIRONMENT frame — so when the last panel asked for "the
            # character", the model had no idea who that was and invented one: a
            # render came back as a close-up of a middle-aged man in a cap,
            # against a brief that spelled out a woman seen from behind, small in
            # a wide. It is the same identity lock the main image pipeline passes
            # (see identity_paths at engine.py:7492); the montage was simply not
            # passing it.
            identity_plates = []
            try:
                import game_identity
                identity_plates = game_identity.identity_reference_paths(
                    include_character=True, include_setting=False)
            except Exception:
                logging.exception("[CUTSCENE] identity plate lookup failed")
            if identity_plates:
                print(f"[CUTSCENE] {len(identity_plates)} identity plate(s) locked "
                      f"to the hero panel", flush=True)
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
                # See GRID_RENDER_SIZE: four panels sliced out of one generation
                # need the render to be big enough that a quarter of it is still
                # a photograph.
                model=GRID_RENDER_MODEL,
                image_size=GRID_RENDER_SIZE,
                identity_paths=identity_plates or None,
                hold_cast=True,
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
    # The montage no longer appends the level plate as a final beat. It did, so
    # that the composition the run continues from was guaranteed correct rather
    # than argued for — but the plate is a 1K file and these panels are 4K, so the
    # last and most important frame arrived visibly softer than the four before
    # it. The hero shot is generated with the rest now, at the same resolution,
    # with its composition spelled out (see the "threshold" shot in MOODS).
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
