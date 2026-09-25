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
import random
import re
from urllib.parse import quote
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent

# Turns without a sighting before the next beat is told to bring it back.
SIGHT_GAP = int(os.getenv("GOAL_SIGHT_GAP", "2"))
NAME_MAX = 34
VISION_MODEL_DEFAULT = "gemini-3.1-flash-lite"

STATE_KEYS = ("goal_name", "goal_why", "goal_look")
# THE SPINE. name/why/look is a waypoint: a noun with a reason. Three goals
# in a row came back "Reinforced Blast Door | Data Siphon Hub | Atmospheric
# Cooling Spire", each with a key inside, because a waypoint is all it could
# be. What makes a place worth walking to is what the player does NOT know
# about it (truth), who else wants it and why (claim), and what getting
# there takes (cost). Drafted with the goal; spent on the approach, on the
# boss, and on the reward that pays the mystery off.
SPINE_KEYS = ("goal_truth", "goal_claim", "goal_cost")

# A newline, for building prompt blocks without fighting the escaping.
NL = chr(10)
# WHAT IS ACTUALLY IN THERE. A place you walk into and then leave is an errand;
# the run wants a thing to come out with. It is drafted with the goal, the
# reward cutscene ENDS on a frame of it with the player facing it, and taking
# it — an interaction with an object in the room, not a card — is what
# completes the goal: "the payoff needs to be interacting with an object in
# the new scene, which completes the goal" (2026-09-22).
PRIZE_KEYS = ("goal_prize", "goal_prize_look", "goal_prize_verb")

# ── THE GEAR THE RUN HANDS OUT ──────────────────────────────────────────────
#
# The look book designs six pieces of gear for every world (look_book.items):
# a weapon, armour, an upgrade, and the rest. A goal's prize is DRAWN from
# that table rather than invented for the goal, so what is waiting inside is
# scarce, belongs to this world, and already has a picture shot on the props
# sheet. The place is then drafted around the thing — "where the Sump Cutter
# is" is a better reason for a place to exist than an object made up to suit
# a place that was made up first.
#
# Falls back to the invented prize (PRIZE_RULES) whenever the book has no gear:
# an older run, a book that failed, a world built before any of this existed.

GEAR_KEYS = ("goal_prize_kind", "goal_prize_power", "goal_prize_plate")


def held(state: Optional[dict]) -> List[dict]:
    """The gear this run has carried out, newest last."""
    st = state if isinstance(state, dict) else {}
    return [g for g in (st.get("gear") or []) if isinstance(g, dict) and g.get("name")]


def draw_gear(state: Optional[dict], session_id: str = "default") -> Dict[str, str]:
    """One piece of this world's gear that the run has not taken yet.

    Random, so two runs of the same world are not the same run — and without
    replacement, so the third goal cannot hand back the first goal's weapon.
    {} when the book has nothing, and the caller invents instead."""
    st = state if isinstance(state, dict) else {}
    try:
        import look_book
        table = look_book.items(session_id) or []
    except Exception as e:
        print(f"[GOAL] no gear table ({e})", flush=True)
        return {}
    if not table:
        return {}
    taken = _spoken_for(st)
    left = [it for it in table if _norm_name(it.get("name")) not in taken]
    if not left:
        print("[GOAL] every piece of gear in this world has been taken", flush=True)
        return {}
    # A goal holds a TREASURE. The spoils are what a beaten hostile drops
    # (award_spoil); a goal only ever falls back to one when the treasures
    # are gone, which a three-goal run cannot reach on a nine-piece table.
    best = [it for it in left if str(it.get("tier") or "treasure") == "treasure"]
    pick = random.choice(best or left)
    print(f"[GOAL] this one holds the {pick.get('kind') or 'gear'}: "
          f"{pick['name']} — {pick.get('power') or pick.get('use') or ''}", flush=True)
    return dict(pick)


