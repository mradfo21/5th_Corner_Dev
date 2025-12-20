#!/bin/bash
# Quick Reset & Restart Script
# Stops the bot, clears all sessions, and restarts with fresh state

echo ""
echo "=== RESETTING SYSTEM ==="
echo ""

# 1. Stop all Python processes running bot.py
echo "Stopping bot..."
pkill -f "python.*bot.py" 2>/dev/null || true
sleep 2

# 2. Clear all sessions
echo "Clearing all sessions..."
rm -rf sessions

# 3. Restart bot
echo "Starting bot..."
python bot.py &

echo ""
echo "=== SYSTEM RESET COMPLETE ==="
echo "Bot is starting in the background..."
echo "Check logs with: tail -f terminals/*.txt"

