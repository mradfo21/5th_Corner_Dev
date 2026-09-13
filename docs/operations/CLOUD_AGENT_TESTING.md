# 🤖 Local & Cloud-Agent Testing (play the game, don't just deploy it)

**Problem this solves:** the game is deployed on Render, so the only way to
"check" a change used to be pushing it and poking the live site — which
means an AI agent (or you) can't easily play-test a change before it ships,
and definitely can't `curl`/screenshot/click around the *production* site
as part of normal iteration. This doc is the "clone it, run it, play it,
tear it down" path that replaces that.

**The short version:** this repo already ships the exact production Flask
app (`api.app`) plus a deterministic, zero-network **mock backend**, so it
can run — and be played, either by a script or by an actual browser — fully
offline, with no API keys, no Discord, no Render.

## Starting the server

Start it yourself when you want to test; it is deliberately NOT auto-started
(see "Why the server isn't auto-started" below):

```bash
python3 run_local.py --mock --no-browser --port 5001
```

- **Play it in a browser:** http://localhost:5001/standalone (the same UI
  production serves — see `templates/standalone.html` /
  `static/js/standalone.js`)
- **Health check:** http://localhost:5001/api/health
- **Admin dashboard:** http://localhost:5001/admin
- **Mock mode** disables real LLM/image calls (`engine.LLM_ENABLED = False`,
  etc. — see `run_local.py`), so every turn resolves instantly with
  contextual fallback narrative/choices and no scene images. That's the
  known trade-off for zero-setup, zero-cost, deterministic testing; pass
  real API keys (see below) when you need to check actual generated
  narrative/images.
- **Stop it when you're done.** Nothing restarts it for you, and a server
  left up against a REAL backend keeps a key-holding process alive for no
  reason.

## Ways to test against it

Pick whichever fits what you're checking — all three exercise the real
HTTP contract the production UI uses, none of them touch Render:

### 1. Drive it with a real browser (best for UI/visual changes)

Use Cursor's `computerUse` tool/subagent, or any browser, and just... play
it, at `http://localhost:5001/standalone`. It's a real page with real
buttons, keyboard shortcuts (`1`-`4` pick a choice, `r` resets), and a
console — treat it like any other local web app you're testing manually.

### 2. Headless HTTP playtest (fast, best for backend/engine changes)

```bash
python3 autoplay.py --url http://127.0.0.1:5001 --turns 6 --strategy mixed
```

Drives `POST /api/reset`, `POST /api/choose`, `GET /api/feed`, exactly like
the browser does, and prints a pass/fail verdict (every turn resolves,
choices regenerate, narrative isn't a stale API-error fallback, etc.) plus
a JSON report (`autoplay_report.json`). Also works against a real backend
or even the live deploy — see `autoplay.py --help`.

### 3. Automated headless-browser test suite (best before opening a PR)

```bash
python3 -m unittest test_standalone_e2e -v
```

Spins up its own `run_local.py --mock` on a scratch port and drives it with
a real headless Chromium via Playwright (page load, reset, choice buttons,
keyboard shortcuts, custom actions, VHS toggle, etc.) — this is the closest
thing to a real regression suite for the standalone UI. Requires
`requirements-dev.txt` + `playwright install chromium`, both of which
`.cursor/install.sh` already does for you.

## Using a real backend instead of mock

Mock mode is the default when no key is set, purely so the loop above works
with zero setup. To exercise real Gemini/OpenAI/Anthropic narrative +
images instead:

```bash
export GEMINI_API_KEY=...        # or OPENAI_API_KEY / ANTHROPIC_API_KEY
python3 run_local.py --port 5001 --no-browser
```

On a Cloud Agent, set these as [secrets in the Cursor
Dashboard](https://cursor.com/dashboard) (Cloud Agents → Secrets) rather
than committing them anywhere; `run_local.py` auto-detects them and only
falls back to mock when none are present.

## How this is wired up for Cloud Agents

`.cursor/environment.json` + `.cursor/install.sh` prepare the environment:

- `install` (`.cursor/install.sh`): installs `requirements-dev.txt`
  (production deps + Playwright/pytest) and the Playwright Chromium
  browser with its OS dependencies.
- `ports`: forwards 5001 so the game is reachable once you start it.

### Why the server isn't auto-started

`environment.json` used to declare a `terminals` entry that launched the
mock server automatically. That is convenient in a throwaway cloud sandbox
and wrong everywhere else: Cursor treats those terminals as persistent, so
it also RESTARTS them. Opening the repo locally left a Flask process with
the whole engine loaded running forever, and killing it just brought it
back under a new pid — with no indication of where it was coming from.

Starting it yourself costs one command and makes the running process
something you chose. It matters most with real keys: an auto-restarting
server holds your API credentials in memory indefinitely, and any harness
pointed at it keeps spending.

See [`DESKTOP_APP_ROADMAP.md`](../plans/DESKTOP_APP_ROADMAP.md) for where this local
path is headed longer-term (a real double-click desktop build), and
[`AGENT_GUIDE.md`](../../AGENT_GUIDE.md) for how the engine itself works.