def _norm_name(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _spoken_for(st: dict) -> set:
    """Every piece of gear this run has already had, or is walking toward."""
    taken = {_norm_name(g.get("name")) for g in held(st)}
    taken |= {_norm_name(n) for n in (st.get("goals_taken") or [])}
    if st.get("goal_prize"):
        taken.add(_norm_name(st.get("goal_prize")))
    taken.discard("")
    return taken


def _table(session_id: str) -> List[dict]:
    try:
        import look_book
        return look_book.items(session_id) or []
    except Exception:
        return []


def _row_for(name: Any, session_id: str) -> Dict[str, Any]:
    """The look book's whole row for a piece of gear, by name: what the
    prize fields on the run do not carry (worth, use, tier, the flat ref)."""
    want = _norm_name(name)
    for it in _table(session_id):
        if want and _norm_name(it.get("name")) == want:
            return dict(it)
    return {}


# ── SPOILS ──────────────────────────────────────────────────────────────────
#
# Winning a fight pays. Played 2026-09-22: "introduce a loot award for winning
# at an encounter". Putting someone down, or talking them down, hands the
# player one piece of this world's gear — a SPOIL first (the look book designs
# three: what its hostiles carry), then any TREASURE the remaining goals will
# not need. The goals' own prizes are never spent this way: a treasure is only
# a spoil when there are more of them left than goals left to hold them.

def award_spoil(state: dict, session_id: str = "default", foe: str = "",
                how: str = "") -> Dict[str, Any]:
    """Put one piece of gear off a beaten hostile into the pack. {} when the
    world has nothing left to drop."""
    st = state if isinstance(state, dict) else {}
    table = _table(session_id)
    if not table:
        return {}
    gone = _spoken_for(st)
    left = [it for it in table if _norm_name(it.get("name")) not in gone]
    spoils = [it for it in left if str(it.get("tier") or "") == "spoil"]
    treasures = [it for it in left if str(it.get("tier") or "treasure") == "treasure"]
    done = len([n for n in (st.get("goals_done") or []) if str(n).strip()])
    # the goal on now already has its prize drawn (it is in _spoken_for);
    # the ones after it will each need a treasure.
    ahead = max(0, GOALS_TO_WIN - done - (1 if st.get("goal_prize") else 0))
    spare = treasures[:max(0, len(treasures) - ahead)] if len(treasures) > ahead else []
    pool = spoils or spare
    if not pool:
        print(f"[GOAL] {foe or 'they'} had nothing on them this world has not "
              f"already handed out", flush=True)
        return {}
    pick = dict(random.choice(pool))
    got = {"name": str(pick.get("name") or "")[:40], "kind": str(pick.get("kind") or "relic"),
           "tier": str(pick.get("tier") or "spoil"),
           "power": str(pick.get("power") or pick.get("use") or "")[:160],
           "worth": str(pick.get("worth") or "")[:220],
           "use": str(pick.get("use") or "")[:220],
           "look": str(pick.get("look") or "")[:300],
           "plate": str(pick.get("plate") or ""),
           "from": str(foe or "").strip()[:60], "source": "encounter",
           "how": str(how or "").strip(),
           "turn": int(st.get("turn_count") or 0)}
    pack = held(st)
    pack.append(got)
    st["gear"] = pack
    got = _stow(st, got)
    print(f"[GOAL] spoils: {got['name']!r} ({got['kind']}, {got['tier']}) off "
          f"{foe or 'the fight'!r} ({how or 'won'}) — carrying {len(held(st))}", flush=True)
    return got


def _stow(st: dict, got: dict) -> dict:
    """A run that is a Character keeps what it finds ON the character
    (characters.py): the plate is copied out of this world's look book, and
    the next run — in any World — starts with it in the pack. Returns the
    stored item (with its id, its slot, whether it is worn)."""
    if not st.get("character_id"):
        return got
    try:
        import characters
        characters.stow_run_gear(st)
    except Exception as e:
        print(f"[GOAL] could not put {got.get('name')!r} on the character: {e}", flush=True)
        return got
    for g in held(st):
        if _norm_name(g.get("name")) == _norm_name(got.get("name")):
            return g
    return got


def gear_edge(state: Optional[dict]) -> Dict[str, List[str]]:
    """What the pack does in a fight (encounter.api_resolve): the weapons and
    the armour carried, newest first. Upgrades, tools and relics do their work
    in the story, not on the dice.

    On a run that is a Character, only what is WORN counts — the sword in the
    pack does not swing itself, and the turnaround every frame is drawn from
    shows what is worn, so the dice and the picture agree about it."""
    out: Dict[str, List[str]] = {}
    st = state if isinstance(state, dict) else {}
    for g in reversed(held(state)):
        if st.get("character_id") and g.get("character_item") and not g.get("worn"):
            continue
        k = str(g.get("kind") or "").strip().lower()
        k = "armor" if k == "armour" else k
        if k in ("weapon", "armor"):
            out.setdefault(k, []).append(str(g.get("name")))
    return out


def world_gear(state: Optional[dict], session_id: str = "default") -> Dict[str, int]:
    """How much of this world's gear the run has found, of how much there is."""
    table = _table(session_id)
    names = {_norm_name(it.get("name")) for it in table}
    have = {_norm_name(g.get("name")) for g in held(state)}
    return {"found": len(names & have), "of": len(table)}


def _pickups(state: Optional[dict]) -> List[Dict[str, str]]:
    """What the narrator's prose picked up along the way (items.py: a crowbar,
    a flashlight). One pack, one concept: they sit in the same slots as the
    gear, shown by name — they have no picture, and the gear is better, which
    is correct, because it is."""
    st = state if isinstance(state, dict) else {}
    try:
        import items as _items
        table = dict(getattr(_items, "ITEMS", {}) or {})
    except Exception:
        table = {}
    out = []
    for iid in (st.get("inventory") or []):
        meta = table.get(str(iid)) or {}
        name = str(meta.get("display") or str(iid).replace("_", " ").title()).strip()
        if name:
            out.append({"name": name, "kind": str(meta.get("type") or ""),
                        "source": "pickup", "plate": ""})
    return out


def pack_cards(state: Optional[dict], session_id: str = "default") -> List[Dict[str, str]]:
    """The pack as the client draws it: the gear, newest last, then whatever
    was picked up along the way."""
    cards = [_pack_card(g) for g in held(state)]
    have = {_norm_name(c.get("name")) for c in cards}
    for p in _pickups(state):
        if _norm_name(p["name"]) not in have:
            have.add(_norm_name(p["name"]))
            cards.append(_pack_card(p))
    return cards


def bind_gear(state: dict, session_id: str = "default") -> Dict[str, str]:
    """A goal walked toward before this world's gear existed (the book was
    still being shot when the first goal was drafted) gets its prize now, on
    the way in — before the reward cutscene shoots what is inside, so the
    thing in the room and the thing in the pack are the same thing."""
    st = state if isinstance(state, dict) else {}
    if not st.get("level_goal") or phase(st) != "place":
        return {}
    if str(st.get("goal_prize_kind") or "").strip():
        return {}
    gear = draw_gear(st, session_id)
    if not gear:
        return {}
    pz = as_prize(gear)
    st["goal_prize"] = _clean_name(pz["name"])[:40]
    st["goal_prize_look"] = pz["look"][:240]
    st["goal_prize_verb"] = pz["verb"]
    st["goal_prize_kind"] = pz["kind"]
    st["goal_prize_power"] = pz["power"]
    st["goal_prize_plate"] = pz["plate"][:400]
    print(f"[GOAL] {st.get('goal_name')!r} now holds the {pz['kind']}: {pz['name']}",
          flush=True)
    return pz


def as_prize(gear: Dict[str, str]) -> Dict[str, str]:
    """A piece of gear in the shape the goal loop already carries a prize in."""
    if not gear or not gear.get("name"):
        return {}
    return {"name": str(gear.get("name") or "")[:40],
            "look": str(gear.get("look") or "")[:300],
            "verb": _verb_for(gear.get("kind")),
            "kind": str(gear.get("kind") or "relic"),
            "power": str(gear.get("power") or gear.get("use") or "")[:160],
            "plate": str(gear.get("plate") or "")}


def _verb_for(kind: Any) -> str:
    """What you DO to the thing to make it yours. A weapon is taken up, armour
    is put on — the tag says the right word for what is in the room."""
    return {"weapon": "Take", "armor": "Wear", "armour": "Wear",
            "upgrade": "Fit", "tool": "Take", "relic": "Lift"}.get(
                str(kind or "").strip().lower(), "Take")



PRIZE_RULES = (
    "Then name WHAT IS INSIDE — the one object in there that the player came "
    "for and leaves with. It is a THING, small enough to be in one shot and "
    "reachable by hand: a case, a core, a body, a switch, a book, a head in a "
    "jar, a door lever. Not a room, not a view, not an idea.\n"
    "- prize.name: 2 or 3 words, Title Case, the label on the picture.\n"
    "- prize.look: ONE sentence for an image prompt — what it is, what it is "
    "made of, how it sits in the room, how it is lit. No proper nouns.\n"
    "- prize.verb: ONE word for what the player does to it: Take, Open, Pull, "
    "Cut, Lift, Free, Read, or similar.\n"
    "- Not another key, core, port or module unless the place really only has "
    "one of those in it. Three keys in a row is three of the same beat. A "
    "thing someone left behind, a thing that is still alive, a thing that "
    "takes both hands to carry, a thing that is a record of what happened "
    "here — those are different payoffs.\n\n"
)


# ─────────────────────────────────────────────────────────────────────────────
# The record on the run
# ─────────────────────────────────────────────────────────────────────────────

def record(state: Optional[dict]) -> Dict[str, str]:
    st = state if isinstance(state, dict) else {}
    rec = {k.split("_", 1)[1]: str(st.get(k) or "").strip()
           for k in STATE_KEYS + SPINE_KEYS}
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
    b = _clean_boss(rec.get("boss"))
    if b:
        with _LOCK:
            _BOSSES.setdefault(line, b)
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
    state["goal_truth"] = str(rec.get("truth") or "").strip()[:240]
    state["goal_claim"] = str(rec.get("claim") or "").strip()[:240]
    state["goal_cost"] = str(rec.get("cost") or "").strip()[:240]
    state["goal_phase"] = "place"
    p = rec.get("prize") if isinstance(rec.get("prize"), dict) else {}
    state["goal_prize"] = _clean_name(p.get("name"))[:40]
    state["goal_prize_look"] = str(p.get("look") or "").strip()[:240]
    verb = str(p.get("verb") or "").strip().split()[0].title() if p.get("verb") else ""
    state["goal_prize_verb"] = verb or "Take"
    # Gear drawn from the look book brings its kind, what it does, and the
    # picture already shot for it on the props sheet. An invented prize has
    # none of those and simply leaves them empty.
    state["goal_prize_kind"] = str(p.get("kind") or "").strip().lower()[:20]
    state["goal_prize_power"] = str(p.get("power") or "").strip()[:160]
    state["goal_prize_plate"] = str(p.get("plate") or "").strip()[:400]
    if isinstance(rec.get("boss"), dict):
        install_boss(state, rec["boss"])
    line = str(line or "").strip() or (f"{name} — {look}" if look else name)
    state["level_goal"] = line
    if state.get("goal_truth") or state.get("goal_claim"):
        print(f"[GOAL] spine of {name!r} | truth: {state.get('goal_truth')} "
              f"| claim: {state.get('goal_claim')} | cost: {state.get('goal_cost')}",
              flush=True)
    return line


_PRIZES: Dict[str, Dict[str, str]] = {}       # level_goal -> the thing inside


def _gear_of(st: dict) -> Dict[str, str]:
    """The gear fields sitting beside a prize on the run, if it is gear."""
    return {"kind": str(st.get("goal_prize_kind") or "").strip(),
            "power": str(st.get("goal_prize_power") or "").strip(),
            "plate": str(st.get("goal_prize_plate") or "").strip()}


def prize(state: Optional[dict], make: bool = False) -> Dict[str, str]:
    """The object in the goal's room that ends the errand when it is taken.

    With ``make``, one is drafted on the spot for a goal whose own draft came
    back without one (an older run, a model that skipped the field) — played
    as a goal that could be walked into and never finished."""
    st = state if isinstance(state, dict) else {}
    out = {"name": str(st.get("goal_prize") or "").strip(),
           "look": str(st.get("goal_prize_look") or "").strip(),
           "verb": str(st.get("goal_prize_verb") or "Take").strip(),
           **_gear_of(st)}
    if out["name"]:
        return out
    line = str(st.get("level_goal") or "").strip()
    with _LOCK:
        got = _PRIZES.get(line)
    if got or not make or not line:
        if got:
            st["goal_prize"] = got["name"]
            st["goal_prize_look"] = got["look"]
            st["goal_prize_verb"] = got["verb"]
        return dict(got or {})
    rec = record(st)
    drafted: Dict[str, str] = {}
    try:
        import ai_provider_manager
        _had = [str(n) for n in (st.get("goals_taken") or []) if str(n).strip()]
        prompt = (
            "The player has just walked into this place, at the end of a long "
            f"trip to reach it: {rec.get('name') or line} — {rec.get('look') or ''}. "
            f"{rec.get('why') or ''}\n"
            + (f"WHAT IS ACTUALLY TRUE OF IT: {rec.get('truth')}\n"
               if rec.get("truth") else "")
            + (f"WHO ELSE WANTS IT: {rec.get('claim')}\n"
               if rec.get("claim") else "")
            + f"World: {str(st.get('world_prompt') or '')[:600]}\n"
            + (f"ALREADY CARRIED OUT OF OTHER PLACES THIS RUN — not another one "
               f"of these: {' | '.join(_had)}\n" if _had else "")
            + "\n"
            + PRIZE_RULES +
            'Reply with JSON only: {"name": "...", "look": "...", "verb": "..."}'
        )
        raw = ai_provider_manager.chat([{"role": "user", "content": prompt}],
                                       temperature=0.9, max_tokens=200)
        d = _parse_json(raw)
        if _clean_name(d.get("name")):
            drafted = {"name": _clean_name(d.get("name"))[:40],
                       "look": str(d.get("look") or "").strip()[:240],
                       "verb": (str(d.get("verb") or "Take").strip().split() or ["Take"])[0].title()}
    except Exception as e:
        print(f"[GOAL] prize draft failed: {e}", flush=True)
    if not drafted:
        drafted = {"name": "The Cache", "verb": "Take",
                   "look": "a sealed case set down where it was left, scratched and "
                           "heavy, the only thing in here worth carrying out"}
    with _LOCK:
        _PRIZES[line] = drafted
    st["goal_prize"] = drafted["name"]
    st["goal_prize_look"] = drafted["look"]
    st["goal_prize_verb"] = drafted["verb"]
    print(f"[GOAL] what is in {rec.get('name')!r}: {drafted['name']!r} "
          f"({drafted['verb'].lower()})", flush=True)
    return dict(drafted)


def phase(state: Optional[dict]) -> str:
    """``place`` while they are walking to it, ``prize`` once they are inside."""
    st = state if isinstance(state, dict) else {}
    return "prize" if str(st.get("goal_phase") or "") == "prize" else "place"


def target(state: Optional[dict]) -> Dict[str, str]:
    """What the tag points at right now: the place, or the thing inside it."""
    if phase(state) == "prize":
        p = prize(state)
        if p:
            return {"name": p["name"], "look": p["look"], "why": "", "verb": p["verb"]}
    rec = record(state)
    return {"name": rec.get("name", ""), "look": rec.get("look", ""),
            "why": rec.get("why", ""), "verb": ""}


def name_from(text: Any) -> str:
    """A label-length name out of a sentence, with no model: its first clause."""
    s = re.split(r"[.,;:(\u2014]| - ", str(text or ""), maxsplit=1)[0]
    return _clean_name(s)


def look_line(state: Optional[dict]) -> str:
    rec = record(state)
    if not rec["name"]:
        return ""
    return f"{rec['name']}: {rec['look']}" if rec["look"] else rec["name"]


_TAIL_WORDS = {"the", "a", "an", "at", "of", "to", "in", "on", "by", "with", "and",
               "or", "for", "from", "near", "into", "onto", "under", "over", "behind",
               "beyond", "past", "its", "their", "his", "her", "end"}


def _clean_name(name: Any) -> str:
    s = re.sub(r"\s+", " ", str(name or "")).strip().strip(".\"'")
    if len(s) > NAME_MAX:
        s = s[:NAME_MAX].rsplit(" ", 1)[0]
        # A label cut to length must not end mid-phrase: the first goal of a
        # live run read "The reinforced blast door at the" in the corner.
        words = s.split(" ")
        while len(words) > 1 and words[-1].lower().strip(",;:") in _TAIL_WORDS:
            words.pop()
        s = " ".join(words).rstrip(",;:")
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


def _last_beat(session_id: str) -> str:
    """The last thing that actually happened, so the next goal follows from it."""
    try:
        import engine
        for entry in reversed(engine._load_history(session_id) or []):
            text = str((entry or {}).get("dispatch") or "").strip()
            if text and not str((entry or {}).get("choice") or "").startswith("__"):
                return text[:400]
    except Exception:
        pass
    return ""


# How many goals to draft before picking one, and the phase each goal of a run
# is drafted against (look_book's CONFLICT row is already ordered this way).
CANDIDATES = max(1, int(os.getenv("SOMEWHERE_GOAL_CANDIDATES", "3")))
PHASES = ("normal", "escalating", "critical")


def world_brief(session_id: str = "default") -> str:
    """The run's OWN designed world, as direction for the goal drafter.

    Every run builds a nine-frame look book before it starts: row 3 is three
    places "dressed with narrative evidence so each tells a story by itself",
    row 2 is three conflicts "at their most dangerous instant", and
    roster_looks is who lives here. The consequence model has read all of it
    since 2026-09-21 (look_book.story_directive). The GOAL — the one thing the
    whole run is pointed at — was the only system still drafted blind to it,
    which is why it kept inventing generic nouns: generic nouns were all it
    had. Empty string when the book is not ready; the run never waits on it."""
    try:
        import look_book
    except Exception:
        return ""
    try:
        book = look_book.current(session_id, rebuild_if_stale=False)
    except Exception:
        return ""
    if not isinstance(book, dict) or book.get("status") != "ready":
        return ""
    lines: List[str] = []

    def _row(row: str) -> List[dict]:
        try:
            return [f for f in (book.get("frames") or [])
                    if isinstance(f, dict)
                    and str(f.get("row") or "").strip().upper().startswith(row)]
        except Exception:
            return []

    def _one(f: dict) -> str:
        bit = f"  - {f.get('title')}: {f.get('subject')}"
        return bit + (f" — {f.get('story')}" if f.get("story") else "")

    sets = _row("SET")
    if sets:
        lines.append(
            "PLACES THIS WORLD HAS ALREADY DRESSED — the production designer "
            "built these for this run, each one full of story. The goal should "
            "BE one of them, or somewhere one of them points at:" + NL
            + NL.join(_one(f) for f in sets[:3]))
    fights = _row("CONFLICT")
    if fights:
        lines.append(
            "WHAT DANGER LOOKS LIKE HERE — the three set pieces this run is "
            "built to stage. Whoever holds the goal, and what it costs to reach "
            "it, comes out of these:" + NL
            + NL.join(_one(f) for f in fights[:3]))
    looks = [e for e in (book.get("roster_looks") or [])
             if isinstance(e, dict) and e.get("look")]
    if looks:
        lines.append(
            "WHO LIVES HERE (designed — name them the way the book does):" + NL
            + NL.join(f"  - {str(e.get('kind') or '')[:48]}: "
                      f"{str(e.get('look'))[:110]}" for e in looks[:6]))
    lr = book.get("look_rules") if isinstance(book.get("look_rules"), dict) else {}
    try:
        motifs = [str(m) for m in (lr.get("motifs") or [])][:4]
    except Exception:
        motifs = []
    if motifs:
        lines.append("THIS RUN'S RECURRING MOTIFS: " + ", ".join(motifs))
    return (NL + NL).join(lines)


def _protagonist() -> str:
    """Who is being asked to walk there. A goal that asks nothing of THIS
    person is an errand anyone could run."""
    try:
        import game_identity
        spec = game_identity.get_spec() or {}
        pc = spec.get("player_character")
        pc = pc if isinstance(pc, dict) else {}
        bits = [f"{k}: {str(pc.get(k))[:160]}" for k in
                ("name", "role", "signature_gear", "demeanor") if pc.get(k)]
        return ("WHO THE PLAYER IS — " + "; ".join(bits)) if bits else ""
    except Exception:
        return ""


def _known(spec=None, *, lore: str = "", world_prompt: str = "", authored: str = "",
           from_inside: str = "", took: str = "", after: str = "",
           done: Optional[List[str]] = None, carried: Optional[List[str]] = None,
           session_id: str = "default", phase: str = "",
           holds: Optional[dict] = None) -> str:
    """Everything the drafter is allowed to know: the level, the author, the
    run so far, and — new — the world this run has already designed for itself
    (world_brief). Empty when there is nothing to go on."""
    import game_identity
    setting = game_identity.authored_setting(spec) or {}
    known = "\n".join(p for p in (
        f"Place: {setting.get('name')}" if setting.get("name") else "",
        f"What it is: {setting.get('summary')}" if setting.get("summary") else "",
        f"Era: {setting.get('era')}" if setting.get("era") else "",
        f"Landmarks: {setting.get('landmarks')}" if setting.get("landmarks") else "",
        (f"THE AUTHOR WROTE THIS AS THE GOAL — honour it: {authored}" if authored else ""),
        (f"THEY HAVE JUST TAKEN: {took}. The next goal comes OUT OF THAT — "
         "what it points at, what it opens, who wants it back, where it has to "
         "be carried. Not a fresh errand; the next move in the same story."
         if took else ""),
        (f"THE BEAT THEY JUST PLAYED: {after}" if after else ""),
        # The gear was drawn before the place was. The place is built to be
        # somewhere that thing would credibly be, and the draft does not get
        # to invent a different prize.
        ((f"WHAT IS WAITING INSIDE IS ALREADY DECIDED — this world's "
          f"{holds.get('kind') or 'gear'}, the {holds['name']}: "
          f"{holds.get('look') or ''} {holds.get('power') or ''}\n"
          "BUILD THE PLACE AROUND IT: somewhere that thing would really be, "
          "somewhere it was used, stored, hidden or lost. The goal is the "
          "PLACE; do not rename the thing and do not invent a different one. "
          "Its `truth` is what it is doing there.")
         if holds and holds.get("name") else ""),
        # Three goals in a row came back "Reinforced Blast Door", "Data Siphon
        # Hub", "Atmospheric Cooling Spire" — one world, three of the same
        # noun, and the run read as the same errand three times. It has to be
        # able to see what it has already been.
        (("ALREADY WALKED TO THIS RUN — do not name any of these again, and do "
          "not pick another of the SAME KIND (another hub, another spire, "
          "another door): " + " | ".join(str(n) for n in done if str(n).strip()))
         if done else ""),
        (("ALREADY CARRIED OUT OF THOSE PLACES — what is inside this one is a "
          "DIFFERENT KIND of thing, not another one of these: "
          + " | ".join(str(n) for n in carried if str(n).strip()))
         if carried else ""),
        (f"THE PLAYER IS STANDING INSIDE {from_inside}, which they have just "
         "taken. The next goal is a DIFFERENT place, somewhere they can see "
         "from in here or from its far side — through a window, down the line, "
         "past the yard — and going to it is the next leg of the run. Never "
         f"name {from_inside} again." if from_inside else ""),
        (f"World notes: {str(lore).strip()[:1200]}" if lore else ""),
        (f"Current situation: {str(world_prompt).strip()[:700]}" if world_prompt else ""),
        _protagonist(),
        # The run's own designed world — its dressed sets, its set pieces, its
        # cast. Everything below this line already existed and the goal was the
        # one system never shown it.
        world_brief(session_id),
        (f"THIS IS THE {phase.upper()} LEG OF THE RUN. "
         + {"normal": "It opens the story: something is visibly off and going "
                      "to look at it is its own reason.",
            "escalating": "The run is already moving. This one costs more than "
                          "the last and somebody is now actively in the way.",
            "critical": "This is the last one. Everything the run has learned "
                        "points here, and it should feel like the end of "
                        "something."}.get(phase, "")
         if phase else ""),
    ) if p).strip()
    return known


def _draft(known: str, n: int = 1, note: str = "") -> List[Dict[str, Any]]:
    """n candidate goals, with their spines. [] when the model gives nothing."""
    import ai_provider_manager
    held_already = "WHAT IS WAITING INSIDE IS ALREADY DECIDED" in known
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
        "item name in a game. Title Case. No articles. Never a generic systems "
        "noun on its own — hub, spire, core, module, port, facility, complex, "
        "terminal are what a run writes when it has nothing to say.\n"
        "- why: ONE short sentence, under 14 words, said plainly about the place, "
        "not to the player. Never 'you must' or 'you need'. Use no proper nouns "
        "that are not written above.\n"
        "- look: ONE sentence describing exactly what it looks like from far "
        "away, for an image prompt: shape, material, light. No proper nouns.\n"
        "- The KIND of place matters as much as the name. A run that walks to "
        "three hubs, or three spires, or three doors is the same errand three "
        "times. Pick something structurally different from anything listed "
        "above: if the last one was a tower, go under; if it was sealed, go "
        "somewhere wide open; if it was machinery, go somewhere people "
        "lived.\n\n"
        + SPINE_RULES
        + ("" if "WHAT IS WAITING INSIDE IS ALREADY DECIDED" in known else PRIZE_RULES)
        + BOSS_RULES
        + (f"Give me {n} DIFFERENT candidates, not variations of one idea. "
           "Build the first on one of the dressed places above, the second on "
           "one of the set pieces, the third on somebody from the cast — and "
           "if there is nothing above to build on, make three genuinely "
           "different answers anyway.\n" if n > 1 else "")
        + (f"THE LAST DRAFT WAS JUDGED AND FELL SHORT: {note} Fix exactly that."
           "\n" if note else "")
        + ('Reply with JSON only: {"goals": [' if n > 1 else "Reply with JSON only: ")
        + '{"name": "...", "why": "...", "look": "...", "truth": "...", '
          '"claim": "...", "cost": "...", '
        + ("" if held_already else '"prize": {"name": "...", "look": "...", "verb": "..."}, ')
        + '"boss": {"name": "...", "kind": "...", "look": "...", "want": "..."}}'
        + ("]}" if n > 1 else "")
    )
    try:
        raw = ai_provider_manager.chat(
            [{"role": "user", "content": prompt}], temperature=1.0,
            max_tokens=420 * max(1, n))
    except Exception as e:
        print(f"[GOAL] invent failed: {e}", flush=True)
        return []
    if "signal interrupted" in str(raw).lower():
        print(f"[GOAL] invent returned nothing usable: {str(raw)[:120]!r}", flush=True)
        return []
    d = _parse_json(raw)
    rows = d.get("goals") if isinstance(d.get("goals"), list) else ([d] if d else [])
    out: List[Dict[str, Any]] = []
    for row in rows:
        one = _clean_goal(row)
        if one:
            out.append(one)
    return out


