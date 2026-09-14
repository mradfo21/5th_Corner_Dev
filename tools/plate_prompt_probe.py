"""What does the encounter plate ask for, versus a normal scene?

The player's read was that the plate is "a radically different prompt from
the previous scene", drops the editor's camera, and "instructs it to look
ugly". This prints both anchors side by side plus the finished plate prompt
so the difference is visible instead of argued about.
"""

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

env = ROOT / ".env"
if env.exists():
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

import game_identity as gi  # noqa: E402
import encounter as E  # noqa: E402
import engine  # noqa: E402


def rule(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


rule("EDITOR CAMERA (what the player configured)")
print("vantage    :", gi.vantage())
spec = gi.get_spec()
cam = spec[gi.CAMERA_KEY]
print("lens       :", cam.get("lens") or "(none)")
print("notes      :", (cam.get("notes") or "(none)")[:200])
print("shows_char :", gi.shows_character())

rule("NORMAL SCENE ANCHOR  (include_vantage=True)")
normal = gi.world_anchor(engine.STYLE_ANCHOR if hasattr(engine, "STYLE_ANCHOR")
                         else "cinematic still", include_character=True,
                         include_vantage=True)
print(normal)

rule("ENCOUNTER PLATE ANCHOR")
plate_anchor = gi.world_anchor(E.ENCOUNTER_PLATE_STYLE_ANCHOR,
                               include_character=True, include_vantage=True)
print(plate_anchor)

rule("DOES THE PLATE KEEP THE EDITOR'S CAMERA?")
for name, bit in (("vantage", gi.vantage()), ("lens", cam.get("lens") or ""),
                  ("notes", cam.get("notes") or "")):
    bit = (bit or "").strip()
    if not bit:
        continue
    verdict = "kept" if bit.rstrip(". ") in plate_anchor else "DROPPED"
    print(f"  {name:8} {verdict}: {bit[:120]}")

brief = E.normalize_encounter_brief({
    "character": {"label": "A man", "kind": "person",
                  "look": "a man in a grease-stained grey coverall",
                  "stance": "hostile"},
    "danger": "he is swinging a wrench at you",
    "stakes": "If you hesitate you will not walk away clean.",
    "place_hold": "outdoor industrial yard, chain-link, rusted plant",
})
prompt = E.build_encounter_plate_prompt(brief, img2img=True, setting="outdoor")

rule("FULL PLATE PROMPT (img2img)")
print(prompt)

rule("PROMPT HEALTH")
print("length          :", len(prompt), "chars")
negs = re.findall(r"\b(?:Do not|Never|No |not a |no HUD|Avoid)\b", prompt)
print("negative phrases:", len(negs))
sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", prompt) if s.strip()]
print("sentences       :", len(sents))
print("\ntruncated / malformed sentences:")
for s in sents:
    if re.search(r"\b(?:never|and|or|the|a|of|with|to|is|are)\s*\.$", s, re.I):
        print("  !!", s[-110:])
print("\nrepeated instructions:")
seen = {}
for s in sents:
    key = re.sub(r"[^a-z ]", "", s.lower())[:40]
    seen[key] = seen.get(key, 0) + 1
for k, n in seen.items():
    if n > 1 and k.strip():
        print(f"  x{n}: {k}")
print("\nanticipation / mood words:", re.findall(
    r"\b(?:anticipation|tension|tense|about to|any second|holding|breath|"
    r"stillness|charged|before)\b", prompt, re.I))
