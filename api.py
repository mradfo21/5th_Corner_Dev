"""
API wrapper for the SOMEWHERE game engine.
Provides RESTful endpoints for game state management, session control, and asset serving.
"""

import base64
import os
import json
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Optional
from urllib.parse import quote
from flask import Flask, request, jsonify, send_file, make_response, render_template, redirect
from flask_cors import CORS
import engine
import ai_provider_manager
import bug_report
import replay_cache
import render_jobs
import scene_audio
import coinop
import keys_store
import billing

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes


# ═══════════════════════════════════════════════════════════════════
# STALL WATCHDOG
#
# Production runs ONE gunicorn worker with a handful of threads. If a request
# blocks on a lock and never returns, the next few requests pile up behind it,
# every thread is consumed, and the whole service stops answering — including
# /api/health, which touches nothing. From the outside that is indistinguishable
# from the box being down, and the logs say nothing at all, because the failure
# is threads waiting rather than an exception.
#
# So: track in-flight requests and, when one overstays, dump EVERY thread's
# stack to stderr (Render's log pipeline) and keep a copy in memory. That turns
# "the game hangs and we have no idea why" into a stack trace naming the exact
# line, for this hang and any future one.
# ═══════════════════════════════════════════════════════════════════

_inflight = {}
_inflight_lock = threading.Lock()
_stall_report = {"at": None, "reason": None, "text": ""}

try:
    _STALL_AFTER_S = max(5.0, float(os.getenv("STALL_DUMP_S", "25")))
except ValueError:
    _STALL_AFTER_S = 25.0
_STALL_REDUMP_S = 60.0
# Last resort. A wedged worker does NOT get recycled on its own: gunicorn's
# --timeout only fires when the worker stops notifying the arbiter, and a worker
# whose request threads are all blocked keeps notifying happily from its accept
# loop. Observed: the service stayed dead for 15+ minutes and only came back on
# a manual redeploy. Exiting hands the process back to gunicorn, which restarts
# it in about a second — an outage measured in seconds beats one that lasts
# until somebody notices.
#
# The threshold is deliberately well above the slowest legitimate request (camp
# entry composites several portraits; a talk portrait is a full image
# generation) so this only ever fires on a genuine wedge, never on a slow turn.
# Set STALL_EXIT_S=0 to disable.
try:
    _STALL_EXIT_S = float(os.getenv("STALL_EXIT_S", "180"))
except ValueError:
    _STALL_EXIT_S = 180.0


def _format_all_stacks(note: str) -> str:
    """Every live thread's stack. Frames only — no locals, no environment."""
    out = [note, ""]
    frames = sys._current_frames()
    names = {t.ident: t.name for t in threading.enumerate()}
    for tid, frame in frames.items():
        out.append(f"--- thread {names.get(tid, '?')} ({tid}) ---")
        out.extend(line.rstrip() for line in traceback.format_stack(frame))
        out.append("")
    return "\n".join(out)


