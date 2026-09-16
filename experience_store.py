"""
experience_store.py — an Experience is a graph of Worlds + Cutscenes + Transitions.

A World is a playable identity (the live prompt set + cast sheet). Those
snapshots already live in ``worlds_store``; this module is the graph that
stitches them: which worlds exist in this Experience, which one a run starts
in, the directed transitions between them, Cutscene nodes (4-shot cinematic
montages derived from a plate), the shared Lore node (background notes and
files every World in the run can draw on), and pacing (the story clock that
fills ``{beat_nudge}`` on the choice slate).

Conditions are an enum (``CONDITION_CATALOG``). Add a row there to grow
the Type dropdown; ``condition_met`` stays False until that id has a hook.
Shipped types today:

- ``turn_count`` — after N turns *in this world*, leave for the target.
- ``game_over`` — the current world's death verdict (``player_alive`` is false).
- ``immediate`` — fire as soon as this node is left (Cutscene → World).

Each Experience is ``experiences/<slug>.json``. Play / Watch / reset read
the *active* slug from disk the same way they read the live prompt file —
not from localStorage. ``experiences/default.json`` always exists; the
picker can switch which file is active.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import worlds_store

ROOT = Path(__file__).parent.resolve()
EXPERIENCES_DIR = ROOT / "experiences"
SESSIONS_DIR = ROOT / "sessions"
ACTIVE_SLUG = "default"
# The shipped Play door. worlds/somewhere.json + this Experience are the
# Phase 0 freeze of the Horizon demo. Empty ``.active`` binds here when the
# file exists so Play cannot silently fall back to a blank Untitled graph.
SHIPPED_SLUG = "somewhere"

# Author-facing Type enum. The editor dropdown is this list; extra fields
# (turns) paint from ``fields``. Add a dict here when a new hook is ready.
CONDITION_CATALOG: Tuple[Dict[str, Any], ...] = (
    {
        "id": "turn_count",
        "label": "After N turns",
        "short": "",
        "hint": "Leave after this many turns in the current World.",
        "fields": ("turns",),
    },
    {
        "id": "game_over",
        "label": "On death",
        "short": "death",
        "hint": "When the run ends here, continue in the next World.",
        "fields": (),
    },
    {
        "id": "immediate",
        "label": "Immediately",
        "short": "now",
        "hint": "Fire as soon as this node is left. Used for Cutscene → World.",
        "fields": (),
    },
)
CUTSCENE_MOODS = ("threshold", "aftermath", "arrival", "departure", "encounter")
CUTSCENE_SOURCES = ("incoming", "dest_world")
CONDITION_TYPES = tuple(row["id"] for row in CONDITION_CATALOG)


def condition_catalog() -> List[Dict[str, Any]]:
    """JSON-safe Type enum for the editor dropdown."""
    out: List[Dict[str, Any]] = []
    for row in CONDITION_CATALOG:
        out.append({
            "id": row["id"],
            "label": row["label"],
            "short": row.get("short") or "",
            "hint": row.get("hint") or "",
            "fields": list(row.get("fields") or ()),
        })
    return out
SOUND_PALETTES = ("tape", "quiet", "silent")
SOUND_FAMILIES = ("clicks", "chrome", "turn", "lens", "body", "voice")
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_LORE_TEXT_EXTS = {".md", ".txt"}

# Product (SOMEWHERE) clock. A new untitled Experience starts slower.
PRODUCT_ESCALATE_AT = 4
PRODUCT_CRITICAL_AT = 9
HARNESS_ESCALATE_AT = 8
HARNESS_CRITICAL_AT = 20
DEFAULT_BEAT_ESCALATING = (
    "BEAT: pressure is rising. Push the situation forward."
)
DEFAULT_BEAT_CRITICAL = (
    "BEAT: the situation is critical. Offer a way through or a last stand."
)
# The opening act used to send the choice slate nothing at all — calm turns
# fell through to "" and the slate leaned on whatever generic forward-motion
# bias it already had, so turn one looked exactly like a quiet turn fifteen
# minutes in: no one on screen, no reason to be afraid yet. A cold open needs
# its own line just as much as the two escalation marks do.
DEFAULT_BEAT_NORMAL = (
    "BEAT: put a character or a direct threat in view early — a patrol, a "
    "figure, a voice — don't let the opening stay empty."
)
_BEAT_MAX = 500
_LORE_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_LORE_MAX_DOCS = 40
_LORE_MAX_NOTES = 80_000
_LORE_MAX_TEXT = 200_000
_LORE_MAX_IMAGE = 8 * 1024 * 1024
# Horizon-scale story bibles are ~9k; leave room for extra notes/files.
# This brief is appended to the world document every turn, and the world
# document is read by every prompt in the game, so at 48k the ceiling was not a
# ceiling — one Experience's notes field was 9.4k characters on its own and made
# up two thirds of the document. Bounded so the living world state, which is the
# part that actually changes between turns, cannot be crowded out.
_LORE_BRIEF_CAP = int(os.getenv("LORE_BRIEF_CAP", "6000"))
_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_LORE_NAME_RE = re.compile(r"[^a-zA-Z0-9._ -]+")

_LOCK = threading.Lock()


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:8]


def _ensure_dir() -> None:
    EXPERIENCES_DIR.mkdir(parents=True, exist_ok=True)


def _safe_slug(raw: Any) -> str:
    s = _SLUG_RE.sub("-", str(raw or "").strip()).strip("-").lower()
    return s[:64]


def _path(slug: str = "") -> Path:
    return EXPERIENCES_DIR / f"{(_safe_slug(slug) or ACTIVE_SLUG)}.json"


def _active_path() -> Path:
    return EXPERIENCES_DIR / ".active"


def shipped_experience_exists() -> bool:
    return _path(SHIPPED_SLUG).exists()


def factory_slug() -> str:
    """Play's door when no ``.active`` pointer is set."""
    if shipped_experience_exists():
        return SHIPPED_SLUG
    return ACTIVE_SLUG


def get_active_slug() -> str:
    """Which Experience file Play / the editor currently bind to."""
    _ensure_dir()
    try:
        raw = _active_path().read_text(encoding="utf-8").strip()
    except Exception:
        raw = ""
    slug = _safe_slug(raw) or factory_slug()
    if slug != factory_slug() and not _path(slug).exists():
        return factory_slug()
    return slug


def _resolve_slug(slug: str = "") -> str:
    return _safe_slug(slug) or get_active_slug()