def _clean_goal(d: Any) -> Dict[str, Any]:
    """One candidate, tidied. {} if it has no name to stand on."""
    if not isinstance(d, dict):
        return {}
    name = _clean_name(d.get("name"))
    if not name:
        return {}
    why = re.sub(r"^(you (must|need to|have to)\s+)", "",
                 str(d.get("why") or "").strip(), flags=re.I)
    why = why[:1].upper() + why[1:] if why else ""
    out: Dict[str, Any] = {
        "name": name, "why": why, "look": str(d.get("look") or "").strip(),
        "truth": str(d.get("truth") or "").strip()[:240],
        "claim": str(d.get("claim") or "").strip()[:240],
        "cost": str(d.get("cost") or "").strip()[:240],
    }
    pz = d.get("prize") if isinstance(d.get("prize"), dict) else {}
    if _clean_name(pz.get("name")):
        out["prize"] = {"name": _clean_name(pz.get("name")),
                        "look": str(pz.get("look") or "").strip(),
                        "verb": str(pz.get("verb") or "Take").strip()}
    b = _clean_boss(d.get("boss"))
    if b:
        out["boss"] = b
    return out


# The bar. A goal is kept when nothing about it is actively bad and the whole
# is better than ordinary; under either, it gets one redraft with the judge's
# note attached and then we take the best of what we have.
JUDGE_FLOOR = int(os.getenv("SOMEWHERE_GOAL_FLOOR", "2"))     # no criterion below this
JUDGE_TOTAL = int(os.getenv("SOMEWHERE_GOAL_TOTAL", "20"))    # out of 30