def _stall_watchdog():
    last_dump = 0.0
    while True:
        try:
            time.sleep(5)
            now = time.time()
            with _inflight_lock:
                stalled = [(p, now - t) for (p, t) in _inflight.values() if now - t > _STALL_AFTER_S]
            if not stalled:
                continue
            worst = max(stalled, key=lambda s: s[1])

            # Dump is throttled (the stacks are long and repetitive); the
            # give-up check is NOT — it has to be evaluated every pass, or a
            # wedge that starts just after a dump waits out the whole throttle
            # window before anyone acts on it.
            if (now - last_dump) >= _STALL_REDUMP_S:
                last_dump = now
                reason = (f"{len(stalled)} request(s) in flight > {_STALL_AFTER_S:.0f}s; "
                          f"worst: {worst[0]} ({worst[1]:.0f}s)")
                text = _format_all_stacks(f"[STALL WATCHDOG] {reason}")
                _stall_report["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
                _stall_report["reason"] = reason
                _stall_report["text"] = text
                print(text, file=sys.stderr, flush=True)

            if _STALL_EXIT_S > 0 and worst[1] > _STALL_EXIT_S:
                print(
                    f"[STALL WATCHDOG] {worst[0]} has been stuck for {worst[1]:.0f}s "
                    f"(> {_STALL_EXIT_S:.0f}s). The worker cannot recover on its own; "
                    f"exiting so gunicorn restarts it.",
                    file=sys.stderr, flush=True,
                )
                sys.stderr.flush()
                os._exit(1)
        except Exception:  # noqa: BLE001 — the watchdog must never take the app down
            pass


_watchdog_thread = None


def _ensure_watchdog():
    """Start the watchdog in whatever process is actually serving requests.

    Starting it at import time alone proved unreliable — in production the
    worker's thread list came back with no watchdog in it, so the one hang we
    most needed a report for went unrecorded. Arming it from the request path
    guarantees it exists wherever requests are being handled.
    """
    global _watchdog_thread
    if _watchdog_thread is not None and _watchdog_thread.is_alive():
        return
    try:
        _watchdog_thread = threading.Thread(
            target=_stall_watchdog, name="stall-watchdog", daemon=True)
        _watchdog_thread.start()
    except Exception:  # noqa: BLE001
        pass


@app.before_request
def _stall_track_start():
    try:
        _ensure_watchdog()
        with _inflight_lock:
            _inflight[id(request._get_current_object())] = (request.path, time.time())
    except Exception:  # noqa: BLE001
        pass


@app.teardown_request
def _stall_track_end(_exc=None):
    try:
        with _inflight_lock:
            _inflight.pop(id(request._get_current_object()), None)
    except Exception:  # noqa: BLE001
        pass


@app.route('/api/diag/stacks', methods=['GET'])
def api_diag_stacks():
    """Thread stacks — the last recorded stall, plus a live snapshot.

    Read-only and frames-only (no locals, no environment, no secrets). This is
    how a hang gets diagnosed on a box you can't attach a debugger to. Set
    DIAG_STACKS=0 to turn it off.
    """
    if os.getenv("DIAG_STACKS", "1").strip().lower() in ("0", "false", "no", "off"):
        return jsonify({"error": "diagnostics disabled"}), 404
    now = time.time()
    with _inflight_lock:
        current = sorted(
            ({"path": p, "seconds": round(now - t, 1)} for (p, t) in _inflight.values()),
            key=lambda d: -d["seconds"],
        )
    return jsonify({
        "inflight": current,
        "stall_threshold_s": _STALL_AFTER_S,
        "last_stall": {k: _stall_report[k] for k in ("at", "reason")},
        "last_stall_stacks": _stall_report["text"],
        "live_stacks": _format_all_stacks("[LIVE SNAPSHOT]"),
    })


_ensure_watchdog()

# Start keeping the tail of everything the server prints, so a bug capture can
# carry the log without knowing where stderr was pointed. Must happen at import,
# before the first turn prints anything worth having.
bug_report.install_log_tap()

# Remember what every model call returned, so a verification run can be replayed
# for free and -- the point -- deterministically. Recording does not change what
# play does; replay is opt-in via SOMEWHERE_REPLAY=replay. Installed here because
# the seams have to be wrapped before the first turn calls them.
replay_cache.install()


@app.route('/api/replay/stats', methods=['GET'])
def api_replay_stats():
    """Hit rate for this process, plus what has been recorded.

    A harness reads `deterministic` to know whether the run it just did proves
    anything: a single miss means part of that run was freshly generated and
    would come back differently next time.
    """
    try:
        return jsonify({"ok": True, "stats": replay_cache.stats(),
                        "library": replay_cache.library()})
    except Exception as e:
        return error_response("Failed to read replay stats", str(e))


@app.route('/api/replay/reset', methods=['POST'])
def api_replay_reset():
    """Zero the counters so one run's hit rate is not confused with another's."""
    try:
        replay_cache.reset_stats()
        return jsonify({"ok": True, "stats": replay_cache.stats()})
    except Exception as e:
        return error_response("Failed to reset replay stats", str(e))


@app.route('/api/bug/capture', methods=['POST'])
def api_bug_capture():
    """Freeze the evidence for "this looks wrong" into one folder.

    The player presses a button; this writes the frame that was on screen, the
    layer stack drawing it, the state that produced it and the tail of the log
    into bugs/<timestamp>/. The situation cannot be reached again by playing --
    the world is generated fresh every run -- so capturing at the moment of the
    complaint is the only way a report stays actionable.

    Deliberately forgiving about its input: a capture is worth having even when
    the client could only tell us half of what it sees.
    """
    try:
        data = request.get_json(silent=True) or {}
        result = bug_report.capture(
            session_id=data.get('session_id') or 'default',
            screen=data.get('screen') if isinstance(data.get('screen'), dict) else {},
            note=str(data.get('note') or ''),
            frame_data_url=str(data.get('frame_data_url') or ''),
        )
        return jsonify(result), (200 if result.get('ok') else 500)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to capture the bug", str(e))


@app.route('/api/bug/list', methods=['GET'])
def api_bug_list():
    """Captures newest first, so a session's reports can be found without ls."""
    try:
        return jsonify({"ok": True, "bugs": bug_report.recent()})
    except Exception as e:
        return error_response("Failed to list bug captures", str(e))


# Optional flask-sock (live TALK websocket). Never let its absence break boot.
try:
    from flask_sock import Sock
    _sock = Sock(app)
except Exception:  # noqa: BLE001
    _sock = None


_BOOTED_AT = time.time()


def _build_info():
    """The commit this process is running, and how long it has been up.

    Render injects RENDER_GIT_COMMIT / RENDER_GIT_BRANCH into the service
    environment, so the running process can state its own provenance rather than
    leaving us to infer it. `uptime_s` is the giveaway for a deploy that never
    happened: a push restarts the worker, so an uptime older than your push means
    the new commit is not live no matter what the dashboard shows.
    """
    commit = (os.getenv("RENDER_GIT_COMMIT") or "").strip()
    return {
        "commit": commit or None,
        "commit_short": commit[:7] if commit else None,
        "branch": (os.getenv("RENDER_GIT_BRANCH") or "").strip() or None,
        "uptime_s": round(time.time() - _BOOTED_AT, 1),
    }


def _talk_status():
    """Can NPC conversation actually work right now, and if not, what's missing?

    Booleans only — never the key or the agent id. TALK degrades silently today:
    a missing ELEVENLABS_AGENT_ID drops you to text with no explanation, and a
    key that is actually an agent id looks identical from the outside. The
    editor's NPC panel reads this so the answer is on screen instead of in the
    boot log.
    """
    info = {}
    try:
        info["agent"] = bool(getattr(engine, "ELEVENLABS_AGENT_ID", ""))
        info["api_key"] = bool(getattr(engine, "ELEVENLABS_API_KEY", ""))
        info["overrides"] = bool(getattr(engine, "ELEVENLABS_ALLOW_OVERRIDES", False))
        # A public agent connects with no key; a private one cannot.
        info["voice"] = bool(info["agent"])
        if not info["agent"]:
            info["reason"] = "ELEVENLABS_AGENT_ID is not set — conversation falls back to text."
        elif not info["api_key"]:
            info["reason"] = "No ELEVENLABS_API_KEY: works only if the agent is public."
        else:
            problem = None
            try:
                problem = engine.elevenlabs_key_problem()
            except Exception:
                problem = None
            if problem:
                info["voice"] = False
                info["reason"] = f"ElevenLabs API key {problem}."
            else:
                info["reason"] = "ready"
    except Exception as e:  # noqa: BLE001
        info = {"voice": False, "reason": f"{type(e).__name__}: {e}"}
    try:
        import voice_design as _vd
        info["designed_voices"] = len((_vd.cache_snapshot() or {}).get("entries") or [])
    except Exception:  # noqa: BLE001
        info["designed_voices"] = 0
    return info


def _music_status():
    """Can scene music / world SFX generate right now, and if not, why."""
    info = {"can_generate": False, "reason": "unavailable", "stock_ready": 0,
            "stock_total": 0}
    try:
        info["can_generate"] = scene_audio.is_available()
        info["reason"] = scene_audio.unavailable_reason() or "ready"
        stock = scene_audio.stock_status()
        info["stock_total"] = len(stock)
        info["stock_ready"] = sum(1 for v in stock.values() if v.get("ready"))
    except Exception as e:  # noqa: BLE001
        info["reason"] = f"{type(e).__name__}: {e}"
    return info


def _detect_backend_status():
    """Which detector /api/detect will use, for /api/health."""
    info = {"backend": getattr(engine, "DETECT_BACKEND", "gemini")}
    local = getattr(engine, "local_vision", None)
    try:
        info["local"] = local.status() if local is not None else {"available": False,
                                                                  "error": "module not imported"}
    except Exception as e:  # noqa: BLE001
        info["local"] = {"available": False, "error": f"{type(e).__name__}: {e}"}
    return info


# Saved runtime knobs, before anything reads them — a detector chosen in the
# editor has to survive the restart that follows Save & Restart, and the warm-up
# below needs to know which backend it is warming up for.
try:
    import tunables as _tunables
    _tunables.apply_all()
except Exception as _tun_err:  # noqa: BLE001
    print(f"[API INIT] tunables not applied: {_tun_err}", flush=True)


def _warn_if_the_world_is_hollow():
    """Say so at boot when nothing anchors the place.

    A `setting_reference` with a name and no summary, era, palette or landmarks
    reads to the engine as a place with no properties, so every turn's generation
    invents a new one and a single run walks from a desert basin to a canyon to a
    shipbreaking yard without the player moving. It looks exactly like a
    rendering bug and it is an empty field.

    This has now silently reverted twice — the live prompt file is rewritten by
    the active Experience on every reset, and by the world editor on save, so any
    of several paths can leave it hollow. Nobody can be expected to notice by
    reading images. Twenty minutes of play chasing "the world is incoherent"
    is the cost of not printing this line.
    """
    try:
        import prompts_store
        setting = prompts_store.PROMPTS.get("setting_reference")
        setting = setting if isinstance(setting, dict) else {}
        filled = [f for f in ("summary", "era", "palette", "landmarks", "opening_shot")
                  if str(setting.get(f) or "").strip()]
        if filled:
            return
        name = str(setting.get("name") or "(unnamed)")
        print("=" * 72, flush=True)
        print(f"[WORLD] SETTING IS HOLLOW: '{name}' has a name and nothing else — "
              f"no summary, era, palette, landmarks or opening shot.", flush=True)
        print("[WORLD] Nothing anchors the place, so every turn will invent a new "
              "one and the run will not stay in one location.", flush=True)
        print("[WORLD] Fix:  python tools/restore_somewhere.py --apply"
              "   (inspect with tools/audit_worlds.py)", flush=True)
        print("=" * 72, flush=True)
    except Exception:
        pass


def _warn_if_the_story_clock_burns_out():
    """Say so at boot when the run will be out of ladder by turn two or three.

    `escalate_at` / `critical_at` are threat POINTS. A choice adds 1 and a
    MOVE TO or INTERACT adds 1 + MAX_RISK_THREAT_BOOST = 2, and SCAN taps are
    most turns in a real run — so a mark of 4 lands on turn 2, not turn 4. The
    editor help read "a typical turn adds one" for a long time, and the obvious
    reading of a field called "Critical at" is a turn number anyway.

    Two saved Experiences on this machine held 2/4 and 2/5. Played, that is
    "escalating" on turn 1 and "critical" on turn 2, and then nothing left:
    past Critical every beat is written as a last stand, UNLUCKY fires on about
    half of all turns, and the consequence model starts inventing injuries to
    justify the register. A measured 9-turn run finished with 8 turns at
    critical and a turn-2 line about a gunshot wound "where you were grazed
    earlier" — for a graze that never happened.

    It reads as the game being incoherent and it is two numbers in a field.
    Same reason as the hollow-world warning above: nobody can be expected to
    diagnose this by reading prose.
    """
    try:
        import engine
        esc, crit = engine._threat_marks()
        per_scan_turn = 1 + engine.MAX_RISK_THREAT_BOOST
        crit_turn = -(-crit // per_scan_turn)  # ceil
        if crit_turn >= 4:
            return
        esc_turn = -(-esc // per_scan_turn)
        ideal = -(-engine.STORY_CRITICAL_AT // per_scan_turn)
        print("=" * 72, flush=True)
        print(f"[PACING] STORY CLOCK BURNS OUT: escalate_at={esc}, "
              f"critical_at={crit} are POINTS, not turns.", flush=True)
        print(f"[PACING] A scanning run adds {per_scan_turn}/turn, so this hits "
              f"'escalating' on turn {esc_turn} and 'critical' on turn "
              f"{crit_turn} — and stays there for the rest of the run.",
              flush=True)
        print("[PACING] Past critical every beat is written as a last stand, so "
              "the story has no arc left and reads as chaos.", flush=True)
        print(f"[PACING] Fix: set 'Critical at' to about "
              f"{engine.STORY_CRITICAL_AT} in the editor's Pacing sheet — that "
              f"is turn {ideal} of a scanning run.", flush=True)
        print("=" * 72, flush=True)
    except Exception:
        pass


_warn_if_the_world_is_hollow()
_warn_if_the_story_clock_burns_out()

# Build the on-device detector now, on the main thread, before any request can
# ask for it. Two reasons this is not left to the first /api/detect: it moves a
# ~440 ms one-off (mediapipe import + TFLite graph build) off the critical path
# of a player's first SCAN, and it keeps a heavy import out of a worker thread —
# this codebase has already been bitten by a threaded import-lock hang (see the
# warm-up block at the top of engine.py). Never fatal: if it can't load,
# engine._detect_objects reports it and SCAN degrades per DETECT_BACKEND.
if getattr(engine, "DETECT_BACKEND", "gemini") in ("local", "auto"):
    try:
        _lv = getattr(engine, "local_vision", None)
        if _lv is not None:
            print(f"[API INIT] local detection warmup: "
                  f"{'ready' if _lv.warmup() else 'unavailable'}", flush=True)
    except Exception as _lv_warm_err:  # noqa: BLE001
        print(f"[API INIT] local detection warmup failed: {_lv_warm_err}", flush=True)


# Local RUN/PLAY sets SOMEWHERE_BOOT. Flask's default static max-age is 12h,
# and WebView2 will keep serving yesterday's standalone.js unless we refuse.
if os.environ.get("SOMEWHERE_BOOT"):
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

# Allow embedding the game in an iframe on the main site.
@app.after_request
def add_embed_headers(response):
    response.headers['Content-Security-Policy'] = (
        "frame-ancestors 'self' https://www.5th-corner.com"
    )
    response.headers['X-Frame-Options'] = (
        "ALLOW-FROM https://www.5th-corner.com"
    )
    if os.environ.get("SOMEWHERE_BOOT"):
        path = request.path or ""
        if path.startswith(("/standalone", "/play", "/lobby", "/realtime",
                            "/live", "/static/js/", "/static/css/")):
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
    return response

# ═══════════════════════════════════════════════════════════════════
# STANDALONE IMMERSIVE UI (feed-based game) + offline mock harness
#
# `engine.py` implements the feed-based game loop (api_reset / api_feed /
# api_choose / api_regenerate_choices) as plain Python functions — it no
# longer owns a Flask app of its own. This is the single Flask app for the
# whole service (gunicorn api:app, see start_production.sh), so we mount
# those functions here with add_url_rule. They share the same in-memory
# `engine.state` / on-disk 'default' session files this code path reads and
# writes via _load_state()/_save_state().
# ═══════════════════════════════════════════════════════════════════

# Session-context wrapper. The engine keeps one active game state in memory
# for the feed endpoints, so before each state-MUTATING session request we
# swap engine.state to the requested instance (see engine.set_active_session)
# — this is where the per-session feed-item-id counter is advanced and the
# session's metadata dir is created. session_id is read from ?session_id=...
# on the query string, the JSON body, or the X-Session-Id header — clients
# pass whichever is convenient.
#
# IMPORTANT: the swap mutates module-global mirrors (engine.state / history /
# _active_session_id) shared by EVERY request thread. Doing it on the
# high-frequency read-only poll (/api/feed) raced the background turn
# pipeline: a poll for session B, landing mid-turn for session A, would swap
# the mirror out from under A's in-flight save and cross-contaminate the two
# sessions' feeds. So /api/feed is deliberately NOT wrapped — its handler
# resolves the caller's session id itself and reads that session's feed_log
# straight from disk (see engine.api_feed), touching no shared global.
def _session_scoped(handler):
    """Decorator that resolves the caller's session id, swaps the engine's
    active session for the duration of the request, and hands off to the
    underlying handler. Used ONLY for the state-mutating endpoints (reset /
    choose / regenerate) that rely on set_active_session's per-session id-
    counter bump + metadata creation. Backwards-compatible: no id → 'default'."""
    from functools import wraps
    @wraps(handler)
    def _wrapped(*args, **kwargs):
        sid = (
            request.args.get('session_id')
            or (request.get_json(silent=True) or {}).get('session_id')
            or request.headers.get('X-Session-Id')
            or 'default'
        )
        try:
            with engine.session_context(sid):
                return handler(*args, **kwargs)
        except Exception:
            traceback.print_exc()
            raise
    return _wrapped


app.add_url_rule('/api/reset', 'standalone_api_reset', _session_scoped(engine.api_reset), methods=['POST'])
# /api/feed is intentionally registered WITHOUT _session_scoped: it is polled
# continuously by every connected client and must not swap the shared global
# mirror (see comment above). engine.api_feed resolves its own session id and
# reads from disk.
app.add_url_rule('/api/feed', 'standalone_api_feed', engine.api_feed, methods=['GET'])


def _spend_blocked(min_usd: float = 0.0):
    """Shared 402 when hosted billing is on and the wallet cannot pay."""
    try:
        blocked = billing.gate(min_usd=min_usd)
        if blocked:
            blocked["usage"] = _usage_payload()
            return jsonify(blocked), 402
    except Exception:
        pass
    return None


def _credit_gated_choose():
    """Wrap engine.api_choose with the arcade credit meter.

    Flow (only active when COINOP_CREDIT_GATING=1 — otherwise a straight
    pass-through so nothing changes for deploys that only want the paid
    death-continue flow):

    1. Attempt a single atomic spend_credit(1) BEFORE touching the engine.
       This is the check + debit in one lock-held operation, so two
       concurrent /api/choose calls for the same session can never both
       succeed off a balance of 1 (the second would see 0 and refuse).
    2. If the spend refuses (balance was 0), return HTTP 402 with
       {needs_coin: true, balance: 0} and do NOT process the turn. The
       client pops the "OUT OF COINS" pause overlay.
    3. Otherwise let the engine handle the turn. On any server error,
       REFUND the credit so a Stripe-live player doesn't lose money to
       a transient LLM outage. Refund is best-effort — if it fails,
       we've cost the player one credit; better than double-charging.

    Why spend-then-maybe-refund instead of check-then-maybe-debit: the
    former composes atomically under a single lock hold, the latter
    has a check-to-debit window where a concurrent turn can slip
    through. The refund on error is cheap and safe (it can never grant
    more credits than were spent).
    """
    sid = engine._resolve_request_session_id()
    blocked = _spend_blocked()
    if blocked:
        return blocked
    cost_before = 0.0
    try:
        import cost_tracker
        cost_before = float(cost_tracker.session_cost_usd(sid) or 0.0)
    except Exception:
        cost_before = 0.0
    gated = coinop.is_credit_gating_enabled()
    debited = False
    if gated:
        spend = coinop.spend_credit(sid, amount=1, reason="choose")
        if not spend.get("ok"):
            return jsonify({
                "needs_coin": True,
                "balance": int(spend.get("balance", 0)),
                "reason": spend.get("reason", "insufficient_credits"),
                "message": "Out of coins — insert more to keep playing.",
            }), 402
        debited = True

    try:
        response = engine.api_choose()
    except Exception:
        if debited:
            try:
                coinop.grant_credits(sid, 1, source="refund")
            except Exception:
                traceback.print_exc()
        raise

    # engine.api_choose signals failure by RETURNING `(jsonify(...), 500)` — a
    # plain tuple, not a Response — and it catches almost everything internally
    # rather than raising. Reading `.status_code` off the result therefore saw
    # no attribute and fell back to 200 for exactly the failures this refund
    # exists to cover, so a player was charged a real credit for a turn that
    # errored and never rendered. Unpack the tuple form too.
    try:
        payload = response[0] if isinstance(response, tuple) else response
        status = response[1] if isinstance(response, tuple) and len(response) > 1 else None
        if not isinstance(status, int):
            status = getattr(payload, "status_code", 200)
    except Exception:
        status = 200
    if debited and status >= 500:
        # The engine returned a 5xx (server-side failure) — refund so we
        # don't burn a coin on a turn the player never saw.
        try:
            coinop.grant_credits(sid, 1, source="refund")
        except Exception:
            traceback.print_exc()
    if status < 500:
        try:
            billing.settle_session(sid, cost_before)
        except Exception:
            traceback.print_exc()
    return response


app.add_url_rule('/api/choose', 'standalone_api_choose', _session_scoped(_credit_gated_choose), methods=['POST'])
# Ambient world drift: one text-only simulation step for a session that's idle
# at a decision point, so the live world model keeps receiving updates instead
# of holding the prompt from the last choice. Registered WITHOUT _session_scoped
# for the same reason as /api/feed — it's polled on a timer by every connected
# client and must not swap the shared global mirror; engine.api_world_tick
# resolves its own session id. See engine.world_drift_tick.
def _gated_world_tick():
    blocked = _spend_blocked()
    if blocked:
        return blocked
    sid = engine._resolve_request_session_id()
    cost_before = 0.0
    try:
        import cost_tracker
        cost_before = float(cost_tracker.session_cost_usd(sid) or 0.0)
    except Exception:
        cost_before = 0.0
    response = engine.api_world_tick()
    try:
        billing.settle_session(sid, cost_before)
    except Exception:
        traceback.print_exc()
    return response


def _gated_regenerate_choices():
    blocked = _spend_blocked()
    if blocked:
        return blocked
    sid = engine._resolve_request_session_id()
    cost_before = 0.0
    try:
        import cost_tracker
        cost_before = float(cost_tracker.session_cost_usd(sid) or 0.0)
    except Exception:
        cost_before = 0.0
    response = engine.api_regenerate_choices()
    try:
        billing.settle_session(sid, cost_before)
    except Exception:
        traceback.print_exc()
    return response


app.add_url_rule('/api/world_tick', 'standalone_api_world_tick', _gated_world_tick, methods=['POST'])
app.add_url_rule('/api/regenerate_choices', 'standalone_api_regenerate_choices', _session_scoped(_gated_regenerate_choices), methods=['POST'])

# ─── COIN-OP (buy-a-continue) ────────────────────────────────────────────
# All coin-op routes are dark-shipped: when FEATURE_COINOP is unset or the
# Stripe keys are missing, /api/coinop/config returns {"enabled": false} and
# the client never renders the button. See coinop.py + COINOP_MVP_SETUP.md.
#
# api_revive is guarded so a client CANNOT revive without first calling
# /api/coinop/redeem — which itself server-side-verifies the Stripe Checkout
# Session before this endpoint can run. This route is only hit through the
# thin _coinop_revive helper below.


@app.route('/api/coinop/config', methods=['GET'])
def _coinop_config():
    # Optional ?comp=<code> query so the client can render a "COMP MODE"
    # badge before the player ever clicks. The server never enumerates
    # allowlisted codes — it only ever reflects back the specific code the
    # client asked about.
    comp = request.args.get('comp') or None
    return jsonify(coinop.public_config(comp))


@app.route('/api/coinop/balance', methods=['GET'])
@_session_scoped
def _coinop_balance():
    """Snapshot of the session's credit ledger.

    Polled by the client to render the always-visible credit HUD chip
    (top-right, next to the REC timecode) and to decide whether the
    "OUT OF COINS" pause overlay should be dismissed after a successful
    purchase. Idempotent — the first call to a fresh session's ledger
    grants the one-shot free starter tier (see coinop._ensure_free_starter).
    """
    sid = engine._resolve_request_session_id()
    return jsonify(coinop.get_balance(sid))


@app.route('/api/coinop/checkout', methods=['POST'])
@_session_scoped
def _coinop_checkout():
    if not coinop.is_enabled():
        return jsonify({"error": "coinop_disabled"}), 404
    sid = engine._resolve_request_session_id()
    data = request.get_json(silent=True) or {}
    comp_code = (data.get('comp') or '').strip() or None
    pack_id = (data.get('pack') or '').strip() or None
    return_to = (data.get('return_to') or '').strip() or None
    try:
        out = coinop.create_checkout(
            sid, request, comp_code=comp_code, pack_id=pack_id, return_to=return_to)
        return jsonify(out)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"error": "checkout_failed", "detail": str(e)}), 500


@app.route('/api/coinop/redeem', methods=['POST'])
@_session_scoped
def _coinop_redeem():
    if not coinop.is_enabled():
        return jsonify({"error": "coinop_disabled"}), 404
    sid = engine._resolve_request_session_id()
    data = request.get_json(silent=True) or {}
    checkout_session_id = (data.get('checkout_session_id') or '').strip()
    if not checkout_session_id:
        return jsonify({"ok": False, "reason": "missing_checkout_session_id"}), 400
    result = coinop.verify_and_redeem(sid, checkout_session_id)
    if not result.get('ok'):
        return jsonify(result), 402  # 402 Payment Required
    # Idempotent replay of the return URL (or a duplicate redeem call
    # from a racy retry): the FIRST successful redeem already invoked
    # api_revive (if applicable) and granted credits. Running either
    # again would double-append the narrative beat / double-grant
    # credits, which reads as a stutter on screen and a phantom refund.
    if result.get('already_redeemed'):
        return jsonify({
            "ok": True,
            "already_redeemed": True,
            "comp": result.get('comp', False),
            "revive_items": [],
            "balance": coinop.get_balance(sid).get("balance", 0),
        })

    # First-time redeem for this checkout id. Two behaviours, decided by
    # the actual player state — one payment endpoint serves both flows:
    #
    #   * DEAD player → mint the revive (calls api_revive to bring them
    #     back and append the continue_used narrative beat), on top of
    #     the credits that verify_and_redeem already granted.
    #   * ALIVE player → don't revive (there's nothing to revive from),
    #     just report the new balance. This is the "insert coin to keep
    #     playing" pause overlay's happy path.
    #
    # This lets one Stripe SKU serve both the death-continue and the
    # credit-topup flows — cheaper cognitively for the player, and we
    # don't have to run two separate product/price ids on the Stripe
    # side just to distinguish them.
    revive_items = None
    try:
        st = engine.get_state(sid) or {}
        alive = (st.get("player_state") or {}).get("alive", True)
    except Exception:
        alive = True

    if not alive:
        revive_response = engine.api_revive()
        revive_items = revive_response.get_json() if hasattr(revive_response, 'get_json') else None

    return jsonify({
        "ok": True,
        "already_redeemed": False,
        "comp": result.get('comp', False),
        "revived": (revive_items is not None),
        "credits_added": result.get('credits_added', 0),
        "balance": result.get('balance', coinop.get_balance(sid).get("balance", 0)),
        "revive_items": revive_items,
    })


@app.route('/webhook/stripe', methods=['POST'])
def _coinop_webhook():
    if not (coinop.is_enabled() or billing.is_payments_enabled()):
        return jsonify({"error": "payments_disabled"}), 404
    payload = request.get_data()
    sig = request.headers.get('Stripe-Signature', '')
    result = coinop.handle_webhook(payload, sig)
    if not result.get('ok'):
        return jsonify(result), 400
    return jsonify(result)
# Vision for the realtime renderer: the client posts the actual on-screen video
# frame; the engine analyzes it and re-grounds the simulation so it tracks the
# video instead of drifting from the still. See engine.api_observe.
def _gated_observe():
    blocked = _spend_blocked()
    if blocked:
        return blocked
    return engine.api_observe()


app.add_url_rule('/api/observe', 'standalone_api_observe', _gated_observe, methods=['POST'])
# Realtime object recognition for the SCAN tool: the client posts the on-screen
# video frame; the engine returns the prominent, interactable objects visible in
# it plus their positions so the UI can float "starfield" tags. Stateless /
# read-only (does not mutate the sim). See engine.api_detect.
def _gated_detect():
    if getattr(engine, "DETECT_BACKEND", "local") != "local":
        blocked = _spend_blocked()
        if blocked:
            return blocked
    return engine.api_detect()


app.add_url_rule('/api/detect', 'standalone_api_detect', _gated_detect, methods=['POST'])
# Realtime danger grading for the peripheral-vignette / health system: the
# client posts the on-screen video frame at ~1 Hz; the engine returns a single
# ordinal threat level (0 safe / 1 threatened / 2 attacking) for that frame.
# The level drives the client's danger state machine (SAFE → WARNING →
# HURTING) which pulses the red peripheral vignette and drains health when
# danger persists. Stateless / read-only. See engine.api_danger.
def _gated_danger():
    blocked = _spend_blocked()
    if blocked:
        return blocked
    return engine.api_danger()


app.add_url_rule('/api/danger', 'standalone_api_danger', _gated_danger, methods=['POST'])


# Dictation for the free-will box. The client records the player speaking and
# posts the clip; the engine returns the words. This is the fallback path for
# the browser's own SpeechRecognition, which is free and instant where it
# works but fails hard with `network` on any Chromium build that shipped
# without Google's speech key — and there is no way to tell in advance, since
# the API is present either way. Stateless / read-only, like /api/danger.
def _gated_transcribe():
    blocked = _spend_blocked()
    if blocked:
        return blocked
    return engine.api_transcribe()


app.add_url_rule('/api/transcribe', 'standalone_api_transcribe',
                 _gated_transcribe, methods=['POST'])
# Opt-in experimental: same wire contract as /api/detect but the frame is
# pushed into a persistent Gemini Live-API WebSocket session, and the endpoint
# returns whatever detections that session has produced most recently. See
# gemini_live_vision.py for the design, tradeoffs, and known caveats (1 FPS
# input cap, ~100 s session rotation, WSS is billed for wall-clock).
# Registered only when DETECT_LIVE_API=1 + GEMINI_API_KEY + google-genai are
# all present, so this is a no-op in the default deploy.
try:
    import gemini_live_vision as _live_vision  # noqa: WPS433
    if _live_vision.is_available():
        def _api_detect_live():
            import base64 as _b64
            import re as _re
            from flask import request as _req, jsonify as _jsonify
            blocked = _spend_blocked()
            if blocked:
                return blocked
            data = _req.get_json(silent=True) or {}
            frame_b64 = data.get('frame')
            session_id = data.get('session_id', 'default')
            if not frame_b64:
                return _jsonify({"error": "missing frame"}), 400
            mime_match = _re.match(r'^data:(image/[^;]+);base64,(.*)$',
                                   frame_b64, _re.DOTALL)
            if mime_match:
                mime_type = mime_match.group(1)
                raw = mime_match.group(2)
            else:
                mime_type = "image/jpeg"
                raw = frame_b64
            try:
                img_bytes = _b64.b64decode(raw)
            except Exception:
                return _jsonify({"error": "bad frame encoding"}), 400
            if len(img_bytes) < 512:
                return _jsonify({"error": "frame too small"}), 400
            scene_prompt = ""
            try:
                _st = engine.get_state(session_id) or {}
                scene_prompt = engine._detect_scene_prior(_st)
            except Exception:
                scene_prompt = ""
            _live_vision.push_frame(
                session_id, img_bytes,
                mime_type=mime_type, scene_prompt=scene_prompt,
            )
            objects = _live_vision.get_latest_detections(session_id) or []
            return _jsonify({"objects": objects, "source": "live-api"})
        app.add_url_rule(
            '/api/detect/live', 'standalone_api_detect_live',
            _api_detect_live, methods=['POST'],
        )
        print("[LIVE VISION] /api/detect/live registered (DETECT_LIVE_API=1)")
except Exception as _e:  # noqa: BLE001
    print(f"[LIVE VISION] not available: {_e}")
# Photo appraisal for the reward loop: the client posts a captured crop and the
# engine returns an evidence-style breakdown (notable items + interest rating +
# a terse "why it matters" note, plus a caption/mood) that the UI prints as a
# scoring "receipt". Stateless / read-only. See engine.api_photo.
def _gated_photo():
    blocked = _spend_blocked()
    if blocked:
        return blocked
    return engine.api_photo()


app.add_url_rule('/api/photo', 'standalone_api_photo', _gated_photo, methods=['POST'])


def _gated_viewfinder():
    blocked = _spend_blocked()
    if blocked:
        return blocked
    return engine.api_viewfinder()


app.add_url_rule('/api/viewfinder', 'standalone_api_viewfinder', _gated_viewfinder, methods=['POST'])
# Investigation textures: the client crops a small thumbnail from the scene
# around/under the TOUCH reticle (or, later, a "photograph") and stores it here.
# These specimens persist to disk + state['investigations'] as raw material for
# future scene-driven prompt mechanics. See engine.api_investigate.
app.add_url_rule('/api/investigate', 'standalone_api_investigate', engine.api_investigate, methods=['POST'])
app.add_url_rule('/api/investigations', 'standalone_api_investigations', engine.api_investigations, methods=['GET'])
# TALK tool: open a story-aware conversation with a SCAN subject the model
# classified as able to speak (a person/character/creature/voice-machine). The
# session endpoint assembles the awareness briefing and, when ElevenLabs is
# configured, returns voice-agent config; otherwise the UI falls back to a text
# conversation driven by the message endpoint. Both are stateless / read-only.
# See engine.api_talk_session / engine.api_talk_message.
def _gated_talk_session():
    blocked = _spend_blocked()
    if blocked:
        return blocked
    return engine.api_talk_session()


def _gated_talk_message():
    blocked = _spend_blocked()
    if blocked:
        return blocked
    return engine.api_talk_message()


def _gated_talk_portrait():
    blocked = _spend_blocked()
    if blocked:
        return blocked
    return engine.api_talk_portrait()


app.add_url_rule('/api/talk/session', 'standalone_api_talk_session', _gated_talk_session, methods=['POST'])
app.add_url_rule('/api/talk/message', 'standalone_api_talk_message', _gated_talk_message, methods=['POST'])
# Conversation Moment portrait: a fast cinematic medium-shot of the subject
# (distinct lens language from the handheld world view). Cached per
# (session, subject, scene); see engine.api_talk_portrait.
app.add_url_rule('/api/talk/portrait', 'standalone_api_talk_portrait', _gated_talk_portrait, methods=['POST'])
# Companions: characters the player has spoken with are saved to a roster WITH
# their cinematic portrait (engine.api_talk_portrait records them), so they can
# be listed and placed back into later scenes for a continuing story.
app.add_url_rule('/api/companions', 'standalone_api_companions', engine.api_companions, methods=['GET'])
app.add_url_rule('/api/companions/place', 'standalone_api_companion_place', engine.api_companion_place, methods=['POST'])
# Rebuild a companion's ElevenLabs voice from the stored Voice Design brief
# (persisted by api_talk_session). Poll /api/talk/voice/status while generating.
def _gated_companion_regenerate_voice():
    blocked = _spend_blocked()
    if blocked:
        return blocked
    return engine.api_companion_regenerate_voice()


app.add_url_rule('/api/companions/regenerate_voice',
                 'standalone_api_companion_regenerate_voice',
                 _gated_companion_regenerate_voice, methods=['POST'])
# CAMP Moment: night campsite establishing shot compositing the jeep prop +
# up to 5 companion portraits. Side pocket — does not advance the turn loop.
app.add_url_rule('/api/camp/enter', 'standalone_api_camp_enter', engine.api_camp_enter, methods=['POST'])


def _gated_encounter_begin():
    blocked = _spend_blocked()
    if blocked:
        return blocked
    return engine.api_encounter_begin()


def _gated_encounter_resolve():
    """One credit, same as /api/choose — do not also POST choose."""
    sid = engine._resolve_request_session_id()
    blocked = _spend_blocked()
    if blocked:
        return blocked
    cost_before = 0.0
    try:
        import cost_tracker
        cost_before = float(cost_tracker.session_cost_usd(sid) or 0.0)
    except Exception:
        cost_before = 0.0
    gated = coinop.is_credit_gating_enabled()
    debited = False
    if gated:
        spend = coinop.spend_credit(sid, amount=1, reason="encounter_resolve")
        if not spend.get("ok"):
            return jsonify({
                "needs_coin": True,
                "balance": int(spend.get("balance", 0)),
                "reason": spend.get("reason", "insufficient_credits"),
                "message": "Out of coins — insert more to keep playing.",
            }), 402
        debited = True

    try:
        response = engine.api_encounter_resolve()
    except Exception:
        if debited:
            try:
                coinop.grant_credits(sid, 1, source="refund")
            except Exception:
                traceback.print_exc()
        raise

    try:
        payload = response[0] if isinstance(response, tuple) else response
        status = response[1] if isinstance(response, tuple) and len(response) > 1 else None
        if not isinstance(status, int):
            status = getattr(payload, "status_code", 200)
    except Exception:
        payload = response
        status = 200
    cached = False
    try:
        body = payload.get_json(silent=True) if hasattr(payload, "get_json") else None
        cached = bool(isinstance(body, dict) and body.get("cached"))
    except Exception:
        cached = False
    if debited and (status >= 400 or cached):
        try:
            coinop.grant_credits(sid, 1, source="refund")
        except Exception:
            traceback.print_exc()
    if status < 400 and not cached:
        try:
            billing.settle_session(sid, cost_before)
        except Exception:
            traceback.print_exc()
    return response


def _gated_cutscene_play():
    blocked = _spend_blocked()
    if blocked:
        return blocked
    return engine.api_cutscene_play()


app.add_url_rule('/api/cutscene/play', 'standalone_api_cutscene_play',
                 _session_scoped(_gated_cutscene_play), methods=['POST'])
app.add_url_rule('/api/cutscene/complete', 'standalone_api_cutscene_complete',
                 _session_scoped(engine.api_cutscene_complete), methods=['POST'])
app.add_url_rule('/api/encounter/begin', 'standalone_api_encounter_begin',
                 _session_scoped(_gated_encounter_begin), methods=['POST'])
app.add_url_rule('/api/encounter/resolve', 'standalone_api_encounter_resolve',
                 _session_scoped(_gated_encounter_resolve), methods=['POST'])
app.add_url_rule('/api/encounter/roll', 'standalone_api_encounter_roll',
                 _session_scoped(engine.api_encounter_roll), methods=['POST'])


@app.route('/api/director/pick', methods=['POST'])
@_session_scoped
def api_director_pick():
    """Which option on screen makes the best next minute of television?

    Watch has nobody at the controls and used to advance on a coin toss.
    Always 200s with a usable index — a slow or unhappy model must never
    be able to stall an unattended run, so every failure is a pick.
    """
    body = request.get_json(silent=True) or {}
    choices = body.get('choices') or []
    session_id = str(body.get('session_id') or 'default')
    try:
        import director
        return jsonify(director.pick(choices, session_id,
                                     scene=str(body.get('scene') or '')))
    except Exception as e:
        engine.log_error(f"[DIRECTOR] endpoint failed: {e}")
        return jsonify({"index": -1, "why": "", "source": "error"})
app.add_url_rule('/api/encounter/travel', 'standalone_api_encounter_travel',
                 _session_scoped(engine.api_encounter_travel), methods=['POST'])
# Refcount + status endpoints for the dynamic per-character voices designed
# on the fly by voice_design.py. /talk/end lets the client drop the refcount
# on the active voice when the TALK widget closes so session-cleanup can
# reap it; /talk/voice/status is the poll a client uses to hot-swap the
# Convai TTS override once a designed voice lands. Both are best-effort:
# 200s even on internal failure so end-of-call cleanup never surfaces as a
# user-visible error, and both no-op when voice_design is unavailable.
app.add_url_rule('/api/talk/end', 'standalone_api_talk_end', engine.api_talk_end, methods=['POST'])
app.add_url_rule('/api/talk/voice/status', 'standalone_api_talk_voice_status', engine.api_talk_voice_status, methods=['GET'])
# Opt-in experimental: bidirectional Gemini Live-API session for TALK,
# replacing the ElevenLabs voice hop with native-audio streaming from Gemini
# itself (and optionally sharing live video frames so the character sees the
# scene the player is looking at). See gemini_live_talk.py + LIVE_TALK_PROTOTYPE.md
# for design, tradeoffs, and known caveats (1 FPS video cap, ~100 s session
# rotation, WSS is billed for wall-clock, transport is not manually validated
# in the cloud env).
# Registered only when TALK_LIVE_API=1 + GEMINI_API_KEY + google-genai + a
# flask-sock instance are ALL present, so this is a no-op in the default
# deploy — the existing /api/talk/session (ElevenLabs) path is untouched.
try:
    import gemini_live_talk as _live_talk  # noqa: WPS433
    if _sock is not None and _live_talk.is_available():
        @_sock.route('/ws/talk/live')
        def _ws_talk_live(ws):
            """First frame is a JSON handshake:
                {"type":"start","subject":{"label":..,"kind":..},"session_id":"default"}
            Then bidirectional streaming (see gemini_live_talk.py docstring)."""
            try:
                first = ws.receive(timeout=10)
            except Exception:
                return
            if not first:
                return
            try:
                handshake = json.loads(first)
            except Exception:
                try:
                    ws.send(json.dumps({"type": "error",
                                        "message": "bad handshake (expected JSON)"}))
                except Exception:
                    pass
                return
            if not isinstance(handshake, dict) or handshake.get("type") != "start":
                try:
                    ws.send(json.dumps({"type": "error",
                                        "message": "first message must be {type:'start', subject, session_id}"}))
                except Exception:
                    pass
                return
            subject = handshake.get("subject") or {}
            session_id = handshake.get("session_id", "default")
            _live_talk.handle_websocket(ws, subject, session_id)
        print("[LIVE TALK] /ws/talk/live registered (TALK_LIVE_API=1)")
except Exception as _e:  # noqa: BLE001
    print(f"[LIVE TALK] not available: {_e}")
# Voice registry: the selectable voices + per-kind/cast mappings, so the client
# can offer a LIVE voice switcher for interactions (and the narrator). The
# session endpoint accepts a `voice_id` to change a subject's voice on the fly.
app.add_url_rule('/api/talk/voices', 'standalone_api_talk_voices', engine.api_talk_voices, methods=['GET'])
# NARRATOR stream: a one-way voice OVER the scene for world-building, able to
# speak as a single archive voice or a small cast (radio-play handoffs). `say`
# voices one line, `narrate` voices a multi-character script, `worldbuild`
# GENERATES a story-aware narration (LLM) and optionally speaks it, and `cast`
# advertises the available voices. Audio needs ELEVENLABS_API_KEY; without it
# they degrade to text. All read-only. See engine.api_narrator_*.
app.add_url_rule('/api/narrator/cast', 'standalone_api_narrator_cast', engine.api_narrator_cast, methods=['GET'])
app.add_url_rule('/api/narrator/say', 'standalone_api_narrator_say', engine.api_narrator_say, methods=['POST'])
app.add_url_rule('/api/narrator/narrate', 'standalone_api_narrator_narrate', engine.api_narrator_narrate, methods=['POST'])
app.add_url_rule('/api/narrator/worldbuild', 'standalone_api_narrator_worldbuild', engine.api_narrator_worldbuild, methods=['POST'])


@app.route('/images/<filename>', methods=['GET'])
def serve_legacy_image(filename):
    """Serve scene images produced by the feed-based engine.

    `_gen_image` writes frames into the per-session image directory
    (sessions/<id>/images/, 'default' for the standalone/web path) but returns
    a flat '/images/<filename>' URL. Each session has its OWN images/ dir, so
    the basename alone is ambiguous: `_to_web_image_url` therefore stamps a
    '?session=<id>' query param whenever the frame belongs to a non-default
    session (e.g. a shared '/play?session=<id>' link). Resolve that session's
    dir first, then fall back to the default session dir and the legacy root
    images/ dir so older/session-less URLs still resolve. As a last resort we
    scan every session's images/ dir for the basename, so a stale URL that lost
    its session param still finds its frame instead of 404ing (the reported
    "game hangs on starting a session" — the intro image never loaded).
    Mirrors the path-traversal protection used by the session/archive image
    routes."""
    try:
        safe_filename = Path(filename).name
        session_id = request.args.get('session') or request.args.get('session_id')
        candidates = []
        if session_id:
            # Sanitize to a bare directory name; _get_image_dir validates + mkdirs,
            # so guard against traversal / empty / malformed ids: on rejection just
            # fall through to the default/legacy/scan candidates instead of 500-ing.
            safe_session = Path(str(session_id)).name
            if safe_session:
                try:
                    candidates.append(Path(engine._get_image_dir(safe_session)) / safe_filename)
                except ValueError:
                    pass
        candidates.append(Path(engine._get_image_dir('default')) / safe_filename)  # standalone/web session
        candidates.append(Path("images") / safe_filename)                          # legacy/global fallback
        mimetype = 'image/gif' if safe_filename.lower().endswith('.gif') else 'image/png'
        for image_path in candidates:
            if image_path.exists():
                return send_file(str(image_path), mimetype=mimetype)
        # Final fallback: locate the frame in any session's images/ dir. Only
        # reached when the direct candidates miss (e.g. a URL that lost its
        # ?session= param), so the extra scan stays off the hot path.
        fallback = _find_image_in_any_session(safe_filename)
        if fallback is not None:
            return send_file(str(fallback), mimetype=mimetype)
        # Every frame is written alongside a downsampled `<name>_small.png` used
        # for vision calls, and the small one outlives the full-res frame when a
        # session sweep trims disk. Serving it beats a 404: a slightly soft
        # frame is invisible next to a hole in the feed.
        low = safe_filename.lower()
        if low.endswith('.png') and not low.endswith('_small.png'):
            small_name = safe_filename[:-4] + '_small.png'
            for image_path in candidates:
                small_path = image_path.with_name(small_name)
                if small_path.exists():
                    return send_file(str(small_path), mimetype='image/png')
            small_fallback = _find_image_in_any_session(small_name)
            if small_fallback is not None:
                return send_file(str(small_fallback), mimetype='image/png')
        return error_response("Image not found", code=404)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to serve image", str(e))


def _find_image_in_any_session(safe_filename):
    """Scan every session's images/ dir for `safe_filename`, returning the first
    match (or None). Used as a last-resort fallback in serve_legacy_image for
    session-less URLs. `safe_filename` must already be sanitized to a basename."""
    try:
        sessions_root = engine._get_session_root('default').parent
        if not sessions_root.exists():
            return None
        for session_dir in sessions_root.iterdir():
            if not session_dir.is_dir():
                continue
            candidate = session_dir / "images" / safe_filename
            if candidate.exists():
                return candidate
    except Exception:
        traceback.print_exc()
    return None


@app.route('/api/scene_audio', methods=['POST'])
def api_scene_audio():
    """Generate (or reuse a cached) scene-matched instrumental clip for a guide
    image and return its URL.

    The standalone UI posts the scene descriptor (`metadata.prompt`, already
    delivered with every `scene_image`) here; we render an ElevenLabs Music
    bed plus looping world SFX the client plays together, re-scoring on each
    new scene. Degrades to null URLs whenever audio can't be produced (no
    ELEVENLABS_API_KEY, or the call failed) so the client stays silent
    instead of erroring."""
    try:
        body = request.get_json(silent=True) or {}
        prompt = (body.get("prompt") or "").strip()
        session_id = body.get("session") or "default"
        mode = (body.get("mode") or "scene").strip().lower()
        if mode not in ("scene", "conversation", "encounter"):
            mode = "scene"
        if not prompt:
            return jsonify({"audio_url": None, "sfx_url": None,
                            "stinger_url": None, "pending_music": False,
                            "pending_sfx": False, "reason": "no_prompt"})
        result = scene_audio.get_scene_audio(prompt, session_id=session_id, mode=mode)
        if not result:
            return jsonify({"audio_url": None, "sfx_url": None,
                            "stinger_url": None, "pending_music": False,
                            "pending_sfx": False,
                            "reason": scene_audio.unavailable_reason() or "unavailable"})
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        # Never surface a hard error for a non-critical enhancement.
        return jsonify({"audio_url": None, "sfx_url": None,
                        "stinger_url": None, "pending_music": False,
                        "pending_sfx": False, "reason": "error", "details": str(e)})


@app.route('/api/action_foley', methods=['POST'])
def api_action_foley():
    """The sound of one player action, from the choice text that names it.

    The client asks for every choice the moment the slate renders and plays the
    matching one on click, so by then it is a cache hit. A miss is reported
    `pending` rather than waited on — see scene_audio.action_foley.
    """
    try:
        body = request.get_json(silent=True) or {}
        action = (body.get("action") or "").strip()
        session_id = body.get("session") or "default"
        result = scene_audio.action_foley(action, session_id=session_id)
        if not result:
            if not scene_audio.action_foley_enabled():
                why = "off"
            elif not scene_audio.is_available():
                why = scene_audio.unavailable_reason() or "unavailable"
            else:
                why = "no_action"
            return jsonify({"url": None, "pending": False, "reason": why})
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"url": None, "pending": False, "reason": "error",
                        "details": str(e)})


