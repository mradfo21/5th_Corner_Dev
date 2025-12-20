"""
API Wrapper for SOMEWHERE Game Engine
Provides HTTP API around engine.py functions

Usage:
    python api.py  # Starts API server on port 5001
"""
from flask import Flask, request, jsonify
from flask_cors import CORS
import engine
import traceback
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
    
    Returns:
        JSON with image data (image_url, prologue, vision_dispatch)
    """
    try:
        result = engine.generate_intro_image_fast()
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
    print("=" * 70)
    print("SOMEWHERE Game Engine API")
    print("=" * 70)
    print("Starting API server on http://0.0.0.0:5001")
    print("API Info: http://localhost:5001/api/info")
    print("Health Check: http://localhost:5001/api/health")
    print("=" * 70)
    
    app.run(debug=True, host='0.0.0.0', port=5001)