def _judge(cands: List[Dict[str, Any]], known: str) -> Dict[str, Any]:
    """Score the candidates and pick one. Falls back to the first candidate —
    a run is never held up because the judge was unavailable."""
    import ai_provider_manager
    # One candidate still gets scored. Comparison is not the only reason to
    # judge — a single weak goal should earn its redraft the same as a weak
    # field of three.
    if not cands:
        return {"pick": 0, "total": 0, "low": 0, "why": "", "fix": ""}
    listing = NL.join(
        f'{i}: {c.get("name")} — {c.get("why")} | truth: {c.get("truth")} | '
        f'claim: {c.get("claim")} | cost: {c.get("cost")} | '
        f'prize: {(c.get("prize") or {}).get("name")} | '
        f'boss: {(c.get("boss") or {}).get("name")}'
        for i, c in enumerate(cands))
    try:
        raw = ai_provider_manager.chat(
            [{"role": "user", "content": JUDGE_RULES + NL + NL
              + "THE WORLD AND THE RUN SO FAR:" + NL + known[:2000] + NL + NL
              + "CANDIDATES:" + NL + listing}],
            temperature=0.2, max_tokens=420)
    except Exception as e:
        print(f"[GOAL] judge unavailable ({e}) — taking the first draft", flush=True)
        return {"pick": 0, "total": 0, "low": 0, "why": "", "fix": ""}
    d = _parse_json(raw)
    rows = d.get("scores") if isinstance(d.get("scores"), list) else []
    best, best_total, best_low = 0, -1, 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            i = int(row.get("i", 0))
        except Exception:
            continue
        if not (0 <= i < len(cands)):
            continue
        vals = []
        for k in ("worth", "only_here", "conflict", "mystery", "payoff", "fresh"):
            try:
                vals.append(max(0, min(5, int(row.get(k) or 0))))
            except Exception:
                vals.append(0)
        total = sum(vals)
        if total > best_total:
            best, best_total, best_low = i, total, (min(vals) if vals else 0)
    try:
        said = int(d.get("pick"))
        if 0 <= said < len(cands) and rows:
            best = said
    except Exception:
        pass
    return {"pick": best, "total": max(0, best_total), "low": best_low,
            "why": str(d.get("why") or "").strip()[:200],
            "fix": str(d.get("fix") or "").strip()[:240]}


def invent(spec: Optional[dict] = None, *, lore: str = "", world_prompt: str = "",
           authored: str = "", from_inside: str = "", took: str = "",
           after: str = "", done: Optional[List[str]] = None,
           carried: Optional[List[str]] = None, session_id: str = "default",
           phase: str = "", holds: Optional[dict] = None) -> Dict[str, str]:
    """The next goal for this run, drafted against its own look book and judged.

    CANDIDATES drafts, scored on six written criteria; the winner is kept, and
    a winner under the bar earns exactly one redraft with the judge's note
    attached. {} when there is nothing to go on or every call fails — the
    caller falls back, and a run is never blocked by this."""
    kw = dict(spec=spec, lore=lore, world_prompt=world_prompt, authored=authored,
              from_inside=from_inside, took=took, after=after, done=done,
              carried=carried, session_id=session_id, phase=phase, holds=holds)
    known = _known(**kw)
    if not known:
        return {}
    cands = _draft(known, CANDIDATES)
    if not cands:
        return {}
    v = _judge(cands, known)
    pick = cands[v["pick"]] if 0 <= v["pick"] < len(cands) else cands[0]
    if v["total"] and (v["low"] < JUDGE_FLOOR or v["total"] < JUDGE_TOTAL):
        print(f"[GOAL] {pick['name']!r} scored {v['total']}/30 (lowest {v['low']}) "
              f"— redrafting: {v['fix']!r}", flush=True)
        again = _draft(known, CANDIDATES, note=v["fix"])
        if again:
            w = _judge(again, known)
            second = again[w["pick"]] if 0 <= w["pick"] < len(again) else again[0]
            if w["total"] >= v["total"]:
                pick, v = second, w
    if v["total"]:
        print(f"[GOAL] chose {pick['name']!r} at {v['total']}/30 out of "
              f"{len(cands)}: {v['why']}", flush=True)
    return pick

