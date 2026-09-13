# Admin Cost & Analytics Dashboard — Implementation Plan

> **Status:** Planning only. Nothing in this doc is implemented yet. This is
> the design record for extending the existing `/admin` dashboard
> (`admin_dashboard.html`, served by `api.py`) with real cost tracking,
> per-session financial breakdowns, historical trends, and general
> business-intelligence views.

---

## 0. TL;DR

Today `/admin` (guarded by `ADMIN_TOKEN`, see `api.py:1338-1382`) already gives
us a **Sessions** tab, an **Archives** tab, and an **AI Models** switcher
(`api.py:1610-1660`), all backed by flat files under `sessions/` — there is
**no cost tracking of any kind** anywhere in the codebase today (confirmed:
zero hits for "cost"/"pricing"/"usage" across `engine.py`, `api.py`,
`ai_provider_manager.py`, and every provider util). We call 7+ paid services
(Gemini, OpenAI, Anthropic, Krea, fal.ai, Veo, ElevenLabs, Reactor) and have no
idea what any individual session, turn, or provider actually costs.

The plan has two halves that have to land in order:

1. **Instrumentation + storage** — a new `cost_tracker.py` module that every
   provider call site reports to, backed by a durable, queryable store
   (SQLite, not more flat JSON — see §3 for why).
2. **Surface it** — a new **Analytics** tab in the existing admin dashboard
   with KPI cards, time-series charts, a cost-sortable session table, a
   per-session cost drill-down, and a pricing editor, all reusing the
   dashboard's existing VHS/terminal visual language and auth model.

---

## 1. Current state (what exists today)

| Piece | Where | Notes |
|---|---|---|
| Admin auth | `_admin_token_ok()` — `api.py:1338` | Token via `?token=`, `X-Admin-Token` header, or `admin_token` cookie. Reuse as-is for every new endpoint. |
| Admin page shell | `serve_admin_dashboard()` — `api.py:1360` | Serves the single `admin_dashboard.html` file (2,809 lines today). |
| Session list/detail | `/api/sessions*` — `api.py:776-935` | Reads `sessions/<id>/meta.json`, `state.json`, `history.json` off disk via `engine._load_session_metadata` / `engine._load_history` / `engine.get_state`. No cost fields anywhere in the schema (`engine.py:506-527`). |
| Session archives | `/api/archives*` (see dashboard `fetchArchives()` at `admin_dashboard.html:2314`) | Post-mortem/death snapshots. |
| AI provider config | `ai_provider_manager.py`, `ai_config.json`, `/api/admin/ai_config`, `/api/admin/ai_switch` — `api.py:1610-1660` | Lets us hot-swap text/image provider+model at runtime. **This is the exact pattern we'll copy for a live-editable `pricing.json`.** |
| Provider call sites | `engine._ask` / `_ask_gemini` / `_ask_openai` / `_ask_claude` (`engine.py:1568-1900ish`); `engine._gen_image` (`engine.py:3563`) dispatching to `gemini_image_utils.py`, `krea_image_utils.py`, `fal_image_utils.py`, `veo_video_utils.py`; `scene_audio.py` / `voice_design.py` for ElevenLabs TTS/Voice Design; `gemini_live_talk.py` / `gemini_live_vision.py` for Gemini Live (streaming realtime audio/vision) | **None of these record tokens, unit counts, latency, or cost today.** Confirmed the raw Gemini REST response (`engine.py:1675`, `response_data`) already contains `usageMetadata.{promptTokenCount,candidatesTokenCount,totalTokenCount}` — it's just never read. |
| Storage | Flat files under `sessions/<id>/` (`engine._get_session_root`, `engine.py:418`) | **Ephemeral** on Render unless a persistent disk is attached — see `RENDER_STORAGE_LIMITATION.md`. Any cost ledger we bolt onto this same disk inherits the same durability risk; see §3. |

**Services in play today** (from `render.yaml` env vars + `ai_config.json` +
`requirements.txt`): Gemini (text + image, "Nano Banana"), OpenAI (text +
image), Anthropic (text), Krea (image, async job API), fal.ai (image,
sync), Veo (video → last-frame extraction), ElevenLabs (TALK conversational
agent minutes + NARRATOR TTS + Voice Design), Reactor (realtime world-model
video renderer). Each has a **different billing unit** (tokens, per-image,
per-second video, per-character or per-minute audio), so the cost model has
to be per-provider, not one-size-fits-all.

