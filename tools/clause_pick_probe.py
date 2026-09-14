"""Which of the two people in a plate does the enemy get locked to?

The plate is a two-shot and the prompt introduces the player first, so
"first clause that mentions clothes" reliably picked the protagonist.
"""

import os
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

env = ROOT / ".env"
if env.exists():
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

import encounter as E  # noqa: E402

CASES = [
    # (plate description, what the brief invented, who the enemy must be)
    ("A man in a green quilted vest and a dark baseball cap faces an older "
     "man in a plaid shirt holding a shotgun.",
     "an older man wearing a baseball cap and plaid shirt who holds a shotgun",
     "plaid"),
    ("A man in a green vest stands in the foreground, and another man in a "
     "grease-stained coverall steps toward him.",
     "a mechanic in a grease-stained coverall",
     "coverall"),
    ("A photojournalist in an olive field jacket confronts a woman in a torn "
     "red windbreaker.",
     "a woman in a torn red windbreaker",
     "windbreaker"),
    # The player is described SECOND here, so the fix must not simply flip.
    ("A man in a heavy rubber apron blocks the path while a man in an olive "
     "field jacket raises a camcorder.",
     "a worker in a heavy rubber apron",
     "apron"),
]

print()
bad = 0
for seen, invented, must in CASES:
    got = E.plate_stranger_look(seen, fallback=invented)
    ok = must in got.lower()
    bad += 0 if ok else 1
    print(f"  [{'ok' if ok else 'WRONG':5}] expect '{must}'")
    print(f"          plate : {seen[:88]}")
    print(f"          locked: {got}")
print()
print("VERDICT:", "enemy locked to the right person every time" if not bad
      else f"{bad} case(s) still lock onto the player")
