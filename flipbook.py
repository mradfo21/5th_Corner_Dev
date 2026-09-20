"""Flipbook sequences — one generation, split back into full-quality frames.

A flipbook turn asks the image model for a GRID of panels instead of a single
still. Panel 1 continues from wherever the previous turn's camera ended up, the
last panel is where this turn's action lands, and the panels in between are the
motion that got us there. Split apart again they are the only in-between frames
this build can produce without a live world model.

Two things about the old implementation are deliberately not carried over.

  · **The grid was hardcoded 4x4.** Sixteen panels of a 1K generation are ~256px
    each, which is why old flipbook runs looked like a bootleg of the game. The
    count belongs in a setting, and the useful end of the range is the low end:
    4 panels of the same generation are 4x the width and height of 16.

  · **Playback went through an animated GIF.** GIF is 256 colours with
    dithering, and on photoreal 1993-VHS frames that reads as mud — it throws
    away exactly the resolution that splitting a big grid was for. So the frames
    themselves are the deliverable: lossless PNG crops, animated by the client.
    Nothing in here writes a GIF.

The shapes are chosen to keep each panel as close to the grid's own aspect
ratio as the count allows, because the panels are shown in the slot a single
still would have used. 4 and 16 are exact (a square split of any canvas keeps
its ratio); 2 and 8 cannot be, and pick the arrangement that stays nearest
square rather than producing letterbox slivers.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# frames -> (rows, cols). Only these counts exist: a count whose grid the model
# cannot lay out reliably is worse than no flipbook at all.
SHAPES: Dict[int, Tuple[int, int]] = {
    2:  (1, 2),
    4:  (2, 2),
    8:  (2, 4),
    16: (4, 4),
}

FRAME_COUNTS: Tuple[int, ...] = tuple(sorted(SHAPES))
DEFAULT_FRAMES = 4
DEFAULT_FRAME_MS = 420

# Generated grids come back with the dividers the prompt asked for, and an even
# crop keeps a sliver of each one on the panel edge — which flickers as a dark
# border on every frame during playback. Trimming a hair off each cell costs
# nothing at these resolutions and takes the divider with it.
DEFAULT_INSET = 0.012

# Below this a "panel" is a thumbnail of a thumbnail. Splitting anyway is how a
# turn ends up animating 2x2px mush instead of falling back to a plain still.
MIN_PANEL_PX = 16


def normalize_frames(value, default: int = DEFAULT_FRAMES) -> int:
    """Coerce anything (config string, form value, None) to a real frame count.

    Rounds to the nearest supported count rather than refusing, so a stale
    setting or a hand-edited config downgrades instead of breaking a turn.
    """
    try:
        n = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    if n in SHAPES:
        return n
    if n < FRAME_COUNTS[0]:
        return FRAME_COUNTS[0]
    if n > FRAME_COUNTS[-1]:
        return FRAME_COUNTS[-1]
    return min(FRAME_COUNTS, key=lambda c: (abs(c - n), c))


def shape_for(frames) -> Tuple[int, int]:
    """(rows, cols) for a frame count."""
    return SHAPES[normalize_frames(frames)]


def grid_label(frames) -> str:
    """Human/prompt label for the shape, e.g. "2x2"."""
    rows, cols = shape_for(frames)
    return f"{rows}x{cols}"


def panel_grid(frames) -> List[List[int]]:
    """Frame numbers laid out in reading order — rows of 1-based indices.

    The prompt draws its diagram from this, so the picture the model is shown
    and the order the frames are read back in can never disagree.
    """
    rows, cols = shape_for(frames)
    return [[r * cols + c + 1 for c in range(cols)] for r in range(rows)]


# How far a drawn divider has to stand out from the picture either side of it,
# in luminance, before it counts as one. Measured on real grids: seams deviate
# 60-90 from their neighbourhood, ordinary panel content 20-30.
SEAM_CONTRAST = 34.0
# The most panels a single axis may be split into. Beyond five the cells are
# too small to be frames of anything, and a "grid" that wide is more likely a
# fence, a louvre or a row of windows inside the photograph.
MAX_AXIS_PANELS = 5
MAX_DETECTED_CELLS = 16


def _boundary_score(profile: List[float], pos: int, reach: int = 9) -> float:
    """How sharply the profile departs from its surroundings at `pos`.

    Deliberately measures the size of the departure, not its direction: a
    generated divider can be a dark line, a light one, or (most often) a dark
    line against a bright one, and a detector that only looked for dark bands
    found none of them — the panels themselves are darker than the seams.
    """
    lo = max(0, pos - reach)
    hi = min(len(profile), pos + reach + 1)
    window = profile[lo:hi]
    if len(window) < 5:
        return 0.0
    mid = sorted(window)[len(window) // 2]
    near = profile[max(0, pos - 2):pos + 3]
    return max(abs(v - mid) for v in near) if near else 0.0


def _panels_along(profile: List[float]) -> int:
    """How many panels this axis is divided into. 1 means no dividers.

    Scores the layouts rather than hunting for lines: for each candidate count
    the boundaries are where an even split would put them, and the layout is
    only credible if EVERY one of them is a seam. Taking the minimum is what
    rejects a multiple — in a 3-way split the 2-way boundary falls in the
    middle of a panel and scores nothing.
    """
    length = len(profile)
    if length < 24:
        return 1
    # The LARGEST split whose every boundary is a seam — not the best-scoring
    # one. A 4-way grid contains a perfectly good 2-way boundary down its
    # middle, so scoring alone reads 2x4 as 2x2 whenever the centre divider
    # happens to be the crispest line in the picture.
    best = 1
    for n in range(2, MAX_AXIS_PANELS + 1):
        cell = length / float(n)
        if cell < 8:
            break
        weakest = min(_boundary_score(profile, int(round(i * cell)))
                      for i in range(1, n))
        if weakest >= SEAM_CONTRAST:
            best = n
    return best


def detect_grid_shape(grid_path) -> Optional[Tuple[int, int]]:
    """(rows, cols) the model ACTUALLY drew, read off the dividers it drew too.

    The count in the prompt is a request, not a guarantee. Asked for a 2x2 the
    model has come back with a 3x3 of nine panels — and `split_grid` cut that
    into quarters, so every "frame" was a collage of two and a quarter panels
    with the grid lines still running through it. The turn resolved, four
    frames existed, they differed from each other, and the playback numbers
    looked healthy: nothing anywhere could tell that what reached the player
    was a tiled mess. Seen on an encounter plate, which is the worst place for
    it — that image is the fight.

    Returns None when there are no legible dividers, which is the honest answer
    for a photograph and for any grid drawn without them. The caller then uses
    the shape it asked for, exactly as before.
    """
    from PIL import Image

    try:
        with Image.open(grid_path) as im:
            g = im.convert("L")
            # Downscale the long edge: the seams survive it and the profiles
            # get cheap. Not so far that a thin divider disappears.
            scale = 512.0 / max(g.width, g.height)
            if scale < 1.0:
                g = g.resize((max(8, int(g.width * scale)),
                              max(8, int(g.height * scale))))
            w, h = g.size
            px = list(g.getdata())
    except Exception:
        return None

    cols_profile = [sum(px[y * w + x] for y in range(h)) / float(h)
                    for x in range(w)]
    rows_profile = [sum(px[y * w + x] for x in range(w)) / float(w)
                    for y in range(h)]
    cols = _panels_along(cols_profile)
    rows = _panels_along(rows_profile)
    if rows * cols < 2 or rows * cols > MAX_DETECTED_CELLS:
        return None
    return (rows, cols)


def split_grid(
    grid_path,
    frames,
    out_dir=None,
    stem: Optional[str] = None,
    inset: float = DEFAULT_INSET,
) -> List[Path]:
    """Cut a grid image into its panels, in reading order, as lossless PNGs.

    Panels are written FLAT into `out_dir` (defaults to the grid's own
    directory) because the web layer serves generated images by basename out of
    the session's images/ dir — a subdirectory would 404.
    """
    from PIL import Image

    grid_path = Path(grid_path)
    frames = normalize_frames(frames)
    rows, cols = shape_for(frames)
    # Cut along the lines the model DREW, not the ones it was asked for. When
    # those disagree the drawn ones win: they are what is actually in the file,
    # and splitting a 3x3 as a 2x2 produces four collages with grid lines
    # through them (see detect_grid_shape). The panels themselves are fine —
    # there are just more of them than we ordered — so this salvages the beat
    # into a longer sequence instead of throwing the motion away.
    drawn = detect_grid_shape(grid_path)
    if drawn and drawn != (rows, cols) \
            and drawn[0] >= rows and drawn[1] >= cols:
        # Only ever override UPWARD. A detection that finds FEWER panels than
        # were asked for is far more likely to be a divider we could not see
        # than a model that drew fewer: two near-identical panels of the same
        # sky share an edge with no contrast across it, and a real 2x2 opening
        # duly read as 2x1. Splitting that as drawn would put two frames in
        # every panel — which is the exact fault this is here to prevent.
        # Cutting to the shape we asked for is the safe answer in that
        # direction, and it is what the code has always done.
        print(f"[FLIPBOOK] asked for {rows}x{cols}, the model drew "
              f"{drawn[0]}x{drawn[1]} - splitting what it drew", flush=True)
        rows, cols = drawn
    out_dir = Path(out_dir) if out_dir else grid_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = stem or grid_path.stem

    with Image.open(grid_path) as img:
        grid = img.convert("RGB")
    cell_w = grid.width // cols
    cell_h = grid.height // rows
    dx = int(cell_w * inset)
    dy = int(cell_h * inset)
    if min(cell_w - 2 * dx, cell_h - 2 * dy) < MIN_PANEL_PX:
        print(f"[FLIPBOOK] grid too small to split into {rows}x{cols}: "
              f"{grid.width}x{grid.height}", flush=True)
        return []

    out: List[Path] = []
    for row in range(rows):
        for col in range(cols):
            left = col * cell_w + dx
            top = row * cell_h + dy
            panel = grid.crop((left, top,
                               left + cell_w - 2 * dx,
                               top + cell_h - 2 * dy))
            path = out_dir / f"{stem}_f{len(out) + 1:02d}.png"
            panel.save(path, "PNG", optimize=False)
            out.append(path)
    print(f"[FLIPBOOK] split {grid.width}x{grid.height} grid into {len(out)} "
          f"{out[0].stat().st_size // 1024}KB PNG panels "
          f"({cell_w - 2 * dx}x{cell_h - 2 * dy} each)", flush=True)
    return out


def sequence_from_grid(
    grid_path,
    frames,
    out_dir=None,
    frame_ms: int = DEFAULT_FRAME_MS,
    inset: float = DEFAULT_INSET,
) -> Optional[dict]:
    """Split a grid and describe it as a playable sequence.

    The `still` is the LAST panel, on purpose: it is where the turn's action
    actually ended, so everything downstream that only understands one image —
    SCAN, the vision pass, the next turn's img2img reference, a client that
    ignores sequences entirely — gets a frame that is true for the new moment
    rather than a mid-action blur.
    """
    paths = split_grid(grid_path, frames, out_dir=out_dir, inset=inset)
    if not paths:
        return None
    return {
        "frame_paths": [str(p) for p in paths],
        "frame_count": len(paths),
        "frame_ms": int(frame_ms),
        "grid": grid_label(frames),
        "grid_path": str(grid_path),
        "first_path": str(paths[0]),
        "still_path": str(paths[-1]),
    }


# ── guide images ────────────────────────────────────────────────────────────
# The old code referenced prompts/flipbook_blank_grid_template.png and a
# numbered variant, and neither file was ever in the repo — every generation
# ran with no layout reference at all, which is a large part of why grid
# compliance was a coin flip. These are generated instead of committed as
# binaries so a new frame count comes with its own guide automatically.
#
# Blank is the one that gets attached. A numbered guide is for humans checking
# the reading order: labels sitting in a reference image come back BURNED INTO
# the generated panels, and telling the model to ignore text it can see does
# not work.

def guide_name(frames) -> str:
    return f"flipbook_guide_{grid_label(frames)}.png"


def guide_path(frames, root=None) -> Path:
    root = Path(root) if root else Path(__file__).resolve().parent / "prompts"
    return root / guide_name(frames)


def find_guide(frames, root=None) -> Optional[Path]:
    """The layout guide for this shape, or None if it hasn't been built."""
    path = guide_path(frames, root)
    return path if path.exists() else None


