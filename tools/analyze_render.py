"""Score a render against the failure modes long runs keep exposing.

    python tools/analyze_render.py playtest_results/renders/render_<stamp>

The 60-turn run parked the player in one room for 18 turns, moved the clock 3
times, and let one wound motif dominate two thirds of the transcript. Reading a
transcript by hand does not surface any of that; these numbers do, and they are
comparable across runs so a change can be shown to have worked.

Pass a run directory. Reads `session.json`, writes nothing.
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

run = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
data = json.loads((run / "session.json").read_text(encoding="utf-8"))
turns = data["turns"]
n = len(turns)


def status(t, key, default=None):
    return (t.get("game_status") or {}).get(key, default)


print(f"{run.name}   {n} turns   verdict={data.get('verdict', {}).get('passed')}")

# --- 1. spatial stagnation -------------------------------------------------
subjects = [(t.get("subject") or "?").lower() for t in turns]
longest, cur, where = 1, 1, subjects[0]
for a, b in zip(subjects, subjects[1:]):
    cur = cur + 1 if a == b else 1
    if cur > longest:
        longest, where = cur, b
print(f"\nSPATIAL    longest streak on one subject: {longest} turns ({where})")
print(f"           distinct subjects: {len(set(subjects))}/{n}")
top = Counter(subjects).most_common(4)
print("           most visited: " + ", ".join(f"{s}x{c}" for s, c in top))

# --- 2. the clock ----------------------------------------------------------
clocks = [str(status(t, "time_of_day", "")) for t in turns]
times = [re.match(r"\s*([\d:apm]+)", c, re.I).group(1) if re.match(r"\s*([\d:apm]+)", c, re.I)
         else c for c in clocks]
moves = sum(1 for a, b in zip(times, times[1:]) if a != b)
print(f"\nCLOCK      changed {moves} times in {n} turns")
print(f"           {times[0]}  ->  {times[-1]}")

# --- 3. the dials ----------------------------------------------------------
# There was a health range printed here, off a hit-point pool. The pool is gone
# (see engine's "how a run ends"), so detection is the danger dial now.
chaos = [status(t, "chaos") for t in turns if status(t, "chaos") is not None]
phases = [status(t, "phase") for t in turns]
print("\nDIALS", end="")

# Detection did not exist before the danger pass, so an older transcript has
# nothing here and should say so rather than reporting a confident "hidden".
detect = [status(t, "detection") for t in turns]
if any(detect):
    order = ("hidden", "suspicious", "alerted", "hunted")
    worst = max((d for d in detect if d), key=lambda d: order.index(d)
                if d in order else 0)
    exposed = sum(1 for d in detect if d and d != "hidden")
    print(f"      detect {' -> '.join(dict.fromkeys(d for d in detect if d))}")
    print(f"           worst {worst}, exposed on {exposed}/{n} turns")
else:
    print("      detect not recorded (run predates the detection system)")

print(f"           chaos  {min(chaos)}-{max(chaos)}   mean {sum(chaos) / len(chaos):.1f}")
seq, last = [], None
for p in phases:
    if p != last:
        seq.append(p)
        last = p
print(f"           phase  {' -> '.join(str(s) for s in seq)}")

# --- 4. motif lock ---------------------------------------------------------
text = " ".join((t.get("narrative") or "").lower() for t in turns)
for motif in ("thigh", "wound", "blood", "leg", "gear", "hum", "door"):
    hits = sum(1 for t in turns if motif in (t.get("narrative") or "").lower())
    if hits:
        bar = "#" * round(20 * hits / n)
        print(f"           {motif:<7} {hits:>3}/{n} {bar}")

lens = [len(t.get("narrative") or "") for t in turns]
capped = sum(1 for x in lens if x >= 395)
print(f"\nPROSE      mean {sum(lens) // len(lens)} chars, {capped}/{n} at the 399 cap")