def set_active(slug: str) -> str:
    """Point Play / reset at this Experience file. Creates ``default`` if needed."""
    slug = _safe_slug(slug) or ACTIVE_SLUG
    path = _path(slug)
    if not path.exists():
        if slug != ACTIVE_SLUG:
            raise KeyError(f"Experience '{slug}' not found.")
        get_experience(slug)
    _ensure_dir()
    with _LOCK:
        _active_path().write_text(slug, encoding="utf-8")
    return slug


def _clamp_turns(raw: Any) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 8
    return max(1, min(n, 999))


def normalize_condition(raw: Any) -> Dict[str, Any]:
    """Drop unknown condition types rather than inventing flags."""
    if not isinstance(raw, dict):
        return {"type": "turn_count", "turns": 8}
    typ = str(raw.get("type") or "turn_count").strip()
    if typ not in CONDITION_TYPES:
        typ = "turn_count"
    out: Dict[str, Any] = {"type": typ}
    if typ == "turn_count":
        out["turns"] = _clamp_turns(raw.get("turns"))
    return out


def condition_met(
    condition: Any,
    *,
    world_turn_count: int,
    player_alive: bool = True,
) -> bool:
    """Pure evaluation. The engine is the only caller that mutates state."""
    cond = normalize_condition(condition)
    typ = cond["type"]
    if typ == "turn_count":
        return int(world_turn_count) >= int(cond["turns"])
    if typ == "game_over":
        return not bool(player_alive)
    if typ == "immediate":
        return True
    return False


def _as_float(raw: Any) -> Optional[float]:
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _normalize_world(raw: Any, index: int = 0) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    wid = str(raw.get("id") or "").strip() or _new_id("w")
    name = str(raw.get("name") or "").strip() or (f"World {index + 1}" if index else "World")
    slug = str(raw.get("slug") or "").strip()
    out: Dict[str, Any] = {"id": wid, "name": name, "slug": slug}
    blurb = str(raw.get("blurb") or "").strip()
    if blurb:
        out["blurb"] = blurb
    x = _as_float(raw.get("x"))
    y = _as_float(raw.get("y"))
    if x is not None:
        out["x"] = x
    if y is not None:
        out["y"] = y
    return out


def _normalize_cutscene(raw: Any, index: int = 0) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    cid = str(raw.get("id") or "").strip() or _new_id("c")
    name = str(raw.get("name") or "").strip() or (
        f"Cutscene {index + 1}" if index else "Cutscene"
    )
    mood = str(raw.get("mood") or "threshold").strip().lower()
    if mood not in CUTSCENE_MOODS:
        mood = "threshold"
    source = str(raw.get("source") or "incoming").strip().lower()
    if source not in CUTSCENE_SOURCES:
        source = "incoming"
    out: Dict[str, Any] = {
        "id": cid,
        "name": name,
        "mood": mood,
        "source": source,
    }
    blurb = str(raw.get("blurb") or "").strip()
    shot_brief = str(raw.get("shot_brief") or "").strip()
    if blurb:
        out["blurb"] = blurb
    if shot_brief:
        out["shot_brief"] = shot_brief
    x = _as_float(raw.get("x"))
    y = _as_float(raw.get("y"))
    if x is not None:
        out["x"] = x
    if y is not None:
        out["y"] = y
    return out


