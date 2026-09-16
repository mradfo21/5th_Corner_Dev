# INTERACT as a Moment

## Why

Scanning only ever offers one verb. INTERACT is disabled, and TALK only appears
on things that can speak — so warehouses, doors, barrels and fences all come
back with nothing but MOVE TO. MOVE is an unconditional hard cut, so the core
loop is scan → teleport → re-imagined world → scan → teleport. Nothing lets the
player deepen the place they are standing in, which is the real source of the
"it doesn't feel like one continuous world" problem.

INTERACT was shelved for a reason that no longer applies. Its original premise
was that the *live* world model would react to a poke in place, and it couldn't
make that read as anything happening. But the game runs on stills now, and the
verb doesn't need the live model at all.

## Shape

INTERACT should feel identical to TALK, because TALK already has the ceremony
that makes a verb land:

1. Zoom in on the scanned object.
2. Resolve into a close-up of that object.
3. Where TALK starts speech, INTERACT presents **choices**, generated the way a
   normal frame's choices are.
4. `X` / exit returns to the scene the player pressed INTERACT from — but
   **regenerated, because they just changed the state of the world**. Not the
   saved frame verbatim, and emphatically not a fresh composition: it is
   img2img off the pre-interact frame, so the player gets *a changed version of
   the scene they were standing in*.

That last point is the whole feature. MOVE is a hard cut to somewhere new;
INTERACT is the same place, altered by what you did to it. The verbs are only
worth having side by side if that difference is visible on screen.

The distinction that matters: this is a **Moment, not a turn**. A turn replaces
the scene and moves the world on. A Moment sits on top of the scene and hands
it back intact. That is what makes INTERACT a way to look closer instead of
another way to get relocated.

## What already exists

Nearly all of it. This is assembly, not new machinery.

| Piece | Where | Note |
|---|---|---|
| Zoom-in + glitch-cut ceremony | `Moments.push(type, payload)`, `moments.js:245` | TALK uses `Moments.push("conversation", {subject})`, `standalone.js:20673` |
| Return to the prior scene | `resumeUnderlay()` on Moment pop | The Moment stack already restores the scene beneath it — this is the exit behaviour, free |
| Object close-up plate | `portrait_mode` + `object_subject`, `gemini_image_utils.py:390,416,1151,1350` | Built for exactly this: close-up of a machine/object, with the anti-person rule so the model cannot invent a face |
| Plate endpoint | `/api/talk/portrait`, called at `standalone.js:20295` | Pass `object_subject: true` |
| Session pause/resume around a Moment | `standalone.js:20401` | TALK already pauses the session for the takeover and resumes after |

## Status

**Done — the changed-scene half.** `interactEnabled()` is on for stills (still
shelved under live video, where the original reasoning holds). The server side
needed nothing: `engine.py`'s `elif interaction` branch is "the refine-from-
current path... the only place a soft continuation is still allowed", so an
INTERACT turn is already refined img2img from the frame the player was looking
at. The comment at `engine.py:15547` had even predicted this verb — "a run that
wants continuous, same-place development belongs on a different verb (INTERACT),
not on a MOVE that sometimes doesn't move."

One caveat to check: that branch *infers* soft-vs-hard from the wording via
`is_hard_transition(choice, dispatch)`. "Interact with the X." should never read
as a relocation, but this is the same guessing that made MOVE a coin flip before
it got a reliable signal. If a scanned object ever trips it into a hard cut,
force soft on the dedicated INTERACT tap the way `is_move` forces hard.

**Not done — the close-up.** This is required, not polish: INTERACT should feel
identical to SPEAK, zooming into the object before offering its choices. It is
more than a flag because the Talk module owns the ceremony (zoom, nameplate,
orb, portrait polling, `savedEnvFrameDataUrl` for the exit restore) and
INTERACT needs that ceremony with choices where the dialogue goes.

## Build order

1. **New Moment type `interact`** alongside `conversation` in `moments.js`. Same
   zoom and cut; the body renders choices rather than dialogue.
2. **Plate**: call the portrait endpoint with `object_subject: true` and the
   scanned object's label as subject. Reuse TALK's retry/fallback path.
3. **Choices**: generate against the object and the current world state. These
   resolve *inside* the Moment — picking one should develop the object, not
   commit a world turn, or the Moment collapses back into being a turn.
4. **Exit**: `X` and Escape pop the Moment. Verify `resumeUnderlay()` restores
   the pre-INTERACT scene rather than triggering a regeneration — this is the
   one behaviour worth testing first, since it's the whole point of the verb.
5. **Enable**: `interactEnabled()` in `standalone.js` (currently hard `false`),
   gated so it stays off on the live-video path where the original shelving
   reasoning still holds.
6. **Harness**: `playtest_app.py` has a `scan_interact` action in its plan that
   currently degrades to MOVE because no INTERACT button exists. Point it at the
   new Moment and assert the scene after exit is byte-identical to the scene
   before — that is the regression that matters.

## Watch for

The same bug class that caused the encounter conclusion issues: a Moment
generating a plate, then something painting over it, or the underlay coming back
as a *new* frame instead of the saved one. Store the pre-INTERACT scene URL
explicitly rather than trusting whatever the renderer last had.
