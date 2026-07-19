# Lobby + Multi-User Session Framework

This doc describes the lobby splash page and the multi-user session
framework that lets separate visitors each play their own persisted
instance of SOMEWHERE against the same server process.

## User flow

```
              ┌──────────────┐        ┌──────────────────┐
Visitor ──▶   │  GET /lobby  │  ────▶ │  Splash page:    │
              │  (splash)    │        │   • start new    │
              └──────────────┘        │   • resume saved │
                                      │   • join by code │
                                      └────────┬─────────┘
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
* `/lobby` renders `templates/lobby.html` — a VHS/analog-horror splash
  that explains the game and gates the entry.
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

Turns serialize on `WORLD_STATE_LOCK`. Two users on two sessions can
each read / write their own on-disk state and interact with the game,
but only one turn is being processed by the engine at a time.

That trade-off is acceptable because:

1. Per-turn work is dominated by LLM + image API latency (multi-second
   external calls), not by CPU on our box. The lock is unlikely to be
   the actual bottleneck.
2. It keeps the engine changes tiny — we didn't have to rewrite every
   global-state reference in `engine.py` (~7000 lines).
3. It's a good foundation for horizontal scaling later. Once we want
   parallel turns, split by session id at the load balancer and give
   each process its own subset of sessions — no client-side change.

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

* **new** `templates/lobby.html` — splash + panels
* **new** `static/css/lobby.css` — VHS/CRT lobby styling
* **new** `static/js/lobby.js` — session mint + resume list controller
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
