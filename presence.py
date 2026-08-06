"""
Lightweight in-process presence tracker: who else is on this run right now.

Scoped PER SESSION. The original version of this assumed everyone who loaded
the game was watching one shared ``engine.state``; main has since moved to
per-session runs (see LOBBY_MULTIUSER.md), where each visitor gets their own
persisted instance. A global headcount under that model is actively wrong — it
would tell a player alone in a private run that four people are watching,
because four unrelated people happened to be on the server. So the unit of
presence is the session, and two tabs only see each other when they are pointed
at the same ``?session=`` id.

It runs entirely in memory. Production is pinned to exactly one gunicorn worker
*process* (see start_production.sh: ``--workers 1 --worker-class gthread``), so
a plain dict guarded by a lock is shared correctly across all request-handling
threads — the same assumption ``engine.state`` already makes. This deliberately
does NOT persist across restarts: it is a live "who's here right now" readout,
not a historical record.

Client contract (see the ``Lobby`` module in static/js/standalone.js):
  * each browser tab generates a random ``viewer_id`` once (sessionStorage) and
    POSTs it to /api/lobby/heartbeat every ~8s to say "still here".
  * the same heartbeat is sent with ``active: true`` right after the player
    commits an action, so the server can tell watching apart from steering.
  * on tab close/hide a best-effort ``navigator.sendBeacon`` hits
    /api/lobby/leave, so a closed tab drops out immediately instead of
    lingering until its heartbeat times out.
"""

import re
import threading
import time

# A viewer who hasn't heartbeat-ed in this long is presumed gone. Covers a
# couple of missed beats (backgrounded tab, brief network hiccup) without
# letting a stale entry linger long after someone actually left.
PRESENCE_TTL_SECONDS = 30

# A viewer counts as "interacting" — actually steering the run rather than just
# watching it — if they committed an action within this many seconds.
ACTIVE_WINDOW_SECONDS = 20

# Sessions with no viewers left are dropped wholesale, so an install that has
# served thousands of one-off runs doesn't keep a row for each forever.
_MAX_SESSIONS = 500

_LABEL_MAX_LEN = 24
_SAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9 _\-#!?.]")

_lock = threading.Lock()
# session_id -> {"viewers": {viewer_id: entry}, "next_seq": int}
_sessions = {}


def _clean_id(value, limit=64):
    if not value or not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _default_label(seq):
    return f"WITNESS #{seq:02d}"


def _sanitize_label(label):
    """An optional client nickname, stripped to a short display-safe token.
    Anything empty or garbage falls back to the auto label."""
    if not label or not isinstance(label, str):
        return None
    cleaned = _SAFE_LABEL_RE.sub("", label).strip()
    return cleaned[:_LABEL_MAX_LEN] or None


def _room_locked(session_id, create=False):
    room = _sessions.get(session_id)
    if room is None and create:
        # Bound the table before adding to it. Rooms are pruned empty below, so
        # hitting this means a genuine flood; dropping the least recently
        # touched is the cheapest correct answer.
        if len(_sessions) >= _MAX_SESSIONS:
            oldest = min(
                _sessions.items(),
                key=lambda kv: max((v["last_seen"] for v in kv[1]["viewers"].values()), default=0),
            )[0]
            _sessions.pop(oldest, None)
        room = {"viewers": {}, "next_seq": 1}
        _sessions[session_id] = room
    return room


def _prune_locked(session_id, now):
    room = _sessions.get(session_id)
    if not room:
        return None
    dead = [vid for vid, v in room["viewers"].items()
            if now - v["last_seen"] > PRESENCE_TTL_SECONDS]
    for vid in dead:
        del room["viewers"][vid]
    if not room["viewers"]:
        _sessions.pop(session_id, None)
        return None
    return room


def touch(session_id, viewer_id, label=None, active=False):
    """Register a heartbeat for ``viewer_id`` on ``session_id``.

    Returns the same shape :func:`snapshot` does, with ``you`` populated, so
    the client never needs a second round trip to render the widget.
    """
    session_id = _clean_id(session_id) or "default"
    viewer_id = _clean_id(viewer_id)
    if not viewer_id:
        return snapshot(session_id)

    now = time.time()
    clean_label = _sanitize_label(label)
    with _lock:
        _prune_locked(session_id, now)
        room = _room_locked(session_id, create=True)
        entry = room["viewers"].get(viewer_id)
        if entry is None:
            entry = {
                "seq": room["next_seq"],
                "label": clean_label or _default_label(room["next_seq"]),
                "first_seen": now,
                "last_seen": now,
                "last_action": now if active else 0,
            }
            room["next_seq"] += 1
            room["viewers"][viewer_id] = entry
        else:
            entry["last_seen"] = now
            if clean_label:
                entry["label"] = clean_label
            if active:
                entry["last_action"] = now
        return _snapshot_locked(session_id, now, viewer_id)


def leave(session_id, viewer_id):
    """Explicit departure (tab close/hide beacon) — drop the viewer now rather
    than waiting out the TTL."""
    session_id = _clean_id(session_id) or "default"
    viewer_id = _clean_id(viewer_id)
    if not viewer_id:
        return
    with _lock:
        room = _sessions.get(session_id)
        if not room:
            return
        room["viewers"].pop(viewer_id, None)
        if not room["viewers"]:
            _sessions.pop(session_id, None)


def snapshot(session_id, viewer_id=None):
    """Read-only view of one run's presence. Passing ``viewer_id`` marks that
    entry as ``you`` but does NOT refresh it — use :func:`touch` for that."""
    session_id = _clean_id(session_id) or "default"
    now = time.time()
    with _lock:
        _prune_locked(session_id, now)
        return _snapshot_locked(session_id, now, viewer_id)


def _snapshot_locked(session_id, now, viewer_id=None):
    viewer_id = _clean_id(viewer_id) or None
    room = _sessions.get(session_id)
    viewers = []
    active_count = 0
    you = None
    for vid, v in sorted((room or {"viewers": {}})["viewers"].items(), key=lambda kv: kv[1]["seq"]):
        is_active = bool(v["last_action"]) and (now - v["last_action"]) <= ACTIVE_WINDOW_SECONDS
        if is_active:
            active_count += 1
        entry = {
            "seq": v["seq"],
            "label": v["label"],
            "active": is_active,
            "seen_secs_ago": round(now - v["last_seen"], 1),
            "you": vid == viewer_id,
        }
        if entry["you"]:
            you = entry
        viewers.append(entry)
    return {
        "session_id": session_id,
        "count": len(viewers),
        "active_count": active_count,
        "viewers": viewers,
        "you": you,
    }


def reset():
    """Drop all presence. Tests only."""
    with _lock:
        _sessions.clear()
