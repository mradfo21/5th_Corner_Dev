#!/usr/bin/env python3
"""
autoplay.py — automated playtester for the SOMEWHERE standalone game.

Drives the real feed API (POST /api/reset, POST /api/choose, GET /api/feed,
GET /api/status) exactly like the browser does, plays a run, and reports on
the things that make the game functional / fast / fun:

  - Does every turn RESOLVE (produce a new choice prompt)?        [functional]
  - How long does each turn take?                                  [fast]
  - Do scene images actually LOAD (HTTP 200, image bytes)?         [functional]
  - Is the narrative REAL AI text (not an API-error fallback)?     [fun]
  - Do the CHOICES change turn to turn (regeneration)?             [fun]
  - Does a custom free-will action work?                           [functional]

Run against local (needs GEMINI_API_KEY for full content) or the live deploy:

    python3 autoplay.py --url https://your-app.onrender.com --turns 6
    python3 autoplay.py --url http://127.0.0.1:5096 --turns 6 --strategy cycle

Writes a JSON report to autoplay_report.json and prints a summary + verdict.
"""
import argparse
import json
import sys
import time
import urllib.request
import urllib.error


FALLBACK_MARKERS = [
    "signal interrupted", "api error", "the situation evolves",
    "you make a tense move in the chaos",
]
# Contextual fallback choice sets the engine emits when the LLM fails.
KNOWN_FALLBACKS = {
    "look around", "move forward", "wait", "investigate further",
    "scan the area", "proceed with caution",
}


