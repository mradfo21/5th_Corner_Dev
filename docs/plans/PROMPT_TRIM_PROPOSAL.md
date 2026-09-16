# Prompt reduction proposal — NOT LIVE

Nothing in this file has been applied. It is for review.

## Why

Measured payload today:

| Template | Words | Proposed |
|---|---|---|
| `action_consequence_instructions` | 2,353 | ~600 |
| `image_camera_rules` | 1,065 | ~150 |
| `image_art_direction` | 599 | ~250 |
| `gemini_image_to_image_instructions` | 477 | ~140 |
| `image_negative_prompt` | 268 | 268 (unchanged) |

One image call currently renders to **14,078 chars / 2,139 words**, of which roughly
40 words describe the actual frame. 155 non-blank lines, 21 majority-caps, 31
containing a prohibition, `CRITICAL` x8, `NEVER` x7.

Nine separate layers legislate the camera in that one call, and two of them
declare precedence over each other:

- `game_identity`: "THIS OVERRIDES ANY CONFLICTING CAMERA LANGUAGE BELOW"
- `gemini_image_to_image_instructions`: "NOTHING BELOW MAY OVERRULE IT"

When precedence has to be asserted the conflict is already structural. With
several assertions, precedence is undefined and the model falls back to the most
*concrete* line in the payload — always the literal `visual_scene` sentence.
That is why capitalised camera directives lose to "the player collapsed on the
red dirt floor just outside".

## Bug 1 (blocking) — camera_rules bypasses game_identity

`prompts_store.render_image_template` substitutes `image_camera_rules` raw:

    camera_rules=PROMPTS.get(CAMERA_RULES_KEY, "") or "",

`game_identity.apply()` only ever runs on the `{prompt}` text inside
`build_image_prompt`. So in third-person mode the payload contains both
"The PLAYER CHARACTER is fully visible — head to feet" and "NEVER show your
face, head, or full body" / "No part of you exists in frame".

Two candidate fixes:

1. Route the shared blocks through `game_identity.retune()` inside
   `render_image_template`. Preserves the existing block, costs one call.
2. Remove all perspective language from `image_camera_rules` so there is
   nothing to retune, leaving `game_identity` as the only voice on perspective.

Proposal below does (2), and (1) as a safety net for custom installs whose
edited camera rules may still contain perspective wording.

## Bug 2 — the consequence prompt authorises the stasis we keep seeing

Current text, verbatim:

> If you struggle, default `visual_scene` to a literal description of the
> environment you were just shown in the CURRENT VISUAL SCENE block, **evolved
> minimally by the action.**

And the only door-related exemplar:

> "Concrete corridor stretches 30 ft ahead, flickering fluorescent overhead,
> **metal door now ajar at the far end** revealing darkness beyond."

That models "the object changes state, the camera stays across the room" — the
exact failure in the playtest. There is no arrival exemplar anywhere.

Meanwhile `MOVEMENT -> moderate to high risk` plus `INJURY IS THE DEFAULT
CONSEQUENCE` give the writer a sanctioned way to cancel a move, while
`ACTIONS COMPLETE IMMEDIATELY`, three sections away, forbids it.

---

# Proposed replacements

## 1. `image_camera_rules` (1,065 -> ~150 words)

Removes every perspective claim (that is `game_identity`'s job and it currently
contradicts it), the duplicated no-text ban, and the eight WRONG/RIGHT framing
examples that exist to say "no macro close-ups". Keeps the physics, the framing
rule, and the gore permission, which all do real work.

```
CAMERA PHYSICS
A real camera carried by a real person. Eye-level height, dropping when crouched,
rising when climbing. It cannot fly, teleport, pass through solid matter, or zoom
— to get closer it has to physically travel closer.

FRAMING
Always show WHERE this is, not only WHAT is being looked at. A subject may
dominate the frame, but keep enough of the surrounding space that the location
stays readable. No macro close-up that fills the frame with one isolated detail
and loses the room.

WHAT MAY BE IN FRAME
Environment, objects, weather, other people, and dynamic action — fire, smoke,
debris, structural failure. Graphic content is permitted and belongs to 1990s
practical effects and medical documentation, never CGI: wounds, remains, blood,
exposed tissue, bone, the red biome's organic growths.

NO OVERLAYS
No text, numbers, timecodes, timestamps, UI, borders, letterboxing, or scanline
overlays. Tape degradation lives in the image itself, never as added graphics.
```