def _normalize_transition(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    src = str(raw.get("from") or "").strip()
    dst = str(raw.get("to") or "").strip()
    if not src or not dst or src == dst:
        return None
    return {
        "id": str(raw.get("id") or "").strip() or _new_id("t"),
        "from": src,
        "to": dst,
        "condition": normalize_condition(raw.get("condition")),
    }


def default_sound() -> Dict[str, Any]:
    """Tape is the shipped palette: dry mechanical, no arcade chimes."""
    return {"palette": "tape", "muted": [], "volume": 1.0}


def normalize_sound(raw: Any) -> Dict[str, Any]:
    """UI synth settings for this Experience. Unknown keys drop away."""
    base = default_sound()
    if not isinstance(raw, dict):
        return base
    pal = str(raw.get("palette") or base["palette"]).strip().lower()
    if pal not in SOUND_PALETTES:
        pal = base["palette"]
    muted: List[str] = []
    seen = set()
    for item in raw.get("muted") or []:
        fam = str(item or "").strip().lower()
        if fam in SOUND_FAMILIES and fam not in seen:
            seen.add(fam)
            muted.append(fam)
    try:
        vol = float(raw.get("volume", base["volume"]))
    except (TypeError, ValueError):
        vol = base["volume"]
    return {
        "palette": pal,
        "muted": muted,
        "volume": max(0.0, min(1.0, vol)),
    }


def _safe_lore_name(raw: Any, fallback: str = "note.md") -> str:
    name = _LORE_NAME_RE.sub("", str(raw or "").strip())[:80].strip(" .")
    return name or fallback


def _normalize_lore_doc(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    did = str(raw.get("id") or "").strip()
    if not did or not re.match(r"^l[a-zA-Z0-9]{6,16}$", did):
        return None
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in ("text", "image"):
        return None
    out: Dict[str, Any] = {
        "id": did,
        "name": _safe_lore_name(raw.get("name"), "note.md" if kind == "text" else "plate.png"),
        "kind": kind,
        "file": str(raw.get("file") or "").strip(),
    }
    try:
        out["chars"] = max(0, int(raw.get("chars") or 0))
    except (TypeError, ValueError):
        out["chars"] = 0
    try:
        out["updated"] = float(raw.get("updated") or 0)
    except (TypeError, ValueError):
        out["updated"] = 0.0
    url = str(raw.get("url") or "").strip()
    if url:
        out["url"] = url
    # Shipped Experiences keep the bible in JSON so Play does not depend on
    # gitignored files under experiences/_lore/.
    if kind == "text":
        inline = str(raw.get("text") or "")
        if len(inline) > _LORE_MAX_TEXT:
            inline = inline[:_LORE_MAX_TEXT]
        if inline:
            out["text"] = inline
            if not out["chars"]:
                out["chars"] = len(inline)
    return out


def normalize_lore(raw: Any) -> Dict[str, Any]:
    """Keep only fields the graph and the run can actually use."""
    base = default_lore()
    if not isinstance(raw, dict):
        return base
    notes = str(raw.get("notes") or "")
    if len(notes) > _LORE_MAX_NOTES:
        notes = notes[:_LORE_MAX_NOTES]
    docs: List[Dict[str, Any]] = []
    seen = set()
    for item in raw.get("documents") or []:
        doc = _normalize_lore_doc(item)
        if not doc or doc["id"] in seen:
            continue
        seen.add(doc["id"])
        docs.append(doc)
        if len(docs) >= _LORE_MAX_DOCS:
            break
    out: Dict[str, Any] = {
        "enabled": False if raw.get("enabled") is False else True,
        "notes": notes,
        "documents": docs,
    }
    x = _as_float(raw.get("x"))
    y = _as_float(raw.get("y"))
    if x is not None:
        out["x"] = x
    if y is not None:
        out["y"] = y
    return out


def default_lore() -> Dict[str, Any]:
    """Background the Worlds share. Empty until someone uploads it."""
    return {"enabled": True, "notes": "", "documents": []}


def _clip_beat(raw: Any, fallback: str) -> str:
    text = raw if isinstance(raw, str) else ""
    text = text.strip()
    if not text:
        text = str(fallback or "").strip()
    return text[:_BEAT_MAX]


def _normalize_threat(raw: Any, fallback: Any = None) -> Dict[str, Any]:
    """Story clock + the two beat lines the choice slate hears.

    Missing marks use the product curve (4 / 9). New Experiences pass an
    explicit slower fallback so Create does not inherit SOMEWHERE's sprint.
    """
    src = raw if isinstance(raw, dict) else {}
    fb = fallback if isinstance(fallback, dict) else {}

    def _int(key: str, default: int) -> int:
        for bag in (src, fb):
            if key not in bag or bag[key] in (None, ""):
                continue
            try:
                return int(bag[key])
            except (TypeError, ValueError):
                continue
        return default

    esc = max(1, min(_int("escalate_at", PRODUCT_ESCALATE_AT), 98))
    crit = max(esc + 1, min(_int("critical_at", PRODUCT_CRITICAL_AT), 99))
    return {
        "escalate_at": esc,
        "critical_at": crit,
        "beat_normal": _clip_beat(
            src.get("beat_normal"),
            fb.get("beat_normal") or DEFAULT_BEAT_NORMAL,
        ),
        "beat_escalating": _clip_beat(
            src.get("beat_escalating"),
            fb.get("beat_escalating") or DEFAULT_BEAT_ESCALATING,
        ),
        "beat_critical": _clip_beat(
            src.get("beat_critical"),
            fb.get("beat_critical") or DEFAULT_BEAT_CRITICAL,
        ),
    }


def default_experience() -> Dict[str, Any]:
    wid = "w-live"
    return {
        "id": ACTIVE_SLUG,
        "name": "Untitled Experience",
        "start_world": wid,
        "worlds": [{"id": wid, "name": "World", "slug": ""}],
        "cutscenes": [],
        "transitions": [],
        "sound": default_sound(),
        "lore": default_lore(),
        "threat": {
            "escalate_at": HARNESS_ESCALATE_AT,
            "critical_at": HARNESS_CRITICAL_AT,
        },
    }


def normalize_experience(raw: Any) -> Dict[str, Any]:
    base = default_experience()
    if not isinstance(raw, dict):
        return base
    worlds: List[Dict[str, Any]] = []
    seen = set()
    for i, item in enumerate(raw.get("worlds") or []):
        w = _normalize_world(item, i)
        if not w or w["id"] in seen:
            continue
        seen.add(w["id"])
        worlds.append(w)
    if not worlds:
        worlds = list(base["worlds"])
    cutscenes: List[Dict[str, Any]] = []
    c_seen = set()
    for i, item in enumerate(raw.get("cutscenes") or []):
        c = _normalize_cutscene(item, i)
        if not c or c["id"] in c_seen:
            continue
        c_seen.add(c["id"])
        cutscenes.append(c)
    ids = {w["id"] for w in worlds} | {c["id"] for c in cutscenes}
    transitions: List[Dict[str, Any]] = []
    t_seen = set()
    for item in raw.get("transitions") or []:
        t = _normalize_transition(item)
        if not t or t["id"] in t_seen:
            continue
        if t["from"] not in ids or t["to"] not in ids:
            continue
        t_seen.add(t["id"])
        transitions.append(t)
    start = str(raw.get("start_world") or "").strip()
    if start not in ids:
        start = worlds[0]["id"]
    out: Dict[str, Any] = {
        "id": str(raw.get("id") or "").strip() or ACTIVE_SLUG,
        "name": str(raw.get("name") or "").strip() or base["name"],
        "start_world": start,
        "worlds": worlds,
        "cutscenes": cutscenes,
        "transitions": transitions,
        "sound": normalize_sound(raw.get("sound")),
        "lore": normalize_lore(raw.get("lore")),
        "threat": _normalize_threat(raw.get("threat")),
    }
    x = _as_float(raw.get("x"))
    y = _as_float(raw.get("y"))
    if x is not None:
        out["x"] = x
    if y is not None:
        out["y"] = y
    return out


def get_experience(slug: str = "") -> Dict[str, Any]:
    """Load an Experience, creating a one-world default if needed.

    An empty slug is the active Experience (``experiences/.active``, else
    ``default``).
    """
    slug = _resolve_slug(slug)
    _ensure_dir()
    path = _path(slug)
    with _LOCK:
        if not path.exists():
            exp = default_experience()
            exp["id"] = slug
            path.write_text(json.dumps(exp, indent=2, ensure_ascii=False), encoding="utf-8")
            out = normalize_experience(exp)
            out["id"] = slug
            out["lore"] = _resolve_lore(out)
            return out
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = default_experience()
            data["id"] = slug
    out = normalize_experience(data)
    out["id"] = slug
    out["lore"] = _resolve_lore(out)
    return out


def save_experience(payload: Any, slug: str = "") -> Dict[str, Any]:
    slug = _resolve_slug(slug)
    exp = normalize_experience(payload)
    exp["id"] = slug
    incoming = payload.get("lore") if isinstance(payload, dict) else None
    # Inherited world-bible notes are a read overlay. Do not write them
    # back as if the author typed them (set_sound / add_world used to).
    if isinstance(incoming, dict) and incoming.get("source") == "world":
        kept = normalize_lore(incoming)
        kept["notes"] = ""
        exp["lore"] = kept
    _ensure_dir()
    with _LOCK:
        _path(slug).write_text(
            json.dumps(exp, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    exp["lore"] = _resolve_lore(exp)
    return exp


def set_sound(payload: Any, slug: str = "") -> Dict[str, Any]:
    """Write the UI synth settings without touching Worlds or transitions."""
    exp = get_experience(slug)
    exp["sound"] = normalize_sound(payload)
    return save_experience(exp, slug)


def set_pacing(payload: Any, slug: str = "") -> Dict[str, Any]:
    """Write the story clock and beat lines without touching Worlds."""
    exp = get_experience(slug)
    current = exp.get("threat") if isinstance(exp.get("threat"), dict) else {}
    incoming = payload if isinstance(payload, dict) else {}
    merged = dict(current)
    for key in ("escalate_at", "critical_at", "beat_normal", "beat_escalating", "beat_critical"):
        if key in incoming:
            merged[key] = incoming[key]
    exp["threat"] = _normalize_threat(merged)
    return save_experience(exp, slug)


def _unique_slug(name: str) -> str:
    base = _safe_slug(name) or "experience"
    slug = base
    n = 2
    while _path(slug).exists():
        slug = f"{base}-{n}"[:64]
        n += 1
    return slug


def rename_experience(name: str, slug: str = "") -> Dict[str, Any]:
    """Stamp a display name. The file slug stays put so Play keeps its pointer."""
    slug = _resolve_slug(slug)
    exp = get_experience(slug)
    exp["name"] = str(name or "").strip() or "Untitled Experience"
    return save_experience(exp, slug)


def create_experience(name: str = "", clone_from: str = "") -> Dict[str, Any]:
    """A new Experience file, then make it active so the editor is in it."""
    title = str(name or "").strip() or "Untitled Experience"
    slug = _unique_slug(title)
    if clone_from:
        src = get_experience(clone_from)
        payload = {
            "id": slug,
            "name": title,
            "start_world": src.get("start_world") or "",
            "worlds": list(src.get("worlds") or []),
            "transitions": list(src.get("transitions") or []),
            "sound": src.get("sound") or default_sound(),
            "lore": src.get("lore") or default_lore(),
            "threat": src.get("threat") or {},
        }
    else:
        payload = default_experience()
        payload["id"] = slug
        payload["name"] = title
        blank = worlds_store.create_blank_world(title)
        wid = _new_id("w")
        payload["start_world"] = wid
        payload["worlds"] = [{"id": wid, "name": blank["name"], "slug": blank["slug"]}]
    exp = save_experience(payload, slug)
    if clone_from:
        _copy_lore_files(clone_from, slug)
        exp = get_experience(slug)
    set_active(slug)
    return exp


def _session_frames(slug: str) -> List[Tuple[float, str]]:
    """On-disk stills for this Experience, oldest first. Never generates."""
    slug = _safe_slug(slug) or ACTIVE_SLUG
    frames: List[Tuple[float, str]] = []
    root = SESSIONS_DIR
    if not root.is_dir():
        return frames
    try:
        dirs = list(root.iterdir())
    except OSError:
        return frames
    for d in dirs:
        if not d.is_dir() or d.name.startswith("_") or d.name == "__pycache__":
            continue
        exp_id = ACTIVE_SLUG
        current = None
        state_path = d / "state.json"
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                exp_id = str(state.get("experience_id") or ACTIVE_SLUG).strip() or ACTIVE_SLUG
                current = state.get("current_image_url")
            except Exception:
                pass
        if exp_id != slug:
            continue
        img_dir = d / "images"
        if img_dir.is_dir():
            try:
                files = list(img_dir.iterdir())
            except OSError:
                files = []
            for f in files:
                if not f.is_file() or f.suffix.lower() not in _IMAGE_EXTS:
                    continue
                try:
                    frames.append((f.stat().st_mtime, f"/images/{f.name}"))
                except OSError:
                    continue
        if isinstance(current, str) and current.startswith("/"):
            frames.append((1e18, current.split("?", 1)[0]))
    frames.sort(key=lambda row: row[0])
    seen = set()
    out: List[Tuple[float, str]] = []
    for mtime, url in frames:
        if url in seen:
            continue
        seen.add(url)
        out.append((mtime, url))
    return out


def _world_plate_url(world: Optional[Dict[str, Any]]) -> Optional[str]:
    """Authored setting still for a World snapshot, if one is on disk."""
    if not world:
        return None
    try:
        import game_identity
    except Exception:
        return None
    spec = None
    wslug = str(world.get("slug") or "").strip()
    if wslug:
        try:
            data = worlds_store.get_world(wslug)
            spec = (data.get("prompts") or {}).get(game_identity.SETTING_KEY)
        except Exception:
            spec = None
    if not isinstance(spec, dict):
        try:
            spec = (game_identity.get_spec() or {}).get(game_identity.SETTING_KEY)
        except Exception:
            return None
    for ref_id in (spec or {}).get("reference_images") or []:
        try:
            if game_identity.reference_path(ref_id):
                return game_identity.reference_url(ref_id)
        except Exception:
            continue
    return None


def _render_thumb_url() -> Optional[str]:
    try:
        import render_jobs
        runs = (render_jobs.history() or {}).get("renders") or []
    except Exception:
        return None
    for run in runs:
        thumb = (run or {}).get("thumbnail")
        if thumb:
            return f"/api/render/file/{thumb}"
    return None


def preview_url_for(slug: str, exp: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Start World's cached first frame, else last still, else the level plate."""
    slug = _safe_slug(slug) or ACTIVE_SLUG
    if exp is None:
        try:
            exp = get_experience(slug)
        except Exception:
            exp = None
    start = None
    if isinstance(exp, dict):
        start = landing_world(exp)
        if start is None and (exp.get("worlds") or []):
            start = exp["worlds"][0]
    wslug = str((start or {}).get("slug") or "")
    if wslug:
        try:
            import world_frames
            rec = world_frames.record(wslug)
            url = rec.get("url") or ""
            source = str(rec.get("source") or "")
            # Placeholder and raw reference plates are not the opening scene.
            # When images can run, keep looking for a generated still.
            skip = source in ("placeholder", "plate") and world_frames._images_enabled()
            if url and not skip:
                return url
        except Exception:
            pass
    frames = _session_frames(slug)
    if frames:
        return frames[-1][1]
    plate = _world_plate_url(start)
    if plate:
        return plate
    return _render_thumb_url()


def catalog_entry(slug: str, exp: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    slug = _safe_slug(slug) or ACTIVE_SLUG
    if exp is None:
        exp = get_experience(slug)
    worlds = list(exp.get("worlds") or [])
    return {
        "id": slug,
        "name": str(exp.get("name") or "").strip() or "Untitled Experience",
        "start_world": exp.get("start_world") or "",
        "world_count": len(worlds),
        "preview_url": preview_url_for(slug, exp),
        "active": slug == get_active_slug(),
        "sound": normalize_sound(exp.get("sound")),
    }


def list_experiences(*, play_catalog: bool = False) -> List[Dict[str, Any]]:
    """Every Experience JSON on disk.

    Play's picker should not show this machine's leftover ``default`` graph
    when the shipped SOMEWHERE file exists — that is how two tiles both
    said SOMEWHERE.
    """
    if not play_catalog:
        get_experience(ACTIVE_SLUG)
    items: List[Dict[str, Any]] = []
    seen = set()
    try:
        files = list(EXPERIENCES_DIR.glob("*.json"))
    except OSError:
        files = []
    hide_default = play_catalog and shipped_experience_exists()
    for path in files:
        if path.name.startswith("_"):
            continue
        slug = _safe_slug(path.stem)
        if not slug or slug in seen:
            continue
        if hide_default and slug == ACTIVE_SLUG:
            continue
        seen.add(slug)
        try:
            exp = get_experience(slug)
        except Exception:
            continue
        items.append(catalog_entry(slug, exp))
    if not play_catalog and ACTIVE_SLUG not in seen:
        items.append(catalog_entry(ACTIVE_SLUG, get_experience(ACTIVE_SLUG)))
    items.sort(key=lambda row: (
        row["id"] != SHIPPED_SLUG,
        row["id"] != ACTIVE_SLUG,
        (row.get("name") or "").lower(),
    ))
    return items


def world_by_id(exp: Dict[str, Any], world_id: str) -> Optional[Dict[str, Any]]:
    wid = str(world_id or "").strip()
    for w in exp.get("worlds") or []:
        if w.get("id") == wid:
            return w
    return None


def cutscene_by_id(exp: Dict[str, Any], cutscene_id: str) -> Optional[Dict[str, Any]]:
    cid = str(cutscene_id or "").strip()
    for c in exp.get("cutscenes") or []:
        if c.get("id") == cid:
            return c
    return None


def node_ids(exp: Dict[str, Any]) -> set:
    ids = {w.get("id") for w in (exp.get("worlds") or []) if w.get("id")}
    ids |= {c.get("id") for c in (exp.get("cutscenes") or []) if c.get("id")}
    return ids


def node_kind(exp: Dict[str, Any], node_id: str) -> str:
    if world_by_id(exp, node_id):
        return "world"
    if cutscene_by_id(exp, node_id):
        return "cutscene"
    return ""


def start_node_id(exp: Dict[str, Any]) -> str:
    start = str((exp or {}).get("start_world") or "").strip()
    if start and start in node_ids(exp):
        return start
    worlds = (exp or {}).get("worlds") or []
    return str((worlds[0] or {}).get("id") or "") if worlds else ""


def landing_world(exp: Dict[str, Any], start_id: str = "") -> Optional[Dict[str, Any]]:
    """World PLAY actually loads (prompts + opening frame).

    A Cutscene start still needs a place to land — the first outgoing World,
    else the first World in the Experience.
    """
    sid = str(start_id or start_node_id(exp) or "").strip()
    world = world_by_id(exp, sid)
    if world:
        return world
    for t in transitions_from(exp, sid):
        dest = world_by_id(exp, t.get("to") or "")
        if dest:
            return dest
    worlds = (exp or {}).get("worlds") or []
    return worlds[0] if worlds else None


def opening_cutscene(exp: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Cutscene PLAY should open on.

    Explicit ``start_world`` pointing at a Cutscene wins. Otherwise a Cutscene
    with no World inbound that leads into the start World is the title card —
    the graph Cutscene → Start World without anyone asking to click Start here.
    """
    start = start_node_id(exp)
    cut = cutscene_by_id(exp, start)
    if cut:
        return cut
    if not start:
        return None
    trans = list(exp.get("transitions") or [])
    if any(t.get("to") == start and world_by_id(exp, t.get("from") or "") for t in trans):
        return None
    for t in trans:
        if t.get("to") != start:
            continue
        src_id = str(t.get("from") or "")
        src = cutscene_by_id(exp, src_id)
        if not src:
            continue
        inbound_world = any(
            x.get("to") == src_id and world_by_id(exp, x.get("from") or "")
            for x in trans
        )
        if not inbound_world:
            return src
    return None


def transitions_from(exp: Dict[str, Any], world_id: str) -> List[Dict[str, Any]]:
    wid = str(world_id or "").strip()
    return [t for t in (exp.get("transitions") or []) if t.get("from") == wid]


def matched_transition(
    exp: Dict[str, Any],
    world_id: str,
    *,
    world_turn_count: int,
    player_alive: bool = True,
) -> Optional[Dict[str, Any]]:
    """First outgoing transition whose condition is true. Order is author order."""
    for t in transitions_from(exp, world_id):
        if condition_met(
            t.get("condition"),
            world_turn_count=world_turn_count,
            player_alive=player_alive,
        ):
            return t
    return None


def _unique_world_name(base: str, existing: List[str]) -> str:
    taken = {n.strip().lower() for n in existing if n}
    name = (base or "World").strip() or "World"
    if name.lower() not in taken:
        return name
    n = 2
    while f"{name} {n}".lower() in taken:
        n += 1
    return f"{name} {n}"


def ensure_world_snapshot(world: Dict[str, Any]) -> Dict[str, Any]:
    """Make sure this graph node has a ``worlds_store`` file.

    The default one-world Experience points at the live prompt file (empty
    slug). The first time we fork or Play needs a real snapshot, we write one.
    """
    if world.get("slug"):
        try:
            worlds_store.get_world(world["slug"])
            return world
        except KeyError:
            pass
    info = worlds_store.save_world(world.get("name") or "World")
    world["slug"] = info["slug"]
    world["name"] = info.get("name") or world.get("name") or "World"
    return world


def add_world(
    name: str = "",
    *,
    clone_from: str = "",
    x: Any = None,
    y: Any = None,
    slug: str = "",
) -> Dict[str, Any]:
    """Add a World node.

    ``clone_from`` set: fork that World. Otherwise seed the generic harness.
    Never snapshot live Play unless the author asked to fork.
    """
    exp = get_experience(slug)
    for w in exp["worlds"]:
        ensure_world_snapshot(w)
    label = _unique_world_name(
        name or "New World", [w.get("name") or "" for w in exp["worlds"]]
    )
    source = world_by_id(exp, clone_from) if clone_from else None
    if source and source.get("slug"):
        info = worlds_store.clone_world(source["slug"], label)
    else:
        info = worlds_store.create_blank_world(label)
    node: Dict[str, Any] = {"id": _new_id("w"), "name": info["name"], "slug": info["slug"]}
    px, py = _as_float(x), _as_float(y)
    if px is not None:
        node["x"] = px
    if py is not None:
        node["y"] = py
    exp["worlds"].append(node)
    return save_experience(exp, slug)


def move_experience(x: Any, y: Any, slug: str = "") -> Dict[str, Any]:
    """Pin the Experience coin on its own graph. Worlds stay where they are."""
    exp = get_experience(slug)
    px, py = _as_float(x), _as_float(y)
    if px is None or py is None:
        raise ValueError("An Experience needs numeric x and y.")
    exp["x"] = px
    exp["y"] = py
    return save_experience(exp, slug)


def move_world(world_id: str, x: Any, y: Any, slug: str = "") -> Dict[str, Any]:
    """Pin a World on the Experience graph. Layout is authoring, not simulation."""
    exp = get_experience(slug)
    world = world_by_id(exp, world_id)
    if not world:
        raise KeyError(f"World '{world_id}' not found.")
    px, py = _as_float(x), _as_float(y)
    if px is None or py is None:
        raise ValueError("A World needs numeric x and y.")
    world["x"] = px
    world["y"] = py
    return save_experience(exp, slug)


def remove_world(world_id: str, slug: str = "") -> Dict[str, Any]:
    exp = get_experience(slug)
    if len(exp["worlds"]) <= 1:
        raise ValueError("An Experience needs at least one World.")
    wid = str(world_id or "").strip()
    exp["worlds"] = [w for w in exp["worlds"] if w.get("id") != wid]
    exp["transitions"] = [
        t for t in exp["transitions"]
        if t.get("from") != wid and t.get("to") != wid
    ]
    if exp["start_world"] == wid:
        exp["start_world"] = exp["worlds"][0]["id"]
    return save_experience(exp, slug)


def add_transition(
    from_id: str,
    to_id: str,
    condition: Any = None,
    slug: str = "",
) -> Dict[str, Any]:
    exp = get_experience(slug)
    ids = node_ids(exp)
    src, dst = str(from_id or "").strip(), str(to_id or "").strip()
    if src not in ids or dst not in ids:
        raise KeyError("Both ends of a transition must be Worlds or Cutscenes in this Experience.")
    if src == dst:
        raise ValueError("A node cannot transition to itself.")
    for t in exp["transitions"]:
        if t.get("from") == src and t.get("to") == dst:
            return exp
    if condition is None and node_kind(exp, src) == "cutscene":
        condition = {"type": "immediate"}
    exp["transitions"].append({
        "id": _new_id("t"),
        "from": src,
        "to": dst,
        "condition": normalize_condition(condition),
    })
    # Cutscene → current start World with no World inbound is an opening title.
    if (
        node_kind(exp, src) == "cutscene"
        and node_kind(exp, dst) == "world"
        and exp.get("start_world") == dst
        and not any(
            t.get("to") == src and world_by_id(exp, t.get("from") or "")
            for t in exp["transitions"]
        )
    ):
        exp["start_world"] = src
    return save_experience(exp, slug)


def update_transition(
    transition_id: str,
    patch: Any,
    slug: str = "",
) -> Dict[str, Any]:
    exp = get_experience(slug)
    tid = str(transition_id or "").strip()
    found = None
    for t in exp["transitions"]:
        if t.get("id") == tid:
            found = t
            break
    if not found:
        raise KeyError(f"Transition '{tid}' not found.")
    if isinstance(patch, dict):
        if "condition" in patch:
            found["condition"] = normalize_condition(patch.get("condition"))
        if "from" in patch or "to" in patch:
            nxt = _normalize_transition({**found, **patch})
            if nxt:
                found["from"] = nxt["from"]
                found["to"] = nxt["to"]
                found["condition"] = nxt["condition"]
    return save_experience(exp, slug)


def remove_transition(transition_id: str, slug: str = "") -> Dict[str, Any]:
    exp = get_experience(slug)
    tid = str(transition_id or "").strip()
    exp["transitions"] = [t for t in exp["transitions"] if t.get("id") != tid]
    return save_experience(exp, slug)


def rename_world(world_id: str, name: str, slug: str = "", *, blurb: Any = None) -> Dict[str, Any]:
    exp = get_experience(slug)
    world = world_by_id(exp, world_id)
    if not world:
        raise KeyError(f"World '{world_id}' not found.")
    world["name"] = str(name or "").strip() or world["name"]
    if blurb is not None:
        text = str(blurb).strip()
        if text:
            world["blurb"] = text
        else:
            world.pop("blurb", None)
    return save_experience(exp, slug)


def set_start_world(world_id: str, slug: str = "") -> Dict[str, Any]:
    """Mark a World or Cutscene as the node PLAY begins on."""
    exp = get_experience(slug)
    nid = str(world_id or "").strip()
    if nid not in node_ids(exp):
        raise KeyError(f"Start node '{nid}' is not a World or Cutscene in this Experience.")
    exp["start_world"] = nid
    return save_experience(exp, slug)


def _unique_cutscene_name(base: str, existing: List[str]) -> str:
    return _unique_world_name(base or "Cutscene", existing)


def add_cutscene(
    name: str = "",
    *,
    mood: str = "threshold",
    source: str = "incoming",
    shot_brief: str = "",
    blurb: str = "",
    x: Any = None,
    y: Any = None,
    slug: str = "",
) -> Dict[str, Any]:
    """Add a Cutscene node. No world snapshot — these are montage beats."""
    exp = get_experience(slug)
    label = _unique_cutscene_name(
        name or "Cutscene", [c.get("name") or "" for c in exp.get("cutscenes") or []]
    )
    node: Dict[str, Any] = {
        "id": _new_id("c"),
        "name": label,
        "mood": mood if mood in CUTSCENE_MOODS else "threshold",
        "source": source if source in CUTSCENE_SOURCES else "incoming",
    }
    if shot_brief:
        node["shot_brief"] = str(shot_brief).strip()
    if blurb:
        node["blurb"] = str(blurb).strip()
    px, py = _as_float(x), _as_float(y)
    if px is not None:
        node["x"] = px
    if py is not None:
        node["y"] = py
    exp.setdefault("cutscenes", []).append(node)
    return save_experience(exp, slug)


def move_cutscene(cutscene_id: str, x: Any, y: Any, slug: str = "") -> Dict[str, Any]:
    exp = get_experience(slug)
    node = cutscene_by_id(exp, cutscene_id)
    if not node:
        raise KeyError(f"Cutscene '{cutscene_id}' not found.")
    px, py = _as_float(x), _as_float(y)
    if px is None or py is None:
        raise ValueError("A Cutscene needs numeric x and y.")
    node["x"] = px
    node["y"] = py
    return save_experience(exp, slug)


def remove_cutscene(cutscene_id: str, slug: str = "") -> Dict[str, Any]:
    exp = get_experience(slug)
    cid = str(cutscene_id or "").strip()
    exp["cutscenes"] = [c for c in (exp.get("cutscenes") or []) if c.get("id") != cid]
    exp["transitions"] = [
        t for t in exp["transitions"]
        if t.get("from") != cid and t.get("to") != cid
    ]
    if exp.get("start_world") == cid:
        exp["start_world"] = exp["worlds"][0]["id"]
    return save_experience(exp, slug)


def rename_cutscene(
    cutscene_id: str,
    name: str,
    slug: str = "",
    *,
    blurb: Any = None,
    mood: Any = None,
    source: Any = None,
    shot_brief: Any = None,
) -> Dict[str, Any]:
    exp = get_experience(slug)
    node = cutscene_by_id(exp, cutscene_id)
    if not node:
        raise KeyError(f"Cutscene '{cutscene_id}' not found.")
    node["name"] = str(name or "").strip() or node["name"]
    if blurb is not None:
        text = str(blurb).strip()
        if text:
            node["blurb"] = text
        else:
            node.pop("blurb", None)
    if mood is not None:
        m = str(mood).strip().lower()
        if m in CUTSCENE_MOODS:
            node["mood"] = m
    if source is not None:
        s = str(source).strip().lower()
        if s in CUTSCENE_SOURCES:
            node["source"] = s
    if shot_brief is not None:
        text = str(shot_brief).strip()
        if text:
            node["shot_brief"] = text
        else:
            node.pop("shot_brief", None)
    return save_experience(exp, slug)


def persist_world_snapshot(world_id: str, slug: str = "") -> Dict[str, Any]:
    """Write the live prompt file back into this World's snapshot."""
    exp = get_experience(slug)
    world = world_by_id(exp, world_id)
    if not world:
        raise KeyError(f"World '{world_id}' not found.")
    info = worlds_store.save_world(
        world.get("name") or "World",
        slug=world.get("slug") or "",
    )
    world["slug"] = info["slug"]
    world["name"] = info.get("name") or world["name"]
    return save_experience(exp, slug)


# ═══════════════════════════════════════════════════════════════════
# LORE — Experience-level background. Worlds share this. Authors drop
# notes and files on the Lore node; Play reads a compact brief.
# ═══════════════════════════════════════════════════════════════════

_LORE_FRAMING = (
    "HISTORICAL BACKGROUND (the player does not know this unless they discover it "
    "in the world — documents, places, people, residue. Never info-dump):\n"
)


def lore_dir(slug: str = "") -> Path:
    return EXPERIENCES_DIR / "_lore" / (_resolve_slug(slug))


def _lore_url(slug: str, doc_id: str) -> str:
    return f"/api/experience/lore/{_resolve_slug(slug)}/{doc_id}"


def _stamp_doc_urls(lore: Dict[str, Any], slug: str) -> Dict[str, Any]:
    slug = _resolve_slug(slug)
    docs = []
    for doc in lore.get("documents") or []:
        item = dict(doc)
        if item.get("kind") == "image":
            item["url"] = _lore_url(slug, item["id"])
        docs.append(item)
    out = dict(lore)
    out["documents"] = docs
    return out


def lore_stats(lore: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = normalize_lore(lore)
    notes = data.get("notes") or ""
    docs = data.get("documents") or []
    chars = len(notes) + sum(int(d.get("chars") or 0) for d in docs)
    images = sum(1 for d in docs if d.get("kind") == "image")
    texts = sum(1 for d in docs if d.get("kind") == "text")
    return {
        "enabled": bool(data.get("enabled", True)),
        "notes_chars": len(notes),
        "chars": chars,
        "documents": len(docs),
        "texts": texts,
        "images": images,
        "rich": bool(notes.strip() or docs),
    }


def get_lore(slug: str = "") -> Dict[str, Any]:
    slug = _resolve_slug(slug)
    exp = get_experience(slug)
    raw = exp.get("lore") or {}
    lore = _stamp_doc_urls(normalize_lore(raw), slug)
    if raw.get("source") == "world":
        lore["source"] = "world"
    return lore


def _write_lore(lore: Dict[str, Any], slug: str = "") -> Dict[str, Any]:
    slug = _resolve_slug(slug)
    exp = get_experience(slug)
    exp["lore"] = normalize_lore(lore)
    saved = save_experience(exp, slug)
    return _stamp_doc_urls(saved.get("lore") or default_lore(), slug)


def set_lore_notes(notes: Any, slug: str = "", *, enabled: Any = None) -> Dict[str, Any]:
    lore = get_lore(slug)
    lore["notes"] = str(notes or "")
    if enabled is not None:
        lore["enabled"] = bool(enabled)
    return _write_lore(lore, slug)


def move_lore(x: Any, y: Any, slug: str = "") -> Dict[str, Any]:
    lore = get_lore(slug)
    px, py = _as_float(x), _as_float(y)
    if px is None or py is None:
        raise ValueError("Lore needs numeric x and y.")
    lore["x"] = px
    lore["y"] = py
    return _write_lore(lore, slug)


def _copy_lore_files(src_slug: str, dst_slug: str) -> None:
    src = lore_dir(src_slug)
    dst = lore_dir(dst_slug)
    if not src.is_dir():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def _doc_path(slug: str, doc: Dict[str, Any]) -> Optional[Path]:
    name = str(doc.get("file") or "").strip()
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    return lore_dir(slug) / name


def lore_file_path(slug: str, doc_id: str) -> Optional[Path]:
    lore = get_lore(slug)
    for doc in lore.get("documents") or []:
        if doc.get("id") == doc_id:
            path = _doc_path(slug, doc)
            if path and path.is_file():
                return path
    return None


def _new_lore_id() -> str:
    return "l" + uuid.uuid4().hex[:10]


def add_lore_document(
    *,
    name: str = "",
    text: str = "",
    image: str = "",
    data: bytes = b"",
    slug: str = "",
) -> Dict[str, Any]:
    """Add one uploaded note or image to the active Experience's Lore node."""
    slug = _resolve_slug(slug)
    lore = get_lore(slug)
    if len(lore.get("documents") or []) >= _LORE_MAX_DOCS:
        raise ValueError(f"An Experience can hold {_LORE_MAX_DOCS} lore files.")
    filename = _safe_lore_name(name, "note.md")
    ext = Path(filename).suffix.lower()
    payload = data or b""
    kind = "text"
    chars = 0
    if image or ext in _LORE_IMAGE_EXTS:
        kind = "image"
        if image:
            payload = _decode_data_url(image)
        if not payload:
            raise ValueError("That image was empty.")
        if len(payload) > _LORE_MAX_IMAGE:
            raise ValueError("That image is too large.")
        if ext not in _LORE_IMAGE_EXTS:
            filename = _safe_lore_name(Path(filename).stem + ".png", "plate.png")
            ext = ".png"
    else:
        body = text if text else payload.decode("utf-8", errors="replace")
        body = body.replace("\x00", "")
        if len(body) > _LORE_MAX_TEXT:
            body = body[:_LORE_MAX_TEXT]
        if not body.strip() and not str(name or "").strip():
            raise ValueError("Drop a text file, an image, or write some background.")
        payload = body.encode("utf-8")
        chars = len(body)
        if ext not in _LORE_TEXT_EXTS:
            filename = _safe_lore_name(Path(filename).stem + ".md", "note.md")
            ext = ".md"
    did = _new_lore_id()
    stored = did + ext
    root = lore_dir(slug)
    root.mkdir(parents=True, exist_ok=True)
    path = root / stored
    path.write_bytes(payload)
    doc: Dict[str, Any] = {
        "id": did,
        "name": filename,
        "kind": kind,
        "file": stored,
        "chars": chars if kind == "text" else len(payload),
        "updated": time.time(),
    }
    if kind == "image":
        doc["url"] = _lore_url(slug, did)
    lore.setdefault("documents", []).append(doc)
    written = _write_lore(lore, slug)
    return {"lore": written, "document": next((d for d in written["documents"] if d["id"] == did), doc)}


def remove_lore_document(doc_id: str, slug: str = "") -> Dict[str, Any]:
    slug = _resolve_slug(slug)
    lore = get_lore(slug)
    did = str(doc_id or "").strip()
    kept = []
    removed = None
    for doc in lore.get("documents") or []:
        if doc.get("id") == did:
            removed = doc
            continue
        kept.append(doc)
    if removed:
        path = _doc_path(slug, removed)
        if path and path.is_file():
            try:
                path.unlink()
            except OSError:
                pass
    lore["documents"] = kept
    return _write_lore(lore, slug)


def _decode_data_url(raw: str) -> bytes:
    blob = str(raw or "")
    if "," in blob and blob.strip().startswith("data:"):
        blob = blob.split(",", 1)[1]
    import base64
    try:
        return base64.b64decode(blob, validate=False)
    except Exception as e:
        raise ValueError("Could not read that image.") from e


def _authored_lore_rich(lore: Optional[Dict[str, Any]]) -> bool:
    data = normalize_lore(lore)
    if str(data.get("notes") or "").strip():
        return True
    return bool(data.get("documents"))


def _implicit_world_bible_from_exp(exp: Optional[Dict[str, Any]]) -> str:
    """Read-only start-World bible. Never calls load_world (that overwrites Play)."""
    if not isinstance(exp, dict):
        return ""
    world = landing_world(exp) or world_by_id(exp, str(exp.get("start_world") or ""))
    wslug = str((world or {}).get("slug") or "").strip()
    if wslug:
        try:
            data = worlds_store.get_world(wslug)
            text = str((data.get("prompts") or {}).get("world_initial_state") or "").strip()
            if text:
                return text
        except Exception:
            pass
    try:
        from prompts_store import PROMPTS
        return str(PROMPTS.get("world_initial_state") or "").strip()
    except Exception:
        return ""


def _resolve_lore(exp: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Empty Lore inherits the start World's story bible so Play and the graph share it."""
    lore = normalize_lore((exp or {}).get("lore"))
    if not lore.get("enabled", True):
        return lore
    if _authored_lore_rich(lore):
        return lore
    bible = _implicit_world_bible_from_exp(exp)
    if not bible:
        return lore
    out = dict(lore)
    out["notes"] = bible[:_LORE_MAX_NOTES]
    out["source"] = "world"
    return out


def _doc_body(slug: str, doc: Dict[str, Any]) -> str:
    path = _doc_path(slug, doc)
    if path and path.is_file():
        try:
            return path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            pass
    return str(doc.get("text") or "").strip()


def _lore_chunks(slug: str = "") -> List[str]:
    lore = get_lore(slug)
    if not lore.get("enabled", True):
        return []
    chunks: List[str] = []
    notes = str(lore.get("notes") or "").strip()
    if notes:
        chunks.append(notes)
    for doc in lore.get("documents") or []:
        if doc.get("kind") != "text":
            continue
        body = _doc_body(slug, doc)
        if not body:
            continue
        title = doc.get("name") or "note"
        chunks.append(f"[{title}]\n{body}")
    return chunks


def lore_already_in(text: str, slug: str = "") -> bool:
    """True when this prompt already carries the Experience bible."""
    hay = text or ""
    if not hay:
        return False
    if _LORE_FRAMING.strip() in hay:
        return True
    for chunk in _lore_chunks(slug):
        needle = (chunk or "").strip()
        if len(needle) >= 80 and needle[:80] in hay:
            return True
        if 40 <= len(needle) < 80 and needle[:40] in hay:
            return True
    return False


def lore_brief(slug: str = "") -> str:
    """Compact background injected into a run's world document."""
    chunks = _lore_chunks(slug)
    if not chunks:
        return ""
    body = "\n\n".join(chunks)
    if len(body) > _LORE_BRIEF_CAP:
        body = body[:_LORE_BRIEF_CAP].rstrip() + "\n…"
    # Lore authored against the shipped protagonist keeps that name forever,
    # so a run whose cast sheet says Wren Alvarez shipped a world document
    # naming Jason six times and Wren once — and every prompt read it. The
    # cast sheet is the authority on who the player is.
    try:
        import game_identity
        body = game_identity.recast(body)
    except Exception:
        pass
    return _LORE_FRAMING + body


def with_lore(text: str, slug: str = "") -> str:
    """Append Experience lore to a world prompt. Empty or already-present lore is a no-op."""
    brief = lore_brief(slug)
    if not brief:
        return text
    if lore_already_in(text, slug):
        return text or ""
    base = (text or "").rstrip()
    return f"{base}\n\n{brief}" if base else brief


def apply_lore_to_prompt(prompt: str, slug: str = "") -> str:
    """Prepend the Experience bible for a narrative LLM call. Dedupes if already present."""
    brief = lore_brief(slug)
    if not brief:
        return prompt
    if lore_already_in(prompt, slug):
        return prompt
    base = (prompt or "").lstrip()
    return f"{brief}\n\n{base}" if base else brief
