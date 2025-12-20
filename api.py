"""
API Wrapper for SOMEWHERE Game Engine
Provides HTTP API around engine.py functions

Usage:
    python api.py  # Starts API server on port 5001
"""
from flask import Flask, request, jsonify, send_file, make_response, Response
from flask_cors import CORS
from functools import wraps
import engine
import traceback
import os
from typing import Optional, Dict, Any

app = Flask(__name__)

# CORS configuration (allow Discord and future frontends)
CORS(app, resources={
    r"/api/*": {
        "origins": ["*"],  # Restrictive in production
        "methods": ["GET", "POST", "PUT", "DELETE"],
        "allow_headers": ["Content-Type", "Authorization"]
    }
})

# ═══════════════════════════════════════════════════════════════════════════
# ADMIN DASHBOARD ACCESS & AUTHENTICATION
# ═══════════════════════════════════════════════════════════════════════════

# Admin token (set via environment variable in production)
ADMIN_TOKEN = os.getenv('ADMIN_TOKEN', 'dev-token-change-in-production')
ALLOWED_ORIGIN = os.getenv('ALLOWED_ORIGIN', '*')  # Set to your main site domain

def requires_admin_token(f):
    """Decorator to require admin token for dashboard access"""
    @wraps(f)
    def decorated(*args, **kwargs):
        # Check token from query param or header
        token = request.args.get('token') or request.headers.get('X-Admin-Token')
        
        if token != ADMIN_TOKEN:
            return jsonify({
                "success": False,
                "error": "Unauthorized",
                "message": "Valid admin token required"
            }), 401
        
        return f(*args, **kwargs)
    return decorated

# ═══════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def success_response(data: Any = None, message: str = "Success") -> Dict:
    """Standard success response format"""
    return {
        "success": True,
        "message": message,
        "data": data
    }

def error_response(error: str, details: str = None, code: int = 500) -> tuple:
    """Standard error response format"""
    response = {
        "success": False,
        "error": error,
        "details": details
    }
    return jsonify(response), code

# ═══════════════════════════════════════════════════════════════════════════
# GAME STATE ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/api/state', methods=['GET'])
def api_get_state():
    """
    Get current game state
    
    Query Parameters:
        session_id (optional): Session ID (defaults to 'default')
    
    Returns:
        JSON with game state (location, frame_idx, world_prompt, etc.)
    """
    try:
        session_id = request.args.get('session_id', 'default')
        state = engine.get_state(session_id)
        return jsonify(success_response(state, "State retrieved"))
    except Exception as e:
        return error_response("Failed to get state", str(e))


@app.route('/api/state/reload', methods=['POST'])
def api_reload_state():
    """
    Force reload state from disk (useful after external changes)
    
    Returns:
        JSON with reloaded state
    """
    try:
        engine.state = engine._load_state()
        state = engine.get_state()
        return jsonify(success_response(state, "State reloaded from disk"))
    except Exception as e:
        return error_response("Failed to reload state", str(e))


