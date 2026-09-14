"""Who decides what happens next when nobody is holding the controller.

Watch advanced by clicking a random button, so a coin toss picked the dull
option as often as the good one, and the run spent its evening walking to
things and looking at them. This asks the model which option on screen makes
the best next minute, told what the run has already spent its last few turns
doing so it stops choosing the same shape of action over and over.

Never raises and never blocks a turn: every failure path returns a pick.
"""

from __future__ import annotations

import random
import re
from typing import Any, Dict, List, Optional

# How many previous actions the director is shown. Enough to notice it has
# taken the same kind of beat three times; short enough that a run is not
# forbidden from ever repeating itself.
RECENT_ACTIONS = 5

PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "index": {"type": "integer"},
        "why": {"type": "string"},
    },
    "required": ["index", "why"],
}

DEFAULT_DIRECTOR_INSTRUCTIONS = (
    "You are directing an episode. Nobody is playing — a camera is running "
    "and you decide what the character does next, so pick the option that "
    "makes the best next minute of television.\n"
    "\n"
    "Prefer the choice that COMMITS to something: one that changes the "
    "situation, forces a reaction, reveals something, or costs the "
    "character something. A choice with a consequence beats a choice with "
    "a view.\n"
    "Reject the option that only looks, waits, scans, approaches, or moves "
    "toward a thing without touching it. Watching a character consider a "
    "door is not television.\n"
    "Vary the kind of beat. If the last few turns were all violence, the "
    "interesting move is the one that is not another punch. If nothing has "
    "happened for a while, escalate.\n"
    "\n"
    "Answer with the index of your pick and one short clause saying what it "
    "buys the episode. Only the indices offered below are valid."
)

# Verbs that produce a frame of somebody standing still, thinking about it.
_PASSIVE = (
    "look", "watch", "observe", "scan", "study", "examine", "inspect",
    "wait", "listen", "consider", "survey", "check", "peer", "glance",
    "assess", "read", "approach", "walk to", "head for", "move to",
    "move toward", "step toward", "continue", "proceed", "follow the",
)

# What KIND of beat an action is. Asking the model to vary itself does not
# work — told "do not repeat yourself" it picks the most violent option every
# single time and explains, every single time, that it forces a
# confrontation. So the variety is enforced here instead: a run that has
# thrown three punches is not offered a fourth while another shape of answer
# is on the slate.
_SHAPES = (
    ("force", ("hit", "punch", "strike", "smash", "kick", "shove", "slam",
               "grab", "choke", "stab", "swing", "tackle", "attack", "crack",
               "break", "force", "pry", "throw", "drag", "shoot", "cut down",
               "wrench", "bash", "ram", "knock")),
    ("flight", ("run", "flee", "bolt", "sprint", "escape", "retreat", "hide",
                "duck", "dive", "vault", "scramble", "back away", "back off",
                "get out", "break away", "slip away")),
    ("talk", ("say", "tell", "ask", "shout", "yell", "call", "speak", "warn",
              "answer", "offer", "plead", "bargain", "explain", "promise",
              "greet", "whisper", "hand over", "give", "surrender", "trade")),
    ("handle", ("open", "pull", "push", "turn", "light", "set off", "cut",
                "tie", "take", "pick up", "pocket", "load", "start", "switch",
                "unlock", "climb", "dig", "burn", "plant", "drop")),
    ("observe", _PASSIVE),
)


def _texts(choices: Any) -> List[str]:
    out = []
    for c in (choices or []):
        if isinstance(c, dict):
            text = str(c.get("text") or "").strip()
        else:
            text = str(c or "").strip()
        out.append(text)
    return out


def _is_passive(text: str) -> bool:
    low = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    return any(low.startswith(p) or f" {p}" in low for p in _PASSIVE)


def _head_verb(text: str) -> str:
    """The first real word, as a crude 'kind of action' key."""
    words = re.findall(r"[a-z']+", str(text or "").lower())
    return words[0] if words else ""


def shape(text: str) -> str:
    """Which kind of beat this is — violence, flight, talk, handling, watching."""
    low = " " + re.sub(r"[^a-z ]+", " ", str(text or "").lower()) + " "
    low = re.sub(r"\s+", " ", low)
    for name, verbs in _SHAPES:
        if any(f" {v} " in low or low.startswith(f" {v} ") for v in verbs):
            return name
    return "other"


