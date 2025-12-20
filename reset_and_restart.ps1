# Quick Reset & Restart Script
# Stops the bot, clears all sessions, and restarts with fresh state

Write-Host "`n=== RESETTING SYSTEM ===" -ForegroundColor Cyan

# 1. Stop all Python processes
Write-Host "Stopping bot..." -ForegroundColor Yellow
Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2

# 2. Clear all sessions
Write-Host "Clearing all sessions..." -ForegroundColor Yellow
Remove-Item -Path "sessions" -Recurse -Force -ErrorAction SilentlyContinue

# 3. Restart bot
Write-Host "Starting bot..." -ForegroundColor Yellow
Start-Process powershell -ArgumentList "-NoExit", "-Command", "python bot.py"

Write-Host "`n=== SYSTEM RESET COMPLETE ===" -ForegroundColor Green
Write-Host "Bot is starting in a new window..." -ForegroundColor Green

