"""
Lore Cache Manager - Context caching for Gemini API
Supports text files and images with hot-reloading
"""

import json
import os
import time
import base64
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any

ROOT = Path(__file__).parent.resolve()
LORE_DIR = ROOT / "lore"
TEXT_DIR = LORE_DIR / "text"
IMAGES_DIR = LORE_DIR / "images"
CONFIG_PATH = LORE_DIR / "cache_config.json"

# Cache state
_cache_id: Optional[str] = None
_cache_expires_at: Optional[datetime] = None
_last_file_mtimes: Dict[str, float] = {}
_cache_lock = threading.Lock()
_cache_data: Dict[str, Any] = {}

def load_config() -> Dict[str, Any]:
    """Load lore cache configuration."""
    if not CONFIG_PATH.exists():
        return {
            "enabled": False,
            "ttl_hours": 2,
            "auto_refresh": True,
            "check_interval_seconds": 60,
            "file_order": []
        }
    
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def get_all_lore_files() -> List[Path]:
    """Get all lore files from text/ and images/ directories."""
    config = load_config()
    files = []
    
    if config.get("file_order"):
        # Use explicit order from config
        for relative_path in config["file_order"]:
            full_path = LORE_DIR / relative_path
            if full_path.exists():
                files.append(full_path)
    else:
        # Auto-discover: all text files, then all images
        if TEXT_DIR.exists():
            for file in sorted(TEXT_DIR.glob("*")):
                if file.suffix in [".md", ".txt"] and not file.name.startswith("EXAMPLE"):
                    files.append(file)
        
        if IMAGES_DIR.exists():
            for file in sorted(IMAGES_DIR.glob("*")):
                if file.suffix.lower() in [".png", ".jpg", ".jpeg", ".webp"]:
                    files.append(file)
    
    return files

def check_files_modified() -> bool:
    """Check if any lore files have been modified since last cache."""
    global _last_file_mtimes
    
    files = get_all_lore_files()
    current_mtimes = {str(f): f.stat().st_mtime for f in files}
    
    if not _last_file_mtimes:
        # First check
        _last_file_mtimes = current_mtimes
        return True
    
    # Compare modification times
    if current_mtimes != _last_file_mtimes:
        print(f"[LORE] Files modified, cache needs refresh")
        _last_file_mtimes = current_mtimes
        return True
    
    return False

def build_cache_contents() -> List[Dict[str, Any]]:
    """Build contents array for Gemini cache API (text + images)."""
    parts = []
    
    # Add framing instruction at the beginning
    framing_instruction = """# WORLD LORE - HISTORICAL BACKGROUND CONTEXT

CRITICAL FRAMING:
This cached content contains HISTORICAL BACKGROUND INFORMATION about the world Jason Fleece is exploring.

HOW TO USE THIS LORE:
1. **Historical Context Only** - These are events, documents, and facts from the PAST
2. **Jason Doesn't Know This** - He must DISCOVER this information through exploration
3. **Environmental Storytelling** - Reveal lore through:
   - Documents found in the environment (memos, files, reports)
   - Overheard radio transmissions or recordings
   - NPC dialogue referencing past events
   - Physical evidence (abandoned equipment, dated materials, signage)
   - Graffiti, notes, or warnings left by previous occupants

4. **Show, Don't Tell** - NEVER directly narrate backstory to the player
5. **Fragmented Discovery** - Reveal information gradually, piece by piece
6. **Maintain Mystery** - Some facts should remain unclear or contradictory
7. **Use for Worldbuilding** - Inform:
   - Why locations look the way they do
   - What equipment/facilities existed
   - Corporate culture and decisions
   - Timeline of events leading to current state
   - Character motivations and relationships

EXAMPLE - WRONG:
"Jason enters the facility. He knows from reports that Horizon Industries conducted experiments here in 1987..."

EXAMPLE - RIGHT:
"Jason enters Building C-7. A faded memo on the wall is dated March 1987. The Horizon Industries logo is barely visible, eaten by rust and time."

USE THE CACHED LORE TO:
- Generate authentic period-appropriate documents
- Create realistic facility layouts and naming conventions
- Inform NPC backstories and motivations
- Maintain internal consistency
- Design environmental clues

DO NOT:
- Info-dump backstory in narration
- Give Jason knowledge he hasn't discovered
- Break the first-person POV with omniscient information
- Contradict established lore

---

# HISTORICAL LORE DOCUMENTS:

"""
    
    parts.append({"text": framing_instruction})
    print(f"[LORE] Adding framing instruction ({len(framing_instruction)} chars)")
    
    files = get_all_lore_files()
    print(f"[LORE] Building cache from {len(files)} file(s)")
    
    for file_path in files:
        if file_path.suffix in [".md", ".txt"]:
            # Text file
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            parts.append({"text": f"\n\n=== {file_path.name} ===\n{content}\n"})
            print(f"[LORE]   + Text: {file_path.name} ({len(content)} chars)")
        
        elif file_path.suffix.lower() in [".png", ".jpg", ".jpeg", ".webp"]:
            # Image file
            with open(file_path, "rb") as f:
                image_data = base64.b64encode(f.read()).decode('utf-8')
            
            mime_type = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp"
            }.get(file_path.suffix.lower(), "image/png")
            
            parts.append({
                "inlineData": {
                    "mimeType": mime_type,
                    "data": image_data
                }
            })
            print(f"[LORE]   + Image: {file_path.name}")
    
    if len(parts) == 1:  # Only framing instruction, no actual lore
        print("[LORE] WARNING: No lore files found!")
        return []
    
    # Add closing instruction
    parts.append({"text": "\n\n--- END OF HISTORICAL LORE ---\n\nRemember: Use this to INFORM the world, not to NARRATE backstory. Jason discovers the truth through exploration."})
    
    # Wrap in message format
    return [{"role": "user", "parts": parts}]