@app.route('/api/state/reset', methods=['POST'])
def api_reset_state():
    """
    Reset game state (new game)
    
    Body Parameters:
        session_id (optional): Session ID (defaults to 'default')
    
    Returns:
        Success message
    """
    try:
        data = request.json or {}
        session_id = data.get('session_id', 'default')
        engine.reset_state(session_id)
        return jsonify(success_response(None, f"State reset successful for session {session_id}"))
    except Exception as e:
        return error_response("Failed to reset state", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# GAME FLOW ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/api/game/intro', methods=['POST'])
def api_generate_intro():
    """
    Generate intro turn (full - image + choices)
    
    Body Parameters:
        session_id (optional): Session ID (defaults to 'default')
    
    Returns:
        JSON with intro data (prologue, image, choices, etc.)
    """
    try:
        data = request.json or {}
        session_id = data.get('session_id', 'default')
        result = engine.generate_intro_turn(session_id)
        return jsonify(success_response(result, "Intro generated"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to generate intro", str(e))


@app.route('/api/game/intro/image', methods=['POST'])
def api_generate_intro_image():
    """
    Generate intro image (Phase 1 - fast)
    
    Body Parameters:
        session_id (optional): Session ID (defaults to 'default')
    
    Returns:
        JSON with image data (image_url, prologue, vision_dispatch)
    """
    try:
        data = request.json or {}
        session_id = data.get('session_id', 'default')
        result = engine.generate_intro_image_fast(session_id)
        return jsonify(success_response(result, "Intro image generated"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to generate intro image", str(e))


@app.route('/api/game/intro/choices', methods=['POST'])
def api_generate_intro_choices():
    """
    Generate intro choices (Phase 2 - deferred)
    
    Body:
        {
            "image_url": "path/to/image.png",
            "prologue": "story text",
            "vision_dispatch": "vision description",
            "dispatch": "optional dispatch text"
        }
    
    Returns:
        JSON with choices
    """
    try:
        data = request.json
        image_url = data.get('image_url')
        prologue = data.get('prologue')
        vision_dispatch = data.get('vision_dispatch')
        dispatch = data.get('dispatch')
        
        result = engine.generate_intro_choices_deferred(
            image_url, prologue, vision_dispatch, dispatch
        )
        return jsonify(success_response(result, "Intro choices generated"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to generate intro choices", str(e))


@app.route('/api/game/action/image', methods=['POST'])
def api_advance_turn_image():
    """
    Process action - Phase 1: Generate consequence image (fast)
    
    Body:
        {
            "choice": "player action text",
            "fate": "LUCKY" | "NORMAL" | "UNLUCKY" (default: "NORMAL"),
            "is_timeout_penalty": false (default: false),
            "session_id": "default" (optional)
        }
    
    Returns:
        JSON with image data and consequences
    """
    try:
        data = request.json
        choice = data.get('choice')
        fate = data.get('fate', 'NORMAL')
        is_timeout_penalty = data.get('is_timeout_penalty', False)
        session_id = data.get('session_id', 'default')
        
        if not choice:
            return error_response("Missing required parameter: choice", code=400)
        
        result = engine.advance_turn_image_fast(choice, fate, is_timeout_penalty, session_id)
        return jsonify(success_response(result, "Action image generated"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to process action", str(e))


@app.route('/api/game/action/choices', methods=['POST'])
def api_advance_turn_choices():
    """
    Process action - Phase 2: Generate new choices (deferred)
    
    Body:
        {
            "consequence_img_url": "path/to/image.png",
            "dispatch": "narrative text",
            "vision_dispatch": "vision description",
            "choice": "player action that led here",
            "consequence_img_prompt": "image prompt used",
            "hard_transition": false (default: false),
            "session_id": "default" (optional)
        }
    
    Returns:
        JSON with new choices
    """
    try:
        data = request.json
        consequence_img_url = data.get('consequence_img_url')
        dispatch = data.get('dispatch')
        vision_dispatch = data.get('vision_dispatch')
        choice = data.get('choice')
        consequence_img_prompt = data.get('consequence_img_prompt', '')
        hard_transition = data.get('hard_transition', False)
        session_id = data.get('session_id', 'default')
        
        result = engine.advance_turn_choices_deferred(
            consequence_img_url,
            dispatch,
            vision_dispatch,
            choice,
            consequence_img_prompt,
            hard_transition,
            session_id
        )
        return jsonify(success_response(result, "Choices generated"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to generate choices", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# UTILITY ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/api/movement', methods=['GET'])
def api_get_movement_type():
    """
    Get last detected movement type
    
    Returns:
        JSON with movement_type (or null)
    """
    try:
        movement_type = engine.get_last_movement_type()
        return jsonify(success_response({
            "movement_type": movement_type
        }, "Movement type retrieved"))
    except Exception as e:
        return error_response("Failed to get movement type", str(e))


@app.route('/api/history', methods=['GET'])
def api_get_history():
    """
    Get game history (all turns)
    
    Returns:
        JSON with history array
    """
    try:
        history = engine.history if hasattr(engine, 'history') else []
        return jsonify(success_response({
            "history": history,
            "length": len(history)
        }, "History retrieved"))
    except Exception as e:
        return error_response("Failed to get history", str(e))


@app.route('/api/config', methods=['GET'])
def api_get_config():
    """
    Get current engine configuration
    
    Returns:
        JSON with config flags (IMAGE_ENABLED, QUALITY_MODE, etc.)
    """
    try:
        config = {
            "IMAGE_ENABLED": getattr(engine, 'IMAGE_ENABLED', True),
            "WORLD_IMAGE_ENABLED": getattr(engine, 'WORLD_IMAGE_ENABLED', True),
            "VEO_MODE_ENABLED": getattr(engine, 'VEO_MODE_ENABLED', False),
            "QUALITY_MODE": getattr(engine, 'QUALITY_MODE', True),
        }
        return jsonify(success_response(config, "Config retrieved"))
    except Exception as e:
        return error_response("Failed to get config", str(e))


@app.route('/api/config', methods=['POST'])
def api_set_config():
    """
    Update engine configuration
    
    Body:
        {
            "IMAGE_ENABLED": true,
            "QUALITY_MODE": false,
            etc.
        }
    
    Returns:
        Updated config
    """
    try:
        data = request.json
        
        # Update config values
        for key, value in data.items():
            if hasattr(engine, key):
                setattr(engine, key, value)
        
        # Return updated config
        config = {
            "IMAGE_ENABLED": getattr(engine, 'IMAGE_ENABLED', True),
            "WORLD_IMAGE_ENABLED": getattr(engine, 'WORLD_IMAGE_ENABLED', True),
            "VEO_MODE_ENABLED": getattr(engine, 'VEO_MODE_ENABLED', False),
            "QUALITY_MODE": getattr(engine, 'QUALITY_MODE', True),
        }
        return jsonify(success_response(config, "Config updated"))
    except Exception as e:
        return error_response("Failed to update config", str(e))


@app.route('/api/prompts/<prompt_key>', methods=['GET'])
def api_get_prompt(prompt_key):
    """
    Get a specific prompt from PROMPTS dictionary
    
    Returns:
        JSON with prompt text
    """
    try:
        prompts = getattr(engine, 'PROMPTS', {})
        prompt = prompts.get(prompt_key)
        
        if prompt is None:
            return error_response(f"Prompt '{prompt_key}' not found", code=404)
        
        return jsonify(success_response({
            "key": prompt_key,
            "value": prompt
        }, "Prompt retrieved"))
    except Exception as e:
        return error_response("Failed to get prompt", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# HEALTH & INFO
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/admin', methods=['GET'])
def serve_admin_dashboard():
    """
    Serve the admin dashboard with cross-origin support
    
    Access:
        - Direct: https://your-api.onrender.com/admin
        - iframe: Embed on your main site
    
    NOTE: NO AUTHENTICATION for testing/development
    TODO: Add authentication before production deployment!
    """
    try:
        response = make_response(send_file('admin_dashboard.html'))
        
        # CORS headers for cross-origin access (allow all origins for testing)
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        
        # Allow iframe embedding from any site (testing only!)
        response.headers['X-Frame-Options'] = 'ALLOWALL'
        
        return response
    except FileNotFoundError:
        return jsonify({
            "success": False,
            "error": "Dashboard file not found",
            "message": "admin_dashboard.html not found in project root"
        }), 404


@app.route('/api/health', methods=['GET'])
def api_health():
    """Health check endpoint"""
    return jsonify({
        "status": "ok",
        "service": "SOMEWHERE Game Engine API",
        "version": "1.0.0"
    })


@app.route('/api/info', methods=['GET'])
def api_info():
    """
    Get API information and available endpoints
    
    Returns:
        JSON with API documentation
    """
    endpoints = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint != 'static':
            endpoints.append({
                "path": str(rule),
                "methods": list(rule.methods - {'HEAD', 'OPTIONS'}),
                "endpoint": rule.endpoint
            })
    
    return jsonify({
        "service": "SOMEWHERE Game Engine API",
        "version": "1.0.0",
        "endpoints": endpoints
    })


# ═══════════════════════════════════════════════════════════════════════════
# SESSION MANAGEMENT (Minecraft-style world management)
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/api/sessions', methods=['POST'])
def api_create_session():
    """
    Create a new game session (like creating a Minecraft world)
    
    Body Parameters:
        name (optional): Session name
        description (optional): Session description
        session_id (optional): Custom session ID (auto-generated if not provided)
    
    Returns:
        JSON with session metadata
    """
    try:
        import uuid
        data = request.json or {}
        
        # Generate session ID if not provided
        session_id = data.get('session_id') or str(uuid.uuid4())
        name = data.get('name')
        description = data.get('description')
        
        # Validate session ID (security check)
        try:
            engine._validate_session_id(session_id)
        except ValueError as e:
            return error_response("Invalid session ID", str(e), 400)
        
        # Initialize the session (creates directories, state, metadata)
        engine.reset_state(session_id)
        
        # Update metadata with custom name/description if provided
        if name or description:
            meta = engine._update_session_metadata(
                session_id,
                name=name if name else f"Game Session {session_id[:8]}",
                description=description if description else ""
            )
        else:
            meta = engine._load_session_metadata(session_id)
        
        return jsonify(success_response(meta, f"Session '{session_id}' created")), 201
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to create session", str(e))


@app.route('/api/sessions', methods=['GET'])
def api_list_sessions():
    """
    List all available sessions (like Minecraft world list)
    
    Query Parameters:
        sort (optional): Sort by 'created' or 'accessed' (default: 'accessed')
        limit (optional): Maximum number of sessions to return
    
    Returns:
        JSON array of session metadata
    """
    try:
        sessions = engine.get_all_sessions()
        
        # Apply sorting
        sort_by = request.args.get('sort', 'accessed')
        if sort_by == 'created':
            sessions.sort(key=lambda s: s.get('created_at', ''), reverse=True)
        else:  # default to accessed
            sessions.sort(key=lambda s: s.get('last_accessed', ''), reverse=True)
        
        # Apply limit
        limit = request.args.get('limit', type=int)
        if limit and limit > 0:
            sessions = sessions[:limit]
        
        return jsonify(success_response(sessions, f"Found {len(sessions)} session(s)"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to list sessions", str(e))


@app.route('/api/sessions/<session_id>', methods=['GET'])
def api_get_session(session_id):
    """
    Get detailed information about a specific session
    
    Returns:
        JSON with session metadata + current state + history
    """
    try:
        # Load metadata
        meta = engine._load_session_metadata(session_id)
        
        # Load state
        state = engine.get_state(session_id)
        
        # Load history
        history = engine.get_history(session_id)
        
        # Combine into detailed session info
        session_info = {
            "metadata": meta,
            "state": state,
            "history_length": len(history),
            "history": history[-10:] if len(history) > 10 else history  # Last 10 entries
        }
        
        return jsonify(success_response(session_info, f"Session '{session_id}' details"))
    except Exception as e:
        traceback.print_exc()
        return error_response(f"Failed to get session '{session_id}'", str(e), 404)


@app.route('/api/sessions/<session_id>', methods=['DELETE'])
def api_delete_session(session_id):
    """
    Delete a session and all its data (like deleting a Minecraft world)
    
    WARNING: This is permanent and cannot be undone!
    
    Returns:
        JSON with deletion confirmation
    """
    try:
        result = engine.delete_session(session_id)
        return jsonify(success_response(result, f"Session '{session_id}' deleted"))
    except ValueError as e:
        return error_response("Cannot delete session", str(e), 400)
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to delete session", str(e))


@app.route('/api/sessions/<session_id>/status', methods=['GET'])
def api_session_status(session_id):
    """
    Get quick status of a session (lighter than full GET)
    
    Returns:
        JSON with basic session info (alive status, turn count, etc.)
    """
    try:
        meta = engine._load_session_metadata(session_id, create_if_missing=False)
        return jsonify(success_response(meta, f"Session '{session_id}' status"))
    except FileNotFoundError as e:
        return error_response(f"Session '{session_id}' not found", str(e), 404)
    except Exception as e:
        traceback.print_exc()
        return error_response(f"Error loading session '{session_id}'", str(e), 500)


@app.route('/api/sessions/<session_id>/history', methods=['GET'])
def api_get_session_history(session_id):
    """
    Get FULL history with all prompts, AI responses, and metadata for deep inspection
    
    Query Parameters:
        limit (optional): Maximum number of entries to return (default: all)
        offset (optional): Skip N entries from the start (for pagination)
    
    Returns:
        JSON with complete history entries including:
        - Turn number
        - Prompts (narrative, image, vision)
        - AI responses
        - Images generated
        - Choices presented
        - Player actions
        - Timestamps
        - Token usage (if available)
    """
    try:
        # Load full history
        history = engine.get_history(session_id)
        
        # Apply pagination if requested
        offset = request.args.get('offset', 0, type=int)
        limit = request.args.get('limit', type=int)
        
        total_entries = len(history)
        
        if offset > 0:
            history = history[offset:]
        
        if limit and limit > 0:
            history = history[:limit]
        
        # Build detailed inspection data
        inspection_data = {
            "session_id": session_id,
            "total_entries": total_entries,
            "returned_entries": len(history),
            "offset": offset,
            "history": history
        }
        
        return jsonify(success_response(inspection_data, f"History retrieved ({len(history)}/{total_entries} entries)"))
    except FileNotFoundError as e:
        return error_response(f"Session '{session_id}' not found", str(e), 404)
    except Exception as e:
        traceback.print_exc()
        return error_response(f"Error loading history for '{session_id}'", str(e), 500)


# ═══════════════════════════════════════════════════════════════════════════
# STATIC ASSET SERVING (Images, Tapes, Videos)
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/api/sessions/<session_id>/images/<filename>', methods=['GET'])
def api_serve_image(session_id, filename):
    """
    Serve a generated image from a session
    
    Security: Validates filename to prevent path traversal
    
    Returns:
        PNG image file
    """
    try:
        from flask import send_file
        from pathlib import Path
        import os
        
        # SECURITY: Strict validation - only allow simple filenames
        # No path separators, no parent directory references
        if not filename or '..' in filename or '/' in filename or '\\' in filename:
            return error_response("Invalid filename", "Path traversal not allowed", 400)
        
        # Only allow alphanumeric, dots, dashes, underscores
        import re
        if not re.match(r'^[a-zA-Z0-9._-]+$', filename):
            return error_response("Invalid filename", "Only alphanumeric characters allowed", 400)
        
        # Get image directory for session
        image_dir = engine._get_image_dir(session_id)
        image_path = image_dir / filename
        
        # Check if file exists
        if not image_path.exists():
            return error_response("Image not found", f"{filename} does not exist", 404)
        
        # Serve the file
        return send_file(str(image_path), mimetype='image/png')
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to serve image", str(e))


@app.route('/api/sessions/<session_id>/tapes', methods=['GET'])
def api_list_tapes(session_id):
    """
    List all available tapes for a session
    
    Returns:
        JSON list of tape filenames with metadata
    """
    try:
        from pathlib import Path
        
        tapes_dir = engine._get_video_dir(session_id) / 'films'
        
        if not tapes_dir.exists():
            return jsonify(success_response([], "No tapes directory found"))
        
        tapes = []
        for tape_file in tapes_dir.glob('*.gif'):
            tape_info = {
                'filename': tape_file.name,
                'size': tape_file.stat().st_size,
                'modified': tape_file.stat().st_mtime,
                'url': f'/api/sessions/{session_id}/tapes/{tape_file.name}'
            }
            tapes.append(tape_info)
        
        # Sort by modified time, newest first
        tapes.sort(key=lambda x: x['modified'], reverse=True)
        
        return jsonify(success_response(tapes, f"Found {len(tapes)} tapes"))
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to list tapes", str(e), 500)


@app.route('/api/sessions/<session_id>/tapes/<filename>', methods=['GET'])
def api_serve_tape(session_id, filename):
    """
    Serve a VHS tape (GIF animation) from a session
    
    Security: Validates filename to prevent path traversal
    
    Returns:
        GIF file
    """
    try:
        from flask import send_file
        from pathlib import Path
        import os
        
        # SECURITY: Strict validation - only allow simple filenames
        # No path separators, no parent directory references
        if not filename or '..' in filename or '/' in filename or '\\' in filename:
            return error_response("Invalid filename", "Path traversal not allowed", 400)
        
        # Only allow alphanumeric, dots, dashes, underscores
        import re
        if not re.match(r'^[a-zA-Z0-9._-]+$', filename):
            return error_response("Invalid filename", "Only alphanumeric characters allowed", 400)
        
        # Get tapes directory for session
        session_root = engine._get_session_root(session_id)
        tapes_dir = session_root / "tapes"
        tape_path = tapes_dir / filename
        
        # Check if file exists
        if not tape_path.exists():
            return error_response("Tape not found", f"{filename} does not exist", 404)
        
        # Serve the file
        return send_file(str(tape_path), mimetype='image/gif')
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to serve tape", str(e))


@app.route('/api/sessions/<session_id>/videos/<filename>', methods=['GET'])
def api_serve_video(session_id, filename):
    """
    Serve a video segment from a session (Veo mode)
    
    Security: Validates filename to prevent path traversal
    
    Returns:
        MP4 video file
    """
    try:
        from flask import send_file
        from pathlib import Path
        import os
        
        # SECURITY: Strict validation - only allow simple filenames
        # No path separators, no parent directory references
        if not filename or '..' in filename or '/' in filename or '\\' in filename:
            return error_response("Invalid filename", "Path traversal not allowed", 400)
        
        # Only allow alphanumeric, dots, dashes, underscores
        import re
        if not re.match(r'^[a-zA-Z0-9._-]+$', filename):
            return error_response("Invalid filename", "Only alphanumeric characters allowed", 400)
        
        # Get video directory for session
        video_dir = engine._get_video_dir(session_id)
        video_path = video_dir / filename
        
        # Check if file exists
        if not video_path.exists():
            return error_response("Video not found", f"{filename} does not exist", 404)
        
        # Serve the file
        return send_file(str(video_path), mimetype='video/mp4')
    except Exception as e:
        traceback.print_exc()
        return error_response("Failed to serve video", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# ERROR HANDLERS
# ═══════════════════════════════════════════════════════════════════════════

@app.errorhandler(404)
def not_found(e):
    return error_response("Endpoint not found", str(e), 404)


@app.errorhandler(500)
def internal_error(e):
    return error_response("Internal server error", str(e), 500)


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import os
    
    # Get port from environment (Render sets this) or default to 5001
    port = int(os.getenv('PORT', 5001))
    
    print("=" * 70)
    print("SOMEWHERE Game Engine API")
    print("=" * 70)
    print(f"Starting API server on http://0.0.0.0:{port}")
    print(f"API Info: http://localhost:{port}/api/info")
    print(f"Health Check: http://localhost:{port}/api/health")
    print(f"Dashboard: http://localhost:{port}/admin")
    print("=" * 70)
    
    # Use debug=False in production
    debug_mode = os.getenv('FLASK_ENV') != 'production'
    app.run(debug=debug_mode, host='0.0.0.0', port=port)


