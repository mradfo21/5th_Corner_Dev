"""Characters — who you are, as a thing the game owns.

Asked (2026-09-23): *"like a classic RPG, when you enter a world, you need a
character … a high level being with a customizable look, who you control, with
persistent inventory, and is completely reactive to items you discover and are
wearing."* The design record is docs/plans/CHARACTER_SYSTEM_PLAN.md; this is it,
built.

Before this the player was three things that were not one thing: WORDS in the
live prompt file's ``player_character`` block, an uploaded POSTER attached to
every render as the "character sheet", and a property of whichever World was
bound. The words and the poster disagreed (the sheet showed a black plate
carrier, the words a blue PRESS flak vest, and the vest flipped whenever the
frame being continued did not show it), the poster leaked its background and
its pose into frames, it only ever showed his front while the camera mostly
sees his back, and it lived in the one shared prompt file that every World
bind rewrites (the harness that "teleported" the player into a new body).

A Character is one record that owns all of it, stored where no World and no
reset can reach it::

    characters/<id>/character.json      identity, inventory, looks, record
    characters/<id>/sources/            what the player gave it (pictures)
    characters/<id>/looks/<look>/       one rendered version of them:
        turnaround_key.png                four views, A-pose, on the key colour
        turnaround.png                    the same, cut out (RGBA)
        turnaround_ref.jpg                on flat grey — what the image models see
        idle_key.png / idle.png           the hero pose (the screens; never the sim)
        face_ref.jpg                      head and shoulders, for close-ups
        thumb.png                         the roster square
        look.json                         what is worn, the garment words READ OFF
                                          the render, when, how long it took
    characters/<id>/items/              plates of what they carry, copied out of
                                        the world's look book when it was taken

The words the prompts read are written BY A VISION PASS OVER THE TURNAROUND,
not typed: the player's line is the brief, the render is the truth, the words
are read off the truth — so they cannot disagree with the picture.

A run points at a character (``state.character_id`` / ``state.look_id``), and
``game_identity.get_spec()`` lays the bound character over the cast sheet's
character block, which is how thirty prompt surfaces and every image call
reach them without being rewritten (see ``bound_block``).

Wearing something is a FITTING: the base turnaround plus the item's plate,
redrawn with the item on — always from the base, with the whole worn set, so
putting things on and taking them off never accumulates drift, and a look is
cached by outfit (taking the helmet off again costs nothing).
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import random
import re
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import app_identity
import authoring_sandbox as _sandbox

_sandbox.guard()  # before the path below is computed — a test never writes a real character

ROOT = Path(__file__).resolve().parent
def _default_dir() -> Path:
    """Where the roster lives. Beside the code when run from the repo (it is
    gitignored, and a reset never touches it). In a PACKAGED build, the
    player's own folder — %APPDATA%\\SOMEWHERE\\characters, like the keys —
    because the game folder is the thing that gets replaced: an update, or
    tools/build_exe.py --clean wiping dist/, would take every character with
    it. A roster an older build kept beside the exe is carried over once."""
    if not getattr(sys, "frozen", False):
        return ROOT / "characters"
    dest = app_identity.data_dir() / "characters"
    old = ROOT / "characters"
    try:
        if not dest.exists() and old.is_dir() and any(old.iterdir()):
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(old, dest)
    except OSError as e:
        print(f"[CHARACTERS] could not carry the roster over from {old}: {e}", flush=True)
    return dest


CHARACTERS_DIR = Path(os.getenv("SOMEWHERE_CHARACTERS_DIR") or _default_dir())
# Shipped with the game, copied in the first time the roster is read, so a
# first run with no keys still has somebody (Jason, drawn through this
# pipeline). assets/characters/<id>/ has the same layout as a live one.
STARTERS_DIR = ROOT / "assets" / "characters"

# ── the renders ─────────────────────────────────────────────────────────────
# The spike (2026-09-23, _claude_chars/) ran three very different briefs
# through turnaround → cut-out → idle → one fitting on the pro model at 2K:
# every turnaround came back as four consistent views and cut out clean first
# time, 23–29 s each; idle 21 s; a fitting 19 s. The fast play model tops out
# below 2K and draws a 150 px face at best on a four-up strip.
IMAGE_MODEL = "gemini-3-pro-image"
IMAGE_SIZE = "2K"
IMAGE_TIMEOUT_S = 150
# The brief, the check and the read-back are text/vision; the look book's
# writers, fastest first here because the player is watching.
TEXT_MODELS = ("gemini-3.5-flash", "gemini-3.8-flash", "gemini-3.1-flash-lite")
# What an image model is shown: a bounded JPEG of the turnaround on grey. A
# 1600 px strip keeps each figure ~900 px tall with a face worth copying.
REF_MAX = 1600
THUMB_SIZE = 192

# How long until a new character or a fitting is PLAYABLE (the turnaround
# is in): what the screens count down from. Measured on the pro model at 2K,
# 2026-09-24, 25–32 s.
READY_ETA_S = 30     # the time to PLAYABLE before this machine has drawn anyone

STATUS_NEW = "new"          # made from something (the old cast sheet) and not drawn yet
STATUS_DRAWING = "drawing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

# Where a worn thing goes on the body. Few enough to be visual, enough to
# place anything the look book can hand out (weapon, armor, upgrade, tool,
# relic — look_book.ITEM_KINDS).
SLOTS = ("head", "face", "neck", "torso", "outer", "hands", "legs", "feet", "back", "held")
SLOT_WORDS = {
    "head": "on the head", "face": "over the face", "neck": "at the neck",
    "torso": "on the body, under everything else", "outer": "worn over the clothes",
    "hands": "on the hands", "legs": "on the legs", "feet": "on the feet",
    "back": "on the back", "held": "carried at the ready (slung or holstered on the body in the turnaround, in hand in the pose)",
}
_SLOT_RULES: Tuple[Tuple[str, str], ...] = (
    ("face", r"\b(gas ?mask|mask|respirator|goggles|visor|glasses|spectacles|monocle|eye ?patch|balaclava|rebreather)\b"),
    ("head", r"\b(helmet|hat|cap|hood|beanie|crown|headset|head ?lamp|head-?torch|circlet|headband|bandana on the head)\b"),
    ("hands", r"\b(gloves?|gauntlets?|bracers?|knuckle[- ]?dusters?|knuckles)\b"),
    ("feet", r"\b(boots?|shoes?|sandals?|greaves)\b"),
    ("legs", r"\b(trousers|pants|leggings|kilt|skirt|chaps|knee ?pads?|shin ?guards?)\b"),
    ("neck", r"\b(amulet|pendant|necklace|locket|dog ?tags?|talisman|choker|collar|scarf|medallion|torc)\b"),
    ("back", r"\b(backpack|rucksack|cape|cloak|quiver|jet ?pack|air tank|oxygen tank|scuba tank)\b"),
    ("outer", r"\b(vest|jacket|coat|armou?r|plate carrier|carrier|flak|poncho|duster|harness|breastplate|cuirass|body ?plate|chest ?plate|suit|parka|trench ?coat|rig)\b"),
    ("torso", r"\b(shirt|tee|t-shirt|sweater|jumper|hoodie|tunic|undershirt|mesh)\b"),
)
_WEARABLE_KINDS = ("weapon", "armor", "armour", "tool")

_LOCK = threading.RLock()
_JOBS: Dict[str, threading.Thread] = {}
_CACHE: Dict[str, Tuple[float, Any]] = {}


# ═══════════════════════════════════════════════════════════════════════════
# STORE
# ═══════════════════════════════════════════════════════════════════════════

def _now() -> float:
    return round(time.time(), 3)


def _slug(text: Any) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return s[:32] or "character"


def valid_id(cid: Any) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", str(cid or "")))


def char_dir(cid: str) -> Path:
    if not valid_id(cid):
        raise ValueError(f"bad character id {cid!r}")
    return CHARACTERS_DIR / cid


def look_dir(cid: str, look_id: str) -> Path:
    if not valid_id(look_id):
        raise ValueError(f"bad look id {look_id!r}")
    return char_dir(cid) / "looks" / look_id


def _read_json(path: Path) -> Optional[dict]:
    """A JSON file, cached by mtime: this is read on every get_spec(). If it
    will not parse (a crash, a disk that filled mid-write), the last good copy
    beside it (.bak, kept by _write_json) is used and put back — a character is
    the player's, and one unreadable file must not take them off the roster."""
    try:
        mt = path.stat().st_mtime
    except OSError:
        return None
    key = str(path)
    hit = _CACHE.get(key)
    if hit and hit[0] == mt:
        return json.loads(json.dumps(hit[1]))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except Exception as e:  # noqa: BLE001
        bak = path.with_suffix(path.suffix + ".bak")
        try:
            data = json.loads(bak.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            print(f"[CHARACTERS] {path} is unreadable ({e}) and has no good backup", flush=True)
            return None
        print(f"[CHARACTERS] {path} was unreadable ({e}) — restored the last good copy", flush=True)
        try:
            shutil.copyfile(path, path.with_suffix(path.suffix + ".broken"))
            _write_json(path, data)
            mt = path.stat().st_mtime
        except OSError:
            pass
    _CACHE[key] = (mt, data)
    return json.loads(json.dumps(data))


def _write_json(path: Path, data: dict) -> None:
    """Written whole or not at all, and flushed to the disk before it replaces
    the old one (a power cut after os.replace can otherwise leave an empty
    file on some filesystems). The previous good copy is kept as .bak. On
    Windows a rename fails while anything else holds the file open — an
    antivirus scan, a backup tool — so it is retried for a moment first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=1, ensure_ascii=False))
        fh.flush()
        os.fsync(fh.fileno())
    bak = path.with_suffix(path.suffix + ".bak")
    for attempt in range(8):
        try:
            if path.is_file():
                try:
                    json.loads(path.read_text(encoding="utf-8"))   # only a good copy is kept
                    shutil.copyfile(path, bak)
                except (ValueError, OSError):
                    pass
            os.replace(tmp, path)
            break
        except PermissionError:
            if attempt == 7:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            time.sleep(0.05 * (attempt + 1))
    _CACHE.pop(str(path), None)


def load(cid: str) -> Optional[dict]:
    try:
        return _read_json(char_dir(cid) / "character.json")
    except ValueError:
        return None


def save(rec: dict) -> dict:
    rec["updated"] = _now()
    _write_json(char_dir(rec["id"]) / "character.json", rec)
    return rec


def load_look(cid: str, look_id: str) -> Optional[dict]:
    if not look_id:
        return None
    try:
        return _read_json(look_dir(cid, look_id) / "look.json")
    except ValueError:
        return None


def save_look(cid: str, look: dict) -> dict:
    _write_json(look_dir(cid, look["id"]) / "look.json", look)
    return look


def _update(cid: str, fn) -> Optional[dict]:
    """Read-modify-write one character under the store lock."""
    with _LOCK:
        rec = load(cid)
        if rec is None:
            return None
        out = fn(rec)
        save(rec)
        return rec if out is None else out


def _new_record(name: str = "", concept: str = "", source: str = "created") -> dict:
    base = _slug(name or concept.split(",")[0].split(".")[0] or "character")
    cid = f"{base}-{uuid.uuid4().hex[:4]}"
    while (CHARACTERS_DIR / cid).exists():
        cid = f"{base}-{uuid.uuid4().hex[:4]}"
    return {
        "id": cid, "name": str(name or "").strip()[:60], "concept": str(concept or "").strip()[:1200],
        "pronouns": "", "role": "", "tagline": "", "demeanor": "", "who": "", "held": "",
        "key": "green", "sources": [], "origin": source, "style": "",
        "status": STATUS_NEW, "job": {}, "error": "",
        "base_look": "", "current_look": "", "wanted_look": "",
        "looks": {}, "outfits": {},
        "inventory": [],
        "record": {"runs": 0, "deaths": 0, "worlds": [], "turns": 0},
        "created": _now(), "updated": _now(), "last_played": 0,
    }


def list_all() -> List[dict]:
    """Every character on this machine, the one played last first."""
    ensure_seeded()
    out = []
    if CHARACTERS_DIR.is_dir():
        for d in CHARACTERS_DIR.iterdir():
            if d.is_dir() and valid_id(d.name) and not d.name.startswith("_"):
                rec = load(d.name)
                if rec is None and (d / "character.json").exists():
                    print(f"[CHARACTERS] {d.name} is on disk but cannot be read — left alone, "
                          f"not deleted", flush=True)
                if rec and rec.get("id") == d.name and not rec.get("deleted"):
                    out.append(rec)
    out.sort(key=lambda r: (-(r.get("last_played") or 0), -(r.get("created") or 0)))
    return out


def last_played() -> Optional[dict]:
    rows = [r for r in list_all() if r.get("status") == STATUS_READY]
    return rows[0] if rows else None


def delete(cid: str) -> bool:
    """Out of the roster. Moved aside, not destroyed: a character is the
    player's, and a mis-click should not cost them one."""
    with _LOCK:
        d = char_dir(cid)
        if not d.is_dir():
            return False
        trash = CHARACTERS_DIR / "_deleted"
        trash.mkdir(parents=True, exist_ok=True)
        shutil.move(str(d), str(trash / f"{cid}-{int(time.time())}"))
        _CACHE.clear()
        return True


# ── seeding: the shipped starter, and the old cast sheet ────────────────────

_SEEDED_FILE = ".seeded.json"


def ensure_seeded() -> None:
    """Copy the shipped starter(s) in once, and turn this machine's old cast
    sheet into a character the first time the roster is read. A starter the
    player deleted is not brought back (the marker remembers it)."""
    with _LOCK:
        CHARACTERS_DIR.mkdir(parents=True, exist_ok=True)
        marker = CHARACTERS_DIR / _SEEDED_FILE
        try:
            seen = json.loads(marker.read_text(encoding="utf-8"))
        except Exception:
            seen = {}
        changed = False
        if STARTERS_DIR.is_dir():
            for d in sorted(STARTERS_DIR.iterdir()):
                if not (d.is_dir() and (d / "character.json").is_file()) or d.name in seen:
                    continue
                dest = CHARACTERS_DIR / d.name
                if not dest.exists():
                    shutil.copytree(d, dest)
                seen[d.name] = _now()
                changed = True
        if "_cast_sheet" not in seen:
            try:
                made = import_cast_sheet()
            except Exception as e:  # noqa: BLE001
                print(f"[CHARACTERS] could not carry the old cast sheet over: {e}", flush=True)
                made = None
            seen["_cast_sheet"] = made["id"] if made else ""
            changed = True
        if changed:
            _write_json(marker, seen)


def import_cast_sheet() -> Optional[dict]:
    """The character this machine was playing before Characters existed —
    the cast sheet's words and its uploaded picture — as a character that is
    not drawn yet. The screen offers DRAW on it; nothing is spent until then.
    The shipped Jason is the starter already, so a sheet that is still him
    brings nothing over."""
    try:
        import game_identity as gi
    except Exception:
        return None
    raw = gi.raw_spec()
    block = raw.get(gi.CHARACTER_KEY) or {}
    if not gi.character_enabled(raw) or gi.is_shipped_cast(raw):
        return None
    name = str(block.get("name") or "").strip()
    if name.lower() in ("jason fleece", "jason") and STARTERS_DIR.is_dir() and any(STARTERS_DIR.iterdir()):
        return None  # he ships as the starter, drawn
    bits = [str(block.get(k) or "").strip() for k in ("role", "appearance", "wardrobe", "signature_gear")]
    concept = ". ".join(b.rstrip(".") for b in bits if b)
    if not (name or concept):
        return None
    rec = _new_record(name, concept or name, source="cast_sheet")
    rec["pronouns"] = str(block.get("pronouns") or "")
    rec["role"] = str(block.get("role") or "")
    rec["demeanor"] = str(block.get("demeanor") or "")
    rec["tagline"] = (rec["role"].rstrip(".") + ".") if rec["role"] else ""
    src_dir = char_dir(rec["id"]) / "sources"
    for ref_id in block.get("reference_images") or []:
        p = gi.reference_path(str(ref_id))
        if p and Path(p).is_file():
            src_dir.mkdir(parents=True, exist_ok=True)
            dest = src_dir / f"source_{len(rec['sources']) + 1}{Path(p).suffix.lower()}"
            shutil.copyfile(p, dest)
            rec["sources"].append(dest.name)
    save(rec)
    print(f"[CHARACTERS] the cast sheet's {name or 'character'!r} is on the roster "
          f"as {rec['id']} (not drawn yet)", flush=True)
    return rec


# ═══════════════════════════════════════════════════════════════════════════
# WHAT THE CLIENT SEES
# ═══════════════════════════════════════════════════════════════════════════

def file_url(cid: str, rel: str) -> str:
    """A character's file as the client fetches it (api.py serves it)."""
    try:
        p = char_dir(cid) / rel
        v = int(p.stat().st_mtime)
    except (OSError, ValueError):
        return ""
    return f"/api/characters/{cid}/file/{rel}?v={v}"


def file_path(cid: str, rel: str) -> Optional[Path]:
    """The one door files leave by: a character's own looks, items and
    sources, nothing else, no traversal."""
    if not valid_id(cid) or not re.fullmatch(
            r"(looks/[a-z0-9][a-z0-9-]{0,63}/[a-z_]+\.(png|jpg)|items/[a-z0-9_-]+\.(png|jpg)|"
            r"sources/source_\d+\.(png|jpg|jpeg|webp|gif))", str(rel or "")):
        return None
    p = char_dir(cid) / rel
    return p if p.is_file() else None


def _look_files(cid: str, look_id: str) -> Dict[str, str]:
    out = {}
    if not look_id:
        return out
    for key, name in (("idle", "idle.png"), ("turnaround", "turnaround.png"),
                      ("thumb", "thumb.png"), ("face", "face_ref.jpg"),
                      ("turnaround_ref", "turnaround_ref.jpg")):
        url = file_url(cid, f"looks/{look_id}/{name}")
        if url:
            out[key] = url
    return out


def card(rec: dict, *, full: bool = False) -> Dict[str, Any]:
    """A character as the screens draw it: name, the line under it, the idle
    and the roster square — and, while something is being drawn, how far."""
    cid = rec["id"]
    look_id = rec.get("current_look") or rec.get("base_look") or ""
    files = _look_files(cid, look_id)
    if not files.get("idle"):
        # Playable before its hero pose is drawn: a fitting keeps showing the
        # look it was fitted onto (posed) until its own lands; a new body
        # shows none — never the turnaround's A-pose.
        parent = (load_look(cid, look_id) or {}).get("parent") or ""
        if parent:
            prev = _look_files(cid, parent)
            if prev.get("idle"):
                files["idle"] = prev["idle"]
                files.setdefault("thumb", prev.get("thumb", ""))
    source = ""
    for s in rec.get("sources") or []:
        source = file_url(cid, f"sources/{s}")
        if source:
            break
    job = dict(rec.get("job") or {})
    out = {
        "id": cid, "name": rec.get("name") or "", "tagline": rec.get("tagline") or rec.get("role") or "",
        "status": rec.get("status") or STATUS_NEW, "error": rec.get("error") or "",
        "idle": files.get("idle", ""), "thumb": files.get("thumb", "") or files.get("idle", ""),
        "source": source, "concept": rec.get("concept") or "",
        "runs": int((rec.get("record") or {}).get("runs") or 0),
        "look": look_id, "wanted_look": rec.get("wanted_look") or "",
        "fitting": bool(rec.get("wanted_look") and rec.get("wanted_look") != rec.get("current_look")),
        # playable, and the hero pose / the words are still being drawn
        "finishing": (job.get("stage") == "finishing"),
        "job": {"kind": job.get("kind", ""), "stage": job.get("stage", ""),
                "line": job.get("line", ""), "progress": job.get("progress", 0),
                "started": job.get("started", 0), "eta": job.get("eta", 0),
                "eta_total": job.get("eta_total", 0)} if job else {},
        "pronouns": rec.get("pronouns") or "",
        "origin": rec.get("origin") or "",
        # "" = the game's own style (DEFAULT_STYLE); anything else is theirs
        "style": rec.get("style") or "",
        "style_known": "style" in rec,
        "base_look": rec.get("base_look") or "",
        # what the look being drawn has so far — the screen shows the
        # turnaround the moment it lands, and the idle after
        "dev": _look_files(cid, job.get("look") or "") if job.get("look") else {},
    }
    if full:
        look = load_look(cid, look_id) or {}
        out["turnaround"] = files.get("turnaround", "")
        out["face"] = files.get("face", "")
        out["garments"] = look.get("garments") or {}
        out["wardrobe_line"] = look.get("wardrobe_line") or ""
        out["body"] = look.get("body") or ""
        out["inventory"] = [item_card(rec, it) for it in rec.get("inventory") or []]
        out["record"] = rec.get("record") or {}
    return out


def item_card(rec: dict, it: dict) -> Dict[str, Any]:
    cid = rec["id"]
    plate = file_url(cid, f"items/{it['plate']}") if it.get("plate") else ""
    return {"id": it.get("id", ""), "name": it.get("name", ""), "kind": it.get("kind", ""),
            "tier": it.get("tier", ""), "power": it.get("power", ""), "worth": it.get("worth", ""),
            "from": it.get("from", ""), "source": it.get("source", ""), "plate": plate,
            "slot": it.get("slot") or "", "wearable": bool(it.get("slot")),
            "worn": bool(it.get("worn"))}


# ═══════════════════════════════════════════════════════════════════════════
# THE MODEL CALLS
# ═══════════════════════════════════════════════════════════════════════════

def _lb():
    import look_book
    return look_book


def have_key() -> bool:
    """A real render needs a Gemini key. Without one (mock mode, the suites,
    a fresh machine) every stage draws a stand-in so the whole flow still runs."""
    if os.getenv("SOMEWHERE_CHARACTERS_OFFLINE"):
        return False
    try:
        if _lb()._api_key():
            return True
        import provider_bridge   # OpenAI chosen in ACCOUNT draws them instead
        return provider_bridge.active()
    except Exception:
        return False


def _text_json(parts: list, operation: str, temperature: float = 0.4) -> dict:
    lb = _lb()
    last: Any = None
    for model in TEXT_MODELS:
        try:
            data = lb._post(model, parts, {"temperature": temperature,
                                           "responseMimeType": "application/json"},
                            timeout=60, operation=operation, service="text")
            raw = re.sub(r"^```[a-z]*\s*|\s*```$", "", lb._text_of(data).strip())
            out = json.loads(raw)
            if isinstance(out, list):
                out = out[0] if out else {}
            if isinstance(out, dict) and out:
                return out
            last = "empty"
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"[CHARACTERS] {operation} via {model} failed: {e}", flush=True)
    raise RuntimeError(f"{operation} failed: {last}")