# ─────────────────────────────────────────────────────────────────────────────
# THE BOSS — who is waiting at the goal
# ─────────────────────────────────────────────────────────────────────────────
#
# Played: reaching the goal was an overlay and then nothing. In the games this
# borrows from, the destination is a confrontation and the payoff is behind it
# (Dagoth Ur at the Heart, the Master in the Cathedral). So the goal is invented
# WITH the one who holds it. The run glimpses them on the way in, ENTER at the
# threshold opens the fight with them (an encounter with the boss tier: more
# rounds, no walking away with the goal), and only putting them down or
# talking them down completes the goal.

SPINE_RULES = (
    "Then give the goal its SPINE — the three things that turn a waypoint into "
    "somewhere worth walking to. None of these is shown to the player up "
    "front; they are what the run pays off.\n"
    "- truth: ONE sentence. What is ACTUALLY going on in there, which the "
    "player does not know when they set off and finds out by arriving. It must "
    "be different from `why` — `why` is what they believe, `truth` is what is "
    "so. If the two say the same thing there is no mystery and the arrival has "
    "nothing to give.\n"
    "- claim: ONE sentence. Who ELSE wants it and why — named as someone this "
    "world already contains, with a reason of their own that is not 'to stop "
    "the player'. This is who the boss is working for, or is.\n"
    "- cost: ONE sentence, under 14 words. What getting there takes from this "
    "player specifically — what they burn, leave behind, or are seen doing.\n\n"
)

JUDGE_RULES = (
    "You are the game's director. Below are candidate goals for the next leg "
    "of one playthrough. A player is going to spend real minutes walking to "
    "whichever one you pick, fight for it, and watch a cutscene when they "
    "arrive. Pick the one that earns that.\n\n"
    "Score EACH candidate 1-5 on each of six things. Be hard; 3 is ordinary.\n"
    "1 WORTH_WALKING_TO — from the first frame, would a player say 'I want to "
    "see that'? A door is not an answer to that question. A place with "
    "something visibly wrong with it is.\n"
    "2 ONLY_HERE — could this be lifted into any other game? It must use THIS "
    "world's own materials, cast and motifs. Score 1 for generic nouns: hub, "
    "spire, core, module, port, facility, complex, terminal.\n"
    "3 CONFLICT — does somebody else want it, for a reason of their own?\n"
    "4 MYSTERY — is `truth` genuinely different from `why`, and does arriving "
    "answer something the player has been wondering?\n"
    "5 PAYOFF — is the prize a thing you can hold, and does holding it change "
    "what the run is about?\n"
    "6 FRESH — is it different in KIND from everywhere this run has already "
    "been? Another of the same noun scores 1.\n\n"
    'Reply with JSON only: {"scores": [{"i": 0, "worth": 0, "only_here": 0, '
    '"conflict": 0, "mystery": 0, "payoff": 0, "fresh": 0}], "pick": 0, '
    '"why": "one short sentence", "fix": "one sentence on what the best one '
    'still lacks"}'
)

BOSS_KINDS = ("person", "group", "creature", "anomaly", "character")

BOSS_RULES = (
    "Also choose the BOSS: the one who holds that place and is waiting in it — "
    "the reason getting there is dangerous. A person, a creature or a thing, "
    "whatever this world makes likely.\n"
    "- boss.name: 2 to 4 words, Title Case, a role or a title rather than a first "
    "name (The Warden, Minister of Order, The Hollow Choir).\n"
    "- boss.kind: one of person, group, creature, anomaly, character.\n"
    "- boss.look: ONE sentence, under 28 words, what they look like up close, "
    "for an image prompt. No proper nouns.\n"
    "- boss.want: ONE sentence, under 14 words, what they want from the player.\n\n"
)

BOSS_KEYS = ("goal_boss_name", "goal_boss_kind", "goal_boss_look", "goal_boss_want")
_BOSSES: Dict[str, Dict[str, str]] = {}       # level_goal -> boss, for runs without one on state


def claim_of(state: Optional[dict]) -> str:
    """Who else wants this place, in their own words. Feeds the boss's want
    and the beats on the way in — an obstacle with a reason is a character."""
    st = state if isinstance(state, dict) else {}
    return str(st.get("goal_claim") or "").strip()


def _clean_boss(raw: Any) -> Dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    name = _clean_name(raw.get("name"))
    if not name:
        return {}
    kind = str(raw.get("kind") or "").strip().lower()
    return {"name": name,
            "kind": kind if kind in BOSS_KINDS else "person",
            "look": str(raw.get("look") or "").strip()[:260],
            "want": str(raw.get("want") or "").strip()[:160]}


def boss(state: Optional[dict], make: bool = False) -> Dict[str, str]:
    """The run's boss: from state, else from this process's memory, else (with
    ``make``) drafted now from the goal and remembered. {} when there is no
    goal."""
    st = state if isinstance(state, dict) else {}
    b = {k.split("_", 2)[2]: str(st.get(k) or "").strip() for k in BOSS_KEYS}
    if b["name"]:
        return b
    line = str(st.get("level_goal") or "").strip()
    if not line:
        return {}
    with _LOCK:
        got = _BOSSES.get(line)
    if got or not make:
        return dict(got or {})
    rec = record(st)
    drafted: Dict[str, str] = {}
    try:
        import ai_provider_manager
        prompt = (
            "A game level's goal is a place the player walks to: "
            f"{rec.get('name') or line} — {rec.get('look') or ''}. {rec.get('why') or ''}\n"
            f"World: {str(st.get('world_prompt') or '')[:600]}\n\n"
            + BOSS_RULES +
            'Reply with JSON only: {"name": "...", "kind": "...", "look": "...", "want": "..."}'
        )
        raw = ai_provider_manager.chat([{"role": "user", "content": prompt}],
                                       temperature=0.9, max_tokens=240)
        drafted = _clean_boss(_parse_json(raw))
    except Exception as e:
        print(f"[GOAL] boss draft failed: {e}", flush=True)
    if not drafted:
        drafted = {"name": f"Keeper of the {(rec.get('name') or 'Place').split()[-1]}",
                   "kind": "person",
                   "look": "a tall figure in dark, weathered gear, face half hidden, standing its ground",
                   "want": "No one reaches what it guards."}
    with _LOCK:
        _BOSSES[line] = drafted
    print(f"[GOAL] boss for {rec.get('name')!r}: {drafted['name']!r} ({drafted['kind']})", flush=True)
    return dict(drafted)


def install_boss(state: dict, b: Dict[str, str]) -> None:
    b = _clean_boss(b)
    if not b:
        return
    for k in BOSS_KEYS:
        state[k] = b[k.split("_", 2)[2]]


def boss_subject(state: Optional[dict]) -> Dict[str, str]:
    """The encounter subject for the fight at the goal."""
    b = boss(state, make=True)
    if not b:
        return {}
    return {"label": b["name"], "kind": b["kind"], "look": b["look"],
            "want": b["want"], "source": "boss", "goal": record(state).get("name", "")}


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
        try:
            import provider_bridge   # OpenAI chosen in ACCOUNT answers this call
            if not provider_bridge.active():
                return miss
        except Exception:
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
# THE RUN IS A LIST OF GOALS. One place walked to, fought for and gone into is
# a beat; three of them is a run you can win. "the actual game loop here is to
# simply find a certain # of goals. 1/3 goals reached, etc. and victory is
# surviving to get to the 3 goals" (2026-09-22).
GOALS_TO_WIN = int(os.getenv("GOALS_TO_WIN", "3"))
_APPROACH: Dict[str, Dict[str, int]] = {}     # level_goal -> {steps, last_turn}
_DONE: set = set()                            # level_goal lines the player walked into
_MET: set = set()                             # lines whose boss has already stepped out


