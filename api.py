"""
API wrapper for the SOMEWHERE game engine.
Provides RESTful endpoints for game state management, session control, and asset serving.
"""

import os
import json
import traceback
from pathlib import Path
from flask import Flask, request, jsonify, send_file, make_response, render_template, redirect
from flask_cors import CORS
import engine
import ai_provider_manager
import scene_audio
import coinop

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Optional realtime music streaming (Increment 2). flask-sock is an optional
# dependency: if it's missing, the /ws/scene_music route simply isn't registered
# and the client falls back to the clip-loop scene audio. Never let its absence
# break app startup.
try:
    from flask_sock import Sock
    _sock = Sock(app)
except Exception:  # noqa: BLE001
    _sock = None


# Allow embedding the game in an iframe (main site + Discord embedded app).
@app.after_request
def add_embed_headers(response):
    response.headers['Content-Security-Policy'] = (
        "frame-ancestors 'self' https://www.5th-corner.com https://discord.com https://canary.discord.com https://ptb.discord.com"
    )
    response.headers['X-Frame-Options'] = (
        "ALLOW-FROM https://www.5th-corner.com https://discord.com https://canary.discord.com https://ptb.discord.com"
    )
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
app.add_url_rule('/api/choose', 'standalone_api_choose', _session_scoped(engine.api_choose), methods=['POST'])
app.add_url_rule('/api/regenerate_choices', 'standalone_api_regenerate_choices', _session_scoped(engine.api_regenerate_choices), methods=['POST'])

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


@app.route('/api/coinop/checkout', methods=['POST'])
@_session_scoped
def _coinop_checkout():
    if not coinop.is_enabled():
        return jsonify({"error": "coinop_disabled"}), 404
    sid = engine._resolve_request_session_id()
    data = request.get_json(silent=True) or {}
    comp_code = (data.get('comp') or '').strip() or None
    try:
        out = coinop.create_checkout(sid, request, comp_code=comp_code)
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
    # api_revive and the resulting continue_used + player_choice_prompt
    # feed items are in the session's feed_log. Running api_revive again
    # would double-append the narrative beat, which reads as a stutter
    # on screen. The client already has everything it needs from the
    # normal /api/feed polling, so just acknowledge the replay.
    if result.get('already_redeemed'):
        return jsonify({
            "ok": True,
            "already_redeemed": True,
            "comp": result.get('comp', False),
            "revive_items": [],
        })
    # First-time redeem for this checkout id → mint the actual revive.
    revive_response = engine.api_revive()
    return jsonify({
        "ok": True,
        "already_redeemed": False,
        "comp": result.get('comp', False),
        "revive_items": revive_response.get_json() if hasattr(revive_response, 'get_json') else None,
    })


@app.route('/webhook/stripe', methods=['POST'])
def _coinop_webhook():
    if not coinop.is_enabled():
        return jsonify({"error": "coinop_disabled"}), 404
    payload = request.get_data()
    sig = request.headers.get('Stripe-Signature', '')
    result = coinop.handle_webhook(payload, sig)
    if not result.get('ok'):
        return jsonify(result), 400
    return jsonify(result)
