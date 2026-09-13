# Engine / Game / Level / Character — a design surface you can think in

> **Update:** The `game_design` spec block (Genre / Tone / What threatens you /
> Win condition / Live world anchor — the in-game World Editor's "Story" node)
> described below has been removed. In practice it only ever reached the
> writing (consequences, choices, a soft tone hint in the world rewrite); the
> one field that could reach what the player actually sees — `world_anchor` —
> was tiered `advanced` and unreachable from the graph-based World Editor most
> players use, so the sheet could be filled in completely and the rendered
> world would never move. Rather than keep half-wired knobs around, the block
> was removed; the four-layer taxonomy below (Engine / Game / Level /
> Character) stands, with the GAME layer now covering camera, look and
> narration only. See `CHANGELOG.md` for the removal entry.

## The problem, stated precisely

The whole simulation is exposed and editable, and that is exactly why it's hard
to design with. The prompt surface is organised by **which subsystem consumes
it** — `world`, `narrative`, `image` — which is the engineer's filing system,
not the designer's. To make a level you must know that "the geography lives in
`world_initial_state`, unless `setting_reference` is enabled, in which case it
overrides the geography but not the tone, and the look is somewhere else
entirely."

Three concrete faults:

1. **`world_initial_state` does three jobs at once.** It is the engine's world
   seed, the game's premise and tone, *and* this specific place's geography.
   There is no field whose only job is "this level". So there is nowhere to go
   to design one.

2. **There is exactly one level, and it isn't an object.** `setting_reference`
   is a thin override plate on a single implied place. You cannot author two
   levels, compare them, or switch between them. "Worlds" snapshots the entire
   prompt set, which is *save the whole game*, not *make a level*.

3. **The game's own identity isn't authorable.** The live world model's style
   anchor (`REALTIME_STYLE_ANCHOR`) is a hardcoded constant in `engine.py`.
   Genre, tone, threat model and win condition exist only as prose buried inside
   the world seed. The layer that should say "what kind of game is this" has no
   home, so it leaks into the level.

## The model

Four layers, ordered by how often you touch them and how much damage a mistake
does. Each answers one question:

| Layer | Question | Volatility | Breaking it costs |
|---|---|---|---|
| **ENGINE** | How does a turn *compute*? | Almost never edit | The simulation stops working |
| **GAME** | What kind of game is this? | Once per product | Every level changes |
| **LEVEL** | Where am I and what's here? | Every level | Only this level |
| **CHARACTER** | Who am I? | Per playthrough | Only the cast |

The layers compose downward: ENGINE defines the contracts, GAME tints
everything, LEVEL localises it, CHARACTER inhabits it. A designer works in the
bottom two layers almost exclusively and should be told so.

### Layer assignment (every existing key, no orphans)

**ENGINE** — output contracts and mechanical rulebooks. JSON schemas, format
templates, physics. `action_consequence_instructions`,
`player_choice_generation_instructions`, `world_evolution_instructions`,
`situation_summary_instructions`, `field_notes_format`, `image_camera_rules`,
`gemini_text_to_image_instructions`, `gemini_image_to_image_instructions`,
`gemini_flipbook_4panel_prefix`.

**GAME** — the product's identity, applied to every level.
`image_art_direction`, `image_negative_prompt`, `camera_perspective`, plus a new
`game_design` block: genre, tone, threat model, win condition, and the live
world-model anchor that was previously unreachable.

**LEVEL** — this place. `world_initial_state` (renamed in the UI to *Level
Brief*), `setting_reference`.

**CHARACTER** — `player_character`.

A test enforces that every editable key belongs to exactly one layer, the same
way `unwired_keys()` enforces that every key is reachable.

## What gets built

1. **`prompt_layers.py`** — the taxonomy. Layer metadata (question, volatility,
   risk, accent, order), key→layer assignment, and completeness validation.

2. **A GAME layer that exists.** New `game_design` spec block carrying genre,
   tone, threat model, win condition and `world_anchor`. Wired additively into
   the narrative directive, the world seed, and `build_realtime_base` — closing
   the hardcoded-anchor gap.

3. **Levels as first-class objects.** `levels_store.py`: save / list / load /
   delete named levels. A level snapshots *only the Level layer*, so switching
   one leaves Engine, Game and Character untouched. This is the actual unlock —
   author three levels, flip between them, keep the game the same.

4. **API** — `layers` added to `/api/admin/studio/content`; `/api/admin/studio/levels` CRUD.

5. **A rebuilt editor** organised as the four layers, with the composition made
   visible: a layer stack you can read at a glance, per-layer risk framing so
   you know what is safe to touch, and a Levels gallery.

## Invariants that must not break

Carried forward from the architecture audit; each has test coverage.

- `state.world_prompt` is runtime truth after turn 1. Evolution must keep
  receiving the cast directive and `structure_lines()`, or authored levels drift
  back toward the shipped world.
- The `visual_scene` / `dispatch` split feeds images vs story log.
- `game_identity.apply()` stays surface-specific (`narrative` / `image` / `raw`).
- `render_image_template()` remains the single injection point for
  `{art_direction}` / `{camera_rules}`.
- `image_negative_prompt` is mode-dependent through
  `game_identity.negative_prompt()`.
- World snapshots round-trip every `editable_keys()`.
- New spec fields are additive and normalised, so old worlds and old level files
  still load.

## Test plan

- Layer taxonomy: total coverage, no key in two layers, every layer populated.
- `game_design` block: normalisation, defaults, round-trip, and that each field
  reaches the surface it claims to.
- `world_anchor` overrides the realtime anchor when set and falls back when not.
- Levels store: save / load / list / delete round-trip; loading a level changes
  only Level-layer keys.
- Regression: existing `test_prompts_store.py`, `test_world_authoring.py`,
  `test_game_identity.py` stay green.
