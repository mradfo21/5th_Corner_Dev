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

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes


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

app.add_url_rule('/api/reset', 'standalone_api_reset', engine.api_reset, methods=['POST'])
app.add_url_rule('/api/feed', 'standalone_api_feed', engine.api_feed, methods=['GET'])
app.add_url_rule('/api/choose', 'standalone_api_choose', engine.api_choose, methods=['POST'])
app.add_url_rule('/api/regenerate_choices', 'standalone_api_regenerate_choices', engine.api_regenerate_choices, methods=['POST'])
# Vision for the realtime renderer: the client posts the actual on-screen video
# frame; the engine analyzes it and re-grounds the simulation so it tracks the
# video instead of drifting from the still. See engine.api_observe.
app.add_url_rule('/api/observe', 'standalone_api_observe', engine.api_observe, methods=['POST'])


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


def _standalone_asset_version():
    """Cache-bust CSS/JS on every deploy so browsers never serve stale UI."""
    try:
        css = os.path.getmtime("static/css/standalone.css")
        js = os.path.getmtime("static/js/standalone.js")
        rjs = 0
        try:
            rjs = os.path.getmtime("static/js/reactor_renderer.js")
        except Exception:
            pass
        return str(int(max(css, js, rjs)))
    except Exception:
        return "0"


@app.route('/standalone', methods=['GET'])
def serve_standalone():
    """Serve the standalone immersive UI. Unlike engine.py's own '/' route,
    this does NOT reset the game on load — the player's in-progress session
    (if any) is preserved across page refreshes; use the Reset button (or
    POST /api/reset) to start over."""
    return render_template('standalone.html', asset_version=_standalone_asset_version())


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


@app.route('/api/status', methods=['GET'])
def api_status():
    """Lightweight state snapshot for the standalone UI's HUD. Does not
    call any LLM/image backend — pure read of the in-memory/disk state."""
    try:
        s = engine.state or {}

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
    """Advertise the realtime-renderer config to the client (no secrets)."""
    return jsonify({
        "enabled": bool(os.getenv("REACTOR_API_KEY")),
        "renderer": getattr(engine, "SCENE_RENDERER", "image"),
        "model_name": os.getenv("REACTOR_MODEL", "reactor/lingbot-world-2"),
        # Whether the client re-seeds the world model with each turn's fresh
        # guide still (set_image + blend). On by default so generated images
        # actually steer the live sim; set REACTOR_RESEED=0 to disable if it
        # causes visible jumps on a given model.
        "reseed": os.getenv("REACTOR_RESEED", "1").strip().lower() not in ("0", "false", "no", "off"),
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


@app.route('/api/reactor/health', methods=['GET'])
def api_reactor_health():
    """Honest, server-side realtime readiness probe.

    This exists because "is realtime working?" kept getting answered from
    `/api/reactor/config` (which only says a key EXISTS) — that is not the same
    as realtime actually playing. This endpoint verifies only what the server
    can truly verify:
      • an API key is configured, and
      • a short-lived token can really be minted (the auth path the browser
        depends on).

    It CANNOT confirm the browser will receive live WebRTC video frames — that
    depends on the client GPU/network and Reactor's live session, which only the
    in-page Realtime HUD can measure (decoded-frame fps). So `ready: true` here
    means "the server side is wired correctly", NOT "realtime is proven to
    play". `verifies_video` is always false on purpose; don't overclaim from it.
    """
    key = os.getenv("REACTOR_API_KEY")
    result = {
        "configured": bool(key),
        "renderer_default": getattr(engine, "SCENE_RENDERER", "image"),
        "model_name": os.getenv("REACTOR_MODEL", "reactor/lingbot-world-2"),
        "token_ok": False,
        "verifies_video": False,  # the server can never confirm client-side frames
        "note": ("Server-side readiness only. Live video is verified in the browser "
                 "by the Realtime HUD (measured fps), not here."),
    }
    if not key:
        result["ready"] = False
        result["detail"] = "REACTOR_API_KEY not set — realtime is disabled; the game shows stills."
        return jsonify(result), 200
    try:
        import requests
        resp = requests.post(
            REACTOR_TOKEN_URL,
            headers={"Reactor-API-Key": key},
            timeout=15,
        )
        result["token_ok"] = resp.status_code == 200
        if resp.status_code != 200:
            result["detail"] = f"token exchange HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        result["detail"] = f"token exchange error: {e}"
    result["ready"] = bool(result["token_ok"])
    return jsonify(result), 200

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
    """Root: send visitors straight to the playable game. Machine clients
    that want the JSON info blob (previously served here) can use
    /api/info instead, which has identical contents."""
    return redirect('/standalone')


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
