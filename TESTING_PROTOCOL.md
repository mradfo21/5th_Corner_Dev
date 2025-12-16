# 🧪 Testing Protocol - READ BEFORE EVERY DEPLOY

## **RULE: NEVER PUSH TO PRODUCTION WITHOUT RUNNING TESTS**

---

## Quick Test (30 seconds)

```bash
python test_before_deploy.py
```

**If this passes, you can deploy. If it fails, DO NOT PUSH.**

---

## What Gets Tested

### 1. **Syntax Check**
- Compiles all Python files
- Catches syntax errors before deployment

### 2. **Import Test**
- Tests each module imports without hanging
- Catches circular dependencies
- 10-second timeout per module

### 3. **Bot Initialization**
- Verifies bot.py can be imported
- Catches Discord/engine integration issues

### 4. **Common Issues**
- `__future__` import placement
- Circular imports at module level
- Known anti-patterns

---

## Manual Tests (if automated tests pass)

### Local Bot Test
```bash
# Terminal 1: Start bot locally
python bot.py

# Expected output:
# [STARTUP] bot.py loading...
# [STARTUP] Basic imports complete
# [STARTUP] Loading Discord libraries...
# [STARTUP] Discord imports complete
# [STARTUP] Loading engine modules...
# [STARTUP] - engine imported
# [BOT] YourBotName is ready!
# [BOT] Sent intro to channel XXXXX
```

**If bot hangs or errors, DO NOT DEPLOY.**

---

## Deployment Checklist

- [ ] Run `python test_before_deploy.py` - MUST PASS
- [ ] Test imports locally: `python -c "import engine; import bot"`
- [ ] Check for recent changes that could cause issues
- [ ] If testing a new feature, test it locally first
- [ ] Git commit with clear message
- [ ] Git push
- [ ] Watch Render logs for 60 seconds after deploy
- [ ] Test bot in Discord immediately after deploy

---

## Common Issues & Fixes

### Issue: "SyntaxError: from __future__ imports must occur at the beginning"
**Fix:** Move `from __future__ import annotations` to be the FIRST line after docstring

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
**Fix:** Check `requirements.txt` has all dependencies

---

## Emergency Rollback

If deploy breaks production:

```bash
git log  # Find last working commit
git revert HEAD  # Undo last commit
git push  # Deploy rollback
```

---

## Time Savings

- **Without tests:** 10-30 minutes debugging on Render per issue
- **With tests:** 30 seconds to catch issues locally
- **ROI:** ~20-60x faster iteration

---

## Test Coverage

| What | Tested |
|------|--------|
| Syntax errors | ✅ |
| Import errors | ✅ |
| Circular dependencies | ✅ |
| Bot initialization | ✅ |
| __future__ placement | ✅ |
| Runtime errors | ⚠️ Manual only |
| Discord functionality | ⚠️ Manual only |

---

**Remember: 30 seconds of testing saves 30 minutes of debugging!** 🚀