## 2. `gemini_image_to_image_instructions` (477 -> ~140 words)

Drops the five-branch CAMERA STATE parser — asking the model to string-match
emoji markers in its own prompt is fragile, and it duplicates what
`build_image_prompt` already decided. Drops both precedence declarations and the
FULL FRAME block (already covered in camera_rules).

```
THIS FRAME:
{prompt}

The attached image is the PREVIOUS moment. It is your reference for film stock,
grain, palette, light, and materials — carry those over exactly.

It is NOT a composition to reproduce. The text above says where the camera is now
and what has changed. Render that. If the camera travelled or entered somewhere,
the reference shows where you WERE — recompose from the new position and let the
old framing go. If the camera held still, match the reference's height and
direction and let only the world react.

Never return the reference with one detail swapped. Whatever the text describes
has actually happened.

Keep the analog degradation — this is real 1993 tape, not a render. Do not clean
it up, sharpen it, or make it look more professional than the source.

{art_direction}

{camera_rules}
```

## 3. `image_art_direction` (599 -> ~250 words)

`CAMERA & FILM STOCK`, `FOUND FOOTAGE`, `OPTICAL PROPERTIES`, and `TAPE
DEGRADATION` are four sections, ~450 words, all saying "heavy VHS grain,
degraded, muted, blown highlights, crushed shadows". Collapsed to one. The
`NEVER show: robots / sci-fi tech / energy weapons...` bullets are already in
`image_negative_prompt` verbatim, so they are dropped here.

```
WORLD & ERA
1993, X-Files era, and nothing in frame postdates it. Technology is VHS
camcorders, CRT monitors, fluorescent tubes, chain-link, concrete, rusted
industrial plant, 1990s trucks and helicopters. Threats are human guards —
trained, ruthless, shooting for centre mass — and biological ones: mutated flesh
and bone, never machinery. The red biome is organic horror, pulsating tissue and
sinew, never technology. Aesthetic touchstones: The X-Files, Twin Peaks, real
1993 industrial sites.

FILM STOCK
A 1993 consumer camcorder on degraded magnetic tape, thirty years in a basement.
Real optics and a real CCD, not a simulation: prominent grain, colour bleeding,
softness, chromatic aberration from cheap glass, 480i interlacing, 1/60 motion
blur. Auto-exposure hunts and white balance drifts. Highlights blow to white,
shadows crush to black. Heavily desaturated from generation loss. Blair Witch,
Paranormal Activity, V/H/S.

COMPOSITION
Amateur handheld. Nothing perfectly centred, horizon slightly tilted,
unintentional movement, available light only. Documentary realism — capture what
exists rather than what was designed.

HORROR REGISTER
Practical effects: The Thing, The Fly, Event Horizon. Prosthetics, animatronics,
latex, stage blood — tangible and physical, never CGI. Injuries, mutations and
remains get clinical precision and visceral detail.
```

## 4. `action_consequence_instructions` (2,353 -> ~600 words)

The material change is the `visual_scene` contract: the "evolved minimally"
fallback is gone, the door-ajar exemplar is replaced by arrival exemplars, and a
chosen MOVE can no longer be cancelled — cost lands on the body or on what is
waiting, never on the arrival. 106 rule bullets collapse to the ones that change
behaviour. Death fairness is kept, compressed.

