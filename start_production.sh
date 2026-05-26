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
GUNICORN_WORKERS="${GUNICORN_WORKERS:-2}"
GUNICORN_TIMEOUT="${GUNICORN_TIMEOUT:-120}"

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
echo "       Workers: ${GUNICORN_WORKERS}"
echo "       Timeout: ${GUNICORN_TIMEOUT}s"
echo "=================================================="

# Use --preload so the engine module loads once in the master and we don't
# pay the cold-start cost N times. --access-logfile - and --error-logfile -
# stream to Render's log pipeline.
exec gunicorn api:app \
    --bind "0.0.0.0:${PORT}" \
    --workers "${GUNICORN_WORKERS}" \
    --timeout "${GUNICORN_TIMEOUT}" \
    --graceful-timeout 30 \
    --keep-alive 5 \
    --access-logfile - \
    --error-logfile - \
    --log-level info
