"""The run's goal: one thing you can see from where you start, and walk to.

The whole design, in the order a run meets it:

  INVENT   at reset, before anything is drawn: a short NAME (the label on the
           picture), one line of WHY, and a LOOK (what it looks like from far
           away, for the image prompts). An authored Level goal is honoured —
           turned into a place if it was written as a situation — never
           replaced. Lives on the run (state), never in the authoring data.
  VISTA    the opening montage's widest shot and the first playable frame both
           carry the LOOK on the horizon (engine: establishing block).
  FIND     every settled frame the client shows is asked once "is it in
           this picture, and where" — one vision call, cached by file.
  LABEL    the client draws the item tag at the box it gets back.
  STEER    the consequence prompt is told to keep it in view, and after
           SIGHT_GAP turns without a sighting, to put it back.

Proven before it was wired in (_claude_goal_proof.py, 2026-09-21): four runs
on three Worlds, the goal found in 4/4 first frames and 4/4 frames after a
walk toward it, and correctly missing when the player stepped indoors.

Everything here is best-effort and never raises into the turn loop: with no
text model the run falls back to the authored goal or the first landmark,
and with no vision model the tag simply never shows.
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent

# Turns without a sighting before the next beat is told to bring it back.
SIGHT_GAP = int(os.getenv("GOAL_SIGHT_GAP", "2"))
NAME_MAX = 34
VISION_MODEL_DEFAULT = "gemini-3.1-flash-lite"

STATE_KEYS = ("goal_name", "goal_why", "goal_look")


# ─────────────────────────────────────────────────────────────────────────────
# The record on the run
# ─────────────────────────────────────────────────────────────────────────────

def record(state: Optional[dict]) -> Dict[str, str]:
    st = state if isinstance(state, dict) else {}
    rec = {k.split("_", 1)[1]: str(st.get(k) or "").strip() for k in STATE_KEYS}
    if not rec["name"]:
        # A run that started before this module existed (or a save carried
        # over from one) has a level_goal line and no record. It gets one on
        # its first sighting request (adopt) and keeps it for the process.
        got = _ADOPTED.get(str(st.get("level_goal") or "").strip())
        if got:
            rec = dict(got)
    return rec


# level_goal line -> record, for runs that never went through a reset here.
_ADOPTED: Dict[str, Dict[str, str]] = {}


def adopt(state: Optional[dict]) -> Dict[str, str]:
    """Give a goal-less run a record from its own level_goal line, once.

    Played in the app: a run begun before the merge kept going with its
    authored premise ("During a massive protest, the president has been
    kidnapped...") as level_goal and no name, so nothing was ever tagged.
    The model labels the line the same way a reset would; with no model the
    line's first clause is the name. Kept in memory, not written to state.json
    (the turn loop owns that file)."""
    st = state if isinstance(state, dict) else {}
    line = str(st.get("level_goal") or "").strip()
    if not line or str(st.get("goal_name") or "").strip():
        return record(st)
    if line in _ADOPTED:
        return dict(_ADOPTED[line])
    rec: Dict[str, str] = {}
    try:
        rec = invent(authored=line, world_prompt=str(st.get("world_prompt") or "")) or {}
    except Exception:
        rec = {}
    name = _clean_name(rec.get("name")) or name_from(line)
    if not name:
        return record(st)
    got = {"name": name, "why": str(rec.get("why") or "").strip(),
           "look": str(rec.get("look") or "").strip()}
    _ADOPTED[line] = got
    return dict(got)


def install(state: dict, rec: Dict[str, str], line: str = "") -> str:
    """Write the record onto the run. Returns the `level_goal` line, which every
    older consumer (directive, slate, narrator, encounter brief, montage) reads.

    A generated goal's line is the name, then what it looks like, so each of
    those consumers knows what to draw or mention without knowing this module
    exists. An authored goal passes its own words as ``line`` and they are used
    verbatim: the record only adds a label for the picture."""
    name = _clean_name(rec.get("name")) or name_from(line)
    look = str(rec.get("look") or "").strip()
    why = str(rec.get("why") or "").strip()
    if not name:
        return str(state.get("level_goal") or "")
    state["goal_name"] = name
    state["goal_why"] = why
    state["goal_look"] = look
    line = str(line or "").strip() or (f"{name} — {look}" if look else name)
    state["level_goal"] = line
    return line


def name_from(text: Any) -> str:
    """A label-length name out of a sentence, with no model: its first clause."""
    s = re.split(r"[.,;:(\u2014]| - ", str(text or ""), maxsplit=1)[0]
    return _clean_name(s)


def look_line(state: Optional[dict]) -> str:
    rec = record(state)
    if not rec["name"]:
        return ""
    return f"{rec['name']}: {rec['look']}" if rec["look"] else rec["name"]


def _clean_name(name: Any) -> str:
    s = re.sub(r"\s+", " ", str(name or "")).strip().strip(".\"'")
    if len(s) > NAME_MAX:
        s = s[:NAME_MAX].rsplit(" ", 1)[0]
    return s


# ─────────────────────────────────────────────────────────────────────────────
# INVENT
# ─────────────────────────────────────────────────────────────────────────────

def _parse_json(raw: str) -> dict:
    if not raw:
        return {}
    s = raw.strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.M).strip()
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else {}
    except Exception:
        m = re.search(r"\{.*\}", s, re.S)
        if m:
            try:
                d = json.loads(m.group(0))
                return d if isinstance(d, dict) else {}
            except Exception:
                return {}
    return {}


def invent(spec: Optional[dict] = None, *, lore: str = "", world_prompt: str = "",
           authored: str = "") -> Dict[str, str]:
    """Draft {name, why, look} for this run. {} when there is nothing to go on
    or the model fails; the caller falls back. Never writes anything."""
    import ai_provider_manager
    import game_identity

    setting = game_identity.authored_setting(spec) or {}
    known = "\n".join(p for p in (
        f"Place: {setting.get('name')}" if setting.get("name") else "",
        f"What it is: {setting.get('summary')}" if setting.get("summary") else "",
        f"Era: {setting.get('era')}" if setting.get("era") else "",
        f"Landmarks: {setting.get('landmarks')}" if setting.get("landmarks") else "",
        (f"THE AUTHOR WROTE THIS AS THE GOAL — honour it: {authored}" if authored else ""),
        (f"World notes: {str(lore).strip()[:1200]}" if lore else ""),
        (f"Current situation: {str(world_prompt).strip()[:700]}" if world_prompt else ""),
    ) if p).strip()
    if not known:
        return {}

    prompt = (
        "You are choosing the GOAL for one playthrough of a game level.\n\n"
        f"{known}\n\n"
        "The goal is ONE PLACE OR STRUCTURE the player can SEE from where they "
        "start, and has to travel to. Outdoors: something big standing on the "
        "horizon — a plant, a tower, a dam, a building, a wreck, a rig. Indoors: "
        "the thing at the far end of the space. If the author's goal is a "
        "situation rather than a place, make it the place where that situation "
        "is (a kidnapping becomes the building they are held in). If the author "
        "named a place, keep it.\n\n"
        "Rules for the words:\n"
        "- name: 2 or 3 words. It is printed on the picture as a label, like an "
        "item name in a game. Title Case. No articles.\n"
        "- why: ONE short sentence, under 14 words, said plainly about the place, "
        "not to the player. Never 'you must' or 'you need'. Use no proper nouns "
        "that are not written above.\n"
        "- look: ONE sentence describing exactly what it looks like from far "
        "away, for an image prompt: shape, material, light. No proper nouns.\n\n"
        'Reply with JSON only: {"name": "...", "why": "...", "look": "..."}'
    )
    try:
        raw = ai_provider_manager.chat(
            [{"role": "user", "content": prompt}], temperature=0.9, max_tokens=320)
    except Exception as e:
        print(f"[GOAL] invent failed: {e}", flush=True)
        return {}
    d = _parse_json(raw)
    name = _clean_name(d.get("name"))
    if not name or "signal interrupted" in str(raw).lower():
        print(f"[GOAL] invent returned nothing usable: {str(raw)[:120]!r}", flush=True)
        return {}
    why = re.sub(r"^(you (must|need to|have to)\s+)", "", str(d.get("why") or "").strip(), flags=re.I)
    why = why[:1].upper() + why[1:] if why else ""
    return {"name": name, "why": why, "look": str(d.get("look") or "").strip()}


# ─────────────────────────────────────────────────────────────────────────────
# FIND
# ─────────────────────────────────────────────────────────────────────────────

def locate_prompt(desc: str) -> str:
    """The question put to the vision model.

    The first version asked for "that specific thing, not something merely
    similar", and in the first real playtest it said "not in view" on two
    frames with the tower standing dead centre: every frame is REDRAWN from a
    description, so the goal never looks exactly like its description, and a
    strict judge rejects the very thing the game drew. So the question is the
    one a player asks: is the place I am going to in this picture?
    """
    return (
        "This frame is from a game. The player is travelling to this place:\n"
        f"  {desc}\n"
        "Every frame is redrawn from that description, so it will not match it "
        "exactly. Is that place VISIBLE in this frame: the building, structure "
        "or landmark that plays that role, usually the dominant one in the "
        "distance? If the place is something seen from outside (a building, a "
        "tower, a plant) and this frame is inside a room, corridor or vehicle, "
        "it counts only if it can be seen through a window, door or gap. A door, "
        "light or opening at the end of a corridor is not it unless the place "
        "itself is a door. Reply with JSON only: "
        '{"found": true|false, "box_2d": [ymin, xmin, ymax, xmax]} '
        "normalised 0-1000, boxing only that place; box_2d is [] when not found."
    )


def _gemini_key() -> str:
    key = os.getenv("GEMINI_API_KEY", "")
    if key:
        return key
    try:
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        if cfg.get("GEMINI_API_KEY"):
            return str(cfg["GEMINI_API_KEY"])
    except Exception:
        pass
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8", errors="ignore").splitlines():
            m = re.match(r"\s*GEMINI_API_KEY\s*=\s*['\"]?([^'\"\s]+)", line)
            if m:
                return m.group(1)
    except Exception:
        pass
    return ""


def locate(image_path: str, rec: Dict[str, str], prompt: str = "") -> Dict[str, Any]:
    """Is the goal in this picture, and where? {"found", "box": {x,y,w,h} 0..1}.
    One vision call. Returns found=False on any failure — a missing tag is the
    worst this can do."""
    miss = {"found": False, "box": None}
    if not rec.get("name") or not image_path or not os.path.exists(image_path):
        return miss
    try:
        import ai_provider_manager
        if ai_provider_manager.is_mock_active("vision"):
            return miss
    except Exception:
        pass
    key = _gemini_key()
    if not key:
        return miss
    import requests

    ext = os.path.splitext(image_path)[1].lower()
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(ext, "image/png")
    b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
    desc = rec["name"] + (f" — {rec['look']}" if rec.get("look") else "")
    prompt = prompt or locate_prompt(desc)
    model = os.getenv("GOAL_VISION_MODEL", VISION_MODEL_DEFAULT)
    body = {
        "contents": [{"parts": [{"inlineData": {"mimeType": mime, "data": b64}},
                                {"text": prompt}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 120,
                             "thinkingConfig": {"thinkingBudget": 0},
                             "responseMimeType": "application/json"},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    try:
        r = requests.post(url, headers={"x-goog-api-key": key,
                                        "Content-Type": "application/json"},
                          json=body, timeout=20)
        r.raise_for_status()
        raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        print(f"[GOAL] locate failed: {e}", flush=True)
        return miss
    d = _parse_json(raw)
    box = d.get("box_2d") or []
    if not d.get("found") or not isinstance(box, list) or len(box) != 4:
        return miss
    try:
        y0, x0, y1, x1 = [max(0.0, min(1000.0, float(v))) / 1000.0 for v in box]
    except Exception:
        return miss
    if x1 <= x0 or y1 <= y0:
        return miss
    return {"found": True, "box": {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}}


# ─────────────────────────────────────────────────────────────────────────────
# SIGHTINGS — kept off the run's state on purpose. A turn loads state at its
# start and saves it at its end; a sighting written into state.json between
# those two would be lost, or would overwrite the turn. So sightings live here,
# keyed by the run's goal line (unique per run: it is invented at reset).
# ─────────────────────────────────────────────────────────────────────────────

_LOCK = threading.Lock()
_BY_FILE: Dict[str, Dict[str, Any]] = {}      # abs path -> locate() result
_SEEN: Dict[str, Dict[str, int]] = {}         # level_goal -> {seen, checked}


def sighting(state: Optional[dict]) -> Dict[str, int]:
    goal = str((state or {}).get("level_goal") or "")
    with _LOCK:
        return dict(_SEEN.get(goal) or {})


def note(state: dict, found: bool) -> None:
    goal = str((state or {}).get("level_goal") or "")
    if not goal:
        return
    turn = int((state or {}).get("turn_count") or 0)
    with _LOCK:
        rec = _SEEN.setdefault(goal, {"seen": -1, "checked": -1})
        rec["checked"] = max(rec["checked"], turn)
        if found:
            rec["seen"] = max(rec["seen"], turn)


# ─────────────────────────────────────────────────────────────────────────────
# APPROACH — clicking the tag walks you there, in a few beats, and you arrive
# ─────────────────────────────────────────────────────────────────────────────
#
# Played: the tag was a label you could not act on, and "reached" waited on the
# consequence model deciding, unprompted, that a beat had arrived — which its
# own directive ("must not hand it over for free") told it never to do. So the
# run had a destination and no way to get there.
#
# Now every click on the tag (or its HUD name) is one step; the directive tells
# each beat how close they are, and on the last step that THIS beat is the one
# where they arrive. The first settled picture after that turn plays REACHED,
# whether or not the model raised goal_reached. Kept in memory, like _SEEN.

APPROACH_STEPS = int(os.getenv("GOAL_APPROACH_STEPS", "3"))
_APPROACH: Dict[str, Dict[str, int]] = {}     # level_goal -> {steps, last_turn}
_DONE: set = set()                            # level_goal lines the player walked into


def approach(state: dict, final: bool = False) -> Dict[str, int]:
    """One step toward the goal, taken on this turn. Returns {steps, of}.
    ``final``: the player clicked REACH (one step out, or the thing already
    fills the frame) — this turn is the arrival."""
    goal = str((state or {}).get("level_goal") or "")
    turn = int((state or {}).get("turn_count") or 0)
    with _LOCK:
        a = _APPROACH.setdefault(goal, {"steps": 0, "last_turn": -1})
        if final:
            a["steps"] = APPROACH_STEPS
            a["last_turn"] = turn
        elif a["last_turn"] != turn:        # one step per turn, however many clicks
            a["steps"] = min(APPROACH_STEPS, a["steps"] + 1)
            a["last_turn"] = turn
        return {"steps": a["steps"], "of": APPROACH_STEPS}


def progress(state: Optional[dict]) -> Dict[str, int]:
    goal = str((state or {}).get("level_goal") or "")
    with _LOCK:
        a = dict(_APPROACH.get(goal) or {"steps": 0, "last_turn": -1})
    return {"steps": a["steps"], "of": APPROACH_STEPS, "last_turn": a["last_turn"]}


def arrived(state: Optional[dict]) -> bool:
    """The arrival beat has been played: the last step was taken on an earlier
    turn than the one on screen now."""
    st = state if isinstance(state, dict) else {}
    if st.get("goal_reached_turn"):
        return True
    p = progress(st)
    return p["steps"] >= APPROACH_STEPS and int(st.get("turn_count") or 0) > p["last_turn"]


def sight_directive(state: Optional[dict]) -> str:
    """One more line for the consequence prompt: keep it in view, or put it back."""
    st = state if isinstance(state, dict) else {}
    rec = record(st)
    if not rec["name"] or arrived(st):
        return ""
    p = progress(st)
    turn_now = int(st.get("turn_count") or 0)
    what_ = rec["name"] + (f" ({rec['look']})" if rec["look"] else "")
    if p["steps"] >= APPROACH_STEPS and p["last_turn"] == turn_now:
        return (
            f"THE PLAYER ARRIVES THIS BEAT. They have walked to {what_} and this "
            "beat is the one where they reach it: they stand at it, at its door, "
            "or step inside. visual_scene shows them THERE, the place filling "
            "the frame. goal_reached is TRUE for this beat.\n"
        )
    if p["steps"] == APPROACH_STEPS - 1 and p["last_turn"] == turn_now:
        return (
            f"THE PLAYER IS NOW RIGHT AT {rec['name'].upper()}. This beat brings "
            f"them to its threshold: {what_} fills the upper frame, close enough "
            "to touch, its way in (door, gate, hatch, opening) plainly in front "
            "of them and lit. They have not gone in yet.\n"
        )
    if p["steps"] > 0 and p["last_turn"] == turn_now:
        return (
            f"THE PLAYER IS HEADING FOR {rec['name'].upper()} (step {p['steps']} of "
            f"{APPROACH_STEPS}). This beat moves them plainly closer: {what_} is "
            "bigger and nearer in visual_scene than in the last frame, still "
            "ahead of them. Something on the way may slow them; nothing stops them.\n"
        )
    turn = int(st.get("turn_count") or 0)
    s = sighting(st)
    seen = s.get("seen", -1)
    gap = turn - seen if seen >= 0 else turn
    what = rec["name"] + (f" ({rec['look']})" if rec["look"] else "")
    if turn >= SIGHT_GAP and gap >= SIGHT_GAP:
        return (
            f"THE GOAL HAS BEEN OUT OF SIGHT FOR {gap} TURNS. This beat's "
            f"visual_scene MUST show {what} again, far off and unreached — at "
            "the end of a street, over a roofline, through a window or a gap, on "
            "the horizon. Do not move the player to do it; turn the view.\n"
        )
    return (
        f"KEEP IT IN VIEW: wherever the place allows, visual_scene keeps {what} "
        "visible in the distance ahead, so the player can always see where they "
        "are going.\n"
    )


def _reached_line(sid: str) -> str:
    """The first sentence of the beat that got them there, for the REACHED card."""
    try:
        import engine
        for entry in reversed(engine._load_history(sid) or []):
            text = str((entry or {}).get("dispatch") or "").strip()
            if text and not str((entry or {}).get("choice") or "").startswith("__"):
                first = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
                return first[:200]
    except Exception:
        pass
    return ""


def api_goal_sight():
    """POST {src} -> {goal, name, why, found, box, reached}. Called by the client
    once per settled picture; cached by file, so repeats are free."""
    from flask import jsonify, request
    import engine

    try:
        sid = engine._resolve_request_session_id()
        st = engine.get_state(sid) or {}
        rec = record(st)
        if not rec["name"]:
            rec = adopt(st)
        if not rec["name"]:
            return jsonify({"ok": True, "goal": False})
        body = request.get_json(silent=True) or {}
        src = str(body.get("src") or "")
        if body.get("complete"):
            # Arrived, and the player clicked the glowing goal: that click is
            # the finish, not the beat before it.
            with _LOCK:
                _DONE.add(str(st.get("level_goal") or ""))
            print(f"[GOAL] completed {rec['name']}", flush=True)
            return jsonify({"ok": True, "goal": True, "name": rec["name"],
                            "completed": True, "reached_line": _reached_line(sid)})
        if body.get("approach"):
            step = approach(st, final=bool(body.get("final")))
            print(f"[GOAL] approach {rec['name']}: step {step['steps']}/{step['of']} "
                  f"on turn {st.get('turn_count')}", flush=True)
            return jsonify({"ok": True, "goal": True, "name": rec["name"], **step})
        p = progress(st)
        out = {"ok": True, "goal": True, "name": rec["name"], "why": rec["why"],
               "reached": arrived(st), "steps": p["steps"], "of": p["of"],
               "completed": str(st.get("level_goal") or "") in _DONE,
               "found": False, "box": None, "src": src}
        if out["reached"]:
            out["reached_line"] = _reached_line(sid)
        if not src:
            return jsonify(out)
        path = engine._resolve_image_path(src, sid)
        path = str(path) if path else ""
        if not path or not os.path.exists(path):
            return jsonify(out)
        cache_key = os.path.normcase(os.path.abspath(path)) + "|" + rec["name"]
        with _LOCK:
            hit = _BY_FILE.get(cache_key)
        if hit is None:
            hit = locate(path, rec)
            with _LOCK:
                _BY_FILE[cache_key] = hit
                if len(_BY_FILE) > 400:
                    for k in list(_BY_FILE)[:200]:
                        _BY_FILE.pop(k, None)
            print(f"[GOAL] sight {os.path.basename(path)}: "
                  f"{'FOUND' if hit['found'] else 'not in view'} "
                  f"{hit.get('box') or ''}", flush=True)
        note(st, bool(hit["found"]))
        out.update(found=bool(hit["found"]), box=hit.get("box"))
        return jsonify(out)
    except Exception as e:
        print(f"[GOAL] sight endpoint failed: {e}", flush=True)
        return jsonify({"ok": False, "goal": False})
