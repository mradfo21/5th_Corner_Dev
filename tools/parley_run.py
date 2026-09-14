"""Walk an encounter choosing the peace lane and see whether it ends.

Violence and escape already had ways out; peace is new, so the thing worth
watching is that picking it produces a slate that reads like a negotiation
and eventually settles the other person instead of looping the standoff.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:5001"
SID = "default"


def post(path, body, timeout=300):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps({**body, "session_id": SID}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read().decode() or "{}")
            return out if isinstance(out, dict) else {"items": out}
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "body": e.read().decode()[:200]}
    except Exception as e:
        return {"error": str(e)}


def state():
    import engine
    return engine._load_state(SID) or {}


def show(tag):
    enc = state().get("encounter") or {}
    print(f"\n--- {tag} ---")
    print(f"  enemy      : {(enc.get('character') or {}).get('label') or ''}")
    print(f"  motive     : {enc.get('motive') or '(none)'}")
    print(f"  enemy_state: {enc.get('enemy_state') or 'ready'}")
    print(f"  stakes     : {enc.get('stakes') or ''}")
    for c in (enc.get("choices") or []):
        lane = c.get("lane", "?") if isinstance(c, dict) else "?"
        text = c.get("text") if isinstance(c, dict) else str(c)
        print(f"  [{lane:<8}] {text}")
    return enc


def main():
    import engine
    st = engine._load_state(SID) or {}
    st["encounter"] = None
    st["encounter_resolving"] = False
    engine._save_state(st, SID)

    post("/api/choose", {"choice": "Walk further into the yard", "source": "probe"})
    time.sleep(2)

    r = post("/api/encounter/begin", {"force": True})
    if r.get("error"):
        print("begin failed:", r["error"], r.get("body", ""))
        return
    time.sleep(1)
    show("plate")

    for rnd in range(2, 8):
        enc = state().get("encounter") or {}
        choices = enc.get("choices") or []
        if not choices:
            print(f"\nround {rnd}: no choices left — the encounter is over")
            break
        peace = next((c for c in choices
                      if isinstance(c, dict) and c.get("lane") == "parley"), None)
        pick = peace or (choices[0] if isinstance(choices[0], dict)
                         else {"text": str(choices[0]), "lane": "parley"})
        print(f"\nround {rnd}: choosing [{pick.get('lane')}] {pick.get('text')}")
        r = post("/api/encounter/resolve",
                 {"choice": pick.get("text"), "lane": pick.get("lane")})
        if "already_resolving" in str(r.get("body", "")):
            time.sleep(12)
            r = post("/api/encounter/resolve",
                     {"choice": pick.get("text"), "lane": pick.get("lane")})
        if r.get("error"):
            print("  resolve failed:", r["error"], r.get("body", ""))
            break
        print(f"  outcome    : {r.get('outcome')}  released={r.get('released')}")
        show(f"after round {rnd}")
        if r.get("released") or not (state().get("encounter") or {}):
            print(f"\nThe encounter ended on round {rnd}.")
            break
        time.sleep(1)


if __name__ == "__main__":
    main()
