# Simplifying the core of the editor

**Status.** The surface — what a person meets when they open the editor — has
shipped, out of order: the graph is now one red dot that blooms into Level,
Character, Game and Controls, and the windows carry the essential fields only.
That was Phase 5 plus the sheet minimisation, taken first because it is what the
author actually touches.

Everything behind the glass is still ahead: Phases 1–4 below (delete the dead
prompts, one camera authority, contract-as-schema, split the rulebook from the
direction) are unstarted, and the numbers in §6 for context size are therefore
unchanged. The engine's contract prompts, the runtime knobs and the saved
levels/builds were demoted rather than deleted — they live in the flat List
behind the header toggle, which is the "machine room" this plan asks for.

The question behind this document was: *why is the graph this complicated, what are we
exposing that we don't need, and what is actually key to the innovation?* The answer
came out of an audit of what the editor exposes versus what a model actually receives
on a turn. The numbers are measured from the shipped `prompts/simulation_prompts.json`
and traced through `engine.py`, `choices.py`, `game_identity.py` and `prompts_store.py`.

---

## 1. The diagnosis

### The graph is complicated because it is a mirror of storage, not of authoring

`buildTree()` walks the prompt schema and turns **every key in the file into a vertex**.
That is why the graph has **37 nodes**, **18 of which require diving three or more levels**
to reach. Nobody designed a 37-node graph; the graph inherited the shape of a JSON file.

```
37 nodes today
  ├─ 12 prompt vertices        (raw prose fields)
  ├─  4 spec sheets            (structured forms)
  ├─  9 control vertices       (runtime knobs)
  ├─  2 galleries + 2 "new" cells
  └─  8 layers/groups/root     (pure navigation)
```

### The thing we invite you to edit most is not authoring at all

The editor's headline surface is **~57,000 characters of editable prompt prose**. The
single biggest field, `action_consequence_instructions` at **15,180 chars**, is the first
thing you meet inside Engine. Broken down by its own section headings:

| Section | chars | What it is |
|---|---:|---|
| Lethality rules (what may kill the player) | 3,744 | engine mechanics |
| Location anchoring | 2,396 | engine mechanics |
| Custom-action handling | 1,621 | engine mechanics |
| Choice outcome rules / hostile world | 1,655 | engine mechanics |
| Tension rhythm + stillness beat | 1,414 | pacing mechanics |
| Permanence + player agency | 963 | engine mechanics |
| Actions complete immediately | 830 | engine mechanics |
| Output format | 405 | machine contract |
| **Perspective** | **143** | **art direction** |

143 characters out of 15,180 are a creative decision. Everything else is the engine's
rulebook wearing a text box. We have been presenting our own implementation as the
author's canvas, and then wondering why authoring feels overwhelming.

### Two prompts in the graph never reach a model at all

- **`field_notes_format`** (892 chars) is read only by `_world_report()` (`engine.py:3646`),
  which is called only by `begin_tick()` (`engine.py:6353`), which is called only by
  `autotest.py:133`. It is not on the web turn path. It is a vertex you can edit that
  changes nothing.
- **`_generate_dispatch()`** (`engine.py:3694`) has no callers; the live path is
  `_generate_combined_dispatches()`. Dead code that still reads a prompt.
- **`cast.json`** is referenced by no Python file at all.

### The same law is stated three or four times on every image call

For a default first-person game, the image model is told not to show the player's body by:

1. `image_camera_rules` — 6,817 chars, with first-person hardcoded in prose
2. `game_identity.camera_directive()` — the compiled camera sheet, prepended
3. `engine._build_vhs_prompt` — a hardcoded `anti_person` block (`engine.py:4638`)
4. `image_negative_prompt` — 2,268 chars banning bodies, then mode-adjusted by `negative_prompt()`

That is the "input feels overdone" feeling, and it is real: roughly **6–9k characters of
redundant camera law per image**. Worse, it can contradict itself. Choose third person in
the camera sheet and layers 1 and 4 are still arguing for first person while 2 and 3
override them. The retune/reconcile/negate pipeline in `game_identity.py` exists mostly to
win that argument — we built a machine to fight our own prompt.

The same pattern is in the choice call: `choices.py` carries a **~3,200-char hardcoded
system block** repeating bans that `player_choice_generation_instructions` (4,065 chars)
already states.

### The output contract is prose, and it is out of date

`action_consequence_instructions` opens with "**OUTPUT CONTRACT — READ THIS FIRST
(NON-NEGOTIABLE)**" declaring exactly three fields. Then `engine.py:10985` appends a
fourth field, `next_choices`, at runtime. The prompt is lying about its own contract, and
enforcement is `json.loads` with string-scraping fallback (`engine.py:11050`).