def unstuck_indices(texts: List[str], recent: List[str]) -> List[int]:
    """Which options are still worth offering, given what the run keeps doing.

    Only narrows when the run is genuinely in a rut — the last three beats all
    the same kind — and only if something different is actually on the slate.
    """
    live = [i for i, t in enumerate(texts) if t]
    if len(recent) < 3:
        return live
    rut = {shape(t) for t in recent[-3:]}
    if len(rut) != 1 or "other" in rut:
        return live
    fresh = [i for i in live if shape(texts[i]) not in rut]
    return fresh or live


def recent_actions(session_id: str = "default", n: int = RECENT_ACTIONS) -> List[str]:
    """What this run has been doing lately, newest last."""
    try:
        import engine
        hist = engine._load_history(session_id) or []
    except Exception:
        return []
    out = []
    for entry in hist[-n:]:
        if not isinstance(entry, dict):
            continue
        choice = str(entry.get("choice") or "").strip()
        if choice and choice.lower() not in ("intro", "initialize simulation"):
            out.append(choice)
    return out


def _scene(session_id: str = "default") -> str:
    """What is on screen, so the pick is about this frame and not the genre."""
    try:
        import engine
        hist = engine._load_history(session_id) or []
    except Exception:
        return ""
    for entry in reversed(hist):
        if not isinstance(entry, dict):
            continue
        seen = str(entry.get("vision_analysis") or "").strip()
        if seen:
            return seen[:500]
    return ""


def fallback_pick(texts: List[str], recent: Optional[List[str]] = None,
                  allowed: Optional[List[int]] = None) -> int:
    """A pick for when the model is unavailable — still better than a coin toss.

    Skips the options that only look at things, and leans away from repeating
    the verb the run just used, which is what made Watch feel like it was
    stuck on one idea.
    """
    if not texts:
        return -1
    used = {_head_verb(t) for t in (recent or [])}
    offer = allowed if allowed else [i for i, t in enumerate(texts) if t]
    live = [i for i in offer if texts[i] and not _is_passive(texts[i])]
    pool = [i for i in live if _head_verb(texts[i]) not in used] or live
    if not pool:
        pool = offer or [i for i, t in enumerate(texts) if t] or list(range(len(texts)))
    return random.choice(pool)


def pick(choices: Any, session_id: str = "default",
         scene: str = "") -> Dict[str, Any]:
    """Choose the most watchable option. Always returns a usable index."""
    texts = _texts(choices)
    live = [t for t in texts if t]
    if not live:
        return {"index": -1, "why": "", "source": "empty"}
    recent = recent_actions(session_id)
    if len(live) == 1:
        return {"index": texts.index(live[0]), "why": "only option",
                "source": "single"}

    instructions = DEFAULT_DIRECTOR_INSTRUCTIONS
    try:
        import engine
        authored = str((getattr(engine, "PROMPTS", {}) or {}).get(
            "director_instructions") or "")
        if authored.strip():
            instructions = authored.strip()
    except Exception:
        pass

    allowed = unstuck_indices(texts, recent)
    seen = (scene or _scene(session_id)).strip()
    bits = [instructions, ""]
    if seen:
        bits.append(f"ON SCREEN RIGHT NOW: {seen}")
    if recent:
        bits.append("ALREADY DONE THIS RUN (newest last): "
                    + "; ".join(f"{t} [{shape(t)}]" for t in recent))
    bits.append("")
    bits.append("THE OPTIONS:")
    for i in allowed:
        bits.append(f"  {i}. {texts[i]}")
    bits.append("")
    bits.append("Answer with one of these indices: "
                + ", ".join(str(i) for i in allowed))
    prompt = "\n".join(bits)

    try:
        import engine
        raw = engine._ask(prompt, model="gemini", temp=0.6, tokens=80,
                          use_lore=False, response_schema=PICK_SCHEMA)
        data = raw
        if isinstance(raw, str):
            import json
            blob = raw.strip()
            if blob.startswith("```"):
                blob = re.sub(r"^```(?:json)?\s*", "", blob)
                blob = re.sub(r"\s*```$", "", blob)
            data = json.loads(blob)
        idx = int((data or {}).get("index"))
        why = str((data or {}).get("why") or "").strip()[:120]
        if idx in allowed:
            return {"index": idx, "why": why, "source": "llm"}
    except Exception as e:
        try:
            import engine
            engine.log_error(f"[DIRECTOR] pick failed: {e}")
        except Exception:
            pass

    return {"index": fallback_pick(texts, recent, allowed), "why": "",
            "source": "fallback"}
