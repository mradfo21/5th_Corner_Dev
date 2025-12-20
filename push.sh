#!/bin/bash
# Simple push script - ensures you're on main and pushes correctly
# Usage: ./push.sh "your commit message"

MESSAGE="${1:-Update}"

echo "================================================"
echo "PUSH TO RENDER (Auto-Deploy)"
echo "================================================"
echo ""

# Check we're on main branch
CURRENT_BRANCH=$(git branch --show-current)
if [ "$CURRENT_BRANCH" != "main" ]; then
    echo "[ERROR] You're on branch '$CURRENT_BRANCH'"
    echo "        Switching to 'main'..."
    git checkout main
fi

echo "[1/4] Current branch: main"

# Show what's changed
echo "[2/4] Files changed:"
git status --short

# Add all changes
echo "[3/4] Adding changes..."
git add .

# Commit
echo "[4/4] Committing: '$MESSAGE'"
git commit -m "$MESSAGE"

# Push
echo ""
echo "Pushing to GitHub (will trigger Render deploy)..."
git push origin main

echo ""
echo "================================================"
echo "PUSHED!"
echo "================================================"
echo ""
echo "Render will now:"
echo "  1. Detect the push"
echo "  2. Start building (~2-3 min)"
echo "  3. Deploy new version"
echo "  4. Service goes live (~4-5 min total)"
echo ""
echo "Check status: https://dashboard.render.com"
echo ""