Meanwhile we already do this properly elsewhere: `_DETECT_RESPONSE_SCHEMA` and
`_DANGER_RESPONSE_SCHEMA` use Gemini's `responseSchema`. Detection gets a real schema;
the main game loop gets a paragraph of shouting.

### 14 mode selectors, several with two homes

DOOM/FPS lives in both the editor footer and the WORLD MODEL panel. The renderer is
switchable from a control vertex *and* the "Stills" button in the model panel. Image model
is switchable in the editor *and* the admin dashboard. Camera perspective is a 4-mode
picker in the sheet, while Happy Oyster's VIEW offers an overlapping 2-mode override that
silently wins per browser. `world_studio.html` is an entire second editor on the same APIs
that has drifted (it only knows 3 of the 4 sheets and still uses the old four-zone model).

---

## 2. What is actually key to the innovation

Worth stating plainly, because it decides everything below.

**The innovation is the compile step, not the prompt editing.** `game_identity.apply()`
takes roughly twenty structured fields and produces consistent camera, character and place
language across five different model calls — consequence, choices, situation, image,
world-evolution — and keeps them from contradicting each other. *That* is the thing nobody
else has: you describe a world once, and every surface of the engine agrees about it.

Editing 57k characters of prose that we then string-concatenate is not the innovation. It
is the thing we did before we had the compiler, and we never took it back out.

So the editor's job is to make **the sheet** excellent, and to get the rulebook out of the
author's way.

---

## 3. The proposal

### The new core: five sheets, about twenty fields

Root is still one cell. It holds **what a person actually art-directs**:

```
THE GAME
  ├─ The Game     genre · tone · what threatens you · what winning looks like
  ├─ The Look     era/palette · the look anchor · what never appears
  ├─ The Place    name · what it is · landmarks that recur · opening shot
  ├─ You          name · role · look · signature gear
  ├─ The Camera   one perspective picker · show hands
  └─ Builds       snapshots of all of the above
```

Six cells, each opening a sheet directly. **Depth 2 instead of depth 4.** Roughly
**14 nodes instead of 37**. Every vertex is a form you fill in, not a wall of prose. The
Look cell is new: it collects the visual decisions currently spread across
`image_art_direction`, `image_negative_prompt` and the advanced `world_anchor` field.

### The rulebook moves to a machine room

The seven prompts in Engine → Advanced are all marked `risk: contract`, which is our own
schema admitting that editing them breaks the game. They should live in the repo, under
version control, and not in a UI that invites a player to rewrite them:

| Key | chars | Disposition |
|---|---:|---|
| `image_camera_rules` | 6,817 | machine room; gut the POV prose, leave a `{camera}` slot |
| `gemini_image_to_image_instructions` | 3,879 | machine room |
| `world_evolution_instructions` | 1,422 | machine room |
| `situation_summary_instructions` | 761 | machine room |
| `gemini_text_to_image_instructions` | 716 | machine room |
| `gemini_flipbook_4panel_prefix` | 5,696 | behind the flipbook feature flag, not a vertex |
| `field_notes_format` | 892 | **delete** — reaches no model |

Not deleted from the product, *demoted*: reachable behind an explicit unlock (a URL flag or
one hidden cell) so power users lose nothing, but the default surface stops shipping the
engine's guts as the main course.

### Cut the duplication at the source

1. **One camera authority.** The camera sheet compiles the entire camera law.
   `image_camera_rules` becomes a template with a `{camera}` slot; the hardcoded
   `anti_person` block in `_build_vhs_prompt` goes away; `image_negative_prompt` stops
   restating perspective. Saves ~6–9k chars per image and removes the contradiction the
   retune/reconcile pipeline currently exists to referee.
2. **One choice authority.** Either `choices.py`'s system block or
   `player_choice_generation_instructions` owns the bans. Not both. Saves ~3.2k per choice call.
3. **Contract as schema, not prose.** Move the four-field consequence contract into a
   Gemini `responseSchema`, the way detection and danger already do. That deletes the
   OUTPUT CONTRACT preamble *and* the OUTPUT FORMAT section from the 15k prompt, and makes
   the `next_choices` drift impossible.
4. **Split the rulebook from the direction.** What remains of
   `action_consequence_instructions` after (3) is still ~14k of mechanics. Move the
   mechanics into the machine room as `turn_mechanics`, and leave the author a short
   `turn_direction` field — the pacing and perspective decisions, measured in a page.

