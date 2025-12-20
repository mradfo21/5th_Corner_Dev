# Simple push script - ensures you're on main and pushes correctly
# Usage: .\push.ps1 "your commit message"

param(
    [string]$message = "Update"
)

Write-Host "================================================" -ForegroundColor Cyan
Write-Host "PUSH TO RENDER (Auto-Deploy)" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# Check we're on main branch
$currentBranch = git branch --show-current
if ($currentBranch -ne "main") {
    Write-Host "[ERROR] You're on branch '$currentBranch'" -ForegroundColor Red
    Write-Host "        Switching to 'main'..." -ForegroundColor Yellow
    git checkout main
}

Write-Host "[1/4] Current branch: main" -ForegroundColor Green

# Show what's changed
Write-Host "[2/4] Files changed:" -ForegroundColor Yellow
git status --short

# Add all changes
Write-Host "[3/4] Adding changes..." -ForegroundColor Yellow
git add .

# Commit
Write-Host "[4/4] Committing: '$message'" -ForegroundColor Yellow
git commit -m "$message"

# Push
Write-Host ""
Write-Host "Pushing to GitHub (will trigger Render deploy)..." -ForegroundColor Cyan
git push origin main

Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "PUSHED!" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Render will now:" -ForegroundColor White
Write-Host "  1. Detect the push" -ForegroundColor Gray
Write-Host "  2. Start building (~2-3 min)" -ForegroundColor Gray
Write-Host "  3. Deploy new version" -ForegroundColor Gray
Write-Host "  4. Service goes live (~4-5 min total)" -ForegroundColor Gray
Write-Host ""
Write-Host "Check status: https://dashboard.render.com" -ForegroundColor Cyan
Write-Host ""

