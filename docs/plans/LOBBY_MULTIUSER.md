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

With that correctness bug fixed, two DIFFERENT sessions' background
work (an in-flight turn, reset, scene-image render, world-evolution
rewrite, or observe/reground) can still be dispatched on separate
threads at the same time. Every one of those paths persists its own
session's state to disk correctly regardless. The one thing they used
to share — and race on — was the set of module-global mirrors
(`state`, `history`, `_active_session_id`) that `set_active_session()`
swaps on every request. A later code-review pass found the earlier
"just serialize turns with `TURN_LOCK`" fix was **insufficient**:
`TURN_LOCK` only serializes turn threads against each other, but
`set_active_session()` runs in the *request* thread (not under
`TURN_LOCK`) and swaps the shared mirror out from under a running turn.
Because `/api/feed` is polled ~1 Hz by every connected browser, and
each poll went through `session_context()` → `set_active_session()`, a
poll for session B landing mid-turn for session A would flip the global
`state` to B's data; A's pipeline then did `_save_state(state, A)` and
wrote **B's data into A's file**. A stress test that only polled
*after* turns finished missed this entirely; one that polls *during*
processing (the real browser pattern) reproduced cross-session leaks on
every round. All the mirror-race vectors are now closed:

1. **`/api/feed` no longer swaps the mirror at all.** It is registered
   WITHOUT `_session_scoped`; the handler resolves the caller's session
   id from the (thread-local) Flask `request` and reads that session's
   `feed_log` straight from disk — one small JSON read, no shared-global
   touch. This removes the dominant (high-frequency) swap source.

2. **The deep turn pipeline runs fully local.**
   `advance_turn_image_fast` / `advance_turn_choices_deferred` take a
   `local_only=True` flag on the web path: they operate on LOCAL
   `state`/`history` variables (loaded + saved per `session_id`) and
   never touch the module globals, so a concurrent mirror swap can't
   change the data they save. `_gen_image` gained a `history_ref`
   parameter so scene rendering collects its img2img reference frames
   from the caller's local history instead of the global — and
   `_generate_and_append_scene_image` passes it. The Discord bot keeps
   the default (`local_only=False`) and publishes its result to the
   globals its handlers read, via `_publish_ambient()`; the bot runs in
   a **separate process** (see `start_production.sh`) with its own
   globals, so the two paths never share memory.

3. **`TURN_LOCK` still fully serializes state-mutating engine turns**
   (`_process_turn_background`, `_perform_game_reset`,
   `_generate_and_append_scene_image`) so only one turn's compute runs
   at a time. This is the documented "only one turn processed by the
   engine at a time" model — cheap here because per-turn latency is
   dominated by multi-second LLM + image calls, not CPU.

4. **Every remaining ambient-mirror write is guarded and atomic.**
   `_sync_ambient_state()` / `_sync_ambient_history()` write the global
   **only when the session is still the active one**, and now do the
   check-and-set **under `WORLD_STATE_LOCK`** so they're atomic against
   `set_active_session()` (which holds the same lock while it swaps
   `_active_session_id` and `state` together). Without that, the
   `active == session_id` test and the assignment straddled a window in
   which the active session could change, leaving `state` pointing at
   one session while `_active_session_id` named another.

5. **The HUD endpoints are session-aware.** `/api/status` and
   `/api/objectives` used to read the shared `engine.state` global, so
   with two players the HUD showed whichever session swapped the mirror
   most recently (wrong, and flickering). They now resolve the caller's
   session id and read that session's state from disk.

The state-mutating endpoints (`/api/reset`, `/api/choose`,
`/api/regenerate_choices`) still use `_session_scoped` because they
depend on `set_active_session()`'s per-session feed-item-id bump and
metadata creation. That swap is now harmless: nothing correctness-
critical reads the mirror anymore (feed + status read disk; the turn
pipeline is local; scene rendering uses `history_ref`), so a swap
racing a running turn can no longer corrupt it.

Verification: two stress tests, both run with **continuous `/api/feed`
polling during turn processing** (the pattern the earlier test lacked):
(a) 5 sessions reset + take turns simultaneously for multiple rounds;
(b) 5 sessions fire `/api/choose` staggered ~0.8 s apart so later
chooses land mid-turn for earlier sessions. Each round asserts every
session's feed contains ONLY its own unique marker (no leaks, no missing
turns), that no poll snapshot ever observed another session's marker,
and that the shared `default` slot stays untouched. Both pass
consistently; against the pre-review code, test (a) failed immediately
with dozens of cross-session leaks. `/api/status` isolation is checked
too (a turn in session A leaves B's reported turn count at 0).

Longer term, once true *parallel* (not just correct, serialized) turns
matter, split by session id at the load balancer and give each process
its own subset of sessions — `TURN_LOCK` is per-process, so horizontal
scaling restores parallelism with no client-side change needed.

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
