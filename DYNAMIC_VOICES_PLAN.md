# Dynamic Character Voices — Implementation Plan

> **Status:** Phases 1–3 are implemented on this branch. See
> `voice_design.py`, the wiring in `engine.py` / `api.py`, the client
> hot-swap in `static/js/standalone.js`, and `test_voice_design.py`
> (30 offline tests + 1 live-gated integration test). The plan below is
> preserved as the design record.


**Goal:** stop mapping every SCAN subject to the same 5-voice `by_kind` roster and
instead **design a voice per character on demand** from the character's own
description (label + kind + scene + persona), then **delete the voice at session
end** so the workspace's ElevenLabs slot quota stays bounded. Voices become as
dynamic as the world does — a "rusted intercom" sounds like a rusted intercom,
not like a stock synthetic "River"; a "wounded courier hiding in the drainage
tunnel" sounds like that specific person, not the generic "Eric".

The existing static `voices.json` cast stays as the **fallback / cold-start**
path so nothing regresses when the API key is absent, the design call fails, or
the workspace's slot quota is full.

---

## 1. Where this plugs into the existing code

The wiring is already 90% in place — the persona pipeline emits exactly the
kind of prompt Voice Design wants, and the TTS/Convai paths already resolve a
`voice_id` per turn. The design/cleanup layer is what's missing.

| Existing seam | File / symbol | What it does today | What changes |
|---|---|---|---|
| Voice registry | `voices.json`, `engine.VOICES_CONFIG`, `engine.get_voice_registry` | Static list of ~11 preset voices, `by_kind` map, named `cast` | Untouched. Becomes the fallback tier. |
| Voice validation | `engine._valid_voice_id` (`engine.py:186`) | Accepts only ids listed in `voices.json` | Extended to also accept ids in the new **designed-voices cache**. |
| Per-kind default | `engine.resolve_voice_for_kind` (`engine.py:200`) | `by_kind[kind] || default_voice` | Wrapped by a new `resolve_voice_for_subject(subject, session_id)` that prefers a cached/newly-designed voice for that subject and falls back to `resolve_voice_for_kind`. |
| TALK context builder | `engine.build_talk_context` (`engine.py:5327`) | Assembles persona prompt from label, kind, scene, recent events, vision snapshot | Adds a sibling `build_voice_design_brief(subject, session_id)` that produces the natural-language voice-design prompt from the same inputs (age, gender-hint, tone, accent, timbre, delivery, register). |
| TALK session endpoint | `engine.api_talk_session` (`engine.py:5503`) | Resolves voice, mints signed URL, returns overrides + dynamic vars | Calls new resolver; if a designed voice is not yet ready, returns the fallback voice AND a `voice_status: "generating"` hint so the client can hot-swap when it lands. |
| Live TTS | `engine._tts_synthesize` (`engine.py:5671`) + `_speak_script` (line 5701) | POST `/v1/text-to-speech/{voice_id}` | No change — a designed `voice_id` works here transparently. |
| Convai override | `overrides["tts"] = {"voice_id": …}` at `engine.py:5594` | Live voice switch on the voice agent | No change — designed ids flow through the same slot. |
| Session lifecycle | `engine.delete_session` (`engine.py:423`), `engine.reset_state` (`engine.py:7243`), `api.api_delete_session` (`api.py:785`) | Wipes per-session dirs | Add a hook that sweeps every designed voice tagged with that `session_id` before wiping the dir. |
| Client voice picker | `static/js/standalone.js` — `postJSON("/api/talk/session", …)` (line 6135), `showVoiceControl` (6198), `localStorage "talk_voice_id"` (6062) | Renders preset picker, sends chosen `voice_id` back | Add a "auto (designed)" affordance that when selected clears the stored id so the server-side per-subject resolver runs. Show the generated voice's description in the tooltip. |

Everything else — scene audio, feed loop, image gen — is untouched.

---

## 2. New module: `voice_design.py`

A single self-contained module, styled after `scene_audio.py` (same idioms:
`is_available()`, coalescing inflight lock, quiet degradation, no crashes).

```python
# High-level surface (draft — not final)
def is_available() -> bool: ...

def brief_for_subject(subject: dict, session_id: str) -> dict:
    """{'description': str, 'sample_text': str, 'labels': dict, 'key': str}"""

def get_or_design_voice(subject: dict, session_id: str,
                        wait: float = 0.0) -> dict | None:
    """Returns {'voice_id', 'source': 'cache'|'designed'|'fallback',
                 'description', 'status': 'ready'|'generating'|'failed'}.
    wait=0 -> non-blocking (kicks off async job, returns fallback).
    wait>0 -> block up to N seconds for an in-flight job to complete."""

def release_session_voices(session_id: str) -> dict:
    """DELETE every voice tagged session_id=<id>. Idempotent."""

def sweep_orphans(max_age_hours: int = 24) -> dict:
    """Reconcile with /v1/voices: delete anything labelled source=somewhere-dyn
    older than max_age_hours whose session_id is no longer active."""

def cache_snapshot() -> list[dict]:  # for the admin dashboard
```