---

## 2. Goals

1. **Every paid API call** (text, image, video, voice) is logged with enough
   detail to compute its dollar cost, without materially slowing down the
   game loop (fire-and-forget / best-effort logging — a tracking failure must
   never break gameplay).
2. **Per-session cost rollups**: "this playthrough cost $X, broken down by
   text/image/voice/video and by provider."
3. **Fleet-wide analytics**: spend over time (hour/day/week), spend by
   provider/model, sessions per day, avg cost per session, error rates,
   projected monthly spend, and enough raw data to answer ad-hoc business
   questions later without re-instrumenting.
4. **A pricing table that's editable at runtime** (mirroring
   `ai_provider_manager`/`ai_config.json`), because provider prices change and
   we don't want a redeploy every time Gemini or ElevenLabs repriced.
5. **A dashboard that's actually pleasant to use**: charts, not just tables;
   sortable/filterable session list; drill-down modal; date-range picker;
   consistent with the existing VHS/"R.A.S.T.E.R. Operations" visual theme so
   it doesn't feel bolted on.
6. Keep the blast radius on gameplay code **small and additive** — no
   provider call site should need more than a few wrapped lines.

## Non-goals (for v1)

- Real-time (sub-second) cost streaming to players — this is an *admin* tool.
- Perfect penny-accurate reconciliation with provider invoices on day one
  (see §8 — we ship an estimator first, add reconciliation later).
- Multi-tenant billing / customer-facing invoicing — this is a single-operator
  cost dashboard.

---

## 3. Storage: SQLite on the (already-needed) persistent disk

Two constraints collide here:

- We want to **query/aggregate** (sum by provider, group by day, sort
  sessions by cost) — flat per-session JSON files (today's pattern) make that
  expensive and awkward at any real event volume.
- The filesystem is **ephemeral** on Render unless a persistent disk is
  mounted (`RENDER_STORAGE_LIMITATION.md` already flags this as a known gap
  for `sessions/`).

**Decision: SQLite file at `sessions/_analytics/usage.db`** (same disk the
project already recommends mounting a persistent volume on), accessed via
Python's stdlib `sqlite3` (zero new dependency). This gets us real `GROUP BY`/
`SUM`/date-range queries for the dashboard without standing up Postgres, and
it's consistent with this project's "flat files, no external DB" philosophy.

- **This makes the persistent-disk upgrade in `RENDER_STORAGE_LIMITATION.md`
  a hard prerequisite for durable cost history**, not just nice-to-have for
  game saves. Call this out explicitly in the PR description / to the
  operator: without the persistent disk, cost history resets on every
  deploy, same as sessions do today.
- Add a lightweight **nightly export job** (reuse the archive-on-delete
  pattern already in `engine.delete_session`) that dumps
  `usage.db` → a timestamped JSON/CSV snapshot under `sessions/_analytics/backups/`
  and, if configured, uploads it somewhere durable (S3/GCS bucket via a new
  optional `ANALYTICS_BACKUP_*` env var). This is a cheap insurance policy
  against disk loss without requiring a hosted DB.
- If usage volume ever outgrows SQLite (unlikely for a single Discord-bot
  game), the schema below translates directly to Postgres — this isn't a
  dead end, just the smallest thing that works today.

### Schema

```sql
CREATE TABLE usage_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,              -- ISO8601 UTC
    session_id    TEXT NOT NULL,
    turn_count    INTEGER,                    -- state.turn_count at call time, for per-turn drill-down
    service_type  TEXT NOT NULL,              -- 'text' | 'image' | 'video' | 'voice' | 'realtime'
    provider      TEXT NOT NULL,               -- 'gemini' | 'openai' | 'anthropic' | 'krea' | 'fal' | 'veo' | 'elevenlabs' | 'reactor'
    model         TEXT NOT NULL,
    operation     TEXT,                        -- 'ask' | 'gen_image' | 'img2img' | 'tts' | 'voice_design' | 'live_stream' | ...
    input_units   REAL,                        -- prompt tokens / n/a
    output_units  REAL,                        -- completion tokens / image count / seconds / characters
    unit_type     TEXT,                        -- 'tokens' | 'images' | 'seconds' | 'characters' | 'minutes'
    cost_usd      REAL NOT NULL,
    latency_ms    INTEGER,
    success       INTEGER NOT NULL DEFAULT 1,
    error_message TEXT,
    discord_guild_id TEXT,
    discord_channel_id TEXT,
    meta_json     TEXT                         -- small free-form JSON blob (model params, hd_mode, etc.)
);

CREATE INDEX idx_usage_session ON usage_events(session_id);
CREATE INDEX idx_usage_ts ON usage_events(ts);
CREATE INDEX idx_usage_provider ON usage_events(provider, model);

CREATE TABLE session_cost_rollup (
    session_id     TEXT PRIMARY KEY,
    total_cost_usd REAL NOT NULL DEFAULT 0,
    cost_by_service_json TEXT,   -- {"text": 0.02, "image": 1.10, "voice": 0.05}
    cost_by_provider_json TEXT,
    event_count    INTEGER NOT NULL DEFAULT 0,
    last_event_ts  TEXT
);
```

`session_cost_rollup` is a denormalized cache updated on every insert
(single-row upsert, cheap) so the session list endpoint doesn't have to
`GROUP BY` the whole `usage_events` table on every page load — it's a fast
read path for the dashboard's main table, while `usage_events` stays the
source of truth for drill-downs and time-series charts.

---

## 4. `cost_tracker.py` — the instrumentation layer

New module, same shape as `ai_provider_manager.py` (small, dependency-free,
importable everywhere):

```python
# cost_tracker.py (sketch)
import sqlite3, threading, time, json
from contextlib import contextmanager

_DB_PATH = "sessions/_analytics/usage.db"
_lock = threading.Lock()

def record_usage(session_id, service_type, provider, model, *, operation=None,
                  input_units=None, output_units=None, unit_type=None,
                  latency_ms=None, success=True, error_message=None,
                  turn_count=None, discord_guild_id=None, discord_channel_id=None,
                  meta=None):
    """Best-effort insert. Never raises — a tracking bug must not break gameplay."""
    try:
        cost_usd = pricing.estimate_cost(provider, model, unit_type, input_units, output_units)
        ...  # INSERT into usage_events, upsert session_cost_rollup
    except Exception as e:
        print(f"[COST TRACKER] failed to record usage (non-fatal): {e}")

@contextmanager
def track(session_id, service_type, provider, model, **kwargs):
    """with cost_tracker.track(...) as t: ...; t.units(input_units=.., output_units=..)"""
    ...
```

Key design choices:

- **Never throws, never blocks the game loop.** Wrap every DB write in
  try/except; log and move on. A `sqlite3` write is sub-millisecond for this
  volume, but we still don't want a locked DB file to freeze a Discord
  interaction.
- **Fire-and-forget is fine synchronously** at this scale (a personal
  Discord-bot game, not high QPS), so no need for a background queue/worker
  in v1. If write contention ever becomes visible in logs, upgrade to a
  single background thread with a `queue.Queue` — the `record_usage()` call
  signature doesn't change either way.
- **`pricing.py` / `pricing.json`** sits next to `cost_tracker.py` and is the
  live-editable rate table — see §5.

### Wiring into existing call sites

| Call site | File:line | What changes |
|---|---|---|
| `_ask_gemini` | `engine.py:1600` | After `response_data = response.json()`, read `response_data.get("usageMetadata", {})` → `promptTokenCount`/`candidatesTokenCount` (confirmed present in the raw REST response today, just unused). Call `cost_tracker.record_usage(session_id, "text", "gemini", model_name, input_units=prompt_tokens, output_units=completion_tokens, unit_type="tokens", ...)`. |
| `_ask_openai` | `engine.py:1709` | OpenAI SDK responses expose `.usage.prompt_tokens` / `.usage.completion_tokens` — same pattern. |
| `_ask_claude` (Anthropic) | `engine.py` (search `_ask_claude`) | Anthropic SDK responses expose `.usage.input_tokens` / `.usage.output_tokens`. |
| `_gen_image` → Gemini path | `gemini_image_utils.py:193` (`generate_with_gemini`) | Gemini image gen doesn't return granular token usage the way text does; treat as **flat per-image cost** keyed by model (Gemini publishes per-image pricing for "Nano Banana" tiers). `unit_type="images"`, `output_units=1`. |
| `_gen_image` → Krea path | `krea_image_utils.py` | Async job API — record on job completion (where the PNG is already written), `unit_type="images"`, cost keyed by `krea-2/medium` vs `krea-2/large` (different flat prices, matches the two presets already in `ai_config.json`). |
| `_gen_image` → fal path | `fal_image_utils.py` | Sync call — record right after the HTTP response, `unit_type="images"`. |
| `_gen_image` → Veo path | `veo_video_utils.py` | Veo bills per-second of generated video (it generates an 8s clip today, per `ai_config.json`'s `veo` preset description) — `unit_type="seconds"`, `output_units=8` (or read the actual clip duration if available). |
| ElevenLabs TTS (NARRATOR) | `scene_audio.py` | ElevenLabs TTS bills per character of input text — `unit_type="characters"`, `input_units=len(text)`. |
| ElevenLabs Voice Design | `voice_design.py` | Per-design-call flat cost (ElevenLabs bills Voice Design as a fixed credit cost) — `unit_type="calls"`, `output_units=1`. Session-scoped so it composes with the existing per-session design budget (`ELEVENLABS_DESIGN_BUDGET_PER_SESSION`, already documented in `render.yaml`). |
| ElevenLabs TALK agent | `gemini_live_talk.py` handshake / wherever the conversational-agent websocket session opens+closes | Billed per **minute of conversation**, not per message — track connection open/close timestamps, record on close with `unit_type="seconds"`, `output_units=elapsed`. This is an estimate; see reconciliation in §8. |
| Gemini Live Vision (SCAN) | `gemini_live_vision.py` | Same idea — Gemini Live bills per audio/video-minute of the streaming session; track session duration as the unit. |
| Reactor renderer | wherever `REACTOR_API_KEY` short-lived tokens are minted (per `render.yaml` comments) | Track render-session duration in seconds if Reactor bills per-second; if pricing is unknown/opaque, log the event with `cost_usd=NULL`/flagged "unpriced" so it still shows up in the dashboard as a usage count even before we know the $ rate — better to see "1,200 Reactor render-minutes, cost unknown" than to silently drop it. |

The common thread: **every call site gets 3-6 added lines, not a rewrite.**
None of the actual provider-calling logic changes; we're only reading fields
that are frequently already sitting in the response and weren't being looked
at, and appending a tracker call right before each `return`/success path
(and once more on the error paths, with `success=False`).

---

## 5. `pricing.json` — live-editable rate table

Same UX pattern as `ai_config.json` / `ai_provider_manager.py`
(`api.py:1610-1660`, `admin_dashboard.html` AI Models tab): a JSON file with a
small manager module, hot-reloadable, editable from the admin UI without a
redeploy.

```jsonc
{
  "last_updated": "2026-07-20T00:00:00Z",
  "rates": {
    "gemini:gemini-3.1-flash-lite":        { "unit_type": "tokens", "input_per_1k": 0.0, "output_per_1k": 0.0 },
    "gemini:gemini-3.1-flash-image":       { "unit_type": "images", "per_unit": 0.0 },
    "openai:gpt-4o-mini":                  { "unit_type": "tokens", "input_per_1k": 0.0, "output_per_1k": 0.0 },
    "openai:gpt-image-1":                  { "unit_type": "images", "per_unit": 0.0 },
    "anthropic:claude-opus-4-5":           { "unit_type": "tokens", "input_per_1k": 0.0, "output_per_1k": 0.0 },
    "krea:krea-2/medium":                  { "unit_type": "images", "per_unit": 0.0 },
    "krea:krea-2/large":                   { "unit_type": "images", "per_unit": 0.0 },
    "fal:fal-ai/fast-lightning-sdxl":      { "unit_type": "images", "per_unit": 0.0 },
    "veo:veo-3.1-generate-preview":        { "unit_type": "seconds", "per_unit": 0.0 },
    "elevenlabs:tts":                      { "unit_type": "characters", "per_1k": 0.0 },
    "elevenlabs:voice_design":             { "unit_type": "calls", "per_unit": 0.0 },
    "elevenlabs:talk_agent":               { "unit_type": "seconds", "per_unit": 0.0 },
    "reactor:default":                     { "unit_type": "seconds", "per_unit": null }
  }
}
```

- `pricing.estimate_cost(provider, model, unit_type, input_units, output_units)`
  looks up `f"{provider}:{model}"`, falls back to `f"{provider}:default"`,
  and returns `None` (shown as "unpriced" in the UI, never silently `$0`) if
  no rate is configured — so gaps are visible instead of hidden.
- New endpoints mirroring the AI config ones:
  - `GET /api/admin/pricing` → current rate table.
  - `PUT /api/admin/pricing` → update one or more rates (admin-token guarded,
    same as `admin_ai_switch`).
- Rates start at real published provider prices at build time (Gemini, OpenAI,
  Anthropic all publish per-1K-token pricing; Krea/fal/ElevenLabs publish
  per-image/per-character pricing) and are meant to be tweaked in the UI as
  providers change pricing — no redeploy required, exactly like model
  switching today.

---

## 6. Backend API additions

All under `/api/admin/analytics/*`, all guarded by the existing
`_admin_token_ok()` (`api.py:1338`), added to `api.py` near the other
`/api/admin/*` routes (`api.py:1335` onward):

| Route | Purpose |
|---|---|
| `GET /api/admin/analytics/summary?range=24h\|7d\|30d\|all` | KPI numbers: total spend, spend by service type, spend by provider, session count, active sessions (state loaded within range), error count/rate, avg cost/session, projected monthly spend (trailing daily average × 30). |
| `GET /api/admin/analytics/timeseries?range=...&granularity=hour\|day` | Buckets of `{ts_bucket, cost_usd, event_count}` for line/bar charts — cost over time, split by service type for a stacked chart. |
| `GET /api/admin/analytics/sessions?sort=cost_desc\|recent\|turns&limit=&offset=` | Session list joined with `session_cost_rollup` — replaces/augments today's plain `/api/sessions` for the analytics view without breaking the existing Sessions tab. |
| `GET /api/admin/analytics/sessions/<id>` | Full per-session drill-down: ledger of every `usage_events` row for that session, per-turn cost (joined against `turn_count`), totals by service/provider, timeline data for a per-session chart. |
| `GET /api/admin/analytics/providers` | Cost & call-count grouped by `provider`/`model`, for a "which provider is actually expensive" table + the pricing editor's context. |
| `GET /api/admin/analytics/errors?range=...` | Recent failed calls (`success=0`) with `error_message`, for a reliability panel. |
| `GET /api/admin/analytics/export.csv?range=...` | Streamed CSV of raw `usage_events` for the selected range — for pulling into a spreadsheet for "other business inquiries" ad hoc analysis. |
| `GET /api/admin/pricing` / `PUT /api/admin/pricing` | Rate table read/update (see §5). |

All read endpoints are simple parameterized SQL against `usage.db` — no ORM
needed at this scale, consistent with the rest of the codebase's
dependency-light style.

---

## 7. Frontend: new "Analytics" tab in `admin_dashboard.html`

`admin_dashboard.html` is already a single-file dashboard with tab-like
sections (Sessions / Archives / AI Models, per the `fetch()` inventory at
lines 1277-2792). We add a fourth tab, **Analytics**, rather than a new page,
so auth/theming/nav are all free.

- **Charting:** add Chart.js via CDN `<script>` tag (`chart.js@4` UMD build) —
  no build step, consistent with the existing CDN-only approach (Google
  Fonts is already loaded the same way). No new npm/build tooling anywhere in
  this repo, so this stays true to the project's zero-build-step philosophy.
- **File split:** at ~2,800 lines already, don't grow
  `admin_dashboard.html`'s inline `<script>` further. Move the new Analytics
  logic into `static/js/admin_analytics.js` (the `static/js/` directory
  already exists and holds `standalone.js`) and `<script src="...">` it in.
  Keep the existing three tabs' inline JS untouched to minimize regression
  risk on this pass.
- **Visual language:** reuse the existing CSS variables (`--color-blood-red`,
  `--color-dark-gray`, `--font-ocr` "Share Tech Mono", the scanline overlay,
  `.stat-card` styling already defined at `admin_dashboard.html:103-120`) so
  Analytics doesn't look like a foreign widget bolted onto the VHS/horror
  theme. Chart.js is themeable enough to match (dark canvas background, red
  accent line, monospace tick labels).

### Layout

1. **Top bar:** date-range selector (24h / 7d / 30d / All / custom) +
   granularity toggle (hour/day) + a manual refresh button, following the
   existing `refreshData()` polling pattern (`admin_dashboard.html:1277`).
2. **KPI card row** (same `.stat-card` grid already used for session stats,
   `admin_dashboard.html:1103-1125`): Total Spend (range), Spend Today, Avg
   Cost / Session, Active Sessions, Error Rate, Projected Monthly Spend.
3. **Charts row:**
   - Line chart: cost over time (stacked by service type: text/image/
     video/voice).
   - Donut chart: spend by provider (Gemini vs OpenAI vs Krea vs fal vs
     ElevenLabs vs Veo vs Reactor vs Anthropic).
   - Bar chart: sessions started per day + avg turns per session, for the
     "how much are people playing" business question.
4. **Sessions-by-cost table:** sortable table (cost desc by default) —
   session id, name, started, turns, cost total, cost-by-service sparkline,
   status (alive/dead/abandoned) — click a row to open a drill-down modal
   (reuse the existing session-modal pattern at `admin_dashboard.html:1404`
   `showSessionModal`, extended with a "Cost" section: per-turn cost timeline
   + a small pie of that session's provider mix).
5. **Provider/pricing panel:** table of every `provider:model` combo with its
   current rate, editable inline (PUT to `/api/admin/pricing`), plus
   total-spend and call-count columns — mirrors the AI Models switcher UX
   (`admin_dashboard.html` "AI MODELS" tab, `refreshAiModels()` /
   `switchPreset()` at lines 2158-2313) so it feels native to this dashboard,
   not a new paradigm.
6. **Errors panel:** small recent-failures table (provider, model, error,
   timestamp) for reliability visibility — this doubles as an "is a provider
   currently broken" panel during live play.
7. **Export button:** hits `/api/admin/analytics/export.csv` for the current
   range/filename download.

---

## 8. Accuracy & reconciliation (why this is an *estimate* engine, and how we tighten it)

Several providers don't return billable-unit data cleanly:

- **Gemini/OpenAI/Anthropic text** → token counts are in the response, so
  these are close to exact from day one.
- **Image providers (Gemini image, Krea, fal, Veo)** → billed per-image or
  per-second at a **flat published rate**, not usage-metered in the response;
  our numbers are only as accurate as the rate we hardcode in `pricing.json`
  and how promptly we update it when a provider reprices.
- **ElevenLabs TALK agent minutes, Gemini Live, Reactor** → these are
  streaming/session-based services billed by connection duration on the
  provider's side; our tracked "seconds" are our own client-side timer, which
  can drift from what the provider actually bills (e.g. minimum billing
  increments, silence trimming).

**Phase 2 add-on (not v1, but designed for):** a scheduled reconciliation job
(`reconcile_usage.py`, run via cron/Render's scheduled jobs or a simple
`asyncio` loop in the bot process) that periodically pulls **actual billed
usage** from provider usage/billing APIs where available (OpenAI usage API,
Anthropic usage API, ElevenLabs usage API all expose this) and stores it
alongside our estimate in a `provider_actuals` table, so the dashboard can
show "estimated $X vs provider-reported $Y" and we can tune `pricing.json`
rates until the gap closes. This turns the estimator into a
self-correcting system over time instead of a one-shot guess.

---

## 9. Other business-intelligence views worth adding while we're in here

Since `usage_events` + the existing session metadata/history give us rich
raw data, cheap additions once the pipeline exists:

- **Survival analytics:** avg turns-to-death, death rate, most common cause
  of death (from `history.json` outcomes) — "is the game too hard/easy"
  business question, joins cleanly against `usage_events.turn_count`.
- **Provider efficiency:** cost-per-turn and latency-per-turn by provider/
  model, to answer "should we default to Krea Medium or fal for cost
  reasons" quantitatively instead of by feel.
- **Discord engagement:** sessions per guild/channel (we already log
  `discord_guild_id`/`discord_channel_id` in `usage_events`), DAU/WAU proxy
  via distinct `session_id`s with activity per day.
- **Budget alerts:** optional daily/monthly spend threshold in
  `pricing.json` (`"daily_budget_usd"`) that, when crossed, posts a warning
  to a Discord webhook or shows a red banner in the Analytics tab — cheap
  guardrail against a runaway provider bill.

None of these need new instrumentation beyond §4 — they're just additional
read-side queries/panels layered on the same ledger.

---

## 10. Phased implementation plan

| Phase | Scope | Exit criteria |
|---|---|---|
| **1. Ledger foundation** | `cost_tracker.py`, `usage.db` schema + migration bootstrap, `pricing.py`/`pricing.json` with real published rates. No UI yet. | Running the game locally produces rows in `usage_events` for a full turn (text + image); verified by inspecting the DB directly. |
| **2. Wire every call site** | Instrument all paths in the table in §4 (text ×3 providers, image ×4 providers, ElevenLabs TTS/Voice Design/TALK, Gemini Live, Reactor best-effort). | Every service type shows nonzero events after a manual playtest exercising SCAN/TALK/image/video. |
| **3. Backend analytics API** | All routes in §6, unit-tested against a seeded SQLite fixture. | `curl` against `/api/admin/analytics/summary` etc. returns correct aggregates for a known fixture. |
| **4. Analytics tab v1** | KPI cards + cost-over-time chart + sessions-by-cost table, `static/js/admin_analytics.js`. | Dashboard shows real numbers for a locally-played session. |
| **5. Drill-down + pricing editor + export** | Per-session modal cost section, provider donut/table, `PUT /api/admin/pricing` UI, CSV export. | Can answer "what did session X cost and why" and edit a rate live without redeploy. |
| **6. Reconciliation + BI extras** | `provider_actuals` table + reconciliation job (§8), survival/engagement panels (§9), budget alert (optional). | Estimated vs actual spend shown side-by-side for at least one provider with a usage API. |
| **7. Hardening** | Backup/export job for `usage.db` (§3), `test_cost_tracker.py` covering rate math for every unit type, update `RENDER_STORAGE_LIMITATION.md`/`DEPLOYMENT.md` to flag the persistent-disk requirement for durable analytics. | Tests green; docs updated; a simulated disk wipe is recoverable from the backup export. |

Phases 1-4 are the minimum useful slice (you can see real cost numbers in the
dashboard). Phases 5-7 are quality-of-life and durability hardening and can
ship incrementally after that.

---

## 11. Testing strategy

Following this repo's existing convention of top-level `test_*.py` files
(e.g. `test_providers.py`, `test_before_deploy.py`):

- `test_cost_tracker.py` — unit tests for `pricing.estimate_cost()` across
  every `unit_type` (tokens/images/seconds/characters/calls), including the
  "unpriced → None" fallback path.
- `test_analytics_api.py` — Flask test-client tests for every
  `/api/admin/analytics/*` route against a seeded temp SQLite DB, including
  the `_admin_token_ok()` auth guard (401 without token, 200 with it — mirror
  the existing pattern likely already used to test `/api/admin/*` routes).
- Extend `test_before_deploy.py`'s smoke-test sweep to hit
  `/api/admin/analytics/summary` so a broken analytics route blocks deploy
  the same way other regressions do today.
- Manual QA checklist: play one full session end-to-end (SCAN + TALK +
  image turns + a death), confirm the session's cost drill-down sums to a
  plausible number and every service type appears at least once.

---

## 12. Risks / open questions for the operator

1. **Persistent disk is now a hard requirement** for durable cost history,
   not just for game saves — worth confirming the Render plan has (or will
   get) the disk from `RENDER_STORAGE_LIMITATION.md` before this ships to
   prod, or the cost ledger resets on every deploy just like sessions do
   today.
2. **Initial `pricing.json` rates need to be filled in with real numbers**
   from each provider's published pricing page at implementation time (rates
   above are placeholders/zeros) — this is a one-time research task per
   provider, then it's editable forever after.
2b. Rates will drift; without the Phase 6 reconciliation job, accuracy is
    only as good as how often we remember to update `pricing.json`.
3. **ElevenLabs TALK / Gemini Live / Reactor cost estimates are
   duration-based approximations**, not billed-unit-exact, until Phase 6
   reconciliation lands — set expectations accordingly in the UI (e.g. an
   "≈" prefix or "estimated" badge on those rows).
4. **Volume assumption**: this plan assumes single-instance, moderate-QPS
   usage (a Discord game, not a high-traffic SaaS). If that changes
   materially, revisit SQLite → Postgres and synchronous writes → a queued
   writer, per the escape hatches noted in §3/§4.
