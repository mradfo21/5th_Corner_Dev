"""Fight an encounter for several rounds and watch who owns the clothes.

The reported bug is that by the third frame the enemy is wearing the
player's vest and cap. This walks a turn to get an exploration frame (the
one place the player appears alone), opens an encounter, resolves a few
rounds, and after each one prints what the player is observed wearing, what
the enemy is locked to, and whether those have collided.
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


def post(path, body, timeout=240):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps({**body, "session_id": SID}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read().decode() or "{}")
            # Some endpoints answer with a bare list of feed items.
            return out if isinstance(out, dict) else {"items": out}
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "body": e.read().decode()[:200]}
    except Exception as e:
        return {"error": str(e)}


def state():
    import engine
    return engine._load_state(SID) or {}


def report(tag):
    import encounter as E
    E._LOOK_READ_CACHE.clear()
    E.set_look_session(SID)
    seen = E.observed_player_look(SID)
    outfit = E.observed_player_wardrobe(SID)
    enc = state().get("encounter") or {}
    char = (enc.get("character") or {}) if isinstance(enc, dict) else {}
    locked = char.get("locked_look") or char.get("look") or ""
    label = char.get("label") or ""
    collide = E.look_clones_player(locked) if locked else False
    print(f"\n--- {tag} ---")
    print(f"  player observed : {outfit or '(none yet)'}")
    print(f"  player frame    : {seen[:100]}")
    print(f"  enemy label     : {label}")
    print(f"  enemy locked    : {locked}")
    print(f"  COLLISION       : {'YES — enemy wears the player' if collide else 'no'}")
    return collide


def reset():
    """Clear a half-finished encounter left by an earlier aborted run."""
    import engine
    st = engine._load_state(SID) or {}
    st["encounter"] = None
    st["encounter_resolving"] = False
    engine._save_state(st, SID)


def main():
    reset()
    print("turn 1: walking so there is an exploration frame to learn from")
    r = post("/api/choose", {"choice": "Walk further into the yard",
                             "source": "probe"})
    if r.get("error"):
        print("  choose failed:", r["error"], r.get("body", ""))
    time.sleep(2)
    report("after exploration turn")

    print("\nopening the encounter")
    r = post("/api/encounter/begin", {"force": True})
    if r.get("error"):
        print("  begin failed:", r["error"], r.get("body", ""))
        return
    time.sleep(1)
    bad = report("plate (frame 1)")

    for i in range(2, 5):
        enc = state().get("encounter") or {}
        choices = enc.get("choices") or []
        pick = ""
        for c in choices:
            pick = c.get("text") if isinstance(c, dict) else str(c)
            if pick:
                break
        if not pick:
            print(f"\nround {i}: no choices left — encounter is over")
            break
        print(f"\nround {i}: {pick}")
        r = post("/api/encounter/resolve", {"choice": pick})
        if "already_resolving" in str(r.get("body", "")):
            time.sleep(12)
            r = post("/api/encounter/resolve", {"choice": pick})
        if r.get("error"):
            print("  resolve failed:", r["error"], r.get("body", ""))
            break
        time.sleep(6)
        bad = report(f"resolve (frame {i})") or bad

    print("\n" + "=" * 60)
    print("VERDICT:", "CAST ROTATED" if bad else "cast held across every frame")


if __name__ == "__main__":
    main()
