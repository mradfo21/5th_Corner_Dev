# Lobby + Multi-User Session Framework

This doc describes the lobby splash page and the multi-user session
framework that lets separate visitors each play their own persisted
instance of SOMEWHERE against the same server process.

## User flow

The lobby is deliberately modeled on a **familiar game main-menu flow**
— think Minecraft's Singleplayer world list, or a JRPG save screen —
rather than a marketing landing page. One centered card, two obvious
actions, and picking either reveals exactly the next thing you need:

```
              ┌────────────────────┐
Visitor ──▶   │      SOMEWHERE     │
              │  ▶ NEW GAME        │──▶ optional name / code ──▶ PLAY
              │  ◧ CONTINUE (3)    │──▶ save-slot list ─────────▶ pick a row
              └────────────────────┘
                                               │
                     POST /api/lobby/create    │  (fresh)
                     ────────────────────────▶ │
                              or               │
                     GET  /api/lobby/sessions  │  (list)
                     ────────────────────────▶ │
                                               ▼
                                      ┌──────────────────┐
                                      │  /play?session=X │
                                      │  (immersive UI)  │
                                      └──────────────────┘
```

* `/` redirects to `/lobby`.
* `/lobby` renders `templates/lobby.html` — a single menu card with
  "NEW GAME" and "CONTINUE" buttons. Clicking either expands an inline
  accordion panel right below the buttons (only one open at a time);
  nothing else to scroll past to get into a run. A collapsed `<details>`
  ("How does this work?") holds the 4-step explainer for anyone who
  wants it, out of the way by default.
* The "CONTINUE" button carries a badge with the saved-run count,
  fetched quietly on page load — like a console menu that already
  knows how many save files exist before you open the list.
* The resume list itself is styled as save-slot rows (name, turn count,
  last-played, alive/ended status, a play chevron) — the same shape as
  a world-select screen, not a marketing card grid.
* `/play` (alias of `/standalone`) reads `?session=<id>` and boots the
  immersive UI against that specific persisted run.

## Session id contract

A session id is:

* 1–100 characters
* `[A-Za-z0-9_-]` only
* Case-sensitive
* Never `default` for a lobby-minted run (that slot is shared / demo)

The client-visible codes minted by `/api/lobby/create` are 8-character
base32-ish strings drawn from `23456789abcdefghjkmnpqrstuvwxyz` — no
look-alike characters so they're safe to read aloud or type on mobile.

## API surface

### Lobby-layer endpoints (new)

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/lobby/create` | POST | Mint (or upsert) a session, return the id + `play_url` |
| `/api/lobby/sessions` | GET | List runs for the "Continue" panel |
| `/api/lobby/sessions/<id>` | GET | Probe that a specific id is playable |

These wrap the lower-level `/api/sessions*` endpoints with a shape
tuned to the splash UI.

### Session-scoped game endpoints

The four endpoints the standalone client hits every turn are now
session-aware:

* `POST /api/reset`
* `GET  /api/feed`
* `POST /api/choose`
* `POST /api/regenerate_choices`

Each is wrapped by `_session_scoped` in `api.py`, which resolves the
caller's session id from (in order):

1. `?session_id=<id>` query string
2. `session_id` field of the JSON body
3. `X-Session-Id` request header

before dispatching to the engine's handler. Missing / invalid id
falls through to the `'default'` slot (backwards-compatible).

## Engine-side context switching

`engine.py` keeps one active game state in memory (module globals
`state`, `history`, `_next_feed_item_id`, `history_path`) so the feed
handlers stay simple. To let multiple people play concurrently, that
in-memory slot is treated as a **context** and swapped before each
session-scoped request:

```python
engine.set_active_session(session_id)   # persists current, loads target
engine.get_active_session_id()          # id of the loaded state

with engine.session_context(session_id) as sid:
    # engine.state / history now reflect this session
    ...
