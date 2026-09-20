# Deploying the hosted web build

Two things run this game: the **desktop app** a player double-clicks
(`QUICKSTART.md`, `SHIPPING.md`) and the **hosted web build** on Render. This
document is only about the second one.

There is no Discord bot. There never is again — `start_production.sh` says so
in its header, and `render.yaml` says so twice. If you find a document
describing a background worker running `bot.py`, it predates the rewrite.

## The source of truth is `render.yaml`

Do not duplicate it here. It is heavily commented and it is what Render reads.
One service:

| | |
|---|---|
| Type | `web` (not a worker — the API has to be reachable and health-checked) |
| Name | `somewhere-game` |
| Build | `pip install -r requirements.txt` |
| Start | `bash start_production.sh` |
| Health | `/api/health` |
| Auto-deploy | on |
| Disk | `game-data`, 1 GB, mounted at `sessions/` |

`start_production.sh` runs `gunicorn api:app` with **exactly one worker process
and multiple threads** (`--workers 1 --worker-class gthread`). That is not a
tuning preference and you must not "scale it up": the feed-based game loop keeps
its state in an in-process global with an in-process lock, so a second worker
process means a second, divergent game world. The reasoning is written out at
length in the script.

## The disk is load-bearing

Everything durable lives under `sessions/`: game state, images, tapes, the
`cost_tracker` SQLite ledger at `sessions/_analytics/usage.db`, billing at
`sessions/_billing/`, and Cast & Camera reference plates at
`sessions/_references/` (which is why `REFERENCES_DIR` is set to a path inside
the mount). Without the disk attached, all of it is wiped on every deploy and
"total spend" silently resets.

**A disk block in `render.yaml` does not guarantee an attached disk.** If the
service was created through Render's "New → Web Service" flow rather than "New →
Blueprint", the disk has to be added by hand once. Check whether it actually
attached using the live banner on the admin dashboard's Cost Analytics tab
(backed by `GET /api/admin/analytics/storage_health`) rather than assuming.
Full background: [RENDER_STORAGE_LIMITATION.md](RENDER_STORAGE_LIMITATION.md).

## Keys and feature flags

All of them are declared in `render.yaml` with comments explaining what breaks
without each one. The short version: `GEMINI_API_KEY` is the one that matters;
`KREA_API_KEY` backs the default image provider; `ELEVENLABS_API_KEY` is needed
for narrator audio but *not* for TALK, which works against a public agent id;
`REACTOR_API_KEY` is only for the realtime renderer. Everything degrades rather
than crashing — with no keys at all the service boots into mock mode.

`FEATURE_BILLING=1` is set **only** on the hosted service. The desktop app is
bring-your-own-key and calls `billing.mark_local_app()`; it must never inherit
that flag.

## Before you deploy

Test locally — you should never need to push to find out whether a change works.

```powershell
python tools/demo_check.py --boot      # the pre-demo gate
python run_local.py --mock --no-browser --port 5001
python -m unittest test_standalone_e2e test_providers test_render_mode
```

See [TESTING_USE_THIS.md](TESTING_USE_THIS.md) for how this project is actually
play-tested, and [CLOUD_AGENT_TESTING.md](CLOUD_AGENT_TESTING.md) for the
offline setup that makes it free.

Deploys are triggered by pushing the branch Render is watching. Check the build
log in the Render dashboard, then `/api/health`. To roll back, redeploy the last
good build from the Deploys tab.
