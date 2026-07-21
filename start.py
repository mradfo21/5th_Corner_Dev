#!/usr/bin/env python3
"""
start.py - production entrypoint shim.

The real launch logic lives in start_production.sh (gunicorn api:app in the
foreground). render.yaml uses `bash start_production.sh` directly, but some
Render services have a dashboard Start Command of `python start.py` that
overrides the blueprint. This shim execs the shell script so both entrypoints
behave identically and there is still a single source of truth for how the app
boots.

Prefer setting the start command to `bash start_production.sh` where you can.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "start_production.sh")

if not os.path.exists(SCRIPT):
    sys.stderr.write(f"[start.py] Missing {SCRIPT}; cannot launch.\n")
    sys.exit(1)

# Replace this process with the production launcher.
os.execvp("bash", ["bash", SCRIPT])