# on exit: the state is persisted back to sessions/<sid>/state.json
```

`set_active_session()`:

* saves the currently-loaded state to its own session file first
* loads the target session's state (or creates a fresh default one)
* rebinds `state`, `history`, `history_path`
* advances `_next_feed_item_id` past any ids in the loaded feed_log so
  new items stay monotonically increasing

## Concurrency model

`engine.py` predates multi-user support: its turn pipeline
(`advance_turn_image_fast`, `advance_turn_choices_deferred`,
`_process_turn_background`, scene-image generation, world evolution,
observe/reground, ...) reads and writes a handful of module-global
mirrors (`state`, `history`) as a convenience, always reloading /
saving the *correct* on-disk file for whichever `session_id` was
passed in. That "always pass session_id explicitly to the file I/O"
part was solid from the start; what was **broken until this pass**
is that the standalone feed's turn loop (`_process_turn_background`,
spawned by `/api/choose`) and the reset flow (`_perform_game_reset`,
`generate_intro_turn_feed_items`) hardcoded `session_id = 'default'`
internally, ignoring the caller's actual session entirely. Every
`/api/choose` and `/api/reset` call — for ANY session — was silently
reading and overwriting the `default` slot's save file. That's now
fixed: both thread the caller's real session id through end-to-end
(verified with two sessions taking concurrent turns while `default`'s
`state.json` stays byte-for-byte unchanged).

With that correctness bug fixed, a narrower concurrency question
remains: two DIFFERENT sessions' background work (an in-flight turn,
scene-image render, world-evolution rewrite, or observe/reground) can
legitimately run on separate threads at the same time. Each of those
paths persists its own session's state to disk correctly regardless.
The one shared resource is the in-memory `state`/`history` mirror used
as a same-session-polling convenience (so `/api/feed` doesn't have to
re-read disk on every poll) — `_sync_ambient_state()` only refreshes
that mirror when the session in question is still the *active* one
(per `get_active_session_id()`), so a finished background task can
never leak its data into whichever session is active by the time it
completes. Scene-image generation additionally serializes system-wide
via `TURN_LOCK`, because `_gen_image` reads img2img reference frames
off the module-global `history` (not a parameter) — running two
sessions' image renders at once would otherwise race on that global.
This doesn't add latency for a single active session: a turn only
ever has one image render in flight, and it already ran sequentially
relative to that turn's own choice generation.

What's **not** fully closed: the deepest turn-pipeline functions
(`advance_turn_image_fast`, `advance_turn_choices_deferred`) still
read/write the ambient mirror many times across a multi-second body
(LLM + image calls). If two *different* sessions' turns are being
processed in that exact window at the same time, there's a narrow
chance one turn's intermediate read observes the other session's
mirrored data before its own next reload corrects it — the on-disk
save is always correct either way, but a stray mid-turn read could
theoretically use the wrong `world_prompt` for one generation step.
Closing this completely would mean eliminating `engine.py`'s ambient-
global pattern outright (it's also used by the Discord bot path),
which is out of scope here. For the deployment scale this framework
targets (a handful of concurrent friends, not a public arcade), the
realistic collision window is small and turns already resolve over
several seconds either way. If this becomes a real problem, the two
follow-ups are (a) route all of `advance_turn_image_fast` /
`advance_turn_choices_deferred` through local variables the same way
`_process_turn_background` was, or (b) the horizontal-scaling option
below.

Longer term, once true parallel turns matter, split by session id at
the load balancer and give each process its own subset of sessions —
no client-side change needed.

## Client-side persistence

The lobby stores a small local index in `localStorage`:

* `somewhere.lobby.recent` — array of `{session_id, name, touched_at}`
  for every run the browser has booted or joined (up to 12 rows).
* `somewhere.lobby.last_session` — most recent session id, so the next
  visit can suggest resuming.

Local rows show up in the "Resume" list even if the server has
forgotten about them (marked `LOCAL`), and locally-known rows also
returned by the server get a `◆` recent-badge.

`static/js/standalone.js` reads `?session=<id>` from the URL on load,
stashes it as `SESSION_ID`, and threads it through every game API call
via a JSON body field + `X-Session-Id` header.

## Files added / touched

* **new** `templates/lobby.html` — main-menu card (New Game / Continue
  accordion, no separate marketing sections)
* **new** `static/css/lobby.css` — VHS/CRT dressing kept subtle; layout
  is a single centered card with save-slot-style resume rows
* **new** `static/js/lobby.js` — accordion controller, session mint,
  resume list rendering
* **new** `LOBBY_MULTIUSER.md` — this doc
* `api.py` — `/lobby`, `/play`, `/api/lobby/*` routes;
  `_session_scoped()` wrapper; root `/` now redirects to `/lobby`
* `engine.py` — `set_active_session()`, `get_active_session_id()`,
  `session_context()`, `_sanitize_session_id()`
* `static/js/standalone.js` — read `?session=<id>` on load; thread it
  through `postJSON` / `getJSON` on every request
* `templates/standalone.html` — no template changes; the immersive UI
  is driven entirely from the URL query on the JS side

## Not (yet) covered

* Auth — sessions are keyed by unguessable-ish 8-char codes, but there
  is no login. Anyone with a code can resume that run. This is fine
  for share-a-link demos and unacceptable for anything sensitive.
* Simultaneous multiplayer inside one session — a session is one
  player's run. Sharing a code lets a second browser co-drive the same
  instance, which is a fun emergent behavior but not designed for.
* Per-session realtime WebSocket streams (`/ws/scene_music`,
  `/api/detect/live`) — these still key off the client-supplied
  `session_id` in their own protocols; they don't route through the
  `_session_scoped` wrapper.
* Session GC — old sessions persist indefinitely on disk under
  `sessions/<id>/`. Use `DELETE /api/sessions/<id>` to prune manually.
