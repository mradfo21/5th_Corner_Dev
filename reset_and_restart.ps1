# Quick Reset & Restart Script
# Stops the local server, clears all sessions, and restarts with fresh state

Write-Host "`n=== RESETTING SYSTEM ===" -ForegroundColor Cyan

# 1. Stop all Python processes
Write-Host "Stopping server..." -ForegroundColor Yellow
Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2

# 2. Clear all sessions
Write-Host "Clearing all sessions..." -ForegroundColor Yellow
Remove-Item -Path "sessions" -Recurse -Force -ErrorAction SilentlyContinue

# 3. Restart the local server
Write-Host "Starting server..." -ForegroundColor Yellow
Start-Process powershell -ArgumentList "-NoExit", "-Command", "python run_local.py"

Write-Host "`n=== SYSTEM RESET COMPLETE ===" -ForegroundColor Green
Write-Host "Server is starting in a new window..." -ForegroundColor Green
