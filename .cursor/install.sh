#!/usr/bin/env bash
# .cursor/install.sh — Cloud Agent bootstrap for SOMEWHERE STORYGEN.
#
# Goal: every Cloud Agent (and anyone who clones the repo) ends up with a
# fully playable, fully offline copy of the game running locally, so the
# game can be played/tested directly instead of against the deployed
# Render site. No API keys are required for this — run_local.py falls back
# to a deterministic mock backend automatically when none are set.
#
# Runs once per Build (or once per agent boot when no Build is configured).
# Must be idempotent and non-interactive.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "[install] Installing Python dependencies (requirements-dev.txt)..."
pip install --break-system-packages -r requirements-dev.txt

echo "[install] Installing Playwright's Chromium browser + OS deps (needed by test_standalone_e2e.py and any browser-driven testing)..."
python3 -m playwright install --with-deps chromium

echo "[install] Installing libEGL (optional: lets the on-device SCAN object detector run instead of falling back to the Gemini vision backend)..."
sudo apt-get update -y >/dev/null 2>&1 || true
sudo apt-get install -y libegl1 >/dev/null 2>&1 \
  || echo "[install] libegl1 install skipped/failed (non-fatal — local_vision.py falls back to the Gemini detector)"

echo "[install] Done."
echo "[install] Play/test locally with:"
echo "[install]   python3 run_local.py --mock --no-browser --port 5001   # fully offline, no keys needed"
echo "[install]   python3 autoplay.py --url http://127.0.0.1:5001        # headless HTTP playtest + verdict"
echo "[install]   python3 -m unittest test_standalone_e2e -v             # real headless-browser e2e suite"
echo "[install] See CLOUD_AGENT_TESTING.md for details."