### 2.1 The voice-design brief

`brief_for_subject` reads the same context `build_talk_context` uses (subject
label + kind, world premise, scene descriptor, time of day, recent events,
vision snapshot) and emits a **structured voice-design prompt**. Structure
matters — ElevenLabs' Voice Design responds much more consistently to a
brief that names the levers explicitly than to a free-form paragraph.

Template (filled from context):

```
A {age_bucket} {gender_hint} voice for a {kind} named "{label}" in a 1993
analog-horror world.

Timbre: {timbre}                # e.g. "gravelly, worn, faint chest resonance"
Age: {age_bucket}               # child / teen / young adult / adult / older / elder
Accent: {accent}                # regional cue derived from world premise
Delivery: {delivery}            # measured / clipped / halting / rapid / breathy
Emotion: {emotion}              # from current chaos_level + phase + recent events
Register: {register}            # whisper / conversational / raised / commanding
Environment: {env}              # e.g. "distant, filtered through a corroded PA"
                                #      "close-mic'd, room tone of a damp basement"
Character notes: {notes}        # 1-2 sentence flavor from the label + scene

Do NOT include: music, background sound effects, singing, non-speech noises.
Speak the sample line as this character would speak it, once, cleanly.
```

- `age_bucket`, `gender_hint`, `timbre`, `accent`, `delivery`, `register` are
  each derived by small deterministic classifiers over `label` + `kind` +
  `world_prompt`, with sensible defaults ("adult", "unspecified", "neutral").
  All classifiers are string-in / string-out so they're unit-testable and
  cheap. **No LLM call in the hot path.**
- `emotion` is derived from `chaos_level` and `phase` — chaos ≥ 7 pushes
  "frayed, breath-short"; `phase == "climax"` pushes "urgent".
- `env` is chosen by keyword hits on the label: `"intercom" | "radio" |
  "speaker" | "phone"` → filtered/PA style; otherwise close-mic'd default.
