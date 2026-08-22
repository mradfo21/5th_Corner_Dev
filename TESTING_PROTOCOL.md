# 🧪 Testing Protocol - READ BEFORE EVERY DEPLOY

## **RULE: NEVER PUSH TO PRODUCTION WITHOUT TESTING LOCALLY FIRST**

The whole point of this protocol is that you never *need* to deploy to
Render just to find out whether a change works — see
[CLOUD_AGENT_TESTING.md](CLOUD_AGENT_TESTING.md) for the full local/offline
testing setup this protocol relies on (a Cloud Agent for this repo already
boots with it running).

---

## Quick Test (a few seconds)

```bash
# Catch syntax/import errors fast, without booting anything
python3 -m py_compile *.py
python3 -c "import api"   # imports engine.py, choices.py, etc. transitively
```

**If this fails, DO NOT PUSH.**

---

## Play Test (offline, no API keys, ~10 seconds to a running server)

```bash
python3 run_local.py --mock --no-browser --port 5001 &
python3 autoplay.py --url http://127.0.0.1:5001 --turns 6
```

`autoplay.py` drives the real `/api/reset` → `/api/choose` → `/api/feed`
loop the production UI uses and prints a PASS/FAIL verdict per turn
(resolves, images load, narrative is real, choices regenerate). If a Cloud
Agent is already running the `game-server` terminal, skip straight to the
`autoplay.py` line and point it at that server's port.

---

## Browser Regression Suite (before opening a PR)

```bash
pip install -r requirements-dev.txt   # once
playwright install chromium           # once
python3 -m unittest test_standalone_e2e -v
```

Real headless-Chromium tests against the standalone UI (page load, choice
buttons, keyboard shortcuts, custom actions, VHS toggle, etc.) — this is
the closest thing this repo has to a CI suite. Runs fully offline.

---

## Common Issues & Fixes

### Issue: "SyntaxError: from __future__ imports must occur at the beginning"
**Fix:** Move `from __future__ import annotations` to be the FIRST line after the docstring.

```python
# ❌ WRONG
print("loading...")
from __future__ import annotations

# ✅ CORRECT
from __future__ import annotations
print("loading...")
```

### Issue: Import hangs/times out
**Fix:** Circular dependency detected. Use local imports:

```python
# ❌ WRONG (circular)
import choices  # at top of engine.py

# ✅ CORRECT
def some_function():
    import choices  # local import
    choices.do_something()
```

### Issue: Module not found on Render
**Fix:** Check `requirements.txt` has all dependencies.

---

## Emergency Rollback

If deploy breaks production:

```bash
git log  # Find last working commit
git revert HEAD  # Undo last commit
git push  # Deploy rollback
```

---

## Deployment Checklist

- [ ] Play test passes (`autoplay.py` verdict is all PASS, or failures are understood/expected)
- [ ] Browser regression suite passes (`python3 -m unittest test_standalone_e2e -v`)
- [ ] Relevant `test_*.py` files for the area you touched still pass
- [ ] Git commit with clear message
- [ ] Git push
- [ ] Watch Render logs for 60 seconds after deploy
- [ ] Spot-check the live site after deploy (this should be a formality, not your first test)

---

**Remember: seconds of local testing saves minutes of debugging on a live deploy.** 🚀
