#!/bin/bash
# Quick Reset & Restart Script
# Stops the local server, clears all sessions, and restarts with fresh state

echo ""
echo "=== RESETTING SYSTEM ==="
echo ""

# 1. Stop any local server (run_local.py / gunicorn)
echo "Stopping server..."
pkill -f "python.*run_local.py" 2>/dev/null || true
pkill -f "gunicorn api:app" 2>/dev/null || true
sleep 2

# 2. Clear all sessions
echo "Clearing all sessions..."
rm -rf sessions

# 3. Restart the local server
echo "Starting server..."
python run_local.py &

echo ""
echo "=== SYSTEM RESET COMPLETE ==="
echo "Server is starting in the background..."
echo "Check logs with: tail -f terminals/*.txt"