def _req(method, url, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return r.status, raw, r.headers.get("Content-Type", ""), time.time() - t0


def get_json(base, path, timeout=30):
    st, raw, _, _ = _req("GET", base + path, timeout=timeout)
    return json.loads(raw.decode())


def post_json(base, path, body, timeout=120):
    st, raw, _, dt = _req("POST", base + path, body, timeout=timeout)
    return json.loads(raw.decode()), dt


def check_image(base, url, timeout=30):
    """Return (ok, status, bytes, content_type)."""
    full = url if url.startswith("http") else base + url
    try:
        st, raw, ctype, _ = _req("GET", full, timeout=timeout)
        ok = st == 200 and ctype.startswith("image/") and len(raw) > 1000
        return ok, st, len(raw), ctype
    except urllib.error.HTTPError as e:
        return False, e.code, 0, ""
    except Exception as e:
        return False, str(e), 0, ""


def latest_prompt(items):
    for it in reversed(items):
        if it.get("type") == "player_choice_prompt":
            return it
    return None


def choice_texts(prompt):
    return [c.get("text", "") for c in (prompt.get("choices") or [])] if prompt else []


def is_fallback_text(text):
    t = (text or "").lower()
    return any(m in t for m in FALLBACK_MARKERS)


def wait_for_turn(base, since_id, timeout_s):
    """Poll the feed until the turn resolves. Returns (items_since, elapsed, status)."""
    start = time.time()
    while time.time() - start < timeout_s:
        items = get_json(base, f"/api/feed?since_id={since_id}")
        types = {i.get("type") for i in items}
        if "player_choice_prompt" in types:
            return items, time.time() - start, "resolved"
        if "game_over" in types:
            return items, time.time() - start, "death"
        if "error_event" in types and "narrative_event" not in types:
            return items, time.time() - start, "error"
        time.sleep(1.0)
    return get_json(base, f"/api/feed?since_id={since_id}"), time.time() - start, "timeout"


def play(base, turns, strategy, turn_timeout):
    report = {"base": base, "turns": [], "started": time.time()}

    reset_items, reset_dt = post_json(base, "/api/reset", {})
    intro_prompt = latest_prompt(reset_items)
    intro_imgs = [i.get("image_url") for i in reset_items if i.get("image_url")]
    intro_img_ok = None
    if intro_imgs:
        intro_img_ok = check_image(base, intro_imgs[0])[0]
    report["intro"] = {
        "elapsed": round(reset_dt, 1),
        "choices": choice_texts(intro_prompt),
        "narrative_real": not any(
            is_fallback_text(i.get("content")) for i in reset_items if i.get("type") == "narrative_event"),
        "image_present": bool(intro_imgs),
        "image_loads": intro_img_ok,
    }
    print(f"[intro] {reset_dt:.1f}s  choices={choice_texts(intro_prompt)}  "
          f"img={'ok' if intro_img_ok else 'MISSING/FAIL'}  "
          f"real_text={report['intro']['narrative_real']}")

    prev_choices = choice_texts(intro_prompt)
    last_id = max((i.get("id", 0) for i in reset_items), default=0)
    current_prompt = intro_prompt

    for n in range(turns):
        choices = choice_texts(current_prompt)
        if strategy == "custom" or (strategy == "mixed" and n % 3 == 2):
            choice = "Raise the camcorder and film the fence line, hands trembling"
            picked = f"[custom] {choice}"
        else:
            idx = (n % max(1, len(choices))) if strategy == "cycle" else 0
            choice = choices[idx] if choices else "Look around"
            picked = choice

        since = last_id
        _, _ = post_json(base, "/api/choose", {"choice": choice, "context_item_id": last_id})
        items, elapsed, status = wait_for_turn(base, since, turn_timeout)

        narr = " ".join(i.get("content", "") for i in items if i.get("type") == "narrative_event")
        scene = next((i for i in items if i.get("type") == "scene_image"), None)
        img_url = (scene or {}).get("image_url") or next((i.get("image_url") for i in items if i.get("image_url")), None)
        img_ok, img_status, img_bytes, _ = check_image(base, img_url) if img_url else (False, "none", 0, "")
        new_prompt = latest_prompt(items)
        new_choices = choice_texts(new_prompt)
        regenerated = bool(new_choices) and new_choices != prev_choices
        real_text = bool(narr) and not is_fallback_text(narr)
        fallback_choices = bool(new_choices) and all(c.lower() in KNOWN_FALLBACKS for c in new_choices)

        turn = {
            "n": n + 1, "picked": picked, "status": status, "elapsed": round(elapsed, 1),
            "narrative": narr[:200], "narrative_real": real_text,
            "image_present": bool(img_url), "image_loads": img_ok, "image_status": img_status, "image_bytes": img_bytes,
            "new_choices": new_choices, "choices_regenerated": regenerated,
            "choices_are_fallback": fallback_choices,
        }
        report["turns"].append(turn)
        print(f"[turn {n+1}] {status} {elapsed:.1f}s  pick={picked[:40]!r}\n"
              f"         img={'ok' if img_ok else 'FAIL('+str(img_status)+')'}  "
              f"real_text={real_text}  regen_choices={regenerated}  fallback_choices={fallback_choices}\n"
              f"         narrative={narr[:110]!r}\n"
              f"         choices={new_choices}")

        if status in ("death", "timeout"):
            if status == "death":
                # restart to keep exercising the loop
                reset_items, _ = post_json(base, "/api/reset", {})
                current_prompt = latest_prompt(reset_items)
                last_id = max((i.get("id", 0) for i in reset_items), default=0)
                prev_choices = choice_texts(current_prompt)
                continue
            else:
                break

        prev_choices = new_choices
        current_prompt = new_prompt
        last_id = max((i.get("id", last_id) for i in items), default=last_id)

    return summarize(report)


def summarize(report):
    turns = report["turns"]
    resolved = [t for t in turns if t["status"] in ("resolved", "death")]
    times = [t["elapsed"] for t in turns if t["status"] != "timeout"]
    report["summary"] = {
        "turns_played": len(turns),
        "turns_resolved": len(resolved),
        "turns_timed_out": sum(1 for t in turns if t["status"] == "timeout"),
        "avg_turn_s": round(sum(times) / len(times), 1) if times else None,
        "max_turn_s": round(max(times), 1) if times else None,
        "image_load_rate": round(sum(1 for t in turns if t["image_loads"]) / len(turns), 2) if turns else 0,
        "real_text_rate": round(sum(1 for t in turns if t["narrative_real"]) / len(turns), 2) if turns else 0,
        "choice_regen_rate": round(sum(1 for t in turns if t["choices_regenerated"]) / len(turns), 2) if turns else 0,
        "fallback_choice_rate": round(sum(1 for t in turns if t["choices_are_fallback"]) / len(turns), 2) if turns else 0,
    }
    s = report["summary"]
    verdict = []
    verdict.append(("functional: every turn resolves", s["turns_resolved"] == s["turns_played"] and s["turns_played"] > 0))
    verdict.append(("fast: avg turn < 20s", (s["avg_turn_s"] or 999) < 20))
    verdict.append(("images load on turns", s["image_load_rate"] >= 0.8))
    verdict.append(("narrative is real AI text", s["real_text_rate"] >= 0.8))
    verdict.append(("choices regenerate", s["choice_regen_rate"] >= 0.6))
    report["verdict"] = {name: ok for name, ok in verdict}

    print("\n==================== SUMMARY ====================")
    for k, v in s.items():
        print(f"  {k}: {v}")
    print("---------------------- VERDICT ------------------")
    for name, ok in verdict:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print("=================================================")
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:5096")
    ap.add_argument("--turns", type=int, default=6)
    ap.add_argument("--strategy", choices=["first", "cycle", "mixed", "custom"], default="mixed")
    ap.add_argument("--turn-timeout", type=int, default=90)
    ap.add_argument("--out", default="autoplay_report.json")
    args = ap.parse_args()
    base = args.url.rstrip("/")
    print(f"Autoplaying {args.turns} turns against {base} (strategy={args.strategy})\n")
    try:
        report = play(base, args.turns, args.strategy, args.turn_timeout)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\n[AUTOPLAY ERROR] {e}")
        sys.exit(1)
    with open(args.out, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport written to {args.out}")
    all_pass = all(report["verdict"].values())
    sys.exit(0 if all_pass else 2)


if __name__ == "__main__":
    main()
