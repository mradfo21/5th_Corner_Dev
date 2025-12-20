"""
SOMEWHERE - Flask Website
Analog Horror Landing Page
"""
from flask import Flask, render_template, jsonify
from pathlib import Path
import json
from datetime import datetime

app = Flask(__name__)

# Path to bot root (for accessing bot data)
BOT_ROOT = Path(__file__).parent.parent
STATE_FILE = BOT_ROOT / "state.json"
CACHE_DIR = BOT_ROOT / "cache"


@app.route('/')
def home():
    """Main landing page"""
    stats = get_live_stats()
    return render_template('index.html', stats=stats)


@app.route('/api/stats')
def api_stats():
    """API endpoint for live stats (AJAX updates)"""
    stats = get_live_stats()
    return jsonify(stats)


def get_live_stats():
    """
    Pull live stats from bot's data
    Returns real data if available, placeholder if not
    """
    stats = {
        'total_deaths': 0,
        'total_frames': 0,
        'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'status': 'offline'
    }
    
    # Try to read bot's state file
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                state = json.load(f)
                stats['status'] = 'online'
                stats['total_frames'] = state.get('frame_idx', 0)
                # Could track deaths in a separate file
                # For now, just show frame count
    except Exception as e:
        print(f"Could not read bot state: {e}")
    
    return stats


def get_example_images():
    """
    Get example images from bot's cache
    Returns list of image paths
    """
    try:
        if CACHE_DIR.exists():
            images = sorted(
                CACHE_DIR.glob("*.png"),
                key=lambda p: p.stat().st_mtime,
                reverse=True
            )
            return [img.name for img in images[:4]]  # Latest 4
    except Exception as e:
        print(f"Could not read cache: {e}")
    
    return []


@app.context_processor
def inject_globals():
    """Make these available to all templates"""
    return {
        'site_name': 'SOMEWHERE',
        'tagline': 'An Analog Horror Story',
        'discord_invite': 'YOUR_DISCORD_INVITE_LINK_HERE',  # UPDATE THIS
        'year': datetime.now().year
    }


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)