def build_guide(
    frames,
    out_path=None,
    cell: Tuple[int, int] = (384, 216),
    numbered: bool = False,
    root=None,
) -> Path:
    """Draw the layout guide for a frame count: empty cells, visible dividers."""
    from PIL import Image, ImageDraw

    frames = normalize_frames(frames)
    rows, cols = shape_for(frames)
    cw, ch = cell
    line = max(2, min(cw, ch) // 48)
    img = Image.new("RGB", (cols * cw, rows * ch), (24, 24, 24))
    draw = ImageDraw.Draw(img)

    for row in range(rows):
        for col in range(cols):
            x0, y0 = col * cw, row * ch
            draw.rectangle(
                [x0 + line, y0 + line, x0 + cw - line, y0 + ch - line],
                fill=(150, 150, 150),
            )
            if numbered:
                n = row * cols + col + 1
                draw.text((x0 + cw // 2 - 6, y0 + ch // 2 - 6), str(n),
                          fill=(20, 20, 20))

    out_path = Path(out_path) if out_path else guide_path(frames, root)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


def build_all_guides(root=None, numbered: bool = False) -> List[Path]:
    """Every supported shape's guide. Safe to re-run; overwrites in place."""
    return [build_guide(n, root=root, numbered=numbered) for n in FRAME_COUNTS]


# ── prompt text ─────────────────────────────────────────────────────────────

def prefix_is_stale(text: str, frames) -> bool:
    """True when an authored flipbook prompt hard-codes a grid we aren't drawing.

    Worlds carry their own copy of the flipbook prompt, and every copy written
    before the count was a setting says "THE RENDER MUST BE A 4×4 GRID" and
    walks through sixteen numbered frames. Handed that alongside a request for a
    2x2 grid the model has two contradictory instructions and picks one — so the
    caller drops the authored text for that turn instead of arguing with it.
    """
    if not text:
        return False
    frames = normalize_frames(frames)
    low = text.lower().replace("×", "x")

    shapes = {m.replace(" ", "") for m in re.findall(r"\d+\s*x\s*\d+", low)}
    if shapes and grid_label(frames) not in shapes:
        return True

    counts = {int(n) for n in
              re.findall(r"(\d+)[- ]?(?:frame|panel|image)s?\b", low)}
    counts -= {1}  # "panel 1 is the earliest moment" is true at every shape
    return bool(counts) and frames not in counts


def grid_prompt(frames, seconds: float = 2.0, cut: bool = False,
                keyframe_subject: bool = True) -> str:
    """The shape-specific half of the flipbook instruction.

    The grid is a KEYFRAME CONTRACT. Panel 1 is the START KEYFRAME — the
    reference labelled that way, the previous sequence's last panel: same
    camera, same spot, the subject in the same pose, just beginning to move.
    The last panel is the END KEYFRAME — the scene the render instruction
    describes, reached and settled. The panels between are in-betweens of ONE
    motion from the first pose to the last. Every earlier draft of this text
    described the motion ("time advances", "the action has visibly happened")
    without ever saying what the first pose WAS, so the model chose one — and
    with a face-on character sheet sitting in reference slot 1 it chose that:
    panel 1 came back as a portrait walking toward the lens, and the grid spent
    its remaining panels swinging round behind the character to satisfy the
    follow-cam rules. Played back, that is a camera that jumps to a new side of
    the subject every 400ms. Reported as "the interpolation jumps around and
    doesn't transition us from the start to the end".

    ``cut`` — this turn TRAVELS: the engine has already decided the character
    leaves the place the reference shows (MOVE TO, a relocating typed action,
    a door). The first version of the cut rule put the whole grid inside the
    destination ("panel 1 is the first frame inside the DESTINATION … a panel
    that still shows the place they left is a FAILED panel"), because handed
    the continuous-shot rules the grid never relocated at all ("Move to the
    doorway" rendered the same hatch at 0.97 continuity while the still path
    relocated four times out of four). It did relocate — as a jump cut, which
    on a 3P follow cam is the start keyframe thrown away: panel 1 became a
    fresh composition of a character already standing somewhere else. And
    because MOVE TO, "further down the corridor" and every relocating verb
    are cuts, that was most turns of a run. The contract now keeps the START
    keyframe on a cut too and moves the relocation demand to where it
    belongs: the LAST panel has arrived, and it is the last panel that fails
    if it still shows the place panel 1 shows. The take travels; the camera
    goes with it.

    The camera line used to read "does not move … same lens from the same
    spot", which is a locked-off tripod. The follow cam and the body cam both
    travel with the subject, and a tripod rule beside a follow-cam rig is a
    contradiction the model resolves however it likes. It now holds the RIG
    constant — height, side, distance, lens — and lets it travel.

    This is generated rather than authored because it is the one part of the
    flipbook prompt that is a function of the grid: the old prompt was prose
    hand-written for 4x4, with a sixteen-cell ASCII diagram and per-row
    commentary, so any other count silently contradicted the picture it drew.

    The continuity rules below are about ANIMATION — one camera, time moving,
    nothing teleporting between frames. They are deliberately not about what may
    happen in the shot. An earlier draft said "nothing appears or vanishes
    between panels", which reads as a ban on anything new entering frame at all,
    and the model obeyed it over the scene it had been asked to draw: an
    encounter came back as four panels of the player alone inspecting a fence
    while the choices offered to fight a man with bolt cutters who was never
    drawn. A rule meant to keep a shot physically coherent had quietly become a
    rule against events.

    ``keyframe_subject`` — panel 1 also inherits the SUBJECT's spot and pose
    from the start keyframe. Off for the establishing beat, whose reference is
    the montage's deliberately unpeopled plate: there is nobody in it to
    inherit a pose from, and the establishing block places the character.
    """
    frames = normalize_frames(frames)
    rows, cols = shape_for(frames)
    layout = panel_grid(frames)
    width = 4 * cols + 1

    lines = ["+" + "-" * (width - 2) + "+"]
    for row in layout:
        lines.append("|" + "|".join(f"{n:^3}" for n in row) + "|")
        lines.append("+" + "-" * (width - 2) + "+")
    diagram = "\n".join(lines)

    order = ("Time runs LEFT to RIGHT along the row.") if rows == 1 else (
        "Time runs LEFT to RIGHT along a row, then DOWN to the row below.")

    if frames == 2:
        between = ""
    elif frames == 4:
        between = "panels 2 and 3"
    else:
        between = f"panels 2 to {frames - 1}"

    if cut:
        keyframes = (
            f"THIS TURN TRAVELS. By panel {frames} the character has LEFT the "
            f"place the START KEYFRAME shows and ARRIVED at the DESTINATION the "
            f"scene names. It is still ONE unbroken take: the camera goes WITH "
            f"them the whole way. It does not cut.\n"
            f"PANEL 1: the START KEYFRAME's camera, spot and pose — the very "
            f"next instant after that reference, the character launching into "
            f"the move with the destination readable in the depth ahead of "
            f"them. A viewer watching the reference and then panel 1 must not "
            f"see a cut. This is the ONLY panel that may still be dominated by "
            f"the surroundings in that reference.\n"
            + (f"BETWEEN ({between}): the distance being crossed — each panel "
               f"a further stride along the way, the destination growing in "
               f"the depth ahead, what surrounded them in panel 1 sliding out "
               f"behind the camera. Even steps of the same motion, each panel "
               f"strictly later than the one before it.\n" if between else "")
            + f"PANEL {frames}: ARRIVED. The destination fills the depth in "
            f"front of them — they are at it or inside it — and this is the "
            f"frame the shot HOLDS on, so it is a clean, settled composition, "
            f"not a mid-blur. From the START KEYFRAME this panel keeps ONLY the "
            f"film stock, the light and palette (unless the scene says the "
            f"light changed), the camera rig, and the person. A panel "
            f"{frames} that still shows the place panel 1 shows — the same "
            f"walls, the same floor, the same landmarks at the same distance — "
            f"is a FAILED panel.\n"
        )
        setting_rule = (
            f"- The SETTING is one continuous place travelled THROUGH, not "
            f"rebuilt: what falls behind the camera stays behind it, what is "
            f"ahead grows as it is approached, nothing rearranges itself "
            f"between panels.\n"
        )
    else:
        keyframes = (
            f"PANEL 1: the very next instant after the START KEYFRAME (the "
            f"reference labelled as such — the previous sequence's last panel): "
            f"same camera height, same direction, same distance, same "
            f"landmarks"
            + (", and the subject in the SAME SPOT and the SAME POSE as that "
               "reference, just beginning to move" if keyframe_subject else "")
            + f". A viewer watching the reference and then panel 1 must not "
            f"see a cut.\n"
            + (f"BETWEEN ({between}): even in-betweens of ONE motion from panel "
               f"1's pose to panel {frames}'s — each panel strictly later than "
               f"the one before it, the body a further step along the same "
               f"path. Never go backwards in time, never repeat a pose, never "
               f"re-order the beats, never storyboard a different beat.\n"
               if between else "")
            + f"PANEL {frames}: the END KEYFRAME — the scene the render "
            f"instruction describes, reached: the action has visibly happened, "
            f"whatever was being approached has been reached, whatever was "
            f"being opened is open. This is the frame the shot HOLDS on, so it "
            f"must be a clean, settled composition, not a mid-blur.\n"
        )
        setting_rule = (
            f"- The SETTING holds still: walls, doors, vehicles, machinery and "
            f"the horizon stay the same size in the same place. The place does "
            f"not rebuild itself between panels.\n"
        )

    return (
        f"OUTPUT FORMAT: a single image containing a {rows}x{cols} grid of "
        f"{frames} panels, all the same size.\n\n"
        # Stated here as well as at the end, because it kept losing. Panels came
        # back with "T=0s" and "1+6 sec" burned into the corner: the instruction
        # talks about time advancing and hands the model a diagram with numbers
        # sitting inside cells, and it read both as things to draw. A ban 3000
        # characters later did not survive that. Say it before the diagram, and
        # say what the diagram is, immediately after it.
        f"NO TEXT IS DRAWN IN THIS IMAGE. Not a timecode, not a clock, not "
        f"\"T=0s\", not a duration, not a frame number, not a panel label, not a "
        f"caption, not a watermark. The panels contain photographed scene and "
        f"nothing else.\n\n"
        f"{diagram}\n\n"
        f"The numbers in that diagram tell YOU which cell is which. They are a "
        f"key to this instruction and they are NOT part of the picture — do not "
        f"draw them, or anything like them, anywhere in the render.\n\n"
        f"{order} Panel 1 is the earliest moment, panel {frames} is the "
        f"latest. TIME ADVANCES across the panels: they carry the action from "
        f"where the START KEYFRAME left off through to its completion, and by "
        f"panel {frames} the world has moved on. Each step forward should be "
        f"big enough to see — a viewer must never wonder whether two panels are "
        f"the same moment. It is still ONE unbroken shot: time moves, the "
        f"camera does not cut.\n\n"
        f"THESE PANELS ARE ANIMATION FRAMES. They are cut apart and played back "
        f"in that exact order as a single moving shot. Draw them as frames of "
        f"one motion, not as {frames} separate pictures of the same subject. If "
        f"the panels were shuffled the animation would be wrong, so where a "
        f"panel sits in the grid is where it sits in time.\n\n"
        + (f"THE KEYFRAMES — panel 1 and panel {frames} are given, the rest "
           f"are in-betweens:\n" if between else
           f"THE KEYFRAMES — both panels are given:\n")
        + keyframes
        + f"\nLOCKED BETWEEN PANELS (this is what makes it read as one shot):\n"
        f"- The camera does not CUT and does not change its rig: the same lens, "
        f"the same height, the same side of the subject and the same distance "
        f"from them in every panel. When the subject moves, the camera moves "
        f"WITH them exactly as it has been — it never jumps to a new angle, "
        f"never swings round to the other side of them, never turns to face "
        f"them, never becomes a different shot.\n"
        + setting_rule
        + f"- Things that MOVE are free to. Whatever the scene calls for can enter "
        f"the frame, cross it, leave it, be revealed, catch fire or fall over — "
        f"this is a moving shot and something is supposed to happen in it. What "
        f"is forbidden is teleporting: no jumping between panels, no blinking in "
        f"and out, no arriving in one panel and being absent in the next. If "
        f"something enters, it enters continuously and stays.\n"
        f"- The light does not change: same time of day, same sources, same "
        f"direction of shadow.\n"
        f"- Anything that moves travels in ONE consistent direction across the "
        f"panels, by a similar amount each step. A subject must not jump from "
        f"one side of frame to the other, or drift back the way it came.\n\n"
        f"Every panel is a photoreal still from the same camera.\n\n"
        f"NOTHING IN THIS INSTRUCTION IS DRAWN. The panels contain the scene "
        f"and nothing else: no text of any kind, no words, no digits, no panel "
        f"numbers or labels, no timecode, no frame counter, no durations, no "
        f"units like \"sec\", no captions, no watermark, no HUD, no drawn "
        f"borders or gutters inside a panel. Describing the timing above is "
        f"direction for you, not type to render into the picture.\n"
    )