# Vision for the realtime renderer: the client posts the actual on-screen video
# frame; the engine analyzes it and re-grounds the simulation so it tracks the
# video instead of drifting from the still. See engine.api_observe.
app.add_url_rule('/api/observe', 'standalone_api_observe', engine.api_observe, methods=['POST'])
# Realtime object recognition for the SCAN tool: the client posts the on-screen
# video frame; the engine returns the prominent, interactable objects visible in
# it plus their positions so the UI can float "starfield" tags. Stateless /
# read-only (does not mutate the sim). See engine.api_detect.
app.add_url_rule('/api/detect', 'standalone_api_detect', engine.api_detect, methods=['POST'])
# Realtime danger grading for the peripheral-vignette / health system: the
# client posts the on-screen video frame at ~1 Hz; the engine returns a single
# ordinal threat level (0 safe / 1 threatened / 2 attacking) for that frame.
# The level drives the client's danger state machine (SAFE → WARNING →
# HURTING) which pulses the red peripheral vignette and drains health when
# danger persists. Stateless / read-only. See engine.api_danger.
app.add_url_rule('/api/danger', 'standalone_api_danger', engine.api_danger, methods=['POST'])
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
                scene_prompt = str(_st.get('current_image_prompt') or "")
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
app.add_url_rule('/api/photo', 'standalone_api_photo', engine.api_photo, methods=['POST'])
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
app.add_url_rule('/api/talk/session', 'standalone_api_talk_session', engine.api_talk_session, methods=['POST'])
app.add_url_rule('/api/talk/message', 'standalone_api_talk_message', engine.api_talk_message, methods=['POST'])
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
    a flat '/images/<filename>' URL. Serve from the default session dir first,
    then fall back to the legacy root images/ dir, so web playthroughs display
    scene art instead of 404ing. Mirrors the path-traversal protection used by
    the session/archive image routes."""
    try:
        safe_filename = Path(filename).name
        candidates = [
            Path(engine._get_image_dir('default')) / safe_filename,  # standalone/web session
            Path("images") / safe_filename,                          # legacy/global fallback
        ]
        mimetype = 'image/gif' if safe_filename.lower().endswith('.gif') else 'image/png'
        for image_path in candidates:
            if image_path.exists():
                return send_file(str(image_path), mimetype=mimetype)
        return error_response("Image not found", code=404)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to serve image", str(e))


@app.route('/api/scene_audio', methods=['POST'])
def api_scene_audio():
    """Generate (or reuse a cached) scene-matched instrumental clip for a guide
    image and return its URL.

    The standalone UI posts the scene descriptor (`metadata.prompt`, already
    delivered with every `scene_image`) here; we render a short Lyria RealTime
    clip the client loops as an ambient score, re-scoring on each new scene.
    Degrades to `{ "audio_url": null }` whenever audio can't be produced (no
    GEMINI_API_KEY, SDK missing, or stream failure) so the client stays silent
    instead of erroring."""
    try:
        body = request.get_json(silent=True) or {}
        prompt = (body.get("prompt") or "").strip()
        session_id = body.get("session") or "default"
        if not prompt:
            return jsonify({"audio_url": None, "reason": "no_prompt"})
        result = scene_audio.get_scene_audio(prompt, session_id=session_id)
        if not result:
            return jsonify({"audio_url": None, "reason": "unavailable"})
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        # Never surface a hard error for a non-critical enhancement.
        return jsonify({"audio_url": None, "reason": "error", "details": str(e)})


@app.route('/audio/<filename>', methods=['GET'])
def serve_scene_audio(filename):
    """Serve generated scene audio WAVs (mirrors the /images route, with the
    same path-traversal protection)."""
    try:
        path = scene_audio.resolve_audio_path(filename, 'default')
        if path and path.exists():
            return send_file(str(path), mimetype='audio/wav')
        return error_response("Audio not found", code=404)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to serve audio", str(e))


if _sock is not None:
    @_sock.route('/ws/scene_music')
    def ws_scene_music(ws):
        """Realtime scene music stream (Lyria RealTime -> browser).

        The client opens this socket, may send an initial `{"prompt": ...}` steer
        message, then receives raw 16-bit/48kHz/stereo PCM binary frames. Sending
        a new `{"prompt": ...}` on each scene re-steers the score live. Opt-in via
        the standalone UI's ?music=stream flag."""
        try:
            first = ws.receive(timeout=5)
            initial_prompt = ""
            if first:
                try:
                    initial_prompt = (json.loads(first).get("prompt") or "").strip()
                except Exception:
                    initial_prompt = ""
            scene_audio.stream_music_over_ws(ws, initial_prompt)
        except Exception:
            traceback.print_exc()


