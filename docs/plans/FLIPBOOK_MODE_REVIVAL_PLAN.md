# 🎞️ Flipbook — what shipped

A flipbook turn asks the image model for a **grid of panels** instead of a single
still, splits it back into full-quality frames, and plays them as the motion of
that turn. This replaces the plan that preceded it; where the two disagree, this
document is what is in the code.

## Turning it on

- **Editor → Image → Motion.** Three knobs, live, no restart: *Flipbook* (off by
  default), *Frames* (2 / 4 / 8 / 16, default 4), *Frame hold* (ms per frame).
  They are `tunables.py` entries, so `PUT /api/admin/studio/tunables` sets them
  too.
- **`POST /api/flipbook`** `{enabled, frames, frame_ms, session}` — the same three
  settings for ONE session. This is what a Watch render wants: flipbook for
  itself without flipping a global on someone else's game. A session that has
  never been told either way follows the global.

Gemini image provider only. The count trades directly against image size: the
panels are slices of one generation, so 4 frames are twice the width and height
of 16 — raise the image size before raising the count.

## How a turn works

1. `flipbook_active()` decides whether this turn is a flipbook (session state
   over global tunable; never for a CAMERA/viewfinder frame).
2. `_flipbook_generate()` builds the prompt — the action block, the generated
   shape rules (`flipbook.grid_prompt`), the camera block, the authored prose —
   and attaches references in the order Gemini weights them: the previous
   sequence's last panel first (that is where the camera is standing *now*), then
   its first panel, then the layout guide last.
3. The grid comes back as one image. `flipbook.split_grid()` cuts it into lossless
   PNG panels, trimming a hair off each cell so the grid dividers don't flicker as
   borders during playback.
4. The **last panel becomes the turn's still**. Everything downstream that
   understands only one image — history, SCAN, the vision pass, the next turn's
   img2img reference, a client that ignores sequences — gets the frame the action
   ended on. The frames ride alongside on the beat (`metadata.sequence`) and in
   `/api/status.current_sequence`.

If the grid never comes back or won't split, the turn falls through to an
ordinary still. A bad flipbook costs quality, not the turn.

## Playback: frames, not a GIF

An animated GIF would loop itself with no JavaScript at all, and that is what the
old implementation used. It is also 256 colours with dithering, which throws away
exactly the resolution that splitting a high-resolution grid was for. So the
frames are the deliverable and `createSequencePlayer` (standalone.js) animates
them:

| Surface | Policy | Lands on |
|---|---|---|
| Play | once | the last frame — which *is* the turn's still, so nothing jumps |
| Watch stage | loop | keeps looping until the next turn renders |

Frames are decoded before the motion starts, in-between frames swap on the
already-visible layer (`paintActiveScene`) rather than running setScene's 1.5s
crossfade and VCR glitch per frame, and reduced motion skips straight to the
outcome. SCAN and PHOTO keep capturing the still, never a mid-motion frame.

## Guide images

`prompts/flipbook_guide_{rows}x{cols}.png` — empty cells with visible dividers,
attached last as the lowest-weight reference. Regenerate (or add a shape) with:

    python tools/build_flipbook_guides.py

They are **blank on purpose**. `--numbered` writes labelled variants for reading
the order yourself; never attach one. Text in a reference image comes back burned
into the generated panels, and telling the model to ignore text it can see does
not work.

The engine used to point at two template files that were never in the repo, so
every flipbook generation ran with no layout reference at all.

## Shapes

`flipbook.SHAPES` is the one table: `2 → 1x2`, `4 → 2x2`, `8 → 2x4`, `16 → 4x4`.
The splitter, the prompt diagram and the wire request all read it, so the layout
the model is shown cannot drift from the order the frames are read back in. A
square split (4, 16) hands each panel the ratio the grid was generated at; 2 and 8
cannot, and take the arrangement that stays nearest square.

An unsupported count is rounded to the nearest real one rather than raising — a
stale setting must not be able to break a turn.

## Stale authored prompts

Worlds carry their own copy of the flipbook prompt, and every copy written before
the count was a setting says "THE RENDER MUST BE A 4×4 GRID" and walks through
sixteen numbered frames. `flipbook.prefix_is_stale()` spots a prompt describing a
grid we are not drawing and drops it for that turn, falling back to the built-in
shape rules, instead of sending the model two contradictory instructions. The
shipped prompt is shape-agnostic and never triggers it.

## Watch renders

The harness saves each flipbook turn's frames beside its view frame as
`turn_NN_seq_MM.png`; `render_jobs._sequences()` groups them per turn and
`to_dict()` exposes them as `sequences` plus `sequence_frame_ms`. They are kept
out of `frames` deliberately, so the scrubber still has one stop per turn rather
than one per in-between. A render inherits the global flipbook setting.

## Not done

- The finished-run reviewer (`WatchPlayer`) still plays the run's video files; it
  has no per-turn sequence variant when scrubbing a completed run.
- The render setup form has no flipbook control of its own — a render follows the
  global tunable, or the session route above.
- Non-Gemini providers (Krea, fal, Veo) ignore flipbook entirely.
