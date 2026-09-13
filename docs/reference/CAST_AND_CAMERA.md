# 🎬 Cast & Camera

**Who you play as, the level you play it in, and where the camera sits.**

Everything in this document is implemented by `game_identity.py` and edited from
either **World Studio** (`/studio`) or the in-game **World Editor** (backtick,
or the EDIT rail button, while playing).

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

Three structured blocks. Each field carries a **tier**: the editors show the
essentials and fold the refinements behind one line you can click, because
twenty equal-looking inputs is where a form stops reading as "who am I and
where am I" and starts reading as paperwork.

```
player_character     ESSENTIAL  name, role, appearance ("Look"), reference_images[]
                     ADVANCED   wardrobe, signature_gear, pronouns, demeanor, backstory

setting_reference    ESSENTIAL  name, summary, landmarks, opening_shot, reference_images[]
                     ADVANCED   era, palette

camera_perspective   ESSENTIAL  mode, show_hands
                     ADVANCED   lens, notes
```

Eleven controls to author a whole world; twenty if you want every dial.
`test_world_authoring.CastSheetSurfaceTestCase` asserts the essentials alone are
enough to drive every compile stage — "advanced" hiding required input would be
worse than showing everything.

**The `enabled` toggle is a switch, not a gate.** Filling in any field on a
switched-off block switches it on, because nothing about naming your character
means "and don't use them". As a gate it was the worst kind of trap: you'd write
a protagonist, watch every field save successfully, watch the game ignore all of
it, and have nothing on screen explaining why. Turning it off explicitly is
respected, so you can still A/B a character without deleting them — and
`wiring_notes()` says so while it's off.

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

Every image provider gets them, not just the default one — a plate that only
survives on Gemini is a plate that vanishes the moment someone switches presets
for speed:

| Provider | How |
|---|---|
| Gemini | Behind the continuity frame(s); frame 0 is built out of the plates. If img2img comes back empty it retries from the plates **alone** rather than dropping to text-to-image, which used to make the recovery frame the one frame in the run without the character in it |
| Krea | Behind the continuity frame(s) as style references; frame 0 seeded from them |
| fal | SDXL takes exactly one reference, so continuity keeps the slot — but on frame 0 there is nothing to continue and the plate takes it |
| Veo | As reference frames. Deliberately NOT as `first_frame_path`: that is the literal first frame of a generated video, and a portrait there produces eight seconds of that portrait |
| OpenAI | Appended to the `/images/edits` reference list, which also routes an authored frame 0 through edits rather than a bare generate |

---

## The compact half of the sheet

Roughly a dozen prompts are too small to carry the directive. A realtime
world-model prompt is capped at 2000 characters; a vision-analysis prompt stops
answering in the requested format if you bury the format under 400 characters
of preamble; a talk persona is three sentences. Bolting the full CAMERA / CAST /
LOCATION block onto those either blows the budget or drowns the instruction.

They still have to know who and where — and until they did, they all quietly
carried the shipped Four Corners photojournalist while the still frames carried
yours. So each has a one-line counterpart:

| Helper | What it is |
|---|---|
| `vantage()` | The camera rig as one clause: *"third-person over-the-shoulder vantage, the camera trailing a metre behind the character"* |
| `motion_clause()` | How to say "the player just did X": *"the camera follows as Wren Alvarez"* / *"the view shifts as you"* |
| `movement_clause()` | The style note on a camera-motion re-steer |
| `scene_floor()` | The neutral prompt a live re-steer falls back to when there is no scene yet |
| `place_line()` | The level with palette and landmarks |
| `place_summary()` | Just the level's name and description, for prompts that carry their own look |
| `protagonist_line()` | The character with appearance, wardrobe, and gear |
| `scene_grounding()` | Camera + place + character, at most three lines |
| `world_anchor(default)` | Retunes a shipped style anchor's perspective and appends the level's era, palette, and lens. Takes `include_character` / `include_vantage` for surfaces whose subject isn't the player |
| `structure_lines()` | WHO / WHERE / ENVIRONMENT / TONE for the world-evolution skeleton |

All of them return `""` when nothing is authored, so callers concatenate
unconditionally and the shipped text stands. (`motion_clause`,
`movement_clause` and `scene_floor` are the exception: they always return a
complete clause, because their callers are substituting them for a sentence
rather than appending to one — at defaults it's the first-person sentence that
was previously hardcoded there.)

---

## The half the browser writes

The live world model is the default renderer, and the browser is not a passive
display in front of it. It **builds** the world — `create_world` takes its own
`perspective`, fixed for that world's lifetime — and it **re-steers** that world
on every movement, nudge and idle drift between turns, composing those prompts
itself so the video reacts now instead of at the end of the next turn.

