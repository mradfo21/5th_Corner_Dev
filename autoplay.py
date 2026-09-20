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


def clear_any_cutscene(base, items, timeout=300):
    """Play out an opening cutscene the way the browser does, or nothing moves.

    An authored opening (or the level's approach montage) parks the choice
    slate and sets `experience_cutscene_id`, and `/api/choose` answers 409
    `cutscene_playing` while it is set. The browser plays the shots and then
    POSTs /api/cutscene/complete, which installs the opening establishing beat
    and releases the slate. No headless driver here ever called it.

    This file hid that rather than failing on it: with no intro prompt,
    `choice_texts(None)` is `[]`, the loop falls through to its canned FALLBACK
    action, and every run reported "functional: every turn resolves" while
    having never once been offered a generated slate. Four runs in the full
    harness all opened on the hardcoded "Look around" for that reason.

    Returns the feed after the cutscene is done.
    """
    if not any((i or {}).get("type") == "cutscene" for i in (items or [])):
        return items
    print("[autoplay] opening cutscene — completing it as the client would")
    try:
        post_json(base, "/api/cutscene/complete", {}, timeout=timeout)
    except Exception as e:
        print(f"[autoplay] cutscene complete failed: {e}")
    return get_json(base, "/api/feed?since_id=0")


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


def backend_capabilities(base):
    """What this backend can actually be held to.

    The offline mock backend generates no images and no model text — it
    answers every turn from canned fallbacks. Scoring it on picture loads
    or on whether the prose is real AI writing measures the harness, not
    the game, and a gate that always fails is a gate nobody reads. The
    mechanical checks (does the turn resolve, is it fast, is the slate
    full) still apply to both.
    """
    try:
        status = get_json(base, "/api/status")
        return {
            "images": bool(status.get("image_enabled")),
            "llm": status.get("backend") != "mock",
        }
    except Exception:
        return {"images": True, "llm": True}


def latest_image_url(items):
    for i in reversed(items or []):
        if i.get("image_url"):
            return i["image_url"]
    return None


def wait_for_scene_image(base, since_id, items, grace_s, prev_url=None):
    """Keep polling for this turn's scene image after the turn resolved.

    Scene rendering runs on its own thread and is deliberately NOT on the
    turn's critical path — the prompt is served the moment the narrative is
    ready, and the image lands in the feed a beat later. Stopping at the
    prompt therefore scored a picture that was still being painted as
    missing. Returns the (possibly extended) item list.

    `prev_url` is what makes the wait meaningful: the choice prompt carries
    the CURRENT scene image, which until the new frame lands is still the
    PREVIOUS turn's picture. Accepting any image_url therefore let a turn
    that rendered nothing score as "image ok" against a stale frame, so hold
    out for a url that differs from the one we came in with.
    """
    if latest_image_url(items) not in (None, prev_url):
        return items
    deadline = time.time() + max(0, grace_s)
    while time.time() < deadline:
        time.sleep(1.0)
        fresh = get_json(base, f"/api/feed?since_id={since_id}")
        if latest_image_url(fresh) not in (None, prev_url):
            return fresh
        items = fresh or items
    return items


def play(base, turns, strategy, turn_timeout, image_grace=25):
    report = {"base": base, "turns": [], "started": time.time()}
    caps = backend_capabilities(base)
    report["images_enabled"] = caps["images"]
    report["llm_enabled"] = caps["llm"]
    if not caps["llm"]:
        print("[autoplay] offline mock backend — narrative/choice-quality checks reported, not scored.")
    elif not caps["images"]:
        print("[autoplay] image generation OFF — image checks reported, not scored.")

    reset_items, reset_dt = post_json(base, "/api/reset", {})
    reset_items = clear_any_cutscene(base, reset_items)
    intro_prompt = latest_prompt(reset_items)
    if report["images_enabled"]:
        reset_items = wait_for_scene_image(base, 0, reset_items, image_grace)
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
    prev_img_url = latest_image_url(reset_items)

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
        if report["images_enabled"] and status == "resolved":
            items = wait_for_scene_image(base, since, items, image_grace, prev_img_url)

        narr = " ".join(i.get("content", "") for i in items if i.get("type") == "narrative_event")
        scene = next((i for i in items if i.get("type") == "scene_image"), None)
        img_url = (scene or {}).get("image_url") or latest_image_url(items)
        img_ok, img_status, img_bytes, _ = check_image(base, img_url) if img_url else (False, "none", 0, "")
        # A turn that reuses the previous frame is not a turn that rendered.
        img_is_new = bool(img_url) and img_url != prev_img_url
        new_prompt = latest_prompt(items)
        new_choices = choice_texts(new_prompt)
        regenerated = bool(new_choices) and new_choices != prev_choices
        # A degraded turn is a masked failure: the player sees an in-world
        # camcorder glitch, so the text no longer LOOKS like an error and
        # is_fallback_text can't spot it. The server flags it explicitly —
        # without this, a run where every LLM call failed would report 100%
        # real narrative.
        degraded_turn = any(
            (i.get("metadata") or {}).get("degraded")
            for i in items if i.get("type") == "narrative_event"
        )
        real_text = bool(narr) and not is_fallback_text(narr) and not degraded_turn
        fallback_choices = bool(new_choices) and all(c.lower() in KNOWN_FALLBACKS for c in new_choices)

        turn = {
            "n": n + 1, "picked": picked, "status": status, "elapsed": round(elapsed, 1),
            "narrative": narr[:200], "narrative_real": real_text,
            "image_present": bool(img_url), "image_loads": img_ok, "image_status": img_status, "image_bytes": img_bytes,
            "image_is_new": img_is_new,
            "new_choices": new_choices, "choices_regenerated": regenerated,
            "choices_are_fallback": fallback_choices,
        }
        report["turns"].append(turn)
        print(f"[turn {n+1}] {status} {elapsed:.1f}s  pick={picked[:40]!r}\n"
              f"         img={'ok' if img_ok else 'FAIL('+str(img_status)+')'}"
              f"{'' if img_is_new else '(stale)'}  "
              f"real_text={real_text}  regen_choices={regenerated}  fallback_choices={fallback_choices}\n"
              f"         narrative={narr[:110]!r}\n"
              f"         choices={new_choices}")

        if status in ("death", "timeout"):
            if status == "death":
                # restart to keep exercising the loop
                reset_items, _ = post_json(base, "/api/reset", {})
                reset_items = clear_any_cutscene(base, reset_items)
                current_prompt = latest_prompt(reset_items)
                last_id = max((i.get("id", 0) for i in reset_items), default=0)
                prev_choices = choice_texts(current_prompt)
                prev_img_url = latest_image_url(reset_items)
                continue
            else:
                break

        prev_choices = new_choices
        current_prompt = new_prompt
        last_id = max((i.get("id", last_id) for i in items), default=last_id)
        prev_img_url = img_url or prev_img_url

    return summarize(report)


