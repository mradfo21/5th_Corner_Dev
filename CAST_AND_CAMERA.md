# 🎬 Cast & Camera

**Who you play as, the level you play it in, and where the camera sits.**

Everything in this document is implemented by `game_identity.py` and edited from
either **World Studio** (`/studio`) or the in-game **World Editor** (`E` /
backtick while playing).

---

## Why it exists

The simulation was already fully re-authorable — World Studio edits every prompt
that drives it — but three things a player actually cares about were not
addressable at all:

| Question | Before |
|---|---|
| **Who am I?** | Hardcoded prose inside `world_initial_state`, with "Jason Fleece" written directly into ~12 strings across `engine.py`, `choices.py`, `evolve_prompt_file.py`, and `lore_cache_manager.py`. Nothing downstream *knew* a character existed, so the image model was never told what you look like. |
| **Where am I?** | A hardcoded list of five Horizon-facility descriptions in `engine.generate_intro_image_fast`. No way to hand the renderer a reference image of a place. |
| **Where's the camera?** | Not a setting. ~40 hardcoded strings across the prompt JSON, `engine.py`, and every image provider. Asking for third person meant editing all of them, and the ones you missed fought the ones you changed. |

Neither editor had any way to attach an image at all.

---

## The cast sheet

Three structured blocks:

```
player_character     name, pronouns, role, appearance, wardrobe,
                     signature_gear, demeanor, backstory, reference_images[]
setting_reference    name, summary, era, palette, landmarks,
                     opening_shot, reference_images[]
camera_perspective   mode, show_hands, lens, notes
```

They live **inside `prompts/simulation_prompts.json`** under those three keys.
That's deliberate:

- `prompts_store` already hot-reloads that file, so an edit in either editor is
  live on the very next turn with no restart.
- `worlds_store` already snapshots every editable key, so **saving a world
  captures your protagonist, your level, and your camera for free**, and loading
  one swaps the whole package.

Reference images are stored as files under `assets/references/` and referenced by
id, so the JSON stays small and diffable. That location is overridable with
`REFERENCES_DIR`, and production points it inside the persistent disk
(`sessions/_references`) so an uploaded portrait survives a deploy — see
`RENDER_STORAGE_LIMITATION.md`. If a plate does go missing its id is simply
skipped, both in the editor's thumbnails and in the image call.

Everything degrades to the shipped behavior while the sheet is at its defaults —
first person, no named character, no level override. `game_identity.is_active()`
gates the whole system, and every helper is a no-op until it returns True.

---

## Perspectives

| Mode | Feel | Character in frame? |
|---|---|---|
| `first_person` | The camera is your eyes | No (hands optional) |
| `over_shoulder` | Camera rides tight behind you — RE4, The Last of Us | Yes, from behind |
| `third_person` | Full-body follow cam — Tomb Raider, Souls | Yes, head to feet |
| `fixed_cinematic` | Locked dramatic angles you walk into — classic RE, Silent Hill | Yes, small in frame |

Each mode in `PERSPECTIVE_MODES` is a complete contract: the camera header that
replaces `FIRST-PERSON CAMERA VIEW`, the rig description, its image rules,
whether the body is in frame, how the narrator refers to the player, and the
phrases its negative prompt should add and drop. **Adding a mode there makes it
selectable in both editors with no other code changes.**

---

## The four-stage pipeline

Perspective can't just be appended. The shipped prompts are saturated with
first-person language, so a directive at the top would spend the whole request
losing an argument with thirty lines of "NEVER show any part of a human body"
below it. Every prompt surface therefore runs through `game_identity.apply()`:

### 1. Compile
An authoritative block goes **first**, where image models weight hardest:

```
🎥 CAMERA DIRECTIVE — THIS OVERRIDES ANY CONFLICTING CAMERA LANGUAGE BELOW
PERSPECTIVE: OVER-THE-SHOULDER THIRD-PERSON VIEW (Over the shoulder).
RIG: a camera floating roughly one metre behind and slightly above the
     character's shoulder.
• The subject on screen is Wren Alvarez — the character the player is controlling.
• ...

🧍 PLAYER CHARACTER — WHO IS ON SCREEN
IDENTITY: Wren Alvarez (she/her) — salvage diver
APPEARANCE: early thirties, cropped black hair, burn scar across the left jaw
...

🗺️ LEVEL PLATE — THE PLACE THIS RUN HAPPENS IN
LOCATION: The Kettle Yard
LANDMARKS THAT MUST RECUR: the listing tanker, the crane gantry
```

Narrative surfaces get a director's sheet instead of the visual blocks.

### 2. Retune
Perspective nouns already baked into the prompts are rewritten inline,
case-preserving, so `FIRST-PERSON` → `THIRD-PERSON OVER-THE-SHOULDER`,
`POV` → the mode's tag, and *the shipped protagonist's name → yours*.
No-op in first person.

### 3. Reconcile
Lines that flatly contradict the active mode are deleted. Once you've asked to
see your character, "NEVER show your face", "ABSOLUTELY NO PERSON VISIBLE", and
"the camera operator does NOT exist" all have to go — and they live in editable
JSON that can't safely be rewritten word by word, so the whole line goes.

### 4. Negate
The negative prompt is recomputed. The shipped one bans "third person
perspective, over shoulder view, behind character" — which silently fights a
third-person request. Those phrases are dropped and first-person ones added
instead.

---

## Reference plates

Uploaded art is threaded into the image call as **extra img2img references**,
annotated so the model knows what it's looking at:

