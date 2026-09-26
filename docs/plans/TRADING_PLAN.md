# Trading — characters and worlds that pass from hand to hand

> **Status: design pass in progress, no code (2026-09-25).** The format, the
> flows and the safety lines below stand; the LOOK is being decided on a
> private design canvas (claude.ai/artifact/EeCGBnSJPHNfHRP96CsJyr).
>
> - **Round one was rejected.** Press print, Polaroid and 35mm negative:
>   "this looks cheesy as hell … it needs to be minimal, techy, glitch art,
>   simple and analogue feeling" (Matt). No physical props, no handwriting.
> - **Round two, waiting on Matt's pick:** the card is a still grabbed off a
>   monitor. Near-black, mono type, a mint CHR / WLD label, the code and a
>   pixel-block QR (decodes with cv2 at 250 px, light on dark, so the
>   importer inverts first), one data line. Three treatments: V1 scanlines;
>   V2 one torn RGB-split slice, placed by the card's own code
>   (recommended); V3 1-bit dither.
> - Receiving is a signal locking on: NO SIGNAL snow, TRACKING, LOCKED.
> - Open from the canvas: a file-only card has no code until GET A CODE;
>   where the signer's name comes from (there is no account name); phones
>   can't play, so the /t/ page leads with the code.
## The idea

Old Game Boy trading worked because the thing you traded was **one small,
whole object** and the trade was **one gesture**. Here the objects are
already small: a World's authored part is a few KB of words, and a
Character is one line plus the pictures it was drawn into. So:

**A share is a picture.** A character or a world leaves the game as a
PNG *card*: it looks like a trading card to anyone (portrait or first frame,
name, a line, its record), and it carries the whole thing inside it. Post it
in Discord, text it, email it, drop it in a group chat, put it on a USB stick.
Whoever has ABYSS drags it onto the window and it is theirs. Nobody needs an
account; nothing needs a server.

Then the server is added as a convenience, not a requirement: every card also
has a **trade code** printed on it — a short thing you could read down a phone
— that fetches the same card from `5th-corner.com`. That covers the places that
strip a PNG's insides (X, some messengers, a screenshot), and it makes a link
you can paste anywhere: `5th-corner.com/t/7K3-QX9` shows the card to anyone
with a browser, and opens it in ABYSS for anyone who has it.

Spore did this with creatures in 2008 (the data lived in the PNG); it is the
proof that "the picture is the file" works with players.

## What is on disk today, and what the smallest whole thing is

**A World** is `worlds/<slug>.json` (15–53 KB) + a `.frame.png` plate
(0.3–1 MB) + whatever `assets/references/` ids its blocks point at. But almost
all of those 15–53 KB are the game's own rulebook copied in. Diffed against the
factory, `somewhere` differs in three keys (~10.8 KB), CYBER HORROR in the
same three (~3.6 KB), `yard` by 604 bytes. And `load_world` already fills any
missing key from the factory (`worlds_store.py:190-214`), so **a World that
carries only its place still plays.**

**A Character** is `characters/<id>/`: 8 MB for one look, 61 MB for a
seven-look Musketeer. But what the game *needs* to play one is
`character.json` + the look's `look.json`, `turnaround_ref.jpg` and
`face_ref.jpg` — ~0.45 MB (`bound_block`); ~2.5 MB with the hero pose and
thumbnail for the screens. Characters **cannot** be regrown from their line:
the drawing has no seed, and the same line makes a different face. So the
pictures travel; the line alone would be a different person.

**An Experience** is a 1–5 KB graph whose nodes point at world slugs, plus
lore. Shareable once worlds are: it is a bundle of them.

Nothing exports, imports, or duplicates any of these to a file today; there is
no URL scheme, protocol handler or file association; the hosted service has no
database. The starter-character seeding (`characters.ensure_seeded`) is the
one existing import-from-folder, and the run shot pack (`run_tape.export_pack`)
the one zip exporter — both are patterns to reuse.

## 1. The card format — `abyss-card` v1

A normal PNG with one extra `iTXt`/`zTXt`-style chunk, `abYs`, holding a zip:

```
card.json         {format:"abyss-card", v:1, kind:"character"|"world"|"experience",
                   uid, name, line, made_by, made_at, game_version,
                   record?, passed:[{by, at}], sha256}
payload/…         kind-specific, below
```

