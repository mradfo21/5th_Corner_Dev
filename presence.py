"""
Lightweight in-process "lobby" presence tracker.

The standalone/live UI (see templates/standalone.html) is a single shared
run — everyone who loads /standalone or /realtime is watching (and can
steer) the SAME `engine.state`, not a private session of their own. Until
now there was no way to tell whether that was one person or fifty, so this
module gives the client a live headcount: "N watching, M interacting".

It runs entirely in memory. Production is pinned to exactly one gunicorn
worker *process* (see start_production.sh: `--workers 1 --worker-class
gthread`), so a plain dict guarded by a lock is shared correctly across all
request-handling threads — the same assumption `engine.state` already makes.
This intentionally does NOT persist across restarts/deploys; the lobby is a
live "who's here right now" readout, not a historical record.

Client contract (see the `Lobby` module in static/js/standalone.js):
    - each browser tab generates a random `viewer_id` once (sessionStorage)
      and POSTs it to /api/lobby/heartbeat every ~8s to say "still here".
    - the same heartbeat is sent with `active: true` immediately after the
      player commits a game action (a choice / free-will action / etc.), so
      the server can tell "watching" apart from "currently driving the run".
    - on tab close/hide, a best-effort `navigator.sendBeacon` POSTs
      /api/lobby/leave so a closed tab drops out immediately instead of
      lingering until its heartbeat times out.
"""

import re
import threading
import time

# A viewer who hasn't heartbeat-ed in this long is presumed gone — covers a
# couple of missed beats (backgrounded tab, brief network hiccup) without
# letting a stale entry linger for long after someone actually leaves.
PRESENCE_TTL_SECONDS = 30

# A viewer counts as "interacting" (actually steering the run right now,
# rather than just watching) if they committed a game action within this
# many seconds.
ACTIVE_WINDOW_SECONDS = 20

_LABEL_MAX_LEN = 24
_SAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9 _\-#!?.]")

_lock = threading.Lock()
_viewers = {}  # viewer_id -> {seq, label, first_seen, last_seen, last_action}
_next_seq = 1


def _default_label(seq):
    return f"WITNESS #{seq:02d}"


def _sanitize_label(label):
    """Accept an optional client-supplied nickname, stripped down to a short,
    display-safe token. Anything empty/garbage falls back to the auto label."""
    if not label or not isinstance(label, str):
        return None
    cleaned = _SAFE_LABEL_RE.sub("", label).strip()
    return cleaned[:_LABEL_MAX_LEN] or None


def _prune_locked(now):
    dead = [vid for vid, v in _viewers.items() if now - v["last_seen"] > PRESENCE_TTL_SECONDS]
    for vid in dead:
        del _viewers[vid]


def touch(viewer_id, label=None, active=False):
    """Register a heartbeat for `viewer_id` (creating it on first contact).

    Returns the same snapshot `snapshot()` would, with `you` populated for
    this viewer — so the client never needs a second round trip just to
    render the widget after a heartbeat.
    """
    global _next_seq
    if not viewer_id or not isinstance(viewer_id, str):
        return snapshot()
    viewer_id = viewer_id.strip()[:64]
    if not viewer_id:
        return snapshot()
    now = time.time()
    clean_label = _sanitize_label(label)
    with _lock:
        _prune_locked(now)
        entry = _viewers.get(viewer_id)
        if entry is None:
            entry = {
                "seq": _next_seq,
                "label": clean_label or _default_label(_next_seq),
                "first_seen": now,
                "last_seen": now,
                "last_action": now if active else 0,
            }
            _next_seq += 1
            _viewers[viewer_id] = entry
        else:
            entry["last_seen"] = now
            if clean_label:
                entry["label"] = clean_label
            if active:
                entry["last_action"] = now
        return _snapshot_locked(now, viewer_id)


def leave(viewer_id):
    """Explicit departure (tab close/hide beacon) — drop the viewer now
    rather than waiting for the TTL to expire."""
    if not viewer_id or not isinstance(viewer_id, str):
        return
    with _lock:
        _viewers.pop(viewer_id.strip()[:64], None)


def snapshot(viewer_id=None):
    """Read-only lobby snapshot. Passing `viewer_id` marks that entry (if
    still present) as `you` in the returned viewer list, but does NOT touch
    it — use `touch()` for that."""
    now = time.time()
    with _lock:
        _prune_locked(now)
        return _snapshot_locked(now, viewer_id)


def _snapshot_locked(now, viewer_id=None):
    viewer_id = (viewer_id or "").strip()[:64] or None
    viewers = []
    active_count = 0
    you = None
    for vid, v in sorted(_viewers.items(), key=lambda kv: kv[1]["seq"]):
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
        "count": len(viewers),
        "active_count": active_count,
        "viewers": viewers,
        "you": you,
    }