```
📎 SUPPLIED REFERENCE PLATES — WHAT THEY ARE:
• One reference is a LOCATION PLATE — a photo of the place this run happens in.
  Copy its architecture, materials, palette, and mood. Do NOT copy its framing.
• One reference is a CHARACTER SHEET for Wren Alvarez — the player's own
  character. Copy their face, build, hair, and outfit exactly so they stay the
  same person. Do NOT copy its background, pose, or framing.
```

Without that annotation a character sheet appended to the reference list reads as
"the previous frame" and the model tries to continue the *pose* instead of
reusing the *person*.

Ordering matters, because Gemini weights the first reference hardest:

- **Continuation turns** — the previous frame stays primary (spatial continuity
  has to keep winning), plates ride behind it.
- **Frame 0** — nothing to continue from, so the plates lead. This is what turns
  a photo of a place into an actual opening shot of *your* level.

A character sheet is only attached when the body or hands can appear; otherwise
it just wastes a reference slot and tempts the model into putting a stranger in
frame.

---

## Where it's wired

| Surface | What happens |
|---|---|
| `engine.build_image_prompt` | Every branch returns through the pipeline; the camera header is the active mode's |
| `engine._gen_image_impl` | Attaches identity plates + their annotation; routes frame 0 through img2img when plates exist |
| `engine.generate_intro_image_fast` / `generate_intro_turn` | An authored level plate beats the shipped Horizon openers |
| `engine.reset_game` | Seeds the run's evolving world document with the cast sheet |
| `engine._generate_combined_dispatches` / `_generate_dispatch` | Director's sheet on top of the consequence contract |
| `engine._generate_vision_dispatch` | In third person, `visual_scene` is composed *with* the character in frame — inverted from the first-person rules |
| `engine._generate_situation_report` | Director's sheet on the bulletin |
| `engine._build_vhs_prompt` | Anti-person block gated on the mode; negative prompt recomputed |
| `choices.generate_choices` | Director's sheet; "Jason" and "from his eyes" recast |
| `evolve_prompt_file.evolve_world_state` | Told to preserve the cast sheet verbatim — otherwise the per-turn world rewrite laundered your character back into the shipped photojournalist within a few turns |
| `gemini_image_utils` | `hero_mode` replaces the anti-person block with a keep-the-character block; negatives follow |
| `krea_image_utils` | `_person_rule()` picks the block the mode wants |
| `fal_image_utils` | Per-mode camera tags + character descriptors (SDXL reads tags, not prose) |

---

## API

| Route | Method | Description |
|---|---|---|
| `/api/admin/studio/content` | GET | Now also returns `identity`, `identity_schema`, `identity_defaults`, `identity_preview` |
| `/api/admin/studio/identity` | GET | The cast sheet plus everything it compiles to |
| `/api/admin/studio/identity` | PUT | Merge a partial update (any subset of blocks and fields) |
| `/api/admin/studio/identity/reset` | POST | Back to first person, nobody, nowhere |
| `/api/admin/studio/reference` | POST | Store a base64 data-URL plate; `attach` wires it into the slot |
| `/api/admin/studio/reference` | DELETE | Delete a plate and unwire it |
| `/api/studio/reference/<id>` | GET | Serve a plate (not admin-gated — inert images behind unguessable ids, rendered in plain `<img>` tags by an editor that has no token to attach) |

Every write normalizes through `game_identity._normalize`, so unknown fields are
dropped, types are coerced, free text is capped at 600 chars, and reference ids
are pattern-checked. A malformed payload can't wedge a turn.

---

## Editing it

**World Studio** (`/studio`) — a new leftmost **Cast & Camera** zone with three
cards. Each opens a structured form: typed inputs, a 2×2 perspective picker,
drag/drop/paste image zones with thumbnails, and a live pane showing the exact
text the block compiles to.

**In-game World Editor** (`E` or backtick) — the same three blocks as a leading
**Cast & Camera** tab, so you can redirect the game mid-run without leaving it.

Text fields save on blur rather than per-keystroke: every save recompiles the
directive server-side, and typing a sentence shouldn't fire forty of them.

---

## Related: the shared image direction

The two image templates (`gemini_text_to_image_instructions` for the first frame,
`gemini_image_to_image_instructions` for every continuation) used to be
near-duplicates — 81 of their ~130 lines were identical — so redirecting how the
world *looks* meant editing the same paragraphs twice and hoping they stayed in
sync. The shared material now lives in two fields:

| Field | What it is |
|---|---|
| `image_art_direction` | **The creative dial.** Era, allowed subject matter, camera and film stock, palette, degradation, horror register. This is the one field to edit to redirect the world's look. |
| `image_camera_rules` | The mechanical rulebook: POV, human-body camera physics, framing distance, what may appear in frame, no-text/no-border bans. Rarely touched. |

Both are substituted into the templates through `{art_direction}` and
`{camera_rules}` by `prompts_store.render_image_template()`, which every image
provider now renders through. Each template keeps only its genuine delta: a
first frame has nothing to continue from, and a continuation has a reference
frame to honour as a spatial lock.

Deleting a placeholder is legal — you might want a fully bespoke template — but
it disconnects that render path from the shared direction, which is invisible
from the resulting image. Both editors show a live warning under the field when
that happens, and the save API returns it as a non-blocking advisory.

Templates that predate the split (no placeholders) render exactly as before,
since they still have all that material written inline.

## Tests

```bash
python3 -m unittest test_game_identity -v
```

49 offline tests covering normalization, all four pipeline stages, the reference
store (including path-traversal rejection and size limits), every mode
compiling, and — most importantly — that the defaults are a genuine no-op.
