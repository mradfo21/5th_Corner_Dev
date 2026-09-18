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

import json
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
            # Absence, not arrival. The character does not appear in any generated
            # panel; the montage cuts to the level plate after these four.
            #
            # A generated hero shot was tried here and reverted. Even when it came
            # back technically correct — Kelsey from behind, small in a wide, the
            # goal readable beyond her — it did not do the job the last frame has
            # to do, which is tell the player who they are. A figure that small,
            # centred, seen from the back, in a composition unlike any the game
            # then uses, reads as spatially confusing rather than as "this is me".
            # The plate is the actual gameplay composition, so ending on it is the
            # only version that answers the question.
            #
            # This brief used to describe "the evidence that people were here and
            # are not now", and the render answered with a pair of legs and boots
            # standing in the dirt. Naming people at all — even to say they are
            # gone — puts a person in the frame. So the subject is stated purely as
            # objects, and the ban names the parts that actually turned up.
            ("leftovers", "What was left",
             "NO PEOPLE. A held frame low on the ground: tyre tracks pressed into "
             "dust, scattered litter and broken board, a door standing open onto "
             "darkness, a chair facing nothing. Objects and ground only. NO legs, "
             "NO feet, NO boots, NO hands, NO shadow of a figure, nothing alive "
             "anywhere in the frame"),
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
# So the opening pays for the good model at a raised resolution. 2K gives
# ~1376x768 a panel — still twice the width and height of the play setting, which
# is what the establishing shots needed — and it generates appreciably faster than
# 4K, which is the whole reason it is not 4K: at 4K the montage took long enough
# that the opening held black for around a minute before the first shot appeared.
#
# The model override travels with the size because the sizes are per-model (see
# ai_provider_manager.available_model_catalogue("image")): flash-lite tops out at
# 2K, so 2K is reachable on either, and gemini-3-pro-image is kept for the
# fidelity of the macro and landscape panels. If it is unavailable the call falls
# back and the montage still renders, just smaller.
GRID_RENDER_MODEL = "gemini-3-pro-image"
GRID_RENDER_SIZE = "2K"

# Optical crops (left, top, right, bottom) as fractions of the source plate.
# Wide / medium / close / offset — a cheap stand-in for four camera moves.
_OPTICAL_BOXES = (
    (0.00, 0.00, 1.00, 1.00),
    (0.14, 0.12, 0.86, 0.88),
    (0.28, 0.10, 0.72, 0.62),
    (0.36, 0.30, 0.98, 0.94),
)


