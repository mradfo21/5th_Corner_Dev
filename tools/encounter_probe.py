"""Drive one encounter end to end against the local server and report what
the player would actually see: the nameplate, the locked look, the plate the
image model drew, and whether the exchange escalated instead of looping.

    python -B tools/encounter_probe.py --session encprobe --rounds 3
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:5001"
OUT = Path("logs/encounter_probe")


def post(path: str, body: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code,
                "_body": e.read().decode("utf-8", "replace")[:400]}
    except Exception as e:
        return {"_error": str(e)}


def get(path: str, timeout: int = 60) -> dict:
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8") or "{}")
    except Exception as e:
        return {"_error": str(e)}
    # /api/state wraps the payload in {success, message, data}.
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return body["data"]
    return body


def disk_state(session: str) -> dict:
    """encounter_outcome is server-side only; read it off the session file."""
    f = Path("sessions", session, "state.json")
    if not f.is_file():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def copy_frame(url: str, session: str, tag: str) -> str:
    """Pull the rendered still out of the session dir so it can be eyeballed."""
    if not url:
        return ""
    name = url.rstrip("/").split("/")[-1].split("?")[0]
    src = Path("sessions", session, "images", name)
    if not src.is_file():
        return ""
    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / f"{tag}_{name}"
    shutil.copy2(src, dest)
    return str(dest.resolve())


def warm_up(session: str, turns: int = 2) -> str:
    """An encounter restages the CURRENT frame. With no frame there is no
    img2img reference, so no plate and nothing to lock the character to."""
    url = ""
    for i in range(turns):
        st = get(f"/api/state?session_id={session}")
        pick = ""
        for c in st.get("choices") or []:
            pick = c.get("text") if isinstance(c, dict) else str(c)
            if pick:
                break
        post("/api/choose",
             {"session_id": session, "choice": pick or "Move forward carefully"})
        url = get(f"/api/state?session_id={session}").get("current_image_url") or url
        print(f"  warm-up turn {i + 1}: {'frame rendered' if url else 'no frame yet'}")
    return url


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="encprobe")
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()
    sid = args.session

    print("=" * 78)
    print("ENCOUNTER PROBE")
    print("=" * 78)

    import game_identity
    spec = game_identity.get_spec()
    who = spec.get("player_character") or {}
    print(f"protagonist on the sheet : {who.get('name')!r} (enabled={who.get('enabled')})")
    print(f"camera                   : {(spec.get('camera_perspective') or {}).get('mode')}")

    print("\n-- warming up so there is a frame to restage --")
    warm_up(sid, turns=2)

    print("\n-- opening the encounter --")
    begun = post("/api/encounter/begin", {"session_id": sid, "force": True})
    if begun.get("_http_error") or begun.get("error") or begun.get("_error"):
        print("  begin failed:", json.dumps(begun)[:400])
        return
    enc = begun.get("encounter") or {}
    char = enc.get("character") or {}
    print(f"  nameplate   : {char.get('label')!r}")
    print(f"  brief look  : {str(char.get('look'))[:120]!r}")
    print(f"  locked look : {str(char.get('locked_look'))[:120]!r}")
    print(f"  plate shows : {str(enc.get('plate_seen'))[:200]!r}")
    print(f"  choices     : {[c.get('text') for c in begun.get('choices') or []]}")
    saved = copy_frame(begun.get("plate_url") or "", sid, "00_standoff")
    if saved:
        print(f"  PLATE IMAGE : {saved}")

    for rnd in range(1, args.rounds + 1):
        live = get(f"/api/state?session_id={sid}").get("encounter") or {}
        if not live:
            print(f"\n-- round {rnd}: the encounter is closed, the fight ended --")
            break
        slate = live.get("choices") or begun.get("choices") or []
        pick = None
        for c in slate:
            if isinstance(c, dict) and c.get("lane") == "confront":
                pick = c
                break
        pick = pick or (slate[0] if slate else None)
        if not pick:
            print(f"\n-- round {rnd}: no choices on the slate --")
            break
        text = pick.get("text") if isinstance(pick, dict) else str(pick)
        lane = pick.get("lane") if isinstance(pick, dict) else ""
        print(f"\n-- round {rnd}: committing to {text!r} (lane={lane}) --")
        print(f"  enemy before : {live.get('enemy_state') or 'ready'}")
        res = post("/api/encounter/resolve",
                   {"session_id": sid, "choice": text, "lane": lane})
        if res.get("_http_error") or res.get("error") or res.get("_error"):
            print("  resolve failed:", json.dumps(res)[:400])
            break
        rec = disk_state(sid).get("encounter_outcome") or {}
        print(f"  outcome      : {res.get('outcome')}")
        print(f"  enemy after  : {rec.get('enemy_state')}")
        print(f"  fight ends   : {res.get('released')}")
        saved = copy_frame(res.get("resolve_url") or "", sid,
                           f"{rnd:02d}_{res.get('outcome')}")
        if saved:
            print(f"  ACTION IMAGE : {saved}")
        if res.get("released"):
            print("  -> the encounter RESOLVED instead of looping")
            break

    print("\n-- identity after the fight --")
    print(f"protagonist still        : {game_identity.display_name()!r}")


if __name__ == "__main__":
    main()