def create_cache() -> Optional[str]:
    """Create a new context cache with Gemini API."""
    global _cache_id, _cache_expires_at, _cache_data
    
    config = load_config()
    if not config.get("enabled", False):
        print("[LORE] Cache disabled in config")
        return None
    
    contents = build_cache_contents()
    if not contents:
        print("[LORE] No contents to cache")
        return None
    
    try:
        import requests
        from engine import GEMINI_API_KEY
        
        ttl_seconds = config.get("ttl_hours", 2) * 3600
        
        # Create cache via REST API
        # Use gemini-2.0-flash (supports caching and is what we use for text generation)
        response = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/cachedContents",
            headers={
                "x-goog-api-key": GEMINI_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "model": "models/gemini-2.0-flash",
                "contents": contents,
                "ttl": f"{ttl_seconds}s",
                "displayName": "Simulation Lore"
            },
            timeout=30
        ).json()
        
        if "error" in response:
            print(f"[LORE] Cache creation failed: {response['error']}")
            return None
        
        cache_name = response.get("name")
        _cache_id = cache_name
        
        # Calculate expiration
        expire_time_str = response.get("expireTime")
        if expire_time_str:
            _cache_expires_at = datetime.fromisoformat(expire_time_str.replace("Z", "+00:00"))
        else:
            _cache_expires_at = datetime.now(timezone.utc) + timedelta(hours=config.get("ttl_hours", 2))
        
        # Store metadata
        _cache_data = {
            "cache_id": cache_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": _cache_expires_at.isoformat(),
            "file_count": len(get_all_lore_files()),
            "token_count": response.get("usageMetadata", {}).get("totalTokenCount", 0)
        }
        
        print(f"[LORE] [OK] Cache created: {cache_name}")
        print(f"[LORE]      Tokens: {_cache_data['token_count']}")
        print(f"[LORE]      Expires: {_cache_expires_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        
        return cache_name
    
    except Exception as e:
        print(f"[LORE] Cache creation error: {e}")
        import traceback
        traceback.print_exc()
        return None

def get_cache_id() -> Optional[str]:
    """
    Get current cache ID, creating LAZILY on first call.
    
    LAZY LOADING: Cache is NOT created until this function is called.
    This happens on the first AI generation during gameplay.
    
    Cost Model:
    - Bot idle = No cache = $0 storage cost
    - Player starts game = Cache created = ~$0.012/hour
    - Cache expires after TTL = Back to $0
    - Next player = Cache recreated
    """
    global _cache_id, _cache_expires_at
    
    with _cache_lock:
        config = load_config()
        if not config.get("enabled", False):
            return None
        
        # Check if cache expired
        if _cache_expires_at and datetime.now(timezone.utc) >= _cache_expires_at:
            print("[LORE] Cache expired, refreshing...")
            _cache_id = None
        
        # Check if files modified (if auto-refresh enabled)
        if config.get("auto_refresh", True) and check_files_modified():
            print("[LORE] Files modified, refreshing cache...")
            _cache_id = None
        
        # LAZY: Create cache only when needed (first gameplay turn)
        if not _cache_id:
            print("[LORE] [LAZY] First AI call detected - creating cache now (lazy loading)")
            _cache_id = create_cache()
            if _cache_id:
                print(f"[LORE] [COST] Storage cost: ~$0.012/hour until cache expires")
        
        return _cache_id

def get_cache_status() -> Dict[str, Any]:
    """Get human-readable cache status."""
    global _cache_data, _cache_expires_at
    
    config = load_config()
    files = get_all_lore_files()
    
    text_files = [f for f in files if f.suffix in [".md", ".txt"]]
    image_files = [f for f in files if f.suffix.lower() in [".png", ".jpg", ".jpeg", ".webp"]]
    
    status = {
        "enabled": config.get("enabled", False),
        "active": _cache_id is not None,
        "cache_id": _cache_id,
        "text_files": len(text_files),
        "image_files": len(image_files),
        "total_files": len(files),
        "token_count": _cache_data.get("token_count", 0),
        "expires_at": _cache_expires_at.isoformat() if _cache_expires_at else None,
        "time_remaining": None
    }
    
    if _cache_expires_at:
        remaining = _cache_expires_at - datetime.now(timezone.utc)
        status["time_remaining"] = str(remaining).split(".")[0]  # Remove microseconds
    
    return status

def refresh_cache() -> bool:
    """Force immediate cache refresh."""
    global _cache_id
    
    with _cache_lock:
        print("[LORE] Manual cache refresh requested")
        _cache_id = None
        check_files_modified()  # Update file mtimes
        new_cache = create_cache()
        return new_cache is not None

def save_config(config: Dict[str, Any]) -> None:
    """Save lore cache configuration to file."""
    global _last_file_mtimes
    
    with _cache_lock:
        with CONFIG_PATH.open("w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        print(f"[LORE CONFIG] Saved: enabled={config.get('enabled', False)}")
        # Reset file mtimes to force refresh check
        _last_file_mtimes = {}

def format_status_message() -> str:
    """Format cache status as Discord-friendly message."""
    status = get_cache_status()
    
    if not status["enabled"]:
        return "**Lore Cache: Disabled**\nEnable in `lore/cache_config.json`"
    
    if not status["active"]:
        return "**Lore Cache: Inactive**\n[X] No files found or cache creation failed"
    
    status_icon = "[OK]" if status["active"] else "[X]"
    
    msg = f"**Lore Cache Status**\n"
    msg += f"{status_icon} **Active**\n"
    msg += f"**Files:** {status['text_files']} text, {status['image_files']} images\n"
    msg += f"**Tokens:** {status['token_count']:,}\n"
    
    if status["time_remaining"]:
        msg += f"**Expires in:** {status['time_remaining']}\n"
    
    # Estimate cost
    if status["token_count"] > 0:
        cost_per_hour = (status["token_count"] / 1_000_000) * 1.00  # $1/M tokens/hour
        msg += f"**Storage cost:** ${cost_per_hour:.4f}/hour\n"
    
    if status["cache_id"]:
        cache_short = status["cache_id"].split("/")[-1][:16]
        msg += f"**Cache ID:** `{cache_short}...`"
    
    return msg

# Initialize on import (no cache created yet - LAZY LOADING)
print("[LORE CACHE MANAGER] Initialized (lazy mode - cache only created when first AI call is made)")
if load_config().get("enabled", False):
    print("[LORE CACHE MANAGER] [OK] Enabled - cache will be created on first turn (not now!)")
    print("[LORE CACHE MANAGER] [COST] $0 while idle, only charged when playing")
else:
    print("[LORE CACHE MANAGER] [WARN] Disabled - set 'enabled: true' in lore/cache_config.json")

