"""Does the clause splitter cut between people, or through one person?"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

import encounter as E  # noqa: E402

CASES = [
    ("The man has long, matted hair and a torn flannel shirt.",
     "a dishevelled man in a torn flannel shirt"),
    ("A man in a green quilted vest and a dark baseball cap faces an older "
     "man in a plaid shirt holding a shotgun.",
     "an older man in a plaid shirt"),
    ("A man in a green vest stands in the foreground, and another man in a "
     "grease-stained coverall steps toward him.",
     "a mechanic in a grease-stained coverall"),
    ("A gaunt figure with sunken eyes, cracked lips, and a filthy bandage "
     "on one forearm blocks the path.",
     "a gaunt figure with a filthy bandage"),
]

print()
for seen, invented in CASES:
    parts = [p for p in E._PERSON_SPLIT_RE.split(seen) if p and p.strip()]
    got = E.plate_stranger_look(seen, fallback=invented)
    print(f"  text   : {seen[:78]}")
    print(f"  split  : {parts}")
    print(f"  locked : {got}")
    print()