def _standalone_asset_version():
    """Cache-bust CSS/JS on every deploy so browsers never serve stale UI.

    Covers the standalone immersive UI and the lobby landing page so an edit
    to either set of assets forces a fresh fetch."""
    candidates = [
        "static/css/standalone.css",
        "static/js/standalone.js",
        "static/js/reactor_renderer.js",
        "static/css/lobby.css",
        "static/js/lobby.js",
    ]
    latest = 0
    for path in candidates:
        try:
            latest = max(latest, os.path.getmtime(path))
        except Exception:
            pass
    return str(int(latest)) if latest else "0"


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
    """Splash / lobby page. Explains the world, shows recent runs the
    visitor can resume, and lets them start a fresh instance. Delegates
    session creation to /api/lobby/create and then routes the browser to
    /play?session=<id> where the immersive UI takes over."""
    return render_template(
        'lobby.html',
        asset_version=_standalone_asset_version(),
    )


@app.route('/api/tape', methods=['GET'])
def api_tape():
    """Ordered scene frames captured this session, for VHS tape playback.

    The standalone game already saves every canonical scene frame to the
    'default' session image directory; we just list them chronologically
    (by mtime) as servable /images/<file> URLs. Downsampled vision helper
    frames (*_small.png) and flipbook grids are excluded."""
    try:
        from pathlib import Path as _P
        img_dir = _P(engine._get_image_dir('default'))
        frames = []
        if img_dir.exists():
            files = [
                p for p in img_dir.glob('*.png')
                if not p.name.endswith('_small.png')
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
        key = (session_id, s.get("turn_count", 0), s.get("current_phase", "normal"), len(s.get("seen_elements") or []))
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
            "chaos": s.get("chaos_level", 0),
            "turn": s.get("turn_count", 0),
            "alive": s.get("player_state", {}).get("alive", True),
            "in_combat": s.get("in_combat", False),
            "time_of_day": s.get("time_of_day", ""),
            "inventory": inventory,
            "backend": ai_provider_manager.active_backend("chat"),
            "image_provider": ai_provider_manager.get_image_provider(),
            "image_model": ai_provider_manager.get_image_model(),
            "image_enabled": engine.IMAGE_ENABLED,
            # Renderer selection + the latest scene prompt, so the standalone
            # client can steer the Reactor realtime world model with the same
            # text used to generate the still image.
            "renderer": getattr(engine, "SCENE_RENDERER", "image"),
            "current_image_prompt": s.get("current_image_prompt", ""),
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
    for back-compat (the SDK name of the default model).
    """
    default_id = getattr(engine, "REACTOR_WORLD_MODEL", "lingbot-world-2")
    models = getattr(engine, "AVAILABLE_WORLD_MODELS", [])
    default_sdk = engine.world_model_sdk_name(default_id) if hasattr(engine, "world_model_sdk_name") \
        else os.getenv("REACTOR_MODEL", "reactor/lingbot-world-2")
    return jsonify({
        "enabled": bool(os.getenv("REACTOR_API_KEY")),
        "renderer": getattr(engine, "SCENE_RENDERER", "image"),
        "model_name": default_sdk,
        "world_model": default_id,
        "available_models": models,
        # When true the client may connect to ANY model name a tester types in,
        # even one not in available_models — so a newly shipped Reactor model is
        # usable the moment it exists, with no server change. It also tells the
        # client how to turn a bare id into an SDK name for custom models.
        "allow_custom_models": bool(getattr(engine, "REACTOR_ALLOW_CUSTOM_MODELS", True)),
        "sdk_name_prefix": "reactor/",
    })


@app.route('/api/reactor/token', methods=['POST'])
def api_reactor_token():
    """Mint a short-lived Reactor JWT by exchanging the server-side API key.

    Mirrors Reactor's documented auth flow: POST /tokens with the
    `Reactor-API-Key` header returns `{ "jwt": ..., "expires_at": ... }`. The
    API key stays on the server; only the short-lived token reaches the browser.
    """
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

    If ADMIN_TOKEN is unset (e.g. local dev without secrets) we leave the
    dashboard open. In production we strongly recommend setting it; the
    dashboard exposes session state and reset controls. The token can be
    supplied as `?token=...`, an `X-Admin-Token` header, or an `admin_token`
    cookie so the dashboard's existing fetch() calls keep working.
    """
    expected = os.getenv('ADMIN_TOKEN')
    if not expected:
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
            "no ELEVENLABS_API_KEY" if not _voice_design.API_KEY
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
_PRESET_UI_META = {
    "fal": {"label": "fal.ai Lightning", "latency": "~1-2s", "speed": 5,
            "blurb": "SDXL Lightning. Fastest possible — lower fidelity. Needs FAL_API_KEY."},
    "krea": {"label": "Krea 2 Medium", "latency": "~12s", "speed": 3,
             "blurb": "Default. Fast, strong quality, style-transfer continuity. Needs KREA_API_KEY."},
    "krea_large": {"label": "Krea 2 Large", "latency": "~24s", "speed": 2,
                   "blurb": "Higher quality / more textured. Needs KREA_API_KEY."},
    "gemini": {"label": "Gemini (Nano Banana)", "latency": "~15-30s", "speed": 2,
               "blurb": "High quality, multi-frame continuity. Needs GEMINI_API_KEY."},
    "openai": {"label": "OpenAI gpt-image-1", "latency": "~20-40s", "speed": 1,
               "blurb": "Highest fidelity, up to 16 reference images. Needs OPENAI_API_KEY."},
    "veo": {"label": "Veo video frames", "latency": "~30-60s", "speed": 1,
            "blurb": "Generates 8s video, extracts last frame. Natural consistency."},
    "anthropic": {"label": "Claude + Gemini", "latency": "~15-30s", "speed": 2,
                  "blurb": "Claude Opus narrative + Gemini images. Premium storytelling."},
}


def _preset_matches_current(preset_cfg, current):
    """A preset is 'active' when its image+text provider/model all match the
    live config (that's what set_preset writes)."""
    keys = ("image_provider", "image_model", "text_provider", "text_model")
    return all(preset_cfg.get(k) == current.get(k) for k in keys)


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
    presets = []
    active_name = None
    for name, cfg in ai_provider_manager.get_available_presets().items():
        meta = _PRESET_UI_META.get(name, {})
        is_active = _preset_matches_current(cfg, current)
        if is_active:
            active_name = name
        presets.append({
            "name": name,
            "label": meta.get("label", name.replace("_", " ").title()),
            "latency": meta.get("latency", ""),
            "speed": meta.get("speed", 0),
            "blurb": meta.get("blurb", cfg.get("description", "")),
            "text_provider": cfg.get("text_provider"),
            "text_model": cfg.get("text_model"),
            "image_provider": cfg.get("image_provider"),
            "image_model": cfg.get("image_model"),
            "active": is_active,
        })
    return {
        "status": "ok",
        "current": current,
        "active_preset": active_name,
        "presets": presets,
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
    ok = ai_provider_manager.set_preset(preset)
    if not ok:
        return {"status": "error", "error": f"Failed to switch to '{preset}'."}, 500
    return {
        "status": "ok",
        "preset": preset,
        "image_provider": ai_provider_manager.get_image_provider(),
        "image_model": ai_provider_manager.get_image_model(),
        "text_provider": ai_provider_manager.get_text_provider(),
        "text_model": ai_provider_manager.get_text_model(),
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
    """Switch the image/text model preset from the in-game menu (no auth)."""
    body = request.get_json(silent=True) or {}
    preset = body.get('preset') or request.args.get('preset')
    try:
        result, status = _ai_switch_result(preset)
        return jsonify(result), status
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to switch AI preset", str(e))


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
# INFO & HEALTH ENDPOINTS
# ═══════════════════════════════════════════════════════════════════

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
        "service": "SOMEWHERE Game Engine API"
    })


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