Estimated effect on a normal turn: consequence call from ~17.7k → **~5k**; image call from
~19.3k → **~11k**. Less context is also cheaper and faster, and it removes the "the model
ignored my instruction" failure mode that comes from burying direction under law.

### Controls: three settings, six session toys

The nine control vertices are two different things wearing one costume:

- **Engine settings** (renderer, world model, image model) — real configuration. Collapse
  to one `Engine` sheet with three pickers, in the machine room, not the art-direction core.
- **Session toys** (autoplay, tape, narrator, debug log, VHS, sound) — not authoring at all.
  They were moved out of the icon rail to fix rail clutter, and landed inside an authoring
  tool. They belong in one collapsed HUD tray. All six already have keyboard shortcuts
  (`P T N D V M`), so nothing becomes unreachable.

Also fix on the way past: `toggleVhs()` and the renderer toggle both call `Sound.toggle()`
(`standalone.js:13928`, `12590`), which looks accidental — turning on film grain should not
mute the game.

### One home per mode

| Mode | Today | Proposal |
|---|---|---|
| DOOM / FPS | editor footer **and** WORLD MODEL panel | WORLD MODEL panel only (it is how you drive, not what the game is) |
| Renderer | control vertex **and** "Stills" in model panel | model panel only |
| Image model | editor **and** admin dashboard | admin dashboard only |
| Camera perspective | 4-mode sheet **and** Happy Oyster VIEW 1ST/3RD | sheet is authoritative; VIEW becomes a read-only reflection |
| Advanced toggle | hides fields in list and graph | mostly disappears once the rulebook is demoted |
| `world_studio.html` | a drifted second editor | retire, or redirect to the real one |

---

## 4. Sequencing

Ordered so that each phase is independently shippable and the risky ones come after the
safe ones have proved the seams.

**Phase 1 — Delete what is provably dead.** `field_notes_format`, `_generate_dispatch()`,
`cast.json`, the nine `_comment_*` keys in the prompt JSON (~1.3k of metadata). No
user-visible change; establishes the audit is right. *Verify:* full e2e suite unchanged.

**Phase 2 — One camera authority.** Gut the POV prose from `image_camera_rules`, delete
the `anti_person` block, give the camera sheet the slot. This is the highest-value change
and it is invisible in the UI — it only makes third-person games stop fighting themselves.
*Verify:* generate a frame in each of the four perspective modes and diff the assembled
prompt; `test_world_authoring` and `test_game_identity` cover the compile helpers.

**Phase 3 — Contract as schema.** `responseSchema` for the consequence call, delete the
prose contract, kill the runtime `next_choices` append. *Verify:* this is the riskiest
step; it needs a soak — the parse fallback at `engine.py:11050` currently hides malformed
output, so instrument it before and after.

**Phase 4 — Split rulebook from direction.** `turn_mechanics` (machine room) and
`turn_direction` (author). Same for the choice call's duplicate bans.

**Phase 5 — Reshape the graph.** Six cells, depth 2, the new Look sheet. Only now, because
the tree is a projection of what is left after 1–4. Reshaping first would just rearrange
the mess. *Verify:* `test_editor_graph_e2e` gets rewritten against the new tree.

**Phase 6 — Rehome the session toys and the duplicate modes.** Retire `world_studio.html`.

---

## 5. What not to do

- **Don't delete the prompt-editing capability**, only demote it. The machine room is how
  we keep the ability to tune the engine live, which has been genuinely useful.
- **Don't merge the four sheets into one long form.** The separation is the point: they
  compose independently, and Builds snapshots them together.
- **Don't reshape the graph first.** Every node removed by Phases 1–4 is a node we don't
  have to design a home for.
- **Don't touch `retune`/`reconcile`/`negate` until Phase 2 lands.** They are currently
  load-bearing precisely because the prompts contradict the sheet; they can only be
  simplified after the contradiction is gone.

---

## 6. The measure of success

| | Before | Target | Now |
|---|---:|---:|---:|
| Graph nodes | 37 | ~14 | **5** |
| Nodes needing a 3+ level dive | 18 | 0 | **0** |
| Editable prose in the default surface (chars) | ~57,000 | ~4,000 | **0** |
| Structured fields in the default surface | 21 | ~20 | **14** |
| Prompts that reach no model | 2 | 0 |
| Places the camera law is stated | 4 | 1 |
| Chars sent on a consequence call | ~17,700 | ~5,000 |
| Chars sent on an image call | ~19,300 | ~11,000 |
| Mode selectors with two homes | 5 | 0 |

The test of whether this worked is not the node count. It is whether a person can open the
editor, describe a place and a character in plain language, and see the world change —
without ever meeting the word "contract".