def _image(parts: list, aspect: str, operation: str) -> bytes:
    lb = _lb()
    data = lb._post(IMAGE_MODEL, parts, {"responseModalities": ["IMAGE"],
                                          "imageConfig": {"aspectRatio": aspect, "imageSize": IMAGE_SIZE}},
                    IMAGE_TIMEOUT_S, operation, "image")
    raw = lb._image_of(data)
    if not raw:
        raise RuntimeError("the image model drew nothing")
    return raw


def _img_part(path: Any, max_side: int = 1536) -> dict:
    return _lb()._img_part(str(path), max_side)


def _key_phrase(key: str) -> str:
    if key == "magenta":
        return "one flat solid chroma-key magenta (#FF00FF) filling the entire image edge to edge"
    return "one flat solid chroma-key green (#00B140) filling the entire image edge to edge"


def _key_for(text: str) -> str:
    lb = _lb()
    blob = str(text or "").lower()
    greens = len(re.findall(lb._GREEN_WORDS, blob))
    pinks = len(re.findall(lb._PINK_WORDS, blob))
    return "magenta" if greens > pinks else "green"


# ── ① the brief ─────────────────────────────────────────────────────────────

_BRIEF = """You design the PLAYER CHARACTER for a cinematic, photoreal survival game. The player described who they want to be{with_pics}:

"{concept}"

Keep everything they said. Fill in what they did not, in the same spirit — specific, visual, grounded, nothing generic. If they named them, keep the name; otherwise invent a real-sounding name that fits.

Return JSON:
{{"name": "First Last",
 "pronouns": "he/him | she/her | they/them",
 "role": "what they are, 2-4 words, Title Case (e.g. Salvage Diver)",
 "tagline": "one short line under the name, ending with a full stop (e.g. Freelance photojournalist.)",
 "demeanor": "2-5 words",
 "who": "ONE paragraph for an image model, head to toe: age, build, skin, face, hair; then EVERY garment top to bottom with colour, material and wear; then everything carried, slung or holstered on the body. No setting, no pose, no lighting.",
 "held": "what they hold in a relaxed hero pose, as a short phrase (e.g. 'a battered brass helmet tucked under one arm'), or 'empty hands'"}}"""