- The **sample text** is one short, character-appropriate line lifted from
  the persona's opening line if we already have it (context.opening_line),
  otherwise a canned neutral line ("There's someone else down here. Stay low
  and don't say my name.") — this text is what ElevenLabs synthesizes into
  the preview, so it should sound like *something the character would say*.

### 2.2 Cache key

```
key = sha1(
    session_id + "|" +
    normalize(label) + "|" +
    normalize(kind) + "|" +
    world_prompt_hash    # so a world regeneration = fresh voice cast
).hexdigest()[:16]
```

Session-scoped so the same "warden" in two runs sounds different — dynamism is
the point. Cross-session identity would be a future extension.

### 2.3 On-disk cache: `sessions/<session_id>/voices.json`

```json
{
  "generated_at": "2026-07-18T23:59:00Z",
  "voices": {
    "<key>": {
      "voice_id": "abc123…",         // real ElevenLabs voice id
      "label": "warden",
      "kind": "person",
      "description": "<the brief we sent>",
      "sample_text": "…",
      "generated_voice_id": "gv_…",  // preview id kept for re-generate/remix
      "created_at": "…",
      "last_used_at": "…",
      "status": "ready"              // ready | generating | failed
    }
  }
}
```

Living inside the session dir means `delete_session` already cleans up the
metadata for free — the sweep just needs to hit the ElevenLabs API for the
actual voice-slot cleanup.

### 2.4 Async design pipeline

```
POST /v1/text-to-voice/design
  body: {voice_description, model_id:"eleven_ttv_v3", text: sample_text,
         auto_generate_text: false, guidance_scale: 25, loudness: 0.5}
  → { previews: [{generated_voice_id, audio_base_64, ...}, ...] }

POST /v1/text-to-voice/{generated_voice_id}
  body: {voice_name: "<session>_<label>_<key8>",
         voice_description: <brief>,
         labels: {"source":"somewhere-dyn",
                  "session_id": session_id,
                  "subject_label": label,
                  "subject_kind": kind,
                  "created_at": iso}}
  → { voice_id: "<real id>" }
```

- Picks preview `[0]` by default (they're all "on-brief" candidates; we don't
  need auditioning UI in v1). Optional future: expose the other 2 previews as
  "re-cast" affordances in the TALK UI.
- Rate-limited to N concurrent design calls process-wide (start N=3) with a
  simple semaphore, mirroring the `_INFLIGHT_LOCK` pattern from
  `scene_audio.py`. Additional requests coalesce onto the in-flight job for
  the same cache key.
- Per-session **design budget** (default: 8 designs / session). When
  exhausted, subsequent subjects fall back to `by_kind` for the rest of the
  session. Keeps credit spend bounded no matter how chaotic a scene gets.
- On any HTTP failure: mark `status: "failed"`, cache the failure with a
  15-minute TTL (so a flapping upstream doesn't retry every SCAN), fall
  through to `by_kind`.

### 2.5 Delete-to-save-slots

- **On session end** — hook into `engine.delete_session` and `engine.reset_state`
  to call `voice_design.release_session_voices(session_id)`.
  `DELETE /v1/voices/{voice_id}` for each cache entry; treat 404 as success;
  swallow errors and continue.
- **On slot pressure** — before a design call, if the cache has ≥ SOFT_CAP
  voices for _other_ sessions, evict the LRU one (delete + drop from its
  session's cache file). SOFT_CAP is `min(workspace_voice_quota - 2, 20)`,
  discovered lazily via `GET /v1/user/subscription` (cached 24 h).
- **Startup + periodic sweep** — `voice_design.sweep_orphans()` called from
  `api.py` startup and again every 6 hours: list `/v1/voices?search=...`,
  keep only ones tagged `source=somewhere-dyn`, delete any older than 24 h
  whose `session_id` label doesn't match a live session on disk. Catches
  orphans left by crashes.
- **Refcount guard for live convai calls** — a small in-memory
  `{voice_id: refcount}` incremented at `api_talk_session` and decremented at
  a new `/api/talk/end` (client emits on widget close). `release_session_voices`
  will skip any id with refcount > 0 and retry after a 30-second grace period.
  This is the safety valve against yanking a voice mid-conversation (line 5594
  passes the id to Convai as a live TTS override — deletion would break the
  next synthesis).

---

## 3. API surface

Additive; no existing endpoint changes shape.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/talk/session` *(existing)* | Now resolves via `voice_design.get_or_design_voice(subject, session_id, wait=0.6)`. Adds `voice_status`, `voice_description` to the response. |
| `POST` | `/api/talk/end` *(new)* | Client posts `{voice_id, session_id}` on widget close so refcounts drop. Fire-and-forget from the client. |
| `GET`  | `/api/talk/voice/status?key=<cachekey>` *(new)* | Poll for a still-generating designed voice; response `{voice_id, status}`. Client can hot-swap the Convai override when it flips to `ready`. |
| `POST` | `/api/talk/voice/regenerate` *(new, optional v2)* | `{subject, session_id}` — force a new design (charge again, evict current cache entry). Powers a "re-cast" button. |
| `POST` | `/api/admin/voices/sweep` *(new, admin-token gated)* | Manual trigger for `sweep_orphans`. Handy during dev. |
| `GET`  | `/api/admin/voices` *(new, admin-token gated)* | Cache snapshot (session_id, label, voice_id, created_at, last_used_at) + workspace slot usage. |

`static/js/standalone.js` changes are small:

- On `showVoiceControl(voice_id)` (line 6198), also render the designed
  voice's `description` in a tooltip so the player knows the voice was
  hand-cast for this character.
- If `voice_status == "generating"`, briefly display "casting voice…" and
  poll `/api/talk/voice/status` every 1.5 s (max 20 s). On success, swap the
  Convai override by re-posting to `/api/talk/session` with the new id
  (`opening_line` is passed to avoid re-generating the greeting — the existing
  reuse path at line 6328 already handles this).
- On widget close, POST `/api/talk/end`.

---

## 4. Configuration

All optional; defaults are safe (falls back to today's behavior).

| Env var | Default | Effect |
|---|---|---|
| `ELEVENLABS_DYNAMIC_VOICES` | `1` | Master switch. `0` disables the whole feature. |
| `ELEVENLABS_TTV_MODEL` | `eleven_ttv_v3` | Voice Design model. |
| `ELEVENLABS_DESIGN_BUDGET_PER_SESSION` | `8` | Max designs per session before fallback. |
| `ELEVENLABS_DESIGN_CONCURRENCY` | `3` | Process-wide semaphore. |
| `ELEVENLABS_VOICE_SOFT_CAP` | *(auto)* | Overrides the discovered slot ceiling. |
| `ELEVENLABS_VOICE_SWEEP_HOURS` | `6` | Orphan sweep cadence. |
| `ELEVENLABS_VOICE_MAX_AGE_HOURS` | `24` | Orphan age threshold. |
| `ELEVENLABS_VOICE_LABEL_TAG` | `somewhere-dyn` | The `source` label used for scoping the sweep. |

Nothing added to `voices.json`; the presets are still the only static thing.

---

## 5. Rollout phases

**Phase 1 — Design-only, feature-flagged.**
- Add `voice_design.py` with the brief builder, design call, cache
  read/write. Wire `resolve_voice_for_subject` and update
  `api_talk_session` to call it with `wait=0` (always returns fallback
  immediately; design runs async).
- Wire `release_session_voices` into `delete_session` + `reset_state`.
- Ship behind `ELEVENLABS_DYNAMIC_VOICES=1`. No client changes yet: the
  player still hears the fallback voice the first time; the *next* TALK with
  the same subject uses the designed voice.
- **Verifiable outcome:** two consecutive TALKs with the same "warden" —
  first uses the preset, second uses a voice designed from the warden's
  description. Session delete removes the designed voice from
  `GET /v1/voices`.

**Phase 2 — Hot-swap + status endpoint.**
- Add `/api/talk/voice/status`, add polling + Convai override refresh in
  `standalone.js`, add `voice_status` in the TALK response, brief wait
  (`wait=0.6`) in `api_talk_session` to catch the fast path when Voice
  Design returns quickly.
- Add `/api/talk/end` + refcount guard.
- **Verifiable outcome:** first TALK with a new subject starts on the
  fallback within <200 ms of the SCAN tap, then swaps mid-conversation (or
  by the second line) to the designed voice without dropping the call.

**Phase 3 — Slot management + admin surface.**
- Add `sweep_orphans`, LRU eviction, workspace quota discovery, startup
  sweep, `/api/admin/voices*` endpoints, admin dashboard panel.
- **Verifiable outcome:** running 20 sessions back-to-back on a Starter
  plan (10 slots) never fails to design a voice; slot count stays at ≤ 8
  between sessions.

**Phase 4 (optional) — Re-cast + audition UI.**
- Keep all 3 preview `generated_voice_id`s from the first design call so
  "re-cast" is free (no additional design charge — just save a different
  preview). Add a small `/api/talk/voice/regenerate` fallback for when the
  cached previews are exhausted or the player wants something else.
- **Verifiable outcome:** player can cycle a subject through 3 candidate
  voices without extra credit spend.

---

## 6. Cost & risk

- **Per-turn TTS cost is unchanged.** A designed voice bills at the same
  per-character rate as a preset.
- **Design cost is bounded** by the per-session budget (default 8) × preview
  cost per design (roughly ~1 k characters of TTS-equivalent). With Phase 4
  reusing the 2 unused previews, a "re-cast" is free.
- **Slot exhaustion is the primary failure mode**, mitigated by
  (a) delete-on-session-end, (b) LRU eviction, (c) orphan sweep, (d) a
  documented fallback to `by_kind` at the cap.
- **Convai mid-call deletion** is the primary correctness risk, mitigated
  by the refcount guard + grace period.
- **Content drift** — a Voice Design output can occasionally not match the
  brief. Mitigation: keep the preset fallback one keystroke away in the
  existing voice picker (already implemented at `standalone.js:6313`) and
  offer a "re-cast" in Phase 4.
- **Content policy** — ElevenLabs Voice Design has moderation. Any brief
  that gets rejected is caught in the design-failure branch and logged;
  the fallback voice ships and the cache is marked failed for 15 min.
- **Paid-tier requirement** — Voice Design needs Starter+ on the ElevenLabs
  account. When the tier check fails (probed once at startup via
  `/v1/user/subscription`), the feature auto-disables and logs it; behavior
  is byte-identical to today.

---

## 7. Testing

- `test_voice_design.py` (new, unit):
  - `brief_for_subject` produces expected fields for a matrix of subjects
    (person / creature / machine / animal, with/without world_prompt,
    high/low chaos).
  - Cache read/write/eviction is idempotent; corrupted cache file recovers.
  - `release_session_voices` calls DELETE once per id and tolerates 404.
- `test_voice_design_integration.py` (new, gated on `ELEVENLABS_API_KEY`):
  - End-to-end: design → save → TTS a line with the designed id → delete →
    confirm the id is gone from `/v1/voices`.
  - Refcount guard: `release_session_voices` skips a refcount>0 id and
    reaps it on retry after decrement.
- Extend `test_realtime_e2e.py` with a scenario that opens two TALKs with
  the same subject in one session and asserts the second call returns
  `source == "cache"`.

---

## 8. Out of scope for this plan

- Cross-session voice identity (a recurring character sounding the same
  across runs). Would need a global (not session-scoped) cache key and a
  policy for when identity should be preserved vs. re-cast.
- Voice cloning from a real audio sample (`/v1/voices/add`). Different
  product, different consent/legal surface.
- Narrator-side dynamic casting (`/api/narrator/*`). The narrator's fixed
  cast in `voices.json` already reads well as a "radio play" ensemble;
  making the narrator cast dynamic is a follow-on plan.

