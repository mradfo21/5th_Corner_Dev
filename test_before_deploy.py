#!/usr/bin/env python3
"""
PRE-DEPLOYMENT TEST SUITE
Run this BEFORE every git push to catch issues locally.

Usage: python test_before_deploy.py
"""

import sys
import time
import subprocess

print("=" * 70)
print("PRE-DEPLOYMENT TEST SUITE")
print("=" * 70)

failed_tests = []

# ============================================================================
# TEST 1: Python Syntax Check
# ============================================================================
print("\n[TEST 1] Python Syntax Check...")
files_to_check = ["bot.py", "engine.py", "choices.py", "evolve_prompt_file.py", 
                  "ai_provider_manager.py", "lore_cache_manager.py", 
                  "gemini_image_utils.py"]

for file in files_to_check:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", file],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            print(f"  [OK] {file} - syntax OK")
        else:
            print(f"  [FAIL] {file} - SYNTAX ERROR")
            print(f"    {result.stderr}")
            failed_tests.append(f"Syntax check: {file}")
    except Exception as e:
        print(f"  [FAIL] {file} - ERROR: {e}")
        failed_tests.append(f"Syntax check: {file}")

# ============================================================================
# TEST 2: Module Import Test (Sequential)
# ============================================================================
print("\n[TEST 2] Module Import Test...")

modules_to_test = [
    ("ai_provider_manager", "import ai_provider_manager"),
    ("lore_cache_manager", "import lore_cache_manager"),
    ("evolve_prompt_file", "from evolve_prompt_file import generate_interim_messages_on_demand"),
    ("choices", "import choices"),
    ("engine", "import engine"),
]

for name, import_stmt in modules_to_test:
    try:
        print(f"  Testing {name}...", end=" ")
        sys.stdout.flush()
        
        result = subprocess.run(
            [sys.executable, "-c", import_stmt],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode == 0:
            print("[OK]")
        else:
            print("[FAIL]")
            print(f"    Error: {result.stderr[:200]}")
            failed_tests.append(f"Import: {name}")
    except subprocess.TimeoutExpired:
        print("[FAIL] TIMEOUT (likely circular import)")
        failed_tests.append(f"Import timeout: {name}")
    except Exception as e:
        print(f"[FAIL] {e}")
        failed_tests.append(f"Import: {name}")

# ============================================================================
# TEST 3: Discord Bot Init Test
# ============================================================================
print("\n[TEST 3] Discord Bot Initialization...")

test_bot_code = """
import sys
import os

# Set test mode
os.environ['DISCORD_ENABLED'] = '1'
os.environ['RESUME_MODE'] = '0'

# Try to import bot
try:
    # Import should complete without hanging
    import bot
    print("[OK] Bot imported successfully")
    sys.exit(0)
except Exception as e:
    print(f"[FAIL] Bot import failed: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
"""

try:
    print("  Testing bot.py import...", end=" ")
    sys.stdout.flush()
    
    result = subprocess.run(
        [sys.executable, "-c", test_bot_code],
        capture_output=True,
        text=True,
        timeout=15
    )
    
    if result.returncode == 0 and "[OK]" in result.stdout:
        print("[OK]")
    else:
        print("[FAIL]")
        print(f"    stdout: {result.stdout[:200]}")
        print(f"    stderr: {result.stderr[:200]}")
        failed_tests.append("Bot initialization")
except subprocess.TimeoutExpired:
    print("[FAIL] TIMEOUT")
    failed_tests.append("Bot init timeout")
except Exception as e:
    print(f"✗ {e}")
    failed_tests.append("Bot initialization")

# ============================================================================
# TEST 4: Check for Common Issues
# ============================================================================
print("\n[TEST 4] Common Issue Detection...")

# Check for __future__ imports not at top
print("  Checking __future__ import placement...", end=" ")
for file in ["engine.py", "bot.py", "choices.py"]:
    try:
        with open(file, "r", encoding="utf-8") as f:
            lines = f.readlines()
            
        # Find first non-comment, non-docstring, non-blank line
        in_docstring = False
        first_code_line = None
        
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            
            # Track docstrings
            if stripped.startswith('"""') or stripped.startswith("'''"):
                if in_docstring:
                    in_docstring = False
                    continue
                else:
                    in_docstring = True
                    continue
            
            if in_docstring:
                continue
                
            # Skip comments and blank lines
            if not stripped or stripped.startswith("#"):
                continue
            
            # This is the first real code line
            first_code_line = (i, stripped)
            break
        
        # Check if it's a __future__ import
        if first_code_line and "from __future__ import" in first_code_line[1]:
            # Good!
            pass
        elif first_code_line and any(keyword in first_code_line[1] for keyword in ["import ", "from ", "print(", "class ", "def "]):
            # Check if there's a __future__ import later
            has_future_import = any("from __future__ import" in line for line in lines)
            if has_future_import:
                # Find where it is
                for i, line in enumerate(lines, 1):
                    if "from __future__ import" in line:
                        print(f"\n    [FAIL] {file}:{i} - __future__ import must be first!")
                        failed_tests.append(f"__future__ placement in {file}")
                        break
    except Exception as e:
        print(f"\n    [WARN] Error checking {file}: {e}")

if "__future__ placement" not in str(failed_tests):
    print("[OK]")

# Check for circular imports in top-level imports
print("  Checking for potential circular imports...", end=" ")
circular_issues = []

# Check engine.py doesn't import choices at module level
try:
    with open("engine.py", "r", encoding="utf-8") as f:
        lines = f.readlines()
    
    for i, line in enumerate(lines[:50], 1):  # Check first 50 lines
        if line.strip().startswith("import choices") or line.strip().startswith("from choices import"):
            if "# Local import" not in line and "def " not in "".join(lines[max(0,i-5):i]):
                circular_issues.append(f"engine.py:{i} imports choices at module level")
except Exception as e:
    print(f"\n    ? Error: {e}")

if circular_issues:
    print("[FAIL]")
    for issue in circular_issues:
        print(f"    {issue}")
    failed_tests.append("Circular import detection")
else:
    print("[OK]")

# ============================================================================
# RESULTS
# ============================================================================
print("\n" + "=" * 70)

if failed_tests:
    print("[FAIL] TESTS FAILED - DO NOT DEPLOY!")
    print("=" * 70)
    print("\nFailed tests:")
    for i, test in enumerate(failed_tests, 1):
        print(f"  {i}. {test}")
    print("\n" + "=" * 70)
    sys.exit(1)
else:
    print("[PASS] ALL TESTS PASSED - SAFE TO DEPLOY")
    print("=" * 70)
    print("\nYou can now run:")
    print("  git add .")
    print("  git commit -m 'your message'")
    print("  git push")
    print("=" * 70)
    sys.exit(0)