None of that knew a cast sheet existed. `perspective` came from a per-browser
localStorage toggle defaulting to first person, and the re-steers were hardcoded
first-person prose (*"the view shifts as you…"*, *"Smooth continuous
first-person motion"*). So authoring a character and asking to see them
redirected every still frame and every server prompt, and then the client built
a first-person world out of them and spent the gaps between turns arguing it
back — with nothing on screen explaining why the character never appeared.

`game_identity.live_camera_contract()` is the fix: one small object, compiled
server-side, carrying the perspective the world is built with plus the clauses
the client composes re-steers from.

| Delivered by | When |
|---|---|
| `GET /api/camera` | At boot, before the first scene lands, so the world is built with the right camera rather than corrected afterwards. Not admin-gated — the audience is the game, and it carries no authored prose |
| `GET /api/reactor/config` | At connect, so the renderer has it without a second round trip |
| `preview()["camera"]` | On every editor save, pushed straight into the running renderer — the client only fetches `/api/camera` once |

The renderer resolves its perspective `?perspective=` → the **VIEW** switch in
the WORLD MODEL panel → the authored camera → first person. Picking a camera in
the editor clears the VIEW override and rebuilds the world, so the switch lands
on the turn you made it: a toggle flipped in some earlier session outliving the
editor is the same trap the `enabled` flag used to be.

The world model has one first/third switch and no vocabulary for
over-the-shoulder versus locked-off, so every mode that puts the body on screen
maps to `third_person` and the finer framing rides in the prompt.

---

## Where it's wired

| Surface | What happens |
|---|---|
| `engine.build_image_prompt` | Every branch returns through the pipeline; the camera header is the active mode's |
| `engine._gen_image_impl` | Attaches identity plates + their annotation; routes frame 0 through img2img when plates exist |
| `engine._flipbook_camera_block` / `_flipbook_action_block` / `_flipbook_shot_block` | A 16-panel grid is ONE image, so its wrapper blocks are inherited by every panel. They shipped hard-wired to a chest-mounted body cam that invalidated "camera following a character" — the exact shot the third-person modes ask for |
| `engine.build_realtime_base` | `world_anchor` + the level's geography. This path has no negative prompt, no directive, and no reference plates, so the anchor is the entire contract |
| `engine.realtime_action_beat` | Follows the character instead of the eyes in third person — off the same `motion_clause` the browser uses, so the two halves of the live loop can't drift apart |
| `reactor_renderer.js` | `create_world({perspective})`, resolved from the authored camera |
| `standalone.js` `Camera` | The contract on the client: the scene floor, the nudge beat, and the movement style note |
| `engine._vision_analyze_all` | `scene_grounding` on top, and the worked example is no longer a Horizon truck on sandy desert. Its output is the spatial anchor for the NEXT frame, so this is a continuity loop — whatever vocabulary it answers in is what the next image is built from |
| `engine._detect_self_rule` | SCAN ignores your hands in first person, and your character in third — otherwise the protagonist gets tagged as an anonymous figure you can walk up to and talk to |
| `engine._perceive_danger` | Grades the danger to whoever the camera is actually watching |
| `engine.generate_intro_image_fast` / `generate_intro_turn` | An authored level plate beats the shipped Horizon openers, and `_intro_world_seed` stops replacing the whole world document with a one-sentence prologue |
| `engine._perform_game_reset` / `reset_state` | Both seed the run's world document through `world_brief`. `/api/reset` — what the editor's **Save & Restart** calls — used to seed the raw `world_initial_state` |
| `engine.generate_intro_turn_feed_items` | Opening narration, the first frame, and the opening choice slate all come off the level plate when there is one |
| `engine._generate_combined_dispatches` / `_generate_dispatch` | Director's sheet on top of the consequence contract; the free-will header gets its own pass |
| `engine._generate_vision_dispatch` | In third person, `visual_scene` is composed *with* the character in frame — inverted from the first-person rules |
| `engine._generate_situation_report` / `_world_report` / `generate_directive` | Director's sheet on the bulletin, the field notes, and the objective lead |
| `engine._generate_random_starting_time` | Weather that belongs to the level, not to the desert |
| `engine._build_camp_prompt` / `_build_camp_realtime_prompt` | Camp keeps its own art direction but takes the level's terrain and the active camera |
| `engine.build_portrait_prompt` | A portrait is a deliberate register change, so it takes the level's era and palette and keeps its own framing |
| `engine.build_talk_context` / `_narrator_script` | Anyone you talk to knows where they are and who you are |
| `engine._build_vhs_prompt` | Anti-person block gated on the mode; negative prompt recomputed |
| `choices.generate_choices` | Director's sheet; "Jason" and "from his eyes" recast |
| `evolve_prompt_file.evolve_world_state` | Told to preserve the cast sheet verbatim, and its section skeleton (WHO / WHERE / ENVIRONMENT / TONE) comes from `structure_lines`. Its output IS the world state every other prompt reads next turn, so a skeleton reading "ENVIRONMENT: Four Corners desert" dragged an authored world back a little further every turn |
| `veo_video_utils._build_veo_cinematic_prompt` | Veo takes one text prompt and no negative prompt, so its hardcoded "NEVER show the player character" was unarguable |
| `gemini_image_utils` | `hero_mode` replaces the anti-person block with a keep-the-character block; negatives follow |
| `krea_image_utils` | `_person_rule()` picks the block the mode wants |
| `fal_image_utils` | Per-mode camera tags + character descriptors (SDXL reads tags, not prose) |

### One caveat about `reconcile`

Stage 3 deletes whole **lines**. A caller that hands it a prompt written as one
long line can therefore have the entire thing deleted by a single offending
clause, which fails silently — an empty image prompt renders whatever the model
feels like. `reconcile` now declines to apply when it would remove everything,
and callers that build a prompt from a list of clauses should join with `\n`
rather than a space so removal stays surgical.

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
| `/api/camera` | GET | The camera as the *playing* client needs it (not admin-gated — see "The half the browser writes") |
| `/api/reactor/config` | GET | Now also carries `camera` |

Every write normalizes through `game_identity._normalize`, so unknown fields are
dropped, types are coerced, free text is capped at 600 chars, and reference ids
are pattern-checked. A malformed payload can't wedge a turn.

---

## Editing it

**World Studio** (`/studio`) — a new leftmost **Cast & Camera** zone with three
cards. Each opens a structured form: typed inputs, a 2×2 perspective picker,
drag/drop/paste image zones with thumbnails, and a live pane showing the exact
text the block compiles to.

**In-game World Editor** (backtick, or the EDIT rail button) — the same three blocks as a leading
**Cast & Camera** tab, so you can redirect the game mid-run without leaving it.

Text fields save on blur rather than per-keystroke: every save recompiles the
directive server-side, and typing a sentence shouldn't fire forty of them.

### Seeing what a field actually did

Both editors used to show one shared blob per card: the director's sheet for the
character and level cards, the image directive for the camera card. Half the
fields therefore looked like dead controls — appearance, wardrobe, era, palette
and the landmark list compile into the **image** blocks and appear nowhere in
the director's sheet, so typing into them changed nothing on screen.

`game_identity.block_preview()` now returns the text each card is individually
responsible for, and both editors render it split by destination: *Sent to the
image model*, *Negative prompt (recomputed for this camera)*, *Sent to the
writer*. The in-game editor adds a **Where else this reaches** block showing the
compact forms above, because "does any of this get past the still frames?" was
the question the editor gave no way to answer.

`game_identity.wiring_notes()` covers the cast sheet's internal dependencies,
which are real and invisible from the form. A character's appearance is only
ever sent to the image model when the camera can see them, so authoring a
detailed look in first person with hands hidden legitimately does nothing to the
picture — the editor now says so instead of going quiet.

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

## Related: the four prompts

The cast sheet answers *who and where*. Everything else the simulation does is
driven by `prompts/simulation_prompts.json`, and four of its fields do the
redirecting: `world_initial_state`, `action_consequence_instructions`,
`player_choice_generation_instructions`, and `image_art_direction`. The rest are
mechanical rulebooks — camera physics, the negative prompt, the two image
templates, the between-turn bulletin — and both editors fold them behind the
same disclosure this sheet uses.

Every key in that file is read by a live code path and reachable from both
editors: `prompts_store.unwired_keys()` is asserted empty, so a prompt you can
save but that changes nothing can't accumulate again. (Four had:
`timeout_penalty_instructions` alone was 7.7KB of dead text that the README told
you to edit.)

---

## Tests

```bash
python3 -m unittest test_game_identity test_world_authoring -v
```

`test_game_identity` covers the sheet in isolation: normalization, all four
pipeline stages, the reference store (including path-traversal rejection and
size limits), every mode compiling, and — most importantly — that the defaults
are a genuine no-op.

`test_world_authoring` covers the wiring, which is what was actually broken. It
authors a world (a named diver in a flooded shipbreaking yard, watched from
behind — nothing about it resembles the shipped desert) and asserts surface by
surface that the world reaches it, *and* that the same surface is byte-identical
to the shipped text while the sheet sits at its defaults.