@app.route('/api/consequence_audio', methods=['POST'])
def api_consequence_audio():
    """The bed the flipbook plays over, from the consequence prose.

    Asked for the moment the consequence lands — five pipeline steps before the
    picture — so the clip is on disk by the time the frames it runs under
    exist. A miss is reported `pending` rather than waited on, exactly like
    action foley; see scene_audio.consequence_bed.
    """
    try:
        body = request.get_json(silent=True) or {}
        text = (body.get("text") or "").strip()
        session_id = body.get("session") or "default"
        result = scene_audio.consequence_bed(text, session_id=session_id)
        if not result:
            if not scene_audio.consequence_bed_enabled():
                why = "off"
            elif not scene_audio.is_available():
                why = scene_audio.unavailable_reason() or "unavailable"
            else:
                why = "no_text"
            return jsonify({"url": None, "pending": False, "reason": why})
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"url": None, "pending": False, "reason": "error",
                        "details": str(e)})


@app.route('/audio/<filename>', methods=['GET'])
def serve_scene_audio(filename):
    """Serve generated scene audio (mirrors /images session fallback)."""
    try:
        session_id = request.args.get("session") or request.args.get("session_id") or "default"
        path = scene_audio.resolve_audio_path(filename, session_id)
        if path and path.exists():
            ext = path.suffix.lstrip('.').lower()
            return send_file(str(path),
                             mimetype=scene_audio.LOOP_EXTS.get(ext, 'audio/wav'))
        return error_response("Audio not found", code=404)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to serve audio", str(e))


def _standalone_asset_version():
    """Cache-bust CSS/JS on every deploy so browsers never serve stale UI.

    Covers the standalone immersive UI and the lobby landing page so an edit
    to either set of assets forces a fresh fetch."""
    candidates = [
        "static/css/standalone.css",
        "static/js/standalone.js",
        "static/js/editor_graph.js",
        "static/js/reactor_renderer.js",
        "static/js/moments.js",
        "static/css/lobby.css",
        "static/js/lobby.js",
    ]
    latest = 0
    for path in candidates:
        try:
            latest = max(latest, os.path.getmtime(path))
        except Exception:
            pass
    stamp = str(int(latest)) if latest else "0"
    boot = (os.environ.get("SOMEWHERE_BOOT") or "").strip()
    return f"{stamp}-{boot}" if boot else stamp


@app.route('/standalone', methods=['GET'])
@app.route('/play', methods=['GET'])
def serve_standalone():
    """Serve the standalone immersive UI. The multi-user session framework
    lets each browser load a different persisted instance via ?session=<id>;
    when no session is provided, we fall through to the legacy 'default'
    slot so direct /standalone links keep working.

    The client bootstrap POSTs /api/reset on cold load, but the reset call
    is now session-aware — see engine.session_context — so a page loaded
    with ?session=abc123 seeds/resets exactly that session's on-disk state
    without touching any other user's run."""
    return render_template(
        'standalone.html',
        asset_version=_standalone_asset_version(),
        session_id=(request.args.get('session') or request.args.get('session_id') or ''),
    )


@app.route('/realtime', methods=['GET'])
@app.route('/live', methods=['GET'])
def serve_realtime():
    """Dedicated URL for the realtime world-model (Reactor) flow.

    Same immersive UI as /standalone, but the scene renderer is forced to
    "reactor" regardless of the server default or any saved per-browser
    preference — so this URL always demonstrates the live video pipeline.
    Handy for testing/sharing the realtime experience directly."""
    return render_template(
        'standalone.html',
        asset_version=_standalone_asset_version(),
        forced_renderer='reactor',
        session_id=(request.args.get('session') or request.args.get('session_id') or ''),
    )


@app.route('/lobby', methods=['GET'])
def serve_lobby():
    """Web start screen — same graphic language as the desktop Play / Watch
    menu. PLAY mints a session and enters the game; WATCH opens the studio;
    CONTINUE lists saved runs. Session create still goes through
    /api/lobby/create, then /play?session=<id>&mode=play."""
    return render_template(
        'lobby.html',
        asset_version=_standalone_asset_version(),
    )


@app.route('/api/tape', methods=['GET'])
def api_tape():
    """Ordered scene frames captured on THIS run, for VHS tape playback.

    Reads the run's own `tape_frames` list, written as each canonical scene
    frame lands. It used to glob the image directory by mtime, which had two
    bugs: it always read the 'default' session (so a player on their own
    session watched somebody else's tape), and mtime order splices every run
    that session has ever played into one reel. The glob survives only as a
    fallback for sessions that predate the list.
    """
    try:
        from pathlib import Path as _P
        session_id = request.args.get('session') or request.args.get('session_id') or 'default'
        session_id = _P(str(session_id)).name or 'default'
        try:
            st = engine._load_state(session_id)
        except Exception:
            st = {}
        frames = [f for f in (st.get('tape_frames') or []) if isinstance(f, str)]

        if not frames:
            img_dir = _P(engine._get_image_dir(session_id))
            if img_dir.exists():
                files = [
                    p for p in img_dir.glob('*.png')
                    if not p.name.endswith('_small.png')
                    # A hard cut writes a `_styleswatch.png` next to the frame:
                    # the previous still reduced to a blurred colour field, an
                    # input to the next render and never something to look at.
                    # Without this the tape showed it as a frame, which is the
                    # "it just displays a blurry image" report.
                    and not p.name.endswith('_styleswatch.png')
                    and 'flipbook' not in p.name.lower()
                    and not p.name.startswith('observed_')  # low-res video grabs, not canonical stills
                ]
                files.sort(key=lambda p: p.stat().st_mtime)
                frames = [f"/images/{p.name}" for p in files]
        return jsonify({"frames": frames, "count": len(frames)})
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to build tape", str(e))


_OBJECTIVES_CACHE = {"key": None, "value": None}


@app.route('/api/objectives', methods=['GET'])
def api_objectives():
    """The GENERATIVE objectives directive for the standalone tracker.

    Returns the player's evolving "current lead" — a short in-world objective
    grounded in the live world state (premise, recent beats, phase, discovered
    elements). The client blends this into its top-right objectives HUD.

    Cached per (turn, phase) so the same turn's directive is only generated once
    even if the client (or auto-play) asks repeatedly. Never errors: engine.
    generate_directive() always degrades to a deterministic, in-fiction lead.

    Response JSON: {"lead": str, "detail": str, "generated": bool}
    """
    try:
        session_id = engine._resolve_request_session_id()
        s = engine.get_state(session_id) or {}
        # Cache is keyed by session too, so two players' leads never collide.
        # NOTE: deliberately NOT keyed on turn_count. Re-deriving the lead every
        # single turn made the objective "drift" — it rewrote itself constantly
        # so the player never had a stable goal to pursue (the "directionless"
        # complaint). Instead the lead is STICKY: it only refreshes when the
        # world meaningfully changes — the phase escalates or a NEW element is
        # discovered — so a lead persists across turns until it's plausibly
        # resolved or the situation shifts.
        key = (session_id, s.get("current_phase", "normal"), len(s.get("seen_elements") or []))
        if _OBJECTIVES_CACHE.get("key") == key and _OBJECTIVES_CACHE.get("value"):
            return jsonify(_OBJECTIVES_CACHE["value"])
        directive = engine.generate_directive(session_id)
        if not isinstance(directive, dict) or not directive.get("lead"):
            directive = {"lead": "Survey the area",
                         "detail": "Read the scene and document your first real subject.",
                         "generated": False}
        _OBJECTIVES_CACHE["key"] = key
        _OBJECTIVES_CACHE["value"] = directive
        return jsonify(directive)
    except Exception as e:
        traceback.print_exc()
        # A safe, in-fiction default so the tracker's LEAD is never blank.
        return jsonify({"lead": "Survey the area",
                        "detail": "Read the scene and document your first real subject.",
                        "generated": False})


def _setting_plate_url():
    """First authored level plate, if any. Cheap disk lookup — no generation."""
    try:
        import game_identity
        spec = game_identity.get_spec()
        ids = (spec.get(game_identity.SETTING_KEY) or {}).get("reference_images") or []
        for ref_id in ids:
            if game_identity.reference_path(ref_id):
                return game_identity.reference_url(ref_id)
    except Exception:
        return None
    return None


def _status_world_name(state: dict) -> str:
    """Display name of the World this run is currently in, or empty."""
    wid = str((state or {}).get("experience_world_id") or "").strip()
    if not wid:
        return ""
    try:
        import experience_store
        world = experience_store.world_by_id(experience_store.get_experience(), wid)
        return str((world or {}).get("name") or "").strip()
    except Exception:
        return ""


@app.route('/api/status', methods=['GET'])
def api_status():
    """Lightweight state snapshot for the standalone UI's HUD. Does not
    call any LLM/image backend — pure read of the on-disk state.

    Session-aware: reads the caller's OWN session from disk (resolved from
    ?session_id=/body/X-Session-Id) instead of the shared engine.state global.
    Reading the global made the HUD show whichever session most recently
    swapped the mirror — wrong (and flickering) as soon as two people play at
    once."""
    try:
        session_id = engine._resolve_request_session_id()
        s = engine.get_state(session_id) or {}

        # Resolve inventory item ids to display names + emoji for the HUD.
        inventory = []
        try:
            from items import ITEMS
            for item_id in (s.get("inventory") or []):
                meta = ITEMS.get(item_id)
                if meta:
                    inventory.append({
                        "id": item_id,
                        "display": meta.get("display", item_id),
                        "emoji": meta.get("emoji", ""),
                    })
                else:
                    inventory.append({"id": item_id, "display": item_id, "emoji": ""})
        except Exception:
            inventory = [{"id": i, "display": i, "emoji": ""} for i in (s.get("inventory") or [])]

        return jsonify({
            "phase": s.get("current_phase", "normal"),
            # Two different dials, and they were easy to confuse while only one
            # was visible. `chaos` is volatile 0..CHAOS_MAX — how bad it is
            # RIGHT NOW, and it falls again on a quiet turn. `threat` only ever
            # climbs and is what derives the phase, so it reads as how far into
            # the story the run has pushed.
            "chaos": s.get("chaos_level", 0),
            "chaos_max": engine.CHAOS_MAX,
            "threat": s.get("threat_level", 0),
            "turn": s.get("turn_count", 0),
            "alive": s.get("player_state", {}).get("alive", True),
            # `health` / `health_max` / `injuries` used to be reported here off a
            # hit-point pool and a scraped wound list. Both were removed (see
            # engine's "how a run ends"): death is the model's verdict now, and
            # detection below is the dial the player watches climb toward it.
            # How much the world knows about the player: hidden / suspicious /
            # alerted / hunted, plus the raw heat behind it. `in_combat` is no
            # longer dead state — it means alerted or worse.
            "detection": engine.DETECT_NAMES[engine.get_detection(s)["level"]],
            "detection_heat": engine.get_detection(s)["heat"],
            "detection_max": engine.DETECT_HEAT_MAX,
            # Which sensor last moved that dial: "frame" when the picture
            # itself showed somebody who could see the player, "prose" when
            # the narration said so, "" when nothing did. The HUD shows it
            # because a number that answers the picture is believable and one
            # that moves for invisible reasons is the thing this replaced.
            "detection_source": engine.get_detection(s)["source"],
            "in_combat": s.get("in_combat", False),
            "time_of_day": s.get("time_of_day", ""),
            "inventory": inventory,
            "backend": ai_provider_manager.active_backend("chat"),
            # Report the RESOLVED image provider (honors a backend override) so a
            # fully-offline/mock run reads "mock" instead of advertising the
            # configured live provider it isn't actually using. Identical to
            # get_image_provider() whenever no override is active (production).
            "image_provider": ai_provider_manager.active_backend("image"),
            "image_model": ai_provider_manager.get_image_model(),
            # Resolution belongs next to the model: the same model at 4K is a
            # different wait and a different bill, and a render transcript that
            # records one without the other can't say what it cost.
            "image_size": ai_provider_manager.get_image_size(),
            "image_enabled": engine.IMAGE_ENABLED,
            # Renderer selection + the latest scene prompt, so the standalone
            # client can steer the Reactor realtime world model with the same
            # text used to generate the still image.
            "renderer": getattr(engine, "SCENE_RENDERER", "image"),
            "current_image_prompt": s.get("current_image_prompt", ""),
            "current_render_prompt": s.get("current_render_prompt", ""),
            # What vision READ off the frame that rendered — not what the image
            # model was asked to draw. The scene audio is scored from this: it
            # is the only text in the system that describes the picture the
            # player is looking at, with no camera or film-stock language in it
            # for a sound model to choke on.
            "current_vision": s.get("current_vision", ""),
            # Free stills the start menu can warm — last run frame, or the
            # authored level plate. Never triggers generation.
            "current_image_url": s.get("current_image_url") or None,
            # Flipbook: the frames of the CURRENT beat, so a client that joined
            # or reloaded mid-run gets this turn's motion and not just the still
            # it ends on. None on a still-only turn.
            "current_sequence": s.get("current_sequence") or None,
            "flipbook": engine.flipbook_settings(s),
            "setting_plate_url": _setting_plate_url(),
            # Live Experience graph: which World this run is standing in, so
            # the editor overlay can mark it during Play / Watch.
            "experience_world_id": s.get("experience_world_id") or "",
            "experience_world_name": _status_world_name(s),
            "world_turn_count": int(s.get("world_turn_count") or 0),
        })
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to get status", str(e))


# ═══════════════════════════════════════════════════════════════════
# REACTOR (REALTIME WORLD-MODEL RENDERER) ENDPOINTS
#
# Reactor's SDK runs in the browser and receives live WebRTC video, but the
# Reactor API key must never reach the client. These endpoints let the server
# proxy a short-lived JWT (per Reactor's auth docs) and advertise the active
# renderer config so the standalone UI can decide whether to show the Gemini
# still or the realtime video. See REACTOR_INTEGRATION_PLAN.md.
# ═══════════════════════════════════════════════════════════════════

REACTOR_TOKEN_URL = os.getenv("REACTOR_API_URL", "https://api.reactor.inc").rstrip("/") + "/tokens"


