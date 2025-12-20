#!/bin/bash
# Start both Discord bot and API server together
# This allows one Render service to run both processes

echo "=================================================="
echo "Starting SOMEWHERE Game - Combined Service"
echo "=================================================="

# Start Discord bot in background
echo "[1/2] Starting Discord bot (bot.py)..."
python bot.py &
BOT_PID=$!
echo "       Discord bot started (PID: $BOT_PID)"

# Give bot a moment to initialize
sleep 2

# Start API server in foreground
echo "[2/2] Starting API server (api.py)..."
echo "       API will be available at port $PORT"
echo "       Dashboard: /admin"
echo "=================================================="

# Run API server (blocks here)
python api.py

# If API stops, clean up bot
echo "API server stopped. Cleaning up..."
kill $BOT_PID 2>/dev/null
wait $BOT_PID 2>/dev/null

