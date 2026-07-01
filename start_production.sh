#!/bin/bash
# Production start script for Render.
#
# Layout: gunicorn (Flask API) runs in the foreground; the Discord bot runs
# in a supervised loop in the background. If the bot crashes due to a real
# config problem (bad token, etc.) it exits non-zero quickly and we stop
# retrying so the operator sees a clear, non-flapping log. If it crashes
# transiently (Discord 5xx, network blip) we restart with exponential
# backoff up to a cap.

set -uo pipefail

echo "=================================================="
echo "Starting SOMEWHERE Game - Production Mode"
echo "=================================================="

PORT="${PORT:-10000}"
GUNICORN_TIMEOUT="${GUNICORN_TIMEOUT:-120}"
GUNICORN_THREADS="${GUNICORN_THREADS:-4}"

# ------------------------------------------------------------------
# Background: supervise the Discord bot
# ------------------------------------------------------------------
supervise_bot() {
    local attempt=0
    local max_backoff=60
    while true; do
        attempt=$((attempt + 1))
        echo "[BOT-SUPERVISOR] Starting Discord bot (attempt #${attempt})..."
        python bot.py
        local rc=$?
        echo "[BOT-SUPERVISOR] bot.py exited with code ${rc}."

        # Exit codes 1 and 2 come from bot.py's main block when the
        # configuration is fundamentally wrong (missing/invalid token).
        # Restarting won't help — the operator has to fix the env var.
        if [ "${rc}" -eq 1 ] || [ "${rc}" -eq 2 ]; then
            echo "[BOT-SUPERVISOR] Fatal config error detected (exit ${rc}). " \
                "Stopping bot supervisor — fix DISCORD_TOKEN / config and redeploy."
            return "${rc}"
        fi

        # Cap exponential backoff: 5s, 10s, 20s, 40s, 60s, 60s, ...
        local sleep_for=$((5 * (2 ** (attempt - 1))))
        if [ "${sleep_for}" -gt "${max_backoff}" ]; then
            sleep_for="${max_backoff}"
        fi
        echo "[BOT-SUPERVISOR] Restarting in ${sleep_for}s..."
        sleep "${sleep_for}"
    done
}

echo "[1/2] Launching Discord bot supervisor in background..."
supervise_bot &
BOT_SUP_PID=$!
echo "       Bot supervisor started (PID: ${BOT_SUP_PID})"

# Forward SIGTERM/SIGINT to the supervisor so a graceful Render shutdown
# stops the bot before the API.
trap 'echo "[SHUTDOWN] Forwarding signal to bot supervisor..."; kill ${BOT_SUP_PID} 2>/dev/null; wait ${BOT_SUP_PID} 2>/dev/null; exit 0' TERM INT

# Give the bot a couple of seconds before starting gunicorn so the first
# log lines are interleaved in a useful order.
sleep 2

# ------------------------------------------------------------------
# Foreground: gunicorn for the API
# ------------------------------------------------------------------
echo "[2/2] Starting API server with Gunicorn..."
echo "       Bind: 0.0.0.0:${PORT}"
echo "       Workers: 1 (gthread, ${GUNICORN_THREADS} threads)"
echo "       Timeout: ${GUNICORN_TIMEOUT}s"
echo "=================================================="

# IMPORTANT: exactly 1 worker PROCESS, with multiple THREADS for concurrency.
#
# engine.py's standalone/feed-based game loop (api_reset/api_feed/api_choose/
# api_regenerate_choices, used by /standalone) keeps its game state in a
# plain in-process Python global (`engine.state`) plus an in-process feed
# item id counter and lock (`_next_feed_item_id`, `WORLD_STATE_LOCK`). None
# of that is shared across separate OS processes. With gunicorn's default
# sync worker model and >1 *worker* (each a separate process with its own
# memory), requests would be load-balanced across processes with no shared
# state -> players would see the feed intermittently reset/go stale/get
# duplicate ids depending on which process handled which request.
#
# Using --workers 1 with --worker-class gthread --threads N keeps everything
# in ONE process (so the in-memory state and its lock stay consistent and
# correct, exactly like running `python run_local.py` does locally) while
# still serving multiple concurrent requests via threads (admin dashboard,
# archives, session API, and the standalone UI's polling all run fine
# concurrently). The session-based API (/api/sessions/*, /api/game/*) is
# unaffected either way - it already reloads/saves state from disk on every
# call instead of relying on a persistent in-memory object.
#
# --access-logfile - and --error-logfile - stream to Render's log pipeline.
# (Deliberately NOT using --preload: with only 1 worker process there's no
# cold-start-cost-times-N to save, and it would mean engine.py's module-level
# init - including the legacy state load and the locks above - runs in the
# master before fork rather than in the worker itself, which is an
# unnecessary behavior change for no benefit here.)
exec gunicorn api:app \
    --bind "0.0.0.0:${PORT}" \
    --workers 1 \
    --worker-class gthread \
    --threads "${GUNICORN_THREADS}" \
    --timeout "${GUNICORN_TIMEOUT}" \
    --graceful-timeout 30 \
    --keep-alive 5 \
    --access-logfile - \
    --error-logfile - \
    --log-level info
