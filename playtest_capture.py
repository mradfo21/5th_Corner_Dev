#!/usr/bin/env python3
"""Capture a full playtest session with per-turn feed snapshots for analysis."""
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "playtest_results"


def req(method, url, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode()), time.time()


def get_json(base, path, timeout=30):
    data, _ = req("GET", base + path, timeout=timeout)
    return data


def post_json(base, path, body, timeout=120):
    data, t = req("POST", base + path, body, timeout=timeout)
    return data, t


def latest_prompt(items):
    for it in reversed(items):
        if it.get("type") == "player_choice_prompt":
            return it
    return None


def wait_for_turn(base, since_id, timeout_s=90):
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
        time.sleep(0.5)
    return get_json(base, f"/api/feed?since_id={since_id}"), time.time() - start, "timeout"


def latest_image_url(items):
    for i in reversed(items or []):
        if i.get("image_url"):
            return i["image_url"]
    return None


def wait_for_scene_image(base, since_id, items, grace_s=25, prev_url=None):
    """Scene rendering runs off the turn's critical path, so the prompt is
    served before the picture lands. Capturing at the prompt recorded a
    scene image that was still being painted as absent.

    The prompt carries the CURRENT scene image, which until the new frame
    lands is the PREVIOUS turn's picture — so waiting for "any image_url"
    returned instantly and recorded a stale frame as this turn's art. Wait
    for a url that actually differs from the one we came in with."""
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


def capture_session(base: str, turns: int, strategy: str = "mixed") -> dict:
    session = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "base": base,
        "strategy": strategy,
        "turns_requested": turns,
        "reset": None,
        "turns": [],
        "full_feed_tail": None,
    }

    images_on = bool(get_json(base, "/api/status").get("image_enabled"))
    session["images_enabled"] = images_on

    reset_items, reset_elapsed = post_json(base, "/api/reset", {})
    intro = latest_prompt(reset_items)
    if images_on:
        reset_items = wait_for_scene_image(base, 0, reset_items)
    session["reset"] = {
        "elapsed_s": round(reset_elapsed, 2),
        "feed_items": reset_items,
        "intro_choices": [c.get("text") for c in (intro.get("choices") or [])],
        "narratives": [i.get("content") for i in reset_items if i.get("type") == "narrative_event"],
        "item_types": [i.get("type") for i in reset_items],
    }

    last_id = max((i.get("id", 0) for i in reset_items), default=0)
    current_prompt = intro
    prev_choices = session["reset"]["intro_choices"]
    prev_image_url = latest_image_url(reset_items)

    for n in range(turns):
        choices = [c.get("text") for c in (current_prompt.get("choices") or [])]
        if strategy == "custom" or (strategy == "mixed" and n % 3 == 2):
            choice = "Raise the camcorder and film the fence line, hands trembling"
            picked_label = f"[custom] {choice}"
        else:
            idx = n % max(1, len(choices))
            choice = choices[idx] if choices else "Look around"
            picked_label = choice

        since = last_id
        choose_resp, choose_elapsed = post_json(base, "/api/choose", {
            "choice": choice,
            "context_item_id": last_id,
        })
        items, wait_elapsed, status = wait_for_turn(base, since)
        if images_on and status == "resolved":
            items = wait_for_scene_image(base, since, items, prev_url=prev_image_url)

        new_prompt = latest_prompt(items)
        new_choices = [c.get("text") for c in (new_prompt.get("choices") or [])]
        narratives = [i.get("content") for i in items if i.get("type") == "narrative_event"]
        # The turn's picture usually rides on the choice prompt itself; a
        # standalone `scene_image` item is only one of the ways it arrives.
        # Looking only for that type recorded turns that DID have art as
        # imageless.
        scene = next((i for i in items if i.get("image_url")), None)
        status_item = get_json(base, "/api/status")

        turn_record = {
            "turn": n + 1,
            "picked": picked_label,
            "choose_elapsed_s": round(choose_elapsed, 2),
            "wait_elapsed_s": round(wait_elapsed, 2),
            "status": status,
            "narratives": narratives,
            "narrative_combined": " ".join(narratives),
            "prev_choices": prev_choices,
            "new_choices": new_choices,
            "choices_changed": bool(new_choices) and new_choices != prev_choices,
            "scene_image_url": (scene or {}).get("image_url"),
            "scene_image_is_new": bool(scene) and scene.get("image_url") != prev_image_url,
            "feed_item_types": [i.get("type") for i in items],
            "feed_items": items,
            "game_status": status_item,
            "choose_response": choose_resp,
        }
        session["turns"].append(turn_record)

        if status in ("death", "timeout"):
            break

        prev_choices = new_choices
        current_prompt = new_prompt
        last_id = max((i.get("id", last_id) for i in items), default=last_id)
        prev_image_url = (scene or {}).get("image_url") or prev_image_url

    session["full_feed_tail"] = get_json(base, "/api/feed")
    return session


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5001"
    turns = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = OUT_DIR / f"full_capture_{stamp}.json"
    session = capture_session(base.rstrip("/"), turns, "mixed")
    out_path.write_text(json.dumps(session, indent=2), encoding="utf-8")
    print(f"Captured {len(session['turns'])} turns -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
