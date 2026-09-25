# Distribution MVP Plan

> **Status: in progress since 2026-09-25.** Source of truth for making ABYSS a
> downloadable, self-updating Windows app. Written from an audit of this working
> tree on 2026-09-25 (the Claude Docs "GOD — Ship Readiness Audit" and
> "GOD — Distribution MVP Plan" carry the original, from before the game was
> renamed ABYSS the same day). Steam is out of scope. Ticked boxes below are
> done and say where; the Progress log at the end is the running record.

## Goal

A stranger clicks Download on the site, runs a signed installer, plays the full
game **without needing an API key** (pay-as-you-go wallet; "use my own key" stays
as an advanced option in ACCOUNT), gets updates automatically, and their bug
reports reach us. Nothing is cut from the game: every feature, provider, the
World Editor, render mode, TALK voices, photo characters, Reactor, SCAN on-device,
mock mode, and the current safety settings all ship as they are.

## Decisions (defaults until Matt says otherwise)

| Decision | Default |
| --- | --- |
| Name / install package ID | `ABYSS` / `5thCorner.ABYSS` (both permanent once players install; chosen 2026-09-25, `app_identity.py`) |
| Player data root | `%APPDATA%\ABYSS\` (moved once from `%APPDATA%\SOMEWHERE\`, M2) |
| Markup / starter credit | 1.5× provider cost / $2, gated on email verification |
| Gateway | New Render service at `api.5th-corner.com`, Render Postgres for wallets + ledger |
| Builds hosted | New public repo `mradfo21/god-releases`; source repo made private |
| Versions | SemVer, starting `0.1.0` on the `beta` channel |
| Signing | Azure Artifact Signing (Matt applies; 1–20 business days) |

## Done means (the clean-machine test, in Windows Sandbox)

- [ ] /get → Download → `ABYSS-Setup.exe`; page shows version, size, SHA-256
- [ ] Installer runs with no "unknown publisher" SmartScreen warning
- [ ] Start Menu shortcut; first launch offers PLAY (starter credit) or "use my own key"
- [ ] 10 turns with SCAN, TALK, PHOTO, music and sound on the wallet; balance goes down by what the ledger says
- [ ] Top up $10 through Stripe (test mode) and keep playing
- [ ] Create a character from a photo; quit
- [ ] Publish the next version; relaunch offers the update on the start menu; after restart the run, character and balance are intact
- [ ] Bug button → Send → report arrives with the build version and no key in it
- [ ] A web page in Edge cannot reach the game's local API while it runs (`tools/drive_by.html`)
- [ ] Install folder made read-only; the game still saves
- [ ] Uninstall removes the program and leaves `%APPDATA%\ABYSS`

## Milestones

| Milestone | Goal | Finished when | Days |
| --- | --- | --- | --- |
| M0 Stabilize source | One pushed branch, pinned deps, one name | `main` builds from a fresh clone | 2 |
| M1 Harden the local app | No website can reach the game's server | Drive-by page refused; guard tests pass | 4 |
| M2 Saves out of the install folder | Updates never lose a save | Runs from a read-only folder | 3 |
| M3 Build pipeline | A tag produces a signed, tested build | Tag → signed artifacts, hands-off | 3 |
| M4 Installer and updates | Install once, update in place | N updates itself to N+1 and keeps the save | 3 |
| G  Gateway (pay-as-you-go) | Play without a key, on our keys, metered | A keyless install plays and is charged correctly | 10 |
| M5 Site and feedback | Site serves downloads; bugs reach us | /get serves Setup.exe; a bug report arrives | 3 |
| M6 Release candidate | Done means passes | Sandbox run passes → friends (beta) → public (stable) | 2 |

## M0 — Stabilize the source

- [ ] Commit and push the working tree; merge `cursor/fix-simulation-playback-coherence-9f80` into `main`. Tag each stale remote branch tip, then delete the 193 cursor branches
- [x] Scan the full git history for committed secrets — gitleaks 8.30.1 over every ref, 2026-09-25, plus GitHub's own secret-scanning alerts. Live-shaped: an OpenAI key, a Discord bot token and Replicate tokens in the Discord-era `config.json` (GitHub alerts 1–3, commit `8d28447b`, reachable on GitHub by sha though no local ref holds it) and an `ADMIN_TOKEN` in the deleted `QUICK_START_CROSS_ORIGIN.md`. Not secrets: ElevenLabs key *ids* as fixtures in `test_talk_voice.py`, a truncated Gemini key in `TEST_VEO_LOCALLY.md`. No Gemini, Stripe or ElevenLabs key is usable anywhere in history
- [ ] Rotate what the scan found (Matt: OpenAI, Discord, Replicate, the site's ADMIN_TOKEN if still set); then make the source repo private
- [ ] Each AI session gets its own git worktree/branch; releases come only from tags on `main`
- [x] `requirements.lock` with exact versions from today's working setup (Python 3.12.10, resolved in a clean venv; `import api`/`play`/every runtime module checked in it)
- [ ] Builds install only from it (M3's workflow; `build_exe.py` too). Settle opencv first: `opencv-python` 4.13 and `opencv-contrib-python` 5.0 both install `cv2` though requirements.txt says they must match
- [x] `app_identity.py`: `APP_NAME`, `APP_ID`, `DATA_DIR_NAME`, `VERSION`, and `appdata_root()`, which play.py's `.env` search, keys_store, billing and characters now all use (still the legacy `SOMEWHERE` folder until M2's migration switches it). `downloads.game_title()` reads `APP_NAME`. The spec's `name` moves with the spec rename in M3
- [x] Remove tracked scratch and ignore it — none was tracked; `.gitignore` now ignores `_claude_*`, loose `_*.png`, `the`, the runner's `.bat` launchers, and tracks every `test_*.py` (the allowlist had dropped eight suites)

## M1 — Harden the local app

Today `CORS(app)` (api.py:30) allows every origin and there is no token or Host
check, so the `remote_addr == 127.0.0.1` guards pass for any browser tab on the
machine. A page can find the port via `/api/health`, then `PUT /api/keys/custom`
with a blank key: `keys_store.set_custom` (keys_store.py:575) reuses the stored
OpenAI key and points the narrator at the attacker's address.

- [ ] Per-launch token: play.py mints `secrets.token_urlsafe(32)`, loads `/standalone?launch=<token>`; the server trades it for an HttpOnly `SameSite=Strict` cookie; a `before_request` guard rejects `/api/*` (websocket upgrades included) without the cookie or an `X-Launch-Token` header. Pass the token to the render child (render_jobs.py) via env. Armed like `enable_shutdown()`, so hosted mode is unaffected
- [ ] Host allowlist: only `127.0.0.1:<port>` / `localhost:<port>` (blocks DNS rebinding)
- [ ] Remove app-wide CORS; if needed, hosted-only and only for `https://www.5th-corner.com`
- [ ] `set_custom` never reuses a stored key when the address changes
- [ ] Validate names in all four `/api/archives/<name>` routes (api.py:2165, 2210, 2227, 2244); resolve and require the path to stay under `archives/` (Windows backslash traversal)
- [ ] `run_local.py --host` defaults to `127.0.0.1` (line 74)
- [ ] Frozen builds read `.env` only from AppData and beside the exe (drop the three-parents walk in `play._env_candidates`)
- [ ] Rotate `somewhere.log` (10 MB × 3); redact key-shaped strings (`AIza…`, `sk-…`) before writing
- [ ] `test_local_guard.py` (no cookie → 403, bad Host → 403, archive traversal → 400, custom address doesn't inherit the key) and `tools/drive_by.html`

## M2 — Saves out of the install folder

- [ ] Frozen play.py sets the existing overrides to `%APPDATA%\ABYSS\…` before importing `api`: `SESSIONS_DIR`, `SOMEWHERE_WORLDS_DIR`, `SOMEWHERE_EXPERIENCES_DIR`, `SOMEWHERE_CHARACTERS_DIR`, `SOMEWHERE_PROMPTS_PATH`, `SOMEWHERE_KEYS_PATH`, `SOMEWHERE_TUNABLES_PATH`, `REFERENCES_DIR`, `SOMEWHERE_ANALYTICS_DIR`
- [ ] `paths.py` for the rest: no override yet for `archives`, `logs`, `levels`, `lore`, `bugs`, `tapes`, `playtest_results`, `assets/music`; hard-coded `"sessions"` in api.py (12), engine.py (5), cost_tracker.py (ignores `SESSIONS_DIR`), scene_audio.py, coinop.py, usage_limits.py, billing.py
- [ ] Seed factory content at first run (move `stamp_factory` logic into startup); on update refresh factory files, never the player's
- [ ] One-time migration of `%APPDATA%\SOMEWHERE\` into the new root
- [ ] `tools/smoke_exe.py` runs the build with its folder read-only (`icacls /deny`) and fails on any write there

## M3 — Build pipeline

- [ ] `.github/workflows/release.yml` on `v*` tags, `windows-latest`: checkout → Python 3.12 → install from lock → mock test suite → `tools/build_exe.py --clean` → `tools/smoke_exe.py` → sign → Velopack pack → upload to the releases repo
- [ ] Stamp the tag into `_version.py`; show it in `/api/health`, window title, log header, bug reports
- [ ] Sign with Azure Artifact Signing via Velopack `--azureTrustedSignFile`; credentials as GitHub secrets; CI holds no game API keys
- [ ] Generate `THIRD-PARTY-NOTICES.txt` (pip-licenses) plus the GPLv3 notice and source link for imageio-ffmpeg's bundled ffmpeg (kept: render/video export use it)
- [ ] Real exe name and icon in the spec (`name`, `icon`); rename the spec `ABYSS.spec`
- [ ] Keep `build_exe.py` / `publish_build.py` working locally as a fallback

## M4 — Installer and updates (Velopack)

- [ ] `velopack.App().run()` at the very top of play.py
- [ ] `vpk pack --packId 5thCorner.ABYSS --packVersion <tag> --packDir dist/ABYSS --mainExe ABYSS.exe --icon <ico> --framework webview2`
- [ ] Channels: `beta` (friends) and `stable` (public link)
- [ ] Background update check at launch; "UPDATE READY — RESTART" on the start menu; never interrupts a run; "later" always allowed
- [ ] Portable zip stays as a secondary download ("no auto-update")

## G — Gateway (pay-as-you-go, no key needed)

Cost baseline from `sessions/_analytics/usage.db`: an automated 15-minute goal
run cost $4.63 at provider prices — images $2.75 (59%), ElevenLabs music (19
tracks) + SFX (30) $1.74 (38%), text $0.14 (3%).

- [ ] New service (separate from the site) exposing only gateway routes. Our keys live only there
- [ ] Identity: random wallet id per install (signed), email link/verify for recovery and the starter credit (`billing.link_email` exists)
- [ ] Client: `provider_bridge.py` gains a "5th Corner" backend: its `requests.Session.request` hook rewrites Gemini `generateContent` calls (17 raw-REST call sites) to the gateway with the wallet token. Route the 6 ElevenLabs calls and Krea the same way. Gemini Live (gemini_live_talk.py, gemini_live_vision.py) and Veo use the SDK → gateway mints short-lived tokens; Reactor already mints server-side
- [ ] Server: allowlist of models/sizes; balance check before each paid call (`billing.gate`), debit after using `cost_tracker` pricing (`charge_cost`); per-minute metering for TALK (`meter_start/stop` exists); rate limits; request size caps
- [ ] Postgres for wallets and the ledger (replaces `billing.json` on the 1 GB disk); more than one worker
- [ ] Stripe Checkout top-ups (existing `create_checkout`, Managed Payments) with the webhook on the gateway
- [ ] Shared caches across players: SFX library, music cues, factory-world look-book sheets and opening frame — generated once, reused
- [ ] ACCOUNT: balance, top-up, and "use my own key" (existing BYOK path) as the advanced option
- [ ] Spend caps and alerts on our Google and ElevenLabs accounts

## M5 — Site and feedback

- [ ] `SITE_MODE=downloads` on the site service: `/` → `/get`; `/standalone`, `/play`, `/lobby`, `/studio` and gameplay `/api/*` return 404; keep `/get*`, `/api/builds/latest`, `/api/health`, bug intake, `/admin` (token)
- [ ] Remove AI provider keys from the site service (they live only on the gateway)
- [ ] /get: prefer `*-Setup.exe` in `downloads._pick_asset`; system requirements (Windows 10/11 64-bit, ~1.5 GB); SHA-256
- [ ] `/privacy`, `/terms` (EULA, 18+), `/licenses`; first-launch 18+ confirmation (Gemini API terms)
- [ ] `POST /api/bug/intake`: 10 MB cap, per-IP rate limit, `MAX_CONTENT_LENGTH`; store + forward a summary to a private Discord webhook held server-side
- [ ] Bug button "Send to 5th Corner": preview what's included, redact key-shaped strings, opt-in every time; startup-failure splash offers "send crash log"

## M6 — Release candidate

- [ ] Done means, by hand in Windows Sandbox, then `tools/film_run.py --first-launch` against the installed build
- [ ] 5–10 friends on `beta`; ship one real update that week
- [ ] Promote the same build to `stable`; share the public /get link

## Waits until after the MVP

Steam (gateway + Steam Wallet + overlay test), hosted browser play and its
session-isolation fixes (the site serves downloads only until then), DPAPI key
encryption, macOS, size trimming (GPL ffmpeg → OpenCV's LGPL one, scipy),
hiding prompts, analytics beyond download counts.

## Needs Matt (not code)

Push the working tree (M0); Azure Artifact Signing application; Google paid
billing + budget cap on the gateway key; ElevenLabs commercial plan + OEM terms
check + usage cap; Stripe live mode; DNS for `api.5th-corner.com`; Render
Postgres + gateway service; private Discord webhook; legal entity name and
contact email for /privacy and /terms; Windows Sandbox enabled; a test budget
for real API calls.

## Progress log

- **2026-09-25** — M0 started in a local Claude Code session. The tree (31
  unpushed commits + ~110 files of uncommitted agent work) committed as
  `baf7376` (ignore rules), `c4d0a46` (the game work) and the M0 commit after
  it, then pushed. Identity chosen: ABYSS / `5thCorner.ABYSS` / `%APPDATA%\ABYSS`.
  `_claude_runner.py` (START_CLAUDE.bat) was still running; stopping it is Matt's.