```
Return one JSON object with exactly these fields:

{
  "dispatch":     2-4 clipped sentences, second person, present tense — what you
                  feel, hear, and experience.
  "visual_scene": 1-2 sentences — only what the camera physically sees now that
                  the action is finished. No feelings, no sounds, no abstractions.
  "player_alive": true or false
}

Both text fields are mandatory and never duplicates of each other: `dispatch`
feeds the story log, `visual_scene` is the only description the image model gets.

THE ACTION HAPPENED
It resolves fully, this turn. Never "you try to", "you begin to", "you're
halfway". Opened means open and you can see in. Climbed means over. Moved means
arrived. Then show what that costs.

A MOVE ALWAYS COMPLETES
When the action was to go somewhere or enter something, the player gets there.
Every time. Bad luck, danger and injury are expressed in what the trip COST and
in what is waiting on arrival — never by cancelling the trip.
  WRONG: "You lunge for the door, but shearing metal drives you staggering back
         into the dirt."
  RIGHT: "You force the door and drop into the dark, shoulder screaming where
         the frame tore it open."

WRITING visual_scene
State where the camera actually is now.

If the action moved the player, describe THE PLACE THEY ARRIVED AT, from inside
it. The space they left is behind the camera and not in this frame. Do not
describe the threshold, the doorway, or the view back the way they came.
  entered a shed -> "Dirt floor, corrugated walls close on both sides, a
    workbench of rusted tools an arm's length ahead, daylight reduced to a hard
    slot behind."
  walked to the machinery -> "The turbine housing fills the frame, grease-black
    and streaked with rust, the bay's far wall now a distant blur beyond it."

If the action did not move the player, describe the same place with the
consequence visible in it.
  -> "Corridor unchanged, fluorescent tube still stuttering, the locker now
     hanging open with its contents across the floor."

Never write it as a minimal edit of the previous frame. Describe what is
genuinely in view.

CUSTOM ACTIONS ARE LAW
When the player writes their own action, it happens exactly as written, provided
it is physically possible. Never deflect it, never soften it, never have them
merely attempt it. Make it real, then show the world's answer.

THE WORLD IS HOSTILE
Cautious actions usually succeed, but ambient threat never goes away — patrols
move, creatures hunt, structures fail. Loud or exposed actions draw attention.
Confronting an obvious lethal threat is usually fatal. Base the outcome on the
player's actual choice first, established threats second, ambient danger third.

INJURY IS THE DEFAULT, NOT DEATH
Only characters and dramatic events kill. Guards and snipers who are visible or
already established, creatures that maul or infect, explosions the player
triggered, vehicles driven by someone, collapses inside a fight already underway.
Everything ambient — rebar, glass, falls, slips, debris, stray rounds, fumes,
equipment failure — wounds badly and never kills. Before writing a death, ask
whether a film audience would call it cheap. If yes, they survive, marked.
A persistent wound is richer than a fast death.

CONTINUITY AND VOICE
Always "you" and "your", never "Jason" or "he". If there is an existing injury,
reference it at least once — a limp, a flash of pain, blood soaking through.
Anything the player broke, took, or left stays broken, taken, or left. Nothing
lethal arrives that was not already established or visible.

RHYTHM
About seven turns in ten end on escalation. The rest end on stillness — held
breath, ringing ears, sudden silence — especially straight after a high-tension
turn or a stealth save. Constant crescendo goes numb.

TIME
Starts at golden hour and mostly stays there. It darkens one tier when the phase
escalates, and one more at critical. Forward only, no skipped tiers.
```

## 5. Code change: one camera sentence instead of marker blocks

`build_image_prompt` currently prepends emoji-marked blocks
(`YOU HAVE CROSSED THE SPACE`, `SPATIAL ANCHOR`, `FORWARD MOVEMENT`, ...) that
the template then string-matches. Proposed: emit a single plain sentence, since
`hard_transition` / `movement_type` / `softened_move` are all known
synchronously.

    hard_transition  -> "The camera is in a new place now: <scene>. Nothing of
                         the previous location is in frame."
    travelled        -> "The camera has walked to it and is standing at it now,
                         close enough to touch."
    softened_move    -> "The camera has moved forward from where the reference
                         was taken."
    exploration      -> "The camera has panned slightly from where the reference
                         was taken."
    stationary       -> "The camera has not moved from the reference position."

Optional spatial/vision text appends as one clause, not a block.

## Verification plan

1. Apply, then assert with a unit test that a third-person spec produces a
   payload containing no first-person perspective language anywhere, including
   the shared blocks.
2. Assert total rendered image payload is under ~800 words.
3. Re-run the identical 15-turn `scan_move` playtest, same seed and settings,
   and compare frames for the two sequences that failed: the shack (turns 1-4)
   and the corridor (turns 13-15). Success is entering the shack and the camera
   ending up inside it.
