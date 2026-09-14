"""What does the encounter slate actually come back as, round by round?

The plate slate fans correctly but a continued round fell back to the canned
choices, which is the "three useless moves" the player complains about. This
prints the raw model answer, what survived parsing, and what survived the
threat filter, so it is obvious which stage is dropping the slate.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

import engine  # noqa: E402
import encounter as enc  # noqa: E402

BRIEF = enc.normalize_encounter_brief({
    "character": {
        "label": "A man in plaid shirt",
        "kind": "person",
        "look": "an older man with disheveled hair, wearing a plaid shirt",
        "stance": "desperate",
    },
    "motive": ("He believes the camera gear is something he can trade for "
               "food and protection from the facility's horrors."),
    "danger": ("He swings an iron pipe toward the player's head in a "
               "desperate, erratic arc."),
    "stakes": "If you hesitate he takes the camera off your body.",
    "place_hold": "a derelict industrial yard",
})

hist = json.load(open(ROOT / "sessions/default/history.json", encoding="utf-8"))
frame = next((e.get("image") for e in reversed(hist)
              if e.get("image") and Path(str(e["image"])).exists()), None)
print(f"frame: {Path(str(frame)).name if frame else '(none)'}\n")

for continued in (False, True):
    tag = "CONTINUED ROUND" if continued else "OPENING PLATE"
    print("=" * 68)
    print(tag)
    print("=" * 68)
    prompt = (enc.DEFAULT_CHOICE_INSTRUCTIONS.strip() + "\n\n"
              + enc.encounter_choice_overlay(BRIEF, continued=continued))
    raw = engine._ask(prompt, model="gemini", temp=0.9, tokens=120,
                      image_path=frame, use_lore=False,
                      response_schema=enc.ENCOUNTER_CHOICE_SCHEMA)
    print(f"raw model answer : {str(raw)[:300]}")
    parsed = enc._parse_choice_payload(raw)
    print(f"after parsing    : {[(c['lane'], c['text']) for c in parsed]}")
    filtered = enc.prefer_threat_choices(parsed, BRIEF)
    print(f"after threat filt: {[(c['lane'], c['text']) for c in filtered]}")
    if len(filtered) < 3:
        print("  -> FELL BACK to the canned slate")
    print()
