import json
import os
from pathlib import Path
import difflib

SUGGESTIONS_FILE = "autotest_suggestions.json"
LOG_FILE = "llm_apply_log.txt"

with open(SUGGESTIONS_FILE, "r", encoding="utf-8") as f:
    suggestions = json.load(f)

log_lines = []

def prompt_yes_no(prompt):
    while True:
        ans = input(prompt + " [y/n]: ").strip().lower()
        if ans in ("y", "yes"): return True
        if ans in ("n", "no"): return False

for idx, s in enumerate(suggestions):
    file = s.get("file")
    location = s.get("location")
    change = s.get("change")
    reason = s.get("reason")
    flag = s.get("flag")
    print(f"\n--- Suggestion {idx+1} ---")
    print(f"File: {file}\nLocation: {location}\nReason: {reason}")
    if flag:
        print(f"[FLAGGED] {flag}")
        log_lines.append(f"[SKIPPED FLAGGED] {flag}\n")
        continue
    print(f"Planned Change:\n{change}\n")
    if not prompt_yes_no("Apply this change?"):
        log_lines.append(f"[SKIPPED] {file} @ {location}: {reason}\n")
        continue
    path = Path(file)
    if not path.exists():
        print(f"[ERROR] File not found: {file}")
        log_lines.append(f"[ERROR] File not found: {file}\n")
        continue
    if file.endswith(".json"):
        # JSON key replacement
        with open(path, "r", encoding="utf-8") as jf:
            data = json.load(jf)
        # Try to find the key (location)
        if location in data:
            before = data[location]
            print(f"Old value:\n{before}\nNew value:\n{change}\n")
            if prompt_yes_no("Replace this value?"):
                data[location] = change
                with open(path, "w", encoding="utf-8") as jf:
                    json.dump(data, jf, indent=2)
                log_lines.append(f"[APPLIED] {file} @ {location}: {reason}\n")
            else:
                log_lines.append(f"[SKIPPED] {file} @ {location}: {reason}\n")
        else:
            print(f"[ERROR] Key '{location}' not found in {file}")
            log_lines.append(f"[ERROR] Key '{location}' not found in {file}\n")
    elif file.endswith(".py"):
        # Function/section replacement
        with open(path, "r", encoding="utf-8") as pf:
            lines = pf.readlines()
        # Find the function or section by name
        start, end = None, None
        for i, line in enumerate(lines):
            if location in line:
                start = i
                # Try to find the end of the function (next def/class or end of file)
                for j in range(i+1, len(lines)):
                    if lines[j].strip().startswith(("def ", "class ")):
                        end = j
                        break
                if end is None:
                    end = len(lines)
                break
        if start is not None:
            before = ''.join(lines[start:end])
            print(f"--- BEFORE ---\n{before}\n--- AFTER (proposed) ---\n{change}\n")
            if prompt_yes_no("Replace this section?"):
                new_lines = lines[:start] + [change + "\n"] + lines[end:]
                with open(path, "w", encoding="utf-8") as pf:
                    pf.writelines(new_lines)
                log_lines.append(f"[APPLIED] {file} @ {location}: {reason}\n")
            else:
                log_lines.append(f"[SKIPPED] {file} @ {location}: {reason}\n")
        else:
            print(f"[ERROR] Location '{location}' not found in {file}")
            log_lines.append(f"[ERROR] Location '{location}' not found in {file}\n")
    else:
        print(f"[ERROR] Unsupported file type: {file}")
        log_lines.append(f"[ERROR] Unsupported file type: {file}\n")

with open(LOG_FILE, "a", encoding="utf-8") as lf:
    lf.writelines(log_lines)

print("\nAll suggestions processed. See llm_apply_log.txt for a summary.") 