_SURPRISE = """Invent ONE memorable player character for a cinematic, photoreal survival game — someone a player would be excited to BE. Grounded and specific, not a fantasy cliche; a real job or life, a look you could draw from a single line. Vary widely: age, build, gender, origin, era of clothing.
Return JSON: {"concept": "one or two sentences in the player's own voice, e.g. 'A salvage diver in her forties. Patched olive canvas suit, brass helmet under one arm, rope and hook at the hip.'"}"""


def surprise() -> str:
    """SURPRISE ME: a line the player could have typed."""
    if not have_key():
        return random.choice([
            "A night-shift paramedic in her thirties. Reflective green jacket, trauma bag slung across the body, hair tied back.",
            "A retired rodeo clown turned courier. Faded patchwork waistcoat, cracked boots, a dented thermos on his belt.",
            "A teenage radio ham from a mining town. Oversized parka, headphones round the neck, a homemade antenna on her back.",
        ])
    out = _text_json([{"text": _SURPRISE}], "character_surprise", temperature=1.0)
    return str(out.get("concept") or "").strip()[:400]


def _expand(rec: dict, sources: List[Path]) -> dict:
    concept = rec.get("concept") or rec.get("name") or "a survivor"
    if rec.get("name") and rec["name"].lower() not in concept.lower():
        concept = f"{rec['name']}. {concept}"
    if not have_key():
        if os.getenv("SOMEWHERE_CHARACTERS_REHEARSE"):
            try:   # the brief's text call, for the rehearsal (see _draw_rehearsal)
                time.sleep(float((os.getenv("SOMEWHERE_CHARACTERS_REHEARSE_SECS") or "22,24,4").split(",")[2]))
            except (IndexError, ValueError):
                time.sleep(4)
        seed = int(hashlib.md5(concept.encode()).hexdigest()[:6], 16)
        stand_in = ["Mara Quill", "Dev Okafor", "Ines Rook", "Cal Brennan", "Yusra Hale", "Tomas Vey"][seed % 6]
        return {"name": rec.get("name") or stand_in,
                "pronouns": rec.get("pronouns") or "they/them",
                "role": rec.get("role") or "Survivor", "tagline": rec.get("tagline") or "Survivor.",
                "demeanor": "watchful", "who": concept, "held": "empty hands"}
    parts: list = []
    for p in sources[:3]:
        parts += [{"text": "A picture the player gave of this character — who they are, not how to frame them:"},
                  _img_part(p, 1024)]
    parts.append({"text": _BRIEF.format(concept=concept.replace('"', "'"),
                                        with_pics=" (pictures attached)" if sources else "")})
    out = _text_json(parts, "character_brief", temperature=0.6)
    return {k: str(out.get(k) or "").strip() for k in
            ("name", "pronouns", "role", "tagline", "demeanor", "who", "held")}


# ── ② the turnaround ────────────────────────────────────────────────────────

# ── the art style ───────────────────────────────────────────────────────────
# Every picture of a character is drawn in ONE style, and by default it is
# photoreal: Luka "Grizzly" Novak came back as inked concept art (2026-09-24)
# because the turnaround prompt opened "CHARACTER TURNAROUND MODEL SHEET" —
# which a model reads as a drawn sheet — and said "Photoreal" once, at the
# very end. "i think having a consistent art style at first is key, but if
# players want we break it, we'll let them" (Matt): the style now LEADS the
# turnaround, the hero pose and the face sheet, and a player can replace it
# per character from the character screen (STYLE, under the name).
DEFAULT_STYLE = (
    "Photorealistic, like a character on the select screen of a modern AAA video game rendered "
    "in Unreal Engine 5: real skin with pores, stubble and fine wrinkles, real cloth weave, "
    "worn leather, scuffed metal, physically based materials, natural human proportions, a "
    "lifelike face. It is NOT a drawing: no illustration, no concept-art linework, no ink "
    "outlines, no comic or cel shading, no painterly brushwork, no anime.")


def style_of(rec: dict) -> str:
    return str((rec or {}).get("style") or "").strip() or DEFAULT_STYLE


def _style_block(rec: dict) -> str:
    return f"ART STYLE — this decides how it is rendered, whatever else is said: {style_of(rec)}\n\n"


_TURN = ("CHARACTER TURNAROUND for a video game: ONE person rendered FOUR times "
         "side by side, left to right, evenly spaced with clear gaps, identical scale, "
         "every figure full length head to toe with margin above the head and below the feet: "
         "1 FRONT view, 2 THREE-QUARTER FRONT view, 3 PROFILE view, 4 BACK view. "
         "Neutral A-pose in all four: arms held straight out 35 degrees from the body, palms "
         "in, feet shoulder-width apart, neutral expression, empty hands (anything carried is "
         "slung or holstered on the body). Flat, even, shadowless studio lighting. "
         "Background: {key} — no floor, no ground line, no cast shadow, no gradient, no "
         "vignette. No text, labels, numbers or arrows anywhere. True colours, sharp. "
         "THE CHARACTER: ")

_FIT = ("The attached CHARACTER TURNAROUND shows ONE person in four views. Redraw the SAME "
        "turnaround — same person, same four views in the same order, same A-pose, same "
        "framing and scale, and the same flat {key_name} background — with these changes "
        "and NO others:\n{changes}\nEverything else — face, hair, build and every other "
        "garment — stays exactly as it is. The changed pieces must show in all four views, "
        "from the right side in each (the back of a helmet in the back view). Render it in "
        "exactly the same art style as the attached turnaround{restyle}. No text.")

# The hero shot. The turnaround is shot in an A-pose on purpose (the four
# views have to line up); drawn "relaxed, confident", the idle kept leaning
# back into it — arms held off the body, feet square — and read as the model
# sheet, not a character (2026-09-24). So it is asked for attitude, and told
# plainly which pose NOT to copy.
_IDLE = ("Draw the person from the CHARACTER TURNAROUND — same face, hair, build, and every "
         "garment and item exactly — ONCE, full length head to toe, as the hero shot on a "
         "video game's character-select screen: a strong silhouette with attitude that says "
         "who they are ({manner}). The body turned 30-40 degrees away from the camera, head "
         "turned back toward it with an expression that fits them; weight on the back leg, "
         "the front knee bent and that foot stepped forward and angled out; shoulders and "
         "hips tilted against each other; one hand busy (in hand: {held} — or a thumb hooked "
         "in a strap, a hand resting on their gear, a hand at the collar), the other loose. "
         "Grounded and believable, like a film still — not a superhero or fighting stance. "
         "NOT the turnaround's A-pose: the arms are NOT held out from the body, the feet are "
         "NOT side by side, the body does NOT face the camera square-on. Cinematic lighting: "
         "a cool rim light from behind left, a soft warm key from front right. Background: "
         "{key} — no floor, no cast shadow, no text. Leave clear margin above the head and "
         "below the feet.")

_FACE = ("Draw the person from the CHARACTER TURNAROUND as a close-up reference: TWO head-"
         "and-shoulders portraits side by side of that SAME person — left facing the camera, "
         "right in profile — same face, hair, skin and everything worn on the head and neck. "
         "Anything that covers the face in the turnaround — a mask, a visor, a respirator, a "
         "helmet — stays on, exactly as drawn: never reveal the face under it. "
         "Neutral expression, flat even studio light, plain mid-grey background, no text.")

_INSPECT = """The image is a CHARACTER TURNAROUND for a video game: it should be ONE person drawn four times, left to right — front, three-quarter, profile, back — full length in an A-pose, on a flat plain background.

Check it and describe it. Return JSON:
{"check": {"figures": <how many full figures>, "same_person_and_outfit": true/false, "back_view_is_a_back": true/false, "full_length": true/false, "problem": "one short sentence if something is wrong, else ''"},
 "body": "one line: apparent age, build, skin, face, hair",
 "garments": {"head": "", "face": "", "neck": "", "torso": "", "outer": "", "hands": "", "legs": "", "feet": "", "back": "", "held": ""},
 "wardrobe_line": "everything worn and carried as a short comma list, top to bottom, the way a game lists it (e.g. 'White tee, blue PRESS flak vest, red bandana, jeans, work boots, 35mm camera')",
 "palette": "3-5 colour words"}
Each garment entry: what is actually drawn there — colour, material, one telling detail — or '' if nothing. Describe ONLY what is in the picture."""


def _tight(cut):
    import numpy as np
    a = np.asarray(cut)[..., 3]
    ys, xs = np.nonzero(a > 8)
    if not len(xs):
        return cut
    return cut.crop((int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1))


def split_views(cut, n: int = 4) -> list:
    """The turnaround's figures, by connected columns of alpha — models do not
    respect equal columns, so the gaps are found, not assumed."""
    import numpy as np
    a = np.asarray(cut)[..., 3] > 20
    col = a.sum(0)
    on = col > max(2, a.shape[0] * 0.01)
    segs, s = [], None
    for x, v in enumerate(on):
        if v and s is None:
            s = x
        if not v and s is not None:
            segs.append((s, x))
            s = None
    if s is not None:
        segs.append((s, len(on)))
    segs = [g for g in segs if g[1] - g[0] > max(6, a.shape[1] * 0.02)]
    segs = sorted(sorted(segs, key=lambda t: t[1] - t[0], reverse=True)[:n])
    return [_tight(cut.crop((x0, 0, x1, cut.height))) for x0, x1 in segs]


def _mechanical_check(cut) -> str:
    """'' when the strip is four whole figures, else what is wrong."""
    if cut is None:
        return "the background would not come off (not a flat key colour)"
    views = split_views(cut)
    if len(views) < 4:
        return f"only {len(views)} separate figure(s) came out, not four"
    hs = [v.height for v in views]
    if min(hs) < 0.72 * max(hs):
        return "the four figures are not drawn at the same scale"
    return ""


def _flat(cut, bg=(118, 118, 118)):
    from PIL import Image
    ref = Image.new("RGB", cut.size, bg)
    ref.paste(cut, (0, 0), cut)
    return ref


def _save_img(img, path: Path, **kw) -> None:
    """Every picture a screen can ask for is written whole or not at all:
    to a temp name, then renamed over. Filmed 2026-09-24: the character
    screen's poll found idle.png while PIL was still writing it, the browser
    fetched the half-file, and — its URL keyed on the file's mtime second —
    kept showing just the head and shoulders."""
    path = Path(path)
    tmp = path.with_name(f".{path.stem}.{uuid.uuid4().hex[:6]}{path.suffix}")
    img.save(tmp, **kw)
    os.replace(tmp, path)