SHOTLIST_SCHEMA = {
    "type": "object",
    "properties": {
        "subjects": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["subjects"],
}

SHOTLIST_INSTRUCTIONS = (
    "You are the second-unit photographer on a 1993 analog-horror film, shooting "
    "the cold open. Read the world bible below and answer one question: what are "
    "the FOUR photographs that open this story?\n"
    "\n"
    "They are not establishing shots of scenery. Each one is a specific physical "
    "thing, standing in this place right now, that tells the audience something "
    "happened here and something is still wrong. Together they should set up the "
    "mystery at the centre of this world and make a viewer need to know the rest "
    "— evidence, aftermath, scale, wrongness — without explaining any of it.\n"
    "\n"
    "Rules, all of them hard:\n"
    "- NOBODY is in any of these photographs. No people, no bodies, no figures.\n"
    "- Only things that would physically be here in 1993, in this landscape. No "
    "creatures, no visible supernatural events, no glowing anomalies. The dread "
    "is in ordinary objects that are wrong.\n"
    "- Specific and photographable. 'A hand-painted evacuation notice bolted over "
    "a company sign' is a shot. 'A sense of unease' is not.\n"
    "- Each of the four is a DIFFERENT subject at a DIFFERENT scale. Do not give "
    "four shots of the same thing.\n"
    "- Do not name the goal outright and do not show it reached.\n"
    "\n"
    "The four roles, in order — match each subject to its role:\n"
    "1. THE WIDEST VIEW: the scale of the place and what has been done to it.\n"
    "2. A MACRO DETAIL: one small worn or marked object, filling the frame.\n"
    "3. A BUILT THING, STANDING EMPTY: architecture as portrait, frontal, nobody.\n"
    "4. WHAT WAS LEFT BEHIND: the evidence that this was abandoned in a hurry.\n"
    "\n"
    "Answer with exactly four subjects, one per role, in that order. Each is one "
    "sentence, at most 25 words, describing only what the camera sees."
)


def mystery_shotlist(session_id: str = "default", *, goal: str = "") -> List[str]:
    """Four photographs, chosen from this world's own lore.

    The montage used to be four fixed generic briefs — widest view, macro detail,
    architectural wide, leftovers. Compositionally that worked and conceptually it
    was empty: nothing in the instruction was about THIS story, so the renders came
    back handsome and inert, a stock desert with a fence.

    Meanwhile the world bible is 9,000 words with a real mystery in it — thousands
    dead in an industrial accident, military raids hunting company mercenaries, and
    a black hole buried miles under an acid mine. The opening had never seen a line
    of it. This asks for the four shots that set that up.

    The compositional roles are kept, because those are what made the frames good;
    only the subjects come from the lore. Never fatal: on any failure the caller
    falls back to the static briefs and the montage still renders.
    """
    import engine

    try:
        st = engine._load_state(session_id) or {}
    except Exception:
        st = {}

    bible = ""
    try:
        import prompts_store
        bible = str(prompts_store.PROMPTS.get("world_initial_state") or "")
    except Exception:
        bible = ""
    if not bible.strip():
        bible = str(st.get("world_prompt") or "")
    if len(bible.strip()) < 400:
        # Nothing to read; the static briefs are no worse than a guess.
        print("[CUTSCENE] no world bible to draw a shotlist from — using the "
              "built-in establishing briefs", flush=True)
        return []

    place = ""
    try:
        import game_identity
        place = game_identity.place_summary() or ""
    except Exception:
        place = ""

    prompt = (
        f"{SHOTLIST_INSTRUCTIONS}\n\n"
        f"WORLD BIBLE:\n{bible[:6000]}\n\n"
        f"THE PLACE THIS OPENS IN: {place or '(see the bible)'}\n"
        + (f"WHAT THE PLAYER IS HERE FOR (do not show it reached): {goal}\n"
           if goal else "")
    )

    try:
        raw = engine._ask(prompt, temp=1.0, tokens=420, use_lore=False,
                          response_schema=SHOTLIST_SCHEMA)
    except Exception:
        logging.exception("[CUTSCENE] shotlist ask failed")
        return []

    subjects: List[str] = []
    try:
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        for item in (data.get("subjects") or []):
            text = str(item or "").strip()
            if text:
                subjects.append(text.rstrip(". ") + ".")
    except Exception:
        logging.exception("[CUTSCENE] shotlist did not parse")
        return []

    if len(subjects) < 4:
        print(f"[CUTSCENE] shotlist returned {len(subjects)} subject(s), need 4 — "
              f"using the built-in establishing briefs", flush=True)
        return []
    for i, s in enumerate(subjects[:4], start=1):
        print(f"[CUTSCENE] shot {i}: {s[:110]}", flush=True)
    return subjects[:4]


def _identity_plates() -> List[str]:
    """The character sheet's reference plates, for montages that draw a person.

    An anchor montage restages a beat the player is already in, so the cast has to
    be held. The opening does not: every panel of it is empty, and handing the
    model a portrait there fights that.
    """
    try:
        import game_identity
        return list(game_identity.identity_reference_paths(
            include_character=True, include_setting=False))
    except Exception:
        logging.exception("[CUTSCENE] identity plate lookup failed")
        return []


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


# How each panel of the opening is FRAMED, independent of what it is OF. The
# subjects come from the lore (see mystery_shotlist); these keep the scale varied
# so four lore-driven shots do not all come back as the same wide.
_ROLE_FRAMING = {
    1: "Shoot it as the widest view the place affords — full depth and scale "
       "running away from the lens, held as a plate.",
    2: "Shoot it as a tight, patient macro — filling the frame, shallow focus, "
       "abstracted by how close the lens is. Not a wide.",
    3: "Shoot it as a static, symmetrical, frontal wide — architecture as "
       "portrait, deadpan, the emptiness part of the subject.",
    4: "Shoot it low and close on the ground it sits on — objects and dirt, "
       "the frame of somewhere left in a hurry.",
}


def build_cutscene_prompt(
    mood: str = DEFAULT_MOOD,
    *,
    shot_brief: str = "",
    setting: str = "",
    name: str = "",
    goal: str = "",
    plate_role: str = "anchor",
    shotlist: Optional[List[str]] = None,
    has_reference: bool = True,
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

    ``has_reference`` is False for the opening, which now draws before anything
    else exists. Every PLACE LOCK clause below describes a photograph that is
    attached to the call, so emitting one with nothing attached tells the model
    to match an image it cannot see.
    """
    mood = normalize_mood(mood)
    pack = MOODS[mood]
    bits: List[str] = []
    setting_line = ""
    opening = (plate_role == "destination")
    try:
        import game_identity
        # NO CAST in the opening's anchor. Every panel of the opening montage is
        # required to be empty of people, and `world_anchor` with the character
        # in it spends its first sentences describing exactly what the panels
        # must not contain — "adult man, olive field jacket, a battered 35mm
        # stills camera on a neck strap". While the montage was img2img off a
        # place-locked plate that contradiction mostly lost; as text-to-image it
        # wins outright. A live run came back with a figure at the fence in
        # panel one and two front-facing portraits of a man holding a camera in
        # panels two and four, which is the one thing the brief bans in capitals.
        #
        # The character belongs to the beat AFTER this one (the idle), which
        # does get the full cast anchor and the reference plates.
        anchor = game_identity.world_anchor(
            "1993 analog photograph, cinematic game cutscene, practical light.",
            include_character=not opening,
            include_vantage=False,
        )
        if anchor:
            bits.append(anchor.rstrip(". ") + ".")
        # Only used when nothing is attached (see has_reference). With a plate
        # in hand the photograph is a better description of the place than any
        # sentence about it.
        setting_line = game_identity.place_summary() or ""
        _sheet = (game_identity.get_spec() or {}).get(
            game_identity.SETTING_KEY) or {}
        for _field in ("landmarks", "palette"):
            _val = str(_sheet.get(_field) or "").strip()
            if _val:
                setting_line = f"{setting_line} {_val.rstrip('. ')}.".strip()
    except Exception:
        bits.append(
            "1993 analog photograph, cinematic game cutscene, practical light."
        )
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
            "ALL FOUR PANELS ARE COMPLETELY EMPTY OF PEOPLE — no figure, no face, "
            "no back, no hands, no legs, no feet, no boots, no silhouette, no "
            "shadow of a person, no crowd, nobody in the distance, nobody "
            "reflected in anything. A cropped body part is still a person: a pair "
            "of legs at the edge of frame fails this. These are photographs of a "
            "place with nobody in it; the person arrives after this montage, in a "
            "shot that is not yours to draw."
        )
    else:
        bits.append(
            "Each panel is a different cinematic camera on the SAME moment and "
            "the SAME place."
        )
    if plate_role == "destination" and not has_reference:
        # The opening draws first now — there is no plate in front of it, and a
        # PLACE LOCK naming a reference that is not attached used to send the
        # model looking for one. THIS render establishes the place; everything
        # after it (the idle beat, then every turn) is locked to these panels.
        bits.append(
            "THIS IS THE FIRST PHOTOGRAPH OF THIS WORLD. There is no reference "
            "image: you are establishing the place, and every later frame of "
            "this story will be drawn from these panels. So commit. One "
            "specific location, one consistent light, one weather — all four "
            "panels are the same place at the same hour, photographed from "
            "different distances. Read the location described below and shoot "
            "THAT; do not drift to a generic version of it."
        )
        if setting_line:
            bits.append(f"THE PLACE: {setting_line}")
    elif plate_role == "destination":
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
    #
    # A `shotlist` replaces the SUBJECT of each panel with something drawn from
    # this world's own lore, and keeps the role. The static briefs make good
    # compositions and say nothing about the story: handsome, inert, a stock
    # desert with a fence. The roles are what was worth keeping.
    for i, (_cam, label, instruction) in enumerate(pack["shots"], start=1):
        subject = ""
        if shotlist and i <= len(shotlist):
            subject = str(shotlist[i - 1] or "").strip()
        if subject:
            bits.append(
                f"Panel {i} — {_cell_name(i)} ({label}): {subject} "
                f"{_ROLE_FRAMING.get(i, '')} NO PEOPLE in this panel."
            )
        else:
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
        "no watermarks."
        + (" Preserve the people and wardrobe already in the reference."
           if has_reference else "")
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
    source_path: Optional[Path],
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
    """Produce four shot files, from a source plate or from nothing.

    ``source_path`` is None for a level's OPENING, which no longer has a plate
    to establish toward — it IS the first render of the run. The grid then goes
    out as text-to-image instead of img2img, which is the right call anyway: the
    four cold-open photographs are unpeopled establishing shots of a place the
    lore already describes, and the plate that used to seed them was itself a
    from-scratch text-to-image guess made moments earlier. Drawing that guess
    and then drawing FROM it was two renders where one does the job, and it let
    the guess and the montage disagree about where the level was.

    Every other mood restages a beat the player can see, and still requires its
    plate.

    ``source`` on the result is ``gemini`` or ``optical``. Optical is the
    prototype / fallback so a missing key never blocks the Moment — it needs a
    plate to crop, so it is unavailable to the opening.
    """
    import engine

    mood = normalize_mood(mood)
    source_path = Path(source_path) if source_path else None
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
            from gemini_image_utils import (generate_gemini_img2img,
                                            generate_with_gemini)
            # Only the opening reads the lore for its subjects. A restage has a
            # beat in front of it already; it does not need inventing.
            shotlist = []
            if plate_role == "destination":
                try:
                    shotlist = mystery_shotlist(session_id, goal=goal)
                except Exception:
                    logging.exception("[CUTSCENE] shotlist failed; using briefs")
                    shotlist = []
            prompt = build_cutscene_prompt(
                mood, shot_brief=shot_brief, setting=setting, name=name,
                goal=goal, plate_role=plate_role, shotlist=shotlist,
                has_reference=source_path is not None,
            )
            tod = ""
            try:
                tod = str((engine.get_state(session_id) or {}).get("time_of_day") or "")
            except Exception:
                tod = ""

            # Deliberately NO character identity plate for the opening montage.
            # It was added when the last panel was a generated hero shot — without
            # it the model invented a stranger — but every generated panel is
            # unpeopled again, so handing it a portrait to hold now argues against
            # the instruction that nobody is in frame. The character arrives in the
            # plate beat, which is already locked to her.
            identity_plates = ([] if plate_role == "destination"
                               else _identity_plates())
            if source_path is None:
                # The opening. No plate exists yet and none is wanted: this IS
                # the run's first render, and the place it establishes comes
                # from the lore (see mystery_shotlist) and the Level sheet,
                # which engine._ensure_level_sheet_is_filled guarantees says
                # something before we get here.
                grid_file = generate_with_gemini(
                    prompt=prompt,
                    caption=stem + "_grid",
                    world_prompt=(shot_brief or "")[:200] or None,
                    time_of_day=tod,
                    hd_mode=False,
                    output_dir=output_dir,
                    # See GRID_RENDER_SIZE.
                    model=GRID_RENDER_MODEL,
                    image_size=GRID_RENDER_SIZE,
                )
            else:
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
                    # See GRID_RENDER_SIZE: four panels sliced out of one
                    # generation need the render to be big enough that a
                    # quarter of it is still a photograph.
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

    if not shots and source_path is not None:
        shots = optical_montage(
            source_path, output_dir, stem=stem, mood=mood,
        )
        used = "optical"
    elif not shots:
        # The opening has no plate to crop, so there is no optical fallback for
        # it. The caller lands the run on its own intro render instead, which
        # is what a montage-less boot has always done.
        print("[CUTSCENE] the opening grid did not render and there is no "
              "plate to fall back on — this run opens without a montage",
              flush=True)
        used = "none"

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
        "source_plate": _to_web(source_path, session_id) if source_path else "",
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

    # The level's OPENING draws first and has no plate — it IS the run's first
    # render (see generate_shots). Every other mood restages something the
    # player can already see, and without that photograph there is nothing to
    # restage, so those still refuse.
    opening = bool(staged.get("opening")) or mood == "approach"
    plate = resolve_source_path(
        session_id,
        source_url=source_url,
        source=source,
        dest_world_id=dest_world,
    )
    if plate is None and not opening:
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
    # The opening used to append the plate as a fifth and final beat, because
    # the plate was the only gameplay-composition frame in the boot — the only
    # one with the protagonist in it, shot the way the game shoots — and the run
    # continued from it.
    #
    # There is no plate now, and nothing is missing. The beat that follows this
    # montage is the IDLE (engine._generate_opening_establishing): the character
    # standing in this place, drawn by the flipbook pipeline that every turn
    # uses, animated, and handed to turn one as its anchor. That is the frame
    # that answers "this is me", and it is a better answer than the plate was
    # because it moves and because the player arrives on it rather than glimpsing
    # it for four seconds inside the montage.
    #
    # What the old fifth beat actually did in practice was flash a separately
    # rendered still that the montage had no reason to agree with — an indoor
    # storeroom between four photographs of an open-pit mine, on 2026-09-17.

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
            # Carry the staged fields forward. Matching on cutscene_id alone was
            # too brittle to hold the ONE field that matters: `opening` is what
            # /api/cutscene/play reads to start the idle render and what
            # /api/cutscene/complete reads to hand the run over, so losing it
            # costs the run its first playable frame AND its authored slate, and
            # the player lands on a "Look around" over a frame drawn from
            # nothing. Any id mismatch — a client that did not echo the id, a
            # re-play, a generated play-id — used to silently wipe it.
            #
            # An opening montage that has not been played yet is unambiguous:
            # there is exactly one, it is flagged, and it has no shots. Keep it.
            keep = {}
            if prev.get("cutscene_id") == play_id:
                keep = prev
            elif prev.get("opening") and not (prev.get("shots") or []):
                keep = prev
                print(f"[CUTSCENE] play id {play_id!r} does not match the staged "
                      f"{prev.get('cutscene_id')!r}, but the staged montage is "
                      f"this run's unplayed opening — keeping its stamp",
                      flush=True)
            # `opening` is WRITTEN, not merely inherited from `keep`.
            #
            # Inheriting it was still one failure away from losing the run. On
            # 2026-09-17 (bugs/20260917_155759) `pending_cutscene` was gone from
            # the persisted state by the time this ran — wiped by the stale
            # module-global mirror described below, written back by one of the
            # ~50 status/feed polls between the reset and the play — so `prev`
            # was empty, BOTH keep branches missed, and the stamp was lost even
            # though the client had echoed the staged `open-…` id correctly.
            #
            # The hand-off then read `opening` falsy, never rendered the first
            # playable frame, never released the boot gate, and the run sat on
            # the World's cached frame repeating the opening prose. Reported as
            # "it completely just failed live, it just defaulted to the default
            # image".
            #
            # We do not have to inherit it, because we can tell: the opening
            # montage is the one the SERVER stages, so it has mood "approach" and
            # no graph node behind it. Deriving it is one fewer thing that a lost
            # write can take with it.
            opening_stamp = (
                bool(keep.get("opening"))
                or bool(staged.get("opening"))
                or (mood == "approach" and not node)
            )
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
                "opening": opening_stamp,
            }
            if opening_stamp and not keep.get("opening"):
                print(f"[CUTSCENE] {play_id!r} is this run's opening montage "
                      f"(mood={mood}, graph={bool(node)}) — stamping it even "
                      f"though the staged copy did not survive", flush=True)
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