EXPECTED_CHOICES = 3


def summarize(report):
    turns = report["turns"]
    skipped = []
    resolved = [t for t in turns if t["status"] in ("resolved", "death")]
    times = [t["elapsed"] for t in turns if t["status"] != "timeout"]
    report["summary"] = {
        "turns_played": len(turns),
        "turns_resolved": len(resolved),
        "turns_timed_out": sum(1 for t in turns if t["status"] == "timeout"),
        "avg_turn_s": round(sum(times) / len(times), 1) if times else None,
        "max_turn_s": round(max(times), 1) if times else None,
        "image_load_rate": round(sum(1 for t in turns if t["image_loads"]) / len(turns), 2) if turns else 0,
        # Distinct from image_load_rate: a turn can serve a perfectly loadable
        # picture that is simply the previous turn's frame, which means the
        # scene never re-rendered.
        "fresh_image_rate": round(sum(1 for t in turns if t.get("image_is_new")) / len(turns), 2) if turns else 0,
        "real_text_rate": round(sum(1 for t in turns if t["narrative_real"]) / len(turns), 2) if turns else 0,
        "choice_regen_rate": round(sum(1 for t in turns if t["choices_regenerated"]) / len(turns), 2) if turns else 0,
        "fallback_choice_rate": round(sum(1 for t in turns if t["choices_are_fallback"]) / len(turns), 2) if turns else 0,
        # A slate that comes back short is a filtered-away choice, not a
        # design decision — the UI lays out EXPECTED_CHOICES buttons.
        "short_slate_turns": sum(
            1 for t in turns
            if t["status"] == "resolved" and len(t["new_choices"]) < EXPECTED_CHOICES
        ),
    }
    s = report["summary"]
    verdict = []
    verdict.append(("functional: every turn resolves", s["turns_resolved"] == s["turns_played"] and s["turns_played"] > 0))
    verdict.append(("fast: avg turn < 20s", (s["avg_turn_s"] or 999) < 20))
    # A backend with image generation switched off (mock/offline) can't fail a
    # check about pictures; scoring it there just buries the real failures.
    if report.get("images_enabled", True):
        verdict.append(("images load on turns", s["image_load_rate"] >= 0.8))
        verdict.append(("turns render a NEW scene image", s["fresh_image_rate"] >= 0.8))
    else:
        skipped.append("images load on turns (image generation disabled)")
        skipped.append("turns render a NEW scene image (image generation disabled)")
    if report.get("llm_enabled", True):
        verdict.append(("narrative is real AI text", s["real_text_rate"] >= 0.8))
        verdict.append(("choices regenerate", s["choice_regen_rate"] >= 0.6))
    else:
        skipped.append("narrative is real AI text (offline mock backend)")
        skipped.append("choices regenerate (offline mock backend)")
    verdict.append(("every turn offers a full slate of choices", s["short_slate_turns"] == 0))
    report["verdict"] = {name: ok for name, ok in verdict}
    report["skipped"] = skipped

    print("\n==================== SUMMARY ====================")
    for k, v in s.items():
        print(f"  {k}: {v}")
    print("---------------------- VERDICT ------------------")
    for name, ok in verdict:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    for name in skipped:
        print(f"  [SKIP] {name}")
    print("=================================================")
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:5096")
    ap.add_argument("--turns", type=int, default=6)
    ap.add_argument("--strategy", choices=["first", "cycle", "mixed", "custom"], default="mixed")
    ap.add_argument("--turn-timeout", type=int, default=90)
    ap.add_argument("--image-grace", type=int, default=25,
                    help="Seconds to keep waiting for a turn's scene image after the turn resolves "
                         "(rendering is off the turn's critical path).")
    ap.add_argument("--out", default="autoplay_report.json")
    args = ap.parse_args()
    base = args.url.rstrip("/")
    print(f"Autoplaying {args.turns} turns against {base} (strategy={args.strategy})\n")
    try:
        report = play(base, args.turns, args.strategy, args.turn_timeout, args.image_grace)
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
