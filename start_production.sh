#!/bin/bash
# Production start script for Render.
#
# Serves the Flask API (which also serves the standalone web game) with
# gunicorn in the foreground. Discord support has been removed — this service
# is now the web app only.

set -uo pipefail

echo "=================================================="
echo "Starting SOMEWHERE Game - Production Mode"
echo "=================================================="

PORT="${PORT:-10000}"
GUNICORN_TIMEOUT="${GUNICORN_TIMEOUT:-120}"
GUNICORN_THREADS="${GUNICORN_THREADS:-4}"

# ------------------------------------------------------------------
# Foreground: gunicorn for the API
# ------------------------------------------------------------------
echo "Starting API server with Gunicorn..."
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
