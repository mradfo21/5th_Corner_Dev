#!/bin/bash
# Production start script using Gunicorn for API server

echo "=================================================="
echo "Starting SOMEWHERE Game - Production Mode"
echo "=================================================="

# Start Discord bot in background
echo "[1/2] Starting Discord bot (bot.py)..."
python bot.py &
BOT_PID=$!
echo "       Discord bot started (PID: $BOT_PID)"

# Give bot a moment to initialize
sleep 2

# Start API server with Gunicorn (production WSGI server)
echo "[2/2] Starting API server with Gunicorn..."
echo "       Binding to 0.0.0.0:$PORT"
echo "       Workers: 2 (adjust based on traffic)"
echo "       Timeout: 120 seconds"
echo "=================================================="

# Run Gunicorn with production settings
gunicorn api:app \
    --bind 0.0.0.0:$PORT \
    --workers 2 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    --log-level info

# If Gunicorn stops, clean up bot
echo "API server stopped. Cleaning up..."
kill $BOT_PID 2>/dev/null
wait $BOT_PID 2>/dev/null