def press(state: dict, steps: int) -> None:
    """Walking at it costs something. Each step toward the goal leans on the
    run's threat dial (engine.advance_story_dynamics reads threat_level), so
    the street is quiet at the far end and the place is awake by the time they
    are at the door. The walk had no teeth before this: three clicks and in.

    Called on the PERSISTED state by the endpoint, never on the live copy the
    turn loop owns — the step itself is counted against the live turn number.

    This sets a FLOOR under the dial; it does not ADD to it. The engine already
    climbs threat_level by +1 every turn on its own
    (advance_story_dynamics), so adding 2, then 3, then 4 on top of that put a
    run at 9 — past critical, which is 6 — on TURN THREE, with nothing in the
    game that ever brings it back down. The first encounter of the run then
    killed the player outright: "we're dead". The walk is supposed to make the
    approach tense, not to end the run before the story has started."""
    try:
        import engine
        crit = int(getattr(engine, "STORY_CRITICAL_AT", 6))
        n = int(steps)
        # 2, then 3 … and never critical on the WAY there. The door is the
        # only place the walk itself is allowed to tip the world over.
        floor = crit if n >= APPROACH_STEPS else min(1 + n, max(1, crit - 1))
        now = int(state.get("threat_level", 0) or 0)
        state["threat_level"] = max(now, floor)
        state["goal_pressure"] = n
    except Exception:
        pass


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
    if phase(st) == "prize":
        pz = prize(st)
        if not pz:
            return ""
        return (
            f"WHAT THEY CAME FOR IS IN THIS ROOM WITH THEM: {pz['name']} — "
            f"{pz['look']}. It is in visual_scene, close enough to reach, lit "
            "so the eye goes to it, and nothing has taken it away. The player "
            f"has not touched it yet; {pz['verb'].lower()}ing it is theirs to "
            "do. Write the room around it: what it cost to get in here, what "
            "is still moving in the dark, what happens if they linger.\n"
        )
    rec = record(st)
    if not rec["name"] or arrived(st):
        return ""
    p = progress(st)
    turn_now = int(st.get("turn_count") or 0)
    what_ = rec["name"] + (f" ({rec['look']})" if rec["look"] else "")
    # Drafted with the goal and spent here: somebody else wants it, and going
    # there is costing them something. Shown as evidence in the world, never
    # stated — the player works it out.
    spine = ""
    if rec.get("claim"):
        spine += (f" SOMEBODY ELSE IS MOVING ON IT TOO: {rec['claim']} Let the "
                  "beat carry one piece of evidence of that — a mark, a sound, "
                  "something recently moved — never an announcement.")
    if rec.get("cost"):
        spine += (f" WHAT THIS IS COSTING THEM: {rec['cost']} Show it on the "
                  "player or on what they are carrying, not in narration.")
    b = boss(st)
    who = (f"{b['name']} ({b['look']})" if b.get("look") else b.get("name", "")) if b else ""
    if p["steps"] >= APPROACH_STEPS and p["last_turn"] == turn_now:
        return (
            f"THE PLAYER ARRIVES THIS BEAT. They have walked to {what_} and this "
            "beat is the one where they reach it: they stand at its threshold, "
            "at its door, the place filling the frame. "
            + (f"And {who} is there, inside, waiting for them — seen now, face "
               "to face across the threshold, not yet fought. " if who else "")
            + "goal_reached is FALSE this beat: it is finished only when what "
            "waits inside has been dealt with.\n"
        )
    if p["steps"] == APPROACH_STEPS - 1 and p["last_turn"] == turn_now:
        return (
            f"THE PLAYER IS NOW RIGHT AT {rec['name'].upper()}. This beat brings "
            f"them to its threshold: {what_} fills the upper frame, close enough "
            "to touch, its way in (door, gate, hatch, opening) plainly in front "
            "of them and lit. They have not gone in yet."
            + (f" A sign that {b['name']} is inside: a shape in a window, a voice, "
               "a mark left for them. Not a meeting yet." if b else "")
            + spine + "\n"
        )
    if p["steps"] > 0 and p["last_turn"] == turn_now:
        return (
            f"THE PLAYER IS HEADING FOR {rec['name'].upper()} (step {p['steps']} of "
            f"{APPROACH_STEPS}). This beat moves them plainly closer: {what_} is "
            "bigger and nearer in visual_scene than in the last frame, still "
            "ahead of them. Something on the way may slow them; nothing stops them."
            + (f" The world hints at who holds it — {b['name']}: their mark, "
               "their people, or their name said by someone — never them in person."
               if b else "")
            + spine + "\n"
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


def boss_defeated(state: dict, label: str = "", enemy_state: str = "") -> None:
    """The boss at the goal is down or has stood down: THE WAY IN IS OPEN.

    He was the last thing between the player and the place, so the place is
    reached from here on (the door glows, ENTER plays the reward). It is NOT
    the goal done — taking the thing in the room is (take_prize). This used to
    add the goal to _DONE as well, which the sight answer reads as
    `completed`, and a completed goal never draws its way in: the first run in
    which the boss was actually flagged as the boss (00:25 loop playtest)
    beat him and then stood at a door that never lit."""
    line = str((state or {}).get("level_goal") or "")
    with _LOCK:
        _WON[line] = str(enemy_state or "down")
    state["goal_boss_defeated"] = str(enemy_state or "down")
    if not state.get("goal_reached_turn"):
        state["goal_reached_turn"] = int(state.get("turn_count") or 0) + 1
    print(f"[GOAL] boss {label!r} {enemy_state}: the way into "
          f"{state.get('goal_name')!r} is open (turn {state['goal_reached_turn']})",
          flush=True)


_WON: Dict[str, str] = {}                      # level_goal -> how the boss ended
_PAID: set = set()                             # level_goal lines whose payoff card has played


# ─────────────────────────────────────────────────────────────────────────────
# THE REWARD
# ─────────────────────────────────────────────────────────────────────────────
#
# Reaching the goal is the payoff of the whole run, and a card over the frame
# is not a payoff: "we need a reward … a cutscene as a reward, showing the goal
# in all its glory, then depositing the player at that goal on the other side"
# (played 2026-09-22). So ENTER plays an in-game cutscene at the game's own
# resolution (cutscene.MOODS["reward"]) and the last panel of it is where the
# run carries on from — the player is through the door, not still outside it.

def reward_brief(state: Optional[dict]) -> str:
    """What the four shots are of: this goal, in its own words."""
    rec = record(state)
    name = rec.get("name") or "the place"
    bits = [f"THE PLACE IS: {name}."]
    if rec.get("look"):
        bits.append(f"FROM OUTSIDE IT LOOKS LIKE: {rec['look']}.")
    if rec.get("why"):
        bits.append(f"WHY IT MATTERS: {rec['why']}")
    # The arrival is where the player finds out they were half wrong. The
    # cutscene is the only place this is ever shown, so the inside shots are
    # dressed with it.
    if rec.get("truth"):
        bits.append(f"WHAT IS ACTUALLY TRUE OF THIS PLACE, WHICH THE PLAYER IS "
                    f"ONLY NOW SEEING — dress the inside shots with it, never "
                    f"write it as words on screen: {rec['truth']}")
    pz = prize(state)
    if pz:
        bits.append(f"WHAT IS INSIDE, AND WHAT THE LAST SHOT IS OF: {pz['name']} "
                    f"— {pz['look']}"
                    + (f" It is this world's {pz['kind']}: {pz['power']} Shoot it "
                       "like the thing the whole run was for."
                       if pz.get("power") else ""))
    # One take from the frame on screen, not four unrelated cameras: the
    # "four different cameras in four different places" direction here is
    # what opened the reward on the ground outside a rig the player was
    # standing on top of, in another man's jacket (2026-09-23).
    bits.append(
        f"This is the moment the player REACHES {name} after walking to it all "
        "run. Shoot it like a game cinematic — hero angles, light used as a "
        "subject — but as ONE CONTINUOUS TAKE that starts on the frame the "
        "player is looking at: 1 the next instant of that frame, the camera "
        f"rising and pulling back so {name} looms; 2 the camera has followed "
        "them to the way in, their hand on it as it opens — a lock, a wheel, a "
        "seal, a light; 3 over their shoulder as they push through; 4 INSIDE, "
        "and this one is the frame the game carries on from: "
        + (f"{pz['name']} plainly in the room, lit hotter than anything else, "
           "the player standing in the near ground FACING it with clear floor "
           "between them and it, close enough to walk up and "
           f"{pz['verb'].lower()} it. "
           if pz else
           "a space the player is standing in that the frame before could not "
           "see into. ")
        + "Eye level on that last shot, no title and no card, and never the "
        "doorway again."
    )
    return " ".join(bits)


def reward_request(state: Optional[dict]) -> Dict[str, Any]:
    """What the client sends to /api/cutscene/play for the reward."""
    rec = record(state)
    return {"mood": "reward", "name": rec.get("name") or "", "graph": True,
            "shot_brief": reward_brief(state)}


def finish_reward(state: dict, session_id: str, pending: Dict[str, Any]) -> Dict[str, Any]:
    """The cutscene is over: put the player down on the other side of the goal.

    Mirrors the World stitch (engine._stitch_history): the last panel becomes
    the picture the run continues from, behind a hard transition so the next
    frame is drawn from INSIDE the place rather than from the street outside
    it, and the goal is marked reached so every directive stops steering them
    toward a door they are now standing behind."""
    import engine

    rec = record(state)
    name = rec.get("name") or "the place"
    shots = [sh for sh in (pending.get("shots") or []) if isinstance(sh, dict)]
    last = next((sh for sh in reversed(shots)
                 if sh.get("path") and os.path.exists(str(sh.get("path")))), {})
    anchor = str(last.get("path") or "")
    web = str(last.get("url") or "") or str(state.get("current_image_url") or "")
    inside = f"You are inside {name} now."
    if rec.get("why"):
        inside += f" {rec['why']}"
    with _LOCK:
        _DONE.add(str(state.get("level_goal") or ""))
    if not state.get("goal_reached_turn"):
        state["goal_reached_turn"] = int(state.get("turn_count") or 0) + 1
    state["goal_inside"] = name
    # The flipbook's own anchors. The history row below is the handoff for the
    # still path, but a flipbook turn — every turn, by default — takes its
    # START KEYFRAME from ``flipbook_last_frame`` and its WIDER VIEW from
    # ``flipbook_first_frame``, and never looks at history while those exist.
    # Left alone they were the last turn BEFORE the reward: outside the place.
    # So "Take the shear", the first thing done inside, was drawn continuing
    # the frame outside the rig, and the room the reward had just walked the
    # player into reached the grid nowhere. The World stitch clears these
    # (engine._WORLD_SCOPED_KEYS); the reward re-points them at the room.
    # The last panel is the start keyframe and there is no wider view: tried
    # with the reward's third panel in that slot, real renders off the
    # 2026-09-23 bug frame, and panel 1 re-framed to a wide behind the player
    # instead of the next instant of the close-up on screen — the cut this
    # whole fix is against. With the keyframe alone panel 1 is that instant.
    # What vision last read off the screen goes with them: SCAN and the
    # narrator treat it as the picture in front of the player.
    # With no panel to stand in, nothing moved on screen and the old anchors
    # are still the truth.
    if anchor:
        state["flipbook_last_frame"] = anchor
        for key in ("flipbook_first_frame", "flipbook_last_grid", "current_sequence",
                    "current_observed_vision"):
            state.pop(key, None)
        state["current_image_prompt"] = f"inside {name}"
        state["scene_objects"] = []
        state["scene_objects_turn"] = -1
    if web:
        state["current_image_url"] = web
        tape = state.get("tape_frames")
        if isinstance(tape, list):
            tape.append(web)
            state["tape_frames"] = tape[-400:]
    # THE TAPE: the goal's cutscene, and the line that says where you are.
    try:
        import run_tape
        run_tape.chapter(session_id, name, state)
        run_tape.record(session_id, "montage", [sh.get("path") or sh.get("url") for sh in shots],
                        prose=inside, turn=state.get("turn_count"), state=state)
    except Exception as _tape_err:
        print(f"[TAPE] goal cutscene not recorded: {_tape_err}", flush=True)
    # The run's situation, so later beats are written from in here.
    wp = str(state.get("world_prompt") or "").strip()
    state["world_prompt"] = (wp + f"\n\nThe player has reached {name} and is "
                             f"inside it now. What happens next happens in "
                             f"here, and getting back out with what they came "
                             f"for is the run.").strip()
    try:
        hist = engine._load_history(session_id) or []
        hist.append({
            "choice": "__goal_reached__",
            "dispatch": inside,
            "vision_dispatch": inside,
            "vision_analysis": "",
            "world_prompt": state.get("world_prompt") or "",
            "setting_type": "indoor",
            "image": anchor,
            "image_url": anchor,
            "analysis_image": anchor,
            "guide_image": anchor,
            "image_prompt": f"inside {name}",
            "hard_transition": True,
            "cached_opening": bool(anchor),
            # Only the shots from the way in onward: panels 1 and 2 are the
            # outside, and a turn that cuts out of the room rides these in as
            # "the place the montage established" (engine opening_montage_refs).
            "montage_refs": [str(sh.get("path") or "") for sh in shots[2:]
                             if sh.get("path") and os.path.exists(str(sh.get("path")))
                             and str(sh.get("path")) != anchor],
            "goal_reached": True,
        })
        engine._save_history(hist, session_id)
    except Exception as e:
        print(f"[GOAL] reward history row failed: {e}", flush=True)
    print(f"[GOAL] reward played for {name!r}: the run continues inside it "
          f"(anchor {os.path.basename(anchor) if anchor else 'none'})", flush=True)
    # NOT done yet. They are in the room; what they came for is in here with
    # them, and taking it is the payoff (take_prize). The tag now points at
    # the thing rather than the building.
    # Only if there IS something in there to take. A goal drafted before the
    # prize existed (or a draft that came back without one) would otherwise
    # land in a phase with nothing to pick up, and the tag would point at the
    # building the player is standing in — seen in the loop playtest as
    # "prize='Reinforced Blast Door' verb=''".
    pz = prize(state, make=True)
    if pz:
        state["goal_phase"] = "prize"
        inside = f"{inside} {pz['name']} is here."
    else:
        print(f"[GOAL] {name!r} had nothing in it to take — "
              "finishing on arrival", flush=True)
        nxt = advance(state, session_id)
        return {"name": name, "image_url": web, "inside": inside,
                "anchor": anchor, "board": standing(state), "phase": "place",
                "prize": None, **nxt}
    out = {"name": name, "image_url": web, "inside": inside, "anchor": anchor,
           "board": standing(state), "phase": "prize",
           "prize": pz or None}
    return out


def take_prize(state: dict, session_id: str = "default") -> Dict[str, Any]:
    """They put their hands on it. THAT is the goal done — the board moves and
    the run is handed its next place, drafted from what just happened here."""
    pz = prize(state)
    rec = record(state)
    with _LOCK:
        _DONE.add(str(state.get("level_goal") or ""))
    if not state.get("goal_reached_turn"):
        state["goal_reached_turn"] = int(state.get("turn_count") or 0) + 1
    state["goal_phase"] = "taken"
    # INTO THE PACK. The thing was designed by the look book, shot on the
    # props sheet, and stood lit in the room they just walked into — it does
    # not evaporate into a name on a board.
    row = _row_for(pz.get("name"), session_id) if pz.get("kind") else {}
    got = {"name": pz.get("name", ""), "kind": pz.get("kind", ""),
           "tier": str(row.get("tier") or ("treasure" if pz.get("kind") else "")),
           "power": pz.get("power", ""), "look": pz.get("look", ""),
           "worth": str(row.get("worth") or "")[:220],
           "use": str(row.get("use") or "")[:220],
           "plate": pz.get("plate", ""), "from": rec.get("name", ""),
           "source": "goal", "turn": int(state.get("turn_count") or 0)}
    pack = held(state)
    if got["name"] and not any(_norm_name(g["name"]) == _norm_name(got["name"])
                               for g in pack):
        pack.append(got)
    state["gear"] = pack
    if got["name"]:
        got = _stow(state, got)
    pack = held(state)
    print(f"[GOAL] {pz.get('name') or 'the prize'!r} "
          f"({pz.get('kind') or 'prize'}) taken out of {rec.get('name')!r} — "
          f"goal complete; carrying {len(pack)}", flush=True)
    nxt = advance(state, session_id, took=pz.get("name", ""))
    out = {"took": pz.get("name", ""), "of_place": rec.get("name", ""),
           "gear": got, "pack": held(state),
           "board": standing(state), **nxt}
    return out


def _carried(st: dict, took: str = "") -> List[str]:
    """Everything this run has walked out with, newest last. Kept on the run so
    the next draft can be told not to hand them a fourth key."""
    got = [str(n) for n in (st.get("goals_taken") or []) if str(n).strip()]
    if took and took not in got:
        got.append(took)
    st["goals_taken"] = got
    return got


def standing(state: Optional[dict]) -> Dict[str, Any]:
    """Where this run is in its list of goals: {done, of, index, names}."""
    st = state if isinstance(state, dict) else {}
    names = [str(n) for n in (st.get("goals_done") or []) if str(n).strip()]
    return {"done": len(names), "of": GOALS_TO_WIN, "index": len(names) + 1,
            "names": names, "victory": bool(st.get("run_victory"))}


def advance(state: dict, session_id: str = "default", took: str = "") -> Dict[str, Any]:
    """One goal is behind them. Hand the run the next one, or call it won.

    The goal that was just reached goes on the board, its record is cleared off
    the run, and a new one is drafted FROM WHERE THEY NOW STAND — inside the
    place they just took — so the next thing on the horizon is a thing this
    room can see. The third one ends the run in victory instead."""
    st = state if isinstance(state, dict) else {}
    rec = record(st)
    done_name = rec.get("name") or ""
    names = [str(n) for n in (st.get("goals_done") or []) if str(n).strip()]
    if done_name and done_name not in names:
        names.append(done_name)
    st["goals_done"] = names
    # THE PLACE IS TAKEN, SO THE WORLD BREATHES OUT. Nothing in the game decays
    # threat_level, and every walk puts a floor under it — so without this the
    # second goal began at critical and the third was unplayable. It does not
    # reset: each goal taken leaves the run a little hotter than the last, so
    # three goals still escalate, by design rather than by accumulation.
    try:
        import engine as _eng
        esc = int(getattr(_eng, "STORY_ESCALATE_AT", 3))
        cooled = max(0, esc - 1 + max(0, len(names) - 1))
        st["threat_level"] = min(int(st.get("threat_level", 0) or 0), cooled)
        st["current_phase"] = _eng._phase_for_threat(int(st["threat_level"]))
    except Exception:
        pass
    st.pop("goal_pressure", None)
    # A new walk gets its own roadside fight (encounter.leg_is_full).
    st.pop("goal_leg_fights", None)
    if len(names) >= GOALS_TO_WIN:
        st["run_victory"] = True
        st["goal_victory_turn"] = int(st.get("turn_count") or 0)
        print(f"[GOAL] {len(names)}/{GOALS_TO_WIN} — the run is won: "
              f"{' | '.join(names)}", flush=True)
        return {"victory": True, "done": len(names), "of": GOALS_TO_WIN,
                "names": names}
    inside = done_name or str(st.get("goal_inside") or "")
    for k in ("goal_name", "goal_why", "goal_look", "level_goal",
              "goal_reached_turn", "goal_boss_defeated", "goal_inside",
              "goal_phase", *SPINE_KEYS, *BOSS_KEYS, *PRIZE_KEYS):
        st.pop(k, None)
    with _LOCK:
        _APPROACH.clear()
    lore = ""
    try:
        import experience_store as _xs
        lore = _xs.lore_brief() or ""
    except Exception:
        lore = ""
    # WHAT IS IN THERE IS DECIDED FIRST. The place is then drafted as the
    # place that holds it, which is a better reason for somewhere to exist
    # than an object invented to suit a building that was invented first.
    gear = draw_gear(st, session_id)
    drafted = {}
    try:
        drafted = invent(lore=lore, world_prompt=str(st.get("world_prompt") or ""),
                         holds=as_prize(gear),
                         from_inside=inside, took=took or "",
                         after=_last_beat(session_id),
                         done=names, carried=_carried(st, took),
                         session_id=session_id,
                         # goal 2 of 3 is the escalating leg, goal 3 the
                         # critical one — the same order look_book's CONFLICT
                         # row is already written in.
                         phase=PHASES[min(len(names), len(PHASES) - 1)]) or {}
    except Exception as e:
        print(f"[GOAL] next goal draft failed: {e}", flush=True)
    if not drafted.get("name"):
        return {"next": "", "done": len(names), "of": GOALS_TO_WIN, "names": names}
    if gear:
        drafted["prize"] = as_prize(gear)
    line = install(st, drafted)
    print(f"[GOAL] {len(names)}/{GOALS_TO_WIN} done — next is "
          f"{drafted['name']!r} (from inside {inside!r})", flush=True)
    return {"next": drafted["name"], "why": drafted.get("why", ""),
            "line": line, "done": len(names), "of": GOALS_TO_WIN, "names": names}


def _plate_url(path: Any) -> str:
    """A props-sheet plate as the client can fetch it. The book lives outside
    the session's images folder, so it goes out through the engine's own
    resolver rather than as a disk path the browser cannot open."""
    p = str(path or "").strip()
    if not p:
        return ""
    if p.startswith("/api/") or p.startswith("http"):
        return p
    try:
        import look_book
        return look_book.plate_url(p) or ""
    except Exception:
        return ""


def _gear_card(st: dict) -> Optional[Dict[str, str]]:
    """The thing in this room, for the tag and the take card."""
    if phase(st) != "prize":
        return None
    pz = prize(st)
    if not pz or not pz.get("name"):
        return None
    return {"name": pz["name"], "kind": pz.get("kind", ""),
            "power": pz.get("power", ""), "verb": pz.get("verb", "Take"),
            "plate": _plate_url(pz.get("plate"))}


def _pack_card(g: dict) -> Dict[str, str]:
    card = {"name": g.get("name", ""), "kind": g.get("kind", ""),
            "tier": g.get("tier", ""),
            "power": g.get("power", ""), "worth": g.get("worth", ""),
            "from": g.get("from", ""), "source": g.get("source", "goal"),
            "plate": _plate_url(g.get("plate"))}
    # A character's own item: its id (WEAR / TAKE OFF name it), where it goes
    # on the body, whether it is on, and its plate out of the character's
    # folder rather than a look book that may belong to another World.
    if g.get("character_item"):
        card.update({"id": g.get("character_item"), "slot": g.get("slot", ""),
                     "wearable": bool(g.get("slot")), "worn": bool(g.get("worn"))})
        try:
            import characters
            cid = _character_of(g)
            if cid and g.get("plate"):
                card["plate"] = characters.file_url(cid, "items/" + Path(str(g["plate"])).name) or card["plate"]
        except Exception:
            pass
    return card


def _character_of(g: dict) -> str:
    """Which character's folder an item's plate is in (…/characters/<id>/items/…)."""
    parts = Path(str(g.get("plate") or "")).parts
    return parts[-3] if len(parts) >= 3 and parts[-2] == "items" else ""


def _pack_body(st: dict, sid: str) -> Dict[str, Any]:
    """What every answer carries about the pack, so the client never has a
    reason to keep its own copy."""
    return {"pack": pack_cards(st, sid), "world_gear": world_gear(st, sid)}


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
        # THE RUN IS WON. Three places walked to, fought for and taken —
        # "victory is surviving to get to the 3 goals". There is no fourth, so
        # there is no tag and no name in the corner: only the board, and the
        # card the client holds over the picture. Answered before `adopt`,
        # which would otherwise hand a won run its last goal back.
        if st.get("run_victory"):
            return jsonify({"ok": True, "goal": False, "victory": True,
                            "board": standing(st), **_pack_body(st, sid)})
        rec = record(st)
        if not rec["name"]:
            rec = adopt(st)
        if not rec["name"]:
            return jsonify({"ok": True, "goal": False, **_pack_body(st, sid)})
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
        if body.get("take"):
            if phase(st) != "prize":
                return jsonify({"ok": False, "reason": "not_inside"})
            with engine.WORLD_STATE_LOCK:
                live = engine._load_state(sid) or {}
                out = take_prize(live, sid)
                engine._save_state(live, sid)
                engine._sync_ambient_state(live, sid)
            out["gear"] = _pack_card(out.get("gear") or {}) if out.get("gear") else None
            out["pack"] = [_pack_card(g) for g in (out.get("pack") or [])]
            out["world_gear"] = world_gear(live, sid)
            return jsonify({"ok": True, "goal": True, **out})
        if body.get("payoff_seen"):
            with _LOCK:
                _PAID.add(str(st.get("level_goal") or ""))
            return jsonify({"ok": True})
        if body.get("approach"):
            # The step is counted against the LIVE state (its turn_count is the
            # one the turn loop is writing; the state on disk lags it, and
            # counting there froze every run at step 1/3 — "its stuck, unable
            # to progress on the goal when it clicks"). The pressure the step
            # puts on the world is then written to the persisted copy.
            step = approach(st, final=bool(body.get("final")))
            # THE BOSS IS THE DOOR. One step out, whoever holds the place comes
            # out to meet them — once. The fight is the last thing between the
            # player and the way in, which is the shape that played well; it is
            # not behind the ENTER click, which is the shape that did not.
            line_now = str(st.get("level_goal") or "")
            b_now = boss(st, make=True) if step["steps"] >= APPROACH_STEPS - 1 else {}
            if b_now and line_now not in _MET and not _DONE.intersection({line_now}):
                with _LOCK:
                    first = line_now not in _MET
                    if first:
                        _MET.add(line_now)
                if first:
                    step["stage_boss"] = {"label": b_now["name"], "kind": b_now.get("kind", "person")}
                    print(f"[GOAL] {b_now['name']!r} steps out at "
                          f"{rec['name']!r} (step {step['steps']}/{step['of']})",
                          flush=True)
            try:
                with engine.WORLD_STATE_LOCK:
                    live = engine._load_state(sid) or {}
                    press(live, step["steps"])
                    bind_gear(live, sid)
                    engine._save_state(live, sid)
                    engine._sync_ambient_state(live, sid)
            except Exception as _press_err:
                print(f"[GOAL] pressure not saved: {_press_err}", flush=True)
            print(f"[GOAL] approach {rec['name']}: step {step['steps']}/{step['of']} "
                  f"on turn {st.get('turn_count')}", flush=True)
            return jsonify({"ok": True, "goal": True, "name": rec["name"], **step})
        p = progress(st)
        line_key = str(st.get("level_goal") or "")
        b = boss(st, make=arrived(st))
        with _LOCK:
            won = _WON.get(line_key) or str(st.get("goal_boss_defeated") or "")
            # The boss down opens the door; it does not finish the goal.
            done = line_key in _DONE
        board = standing(st)
        tgt = target(st)
        out = {"ok": True, "goal": True, "name": tgt["name"] or rec["name"],
               "place": rec["name"], "phase": phase(st), "verb": tgt.get("verb") or "",
               "why": tgt.get("why") or rec["why"],
               "reached": arrived(st) and phase(st) != "prize",
               "steps": p["steps"], "of": p["of"],
               "completed": done, "boss_won": won, "board": board,
               "victory": board["victory"],
               "payoff_seen": line_key in _PAID,
               "boss": ({"name": b["name"], "kind": b["kind"]} if b else None),
               # What is in this room and what is already in the pack, so the
               # tag can say "Wear the Tidal Anchor" and the pack can show it.
               "gear": _gear_card(st),
               **_pack_body(st, sid),
               "found": False, "box": None, "src": src}
        if out["reached"]:
            out["reached_line"] = _reached_line(sid)
        if not src:
            return jsonify(out)
        path = engine._resolve_image_path(src, sid)
        path = str(path) if path else ""
        if not path or not os.path.exists(path):
            return jsonify(out)
        cache_key = os.path.normcase(os.path.abspath(path)) + "|" + tgt["name"]
        with _LOCK:
            hit = _BY_FILE.get(cache_key)
        if hit is None:
            hit = locate(path, {"name": tgt["name"], "look": tgt.get("look", "")})
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
