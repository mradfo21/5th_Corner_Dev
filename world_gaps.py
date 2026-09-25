"""A World is played from what it carries. GENERATE fills in what it lacks.

Asked: "SWAT has no world bible, so its opening montage uses the built-in
scene descriptions. THE FIFTH CORNER has no level name. Why aren't these
fixed in the generation process?"

They weren't, because nothing in generation wrote either one. A World made in
the editor starts as the blank place: its bible (`world_initial_state`) is the
266-character harness line about a camera following a person, and its level
name is whatever the node was called ("World"). The author then fills in the
Level sheet and the Experience lore, and every consumer reads around the hole:
  * the opening montage wants 400+ characters of bible to write its shotlist,
    and falls back to stock establishing briefs;
  * the narrator gets the harness line as its whole sense of place;
  * the montage and the HUD title the level "World".

`fill(slug)` runs when GENERATE binds a World and when a run is prepared. If
the bound World's bible is thin or its level name is a placeholder, one text
call drafts them from what the author did write: the Level sheet, the
character, the Experience lore (via `_ask`'s lore), and the thin bible itself.
The draft goes INTO the World's file and the live sheet, so the editor shows
it and the author can rewrite it; it is never regenerated once there. The
author's own bible text is kept word for word, after the drafted premise.
Never fatal: any failure leaves the World as it was.
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any, Dict, List

# The montage's own threshold (cutscene._shotlist_from_bible): under this
# there is nothing to read.
BIBLE_MIN = 400

# Names a World gets from the editor, not from its author.
_PLACEHOLDER = re.compile(
    r"^\s*(|world|new world|new level|level|untitled|untitled world|untitled level|"
    r"untitled experience|an open place|somewhere new)(\s*[-#]?\s*\d+)?\s*$",
    re.I)

_LOCK = threading.Lock()

_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "level_name": {"type": "STRING"},
        "premise": {"type": "STRING"},
    },
    "required": ["level_name", "premise"],
}


def _setting(prompts: Dict[str, Any]) -> Dict[str, Any]:
    s = prompts.get("setting_reference")
    return s if isinstance(s, dict) else {}


def is_placeholder_name(name: Any) -> bool:
    return bool(_PLACEHOLDER.match(str(name or "")))


def gaps(prompts: Dict[str, Any]) -> List[str]:
    """What this World lacks: "bible" and/or "level_name"."""
    out = []
    if len(str(prompts.get("world_initial_state") or "").strip()) < BIBLE_MIN:
        out.append("bible")
    if is_placeholder_name(_setting(prompts).get("name")):
        out.append("level_name")
    return out


def _brief(prompts: Dict[str, Any]) -> str:
    s = _setting(prompts)
    c = prompts.get("player_character") if isinstance(prompts.get("player_character"), dict) else {}
    lines = []
    for k in ("summary", "goal", "era", "palette", "landmarks", "opening_shot"):
        v = str(s.get(k) or "").strip()
        if v:
            lines.append(f"LEVEL {k.upper()}: {v}")
    who = ", ".join(str(c.get(k) or "").strip() for k in ("name", "role") if str(c.get(k) or "").strip())
    if who:
        lines.append(f"PROTAGONIST: {who}")
    return "\n".join(lines)


def _prompt(prompts: Dict[str, Any], need: List[str]) -> str:
    thin = str(prompts.get("world_initial_state") or "").strip()
    return (
        "You are the story editor for a generative adventure game. A designer "
        "authored the sheet below (and any lore attached above) but left gaps. "
        "Fill them from what IS authored; invent nothing that contradicts it.\n\n"
        f"{_brief(prompts)}\n"
        + (f"CURRENT BIBLE (keep its intent): {thin}\n" if thin else "")
        + "\nReturn JSON:\n"
        "- level_name: the name of THIS place, 2-5 words, the way a title card "
        "would print it (a location, not a sentence, no quotes"
        + (", keep it empty if not needed" if "level_name" not in need else "")
        + ").\n"
        "- premise: the world bible the narrator plays from, 1800-2800 "
        "characters, plain prose, no headings. Cover: the genre and tone; what "
        "happened here and what is at stake; the factions and who the player "
        "will run into from the first minute; the look of the place (era, "
        "materials, light, weather, palette); the dangers and how death comes; "
        "one unanswered question the run is about. Second person where it "
        "addresses the player."
        + (" Keep it empty if not needed." if "bible" not in need else "")
    )


def _parse(raw: str) -> Dict[str, str]:
    raw = re.sub(r"^```[a-z]*\s*|\s*```$", "", str(raw or "").strip())
    try:
        got = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        got = json.loads(m.group(0)) if m else {}
    return got if isinstance(got, dict) else {}


def fill(slug: str, reason: str = "generate") -> Dict[str, Any]:
    """Draft whatever the bound World lacks; write it into the World and live.

    Returns the fields written ({} when nothing was missing or it failed).
    """
    try:
        import prompts_store
        import worlds_store
    except Exception:
        return {}
    if not slug or worlds_store.bound_slug() != worlds_store._slug(slug):
        return {}
    with _LOCK:
        prompts = dict(prompts_store.PROMPTS)
        need = gaps(prompts)
        if not need:
            return {}
        print(f"[WORLD GAPS] '{slug}' ({reason}) lacks {', '.join(need)} — drafting from "
              "its Level sheet and lore", flush=True)
        try:
            import engine
            raw = engine._ask(_prompt(prompts, need), temp=0.8, tokens=2000,
                              use_lore=True, response_schema=_SCHEMA)
            got = _parse(raw)
        except Exception as err:  # noqa: BLE001
            print(f"[WORLD GAPS] draft failed ({err}); the World plays as authored", flush=True)
            return {}
        fields: Dict[str, Any] = {}
        name = " ".join(str(got.get("level_name") or "").split()).strip(" \"'.")
        if "level_name" in need and name and not is_placeholder_name(name) and len(name) <= 60:
            setting = dict(_setting(prompts))
            setting["name"] = name
            fields["setting_reference"] = setting
        premise = str(got.get("premise") or "").strip()
        if "bible" in need and len(premise) >= BIBLE_MIN:
            thin = str(prompts.get("world_initial_state") or "").strip()
            fields["world_initial_state"] = premise + (f"\n\n{thin}" if thin else "")
        if not fields:
            print("[WORLD GAPS] the draft came back unusable; the World plays as authored", flush=True)
            return {}
        # The World first, so a rebind (the reset's) finds it; then the live sheet.
        worlds_store.patch_world_prompts(slug, fields)
        prompts_store.save_prompts_bulk(fields)
        wrote = []
        if "setting_reference" in fields:
            wrote.append(f"level name '{fields['setting_reference']['name']}'")
        if "world_initial_state" in fields:
            wrote.append(f"a {len(fields['world_initial_state'])}-character bible")
        print(f"[WORLD GAPS] '{slug}': wrote {' and '.join(wrote)} into the World", flush=True)
        return fields