@app.route('/api/reactor/config', methods=['GET'])
def api_reactor_config():
    """Advertise the realtime-renderer config to the client (no secrets).

    `available_models` lists the world models the client can switch between
    live, mid-game; `world_model` is the server default. `model_name` is kept
    for back-compat (the SDK name of the default model). `camera` is the
    authored cast-sheet camera (see /api/camera), which the world build and
    every live re-steer have to honour.
    """
    import game_identity
    default_id = getattr(engine, "REACTOR_WORLD_MODEL", "happy-oyster")
    models = getattr(engine, "AVAILABLE_WORLD_MODELS", [])
    default_sdk = engine.world_model_sdk_name(default_id) if hasattr(engine, "world_model_sdk_name") \
        else os.getenv("REACTOR_MODEL", "reactor/happy-oyster")
    resp = jsonify({
        "enabled": keys_store.has_provider_key("reactor"),
        "renderer": getattr(engine, "SCENE_RENDERER", "image"),
        "model_name": default_sdk,
        "world_model": default_id,
        "available_models": models,
        "camera": game_identity.live_camera_contract(),
        # When true the client may connect to ANY model name a tester types in,
        # even one not in available_models — so a newly shipped Reactor model is
        # usable the moment it exists, with no server change. It also tells the
        # client how to turn a bare id into an SDK name for custom models.
        "allow_custom_models": bool(getattr(engine, "REACTOR_ALLOW_CUSTOM_MODELS", True)),
        "sdk_name_prefix": "reactor/",
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route('/api/reactor/token', methods=['POST'])
def api_reactor_token():
    """Mint a short-lived Reactor JWT by exchanging the server-side API key.

    Mirrors Reactor's documented auth flow: POST /tokens with the
    `Reactor-API-Key` header returns `{ "jwt": ..., "expires_at": ... }`. The
    API key stays on the server; only the short-lived token reaches the browser.
    """
    blocked = _spend_blocked(min_usd=0.25)
    if blocked:
        return blocked
    keys_store.has_provider_key("reactor")
    api_key = os.getenv("REACTOR_API_KEY")
    if not api_key:
        return error_response(
            "Reactor is not configured",
            "Set the REACTOR_API_KEY environment variable to enable the realtime renderer.",
            code=503,
        )
    try:
        import requests
        resp = requests.post(
            REACTOR_TOKEN_URL,
            headers={"Reactor-API-Key": api_key},
            timeout=15,
        )
        if resp.status_code != 200:
            return error_response(
                "Reactor token exchange failed",
                f"HTTP {resp.status_code}: {resp.text[:500]}",
                code=502,
            )
        return jsonify(resp.json())
    except Exception as e:
        traceback.print_exc()
        return error_response("Reactor token exchange error", str(e), code=502)


@app.route('/api/reactor/health', methods=['GET'])
def api_reactor_health():
    """Can realtime actually work right now, and if not, why?

    /api/reactor/config only reports whether a key is *set*. That is not the
    same question: the key can be present and wrong, expired, or rate-limited,
    and the only symptom a player gets is the stills fallback with no
    explanation. This actually mints a token, so "realtime unavailable" comes
    with a reason instead of a shrug.

    Never raises, and never returns the key or the token itself.
    """
    keys_store.has_provider_key("reactor")
    api_key = os.getenv("REACTOR_API_KEY")
    if not api_key:
        return jsonify({
            "ok": False, "configured": False, "reason": "no_api_key",
            "detail": "REACTOR_API_KEY is not set on the server.",
        })
    try:
        import requests
        resp = requests.post(
            REACTOR_TOKEN_URL, headers={"Reactor-API-Key": api_key}, timeout=10,
        )
        if resp.status_code == 200:
            return jsonify({"ok": True, "configured": True, "reason": "ready"})
        reason = "rate_limited" if resp.status_code == 429 else (
            "bad_api_key" if resp.status_code in (401, 403) else "token_exchange_failed"
        )
        return jsonify({
            "ok": False, "configured": True, "reason": reason,
            "detail": f"HTTP {resp.status_code}: {resp.text[:200]}",
        })
    except Exception as e:
        return jsonify({
            "ok": False, "configured": True, "reason": "unreachable", "detail": str(e)[:200],
        })


@app.route('/api/reactor/usage', methods=['POST'])
def api_reactor_usage():
    """Client-reported connected-seconds for a realtime Reactor session.

    Reactor's video stream runs browser<->Reactor directly over WebRTC — the
    server never sees a per-frame call to bill from, unlike every other
    provider in cost_tracker. `reactor_renderer.js` tracks how long a session
    was actually connected and reports it here (via sendBeacon) whenever that
    session ends: on model swap, disable, or page unload. Fire-and-forget:
    always 200, never blocks or breaks the client on failure.
    """
    blocked = _spend_blocked(min_usd=0.25)
    if blocked:
        return blocked
    try:
        data = request.get_json(silent=True) or {}
        session_id = str(data.get("session_id") or "default").strip() or "default"
        model = str(data.get("model") or "default").strip() or "default"
        try:
            seconds = float(data.get("duration_seconds") or 0)
        except (TypeError, ValueError):
            seconds = 0.0
        if seconds > 0:
            import cost_tracker
            cost_tracker.record_usage(
                session_id, "video", "reactor", model,
                output_units=seconds, unit_type="seconds", success=True,
            )
        return jsonify({"ok": True})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": True, "error": str(e)})

# ═══════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════

def success_response(data, message="Success"):
    """Standard success response format"""
    return {
        "success": True,
        "message": message,
        "data": data
    }

def error_response(message, details=None, code=500):
    """Standard error response format"""
    response = {
        "success": False,
        "error": message
    }
    if details:
        response["details"] = str(details)
    return jsonify(response), code

# ═══════════════════════════════════════════════════════════════════
# ARCHIVE ENDPOINTS
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/archives', methods=['GET'])
def api_list_archives():
    """
    List all archived game sessions.
    Returns: JSON array of archive metadata sorted by date (newest first)
    """
    try:
        archives_root = Path("archives")
        if not archives_root.exists():
            return jsonify(success_response([], "No archives found"))
        
        archives = []
        for archive_dir in sorted(archives_root.iterdir(), reverse=True):
            if not archive_dir.is_dir():
                continue
            
            metadata_file = archive_dir / "archive_metadata.json"
            if metadata_file.exists():
                metadata = json.loads(metadata_file.read_text())
                metadata["archive_name"] = archive_dir.name
                archives.append(metadata)
            else:
                # Archive without metadata - create basic info
                archives.append({
                    "archive_name": archive_dir.name,
                    "session_id": "unknown",
                    "archive_timestamp": archive_dir.name.split('_')[-2:] if '_' in archive_dir.name else "unknown",
                    "archive_reason": "unknown"
                })
        
        return jsonify(success_response(archives, f"Found {len(archives)} archives"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to list archives", str(e))


@app.route('/api/archives/<archive_name>', methods=['GET'])
def api_get_archive(archive_name):
    """
    Get detailed information about a specific archive.
    Returns: Full archive metadata, state, and history
    """
    try:
        archive_path = Path("archives") / archive_name
        if not archive_path.exists():
            return error_response(f"Archive '{archive_name}' not found", code=404)
        
        # Load metadata
        metadata_file = archive_path / "archive_metadata.json"
        metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
        
        # Load state
        state_file = archive_path / "state.json"
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
        
        # Load history
        history_file = archive_path / "history.json"
        history = json.loads(history_file.read_text()) if history_file.exists() else []
        
        # Count assets
        images_dir = archive_path / "images"
        tapes_dir = archive_path / "tapes"
        
        asset_counts = {
            "images": len(list(images_dir.glob("*.png"))) if images_dir.exists() else 0,
            "tapes": len(list(tapes_dir.glob("*.gif"))) if tapes_dir.exists() else 0
        }
        
        return jsonify(success_response({
            "metadata": metadata,
            "state": state,
            "history": history,
            "asset_counts": asset_counts,
            "archive_path": str(archive_path)
        }, f"Archive '{archive_name}' details"))
        
    except Exception as e:
        traceback.print_exc()
        return error_response(f"Failed to get archive '{archive_name}'", str(e))


@app.route('/api/archives/<archive_name>/images/<filename>', methods=['GET'])
def api_serve_archive_image(archive_name, filename):
    """Serve an image from an archived session"""
    try:
        # Prevent path traversal
        safe_filename = Path(filename).name
        image_path = Path("archives") / archive_name / "images" / safe_filename
        
        if not image_path.exists():
            return error_response("Image not found", code=404)
        
        return send_file(str(image_path), mimetype='image/png')
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to serve image", str(e))


@app.route('/api/archives/<archive_name>/tapes/<filename>', methods=['GET'])
def api_serve_archive_tape(archive_name, filename):
    """Serve a GIF tape from an archived session"""
    try:
        # Prevent path traversal
        safe_filename = Path(filename).name
        tape_path = Path("archives") / archive_name / "images" / safe_filename
        
        if not tape_path.exists():
            return error_response("Tape not found", code=404)
        
        return send_file(str(tape_path), mimetype='image/gif')
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to serve tape", str(e))


@app.route('/api/archives/<archive_name>', methods=['DELETE'])
def api_delete_archive(archive_name):
    """
    Delete an archived session permanently.
    WARNING: This cannot be undone!
    """
    try:
        archive_path = Path("archives") / archive_name
        if not archive_path.exists():
            return error_response(f"Archive '{archive_name}' not found", code=404)
        
        import shutil
        shutil.rmtree(archive_path)
        
        return jsonify(success_response({}, f"Archive '{archive_name}' deleted permanently"))
    except Exception as e:
        traceback.print_exc()
        return error_response(f"Failed to delete archive '{archive_name}'", str(e))


# ═══════════════════════════════════════════════════════════════════
# SESSION ENDPOINTS
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/sessions', methods=['POST'])
def api_create_session():
    """
    Create a new game session.
    Body: { "session_id": "optional_custom_id" }
    Returns: Session metadata
    """
    try:
        data = request.json or {}
        session_id = data.get('session_id')
        
        # Generate UUID if not provided
        if not session_id:
            import uuid
            session_id = str(uuid.uuid4())[:8]
        
        # Create session metadata
        metadata = engine._create_session_metadata(session_id)
        
        return jsonify(success_response(metadata, f"Session '{session_id}' created"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to create session", str(e))


@app.route('/api/sessions', methods=['GET'])
def api_list_sessions():
    """
    List all active game sessions.
    Returns: JSON array of session metadata
    """
    try:
        sessions_root = Path("sessions")
        if not sessions_root.exists():
            return jsonify(success_response([], "No sessions found"))
        
        sessions = []
        for session_dir in sessions_root.iterdir():
            if not session_dir.is_dir():
                continue
            
            session_id = session_dir.name
            try:
                metadata = engine._load_session_metadata(session_id)
                sessions.append(metadata)
            except:
                # Session without metadata - create basic info
                sessions.append({
                    "session_id": session_id,
                    "created_at": "unknown",
                    "last_accessed": "unknown"
                })
        
        return jsonify(success_response(sessions, f"Found {len(sessions)} sessions"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to list sessions", str(e))


@app.route('/api/sessions/<session_id>', methods=['GET'])
def api_get_session(session_id):
    """
    Get detailed information about a specific session.
    Returns: Session metadata, state, and history summary
    """
    try:
        metadata = engine._load_session_metadata(session_id)
        state = engine.get_state(session_id)
        history = engine._load_history(session_id)
        
        return jsonify(success_response({
            "metadata": metadata,
            "state": state,
            "history_length": len(history),
            "last_turn": history[-1] if history else None
        }, f"Session '{session_id}' details"))
    except FileNotFoundError:
        return error_response(f"Session '{session_id}' not found", code=404)
    except Exception as e:
        traceback.print_exc()
        return error_response(f"Failed to get session '{session_id}'", str(e))


@app.route('/api/sessions/<session_id>/status', methods=['GET'])
def api_get_session_status(session_id):
    """
    Get quick status of a session (lightweight endpoint).
    Returns: Basic session info without full history
    """
    try:
        state = engine.get_state(session_id)
        metadata = engine._load_session_metadata(session_id)
        
        return jsonify(success_response({
            "session_id": session_id,
            "turn_count": state.get('turn_count', 0),
            "player_alive": state.get('player_alive', True),
            "location": state.get('location', 'unknown'),
            "last_accessed": metadata.get('last_accessed', 'unknown')
        }, f"Session '{session_id}' status"))
    except FileNotFoundError:
        return error_response(f"Session '{session_id}' not found", code=404)
    except Exception as e:
        traceback.print_exc()
        return error_response(f"Failed to get session status", str(e))


@app.route('/api/sessions/<session_id>', methods=['DELETE'])
def api_delete_session(session_id):
    """
    Delete a session and all its data.
    Query params: ?archive=true (default) to archive before deletion
    """
    try:
        archive_first = request.args.get('archive', 'true').lower() == 'true'
        
        success = engine.delete_session(session_id, archive_first=archive_first)
        
        if success:
            message = f"Session '{session_id}' deleted"
            if archive_first:
                message += " (archived first)"
            return jsonify(success_response({}, message))
        else:
            return error_response(f"Failed to delete session '{session_id}'")
    except Exception as e:
        traceback.print_exc()
        return error_response(f"Failed to delete session '{session_id}'", str(e))


@app.route('/api/sessions/<session_id>/history', methods=['GET'])
def api_get_session_history(session_id):
    """
    Get detailed history for a specific session with pagination.
    Query params: ?limit=10&offset=0
    Returns: JSON array of history entries
    """
    try:
        limit = request.args.get('limit', type=int)
        offset = request.args.get('offset', type=int)
        
        history = engine._load_history(session_id)
        
        total_entries = len(history)
        
        if offset is not None and limit is not None:
            history = history[offset:offset + limit]
        elif limit is not None:
            history = history[:limit]
        
        return jsonify(success_response({
            "total_entries": total_entries,
            "returned_entries": len(history),
            "history": history
        }, f"History for session '{session_id}'"))
    except FileNotFoundError:
        return error_response(f"Session '{session_id}' not found", code=404)
    except Exception as e:
        traceback.print_exc()
        return error_response(f"Failed to get history for session '{session_id}'", str(e))


# ═══════════════════════════════════════════════════════════════════
# LOBBY / MULTI-USER FRAMEWORK ENDPOINTS
#
# These wrap the lower-level session APIs above with lobby-oriented shapes:
# the create endpoint mints a URL-safe id if none is supplied, and the list
# endpoint returns just what the splash page needs (id, name, turn count,
# last-accessed timestamp, alive/dead status) so the client can render the
# "Continue" panel without pulling full state per row.
# ═══════════════════════════════════════════════════════════════════

def _mint_session_id() -> str:
    """Short, URL-safe id used when the caller doesn't specify one. Uses a
    base32-ish alphabet without look-alike characters (0/O, 1/I/l) so the
    id is safe to read aloud, type on mobile, or share via chat."""
    import secrets
    alphabet = "23456789abcdefghjkmnpqrstuvwxyz"  # 31 chars, no lookalikes
    return "".join(secrets.choice(alphabet) for _ in range(8))


@app.route('/api/lobby/create', methods=['POST'])
def api_lobby_create():
    """Create a fresh game instance and return the resolved session id.

    Body (all optional):
        { "session_id": "custom-slug", "name": "My Run", "description": "..." }

    If session_id is omitted (or is 'default'/blank), we mint a short
    URL-safe id. If the requested id already exists, we return the existing
    session's metadata rather than error out — a resume-by-id link should
    always land the caller in something playable."""
    try:
        data = request.get_json(silent=True) or {}
        requested = (data.get('session_id') or '').strip()
        name = (data.get('name') or '').strip() or None
        description = (data.get('description') or '').strip() or None

        # Never let the lobby overwrite the shared 'default' slot; the
        # lobby always mints a private id for a new instance.
        if not requested or requested.lower() == 'default':
            session_id = _mint_session_id()
            # Vanishingly unlikely, but guard against a mint collision so
            # we don't clobber an existing run.
            attempts = 0
            while (Path("sessions") / session_id).exists() and attempts < 5:
                session_id = _mint_session_id()
                attempts += 1
        else:
            # Client-supplied id: validate against the engine's rules.
            try:
                engine._validate_session_id(requested)
            except Exception as e_val:
                return error_response(f"Invalid session id: {e_val}", code=400)
            session_id = requested

        session_root = Path("sessions") / session_id
        already_exists = session_root.exists()

        # _create_session_metadata (used by engine) writes the initial meta
        # file. For an existing session we just refresh metadata + return
        # what we have so "create with an existing id" degrades to "join".
        if already_exists:
            try:
                metadata = engine._load_session_metadata(session_id)
                if name:
                    metadata = engine._update_session_metadata(session_id, name=name)
            except Exception:
                metadata = engine._create_session_metadata(session_id, name=name, description=description)
        else:
            metadata = engine._create_session_metadata(session_id, name=name, description=description)

        return jsonify(success_response({
            "session_id": session_id,
            "metadata": metadata,
            "already_existed": already_exists,
            "play_url": f"/play?session={session_id}",
        }, f"Session '{session_id}' ready"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to create session", str(e))


@app.route('/api/lobby/sessions', methods=['GET'])
def api_lobby_sessions():
    """List runs the lobby's "Continue" panel can offer.

    Query params:
        ?limit=N (default 25) - cap the number of rows returned
        ?include_default=false - hide the shared 'default' slot

    Returns a compact per-session shape (id, name, turn_count, last_accessed,
    player_alive) rather than the full state blob — the splash renders many
    rows and can tolerate a slightly stale count for the trade-off of a
    fast, cache-friendly list call."""
    try:
        limit = request.args.get('limit', default=25, type=int)
        include_default = request.args.get('include_default', 'true').lower() != 'false'

        sessions_root = Path("sessions")
        if not sessions_root.exists():
            return jsonify(success_response({"sessions": []}, "No sessions yet"))

        rows = []
        for session_dir in sessions_root.iterdir():
            if not session_dir.is_dir():
                continue
            sid = session_dir.name
            if sid == '__pycache__':
                continue
            if not include_default and sid == 'default':
                continue
            try:
                meta = engine._load_session_metadata(sid, create_if_missing=False)
            except FileNotFoundError:
                # Session directory without metadata — surface a bare row so
                # the user can still see + resume it.
                meta = {"session_id": sid, "name": f"Run {sid}", "turn_count": 0, "player_alive": True}
            except Exception:
                continue

            rows.append({
                "session_id": sid,
                "name": meta.get("name") or f"Run {sid}",
                "description": meta.get("description") or "",
                "turn_count": int(meta.get("turn_count", 0) or 0),
                "player_alive": bool(meta.get("player_alive", True)),
                "created_at": meta.get("created_at") or "",
                "last_accessed": meta.get("last_accessed") or meta.get("created_at") or "",
            })

        rows.sort(key=lambda r: r.get("last_accessed", ""), reverse=True)
        if limit and limit > 0:
            rows = rows[:limit]

        return jsonify(success_response({"sessions": rows}, f"{len(rows)} sessions"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to list sessions", str(e))


@app.route('/api/lobby/sessions/<session_id>', methods=['GET'])
def api_lobby_session_status(session_id):
    """Quick per-session status probe used by the lobby before it hands off
    to /play — verifies the session actually exists and returns just enough
    metadata to render the resume card."""
    try:
        sanitized = engine._sanitize_session_id(session_id)
        session_root = Path("sessions") / sanitized
        if not session_root.exists() and sanitized != 'default':
            return error_response(f"Session '{sanitized}' not found", code=404)
        try:
            meta = engine._load_session_metadata(sanitized, create_if_missing=False)
        except FileNotFoundError:
            return error_response(f"Session '{sanitized}' not found", code=404)
        return jsonify(success_response({
            "session_id": sanitized,
            "metadata": meta,
            "play_url": f"/play?session={sanitized}",
        }, "ok"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to fetch session", str(e))


# ═══════════════════════════════════════════════════════════════════
# PRESENCE — who else is on THIS run right now
#
# Scoped per session on purpose: main gives each visitor their own persisted
# instance, so a global headcount would tell someone alone in a private run
# that four people are watching. See presence.py.
# ═══════════════════════════════════════════════════════════════════

def _presence_session_id():
    raw = (request.get_json(silent=True) or {}).get('session_id') \
        or request.args.get('session_id') or request.args.get('session') \
        or request.headers.get('X-Session-Id') or 'default'
    try:
        return engine._sanitize_session_id(str(raw))
    except Exception:
        return 'default'


@app.route('/api/lobby/heartbeat', methods=['POST'])
def api_lobby_heartbeat():
    """"Still here." Returns the run's presence snapshot in the same call, so
    the widget never needs a second round trip."""
    try:
        import presence
        data = request.get_json(silent=True) or {}
        snap = presence.touch(
            _presence_session_id(),
            str(data.get('viewer_id') or ''),
            label=data.get('label'),
            active=bool(data.get('active')),
        )
        return jsonify(snap)
    except Exception as e:
        # Presence is decoration; it must never take a turn down with it.
        traceback.print_exc()
        return jsonify({"count": 0, "active_count": 0, "viewers": [], "you": None, "error": str(e)})


@app.route('/api/lobby/leave', methods=['POST'])
def api_lobby_leave():
    """Tab closed. Sent via sendBeacon, so it must always 200 and never block."""
    try:
        import presence
        data = request.get_json(silent=True) or {}
        presence.leave(_presence_session_id(), str(data.get('viewer_id') or ''))
    except Exception:
        traceback.print_exc()
    return jsonify({"ok": True})


@app.route('/api/lobby/presence', methods=['GET'])
def api_lobby_presence():
    """Read-only presence for a run, for anything that wants the headcount
    without claiming to be a viewer."""
    try:
        import presence
        return jsonify(presence.snapshot(
            _presence_session_id(), request.args.get('viewer_id')))
    except Exception as e:
        traceback.print_exc()
        return jsonify({"count": 0, "active_count": 0, "viewers": [], "you": None, "error": str(e)})


# ═══════════════════════════════════════════════════════════════════
# ASSET SERVING ENDPOINTS
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/sessions/<session_id>/images/<filename>', methods=['GET'])
def api_serve_session_image(session_id, filename):
    """Serve an image from a specific session"""
    try:
        # Prevent path traversal by using only the base filename
        safe_filename = Path(filename).name
        image_path = Path("sessions") / session_id / "images" / safe_filename
        
        if not image_path.exists():
            return error_response("Image not found", code=404)
        
        return send_file(str(image_path), mimetype='image/png')
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to serve image", str(e))


@app.route('/api/sessions/<session_id>/tapes/<filename>', methods=['GET'])
def api_serve_session_tape(session_id, filename):
    """Serve a GIF tape from a specific session"""
    try:
        # Prevent path traversal
        safe_filename = Path(filename).name
        tape_path = Path("sessions") / session_id / "tapes" / safe_filename
        
        if not tape_path.exists():
            return error_response("Tape not found", code=404)
        
        return send_file(str(tape_path), mimetype='image/gif')
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to serve tape", str(e))


@app.route('/api/sessions/<session_id>/videos/<filename>', methods=['GET'])
def api_serve_session_video(session_id, filename):
    """Serve a video file from a specific session"""
    try:
        # Prevent path traversal
        safe_filename = Path(filename).name
        video_path = Path("sessions") / session_id / "films" / safe_filename
        
        if not video_path.exists():
            return error_response("Video not found", code=404)
        
        return send_file(str(video_path), mimetype='video/mp4')
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to serve video", str(e))


# ═══════════════════════════════════════════════════════════════════
# GAME STATE ENDPOINTS
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/state', methods=['GET'])
def api_get_state():
    """
    Get current game state.
    Query params: ?session_id=default
    
    Returns:
        JSON with current state
    """
    try:
        session_id = request.args.get('session_id', 'default')
        state = engine.get_state(session_id)
        return jsonify(success_response(state, "State retrieved"))
    except Exception as e:
        return error_response("Failed to get state", str(e))


@app.route('/api/state/save', methods=['POST'])
def api_save_state():
    """
    Save game state to disk.
    Body: { "session_id": "default", "state": {...} }
    
    Returns:
        JSON confirmation
    """
    try:
        data = request.json or {}
        session_id = data.get('session_id', 'default')
        state = data.get('state', {})
        
        if not state:
            return error_response("No state provided", code=400)
        
        engine._save_state(state, session_id)
        return jsonify(success_response({"saved": True}, "State saved successfully"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to save state", str(e))


@app.route('/api/state/reset', methods=['POST'])
def api_reset_state():
    """
    Reset game state to initial conditions.
    Body: { "session_id": "default" }
    
    Returns:
        JSON confirmation
    """
    try:
        data = request.json or {}
        session_id = data.get('session_id', 'default')
        engine.reset_state(session_id)
        return jsonify(success_response({}, f"State reset for session '{session_id}'"))
    except Exception as e:
        return error_response("Failed to reset state", str(e))


@app.route('/api/history', methods=['GET'])
def api_get_history():
    """
    Get game history (all turns).
    Query params: ?session_id=default
    
    Returns:
        JSON with history array
    """
    try:
        session_id = request.args.get('session_id', 'default')
        history = engine._load_history(session_id)
        return jsonify(success_response({
            "history": history,
            "length": len(history)
        }, "History retrieved"))
    except Exception as e:
        return error_response("Failed to get history", str(e))


# ═══════════════════════════════════════════════════════════════════
# GAME FLOW ENDPOINTS
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/game/intro', methods=['POST'])
def api_generate_intro():
    """
    Generate intro image and prologue (Phase 1).
    Body: { "session_id": "default" }
    
    Returns:
        JSON with image_url, prologue, vision_dispatch, dispatch
    """
    try:
        data = request.json or {}
        session_id = data.get('session_id', 'default')
        result = engine.generate_intro_image_fast(session_id)
        return jsonify(success_response(result, "Intro generated"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to generate intro", str(e))


@app.route('/api/game/intro/choices', methods=['POST'])
def api_generate_intro_choices():
    """
    Generate intro choices (Phase 2).
    Body: { "image_url": "...", "prologue": "...", "vision_dispatch": "...", "dispatch": "...", "session_id": "default" }
    
    Returns:
        JSON with choices array
    """
    try:
        data = request.json
        image_url = data.get('image_url')
        prologue = data.get('prologue')
        vision_dispatch = data.get('vision_dispatch')
        dispatch = data.get('dispatch')
        session_id = data.get('session_id', 'default')
        
        result = engine.generate_intro_choices_deferred(
            image_url, prologue, vision_dispatch, dispatch, session_id
        )
        return jsonify(success_response(result, "Intro choices generated"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to generate intro choices", str(e))


@app.route('/api/game/action/image', methods=['POST'])
def api_advance_turn_image():
    """
    Advance turn - generate consequence image (Phase 1).
    Body: { "choice_index": 0, "custom_action": null, "session_id": "default" }
    
    Returns:
        JSON with consequence_img_url, consequence_summary
    """
    try:
        data = request.json
        choice_index = data.get('choice_index')
        custom_action = data.get('custom_action')
        session_id = data.get('session_id', 'default')
        
        result = engine.advance_turn_image_fast(choice_index, custom_action, session_id)
        return jsonify(success_response(result, "Turn image generated"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to generate turn image", str(e))


@app.route('/api/game/action/choices', methods=['POST'])
def api_advance_turn_choices():
    """
    Advance turn - generate new choices (Phase 2).
    Body: { "consequence_img_url": "...", "consequence_summary": "...", "session_id": "default" }
    
    Returns:
        JSON with choices array and updated state
    """
    try:
        data = request.json
        consequence_img_url = data.get('consequence_img_url')
        consequence_summary = data.get('consequence_summary')
        session_id = data.get('session_id', 'default')
        
        result = engine.advance_turn_choices_deferred(
            consequence_img_url, consequence_summary, session_id
        )
        return jsonify(success_response(result, "Turn choices generated"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to generate turn choices", str(e))


# ═══════════════════════════════════════════════════════════════════
# ADMIN DASHBOARD
# ═══════════════════════════════════════════════════════════════════

def _admin_token_ok():
    """
    Validate the admin token from query string, header, or cookie.

    Local desktop (no hosted billing) may leave ADMIN_TOKEN unset and keep
    the dashboard open. Hosted billing cannot: an unset token would expose
    sessions, pricing, and reset on a paying service.
    """
    expected = os.getenv('ADMIN_TOKEN')
    if not expected:
        if os.getenv("FEATURE_BILLING", "").strip().lower() in ("1", "true", "on", "yes"):
            return False
        return True
    from flask import request
    provided = (
        request.args.get('token')
        or request.headers.get('X-Admin-Token')
        or request.cookies.get('admin_token')
    )
    return provided is not None and provided == expected


@app.route('/admin', methods=['GET'])
def serve_admin_dashboard():
    """Serve the admin dashboard with cross-origin support"""
    if not _admin_token_ok():
        return jsonify({
            "error": "unauthorized",
            "message": "Provide ADMIN_TOKEN via ?token=, X-Admin-Token header, or admin_token cookie."
        }), 401
    try:
        response = make_response(send_file('admin_dashboard.html'))
        # Pin Access-Control-Allow-Origin to the request origin (or omit it)
        # rather than '*' so credentials/cookies still work for the protected
        # variant and we don't broadcast the dashboard to every origin.
        from flask import request
        origin = request.headers.get('Origin')
        if origin:
            response.headers['Access-Control-Allow-Origin'] = origin
            response.headers['Vary'] = 'Origin'
            response.headers['Access-Control-Allow-Credentials'] = 'true'
        return response
    except FileNotFoundError:
        return jsonify({"error": "Dashboard file not found"}), 404


@app.route('/api/admin/reset', methods=['POST'])
def admin_reset_session():
    """
    Emergency-reset the engine state for a session.

    Useful when the Discord UI is stuck (e.g. the bot resumed into a state
    with no choices and players have nothing to click). Guarded by
    ADMIN_TOKEN — the same token the dashboard uses. The session id can be
    passed as `?session=<id>` (defaults to `default`).

    Example:
        curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" \
            "https://<host>/api/admin/reset?session=default"
    """
    if not _admin_token_ok():
        return jsonify({
            "error": "unauthorized",
            "message": "Provide ADMIN_TOKEN via ?token=, X-Admin-Token header, or admin_token cookie."
        }), 401

    from flask import request
    session_id = (
        request.args.get('session')
        or (request.get_json(silent=True) or {}).get('session')
        or 'default'
    )
    try:
        import engine as _engine
        _engine.reset_state(session_id)
        return jsonify({"status": "ok", "session": session_id, "action": "reset"})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            "status": "error",
            "session": session_id,
            "error": str(e),
        }), 500


# ═══════════════════════════════════════════════════════════════════
# DYNAMIC VOICES (admin)
#
# Read-only snapshot of the voice_design cache + workspace slot usage, and
# a manual sweep trigger. Same ADMIN_TOKEN guard as the rest of /api/admin/*.
# See voice_design.py + DYNAMIC_VOICES_PLAN.md.
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/admin/voices', methods=['GET'])
def admin_voices_snapshot():
    if not _admin_token_ok():
        return jsonify({
            "error": "unauthorized",
            "message": "Provide ADMIN_TOKEN via ?token=, X-Admin-Token header, or admin_token cookie."
        }), 401
    try:
        import voice_design as _vd
        return jsonify(_vd.cache_snapshot())
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e), "enabled": False, "entries": []}), 500


@app.route('/api/admin/voices/sweep', methods=['POST'])
def admin_voices_sweep():
    if not _admin_token_ok():
        return jsonify({
            "error": "unauthorized",
            "message": "Provide ADMIN_TOKEN via ?token=, X-Admin-Token header, or admin_token cookie."
        }), 401
    try:
        import voice_design as _vd
        active = _list_active_sessions()
        result = _vd.sweep_orphans(active_session_ids=active)
        return jsonify({"ok": True, "active_sessions": active, "result": result})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


def _list_active_sessions():
    """Best-effort list of session-ids currently on disk. Used by the voice
    sweeper to decide which designed voices belong to a live story vs a
    session that's been reset/deleted."""
    try:
        sessions_dir = Path(__file__).parent / "sessions"
        if not sessions_dir.exists():
            return []
        return [p.name for p in sessions_dir.iterdir() if p.is_dir()]
    except Exception:
        return []


# Start the periodic voice-design sweep at import time so orphans left by a
# crashed prior process are reaped shortly after boot (and every SWEEP_HOURS
# thereafter). Guarded by voice_design.is_available() — a no-op when no
# ElevenLabs key is configured, which matches the rest of the module.
try:
    import voice_design as _voice_design
    # Loud, easy-to-grep startup line: makes it trivial to tell from prod
    # logs whether a "voices sound the same" report is (a) the feature
    # sitting disabled (missing key / flag off), (b) the fallback playing
    # because a design is still generating, or (c) something else.
    if _voice_design.is_available():
        print(
            "[VOICE DESIGN] ENABLED — model={m} budget/session={b} "
            "concurrency={c} label_tag={t}".format(
                m=_voice_design.TTV_MODEL,
                b=_voice_design.DESIGN_BUDGET_PER_SESSION,
                c=_voice_design.DESIGN_CONCURRENCY,
                t=_voice_design.LABEL_TAG,
            )
        )
        _voice_design.start_periodic_sweep(active_sessions_getter=_list_active_sessions)
    else:
        _reason = (
            "no ELEVENLABS_API_KEY" if not _voice_design._api_key()
            else "ELEVENLABS_DYNAMIC_VOICES=0"
        )
        print(f"[VOICE DESIGN] DISABLED ({_reason}) — TALK will use the "
              f"static by_kind roster only")
except Exception as _e:  # noqa: BLE001
    print(f"[VOICE DESIGN] init failed: {_e}")


# ═══════════════════════════════════════════════════════════════════
# AI PROVIDER SWITCHING (admin)
#
# Powers the drag-and-drop model switcher in the admin dashboard. Reads and
# writes the same ai_config.json / preset system that the Discord /ai_switch
# command uses, so a change here takes effect on the very next turn without a
# redeploy. Guarded by the same ADMIN_TOKEN as the rest of the dashboard.
# ═══════════════════════════════════════════════════════════════════

# Friendly display metadata for the presets, so the UI can show a human label
# and an at-a-glance latency badge without hardcoding it in the HTML. Keyed by
# preset name; unknown presets fall back to sensible defaults derived from the
# preset config itself.
# Play / Watch / Editor / admin image pickers only offer these. Text-only
# presets (anthropic, openai) stay in available_configs for narrator switching
# but must not appear as image generators.
_IMAGE_PRESET_NAMES = ("gemini_pro", "gemini", "krea")

_PRESET_UI_META = {
    "gemini_pro": {"label": "Gemini Pro", "latency": "~15-30s", "speed": 2,
                   "blurb": "Highest fidelity. Slow."},
    "gemini": {"label": "Gemini Fast", "latency": "~3-4s", "speed": 4,
               "blurb": "Seconds per frame. The default."},
    "krea": {"label": "Krea", "latency": "~12s", "speed": 3,
             "blurb": "Style-transfer continuity. Needs KREA_API_KEY."},
}


def _preset_matches_current(preset_cfg, current):
    """An image preset is active when its stills match. Text is chosen
    separately — switching Gemini Fast / Pro / Krea must not demand the
    narrator still be Gemini."""
    return (
        preset_cfg.get("image_provider") == current.get("image_provider")
        and preset_cfg.get("image_model") == current.get("image_model")
    )


def _ai_config_payload():
    """Build the live AI configuration + all presets (with friendly labels /
    latency / speed metadata) shared by the admin dashboard switcher AND the
    in-game (standalone / realtime) model menu."""
    config = ai_provider_manager.load_ai_config()
    current = {
        "text_provider": config.get("text_provider"),
        "text_model": config.get("text_model"),
        "image_provider": config.get("image_provider"),
        "image_model": config.get("image_model"),
        "last_updated": config.get("last_updated"),
    }
    # Live $/image for each preset, pulled straight from pricing.json — so the
    # picker shows real running cost right next to speed/quality instead of
    # making someone cross-reference the Cost Analytics tab.
    try:
        import pricing as _pricing
    except Exception:
        _pricing = None

    offered_ids = {e["id"] for e in ai_provider_manager.available_model_catalogue("image")}
    all_presets = ai_provider_manager.get_available_presets()
    presets = []
    active_name = None
    for name in _IMAGE_PRESET_NAMES:
        cfg = all_presets.get(name)
        if not cfg:
            continue
        img_provider = cfg.get("image_provider")
        img_model = cfg.get("image_model")
        # Same key-hiding as available_model_catalogue: a missing KREA_API_KEY
        # must not produce a tile that fails on the first frame.
        if img_model not in offered_ids:
            continue
        meta = _PRESET_UI_META.get(name, {})
        is_active = _preset_matches_current(cfg, current)
        if is_active:
            active_name = name
        rate = _pricing.get_rate(img_provider, img_model) if (_pricing and img_provider) else None
        cost_per_image = None
        if rate:
            if rate.get("unit_type") == "images" and rate.get("per_unit") is not None:
                cost_per_image = rate["per_unit"]
            elif rate.get("unit_type") == "seconds" and rate.get("per_unit") is not None:
                # Veo bills by the ~8s clip _gen_image records as one unit.
                cost_per_image = rate["per_unit"] * 8.0
        presets.append({
            "name": name,
            "label": meta.get("label", name.replace("_", " ").title()),
            "latency": meta.get("latency", ""),
            "speed": meta.get("speed", 0),
            "blurb": meta.get("blurb", cfg.get("description", "")),
            "text_provider": cfg.get("text_provider"),
            "text_model": cfg.get("text_model"),
            "image_provider": img_provider,
            "image_model": img_model,
            "cost_per_image": cost_per_image,
            "active": is_active,
        })
    offered_text = {e["id"] for e in ai_provider_manager.available_model_catalogue("text")}
    text_models = []
    for entry in ai_provider_manager.model_catalogue("text"):
        mid = entry.get("id")
        if not mid:
            continue
        text_models.append({
            "id": mid,
            "provider": entry.get("provider"),
            "label": entry.get("label") or mid,
            "note": entry.get("note") or "",
            "available": mid in offered_text,
        })
    return {
        "status": "ok",
        "current": current,
        "active_preset": active_name,
        "presets": presets,
        "text_models": text_models,
    }


def _ai_switch_result(preset):
    """Apply a preset switch. Returns (response_dict, http_status)."""
    if not preset:
        return {"status": "error", "error": "Missing 'preset'."}, 400
    presets = ai_provider_manager.get_available_presets()
    if preset not in presets:
        return {
            "status": "error",
            "error": f"Unknown preset '{preset}'.",
            "available": list(presets.keys()),
        }, 400
    cfg = presets[preset]
    # Image presets used to config.update() the whole block, which reset the
    # narrator to Gemini Flash every time someone picked Krea or Pro stills.
    try:
        settings = ai_provider_manager.apply_models(
            image_model=cfg.get("image_model"),
            image_size=cfg.get("image_size"),
        )
    except ValueError as e:
        return {"status": "error", "error": str(e)}, 400
    return {
        "status": "ok",
        "preset": preset,
        "image_provider": settings.get("image_provider"),
        "image_model": settings.get("image_model"),
        "text_provider": settings.get("text_provider"),
        "text_model": settings.get("text_model"),
    }, 200


@app.route('/api/admin/ai_config', methods=['GET'])
def admin_ai_config():
    """Return the live AI configuration plus all available presets (with
    friendly labels/latency metadata) for the dashboard's model switcher."""
    if not _admin_token_ok():
        return jsonify({
            "error": "unauthorized",
            "message": "Provide ADMIN_TOKEN via ?token=, X-Admin-Token header, or admin_token cookie."
        }), 401
    try:
        return jsonify(_ai_config_payload())
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to load AI config", str(e))


@app.route('/api/admin/ai_switch', methods=['POST'])
def admin_ai_switch():
    """Switch the live AI configuration to a named preset. Takes effect on the
    next turn (config is hot-reloaded), no redeploy needed."""
    if not _admin_token_ok():
        return jsonify({
            "error": "unauthorized",
            "message": "Provide ADMIN_TOKEN via ?token=, X-Admin-Token header, or admin_token cookie."
        }), 401

    body = request.get_json(silent=True) or {}
    preset = body.get('preset') or request.args.get('preset')
    try:
        result, status = _ai_switch_result(preset)
        return jsonify(result), status
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to switch AI preset", str(e))


# ── Public (player-facing) variants ──────────────────────────────────
# The in-game model menu on /standalone and /realtime is player-facing, just
# like the existing live world-model switcher. These endpoints are NOT token
# gated so the menu works for players, mirroring that design. A failed/expensive
# provider always auto-falls back to Gemini in engine._gen_image, so the world
# never goes blank on a bad switch.

@app.route('/api/ai/config', methods=['GET'])
def public_ai_config():
    """Live AI config + presets for the in-game model menu (no auth)."""
    try:
        return jsonify(_ai_config_payload())
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to load AI config", str(e))


@app.route('/api/ai/switch', methods=['POST'])
def public_ai_switch():
    """Switch the image model preset from the in-game menu (no auth).
    Does not change the narrator — use /api/ai/models for text."""
    body = request.get_json(silent=True) or {}
    preset = body.get('preset') or request.args.get('preset')
    try:
        result, status = _ai_switch_result(preset)
        return jsonify(result), status
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to switch AI preset", str(e))


@app.route('/api/ai/models', methods=['POST'])
def public_ai_apply_models():
    """Point text and/or image at a catalogue id without a named preset.

    This is how Claude (or GPT) becomes the narrator while Gemini/Krea
    keep drawing the stills. Providers are derived from the catalogue.
    """
    body = request.get_json(silent=True) or {}
    text_model = (body.get("text_model") or "").strip() or None
    image_model = (body.get("image_model") or "").strip() or None
    image_size = (body.get("image_size") or "").strip() or None
    if not any((text_model, image_model, image_size)):
        return jsonify({"status": "error", "error": "Nothing to apply."}), 400
    try:
        settings = ai_provider_manager.apply_models(
            text_model=text_model,
            image_model=image_model,
            image_size=image_size,
        )
    except ValueError as e:
        return jsonify({"status": "error", "error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to apply models", str(e))
    return jsonify({"status": "ok", **settings})


# ═══════════════════════════════════════════════════════════════════
# RENDER — an offline playtest, started from the game UI
#
# The server plays a full run against itself on whichever models you pick,
# then hands back the frames, the flipbooks and the verdict. See
# render_jobs.py for why a render owns the renderer while it runs.
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/render/options', methods=['GET'])
def api_render_options():
    """Models, sizes, modes and defaults for the render form."""
    try:
        return jsonify(render_jobs.options())
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to load render options", str(e))


@app.route('/api/render/start', methods=['POST'])
def api_render_start():
    """Start a render. The run plays against this server, so it inherits
    whatever the editor currently says about level, character and prompts."""
    body = request.get_json(silent=True) or {}
    try:
        turns = int((body or {}).get("turns") or 40)
    except (TypeError, ValueError):
        turns = 40
    # Rough hold so a 60-turn Pro render cannot start on an empty wallet.
    blocked = _spend_blocked(min_usd=max(0.5, turns * 0.05))
    if blocked:
        return blocked
    try:
        return jsonify(render_jobs.start(body, request.host_url))
    except ValueError as e:
        return error_response("Bad render request", str(e), code=400)
    except RuntimeError as e:
        return error_response("Render already running", str(e), code=409)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to start render", str(e))


@app.route('/api/render/status', methods=['GET'])
def api_render_status():
    """Where the current (or last) render got to."""
    try:
        return jsonify(render_jobs.status())
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to read render status", str(e))


@app.route('/api/render/cancel', methods=['POST'])
def api_render_cancel():
    """Stop the running render. Frames already written stay on disk."""
    try:
        return jsonify(render_jobs.cancel())
    except RuntimeError as e:
        return error_response("Nothing to cancel", str(e), code=409)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to cancel render", str(e))


@app.route('/api/render/live', methods=['POST'])
def api_render_live():
    """Store the browser-recorded live world film for the current run.

    Watch mode drives the Reactor stream in the TV from each still and records
    it (see WatchFilm in standalone.js). The finished clip is uploaded here and
    becomes the run's Film cut. Guarded so it can only write the run this
    browser is (or just was) driving.
    """
    job_id = (request.form.get("job") or "").strip()
    film = request.files.get("film")
    if not job_id or film is None:
        return error_response("Bad live film upload", "missing job or film", code=400)
    try:
        data = film.read()
        result = render_jobs.save_live(job_id, data, request.form.get("timeline"))
        return jsonify(result)
    except ValueError as e:
        return error_response("Live film rejected", str(e), code=400)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to save live film", str(e))


@app.route('/api/render/history', methods=['GET'])
def api_render_history():
    """Every render still on disk, newest first — the browsing list."""
    try:
        return jsonify(render_jobs.history())
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to list renders", str(e))


@app.route('/api/render/run/<run_id>', methods=['GET'])
def api_render_run(run_id):
    """One finished render, turn by turn, for the reviewer."""
    try:
        return jsonify(render_jobs.detail(run_id))
    except (ValueError, FileNotFoundError):
        return error_response("No such render", run_id, code=404)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to read render", str(e))


@app.route('/api/render/download/<run_id>', methods=['GET'])
def api_render_download(run_id):
    """Export a whole render as one zip — frames, flipbooks, summary, transcript."""
    try:
        path = render_jobs.bundle(run_id)
        return send_file(str(path), as_attachment=True, download_name=path.name)
    except (ValueError, FileNotFoundError):
        return error_response("No such render", run_id, code=404)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to bundle render", str(e))


@app.route('/api/render/file/<path:rel>', methods=['GET'])
def api_render_file(rel):
    """Serve a render artifact — a frame, the GIF, a flipbook, the summary.

    `?download=1` forces a save rather than letting the browser display it, so
    the same URL backs both the inline reviewer and the export buttons.
    """
    try:
        path = render_jobs.artifact_path(rel)
        if request.args.get('download'):
            return send_file(str(path), as_attachment=True, download_name=path.name)
        return send_file(str(path))
    except (ValueError, FileNotFoundError):
        return error_response("No such render artifact", rel, code=404)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to read render artifact", str(e))


# ═══════════════════════════════════════════════════════════════════
# COST & USAGE ANALYTICS (admin)
#
# Backs the dashboard's "Analytics" tab: KPI totals, time-series spend,
# a cost-sortable session list, per-session drill-down, provider/model
# breakdown, recent errors, and a CSV export. Every route here is read-only
# and reads from the `usage_events` / `session_cost_rollup` tables that
# cost_tracker.record_usage() writes to from every instrumented provider
# call site. Same ADMIN_TOKEN guard as the rest of /api/admin/*.
# See ADMIN_COST_ANALYTICS_DASHBOARD_PLAN.md for the full design record.
# ═══════════════════════════════════════════════════════════════════

_VALID_RANGES = ("24h", "7d", "30d", "all")


def _admin_unauthorized():
    return jsonify({
        "error": "unauthorized",
        "message": "Provide ADMIN_TOKEN via ?token=, X-Admin-Token header, or admin_token cookie."
    }), 401


def _clean_range(value):
    return value if value in _VALID_RANGES else "7d"


@app.route('/api/admin/analytics/summary', methods=['GET'])
def admin_analytics_summary():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import cost_tracker
        range_key = _clean_range(request.args.get('range', '7d'))
        return jsonify(success_response(cost_tracker.get_summary(range_key)))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to load analytics summary", str(e))


@app.route('/api/admin/analytics/storage_health', methods=['GET'])
def admin_analytics_storage_health():
    """Is the cost ledger actually going to survive the next restart?

    See cost_tracker.get_storage_health() — there's no direct way to ask
    Render "is my disk attached", so this combines a mount-point check with
    whether the oldest ledger row predates this process's own start time.
    """
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import cost_tracker
        return jsonify(success_response(cost_tracker.get_storage_health()))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to load storage health", str(e))


@app.route('/api/admin/analytics/timeseries', methods=['GET'])
def admin_analytics_timeseries():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import cost_tracker
        range_key = _clean_range(request.args.get('range', '7d'))
        granularity = 'hour' if request.args.get('granularity') == 'hour' else 'day'
        return jsonify(success_response(cost_tracker.get_timeseries(range_key, granularity)))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to load analytics timeseries", str(e))


@app.route('/api/admin/analytics/sessions', methods=['GET'])
def admin_analytics_sessions():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import cost_tracker
        sort = request.args.get('sort', 'cost_desc')
        limit = min(max(request.args.get('limit', 50, type=int) or 50, 1), 500)
        offset = max(request.args.get('offset', 0, type=int) or 0, 0)
        return jsonify(success_response(cost_tracker.get_sessions(sort=sort, limit=limit, offset=offset)))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to load session cost list", str(e))


@app.route('/api/admin/analytics/sessions/<session_id>', methods=['GET'])
def admin_analytics_session_detail(session_id):
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import cost_tracker
        limit = min(max(request.args.get('limit', 500, type=int) or 500, 1), 2000)
        return jsonify(success_response(cost_tracker.get_session_detail(session_id, limit=limit)))
    except Exception as e:
        traceback.print_exc()
        return error_response(f"Failed to load cost detail for session '{session_id}'", str(e))


@app.route('/api/admin/analytics/providers', methods=['GET'])
def admin_analytics_providers():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import cost_tracker
        range_key = _clean_range(request.args.get('range', '30d'))
        return jsonify(success_response(cost_tracker.get_providers_breakdown(range_key)))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to load provider breakdown", str(e))


@app.route('/api/admin/analytics/errors', methods=['GET'])
def admin_analytics_errors():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import cost_tracker
        range_key = _clean_range(request.args.get('range', '7d'))
        limit = min(max(request.args.get('limit', 100, type=int) or 100, 1), 500)
        return jsonify(success_response(cost_tracker.get_errors(range_key, limit=limit)))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to load recent errors", str(e))


@app.route('/api/admin/analytics/export.csv', methods=['GET'])
def admin_analytics_export_csv():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import csv
        import io as _io
        import cost_tracker
        range_key = _clean_range(request.args.get('range', '30d'))

        buf = _io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "id", "ts", "session_id", "turn_count", "service_type", "provider", "model",
            "operation", "input_units", "output_units", "unit_type", "cost_usd", "latency_ms",
            "success", "error_message"
        ])
        for row in cost_tracker.iter_events_for_export(range_key):
            writer.writerow([
                row["id"], row["ts"], row["session_id"], row["turn_count"], row["service_type"],
                row["provider"], row["model"], row["operation"], row["input_units"],
                row["output_units"], row["unit_type"], row["cost_usd"], row["latency_ms"],
                row["success"], row["error_message"],
            ])

        response = make_response(buf.getvalue())
        response.headers["Content-Type"] = "text/csv"
        response.headers["Content-Disposition"] = f"attachment; filename=usage_{range_key}.csv"
        return response
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to export usage CSV", str(e))


@app.route('/api/admin/pricing', methods=['GET'])
def admin_pricing_get():
    """Current provider:model rate table for the Analytics > Pricing panel."""
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import pricing
        return jsonify(success_response(pricing.load_pricing(force=True)))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to load pricing", str(e))


@app.route('/api/admin/pricing', methods=['PUT'])
def admin_pricing_put():
    """
    Update one provider:model rate (or the whole table) without a redeploy —
    mirrors the ai_config.json hot-swap pattern used by /api/admin/ai_switch.

    Body: either {"provider": "krea", "model": "krea-2/medium", "rate": {...}}
    to set a single row, or {"rates": {...}} to replace the whole rate map.
    """
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import pricing
        body = request.get_json(silent=True) or {}
        if 'rates' in body and isinstance(body['rates'], dict):
            data = pricing.load_pricing(force=True)
            data = dict(data)
            data['rates'] = body['rates']
            pricing.save_pricing(data)
            return jsonify(success_response(pricing.load_pricing(force=True), "Pricing table replaced"))

        provider = body.get('provider')
        model = body.get('model')
        rate = body.get('rate')
        if not provider or not model or not isinstance(rate, dict):
            return error_response("Body must include 'provider', 'model', and a 'rate' object "
                                   "(or a top-level 'rates' object to replace the whole table).", code=400)
        data = pricing.set_rate(provider, model, rate)
        return jsonify(success_response(data, f"Updated rate for {provider}:{model}"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to update pricing", str(e))


# ═══════════════════════════════════════════════════════════════════
# WORLD STUDIO — spatial editor for every story/narrative/choice/image
# prompt the game actually runs on. Same ADMIN_TOKEN guard and
# success/error response shape as the rest of /api/admin/*. See
# prompts_store.py for the hot-reload + defaults/reset mechanics.
# ═══════════════════════════════════════════════════════════════════

@app.route('/studio', methods=['GET'])
def serve_world_studio():
    """Create lives in the in-game graph. Keep the admin gate."""
    if not _admin_token_ok():
        return jsonify({
            "error": "unauthorized",
            "message": "Provide ADMIN_TOKEN via ?token=, X-Admin-Token header, or admin_token cookie."
        }), 401
    token = (request.args.get("token") or "").strip()
    dest = "/?mode=create"
    if token:
        dest = f"/?mode=create&token={quote(token)}"
    return redirect(dest, code=302)


@app.route('/api/admin/studio/content', methods=['GET'])
def admin_studio_content():
    """Everything the World Studio UI needs in one shot: current + default
    prompts, and the schema that drives grouping, descriptions, and
    placeholder legends."""
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import prompts_store
        import game_identity
        import prompt_layers
        import levels_store
        import experience_store
        game_identity.ensure_spec_keys()
        spec = game_identity.get_spec()
        return jsonify(success_response({
            "prompts": dict(prompts_store.PROMPTS),
            "prompts_defaults": prompts_store.load_defaults(),
            "schema": prompts_store.PROMPT_SCHEMA,
            "groups": prompts_store.GROUP_LABELS,
            # One line per tab, so a tab never opens onto an unlabelled wall of
            # prompt text.
            "group_blurbs": prompts_store.GROUP_BLURBS,
            # Cast & Camera: the structured spec, its form definition, the
            # thumbnails for any uploaded plates, and the exact text it all
            # compiles to (so the editor can show the real prompt, not a guess).
            "identity": spec,
            "identity_schema": game_identity.identity_schema(),
            "identity_defaults": game_identity.default_spec(),
            "identity_preview": game_identity.preview(),
            # The design surface, filed as Engine / Game / Level / Character:
            # which layer owns which prompt, what question each answers, and
            # whether editing it is a contract change or just content. Lets a
            # client lay the editor out without hardcoding the taxonomy.
            "layers": prompt_layers.layer_manifest(),
            "level_keys": levels_store.level_keys(),
            "levels": levels_store.list_levels(),
            "experience": _experience_json(),
        }))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to load World Studio content", str(e))


@app.route('/api/admin/studio/prompts', methods=['PUT'])
def admin_studio_prompts_put():
    """
    Update one prompt field (or several at once) and persist immediately —
    same hot-swap pattern as /api/admin/pricing and /api/admin/ai_switch.

    Body: either {"key": "...", "value": "..."} for a single field, or
    {"data": {"key1": "...", "key2": [...]}} to update several at once.
    Pass {"force": true} to save anyway despite placeholder-validation
    warnings (only relevant for the small set of fields that are run
    through Python's str.format() at request time).
    """
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import prompts_store
        body = request.get_json(silent=True) or {}
        force = bool(body.get('force'))

        if 'data' in body and isinstance(body['data'], dict):
            fields = body['data']
        elif 'key' in body:
            fields = {body['key']: body.get('value')}
        else:
            return error_response(
                "Body must include either {'key','value'} for a single field "
                "or {'data': {...}} to update several at once.", code=400)

        all_warnings = {}
        for key, value in fields.items():
            ok, warnings = prompts_store.validate_prompt_value(key, value)
            if warnings:
                all_warnings[key] = warnings
            if not ok and not force:
                return jsonify({
                    "success": False,
                    "error": f"'{key}' failed placeholder validation.",
                    "warnings": all_warnings,
                }), 400

        data = prompts_store.save_prompts_bulk(fields)
        return jsonify(success_response(
            {"prompts": data, "warnings": all_warnings},
            "Prompt(s) saved" + (" with warnings" if all_warnings else "")
        ))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to save prompt(s)", str(e))


@app.route('/api/admin/studio/prompts/reset', methods=['POST'])
def admin_studio_prompts_reset():
    """Restore one field (or every field) to its factory default.

    Body: {"key": "..."} to reset a single field, or {"all": true} to
    restore the entire prompts file.
    """
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import prompts_store
        body = request.get_json(silent=True) or {}
        if body.get('all'):
            data = prompts_store.reset_all_prompts()
            return jsonify(success_response({"prompts": data}, "All prompts reset to defaults"))
        key = body.get('key')
        if not key:
            return error_response("Body must include 'key' or {'all': true}.", code=400)
        data = prompts_store.reset_prompt_field(key)
        return jsonify(success_response({"prompts": data}, f"'{key}' reset to default"))
    except KeyError as e:
        return error_response(str(e), code=404)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to reset prompt(s)", str(e))


# ═══════════════════════════════════════════════════════════════════
# CAST & CAMERA — who you play as, the level you play it in, and where the
# camera sits. Structured counterpart to the free-text prompts above; stored in
# the same hot-reloaded prompt file so a saved world carries it too.
# See game_identity.py.
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/admin/studio/identity', methods=['GET'])
def admin_studio_identity_get():
    """The cast sheet plus everything it currently compiles to."""
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import game_identity
        game_identity.ensure_spec_keys()
        return jsonify(success_response({
            "identity": game_identity.get_spec(),
            "schema": game_identity.identity_schema(),
            "defaults": game_identity.default_spec(),
            "preview": game_identity.preview(),
        }))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to load the cast sheet", str(e))


@app.route('/api/admin/studio/identity', methods=['PUT'])
def admin_studio_identity_put():
    """Merge a partial cast-sheet update and persist immediately.

    Body: any subset of {"player_character": {...}, "setting_reference": {...},
    "camera_perspective": {...}} — and any subset of each block's fields, so
    the editors can send only what changed. Unknown fields are dropped and
    every value is normalized (see game_identity._normalize), so a malformed
    payload can never wedge a turn.
    """
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import game_identity
        body = request.get_json(silent=True) or {}
        payload = body.get('identity') if isinstance(body.get('identity'), dict) else body
        if not any(k in payload for k in game_identity.SPEC_KEYS):
            return error_response(
                "Body must include at least one of: "
                + ", ".join(game_identity.SPEC_KEYS) + ".", code=400)
        spec = game_identity.save_spec(payload)
        # Play / reset reloads the bound World snapshot. If we only write the
        # live prompt file, the next reset silently throws the sheet away.
        persisted = None
        try:
            import experience_store
            exp = experience_store.get_experience()
            wid = (body.get("world_id") or body.get("persist_world")
                   or exp.get("start_world") or "")
            if wid:
                exp = experience_store.persist_world_snapshot(wid)
                persisted = experience_store.world_by_id(exp, wid) or {}
        except Exception:
            traceback.print_exc()
        return jsonify(success_response(
            {
                "identity": spec,
                "preview": game_identity.preview(),
                "persisted_world": (persisted or {}).get("slug") or "",
            },
            "Cast & camera saved — live on your next turn"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to save the cast sheet", str(e))


@app.route('/api/admin/studio/identity/goal', methods=['POST'])
def admin_studio_identity_goal():
    """Draft the Level sheet's "what you're here for" from the Experience lore.

    The other Level fields can be read off a plate (see the reference upload
    above), but a goal cannot: an image can say what a place looks like, never
    what you came there for. That only exists in the fiction, so this reads the
    Experience bible instead.

    Returns the draft WITHOUT saving it. The author sees it in the field and
    keeps, edits or discards it — a goal quietly written into the sheet would
    change what the opening montage establishes toward with nobody agreeing to it.
    """
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import game_identity
        body = request.get_json(silent=True) or {}
        lore = str(body.get('lore') or '')
        if not lore:
            try:
                import experience_store
                lore = experience_store.lore_brief(str(body.get('slug') or ''))
            except Exception:
                traceback.print_exc()
        goal = game_identity.draft_level_goal(
            lore=lore, world_prompt=str(body.get('world_prompt') or ''))
        if not goal:
            return error_response(
                "Nothing to draft from yet — name the level or write its lore first.",
                code=400)
        return jsonify(success_response({"goal": goal}, "Goal drafted"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to draft a goal", str(e))


@app.route('/api/admin/studio/identity/reset', methods=['POST'])
def admin_studio_identity_reset():
    """Clear the cast sheet back to first person, no character, no level.

    Body may include ``{"block": "player_character"}`` (or ``setting_reference``
    / ``camera_perspective``) to empty just that sheet. Without it, all three
    go. The World Editor's per-pane Clear uses the single-block form.
    """
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import game_identity
        body = request.get_json(silent=True) or {}
        block = body.get('block')
        if block:
            if block not in game_identity.SPEC_KEYS:
                return error_response(
                    "block must be one of: "
                    + ", ".join(game_identity.SPEC_KEYS) + ".", code=400)
            spec = game_identity.clear_block(block)
            label = {
                game_identity.CHARACTER_KEY: "Character",
                game_identity.SETTING_KEY: "Level",
                game_identity.CAMERA_KEY: "Camera",
            }.get(block, block)
            return jsonify(success_response(
                {"identity": spec, "preview": game_identity.preview()},
                f"{label} cleared"))
        spec = game_identity.reset_spec()
        return jsonify(success_response(
            {"identity": spec, "preview": game_identity.preview()},
            "Cast & camera reset to defaults"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to reset the cast sheet", str(e))


@app.route('/api/admin/studio/identity/fill', methods=['POST'])
def admin_studio_identity_fill():
    """Draft the blank fields of a cast/level sheet. The contract's missing door.

    game_identity.apply_image_fill has always been able to do this and nothing
    could reach it: no route called it, so the only way a sheet ever got drafted
    was as a side effect of uploading a reference plate. A world authored in words
    therefore kept every field the schema gained but never filled one, and
    `setting_reference.goal` stayed "" — which is why level_goal() fell back to
    naming a landmark and the opening montage told the player they had come here
    to reach a fence they were already standing at.

    Body: {"block": "setting_reference", "overwrite": false}
      overwrite=false  fill blanks only, keep everything the author wrote
      overwrite=true   redraw the whole sheet from scratch

    Fills from the attached plate where there is one, and from the world's own
    prose for whatever a photograph cannot answer — a picture of a valley cannot
    tell you what the player came here for.
    """
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import game_identity
        body = request.get_json(silent=True) or {}
        block = str(body.get('block') or '').strip()
        if block not in game_identity.image_fillable_blocks():
            return error_response(
                "block must be one of: "
                + ", ".join(game_identity.image_fillable_blocks()) + ".", code=400)
        result = game_identity.apply_image_fill(
            block, overwrite=bool(body.get('overwrite')),
        )
        filled = list((result.get('filled') or {}).keys())
        if filled:
            message = (f"Drafted {len(filled)} field(s) from "
                       f"{result.get('source') or 'the sheet'}: "
                       f"{', '.join(filled)}")
        elif result.get('reason') == 'all_filled':
            message = "Nothing to draft — every field is already written"
        else:
            message = f"Nothing drafted ({result.get('reason') or 'unknown'})"
        return jsonify(success_response(
            {"fill": result,
             "identity": game_identity.get_spec(),
             "preview": game_identity.preview()},
            message))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to draft the sheet", str(e))


@app.route('/api/admin/studio/regenerate', methods=['GET', 'POST'])
def admin_studio_regenerate():
    """Regenerate everything the world has not been told, and report each field.

    GET  — what is currently unauthored, changing nothing.
    POST — draft it. {"overwrite": true} redraws fields that already have text,
           which is what a re-draw means; without it only blanks are filled so
           authored work is never lost. {"block": "..."} limits the pass.

    Editor-wide on purpose. The old contract covered character and level sheets
    that had a plate attached, and nothing else: camera_perspective's lens and
    notes could not be filled by any path (every route was gated on supporting
    images, and a camera has no photograph to read), and music direction was not
    in the identity spec at all so nobody regenerated it. The report is half the
    point — a regeneration you cannot see is indistinguishable from one that did
    not happen, which is how the world stayed hollow through a dozen runs.
    """
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import world_regen
        if request.method == 'GET':
            return jsonify(success_response({"plan": world_regen.plan()},
                                            "Nothing changed"))
        body = request.get_json(silent=True) or {}
        blocks = body.get('block')
        if isinstance(blocks, str):
            blocks = [blocks]
        report = world_regen.regenerate(
            overwrite=bool(body.get('overwrite')), blocks=blocks,
        )
        for line in world_regen.describe(report):
            print(f"[WORLD REGEN] {line}", flush=True)
        count = report.get('filled', 0)
        import game_identity
        return jsonify(success_response(
            {"report": report,
             "lines": world_regen.describe(report),
             "identity": game_identity.get_spec(),
             "preview": game_identity.preview()},
            f"Regenerated {count} field(s)" if count
            else "Nothing to regenerate — the world is fully authored"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to regenerate the world", str(e))


@app.route('/api/admin/studio/reference', methods=['POST'])
def admin_studio_reference_upload():
    """Store an uploaded character sheet / level plate.

    Body: {"image": "data:image/png;base64,...", "kind": "character"|"setting",
    "label": "optional", "attach": true} — `attach` wires the new id and
    drafts the sheet from the plate in one write. The editor must wait for
    that draft before dirtying the World node.
    """
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import game_identity
        body = request.get_json(silent=True) or {}
        kind = (body.get('kind') or 'character').lower()
        slot = {
            'character': game_identity.CHARACTER_KEY,
            'setting': game_identity.SETTING_KEY,
        }.get(kind)
        if not slot:
            return error_response("'kind' must be 'character' or 'setting'.", code=400)

        meta = game_identity.save_reference(body.get('image', ''), kind, body.get('label', ''))

        image_fill = None
        if body.get('attach', True):
            # Plate + drafted text in one write. Attaching first used to dirty
            # the World on leftover Jason copy before vision had read the image.
            fill_from_image = body.get('fill_from_image', body.get('fill_empty', True))
            if fill_from_image:
                image_fill = game_identity.attach_reference_and_fill(
                    slot, meta['id'], overwrite=True)
            else:
                existing = game_identity.get_spec()[slot].get('reference_images', [])
                game_identity.save_spec({
                    slot: {'reference_images': existing + [meta['id']], 'enabled': True},
                })

        return jsonify(success_response({
            "reference": meta,
            "identity": game_identity.get_spec(),
            "preview": game_identity.preview(),
            "image_fill": image_fill,
        }, "Reference image added"))
    except ValueError as e:
        return error_response(str(e), code=400)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to store the reference image", str(e))


@app.route('/api/admin/studio/reference', methods=['DELETE'])
def admin_studio_reference_delete():
    """Delete a reference image and unwire it from whichever slot used it."""
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import game_identity
        body = request.get_json(silent=True) or {}
        ref_id = body.get('id')
        if not ref_id:
            return error_response("Body must include 'id'.", code=400)
        removed = game_identity.delete_reference(ref_id)
        return jsonify(success_response({
            "removed": removed,
            "identity": game_identity.get_spec(),
            "preview": game_identity.preview(),
        }, "Reference image removed" if removed else "Reference image was already gone"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to delete the reference image", str(e))


@app.route('/api/studio/reference/<ref_id>', methods=['GET'])
def serve_studio_reference(ref_id):
    """Serve a stored reference plate.

    Deliberately NOT admin-gated: these are inert user-uploaded images behind
    unguessable ids, and both editors render them in plain <img> tags — the
    in-game World Editor has no token to attach, so gating this would just
    break its thumbnails in production.
    """
    try:
        import game_identity
        path = game_identity.reference_path(ref_id)
        if not path:
            return jsonify({"error": "not_found"}), 404
        return send_file(str(path), max_age=31536000)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to serve the reference image", str(e))


@app.route('/api/camera', methods=['GET'])
def api_camera():
    """The authored camera, as the PLAYING client needs it.

    Not admin-gated, because the audience is the game itself rather than the
    editor: the realtime renderer builds its world with a `perspective` of its
    own and re-steers that world on every movement and nudge between turns, so
    without this the browser was inventing first-person camera language while
    the server sent third-person prompts. Carries no authored prose — just the
    camera and the protagonist's name, both of which are already visible in the
    scene the player is looking at.
    """
    try:
        import game_identity
        return jsonify(success_response({"camera": game_identity.live_camera_contract()}))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to read the camera", str(e))


# ═══════════════════════════════════════════════════════════════════
# WORLDS — named, saveable prompt-sets ("save our world"). Snapshot the
# current live prompts as a named world, list them, load one back (which
# hot-reloads into the running engine), or delete. Backs the in-game WORLD
# EDITOR's Worlds tab. Same ADMIN_TOKEN guard as the rest of /api/admin/*.
# See worlds_store.py.
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/admin/studio/worlds', methods=['GET'])
def admin_studio_worlds_list():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import worlds_store
        return jsonify(success_response({"worlds": worlds_store.list_worlds()}))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to list worlds", str(e))


@app.route('/api/admin/studio/worlds', methods=['POST'])
def admin_studio_worlds_save():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import worlds_store
        body = request.get_json(silent=True) or {}
        name = body.get('name')
        if not name:
            return error_response("Body must include 'name'.", code=400)
        info = worlds_store.save_world(name, body.get('note', ''))
        return jsonify(success_response(
            {"world": info, "worlds": worlds_store.list_worlds()},
            f"Saved world '{info['name']}'"))
    except ValueError as e:
        return error_response(str(e), code=400)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to save world", str(e))


@app.route('/api/admin/studio/worlds/load', methods=['POST'])
def admin_studio_worlds_load():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import worlds_store
        import prompts_store
        body = request.get_json(silent=True) or {}
        slug = body.get('slug') or body.get('name')
        if not slug:
            return error_response("Body must include 'slug'.", code=400)
        info = worlds_store.load_world(slug)
        return jsonify(success_response(
            {"world": info, "prompts": dict(prompts_store.PROMPTS)},
            f"Loaded world '{info['name']}'"))
    except KeyError as e:
        return error_response(str(e), code=404)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to load world", str(e))


@app.route('/api/admin/studio/worlds', methods=['DELETE'])
def admin_studio_worlds_delete():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import worlds_store
        body = request.get_json(silent=True) or {}
        slug = body.get('slug') or body.get('name')
        if not slug:
            return error_response("Body must include 'slug'.", code=400)
        ok = worlds_store.delete_world(slug)
        return jsonify(success_response({"deleted": ok, "worlds": worlds_store.list_worlds()}))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to delete world", str(e))


def _experience_json(exp=None):
    """Experience payload with per-World first-frame URLs stamped on."""
    import experience_store
    import world_frames
    if exp is None:
        exp = experience_store.get_experience()
    out = world_frames.annotate_experience(exp)
    if isinstance(out, dict):
        out = dict(out)
        out["transition_types"] = experience_store.condition_catalog()
    return out


# ═══════════════════════════════════════════════════════════════════
# EXPERIENCE — worlds stitched by transitions
#
# The bubble editor authors one World at a time. This graph says which
# Worlds exist in the designed run, which one Play starts in, and when
# the engine should leave one for another. Stored as
# experiences/<slug>.json. The start-menu picker lists every file and
# POSTs /api/experiences/activate so Play / reset bind to that slug.
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/worlds/<slug>/frame', methods=['GET'])
def api_world_frame(slug):
    """Serve a World's cached first frame. This is the app's load-time still."""
    try:
        import world_frames
        rec = world_frames.record(slug)
        path = rec.get("path")
        if not path or not Path(path).is_file():
            return error_response("No first frame yet.", code=404)
        resp = make_response(send_file(path, mimetype="image/png", conditional=True))
        resp.headers["Cache-Control"] = "private, max-age=120"
        return resp
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to serve that frame", str(e))


@app.route('/api/admin/studio/worlds/frames', methods=['GET'])
def admin_studio_world_frames_get():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        exp = _experience_json()
        frames = {}
        for world in exp.get("worlds") or []:
            wslug = world.get("slug") or ""
            if wslug:
                frames[wslug] = {
                    "url": world.get("frame_url") or "",
                    "status": world.get("frame_status") or "missing",
                    "generating": bool(world.get("frame_generating")),
                    "source": world.get("frame_source") or "",
                }
        return jsonify(success_response({"experience": _experience_json(exp), "frames": frames}))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to load world frames", str(e))


@app.route('/api/admin/studio/worlds/frames/ensure', methods=['POST'])
def admin_studio_world_frames_ensure():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import world_frames
        body = request.get_json(silent=True) or {}
        wslug = (body.get("slug") or "").strip()
        if wslug:
            rec = world_frames.ensure(wslug, wait=False)
            return jsonify(success_response({
                "frame": rec,
                "experience": _experience_json(),
            }))
        world_frames.schedule_ensure_all()
        return jsonify(success_response({"experience": _experience_json()}))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to refresh world frames", str(e))


@app.route('/api/admin/studio/worlds/frames/reset', methods=['POST'])
def admin_studio_world_frames_reset():
    """Snapshot this World's live design, then regenerate its first frame.

    The desk REDRAW uses this. Body: ``id`` (World id) or ``slug``.
    """
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        import world_frames
        body = request.get_json(silent=True) or {}
        slug = (body.get("slug") or "").strip()
        wid = body.get("id") or body.get("world_id")
        exp = experience_store.get_experience()
        if wid:
            try:
                exp = experience_store.persist_world_snapshot(wid)
            except KeyError:
                pass
            world = experience_store.world_by_id(exp, wid)
            slug = str((world or {}).get("slug") or slug)
        if not slug:
            slug = world_frames.start_world_slug(exp)
        if not slug:
            return error_response("No World to reset.", code=400)
        rec = world_frames.force_reset(slug, wait=False)
        return jsonify(success_response({
            "frame": rec,
            "experience": _experience_json(),
        }))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to reset that World", str(e))


@app.route('/api/experiences', methods=['GET'])
def api_experiences_list():
    """Catalog of Experiences on disk for the start-menu picker."""
    try:
        import experience_store
        import world_frames
        items = experience_store.list_experiences(play_catalog=True)
        try:
            world_frames.maybe_kick_all()
        except Exception:
            pass
        return jsonify(success_response({
            "experiences": items,
            "active": experience_store.get_active_slug(),
        }))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to list Experiences", str(e))


@app.route('/api/admin/studio/experiences', methods=['POST'])
def admin_studio_experience_create():
    """New Experience file. The editor carousel's + NEW."""
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        body = request.get_json(silent=True) or {}
        exp = experience_store.create_experience(
            body.get("name") or "",
            clone_from=body.get("clone_from") or "",
        )
        return jsonify(success_response({
            "experience": _experience_json(exp),
            "active": experience_store.get_active_slug(),
            "experiences": experience_store.list_experiences(),
        }, "Experience created"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to create that Experience", str(e))


@app.route('/api/admin/studio/experiences/rename', methods=['POST'])
def admin_studio_experience_rename():
    """Name the Experience the editor is looking at."""
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        body = request.get_json(silent=True) or {}
        slug = body.get("slug") or body.get("id") or ""
        name = body.get("name")
        if name is None:
            return error_response("Body must include 'name'.", code=400)
        exp = experience_store.rename_experience(name, slug)
        return jsonify(success_response({
            "experience": _experience_json(exp),
            "active": experience_store.get_active_slug(),
            "experiences": experience_store.list_experiences(),
        }, "Experience named"))
    except KeyError as e:
        return error_response(str(e), code=404)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to name that Experience", str(e))


@app.route('/api/experiences/activate', methods=['POST'])
def api_experiences_activate():
    """Make this Experience the one Play / reset / the editor bind to."""
    try:
        import experience_store
        body = request.get_json(silent=True) or {}
        slug = body.get("slug") or body.get("id") or experience_store.ACTIVE_SLUG
        slug = experience_store.set_active(slug)
        return jsonify(success_response({
            "experience": _experience_json(experience_store.get_experience(slug)),
            "active": slug,
        }, "Experience ready"))
    except KeyError as e:
        return error_response(str(e), code=404)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to activate that Experience", str(e))


@app.route('/api/admin/studio/experience', methods=['GET'])
def admin_studio_experience_get():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        return jsonify(success_response({"experience": _experience_json()}))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to load the Experience", str(e))


@app.route('/api/admin/studio/experience', methods=['PUT'])
def admin_studio_experience_put():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        body = request.get_json(silent=True) or {}
        payload = body.get('experience') if isinstance(body.get('experience'), dict) else body
        exp = experience_store.save_experience(payload)
        return jsonify(success_response({"experience": _experience_json(exp)}, "Experience saved"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to save the Experience", str(e))


@app.route('/api/admin/studio/experience/sound', methods=['PUT'])
def admin_studio_experience_sound():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        body = request.get_json(silent=True) or {}
        payload = body.get("sound") if isinstance(body.get("sound"), dict) else body
        exp = experience_store.set_sound(payload)
        return jsonify(success_response({"experience": _experience_json(exp)}, "Sound saved"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to save sound", str(e))


@app.route('/api/admin/studio/experience/pacing', methods=['PUT'])
def admin_studio_experience_pacing():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        body = request.get_json(silent=True) or {}
        payload = body.get("threat") if isinstance(body.get("threat"), dict) else body
        if isinstance(body.get("pacing"), dict):
            payload = body.get("pacing")
        exp = experience_store.set_pacing(payload)
        return jsonify(success_response({"experience": _experience_json(exp)}, "Pacing saved"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to save pacing", str(e))


@app.route('/api/admin/studio/experience/lore', methods=['GET'])
def admin_studio_experience_lore_get():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        lore = experience_store.get_lore()
        return jsonify(success_response({
            "lore": lore,
            "stats": experience_store.lore_stats(lore),
            "experience": _experience_json(),
        }))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to read lore", str(e))


@app.route('/api/admin/studio/experience/lore', methods=['PUT'])
def admin_studio_experience_lore_put():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        body = request.get_json(silent=True) or {}
        lore = experience_store.set_lore_notes(
            body.get("notes"),
            enabled=body.get("enabled"),
        )
        return jsonify(success_response({
            "lore": lore,
            "stats": experience_store.lore_stats(lore),
            "experience": _experience_json(),
        }, "Lore saved"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to save lore", str(e))


@app.route('/api/admin/studio/experience/lore', methods=['POST'])
def admin_studio_experience_lore_add():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        body = request.get_json(silent=True) or {}
        added = experience_store.add_lore_document(
            name=body.get("name") or "",
            text=body.get("text") or "",
            image=body.get("image") or "",
        )
        return jsonify(success_response({
            "lore": added["lore"],
            "document": added.get("document"),
            "stats": experience_store.lore_stats(added["lore"]),
            "experience": _experience_json(),
        }, "Lore added"))
    except ValueError as e:
        return error_response(str(e), code=400)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to add lore", str(e))


@app.route('/api/admin/studio/experience/lore', methods=['DELETE'])
def admin_studio_experience_lore_delete():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        body = request.get_json(silent=True) or {}
        did = body.get("id") or body.get("doc_id")
        if not did:
            return error_response("Body must include 'id'.", code=400)
        lore = experience_store.remove_lore_document(did)
        return jsonify(success_response({
            "lore": lore,
            "stats": experience_store.lore_stats(lore),
            "experience": _experience_json(),
        }, "Lore removed"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to remove that lore", str(e))


@app.route('/api/admin/studio/experience/lore/move', methods=['POST'])
def admin_studio_experience_lore_move():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        body = request.get_json(silent=True) or {}
        lore = experience_store.move_lore(body.get("x"), body.get("y"))
        return jsonify(success_response({
            "lore": lore,
            "experience": _experience_json(),
        }))
    except ValueError as e:
        return error_response(str(e), code=400)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to move lore", str(e))


@app.route('/api/experience/lore/<slug>/<doc_id>', methods=['GET'])
def serve_experience_lore(slug, doc_id):
    """Serve an uploaded lore image. Unguessable ids; same contract as plates."""
    try:
        import experience_store
        path = experience_store.lore_file_path(slug, doc_id)
        if not path:
            return error_response("No such lore file.", code=404)
        suffix = path.suffix.lower()
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".md": "text/plain; charset=utf-8",
            ".txt": "text/plain; charset=utf-8",
        }.get(suffix, "application/octet-stream")
        resp = make_response(send_file(path, mimetype=mime, conditional=True))
        resp.headers["Cache-Control"] = "private, max-age=120"
        return resp
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to read that file", str(e))


@app.route('/api/admin/studio/experience/worlds', methods=['POST'])
def admin_studio_experience_world_add():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        body = request.get_json(silent=True) or {}
        exp = experience_store.add_world(
            body.get('name') or "",
            clone_from=body.get('clone_from') or "",
            x=body.get('x'),
            y=body.get('y'),
        )
        added = (exp.get("worlds") or [])[-1] if exp.get("worlds") else None
        if added and added.get("slug"):
            try:
                import world_frames
                world_frames.schedule_ensure(added["slug"], delay=0.4)
            except Exception:
                pass
        return jsonify(success_response({"experience": _experience_json(exp)}, "World added"))
    except (ValueError, KeyError) as e:
        return error_response(str(e), code=400)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to add a World", str(e))


@app.route('/api/admin/studio/experience/worlds', methods=['DELETE'])
def admin_studio_experience_world_delete():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        body = request.get_json(silent=True) or {}
        wid = body.get('id') or body.get('world_id')
        if not wid:
            return error_response("Body must include 'id'.", code=400)
        exp = experience_store.remove_world(wid)
        return jsonify(success_response({"experience": _experience_json(exp)}, "World removed"))
    except ValueError as e:
        return error_response(str(e), code=400)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to remove that World", str(e))


@app.route('/api/admin/studio/experience/move', methods=['POST'])
def admin_studio_experience_move():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        body = request.get_json(silent=True) or {}
        exp = experience_store.move_experience(body.get('x'), body.get('y'))
        return jsonify(success_response({"experience": _experience_json(exp)}))
    except ValueError as e:
        return error_response(str(e), code=400)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to move that Experience", str(e))


@app.route('/api/admin/studio/experience/worlds/move', methods=['POST'])
def admin_studio_experience_world_move():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        body = request.get_json(silent=True) or {}
        wid = body.get('id') or body.get('world_id')
        if not wid:
            return error_response("Body must include 'id'.", code=400)
        exp = experience_store.move_world(wid, body.get('x'), body.get('y'))
        return jsonify(success_response({"experience": _experience_json(exp)}))
    except KeyError as e:
        return error_response(str(e), code=404)
    except ValueError as e:
        return error_response(str(e), code=400)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to move that World", str(e))


@app.route('/api/admin/studio/experience/worlds/rename', methods=['POST'])
def admin_studio_experience_world_rename():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        body = request.get_json(silent=True) or {}
        wid = body.get('id') or body.get('world_id')
        name = body.get('name')
        if not wid or not name:
            return error_response("Body must include 'id' and 'name'.", code=400)
        blurb = body.get('blurb') if 'blurb' in body else None
        exp = experience_store.rename_world(wid, name, blurb=blurb)
        return jsonify(success_response({"experience": _experience_json(exp)}))
    except KeyError as e:
        return error_response(str(e), code=404)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to rename that World", str(e))


@app.route('/api/admin/studio/experience/start', methods=['POST'])
def admin_studio_experience_start():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        body = request.get_json(silent=True) or {}
        wid = body.get('id') or body.get('world_id')
        if not wid:
            return error_response("Body must include 'id'.", code=400)
        exp = experience_store.set_start_world(wid)
        return jsonify(success_response({"experience": _experience_json(exp)}, "Start set"))
    except KeyError as e:
        return error_response(str(e), code=404)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to set the start World", str(e))


@app.route('/api/admin/studio/experience/enter', methods=['POST'])
def admin_studio_experience_enter():
    """Load a graph World's snapshot into the live editor."""
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        import worlds_store
        import prompts_store
        import game_identity
        body = request.get_json(silent=True) or {}
        wid = body.get('id') or body.get('world_id')
        exp = experience_store.get_experience()
        world = experience_store.world_by_id(exp, wid) if wid else None
        if not world:
            return error_response("World not found.", code=404)
        if world.get("slug"):
            worlds_store.load_world(world["slug"])
        return jsonify(success_response({
            "experience": _experience_json(exp),
            "world": world,
            "prompts": dict(prompts_store.PROMPTS),
            "identity": game_identity.get_spec(),
            "identity_preview": game_identity.preview(),
        }, f"Editing '{world.get('name') or 'World'}'"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to enter that World", str(e))


@app.route('/api/admin/studio/experience/persist', methods=['POST'])
def admin_studio_experience_persist():
    """Write the live prompt file back into the World currently being edited."""
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        body = request.get_json(silent=True) or {}
        wid = body.get('id') or body.get('world_id')
        if not wid:
            return error_response("Body must include 'id'.", code=400)
        exp = experience_store.persist_world_snapshot(wid)
        world = experience_store.world_by_id(exp, wid)
        if world and world.get("slug"):
            try:
                import world_frames
                world_frames.schedule_ensure(world["slug"], delay=0.25)
            except Exception:
                pass
        return jsonify(success_response({"experience": _experience_json(exp)}, "World snapshot updated"))
    except KeyError as e:
        return error_response(str(e), code=404)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to persist that World", str(e))


@app.route('/api/admin/studio/experience/cutscenes', methods=['POST'])
def admin_studio_experience_cutscene_add():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        body = request.get_json(silent=True) or {}
        exp = experience_store.add_cutscene(
            body.get('name') or "",
            mood=body.get('mood') or "threshold",
            source=body.get('source') or "incoming",
            shot_brief=body.get('shot_brief') or "",
            blurb=body.get('blurb') or "",
            x=body.get('x'),
            y=body.get('y'),
        )
        return jsonify(success_response({"experience": _experience_json(exp)}, "Cutscene added"))
    except (ValueError, KeyError) as e:
        return error_response(str(e), code=400)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to add a Cutscene", str(e))


@app.route('/api/admin/studio/experience/cutscenes', methods=['DELETE'])
def admin_studio_experience_cutscene_delete():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        body = request.get_json(silent=True) or {}
        cid = body.get('id') or body.get('cutscene_id')
        if not cid:
            return error_response("Body must include 'id'.", code=400)
        exp = experience_store.remove_cutscene(cid)
        return jsonify(success_response({"experience": _experience_json(exp)}, "Cutscene removed"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to remove that Cutscene", str(e))


@app.route('/api/admin/studio/experience/cutscenes/move', methods=['POST'])
def admin_studio_experience_cutscene_move():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        body = request.get_json(silent=True) or {}
        cid = body.get('id') or body.get('cutscene_id')
        if not cid:
            return error_response("Body must include 'id'.", code=400)
        exp = experience_store.move_cutscene(cid, body.get('x'), body.get('y'))
        return jsonify(success_response({"experience": _experience_json(exp)}))
    except KeyError as e:
        return error_response(str(e), code=404)
    except ValueError as e:
        return error_response(str(e), code=400)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to move that Cutscene", str(e))


@app.route('/api/admin/studio/experience/cutscenes/rename', methods=['POST'])
def admin_studio_experience_cutscene_rename():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        body = request.get_json(silent=True) or {}
        cid = body.get('id') or body.get('cutscene_id')
        name = body.get('name')
        if not cid or not name:
            return error_response("Body must include 'id' and 'name'.", code=400)
        exp = experience_store.rename_cutscene(
            cid, name,
            blurb=body.get('blurb') if 'blurb' in body else None,
            mood=body.get('mood') if 'mood' in body else None,
            source=body.get('source') if 'source' in body else None,
            shot_brief=body.get('shot_brief') if 'shot_brief' in body else None,
        )
        return jsonify(success_response({"experience": _experience_json(exp)}))
    except KeyError as e:
        return error_response(str(e), code=404)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to rename that Cutscene", str(e))


@app.route('/api/admin/studio/experience/transitions', methods=['POST'])
def admin_studio_experience_transition_add():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        body = request.get_json(silent=True) or {}
        exp = experience_store.add_transition(
            body.get('from'), body.get('to'), body.get('condition'))
        return jsonify(success_response({"experience": _experience_json(exp)}, "Transition added"))
    except (ValueError, KeyError) as e:
        return error_response(str(e), code=400)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to add that transition", str(e))


@app.route('/api/admin/studio/experience/transitions', methods=['PUT'])
def admin_studio_experience_transition_put():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        body = request.get_json(silent=True) or {}
        tid = body.get('id') or body.get('transition_id')
        if not tid:
            return error_response("Body must include 'id'.", code=400)
        exp = experience_store.update_transition(tid, body)
        return jsonify(success_response({"experience": _experience_json(exp)}, "Transition updated"))
    except KeyError as e:
        return error_response(str(e), code=404)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to update that transition", str(e))


@app.route('/api/admin/studio/experience/transitions', methods=['DELETE'])
def admin_studio_experience_transition_delete():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import experience_store
        body = request.get_json(silent=True) or {}
        tid = body.get('id') or body.get('transition_id')
        if not tid:
            return error_response("Body must include 'id'.", code=400)
        exp = experience_store.remove_transition(tid)
        return jsonify(success_response({"experience": _experience_json(exp)}, "Transition removed"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to remove that transition", str(e))


# ═══════════════════════════════════════════════════════════════════
# LEVELS — the LEVEL LAYER only
#
# A world snapshots every editable prompt; a level snapshots just the place.
# That difference is the point: loading a level must leave the engine's
# contracts, the game's identity and the cast exactly as they were, so a
# designer can build a set of levels for one game and flip between them.
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/admin/studio/levels', methods=['GET'])
def admin_studio_levels_list():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import levels_store
        return jsonify(success_response({
            "levels": levels_store.list_levels(),
            "level_keys": levels_store.level_keys(),
        }))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to list levels", str(e))


@app.route('/api/admin/studio/levels', methods=['POST'])
def admin_studio_levels_save():
    """Snapshot the current level layer under a name."""
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import levels_store
        body = request.get_json(silent=True) or {}
        name = body.get('name')
        if not name:
            return error_response("Body must include 'name'.", code=400)
        info = levels_store.save_level(name, body.get('note', ''))
        return jsonify(success_response(
            {"level": info, "levels": levels_store.list_levels()},
            f"Saved level '{info['name']}'"))
    except ValueError as e:
        return error_response(str(e), code=400)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to save level", str(e))


@app.route('/api/admin/studio/levels/load', methods=['POST'])
def admin_studio_levels_load():
    """Swap in a saved level, leaving every other layer untouched."""
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import levels_store
        import prompts_store
        import game_identity
        body = request.get_json(silent=True) or {}
        slug = body.get('slug') or body.get('name')
        if not slug:
            return error_response("Body must include 'slug'.", code=400)
        info = levels_store.load_level(slug)
        return jsonify(success_response({
            "level": info,
            "prompts": dict(prompts_store.PROMPTS),
            "identity": game_identity.get_spec(),
            "identity_preview": game_identity.preview(),
        }, f"Loaded level '{info['name']}'"))
    except KeyError as e:
        return error_response(str(e), code=404)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to load level", str(e))


@app.route('/api/admin/studio/levels', methods=['DELETE'])
def admin_studio_levels_delete():
    if not _admin_token_ok():
        return _admin_unauthorized()
    try:
        import levels_store
        body = request.get_json(silent=True) or {}
        slug = body.get('slug') or body.get('name')
        if not slug:
            return error_response("Body must include 'slug'.", code=400)
        ok = levels_store.delete_level(slug)
        return jsonify(success_response({"deleted": ok, "levels": levels_store.list_levels()}))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to delete level", str(e))


# ═══════════════════════════════════════════════════════════════════
# INFO & HEALTH ENDPOINTS
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/music', methods=['GET'])
def api_music_get():
    """What is scoring the game: a loop you chose, or the per-scene generator."""
    try:
        scene_audio.kick_stock_warmup()
        loop = scene_audio.custom_loop()
        return jsonify({"data": {
            "loop": loop,
            "direction": scene_audio.get_music_direction(),
            "preview": scene_audio.last_preview("preview"),
            "menu_loop": scene_audio.menu_loop(),
            "menu_direction": scene_audio.get_menu_direction(),
            "menu_preview": scene_audio.last_preview("menu_preview"),
            "sfx_direction": scene_audio.get_sfx_direction(),
            "can_generate": scene_audio.is_available(),
            "can_generate_reason": scene_audio.unavailable_reason(),
            "provider": "elevenlabs",
            "accepts": sorted(scene_audio.LOOP_EXTS.keys()),
            "max_bytes": scene_audio.MAX_LOOP_BYTES,
            "stock": scene_audio.stock_status(),
            "cache": scene_audio.list_generated_cache(
                request.args.get("session") or "default"),
        }})
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return error_response("Failed to read music", str(e))


@app.route('/api/music', methods=['PUT'])
def api_music_direction():
    """Save how this world sounds. Used on every scene, including the next run."""
    body = request.get_json(silent=True) or {}
    try:
        # Only write a field that was actually sent. Saving the title screen
        # used to POST {menu_prompt} and wipe the match direction to "".
        if "prompt" in body:
            prompt = scene_audio.set_music_direction(body.get("prompt") or "")
        else:
            prompt = scene_audio.get_music_direction()
        menu_prompt = body.get("menu_prompt")
        menu = scene_audio.get_menu_direction()
        if menu_prompt is not None:
            menu = scene_audio.set_menu_direction(menu_prompt)
        sfx_prompt = body.get("sfx_prompt")
        sfx = scene_audio.get_sfx_direction()
        if sfx_prompt is not None:
            sfx = scene_audio.set_sfx_direction(sfx_prompt)
        return jsonify({"data": {
            "direction": prompt,
            "loop": scene_audio.custom_loop(),
            "menu_direction": menu,
            "menu_loop": scene_audio.menu_loop(),
            "sfx_direction": sfx,
        }})
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return error_response("Failed to save music direction", str(e))


@app.route('/api/music/stock', methods=['GET', 'POST'])
def api_music_stock():
    """Pre-cached encounter stingers and fallback ambience beds."""
    try:
        if request.method == 'POST':
            body = request.get_json(silent=True) or {}
            key = (body.get("id") or "").strip()
            force = bool(body.get("force"))
            if key:
                rec = scene_audio.ensure_one_stock(key, force=force)
                if not rec:
                    return jsonify({"error": "invalid", "message": "Unknown stock id."}), 400
                return jsonify({"data": rec})
            return jsonify({"data": scene_audio.ensure_stock_sounds(force=force)})
        scene_audio.kick_stock_warmup()
        return jsonify({"data": {
            "can_generate": scene_audio.is_available(),
            "files": scene_audio.stock_status(),
            "designer": scene_audio.encounter_designer_urls(),
        }})
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return error_response("Failed to read stock audio", str(e))


@app.route('/api/music/inspect', methods=['POST'])
def api_music_inspect():
    """Show the music + SFX prompts a scene would send. No generation."""
    body = request.get_json(silent=True) or {}
    try:
        return jsonify({"data": scene_audio.inspect_scene(
            body.get("prompt") or "",
            mode=(body.get("mode") or "scene"),
        )})
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return error_response("Failed to inspect music", str(e))


@app.route('/api/music/test', methods=['POST'])
def api_music_test():
    """Generate a one-off test clip (music, ambience, or stock stinger)."""
    body = request.get_json(silent=True) or {}
    prompt = (body.get("prompt") or "").strip()
    layer = (body.get("layer") or "music").strip().lower()
    if layer != "stinger" and not prompt:
        return jsonify({"error": "invalid", "message": "Write a scene or prompt first."}), 400
    try:
        rec = scene_audio.generate_test_clip(
            prompt,
            mode=(body.get("mode") or "scene"),
            layer=layer,
            seconds=body.get("seconds"),
            session_id=body.get("session") or "default",
        )
        if not rec:
            return jsonify({"error": "unavailable", "message":
                            "Couldn't generate that — check ELEVENLABS_API_KEY."}), 502
        if rec.get("error") == "no_key":
            return jsonify({"error": "unavailable", "message":
                            "Couldn't generate that — check ELEVENLABS_API_KEY."}), 502
        return jsonify({"data": rec})
    except ValueError as e:
        return jsonify({"error": "invalid", "message": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return error_response("Failed to test audio", str(e))


@app.route('/api/music/cache', methods=['GET', 'DELETE'])
def api_music_cache():
    """Per-scene generated clips. DELETE clears them; stock and locked loops stay."""
    session_id = request.args.get("session") or "default"
    try:
        if request.method == "DELETE":
            removed = scene_audio.clear_generated_cache(session_id)
            return jsonify({"data": {"removed": removed,
                                     "cache": scene_audio.list_generated_cache(session_id)}})
        return jsonify({"data": {"cache": scene_audio.list_generated_cache(session_id)}})
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return error_response("Failed to read audio cache", str(e))


@app.route('/api/music/preview', methods=['POST'])
def api_music_preview():
    """Hear a prompt without locking it as the only track."""
    body = request.get_json(silent=True) or {}
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "invalid", "message": "Write how it should sound first."}), 400
    try:
        stem = "menu_preview" if (body.get("for") == "menu") else "preview"
        preview = scene_audio.generate_preview(
            prompt, seconds=body.get("seconds") or 8, stem=stem)
        if not preview:
            return jsonify({"error": "unavailable", "message":
                            "Couldn't preview that — check ELEVENLABS_API_KEY, then try again."}), 502
        return jsonify({"data": {"preview": preview}})
    except ValueError as e:
        return jsonify({"error": "invalid", "message": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return error_response("Failed to preview music", str(e))


@app.route('/api/music/generate', methods=['POST'])
def api_music_generate():
    """Write the music yourself: {prompt, seconds?} straight to ElevenLabs Music.

    Distinct from /api/scene_audio, which derives music direction from a scene
    description. Here the prompt IS the direction.
    """
    body = request.get_json(silent=True) or {}
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "invalid", "message": "Write a prompt first."}), 400
    try:
        stem = "menu" if (body.get("for") == "menu") else "loop"
        loop = scene_audio.generate_loop(
            prompt, seconds=body.get("seconds") or 12, stem=stem)
        if not loop:
            return jsonify({"error": "unavailable", "message":
                            "Couldn't generate that — no ELEVENLABS_API_KEY, or "
                            "the generate call failed."}), 502
        key = "menu_loop" if stem == "menu" else "loop"
        return jsonify({"data": {key: loop, "loop": loop}})
    except ValueError as e:
        return jsonify({"error": "invalid", "message": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return error_response("Failed to generate music", str(e))


@app.route('/api/music/upload', methods=['POST'])
def api_music_upload():
    """Adopt a file the player uploaded: {audio: <data-url>, name?}."""
    body = request.get_json(silent=True) or {}
    raw = str(body.get("audio") or "")
    try:
        if "," in raw and raw.strip().lower().startswith("data:"):
            header, b64 = raw.split(",", 1)
        else:
            header, b64 = "", raw
        data = base64.b64decode(b64 or "", validate=False)
        # Trust the filename's extension over the data URL's mime type: browsers
        # are inconsistent about the latter (audio/mp3 vs audio/mpeg vs empty).
        name = str(body.get("name") or "")
        ext = (name.rsplit(".", 1)[-1] if "." in name else "").lower()
        if ext not in scene_audio.LOOP_EXTS:
            for candidate, mime in scene_audio.LOOP_EXTS.items():
                if mime in header:
                    ext = candidate
                    break
        stem = "menu" if (body.get("for") == "menu") else "loop"
        loop = scene_audio.set_uploaded_loop(
            data, ext or "wav", name=name, stem=stem)
        key = "menu_loop" if stem == "menu" else "loop"
        return jsonify({"data": {key: loop, "loop": loop}})
    except ValueError as e:
        return jsonify({"error": "invalid", "message": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return error_response("Failed to save that audio", str(e))


@app.route('/api/music', methods=['DELETE'])
def api_music_clear():
    """Back to scoring each scene as it comes. ?for=menu clears the title track."""
    try:
        if (request.args.get("for") or "").strip() == "menu":
            scene_audio.clear_menu_loop()
            return jsonify({"data": {"menu_loop": None}})
        scene_audio.clear_custom_loop()
        return jsonify({"data": {"loop": None}})
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return error_response("Failed to clear music", str(e))


@app.route('/api/admin/studio/tunables', methods=['GET'])
def studio_tunables_get():
    """The runtime knobs, their schema, and what each is actually set to.

    See tunables.py. These were environment variables read once at boot, which
    meant the editor could report that SCAN had fallen back to Gemini but not do
    anything about it.
    """
    try:
        import tunables
        return jsonify({"data": {
            "schema": tunables.schema(),
            "values": tunables.current(),
        }})
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return error_response("Failed to read settings", str(e))


@app.route('/api/admin/studio/tunables', methods=['PUT'])
def studio_tunables_put():
    """Set knobs. Body is `{name: value}`; a null clears one back to the
    environment's value. Validated against the schema — this is browser
    reachable, so out-of-range is a 400 rather than a surprise.
    """
    body = request.get_json(silent=True) or {}
    try:
        import tunables
        values = tunables.clear() if body.get("_clear") else tunables.update(body)
        return jsonify({"data": {"schema": tunables.schema(), "values": values}})
    except ValueError as e:
        return jsonify({"error": "invalid", "message": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return error_response("Failed to save settings", str(e))


@app.route('/api/flipbook', methods=['GET', 'POST'])
def api_flipbook():
    """Flipbook for ONE session: on/off, how many in-betweens, how fast.

    The same three knobs live in tunables (studio settings) as the process-wide
    default. This route is the per-session override, which is what a Watch
    render needs: it turns flipbook on for its own session and leaves live Play
    alone, instead of flipping a global and hoping nobody else is playing.

    POST body: {"enabled": bool, "frames": 2|4|8|16, "frame_ms": int,
                "session": "<id>"}. Omitted keys are left as they were; a null
    `enabled` drops the session back to following the global setting.
    """
    try:
        import flipbook as flipbook_mod
        body = request.get_json(silent=True) or {}
        session_id = (body.get("session") or body.get("session_id")
                      or request.args.get("session") or "default")
        session_id = Path(str(session_id)).name or "default"

        if request.method == 'POST':
            with engine.WORLD_STATE_LOCK:
                st = engine._load_state(session_id)
                if "enabled" in body:
                    st["flipbook_mode"] = (None if body["enabled"] is None
                                           else bool(body["enabled"]))
                if body.get("frames") is not None:
                    st["flipbook_frames"] = flipbook_mod.normalize_frames(body["frames"])
                if body.get("frame_ms") is not None:
                    st["flipbook_frame_ms"] = max(
                        80, min(1000, int(body["frame_ms"])))
                engine._save_state(st, session_id)
        else:
            st = engine._load_state(session_id)

        return jsonify({"data": {
            "session": session_id,
            "settings": engine.flipbook_settings(st),
            "frame_counts": list(flipbook_mod.FRAME_COUNTS),
            # What the process default is, so a client can show whether this
            # session is following it or overriding it.
            "global_enabled": bool(engine.FLIPBOOK_ENABLED),
            "current_sequence": st.get("current_sequence") or None,
        }})
    except ValueError as e:
        return jsonify({"error": "invalid", "message": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return error_response("Failed to set flipbook", str(e))


@app.route('/api/talk/voices/library', methods=['GET'])
def talk_voice_library():
    """Every voice on the ElevenLabs account, including the ones you designed
    there. `voices.json` is a curated list of stock ids baked into the repo, so
    without this a workspace full of custom voices is invisible in game.
    """
    try:
        import voice_design
        force = request.args.get("refresh") in ("1", "true", "yes")
        out = voice_design.voice_library(force=force)
        # A 400 from BOTH listing endpoints is almost never the request; it is
        # usually the key. engine already knows how to spot the classic mistake
        # (an agent id pasted into ELEVENLABS_API_KEY), so ask it rather than
        # leaving "http_400" on screen for someone to guess at.
        if not out.get("ok"):
            try:
                problem = engine.elevenlabs_key_problem()
            except Exception:  # noqa: BLE001
                problem = None
            if problem:
                out["reason"] = "bad_key"
                out["detail"] = str(problem)[:200]
        return jsonify({"data": out})
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"data": {"ok": False, "reason": str(e)[:120], "voices": []}})


@app.route('/api/info', methods=['GET'])
def api_info():
    """Get API information"""
    return jsonify({
        "name": "SOMEWHERE Game Engine API",
        "version": "2.0.0",
        "features": [
            "Session management",
            "Archive system",
            "Multi-user support",
            "Asset serving",
            "Admin dashboard"
        ],
        "endpoints": {
            "sessions": "/api/sessions",
            "archives": "/api/archives",
            "state": "/api/state",
            "history": "/api/history",
            "game": "/api/game/*",
            "admin": "/admin"
        }
    })


@app.route('/api/health', methods=['GET'])
def api_health():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "service": "SOMEWHERE Game Engine API",
        # WHICH BUILD IS ACTUALLY SERVING. Without this, "did my push deploy?"
        # can only be answered by inferring it from behaviour, and the dashboard's
        # event list is easy to read stale — so a live deploy looks like a missing
        # one. Answer it directly instead: curl /api/health and compare `commit`
        # to the SHA you pushed.
        "build": _build_info(),
        # Which detector is answering /api/detect, and whether the on-device one
        # actually loaded. Worth surfacing here because a missing .tflite or an
        # uninstalled mediapipe degrades SCAN silently otherwise.
        "detect": _detect_backend_status(),
        # Whether NPC conversation can connect, and why not when it can't.
        "talk": _talk_status(),
        "music": _music_status(),
    })


# ═══════════════════════════════════════════════════════════════════
# QUITTING (desktop app only)
# ═══════════════════════════════════════════════════════════════════
#
# The same Flask app serves the hosted game, where "any client can stop the
# process" is a denial-of-service button. So the route below is inert unless
# something calls enable_shutdown() first, and only the desktop launcher does
# (see play.py). Hosted deployments never arm it and answer 403.

_SHUTDOWN_HOOKS: list = []
_shutdown_armed = False
_local_keys_armed = False


def enable_local_keys() -> None:
    """Allow PUT /api/keys to write the local key store.

    Same contract as enable_shutdown: only play.py / run_local.py arm this.
    Hosted gunicorn never does, so a visitor cannot overwrite the host's
    accounts or exfiltrate a write of their own key onto a shared box.
    """
    global _local_keys_armed
    _local_keys_armed = True
    try:
        billing.mark_local_app()
    except Exception:
        pass


def _keys_write_allowed() -> bool:
    if not _local_keys_armed:
        return False
    return request.remote_addr in ("127.0.0.1", "::1", "localhost")


def enable_shutdown(hook=None) -> None:
    """Allow POST /api/shutdown to stop this process.

    `hook` runs before the process goes, and is how the launcher closes its
    native window: destroying the window unblocks webview.start() so the app
    exits through main() normally instead of being shot in the head.
    """
    global _shutdown_armed
    _shutdown_armed = True
    if hook is not None:
        _SHUTDOWN_HOOKS.append(hook)


def _release_compute() -> list:
    """Stop everything that would otherwise keep spending after we quit.

    Killing the process covers the threads, because they are all daemons. It
    does NOT cover a render, which drives a *child* process — that one survives
    its parent and keeps calling paid image models into a folder nobody is
    watching. So the render is cancelled explicitly and first.
    """
    stopped = []

    try:
        # Forced: quitting means "stop spending right now", not "wait for a
        # nice video" — the graceful stop the UI's STOP button uses can take
        # a full turn to land, which is fine mid-session but not on the way out.
        render_jobs.cancel(force=True)
        stopped.append("render")
    except RuntimeError:
        pass  # nothing running, which is the normal case
    except Exception as e:  # noqa: BLE001
        print(f"[QUIT] could not cancel the render: {e}", flush=True)

    try:
        import gemini_live_talk as _talk
        with _talk._SESSIONS_LOCK:
            live = list(_talk._SESSIONS)
        for bridge in live:
            try:
                bridge.ws.close()
            except Exception:  # noqa: BLE001
                pass
        if live:
            stopped.append(f"{len(live)} conversation(s)")
    except Exception:  # noqa: BLE001
        pass

    # MediaPipe complains loudly if it is collected during interpreter teardown,
    # and os._exit() below skips the atexit handler that normally prevents that.
    try:
        import local_vision
        local_vision._close_detector()
    except Exception:  # noqa: BLE001
        pass

    return stopped


def _quit_process() -> None:
    """Let the response finish, close the window, then make sure we die."""
    # A deadline that touches nothing and therefore cannot be blocked. The
    # ordinary path below can be: a window hook that hangs, or — the way this
    # was actually found — a print into a stdout pipe nobody is draining, which
    # blocks forever and never reaches the exit two lines later. Quitting must
    # not depend on anything, least of all on logging that we are quitting.
    def _failsafe() -> None:
        time.sleep(6.0)
        os._exit(0)

    threading.Thread(target=_failsafe, name="quit-failsafe", daemon=True).start()

    time.sleep(0.6)  # the browser needs the 200 before its socket goes away
    for hook in _SHUTDOWN_HOOKS:
        try:
            hook()
        except Exception:  # noqa: BLE001
            pass
    # If a hook closed the window, the process is already on its way out and
    # this never runs. If there was no window — browser mode — nothing else
    # will ever stop the serving thread, so end it here.
    time.sleep(2.0)
    os._exit(0)


@app.route('/api/shutdown', methods=['POST'])
def api_shutdown():
    """Stop the local app: cancel paid work, close the window, exit."""
    if not _shutdown_armed:
        return error_response(
            "Shutdown is not available on this server",
            "Only the desktop app can stop itself.", code=403)
    if request.remote_addr not in ("127.0.0.1", "::1", "localhost"):
        return error_response(
            "Shutdown is local-only", request.remote_addr, code=403)

    stopped = _release_compute()
    print(f"[QUIT] shutting down; stopped: {', '.join(stopped) or 'nothing running'}",
          flush=True)
    threading.Thread(target=_quit_process, name="quit", daemon=True).start()
    return jsonify({"status": "closing", "stopped": stopped})


# ═══════════════════════════════════════════════════════════════════
# LOCAL KEYS (BYOK) — start-menu ACCOUNT pane
# ═══════════════════════════════════════════════════════════════════
#
# GET is always safe: presence + last-four hint, never the secret.
# PUT is local-only and only when the launcher armed it. Hosted stays
# read-only so a player cannot write keys onto a shared Render process.


@app.route("/api/keys", methods=["GET"])
def api_keys_status():
    return jsonify(keys_store.public_status(editable=_keys_write_allowed()))


@app.route("/api/keys", methods=["PUT"])
def api_keys_put():
    if not _local_keys_armed:
        return error_response(
            "Keys cannot be edited on this server",
            "Only the local app can save API keys.", code=403)
    if request.remote_addr not in ("127.0.0.1", "::1", "localhost"):
        return error_response(
            "Keys are local-only", request.remote_addr, code=403)
    body = request.get_json(silent=True) or {}
    provider_id = str(body.get("id") or "").strip()
    if "value" not in body:
        return error_response("Missing key value", "Send {id, value}.", code=400)
    value = body.get("value")
    if value is None:
        value = ""
    if not isinstance(value, str):
        return error_response("Invalid key", "value must be a string.", code=400)
    try:
        status = keys_store.set_key(provider_id, value)
    except KeyError:
        return error_response("Unknown provider", provider_id, code=400)
    except ValueError as e:
        return error_response("Invalid key", str(e)[:200], code=400)
    except OSError:
        return error_response("Could not save keys", "The local store is not writable.", code=500)
    return jsonify(status)


def _usage_payload(*, ignore_cookie: bool = False) -> dict:
    """Ledger + account wallet. No secrets."""
    import usage_limits
    status = usage_limits.public_status()
    status.update(billing.public_status(ignore_cookie=ignore_cookie))
    status["editable"] = _keys_write_allowed()
    status["billing_editable"] = True
    if billing.requires_wallet() and status.get("account", {}).get("linked"):
        status["monthly_cap_usd"] = status.get("account_cap_usd")
        status["remaining_usd"] = status.get("account_remaining_usd")
        status["over_cap"] = bool(status.get("account_over_cap"))
    return status


def _set_account_cookie(resp, email: Optional[str]):
    if email:
        resp.set_cookie(
            billing.COOKIE, billing.cookie_value(email),
            max_age=365 * 24 * 3600, httponly=True, samesite="Lax", path="/",
        )
    else:
        resp.delete_cookie(billing.COOKIE, path="/")
    return resp


@app.route("/api/usage", methods=["GET"])
def api_usage_status():
    """This month's model spend, account, wallet, and optional cap."""
    try:
        return jsonify(_usage_payload())
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to load usage", str(e))


@app.route("/api/usage", methods=["PUT"])
def api_usage_put():
    """Spend cap, on-demand toggle. Hosted caps are per-account."""
    body = request.get_json(silent=True) or {}
    if not body:
        return error_response("Missing body", "Send monthly_cap_usd and/or on_demand.", code=400)
    try:
        if "on_demand" in body:
            billing.set_on_demand(body.get("on_demand"))
        if "monthly_cap_usd" in body:
            if billing.requires_wallet() and billing.current_account().get("email"):
                billing.set_monthly_cap_usd(body.get("monthly_cap_usd"))
            elif _keys_write_allowed():
                import usage_limits
                usage_limits.set_monthly_cap_usd(body.get("monthly_cap_usd"))
            else:
                return error_response(
                    "Usage limits cannot be edited on this server",
                    "Link an account to set your own spend cap.", code=403)
        return jsonify(_usage_payload())
    except ValueError as e:
        return error_response("Invalid setting", str(e)[:200], code=400)
    except OSError:
        return error_response("Could not save usage limit", "The local store is not writable.", code=500)


@app.route("/api/billing/account", methods=["POST"])
def api_billing_link():
    body = request.get_json(silent=True) or {}
    try:
        email = billing.link_email(body.get("email"))
    except ValueError as e:
        return error_response("Invalid email", str(e)[:200], code=400)
    resp = jsonify(_usage_payload())
    return _set_account_cookie(resp, email)


@app.route("/api/billing/account", methods=["DELETE"])
def api_billing_unlink():
    billing.unlink()
    resp = jsonify(_usage_payload(ignore_cookie=True))
    return _set_account_cookie(resp, None)


@app.route("/api/billing/checkout", methods=["POST"])
def api_billing_checkout():
    if not billing.is_payments_enabled():
        return error_response("Payments are off", "Stripe keys are not configured.", code=404)
    body = request.get_json(silent=True) or {}
    try:
        out = billing.create_checkout(
            body.get("kind") or "pack", request, pack_id=body.get("pack"))
        return jsonify(out)
    except ValueError as e:
        return error_response("Checkout refused", str(e)[:200], code=400)
    except Exception as e:
        traceback.print_exc()
        return error_response("Checkout failed", str(e)[:200], code=500)


@app.route("/api/billing/redeem", methods=["POST"])
def api_billing_redeem():
    if not billing.is_payments_enabled():
        return error_response("Payments are off", "Stripe keys are not configured.", code=404)
    body = request.get_json(silent=True) or {}
    cs = str(body.get("checkout_session_id") or "").strip()
    if not cs:
        return error_response("Missing checkout", "Send {checkout_session_id}.", code=400)
    result = billing.redeem(cs)
    if not result.get("ok"):
        return jsonify(result), 402
    resp = jsonify({**result, "usage": _usage_payload()})
    email = (billing.current_account().get("email") or "").strip() or None
    return _set_account_cookie(resp, email)


@app.route('/', methods=['GET'])
def index():
    """Root: send visitors to the lobby splash. From there they either
    start a fresh instance of the experience or resume a saved run — the
    lobby then routes them to /play?session=<id> where the immersive UI
    takes over. Machine clients that want the JSON info blob (previously
    served here) can use /api/info instead, which has identical contents.

    Legacy behavior: /standalone still serves the immersive UI directly
    (defaulting to the shared 'default' session when no ?session=<id> is
    supplied), so bookmarks and embed links continue to work.

    Query string forwarding: any query params on `/` (e.g. `?comp=<code>`
    handed to an influencer, or utm tags) are carried through to `/lobby`
    so downstream code (the coin-op comp mechanism, analytics) can see
    them. Without this a shared root URL would silently drop the comp
    code and drop the influencer into the paid flow on first play."""
    qs = request.query_string.decode("utf-8") if request.query_string else ""
    target = "/lobby" + (f"?{qs}" if qs else "")
    return redirect(target)


# ═══════════════════════════════════════════════════════════════════
# RUN SERVER
# ═══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print("=" * 70)
    print("SOMEWHERE Game Engine API")
    print("=" * 70)
    port = int(os.getenv('PORT', 5001))
    # Debug mode is a security and stability hazard in production: it exposes
    # the interactive Werkzeug debugger to any HTTP client (arbitrary code
    # execution from the browser) and is single-threaded with auto-reload.
    # We default to off and only enable when FLASK_DEBUG=1 is explicitly set
    # (e.g. for local development). Render and any other production environment
    # will get a normal, threaded WSGI server.
    debug_mode = os.getenv('FLASK_DEBUG', '0') == '1'
    print(f"Starting API server on http://0.0.0.0:{port} (debug={debug_mode})")
    print(f"API Info: http://localhost:{port}/api/info")
    print(f"Health Check: http://localhost:{port}/api/health")
    print(f"Admin Dashboard: http://localhost:{port}/admin")
    print("=" * 70)

    app.run(debug=debug_mode, host='0.0.0.0', port=port, use_reloader=False, threaded=True)
