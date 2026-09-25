# Characters — who you are, as a thing the game owns

> **Status: SHIPPED 2026-09-24** (phases 1–5; `characters.py`,
> `static/js/characters.js`, `static/js/pack.js`, `test_characters.py`; the
> CHANGELOG entry of that date is the record). What was built differs from the
> body below in these places, on purpose:
>
> - **The screens are the canvas design, not §5's sketch**: black, drawn
>   smoke, the character right, the controls left; select / create (one line,
>   pictures, SURPRISE ME) / developing / reveal (PLAY, TRY AGAIN, CHANGE
>   SOMETHING). No turnaround and no garment line on any screen a player sees;
>   CHANGE SOMETHING is the one-line garment editor.
> - **`player_character` stays in the prompt file and the World snapshots**, and
>   `sync_cast_to_worlds` / the cast branch of `_bind_world_prompts` stay: a
>   run WITHOUT a character (an older save, every harness session) is still the
>   sheet. A run with one never reads it — `get_spec()` overlays the character,
>   and every writer reads `raw_spec()` so the overlay never lands in the file.
> - **Location**: `characters/` beside the repo (`SOMEWHERE_CHARACTERS_DIR`
>   overrides; the authoring sandbox points it at an EMPTY folder, not a copy).
>   The starter ships in `assets/characters/jason-fleece/`, built by
>   `tools/build_starter_character.py` from the spike's render: the blue PRESS
>   flak vest, which is what the runs were written with.
> - **Timing, measured on the real models**: a new character is PLAYABLE in
>   ~23 s and finished at ~47 s. The brief (a text call) runs beside the
>   turnaround instead of before it; the character is marked ready the moment
>   the turnaround lands (`_draw_look`'s `on_ready`) and the drawing screen
>   offers PLAY NOW; the read-back, the hero idle and the face sheet finish
>   behind it, and the screen reveals the character when the POSED portrait
>   exists (~50 s). No portrait is ever cut off the A-pose sheet (a stand-in
>   did that for a morning: "why is he in an A pose"). Before this it was
>   ~55–60 s of waiting to play. A fitting is the same shape: the new look is
>   worn from the next move once its turnaround lands (24 s measured); the
>   pack keeps the old pose, dimmed, until the new one is drawn.
> - **The hero pose** (`_IDLE`) is a character-select shot with attitude —
>   turned from camera, weight back, a step, hands busy — and names the
>   A-pose as what not to copy. `tools/repose_characters.py` redraws only the
>   poses of characters made before that prompt.
>   Taking something off that was worn before is instant (the look cache).
>   `look.json` records `ready_secs` and `secs`.
> - **Fittings always start from the BASE look** with the whole worn set, so
>   putting things on and off never accumulates drift.
> - Not built: an animated idle, NPCs as Characters, a World that locks a
>   character, credit-gating creation on a coin-op build (§8.5).
>
> The original proposal follows.

Asked: *"like a classic RPG, when you enter a world, you need a character. i
propose we have a high level concept, "characters", and this is your
character, a high level being with a customizable look, who you control, with
persistent inventory, and is completely reactive to items you discover and are
wearing … when you click "play" we need to go to the character creation
screen … the generation process needs to consistently produce a transparent
nicely posed version of the character, idling in a cool pose, AND an a pose
character turntable we actually inject into the simulation … in the future …
if you equip an inventory item, etc, it gets rendered as the pose / sheet,
which continuously gets updated into the sim."*

---

## 1. Why the current approach breaks

The character today is three things that are not one thing:

1. **Words** in `player_character` (`appearance`, `wardrobe`,
   `signature_gear`), inside `prompts/simulation_prompts.json`.
2. **A picture**, `character_069acca76b5f.png`, an uploaded image attached to
   every render as the CHARACTER SHEET.
3. **A World's property.** The block is snapshotted into every
   `worlds/*.json`. `sync_cast_to_worlds` and `_bind_world_prompts` exist only
   to stop a World bind from swapping who you are.

The last two days showed each of those failing:

- **The words and the picture disagree, so the vest flips.** The sheet shows a
  black plate carrier with a small PRESS patch. The words say "a blue flack
  jacket with 'press' written across it". The run was drawn from the words.
  Whenever the frame being continued does not show the vest (the reward ends
  on his hands), the model reaches for the picture and the plate carrier
  comes back. Nothing ever checks that the two agree, because nothing
  produced them together.
- **The picture is a poster, not a reference.** It is a hero illustration
  with a mine battle, a tentacle refinery and soldiers behind him. A thinly
  prompted render reproduced that background almost panel for panel. Its
  pose and its illustrated finish leak the same way.
- **It shows only his front, and the camera mostly sees his back.**
  Third-person follow-cam frames the back and the shoulder. The only picture
  of the character has neither.
- **It lives in the one live prompt file.** Every server on the machine reads
  it and every World bind rewrites it. That is how a harness run swapped
  Jason for a stranger mid-run ("i was teleported … with a new character").
  Who the player is should belong to the player, not to a shared file.

So the fix is not a better upload field. It is one record that owns the
character, with the words and the pictures generated **from each other**,
stored where no World and no reset can reach it.

---

## 2. The object

A **Character** is a being the player owns across runs and Worlds:

```
Character
├── identity      name, pronouns, one-line concept (what the player typed)
├── body          build, height class, skin, face, hair       (stable)
├── base outfit   one garment per slot                        (changes when you equip)
├── inventory     every item carried, each marked worn / carried
├── looks[]       rendered versions: the base, then one per equip change
│   └── look      turnaround (A-pose, 4 views), idle hero pose, face sheet,
│                 garment list written FROM the render, which slots were worn
├── current_look  the look the simulation is being shown right now
└── record        runs played, worlds entered, deaths, where each item came from
```

**Slots**, kept few enough to be visual, plus enough to place any item the
game can hand out: `head`, `face`, `torso` (inner), `outer` (jacket or vest),
`hands`, `legs`, `feet`, `back`, `held`. Every item the look book designs
already has a `kind` (weapon, armour, upgrade, spoil…); each kind maps to a
slot (weapon → `held`, armour → `outer`, and so on). That mapping is what
lets an item you pick up go *on* the character.

**One source of truth for the words.** The garment list the narrator and the
image prompts read (`WARDROBE:`, `CARRIED / WORN:`) is **written by a vision
pass over the rendered turnaround**, not by the player or a drafter. The
words describe the picture, so they cannot disagree with it. The player's
prompt is the brief; the render is the truth; the words are read off the
truth.

### Where it lives

`characters/<id>/`, outside `sessions/`, `prompts/` and `worlds/`:

```
characters/<id>/character.json
characters/<id>/sources/                 what the player gave it (refs, photos, sketches)
characters/<id>/looks/<look_id>/
    turnaround.png        RGBA strip: front, three-quarter, side, back — A-pose
    turnaround_ref.jpg    the same on flat neutral grey, for image models
    views/front.png …     each view cut out on its own (the screen's spin)
    idle.png              RGBA hero idle pose (the screen, the pack, the end cards)
    face_ref.jpg          head front + profile, for close-ups and TALK
    look.json             slots worn, garment words, parent look, cost, created
characters/<id>/items/                   plates of carried items, copied out of the world's book
```

- **A reset never touches it.** Resets delete `sessions/` and `.cache/`.
  Characters are the player's, like their tapes.
- **Location.** `CHARACTERS_DIR` env override. The default is
  `ROOT/characters` in the repo (gitignored) and the app's user folder in a
  packaged build (next to where `.env` is looked for, `%APPDATA%/SOMEWHERE`).
- **Test isolation.** `authoring_sandbox.engage()` redirects `CHARACTERS_DIR`
  like every other store, so no suite writes a real character.
- **The run points at it.** A run stores `character_id` and `look_id` in its
  own `state.json`, and every image and prompt surface reads the character
  through that pointer, never through a shared file. Two servers on one
  machine can play two different characters.
- **A starter ships with the game.** It lives in `assets/characters/` (Jason
  Fleece, rebuilt through this pipeline from today's poster and words), so a
  first run with no keys still has somebody.

---

## 3. The generation pipeline

Everything here reuses machinery the game already trusts: the key-colour
props sheet and its chroma cut-out (`look_book._key_colour`, `_cut_out`), the
grid split (`extract_grid_panels`), labelled references (`reference_labels`),
and the image layer's render settings.

```
brief ──► ① TURNAROUND ──► ② CHECK ──► ③ CUT OUT ──► ④ READ BACK ──► ⑤ IDLE + FACE
  ▲            │              │ fail: retry once, then show why
  │            └── one render, four views, one image
  └── the player's words + refs, auto-filled like the Cast tab does today
```

**① The turnaround is one render, all four views in one image.** Four
separate renders never agree on a buckle. One image of the same person from
four sides is the most consistent thing an image model makes. The spec, in
the prompt and as a blank layout reference (the flipbook guide trick):

- one full-length figure per view: front, three-quarter front, profile, back;
- A-pose (arms 30–45° from the body, feet shoulder-width, neutral face);
- orthographic-feeling camera at waist height, head to toe with margin, same
  scale in all four;
- flat, even, shadowless studio light, so the world can light them;
- on a flat key colour (green, or magenta if the character is green, same
  rule as the props sheet);
- no text, no labels, no ground plane, no props on the floor;
- the player's reference images attached, labelled "who this is, not how to
  frame it".

Recommended render: `gemini-3-pro-image` at 2K, 16:9. That gives a figure
about 1,400 px tall and a face about 150 px across. It is the same model the
opening montage uses.

**② Check it before anyone sees it.** One cheap vision call answers in JSON:
four figures? whole bodies? A-pose? the same outfit in all four? a back view
that is really a back? plain background? Plus the mechanical check that the
key is on the border (the cut-out already does this). One automatic retry,
then show the player what failed ("the back view came out as a front").
Never silently accept a bad turnaround, because every frame of every run
will copy it.

**③ Cut out.** The chroma key comes off (`_cut_out`, despill included) to
give an RGBA strip. It is split into four views by connected alpha
components, not equal columns, because models do not respect columns. It is
flattened onto neutral grey as `turnaround_ref.jpg` for image models: they
handle alpha badly, and grey says "no background" better than black. Hair is
the hard case for a chroma key. If the spike shows fringing, add a local
matting pass (BiRefNet or rembg) for the hair band only.

**④ Read it back.** A vision pass over the turnaround writes the garment per
slot, the body line and the palette into `look.json`. Those words are what
`character_visual_sheet`, `protagonist_line` and the narrator read from now on.

**⑤ The idle hero and the face sheet are drawn FROM the turnaround.** Both are
img2img with the turnaround as the only person reference, so they cannot
diverge from it:

- **Idle:** three-quarter view, full body, a characteristic stance (weight on
  one leg, the held item in hand, head turned a little), rim-lit, on key,
  then cut out. This is the showcase, and the simulation never sees it: a
  posed picture leaks its pose.
- **Face sheet:** head and shoulders, front and profile, on grey. It rides
  only into close-ups (TALK portraits, INTERACT dives, a fight's end card),
  where a 150 px face is not enough.

**Cost and time, per new character:** turnaround about 25 s, check about
3 s, idle and face about 15 s each in parallel, so about 45 s end to end and
roughly four image calls ($0.30–0.50 at pro 2K; the spike will measure it).
The screen shows the turnaround the moment it lands and the idle after.

---

## 4. Into the simulation

**The simulation sees the turnaround, and only the turnaround.** The cast-sheet
functions stay as they are (they are how thirty prompt surfaces reach the
character); their *source* changes:

| Today | With Characters |
|---|---|
| `character_reference_paths()` → the uploaded image | the current look's `turnaround_ref.jpg` |
| `authored_character()` → `player_character` in the live prompt file | the Character bound to this run (`state.character_id`) |
| `character_visual_sheet()` → the typed words | the words read off the turnaround (§3 ④) |
| label: "CHARACTER SHEET … copy face, body, hair, and clothes" | "CHARACTER TURNAROUND — this person from four sides in a neutral A-pose. Copy face, hair, build and every garment. The pose, the grey and the four-up layout are NOT the scene." |
| close-ups: the same sheet | the turnaround plus `face_ref.jpg` |

The ordering rules the last two days fixed stay: a continuing turn puts the
START KEYFRAME first and the turnaround second. A fight round puts the cast
photograph first and the turnaround second (never slot 1). The reward
cutscene puts the frame on screen first and the turnaround second.
`TestEveryRoundCarriesThePlayersSheet` and
`test_the_turn_after_it_starts_from_inside…` already pin that at the wire. The
identity audit from 2026-09-23 (fake only the HTTP call, record what each beat
attaches) becomes a permanent test over every beat: turn, standoff, round,
death, reward, the turn after, TALK, INTERACT, CAMP.

**First-person Worlds** keep working: the camera is still the World's
(`camera_perspective`). With a hands-only camera the character contributes
the `hands` and `outer` sleeve slots only (`character_visual_sheet(hands_only=True)`
already has the shape for that).

**The camera stays a World property; the character stops being one.**
`player_character` leaves the World snapshot. `sync_cast_to_worlds` and the
cast-keeping branch of `_bind_world_prompts` are deleted, not maintained.
`prompts_store.unwired_keys()` will flag `player_character` once nothing
reads it, which is the reminder to take it out of the factory prompts.

**The tape gets it for free.** The export pack (`run_tape.export_pack`) ships
the run's look turnarounds beside `shots.json`, so a Seedance/Kling pass has
the same character reference the game used. A mid-run look change becomes a
chapter mark ("Suited up").

---

## 5. The screen

Title → **PLAY** → **CHARACTER** (select one, or Create new → one prompt →
back here with them selected) → **PLAY** → the normal start (World picker, the
run). In a run, the inventory (B) uses the same frame: the character on the
right, the inventory on the left, and wearing something changes him.

**The player-facing design is the "GOD — Characters" canvas** (2026-09-23,
after two rounds of Matt's notes): black with drawn smoke (no backdrop
renders), the character framed right, the controls left, no turnarounds or
slot lists on any screen a player sees. The sketch below was the first pass
and is superseded by it.

Returning players land on the character they played last with CONTINUE
focused, so it costs one Enter. First-timers land on the starter, with the
prompt line inviting them to make their own.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ CHARACTER                                                     ◂  ▸  + NEW │  ← your roster, like the xp strip
│                                                                          │
│   J A S O N   F L E E C E                                                 │  ← the wordmark treatment: 200 weight,
│   Freelance photojournalist                                              │    tracked out, bone on black
│                                                                          │
│                             ▗▄▄▖                                         │
│                            ▐████▌      ← the idle, big, cut out,          │
│                             ▐██▌          standing on nothing but a        │
│                           ▗▟████▙▖         soft floor shadow; breathing     │
│                           ▐██████▌         (a 1% scale loop); follows the  │
│                            ▐█▌▐█▌          pointer by a degree or two       │
│                            ▐█▌▐█▌                                        │
│                          ▔▔▔▔▔▔▔▔▔▔                                       │
│                                                                          │
│   White tee · Blue flak vest, PRESS · Red bandana · Jeans · Camera rig    │  ← the garment line, READ OFF THE RENDER;
│                                                                          │    click one to change just that piece
│   ◐ TURNAROUND     ▫ ▫ ▫ ▫                                               │  ← the four A-pose views; drag the big
│                                                                          │    figure to spin through them
│   ┌────────────────────────────────────────────────────┐  ⌗ REFS         │
│   │ Who are you?  "a salvage diver in a patched suit…" │                  │  ← ONE line. Enter = draw.
│   └────────────────────────────────────────────────────┘                  │    Drop images anywhere on the screen.
│                                                                          │
│                                         REDRAW (R)        CONTINUE ⏎      │
└──────────────────────────────────────────────────────────────────────────┘
```

Principles, all taken from screens that already work in this app (the title
card, the picker, the tape, the fight):

- **The character is the screen.** Typography over the figure, no panels, no
  form. Everything the Cast tab asks for today (pronouns, role, backstory,
  demeanour) is either auto-filled from the one line and the refs or not
  asked at all.
- **The garment line is the editor.** Click "Blue flak vest", type "long
  waxed duster" and only that slot is redrawn: an img2img fitting from the
  current turnaround, the same path an equipped item will take (§6). This is
  the one piece of detail worth exposing, because it is the one the player
  can see.
- **Generating is a show, not a spinner.** The key-coloured turnaround
  develops in, one view at a time as the cut-out finishes. The figure is
  lifted off it into the idle. Honest words underneath, in the game's own
  voice ("Drawing you from behind…"). The first figure appears at about
  25 s; a spinner would hide that.
- **Keyboard first**, like the picker and the tape: ← → roster, Enter
  continue, R redraw, T turnaround, Esc back to the title.
- **It is designed on the canvas first**, as the tape was: the empty state,
  the developing state, the finished character, one garment being changed,
  and the roster.

**Both editors:** the World Editor's and World Studio's Character block
becomes a card (the idle, the name, "Change character →" opening this
screen). Nobody edits a character in two places.

---

## 6. Inventory that you wear

**The pack moves onto the character.** `state["gear"]` is per run today:
`goal.held` reads it, `award_spoil` and `take_prize` write it, and the prose's
own pickups (`items.py`) share the pack's slots. All of them go through one
function that writes `character.inventory` instead, and the run keeps a view
of it. A spoil's or a treasure's plate is copied into
`characters/<id>/items/` when it is taken, because the look book that drew it
belongs to a World and a session.

**Worn vs carried.** Each item in the pack gets WEAR / TAKE OFF. Putting
something on runs a **fitting**:

```
current turnaround + the item's plate + "put this on the <slot>, change nothing else"
      ──► new turnaround ──► check (the new item is there; nothing else changed)
      ──► read back ──► new idle ──► new look, becomes current_look
```

- **Into the sim at the next turn boundary**, never mid-render: the run's
  `look_id` moves, and the next frame's references are the new turnaround.
  The narrator's next beat gets one line, "You are now wearing …".
- **Cached by outfit**: a look is keyed by (base look + the sorted set of
  worn item ids). Taking the helmet off again costs nothing; the old look is
  already on disk.
- **Batched**: equipping three things is one fitting, not three.
- **The gear still does its fight work** (`WEAPON_EDGE`, armour taking a
  killing blow), but only when it is **worn**, and a held weapon is drawn in
  the play-out because the turnaround now shows it in hand.
- **Cost**: one fitting is about the price of one turn's image. The pack
  shows the fitting developing on the figure, the same treatment as creation.

---

## 7. Build order

Each phase ends playable, and each gets the usual changelog entry verified by
playing it.

0. **Spike: can we make it consistently?** (no product code) Twenty varied
   briefs through ① to ⑤. Measure the validator's pass rate, hair edges on
   the key, whether the four views agree, what it costs and how long it takes.
   Decide strip vs 2×2, pro vs flash, whether matting is needed. **This is
   the riskiest part and it goes first.**
1. **The object and the pipeline**: `characters.py` (store, pipeline,
   validator, read-back), `/api/characters/*`, the sandbox redirect, and Jason
   rebuilt from today's poster as the shipped starter. No UI beyond a debug
   page.
2. **Into the simulation**: the source swap in `game_identity`,
   `state.character_id`, the new labels, the face sheet into close-ups,
   `player_character` retired from Worlds, and the all-beats wire test.
   *This alone fixes the vest.*
3. **The screen**: canvas design, then PLAY → CHARACTER → picker, the roster,
   garment editing, and the card in both editors.
4. **Persistent pack**: inventory on the character, item plates copied in,
   worn vs carried, gear effects only when worn.
5. **Wearing things changes you**: fittings, the look cache, the next-turn
   swap, the tape chapter.
6. **Later**: an animated idle (the flipbook machinery, a 4-frame breathing
   loop), NPCs and companions as Characters (the same object, the roster's
   bosses included), a World that suggests or locks a character for an
   authored story.

---

## 8. Decisions that are Matt's

1. **Death and the pack.** Keep everything (the character is a build you grow)?
   Lose what was carried but keep what was worn? Or lose it all (hardcore)?
   *Proposed:* keep everything for now. Revisit once runs are long enough for
   loss to matter.
2. **Character before World, or World before Character?** *Proposed:*
   character first, as asked. A World cannot currently demand a particular
   character; add that only when a story World needs it.
3. **One style across Worlds?** *Proposed:* the turnaround is neutral (flat
   light, true colour) and each World lights and grades them. The same Jason
   in the desert and in the neon level, not a re-drawn one.
4. **A roster or one character?** *Proposed:* a roster (it is nearly free once
   the object exists), with the last-played selected.
5. **What creation costs the player** on a credit build: about four image
   calls per character and one per fitting. Free creation with a cap on
   redraws is the simplest honest rule.
6. **Today's Jason: which vest?** Rebuilding him in phase 1 forces the
   choice: the poster's plate carrier or the run's blue PRESS flak vest.

---

## 9. What this replaces

- `player_character` in the prompt file and every World snapshot, the
  Cast tab's character fields, `save_reference` / `attach_reference_and_fill`
  for the character slot, `sync_cast_to_worlds`, and the cast branch of
  `_bind_world_prompts`.
- Kept: the level plate (`setting_reference`) and the camera
  (`camera_perspective`), which are the World's; the four-stage `apply`
  pipeline; and every call site that asks `game_identity` who is on screen.
  Those call sites are why this is a source swap and not a rewrite.