def _write_atomic(path: Path, data: bytes) -> None:
    path = Path(path)
    tmp = path.with_name(f".{path.stem}.{uuid.uuid4().hex[:6]}{path.suffix}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _save_ref(img, path: Path, max_side: int = REF_MAX) -> None:
    im = img.convert("RGB")
    im.thumbnail((max_side, max_side))
    _save_img(im, path, quality=92)


def _thumb_from(idle_path, out: Path) -> None:
    """The roster square: head and shoulders off the idle (a path or an image)."""
    from PIL import Image
    im = (idle_path if hasattr(idle_path, "convert") else Image.open(idle_path)).convert("RGBA")
    w, h = im.size
    side = min(w, int(h * 0.42))
    x0 = max(0, (w - side) // 2)
    crop = im.crop((x0, 0, x0 + side, side))
    crop = crop.resize((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
    bg = Image.new("RGBA", crop.size, (22, 22, 20, 255))
    bg.alpha_composite(crop)
    _save_img(bg, out)


def _cut(raw: bytes):
    from PIL import Image
    return _lb()._cut_out(Image.open(io.BytesIO(raw)))


def _stage(cid: str, look_id: str, stage: str, line: str, progress: float) -> None:
    def fn(rec):
        job = rec.setdefault("job", {})
        ln = line
        # The brief runs beside the turnaround and can name them between the
        # line being written and this landing: read the name under the lock.
        first = str(rec.get("name") or "").split(" ")[0]
        if first and stage == "turnaround":
            ln = re.sub(r"^Drawing (them|him|her) ", f"Drawing {first} ", ln)
        job.update({"stage": stage, "line": ln, "progress": round(progress, 2)})
    _update(cid, fn)
    try:
        look = load_look(cid, look_id)
        if look:
            look["stage"] = stage
            save_look(cid, look)
    except Exception:
        pass


def _pronoun(rec: dict) -> str:
    p = str(rec.get("pronouns") or "").lower()
    return "her" if p.startswith("she") else "him" if p.startswith("he") else "them"


def _pose(cid: str, look_id: str, *, rec: dict, key: str, held: str,
          changes: Optional[List[str]] = None, pose_from: str = "") -> Tuple[bool, str]:
    """The hero pose for one look, drawn off its turnaround (two tries).
    Writes idle_key.png and the cut-out idle.png; (ok, reason)."""
    d = look_dir(cid, look_id)
    parts = [{"text": "CHARACTER TURNAROUND — the person to draw."},
             _img_part(d / "turnaround_key.png", 2048)]
    if pose_from and (look_dir(cid, pose_from) / "idle_key.png").is_file():
        # Seen 2026-09-24: labelled only "copy the stance", the fitted idle
        # came back as the old idle, helmet and all — the pose reference
        # outvoted the new turnaround. It is out of date on purpose.
        parts += [{"text": "POSE REFERENCE — copy ONLY this stance and camera. It shows them "
                           "BEFORE the change: its clothes and gear are out of date. Every "
                           "garment comes from the CHARACTER TURNAROUND."},
                  _img_part(look_dir(cid, pose_from) / "idle_key.png")]
    manner = ", ".join(x for x in (rec.get("role"), rec.get("demeanor")) if x) or "a survivor"
    text = _style_block(rec) + _IDLE.format(held=held, key=_key_phrase(key), manner=manner) + (
        " Render it in exactly the same art style as the CHARACTER TURNAROUND.")
    if changes:
        text += ("\nWHAT IS NEW ON THEM — it must be plainly visible in this pose, exactly as the "
                 "turnaround shows it:\n" + "\n".join(f"- {c}" for c in changes))
    parts.append({"text": text})
    err = "would not cut out"
    for _ in (1, 2):
        try:
            r = _image(parts, "3:4", "character_idle")
            c = _cut(r)
            if c is None:
                continue
            _write_atomic(d / "idle_key.png", r)
            _save_img(_tight(c), d / "idle.png")
            return True, ""
        except Exception as e:  # noqa: BLE001
            err = str(e)[:120]
    return False, err


def repose(cid: str) -> Dict[str, str]:
    """Draw every look's hero pose again with today's pose prompt — for
    characters posed before it (2026-09-24: stiff, arms off the body, the
    turnaround's A-pose showing through). The base first, from nothing; each
    fitting after it, posed like the new base. The turnarounds, the words and
    the face sheets are untouched. Returns {look_id: "ok" | reason}."""
    rec = load(cid) or {}
    if not rec or not have_key():
        return {}
    base = rec.get("base_look") or ""
    key = rec.get("key") or "green"
    order = [base] + [lid for lid in (rec.get("looks") or {}) if lid != base]
    out: Dict[str, str] = {}
    for lid in order:
        d = look_dir(cid, lid)
        if not lid or not (d / "turnaround_key.png").is_file():
            continue
        look = load_look(cid, lid) or {}
        changes = look.get("changes") or []
        held = rec.get("held") or "empty hands"
        ok, err = _pose(cid, lid, rec=rec, key=key, held=held, changes=changes,
                        pose_from="" if lid == base else base)
        if ok:
            _thumb_from(d / "idle.png", d / "thumb.png")
        out[lid] = "ok" if ok else err
        print(f"[CHARACTERS] {cid}/{lid} re-posed: {out[lid]}", flush=True)
    return out


def _draw_look(cid: str, look_id: str, *, fit_from: str = "", changes: List[str] = None,
               item_parts: list = None, pose_from: str = "", on_ready=None) -> dict:
    """Render one look into looks/<look_id>/: turnaround (checked, one retry),
    cut-out, the grey reference, the read-back, then the idle and the face in
    parallel. Raises with a player-readable reason.

    ``on_ready`` is called the moment the TURNAROUND is saved — the only thing
    the simulation needs. The character is playable from then on; the
    read-back, the hero idle and the face sheet finish behind it. The screens
    wait for the hero idle to show a portrait (see card(): a fitting shows the
    look it was fitted onto meanwhile).
    Measured 2026-09-24: the turnaround lands ~25-30 s in and the finish takes
    another ~25, so this is what halves the wait."""
    rec = load(cid) or {}
    d = look_dir(cid, look_id)
    d.mkdir(parents=True, exist_ok=True)
    key = rec.get("key") or "green"
    t0 = time.time()
    him = _pronoun(rec)
    sources = [char_dir(cid) / "sources" / s for s in rec.get("sources") or []]
    sources = [p for p in sources if p.is_file()]

    if os.getenv("SOMEWHERE_CHARACTERS_REHEARSE") and not have_key():
        return _draw_rehearsal(cid, look_id, on_ready, him, fit=bool(fit_from))
    if not have_key():
        got = _draw_offline(cid, look_id, changes or [])
        if on_ready:
            on_ready()
        return got

    problem, cut, raw = "", None, b""
    for attempt in (1, 2):
        # The brief runs beside this and may already have named them.
        first = str((load(cid) or {}).get("name") or "").split(" ")[0]
        _stage(cid, look_id, "turnaround",
               f"Drawing {first or him} from every side…" if attempt == 1 else "Drawing again — the first one was wrong…",
               0.12 if attempt == 1 else 0.3)
        if fit_from:
            base = look_dir(cid, fit_from) / "turnaround_key.png"
            parts = [{"text": "CHARACTER TURNAROUND — the person, as they are now:"}, _img_part(base, 2048)]
            parts += list(item_parts or [])
            restyle = rec.get("_restyle") or ""
            parts.append({"text": _FIT.format(key_name="green" if key == "green" else "magenta",
                                              changes="\n".join(f"- {c}" for c in (changes or [])),
                                              restyle=(" — EXCEPT the rendering, which changes completely to "
                                                       "the ART STYLE below; the person, their face and "
                                                       "everything they wear stay the same") if restyle else "")
                          + (f"\n\n{_style_block(rec)}" if restyle else "")
                          + (f"\nThe last attempt was wrong: {problem}" if problem else "")})
        else:
            parts = []
            for p in sources[:3]:
                parts += [{"text": "Reference — THIS person: copy face, hair, build and every "
                                   "garment. Do not copy the pose, framing, light or background."},
                          _img_part(p)]
            parts.append({"text": _style_block(rec) + _TURN.format(key=_key_phrase(key)) +
                          (rec.get("who") or rec.get("concept") or "")
                          + (f"\nThe last attempt was wrong: {problem}. Fix that." if problem else "")})
        try:
            raw = _image(parts, "16:9", "character_turnaround" if not fit_from else "character_fitting")
        except Exception as e:  # noqa: BLE001
            problem = f"the image model did not answer ({str(e)[:80]})"
            continue
        cut = _cut(raw)
        problem = _mechanical_check(cut)
        if problem:
            print(f"[CHARACTERS] {cid}/{look_id} turnaround rejected: {problem}", flush=True)
            (d / f"rejected_{attempt}.png").write_bytes(raw)
            continue
        cut = _tight(cut)
        break
    if problem:
        raise RuntimeError(problem)

    # The roster square, cut off the turnaround's three-quarter view (a head
    # and shoulders — the sheet's A-pose never shows in it). There is NO
    # stand-in idle any more: the three-quarter view in the sheet's A-pose
    # was shown as the portrait while the hero pose drew, and "why is he in
    # an A pose" (Matt, 2026-09-24). A screen shows a posed portrait or none.
    views = split_views(cut)
    if views:
        _thumb_from(views[1] if len(views) > 1 else views[0], d / "thumb.png")
    _write_atomic(d / "turnaround_key.png", raw)
    _save_img(cut, d / "turnaround.png")
    _save_ref(_flat(cut), d / "turnaround_ref.jpg")
    t_ready = round(time.time() - t0, 1)
    if on_ready:
        try:
            on_ready()
        except Exception as e:  # noqa: BLE001
            print(f"[CHARACTERS] on_ready failed: {e}", flush=True)

    # The read-back, the idle and the face all need only the turnaround, so
    # they run side by side, behind a character that can already be played.
    inspect: dict = {}
    idle_done = {"ok": False}

    def read_back():
        try:
            inspect.update(_text_json([_img_part(d / "turnaround_ref.jpg", 1600), {"text": _INSPECT}],
                                      "character_readback", temperature=0))
        except Exception as e:  # noqa: BLE001
            print(f"[CHARACTERS] read-back failed ({e}) — the words fall back to the brief", flush=True)

    held = rec.get("held") or "empty hands"
    worn_held = [c for c in (changes or []) if "carried at the ready" in c]
    if worn_held:
        held = "holding the new " + worn_held[0].split(":")[0].replace("- ", "").strip() + " at the ready"
    errs: Dict[str, str] = {}

    def idle():
        ok, err = _pose(cid, look_id, rec=rec, key=key, held=held, changes=changes, pose_from=pose_from)
        if ok:
            idle_done["ok"] = True
        else:
            errs["idle"] = err

    def face():
        try:
            r = _image([{"text": "CHARACTER TURNAROUND — the person."},
                        _img_part(d / "turnaround_key.png", 2048),
                        {"text": _style_block(rec) + _FACE}],
                       "16:9", "character_face")
            from PIL import Image
            _save_ref(Image.open(io.BytesIO(r)), d / "face_ref.jpg", 1400)
        except Exception as e:  # noqa: BLE001
            errs["face"] = str(e)[:120]

    th = [threading.Thread(target=f, daemon=True) for f in (read_back, idle, face)]
    for t in th:
        t.start()
    for t in th:
        t.join(IMAGE_TIMEOUT_S * 2 + 30)
    chk = inspect.get("check") or {}
    if chk and (chk.get("same_person_and_outfit") is False or int(chk.get("figures") or 4) < 4):
        print(f"[CHARACTERS] {cid}/{look_id} vision check: {chk.get('problem') or chk}", flush=True)
    if not idle_done["ok"]:
        idle()          # a third try, alone
    if not idle_done["ok"]:
        # Nothing posed came back: the three-quarter view off the sheet is
        # the last resort, so the character is never without a figure.
        print(f"[CHARACTERS] idle failed ({errs.get('idle')}) — the three-quarter view stands in", flush=True)
        if views:
            _save_img(views[1] if len(views) > 1 else views[0], d / "idle.png")
    else:
        _thumb_from(d / "idle.png", d / "thumb.png")
    garments = {k: str(v or "").strip() for k, v in (inspect.get("garments") or {}).items()
                if k in SLOTS and str(v or "").strip()}
    return {"body": str(inspect.get("body") or "").strip(),
            "garments": garments,
            "wardrobe_line": str(inspect.get("wardrobe_line") or "").strip(),
            "palette": str(inspect.get("palette") or "").strip(),
            "check": chk, "secs": round(time.time() - t0, 1), "ready_secs": t_ready,
            "model": IMAGE_MODEL, "errors": errs}


def _draw_rehearsal(cid: str, look_id: str, on_ready, him: str, fit: bool = False) -> dict:
    """A dress rehearsal of the real pipeline for the SCREENS, with no model
    calls: the same stages, lines and timing, and a real character's pictures.

        SOMEWHERE_CHARACTERS_REHEARSE=<a look folder with real renders>
        SOMEWHERE_CHARACTERS_REHEARSE_SECS=<to playable>,<to finished>  (22,24)

    Built to film the character screen's transitions at the speed a player
    meets them (2026-09-24) — offline, a character is drawn in a blink and
    the progress, the reveal and the cross-fade are never seen."""
    # A fitting rehearses from SOMEWHERE_CHARACTERS_REHEARSE_FIT (a look with
    # the thing on) when it is set, so the pack can be filmed putting it on.
    src = Path((fit and os.getenv("SOMEWHERE_CHARACTERS_REHEARSE_FIT")) or
               os.getenv("SOMEWHERE_CHARACTERS_REHEARSE") or "")
    if not src.is_absolute():
        src = ROOT / src
    try:
        t_play, t_fin = [float(x) for x in (os.getenv("SOMEWHERE_CHARACTERS_REHEARSE_SECS") or "22,24").split(",")[:2]]
    except ValueError:
        t_play, t_fin = 22.0, 24.0
    d = look_dir(cid, look_id)
    t0 = time.time()
    first = str((load(cid) or {}).get("name") or "").split(" ")[0]
    _stage(cid, look_id, "turnaround", f"Drawing {first or him} from every side…", 0.12)
    time.sleep(t_play)
    from PIL import Image
    views = split_views(Image.open(src / "turnaround.png"))
    if views:
        _thumb_from(views[1] if len(views) > 1 else views[0], d / "thumb.png")
    for f in ("turnaround_key.png", "turnaround.png", "turnaround_ref.jpg"):
        _write_atomic(d / f, (src / f).read_bytes())
    ready_secs = round(time.time() - t0, 1)
    if on_ready:
        on_ready()
    time.sleep(t_fin)
    for f in ("idle_key.png", "idle.png", "face_ref.jpg"):
        _write_atomic(d / f, (src / f).read_bytes())
    _thumb_from(d / "idle.png", d / "thumb.png")
    try:
        words = json.loads((src / "look.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        words = {}
    return {"body": words.get("body", ""), "garments": words.get("garments", {}),
            "wardrobe_line": words.get("wardrobe_line", ""), "palette": words.get("palette", ""),
            "check": {}, "secs": round(time.time() - t0, 1), "ready_secs": ready_secs,
            "model": "rehearsal", "errors": {}}


def _draw_offline(cid: str, look_id: str, changes: List[str]) -> dict:
    """Mock mode: a stand-in figure so the whole flow — the screens, the run,
    the pack — works with no key. Clearly a placeholder, never mistaken for art."""
    from PIL import Image, ImageDraw
    rec = load(cid) or {}
    d = look_dir(cid, look_id)
    h = int(hashlib.md5((cid + look_id).encode()).hexdigest()[:6], 16)
    col = (80 + h % 150, 80 + (h >> 8) % 150, 80 + (h >> 16) % 150, 255)

    def figure(w, hgt, back=False):
        im = Image.new("RGBA", (w, hgt), (0, 0, 0, 0))
        dr = ImageDraw.Draw(im)
        cx = w // 2
        dr.ellipse((cx - w * 0.12, hgt * 0.02, cx + w * 0.12, hgt * 0.16), fill=col)
        dr.rectangle((cx - w * 0.2, hgt * 0.17, cx + w * 0.2, hgt * 0.55), fill=col)
        dr.polygon([(cx - w * 0.2, hgt * 0.18), (cx - w * 0.46, hgt * 0.5), (cx - w * 0.4, hgt * 0.53), (cx - w * 0.18, hgt * 0.26)], fill=col)
        dr.polygon([(cx + w * 0.2, hgt * 0.18), (cx + w * 0.46, hgt * 0.5), (cx + w * 0.4, hgt * 0.53), (cx + w * 0.18, hgt * 0.26)], fill=col)
        dr.rectangle((cx - w * 0.18, hgt * 0.55, cx - w * 0.03, hgt * 0.98), fill=col)
        dr.rectangle((cx + w * 0.03, hgt * 0.55, cx + w * 0.18, hgt * 0.98), fill=col)
        if changes:
            dr.rectangle((cx - w * 0.14, 0, cx + w * 0.14, hgt * 0.06), fill=(30, 30, 30, 255))
        return im
    strip = Image.new("RGBA", (4 * 260, 620), (0, 0, 0, 0))
    for i in range(4):
        strip.alpha_composite(figure(220, 600, back=i == 3), (i * 260 + 20, 10))
    key = Image.new("RGB", strip.size, (0, 177, 64))
    key.paste(strip, (0, 0), strip)
    key.save(d / "turnaround_key.png")
    strip.save(d / "turnaround.png")
    _save_ref(_flat(strip), d / "turnaround_ref.jpg")
    idle = figure(360, 900)
    idle.save(d / "idle.png")
    idle_key = Image.new("RGB", idle.size, (0, 177, 64))
    idle_key.paste(idle, (0, 0), idle)
    idle_key.save(d / "idle_key.png")
    _save_ref(_flat(figure(300, 300)), d / "face_ref.jpg", 600)
    _thumb_from(d / "idle.png", d / "thumb.png")
    worn = ", ".join(c.split(":")[0].lstrip("- ") for c in changes)
    who = rec.get("who") or rec.get("concept") or ""
    return {"body": who[:160], "garments": {},
            "wardrobe_line": (who[:120] + (f"; now wearing {worn}" if worn else "")).strip(),
            "palette": "", "check": {}, "secs": 0.0, "model": "offline", "errors": {}}


# ═══════════════════════════════════════════════════════════════════════════
# JOBS — creating, redrawing, revising, fitting
# ═══════════════════════════════════════════════════════════════════════════

def _learned_secs(key: str, default: int) -> int:
    got = []
    try:
        for f in CHARACTERS_DIR.glob("*/looks/*/look.json"):
            try:
                got.append((f.stat().st_mtime, float(json.loads(f.read_text(encoding="utf-8")).get(key) or 0)))
            except (OSError, ValueError, TypeError):
                continue
    except OSError:
        pass
    secs = sorted(v for _, v in sorted(got)[-6:] if v > 0)
    if not secs:
        return default
    return int(max(8, min(180, round(secs[len(secs) // 2] + 2))))


def _total_eta() -> int:
    """Until the hero pose is on screen — what the portrait counts down to."""
    return max(_learned_secs("secs", READY_ETA_S + 24), _ready_eta() + 10)


def _ready_eta() -> int:
    """What the screen counts down from: how long the last few drawings on
    THIS machine took to become playable (the look's ``ready_secs``), not a
    constant — a bar that stops at two thirds and then jumps reads as a
    guess (2026-09-24)."""
    got = []
    try:
        for f in CHARACTERS_DIR.glob("*/looks/*/look.json"):
            try:
                got.append((f.stat().st_mtime, float(json.loads(f.read_text(encoding="utf-8")).get("ready_secs") or 0)))
            except (OSError, ValueError, TypeError):
                continue
    except OSError:
        pass
    secs = sorted(v for _, v in sorted(got)[-6:] if v > 0)
    if not secs:
        return READY_ETA_S
    return int(max(8, min(120, round(secs[len(secs) // 2] + 2))))


def _new_look_id(kind: str) -> str:
    return f"{kind}-{uuid.uuid4().hex[:6]}"


def _spawn(cid: str, fn) -> None:
    def run():
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            print(f"[CHARACTERS] {cid}: {e}", flush=True)
        finally:
            with _LOCK:
                _JOBS.pop(cid, None)
    th = threading.Thread(target=run, daemon=True, name=f"character-{cid}")
    with _LOCK:
        _JOBS[cid] = th
    th.start()


def busy(cid: str) -> bool:
    with _LOCK:
        th = _JOBS.get(cid)
        return bool(th and th.is_alive())


def _data_url_bytes(data_url: str) -> Tuple[bytes, str]:
    m = re.match(r"^data:(image/(png|jpe?g|webp|gif));base64,(.+)$", str(data_url or ""), re.S)
    if not m:
        raise ValueError("not an image")
    raw = base64.b64decode(m.group(3))
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("picture is over 8 MB")
    ext = {"png": ".png", "jpeg": ".jpg", "jpg": ".jpg", "webp": ".webp", "gif": ".gif"}[m.group(2)]
    return raw, ext


def create(concept: str = "", *, name: str = "", pictures: List[str] = None,
           draw: bool = True, wait: bool = False, style: str = "") -> dict:
    """A new character from one line (and any pictures), drawn in the
    background. Poll ``load`` / the API for ``status``."""
    concept = str(concept or "").strip()
    pictures = [p for p in (pictures or []) if p][:3]
    if not concept and not pictures and not name:
        raise ValueError("say who they are")
    ensure_seeded()
    rec = _new_record(name, concept or "the person in the picture", "created")
    style = str(style or "").strip()[:800]
    # Only a style that differs from the default is stored: "" means "the
    # game's style", so a future change to the default reaches them too.
    rec["style"] = "" if style == DEFAULT_STYLE else style
    d = char_dir(rec["id"])
    (d / "sources").mkdir(parents=True, exist_ok=True)
    for i, pic in enumerate(pictures):
        raw, ext = _data_url_bytes(pic)
        fn = f"source_{i + 1}{ext}"
        (d / "sources" / fn).write_bytes(raw)
        rec["sources"].append(fn)
    save(rec)
    if draw:
        start_draw(rec["id"], wait=wait)
    return load(rec["id"]) or rec


def start_draw(cid: str, *, concept: Optional[str] = None, wait: bool = False) -> dict:
    """(Re)draw a character from its brief: a new BASE look. TRY AGAIN and
    DRAW on an imported cast sheet both land here."""
    if busy(cid):
        return load(cid) or {}
    look_id = _new_look_id("base")

    def begin(rec):
        if concept is not None and str(concept).strip() and str(concept).strip() != rec.get("concept"):
            rec["concept"] = str(concept).strip()[:1200]
            rec["who"] = ""          # the old brief described someone else
        # Decided before anything is drawn: the turnaround is shot on it.
        rec["key"] = _key_for(rec.get("who") or rec.get("concept") or "")
        # A character that already has a body stays playable while it is
        # redrawn; only a first drawing is "drawing".
        if not rec.get("base_look"):
            rec["status"] = STATUS_DRAWING
        rec["error"] = ""
        rec["job"] = {"kind": "create" if not rec.get("base_look") else "redraw", "look": look_id,
                      "stage": "turnaround", "line": "Picturing them…", "progress": 0.04,
                      "started": _now(), "eta": _ready_eta(), "eta_total": _total_eta()}
        rec["wanted_look"] = look_id
        rec["looks"][look_id] = {"kind": "base", "status": STATUS_DRAWING, "worn": [], "created": _now()}
    _update(cid, begin)

    def work():
        rec = load(cid) or {}
        sources = [char_dir(cid) / "sources" / s for s in rec.get("sources") or []]
        # The BRIEF (name, pronouns, the line under the name, the words for
        # the image) is a text call that used to run before the turnaround —
        # ~5 s of nothing on screen. The turnaround is drawn from the
        # player's own line (the image model fills in what they did not
        # say), so the two now run side by side; the brief has always landed
        # by the time the turnaround has.
        brief: Dict[str, str] = {}

        def write_brief():
            try:
                brief.update(_expand(rec, [p for p in sources if p.is_file()]))
            except Exception as e:  # noqa: BLE001
                print(f"[CHARACTERS] {cid}: the brief failed ({e}) — the line stands in", flush=True)
                return

            # The name lands ~5 s in, long before the picture: the screen
            # shows it while the turnaround is still drawing — who they are
            # arrives first, then what they look like.
            def named(r):
                if (r.get("job") or {}).get("look") != look_id:
                    return
                if brief.get("name") and not (r.get("name") and r.get("origin") == "cast_sheet"):
                    r["name"] = brief["name"]
                for k in ("pronouns", "tagline", "role"):
                    if brief.get(k):
                        r[k] = brief[k]
                first = str(r.get("name") or "").split(" ")[0]
                if first and r["job"].get("stage") == "turnaround":
                    r["job"]["line"] = f"Drawing {first} from every side…"
                    r["job"]["progress"] = max(float(r["job"].get("progress") or 0), 0.2)
            _update(cid, named)
        tb = threading.Thread(target=write_brief, daemon=True)
        tb.start()
        try:
            save_look(cid, {"id": look_id, "kind": "base", "parent": "", "worn": [],
                            "status": STATUS_DRAWING, "stage": "turnaround", "created": _now()})

            def ready():
                tb.join(60)

                def put(r):
                    for k in ("name", "pronouns", "role", "tagline", "demeanor", "who", "held"):
                        if brief.get(k) and not (k == "name" and r.get("name") and r.get("origin") == "cast_sheet"):
                            r[k] = brief[k]
                    if not r.get("name"):
                        r["name"] = (r.get("concept") or "Nobody").split(".")[0][:40]
                    r["looks"][look_id] = {"kind": "base", "status": STATUS_READY, "worn": [], "created": _now()}
                    r["base_look"] = look_id
                    r["current_look"] = look_id
                    r["wanted_look"] = look_id
                    # A new body: every outfit fitted onto the old one is stale.
                    r["outfits"] = {_outfit_key(look_id, []): look_id}
                    for it in r.get("inventory") or []:
                        it["worn"] = False
                    r["status"] = STATUS_READY
                    r["error"] = ""
                    first = str(r.get("name") or "").split(" ")[0] or "them"
                    r["job"] = dict(r.get("job") or {}, stage="finishing", progress=0.5,
                                    line=f"Posing {first} for the portrait…")
                _update(cid, put)
                print(f"[CHARACTERS] {cid} is playable", flush=True)

            got = _draw_look(cid, look_id, on_ready=ready)
            look = load_look(cid, look_id) or {"id": look_id}
            look.update(got)
            look["status"] = STATUS_READY
            look["finished"] = _now()
            save_look(cid, look)

            def done(r):
                if (r.get("job") or {}).get("look") == look_id:
                    r["job"] = {}
            _update(cid, done)
            print(f"[CHARACTERS] {cid} drawn (playable at {look.get('ready_secs')}s, finished at "
                  f"{look.get('secs')}s): {look.get('wardrobe_line')!r}", flush=True)
        except Exception as e:  # noqa: BLE001
            reason = str(e)[:200]

            def failed(r):
                r["looks"].pop(look_id, None)
                r["wanted_look"] = r.get("current_look") or ""
                r["status"] = STATUS_READY if r.get("base_look") else STATUS_FAILED
                r["error"] = reason
                r["job"] = {}
            _update(cid, failed)
            print(f"[CHARACTERS] {cid} could not be drawn: {reason}", flush=True)

    if wait:
        work()
    else:
        _spawn(cid, work)
    return load(cid) or {}


def restyle(cid: str, style: str, *, wait: bool = False) -> dict:
    """STYLE: the same person redrawn in another art style — a fitting on
    the base (their face, build and everything they wear kept) whose only
    change is the rendering, which then IS the base. The hero pose and the
    face sheet follow it. "" or the default text puts them back to the
    game's style."""
    style = str(style or "").strip()[:800]
    if style == DEFAULT_STYLE:
        style = ""
    rec = load(cid)
    if not rec:
        raise ValueError("no such character")
    # A character drawn before styles existed has no "style" at all — what it
    # was drawn in is unknown (Luka came back as ink under the old prompt), so
    # any style, the default included, redraws it.
    if "style" in rec and style == (rec.get("style") or ""):
        raise ValueError("that is already their style")
    if not rec.get("base_look"):
        _update(cid, lambda r: r.update(style=style))
        return start_draw(cid, wait=wait)
    was = rec.get("style") or ""
    _update(cid, lambda r: r.update(style=style, _restyle={"was": was}))
    try:
        return _start_fit(cid, [], changes=["THE RENDERING ONLY: redraw all four figures in the ART STYLE "
                                            "given below."], rebase=True, wait=wait)
    except Exception:
        _update(cid, lambda r: (r.update(style=was), r.pop("_restyle", None)))
        raise


def revise(cid: str, change: str, *, wait: bool = False) -> dict:
    """CHANGE SOMETHING: one line ("make the jacket a long waxed duster")
    redrawn onto the base as a fitting, which then IS the base."""
    change = str(change or "").strip()[:300]
    rec = load(cid)
    if not rec or not rec.get("base_look") or not change:
        raise ValueError("nothing to change")
    return _start_fit(cid, [], changes=[change], rebase=True, wait=wait)


# ── fittings ────────────────────────────────────────────────────────────────

def _outfit_key(base: str, worn: List[str]) -> str:
    return base + "|" + ",".join(sorted(set(worn)))


def _item_change(it: dict) -> str:
    slot = it.get("slot") or "outer"
    look = str(it.get("look") or "").strip().rstrip(".")
    return f"{it.get('name')}: {SLOT_WORDS.get(slot, 'worn')}" + (f" — {look}" if look else "")


def _start_fit(cid: str, worn_ids: List[str], *, changes: List[str] = None,
               rebase: bool = False, wait: bool = False) -> dict:
    rec = load(cid) or {}
    base = rec.get("base_look") or ""
    if not base:
        raise ValueError("this character has not been drawn yet")
    items = {it["id"]: it for it in rec.get("inventory") or [] if it.get("id")}
    worn = [i for i in worn_ids if i in items]
    key = _outfit_key(base, worn)
    if not rebase and not changes:
        have = (rec.get("outfits") or {}).get(key)
        if have and (rec.get("looks") or {}).get(have, {}).get("status") in (STATUS_READY, STATUS_DRAWING):
            def point(r):
                r["wanted_look"] = have
                if r["looks"][have]["status"] == STATUS_READY:
                    r["current_look"] = have
            _update(cid, point)
            return load(cid) or {}
    look_id = _new_look_id("base" if rebase else "fit")
    # A new base (STYLE, CHANGE SOMETHING) is drawn bare, from the old base —
    # and what they have on goes back on over it afterwards. It used to be
    # dropped: restyling Luka took his riot visor off, and every scene after
    # it drew him without it while the pack still said WEARING.
    keep: List[str] = []
    if rebase:
        cur = (rec.get("looks") or {}).get(rec.get("current_look") or "") or {}
        keep = [i for i in (cur.get("worn") or []) if i in items]
    all_changes = [_item_change(items[i]) for i in worn] + list(changes or [])
    item_parts: list = []
    for i in worn:
        it = items[i]
        if it.get("ref") and (char_dir(cid) / "items" / it["ref"]).is_file():
            item_parts += [{"text": f"ITEM TO PUT ON — {it.get('name')} ({SLOT_WORDS.get(it.get('slot') or 'outer')}):"},
                           _img_part(char_dir(cid) / "items" / it["ref"], 768)]

    def begin(r):
        r["looks"][look_id] = {"kind": "base" if rebase else "fit", "status": STATUS_DRAWING,
                               "worn": [] if rebase else worn, "created": _now()}
        if not rebase:
            r.setdefault("outfits", {})[key] = look_id
        r["wanted_look"] = look_id
        r["job"] = {"kind": "revise" if rebase else "fit", "look": look_id, "stage": "turnaround",
                    "line": "Redrawing them in the new style…" if r.get("_restyle") else "Suiting up…", "progress": 0.08, "started": _now(), "eta": _ready_eta(), "eta_total": _total_eta()}
    _update(cid, begin)
    save_look(cid, {"id": look_id, "kind": "base" if rebase else "fit", "parent": base,
                    "worn": [] if rebase else worn, "changes": all_changes,
                    "status": STATUS_DRAWING, "stage": "turnaround", "created": _now()})

    def work():
        try:
            def ready():
                # The new turnaround is in: that is all the simulation reads, so
                # the run can take this look from its next turn while the words,
                # the idle and the face finish behind it.
                def point(r):
                    r["looks"][look_id]["status"] = STATUS_READY
                    if rebase:
                        r["base_look"] = look_id
                        r["outfits"] = {_outfit_key(look_id, []): look_id}
                        if not keep:
                            for it in r.get("inventory") or []:
                                it["worn"] = False
                            r["current_look"] = look_id
                            r["wanted_look"] = look_id
                        # else: they stay in the look they have on until it
                        # is redrawn over the new base (below) — never bare
                        # in between.
                    elif r.get("wanted_look") == look_id:
                        r["current_look"] = look_id
                    if (r.get("job") or {}).get("look") == look_id:
                        r["job"] = dict(r["job"], stage="finishing", progress=0.5,
                                        line="Posing them in it…")
                _update(cid, point)

            got = _draw_look(cid, look_id, fit_from=base, changes=all_changes,
                             item_parts=item_parts, pose_from=base, on_ready=ready)
            look = load_look(cid, look_id) or {"id": look_id}
            look.update(got)
            look["status"] = STATUS_READY
            look["finished"] = _now()
            save_look(cid, look)

            def done(r):
                if (r.get("job") or {}).get("look") == look_id:
                    r["job"] = {}
                r.pop("_restyle", None)
            _update(cid, done)
            if rebase and keep:
                try:
                    _start_fit(cid, keep)
                    _update(cid, lambda r: r.get("job") and r["job"].update(
                        line="Putting their things back on…"))
                except Exception as e:  # noqa: BLE001
                    print(f"[CHARACTERS] {cid}: could not put {keep} back on: {e}", flush=True)
            print(f"[CHARACTERS] {cid} fitted into {look_id} (worn from the next move at "
                  f"{look.get('ready_secs')}s, finished at {look.get('secs')}s): "
                  f"{look.get('wardrobe_line')!r}", flush=True)
        except Exception as e:  # noqa: BLE001
            reason = str(e)[:200]

            def failed(r):
                r["looks"].pop(look_id, None)
                if r.get("_restyle"):
                    # The new style could not be drawn: they stay as they were.
                    r["style"] = (r.pop("_restyle") or {}).get("was", "")
                for k, v in list((r.get("outfits") or {}).items()):
                    if v == look_id:
                        r["outfits"].pop(k, None)
                if r.get("wanted_look") == look_id:
                    r["wanted_look"] = r.get("current_look") or ""
                    # What they asked to wear could not be drawn: the pack goes
                    # back to what the picture shows.
                    cur = r["looks"].get(r.get("current_look") or "", {})
                    shown = set(cur.get("worn") or [])
                    for it in r.get("inventory") or []:
                        it["worn"] = it.get("id") in shown
                r["error"] = reason
                if (r.get("job") or {}).get("look") == look_id:
                    r["job"] = {}
            _update(cid, failed)
            print(f"[CHARACTERS] {cid} fitting failed: {reason}", flush=True)

    if wait:
        work()
    else:
        _spawn(cid + "-fit", work)
    return load(cid) or {}


def adopt_renders(rec: dict, turnaround_key: Path, idle_key: Optional[Path] = None,
                  *, read_back: bool = True) -> dict:
    """A character whose turnaround (and idle) were drawn elsewhere — the
    shipped starter, built from the 2026-09-23 spike's renders by
    tools/build_starter_character.py — made a character: cut out, read back,
    a face sheet drawn if a key is present. Returns the saved record."""
    from PIL import Image
    look_id = _new_look_id("base")
    d = look_dir(rec["id"], look_id)
    d.mkdir(parents=True, exist_ok=True)
    raw = Path(turnaround_key).read_bytes()
    cut = _cut(raw)
    problem = _mechanical_check(cut)
    if problem:
        raise RuntimeError(problem)
    cut = _tight(cut)
    (d / "turnaround_key.png").write_bytes(raw)
    cut.save(d / "turnaround.png")
    _save_ref(_flat(cut), d / "turnaround_ref.jpg")
    if idle_key and Path(idle_key).is_file():
        shutil.copyfile(idle_key, d / "idle_key.png")
        c = _cut(Path(idle_key).read_bytes())
        (_tight(c) if c is not None else split_views(cut)[0]).save(d / "idle.png")
    else:
        split_views(cut)[0].save(d / "idle.png")
    _thumb_from(d / "idle.png", d / "thumb.png")
    inspect: dict = {}
    if read_back and have_key():
        inspect = _text_json([_img_part(d / "turnaround_ref.jpg", 1600), {"text": _INSPECT}],
                             "character_readback", temperature=0)
        try:
            r = _image([{"text": "CHARACTER TURNAROUND — the person."},
                        _img_part(d / "turnaround_key.png", 2048), {"text": _FACE}], "16:9", "character_face")
            _save_ref(Image.open(io.BytesIO(r)), d / "face_ref.jpg", 1400)
        except Exception as e:  # noqa: BLE001
            print(f"[CHARACTERS] face sheet failed: {e}", flush=True)
    look = {"id": look_id, "kind": "base", "parent": "", "worn": [], "status": STATUS_READY,
            "body": str(inspect.get("body") or rec.get("who") or "")[:300],
            "garments": {k: str(v).strip() for k, v in (inspect.get("garments") or {}).items()
                         if k in SLOTS and str(v or "").strip()},
            "wardrobe_line": str(inspect.get("wardrobe_line") or "").strip(),
            "palette": str(inspect.get("palette") or "").strip(),
            "check": inspect.get("check") or {}, "created": _now(), "finished": _now(),
            "model": IMAGE_MODEL, "secs": 0}
    save_look(rec["id"], look)
    rec["looks"] = {look_id: {"kind": "base", "status": STATUS_READY, "worn": [], "created": _now()}}
    rec["base_look"] = rec["current_look"] = rec["wanted_look"] = look_id
    rec["outfits"] = {_outfit_key(look_id, []): look_id}
    rec["status"] = STATUS_READY
    rec["job"] = {}
    save(rec)
    return rec


# ═══════════════════════════════════════════════════════════════════════════
# THE PACK — what they carry, and what they wear
# ═══════════════════════════════════════════════════════════════════════════

def slot_for(item: dict) -> str:
    """Where a thing goes on the body, or '' if it is only carried."""
    kind = str(item.get("kind") or "").lower().strip()
    name = str(item.get("name") or "").lower()
    look = str(item.get("look") or "").lower()
    for text in (name, look):
        for slot, pat in _SLOT_RULES:
            if re.search(pat, text):
                if kind == "weapon" and slot not in ("hands",):
                    break
                return slot
        if kind == "weapon":
            return "held"
    if kind in ("armor", "armour"):
        return "outer"
    if kind == "weapon":
        return "held"
    if kind == "tool":
        return "held"
    return ""


def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def add_item(cid: str, item: dict) -> Optional[dict]:
    """Into the character's own pack: the world's plate copied in (the look
    book belongs to a session and a World; this outlives both). The same
    thing twice is one thing. Returns the stored item."""
    if not item or not item.get("name"):
        return None
    rec = load(cid)
    if rec is None:
        return None
    for it in rec.get("inventory") or []:
        if _norm(it.get("name")) == _norm(item.get("name")):
            return it
    iid = f"{_slug(item.get('name'))[:24]}-{uuid.uuid4().hex[:4]}"
    stored = {k: item.get(k, "") for k in ("name", "kind", "tier", "power", "worth", "use",
                                            "look", "from", "source", "how", "turn")}
    stored.update({"id": iid, "slot": slot_for(item), "worn": False, "acquired": _now(),
                   "world": str(item.get("world") or ""), "plate": "", "ref": ""})
    plate = str(item.get("plate") or "")
    idir = char_dir(cid) / "items"
    try:
        if plate and Path(plate).is_file():
            idir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(plate, idir / f"{iid}.png")
            stored["plate"] = f"{iid}.png"
            ref = Path(plate).with_name(Path(plate).stem + "_ref.jpg")
            if ref.is_file():
                shutil.copyfile(ref, idir / f"{iid}_ref.jpg")
            else:
                from PIL import Image
                im = Image.open(plate).convert("RGBA")
                _save_ref(_flat(im), idir / f"{iid}_ref.jpg", 768)
            stored["ref"] = f"{iid}_ref.jpg"
    except Exception as e:  # noqa: BLE001
        print(f"[CHARACTERS] could not keep the plate for {item.get('name')!r}: {e}", flush=True)

    def put(r):
        r.setdefault("inventory", []).append(stored)
    _update(cid, put)
    print(f"[CHARACTERS] {cid} now carries {stored['name']!r} "
          f"({stored['kind'] or 'item'}{', wearable on the ' + stored['slot'] if stored['slot'] else ''})", flush=True)
    return stored


def set_worn(cid: str, item_id: str, on: bool, *, wait: bool = False) -> dict:
    """WEAR / TAKE OFF. The pack says so at once (and the gear does its fight
    work at once); the picture follows when the fitting lands, and the run
    takes the new look at its next turn."""
    rec = load(cid)
    if rec is None:
        raise ValueError("no such character")
    items = {it.get("id"): it for it in rec.get("inventory") or []}
    it = items.get(item_id)
    if not it:
        raise ValueError("not in the pack")
    if on and not it.get("slot"):
        raise ValueError(f"{it.get('name')} is carried, not worn")

    def flip(r):
        for x in r.get("inventory") or []:
            if x.get("id") == item_id:
                x["worn"] = bool(on)
            elif on and x.get("worn") and x.get("slot") == it.get("slot"):
                x["worn"] = False       # one thing per slot: the new one replaces it
        return [x["id"] for x in r.get("inventory") or [] if x.get("worn")]
    worn = _update(cid, flip) or []
    if not rec.get("base_look"):
        return load(cid) or {}
    return _start_fit(cid, worn, wait=wait)


def worn_items(rec: Optional[dict]) -> List[dict]:
    return [it for it in (rec or {}).get("inventory") or [] if it.get("worn")]


def run_gear(rec: dict) -> List[dict]:
    """The pack as a run keeps it (state["gear"]): goal.held reads this, the
    fight's edge reads what is worn. Plates as paths the image layer can open."""
    out = []
    for it in rec.get("inventory") or []:
        g = dict(it)
        g["plate"] = str(char_dir(rec["id"]) / "items" / it["plate"]) if it.get("plate") else ""
        g["character_item"] = it.get("id")
        out.append(g)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# THE RUN — which character it is, and what the simulation is shown
# ═══════════════════════════════════════════════════════════════════════════

def bind_run(state: dict, cid: str) -> Optional[dict]:
    """A new run is this character: the pointer, their pack, and one more run
    on their record. Called by the reset; nothing else ever names who you are."""
    rec = load(cid)
    if not rec or rec.get("status") != STATUS_READY or not rec.get("current_look"):
        return None
    state["character_id"] = cid
    state["look_id"] = rec["current_look"]
    state["gear"] = run_gear(rec)
    state.pop("look_note", None)
    state.pop("_character_death_noted", None)

    def count(r):
        rr = r.setdefault("record", {})
        rr["runs"] = int(rr.get("runs") or 0) + 1
        r["last_played"] = _now()
    _update(cid, count)
    print(f"[CHARACTERS] this run is {rec.get('name')!r} ({cid}, look {rec['current_look']}, "
          f"carrying {len(state['gear'])})", flush=True)
    return rec


def note_world(state: dict, world: str) -> None:
    cid = (state or {}).get("character_id")
    if not cid or not world:
        return

    def put(r):
        ws = r.setdefault("record", {}).setdefault("worlds", [])
        if world not in ws:
            ws.append(world)
    _update(cid, put)


def note_death(state: dict) -> None:
    cid = (state or {}).get("character_id")
    if not cid or state.get("_character_death_noted"):
        return
    state["_character_death_noted"] = True

    def put(r):
        rr = r.setdefault("record", {})
        rr["deaths"] = int(rr.get("deaths") or 0) + 1
    _update(cid, put)


def sync_run_look(state: dict, session_id: Optional[str] = None) -> str:
    """At a turn boundary: if the character's look moved (a fitting landed),
    the run takes it now — never mid-render — and the pack mirror follows the
    character. Returns the one line the narrator gets ('' when nothing
    changed)."""
    cid = (state or {}).get("character_id")
    if not cid:
        return ""
    rec = load(cid)
    if not rec:
        return ""
    # the pack: the character is the truth, the run keeps a view of it
    state["gear"] = _merge_run_gear(state.get("gear") or [], rec)
    cur = rec.get("current_look") or ""
    if not cur or cur == state.get("look_id"):
        return ""
    old = state.get("look_id") or ""
    state["look_id"] = cur
    if session_id:
        set_binding(session_id, cid, cur)
    look = load_look(cid, cur) or {}
    before = set((load_look(cid, old) or {}).get("worn") or [])
    after = set(look.get("worn") or [])
    names = {it.get("id"): it.get("name") for it in rec.get("inventory") or []}
    refs = {it.get("id"): (str(char_dir(cid) / "items" / it["ref"]) if it.get("ref") else "")
            for it in rec.get("inventory") or []}
    put_on_ids = [i for i in after - before if i in names]
    put_on = [names[i] for i in put_on_ids]
    took_off = [names[i] for i in before - after if i in names]
    looks = {it.get("id"): str(it.get("look") or "").strip().rstrip(".")[:140]
             for it in rec.get("inventory") or []}
    bits = []
    if put_on:
        bits.append("now wearing " + ", ".join(
            f"{names[i]} ({looks[i]})" if looks.get(i) else names[i] for i in put_on_ids))
    if took_off:
        bits.append("no longer wearing " + ", ".join(took_off))
    line = ""
    if bits:
        line = f"{rec.get('name') or 'The player'} is " + "; ".join(bits) + "."
    elif look.get("kind") == "base":
        line = f"{rec.get('name') or 'The player'} now looks like this: {look.get('wardrobe_line') or ''}".strip()
    print(f"[CHARACTERS] the run takes look {cur} — {line or 'same outfit'}", flush=True)
    state["look_note"] = line
    # What the image model is told this turn and the next (engine.
    # _wardrobe_change_directive): the frame it continues is still the old
    # outfit, and continuity copies whatever the last frame shows.
    state["look_changed"] = {"at": int(state.get("turn_count") or 0), "put_on": put_on,
                             "plates": [refs.get(i, "") for i in put_on_ids],
                             "took_off": took_off, "who": rec.get("name") or ""}
    return line


def _merge_run_gear(current: List[dict], rec: dict) -> List[dict]:
    """The character's pack, plus anything the run is holding that has not
    reached the character yet (it will on its next add)."""
    mine = run_gear(rec)
    have = {_norm(g.get("name")) for g in mine}
    return mine + [g for g in current if isinstance(g, dict) and g.get("name")
                   and not g.get("character_item") and _norm(g.get("name")) not in have]


def stow_run_gear(state: dict) -> List[dict]:
    """Everything the run picked up goes onto its character (called after the
    goal's take and a fight's spoils write state["gear"]). Returns the new pack."""
    cid = (state or {}).get("character_id")
    if not cid:
        return list(state.get("gear") or [])
    for g in list(state.get("gear") or []):
        if isinstance(g, dict) and g.get("name") and not g.get("character_item"):
            add_item(cid, g)
    rec = load(cid)
    if rec:
        state["gear"] = run_gear(rec)
    return state["gear"]


# ── what game_identity is told ──────────────────────────────────────────────

def _current_session_id() -> Optional[str]:
    eng = sys.modules.get("engine")
    if eng is None:
        return "default"
    try:
        from flask import has_request_context
        if has_request_context():
            return eng._resolve_request_session_id()
    except Exception:
        pass
    try:
        return eng.get_active_session_id()
    except Exception:
        return None


# session -> (character_id, look_id), set the moment a reset or a turn
# boundary decides it. The state file is the durable copy (a restarted server
# reads it), but a reset renders its intro BEFORE the new state is saved, and
# until then the file still names the previous run's character.
_RUN_BIND: Dict[str, Tuple[str, str]] = {}


def set_binding(session_id: str, cid: str, look_id: str) -> None:
    with _LOCK:
        _RUN_BIND[str(session_id or "default")] = (str(cid or ""), str(look_id or ""))


def binding(session_id: Optional[str] = None) -> Tuple[str, str]:
    """(character_id, look_id) the run in this session is, or ('', '')."""
    sid = session_id or _current_session_id()
    if not sid:
        return "", ""
    with _LOCK:
        if sid in _RUN_BIND:
            return _RUN_BIND[sid]
    eng = sys.modules.get("engine")
    if eng is None:
        return "", ""
    try:
        st = json.loads(Path(eng._get_state_path(sid)).read_text(encoding="utf-8"))
    except Exception:
        return "", ""
    # Remembered from here on: every change to it goes through set_binding
    # (the reset, a turn boundary, a bind), and the state file can be large.
    got = (str(st.get("character_id") or ""), str(st.get("look_id") or ""))
    with _LOCK:
        _RUN_BIND.setdefault(sid, got)
    return got


def bound(session_id: Optional[str] = None) -> Tuple[Optional[dict], Optional[dict]]:
    cid, look_id = binding(session_id)
    if not cid:
        return None, None
    rec = load(cid)
    if not rec:
        return None, None
    return rec, load_look(cid, look_id or rec.get("current_look") or "")


def bound_block(session_id: Optional[str] = None) -> Optional[dict]:
    """The cast sheet's character block, as the bound character. None when the
    run is not a character (an older run, a harness session), and the sheet
    stays what it was."""
    rec, look = bound(session_id)
    if not rec:
        return None
    look = look or {}
    lid = look.get("id") or ""
    d = look_dir(rec["id"], lid) if lid else None
    ref = d / "turnaround_ref.jpg" if d else None
    face = d / "face_ref.jpg" if d else None
    garments = look.get("garments") or {}
    # A look is playable before its words are read back (see _draw_look's
    # on_ready). Until they land: a fitting is the look it was fitted onto
    # plus what was put on; a new body is the brief.
    wardrobe = look.get("wardrobe_line") or ""
    if not wardrobe and not garments and look.get("parent"):
        parent = load_look(rec["id"], look["parent"]) or {}
        garments = dict(parent.get("garments") or {})
        wardrobe = parent.get("wardrobe_line") or ""
        on = [it for it in rec.get("inventory") or [] if it.get("id") in set(look.get("worn") or [])]
        if on:
            wardrobe = (wardrobe + "; " if wardrobe else "") + "now wearing " + ", ".join(
                str(it.get("name")) for it in on)
            for it in on:
                if it.get("slot") and it["slot"] != "held":
                    garments[it["slot"]] = it.get("look") or it.get("name")
        elif look.get("changes"):
            # a CHANGE SOMETHING: the parent's garment list is out of date
            garments = {}
            wardrobe = (wardrobe + "; " if wardrobe else "") + "changed: " + "; ".join(
                str(c) for c in look["changes"])
        body = look.get("body") or parent.get("body") or ""
    else:
        body = look.get("body") or ""
    wardrobe = wardrobe or rec.get("who") or ""
    if garments:
        wardrobe = "; ".join(f"{k}: {v}" for k, v in garments.items() if k != "held" and v) or wardrobe
    worn = worn_items(rec)
    held = [it.get("name") for it in worn if it.get("slot") == "held"]
    # Only what the picture shows. The pack's other contents are not part of
    # how they look — said here, the narrator opened a run with "You carry in
    # the pack: Riot Helmet." (2026-09-24 harness run).
    gear_bits = []
    if garments.get("held"):
        gear_bits.append(garments["held"])
    gear_bits += [h for h in held if h and h.lower() not in " ".join(gear_bits).lower()]
    return {
        "enabled": True,
        "name": rec.get("name") or "",
        "pronouns": rec.get("pronouns") or "",
        "role": rec.get("role") or "",
        "appearance": body,
        "wardrobe": wardrobe,
        "signature_gear": "; ".join(g for g in gear_bits if g),
        "demeanor": rec.get("demeanor") or "",
        "backstory": rec.get("concept") or "",
        "reference_images": [],
        "_character": {
            "id": rec["id"], "look": lid,
            "turnaround_ref": str(ref) if ref and ref.is_file() else "",
            "face_ref": str(face) if face and face.is_file() else "",
            "wardrobe_line": look.get("wardrobe_line") or "",
        },
    }


def turnaround_path(block: Optional[dict]) -> str:
    return str(((block or {}).get("_character") or {}).get("turnaround_ref") or "")


def face_path(block: Optional[dict]) -> str:
    return str(((block or {}).get("_character") or {}).get("face_ref") or "")


def is_turnaround(path: Any) -> bool:
    p = str(path or "").replace("\\", "/")
    return p.endswith("/turnaround_ref.jpg") and "/looks/" in p


def is_face(path: Any) -> bool:
    p = str(path or "").replace("\\", "/")
    return p.endswith("/face_ref.jpg") and "/looks/" in p


def export_looks(state: dict, dest: Path) -> List[str]:
    """The run's character references for a shot pack (run_tape.export_pack):
    the looks this run wore, so a video pass has the same person the game used."""
    cid = (state or {}).get("character_id")
    rec = load(cid) if cid else None
    if not rec:
        return []
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for lid, meta in (rec.get("looks") or {}).items():
        if meta.get("status") != STATUS_READY:
            continue
        src = look_dir(cid, lid) / "turnaround_ref.jpg"
        if src.is_file():
            name = f"character_{lid}.jpg"
            shutil.copyfile(src, dest / name)
            out.append(name)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# HTTP (mounted in api.py)
# ═══════════════════════════════════════════════════════════════════════════

def _ok(**kw):
    from flask import jsonify
    return jsonify({"ok": True, **kw})


def _bad(msg: str, code: int = 400):
    from flask import jsonify
    return jsonify({"ok": False, "error": str(msg)}), code


def _body() -> dict:
    from flask import request
    return request.get_json(silent=True) or {}


def api_list():
    """GET /api/characters -> the roster, the one played last first."""
    rows = list_all()
    last = next((r["id"] for r in rows if r.get("status") == STATUS_READY), "")
    return _ok(characters=[card(r) for r in rows], last=last, default_style=DEFAULT_STYLE,
               offline=not have_key() and not os.getenv("SOMEWHERE_CHARACTERS_REHEARSE"))


def api_get(cid):
    rec = load(cid) if valid_id(cid) else None
    if not rec:
        return _bad("no such character", 404)
    return _ok(character=card(rec, full=True))


def api_create():
    """POST {concept, name?, pictures?: [data urls]} -> the new character,
    drawing in the background. Poll GET /api/characters/<id>."""
    b = _body()
    try:
        rec = create(str(b.get("concept") or ""), name=str(b.get("name") or ""),
                     pictures=[str(p) for p in (b.get("pictures") or [])][:3],
                     style=str(b.get("style") or ""))
    except ValueError as e:
        return _bad(str(e))
    return _ok(character=card(rec, full=True))


def api_surprise():
    try:
        return _ok(concept=surprise())
    except Exception as e:  # noqa: BLE001
        return _bad(f"could not think of anyone ({e})", 502)


def api_draw(cid):
    """POST {concept?} -> draw (a character imported from the cast sheet) or
    TRY AGAIN (a new base look from the brief, or from a changed line)."""
    rec = load(cid) if valid_id(cid) else None
    if not rec:
        return _bad("no such character", 404)
    b = _body()
    concept = b.get("concept")
    rec = start_draw(cid, concept=str(concept) if concept else None)
    return _ok(character=card(rec, full=True))


def api_revise(cid):
    """POST {change} -> CHANGE SOMETHING: one line redrawn onto them."""
    rec = load(cid) if valid_id(cid) else None
    if not rec:
        return _bad("no such character", 404)
    try:
        rec = revise(cid, str(_body().get("change") or ""))
    except ValueError as e:
        return _bad(str(e))
    return _ok(character=card(rec, full=True))


def api_style(cid):
    """POST {style} -> STYLE: the same person redrawn in another art style
    ("" puts them back to the game's own)."""
    rec = load(cid) if valid_id(cid) else None
    if not rec:
        return _bad("no such character", 404)
    if busy(cid):
        return _bad("they are still being drawn", 409)
    try:
        rec = restyle(cid, str(_body().get("style") or ""))
    except ValueError as e:
        return _bad(str(e))
    return _ok(character=card(rec, full=True))


def api_delete(cid):
    if not valid_id(cid) or not delete(cid):
        return _bad("no such character", 404)
    return _ok()


def api_file(cid, rel):
    from flask import send_file
    p = file_path(cid, rel)
    if not p:
        return _bad("not found", 404)
    resp = send_file(str(p), max_age=3600)
    return resp


def _pack_payload(st: dict, sid: str) -> dict:
    try:
        import goal
        return {"pack": goal.pack_cards(st, sid), "world_gear": goal.world_gear(st, sid)}
    except Exception:
        return {"pack": [], "world_gear": {}}


def api_bound():
    """GET /api/character -> the character this run is (the pack's figure),
    with the pack; {character: null} for a run that is not one."""
    import engine
    sid = engine._resolve_request_session_id()
    st = engine._load_state(sid) or {}
    rec = load(st.get("character_id") or "") if st.get("character_id") else None
    if not rec:
        return _ok(character=None, **_pack_payload(st, sid))
    return _ok(character=card(rec, full=True), run_look=st.get("look_id") or "",
               **_pack_payload(st, sid))


def api_wear():
    """POST {item, on} -> WEAR IT / TAKE IT OFF on this run's character. The
    pack flips at once (and so do the fight's edges); the fitting draws in the
    background and the run takes it at its next turn."""
    import engine
    sid = engine._resolve_request_session_id()
    b = _body()
    with engine.WORLD_STATE_LOCK:
        st = engine._load_state(sid) or {}
        cid = st.get("character_id") or ""
        if not cid:
            return _bad("this run is not a character")
        try:
            rec = set_worn(cid, str(b.get("item") or ""), bool(b.get("on", True)))
        except ValueError as e:
            return _bad(str(e))
        rec = load(cid) or rec
        st["gear"] = _merge_run_gear(st.get("gear") or [], rec)
        engine._save_state(st, sid)
        engine._sync_ambient_state(st, sid)
    return _ok(character=card(rec, full=True), run_look=st.get("look_id") or "",
               **_pack_payload(st, sid))


def api_bind():
    """POST {character_id} -> this run becomes that character from its next
    frame (the editors' "Change character"). Their pack comes with them."""
    import engine
    sid = engine._resolve_request_session_id()
    cid = str(_body().get("character_id") or "")
    rec = load(cid) if valid_id(cid) else None
    if not rec or rec.get("status") != STATUS_READY:
        return _bad("that character is not ready")
    with engine.WORLD_STATE_LOCK:
        st = engine._load_state(sid) or {}
        st["character_id"] = cid
        st["look_id"] = rec.get("current_look") or ""
        st["gear"] = run_gear(rec)
        st["look_note"] = f"The player is now {rec.get('name')}."
        set_binding(sid, cid, st["look_id"])
        engine._save_state(st, sid)
        engine._sync_ambient_state(st, sid)

    def mark(r):
        r["last_played"] = _now()
    _update(cid, mark)
    return _ok(character=card(load(cid) or rec, full=True))