The visible image is the card design (§7). The chunk survives a file copy,
Discord, email, USB, Google Drive; it does not survive a recompress or a
screenshot — that is what the printed code is for (§3).

**Character payload (target ≤ 3 MB):**
- `character.json`, **scrubbed**: drop `sources[]` (the uploaded photos — the
  likeness and copyright risk never leaves the machine), `job`, `error`,
  `origin`, `last_played`, and per-item `from`/`world`. Keep `record`
  (runs, deaths, turns, worlds seen) by default — a character who has died
  fourteen times is the story; the sender can untick it.
- The **current look** only: `look.json`, `turnaround_ref.jpg`,
  `face_ref.jpg`, `idle` (re-encoded to WebP), `thumb`. Plus the plates of
  the items it wears or carries (56–246 KB each).
- `voice.description` when the character has one (the audio plan's
  provider-neutral field — the id is redesigned on the receiver's key).

**World payload (target ≤ 1.5 MB):**
- Only the place: `PLACE_KEYS` (`world_initial_state`, `setting_reference`,
  `camera_perspective`) + the game layer (`image_art_direction`,
  `image_negative_prompt`, `narrator_direction`, `camp_scene_prompt`) **only
  where it differs from the factory**.
- **Never** `player_character` (the run's, and in `world.json` it is still
  Ghost) and **never** the engine layer or `DOCTRINE_KEYS`. A card cannot
  rewrite the game's rules — that is both drift control and a safety line
  (below).
- The `.frame.png` plate (JPEG, 1200 px) and `.frame.json` with its `prompt`
  scrubbed of the cast; each `reference_images` id bundled as a downscaled JPEG
  with its label replaced (labels today are upload filenames —
  `"call-of-duty-ghost-skin-3d-model-….webp"`, `"KelseyRowe (1) meshy.png"`).

**Experience payload:** the graph, each world's payload, text lore inline,
image lore capped. Cutscenes are words; their panels are drawn at play time.

**On import** a card is data and is treated that way: size caps per file and
total, schema allowlist, every image decoded and re-encoded (drops anything
hidden in it), text length caps as in the stores, new local ids (a character
`uid` already present asks "keep both / replace"), world slugs and graph ids
remapped, `passed` appended. Imported prose goes into prompts sent under the
receiver's key; the doctrine keys it cannot carry are what keep a stranger's
card from redirecting the game.

## 2. Trade by file (offline, first)

- **Give:** a SHARE button on a ready character (the character screen,
  `.cs-buttons`) and on a world (the Experience picker; the editor graph's
  world inspector — **both editors**: World Studio at `/studio` too). It
  renders the card, writes `Pictures\ABYSS\<Name>.png`, shows it, and offers
  COPY (the image to the clipboard — pastes straight into Discord) and
  SHOW IN FOLDER.
- **Receive:** drop a card anywhere on the window (WebView2 hands the file to
  the page), paste one (Ctrl+V), or RECEIVE on the character screen/
  Experience picker. The card *develops* in — the same developing motif as
  character creation — then it is in the roster.
- **Copy, not move.** The Pokémon trade took the creature away; a file can't,
  without a server keeping score. Both keep it; `passed` is the trace of
  where it has been, and that is the collectible part.

## 3. Trade by code (online)

A small service beside the gateway (`api.5th-corner.com`, distribution plan
G; this part needs no wallet and can go first):

- `POST /t` — the card bytes; returns a code and a delete token. Stored by
  `sha256` (the same card shared twice is one blob). Postgres row
  `{code, sha256, kind, name, created, deleted, reports}` + object storage.
- `GET /t/<code>.png` — the card.
- `5th-corner.com/t/<code>` — a page with the card, OpenGraph image = the
  card (so the link *previews as the card* in Discord, iMessage, Slack),
  **OPEN IN ABYSS** (`abyss://t/<code>`) and GET ABYSS.
- Codes: 6 characters from a 31-letter alphabet without look-alikes
  (0/O, 1/I/L), shown `7K3-QX9` — ~890 M codes, unguessable at a rate limit,
  easy to read aloud. Printed on the card, so a screenshot still works:
  import reads it back off the picture (the code printed in a fixed place,
  plus a small 2D code for OpenCV's `QRCodeDetector` — `cv2` is already a
  dependency; how it looks is a design question).
- **Unlisted by default:** no public gallery, no search. A code is shared on
  purpose, like an unlisted video. A gallery is a later decision (and a
  moderation commitment).
- Upload is the one moment the game sends a player's work to us, so it asks:
  what is in the card, and that anyone with the code can see it.

`abyss://` is registered per user (HKCU, no admin) by Velopack's install hook
and removed on uninstall. `play.py` reads the argument; if ABYSS is already
running it hands the code to the running window (a POST to its own server with
`local_guard.client_headers()`) and exits.

## 4. Safety — what a stranger's card could carry

| Risk | Line |
|---|---|
| A real person's face (characters made from photos of friends) | Sources never leave. A character made **from pictures** says so before SHARE and asks. A `keep_local` flag on a character hides SHARE (Ghost gets it). |
| Someone else's IP (Ghost) | Same flag; the upload runs one vision check on the card ("a recognisable real person or franchise character?") and warns — it does not silently refuse. |
| Prompt injection via world prose | Doctrine/engine keys are never imported; place prose is capped; the consequence model's output is already data. The worst case is a strange run on the receiver's key, not their key leaving. |
| Malformed or huge files | Caps, re-encode, schema allowlist, fuzz test. |
| Abusive uploads | Unlisted codes, a REPORT link on the page, delete token for the uploader, image moderation on upload, rate limits. |

## 5. Order of work

| Step | What | Done when | Days |
|---|---|---|---|
| — | **Design pass (§7)** | Matt picks the card, the give and receive screens | — |
| T1 | `cards.py`: write/read `abyss-card` v1, character kind; scrub + import | Round trip: export → import into a sandboxed roster → it plays in mock and on real models; the same face on the new machine | 3 |
| T2 | Character SHARE/RECEIVE on the character screen, drop + paste | A card made on this machine, sent over Discord, imported on another, played | 2 |
| T3 | World kind + both editors + Experience picker | CYBER HORROR's world travels as a card and opens looking like itself | 3 |
| T4 | Code service + `/t/<code>` page + upload consent | A pasted link previews as the card; the code typed in fetches it | 3 |
| T5 | `abyss://` + running-instance handoff; read the code off a screenshot | Clicking the link on a machine with ABYSS opens the card in the game | 2 |
| T6 | Experience kind (bundle) | An Experience with two worlds and a cutscene travels as one card | 2 |
| Later | Item trading between characters; a public gallery | — | — |

## 6. Tests that encode the invariants

- **A card never carries** `sources/`, `player_character`, any engine-layer or
  doctrine key, an absolute path, or a key-shaped string (assert over every
  file in the zip).
- **Round trip:** export → import → the imported character's `bound_block`
  refs point at files that exist and byte-match the sender's.
- **A stripped card still resolves:** the same card re-encoded with the chunk
  removed imports via its printed code (mocked service).
- **Import can't escape:** zip-slip names, oversize members, a PNG bomb, a
  world card with `action_consequence_instructions` — each refused, nothing
  written.
- **Both editors:** World SHARE exists in `/studio` and the in-game editor
  (in the `test_editor_wiring` pattern).

## 7. For the design pass (before any code)

The game's look — black, drawn smoke, the 1993 photojournalist — should decide
these, not a generic "share sheet":

1. **What a card is in-world.** A press print? A Polaroid with the name in
   marker along the bottom? A contact-sheet frame with the code as the film's
   edge number (`NEG 7K3-QX9`)? The code has to be readable and the 2D code has
   to not look like 2026.
2. **The two kinds side by side.** A character card (the hero pose) and a world
   card (the first frame) must read as one family and be told apart at a
   glance in a chat thread.
3. **What the record shows:** runs, deaths, turns, worlds, `passed` — how much
   before it's clutter.
4. **The give moment:** SHARE → the card appears → COPY / SAVE / GET A CODE.
5. **The receive moment:** drop or code → the card develops in → "Jason Fleece
   is in your roster." The Game Boy feeling lives here.
6. **The web page** at `/t/<code>`: the card alone on black, OPEN IN ABYSS,
   GET ABYSS — it is also a download page for whoever doesn't have the game.
7. **Sizes it must survive:** a Discord preview (~400 px wide), a phone
   screen, a full-size download.

## Decisions for Matt

- Copy (both keep it) vs a real trade (the sender gives it up — needs the
  server to keep score). Default here: copy, with `passed` as the trail.
- Does a shared character bring its record (deaths, runs) by default?
- Is there ever a public gallery? Not in v1.
- What the thing is called — card, print, tape, negative — is the design pass's.
