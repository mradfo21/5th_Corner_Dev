"""
AI Provider Manager - Centralized configuration for AI models
Allows flexible switching between providers (OpenAI, Gemini) at runtime
"""

import json
import os
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional

ROOT = Path(__file__).parent.resolve()
AI_CONFIG_PATH = ROOT / "ai_config.json"
CONFIG_LOCK = threading.Lock()

# Cache the config in memory
_cached_config: Optional[Dict[str, Any]] = None
_cache_timestamp = 0

def load_ai_config() -> Dict[str, Any]:
    """Load AI configuration from file with caching."""
    global _cached_config, _cache_timestamp
    
    current_time = datetime.now(timezone.utc).timestamp()
    
    # Refresh cache every 5 seconds (allows hot-reloading)
    if _cached_config and (current_time - _cache_timestamp) < 5:
        return _cached_config
    
    with CONFIG_LOCK:
        try:
            with AI_CONFIG_PATH.open("r", encoding="utf-8") as f:
                config = json.load(f)
            _cached_config = config
            _cache_timestamp = current_time
            return config
        except FileNotFoundError:
            # Create default config if missing
            default_config = {
                "text_provider": "gemini",
                "text_model": "gemini-2.0-flash",
                "image_provider": "gemini",
                "image_model": "gemini-2.0-flash-exp-imagen",
                "last_updated": datetime.now(timezone.utc).isoformat()
            }
            save_ai_config(default_config)
            return default_config

def save_ai_config(config: Dict[str, Any]) -> None:
    """Save AI configuration to file."""
    global _cached_config, _cache_timestamp
    
    with CONFIG_LOCK:
        config["last_updated"] = datetime.now(timezone.utc).isoformat()
        with AI_CONFIG_PATH.open("w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        _cached_config = config
        _cache_timestamp = datetime.now(timezone.utc).timestamp()
        print(f"[AI CONFIG] Saved: {config['text_provider']}/{config['text_model']} (text), {config['image_provider']}/{config['image_model']} (image)")

def get_text_provider() -> str:
    """Get current text generation provider."""
    return load_ai_config().get("text_provider", "gemini")

def get_text_model() -> str:
    """Get current text generation model."""
    return load_ai_config().get("text_model", "gemini-2.0-flash")

def get_image_provider() -> str:
    """Get current image generation provider."""
    return load_ai_config().get("image_provider", "gemini")

def get_image_model() -> str:
    """Get current image generation model."""
    return load_ai_config().get("image_model", "gemini-2.0-flash-exp-imagen")

def set_preset(preset_name: str) -> bool:
    """
    Set AI configuration from a preset.
    
    Available presets:
    - gemini_fast: All Gemini (fastest, cheapest)
    - openai: All OpenAI (highest quality, expensive)
    - hybrid_fast: Gemini text + OpenAI images
    
    Returns True if successful, False if preset not found.
    """
    config = load_ai_config()
    presets = config.get("available_configs", {})
    
    if preset_name not in presets:
        print(f"[AI CONFIG] Preset '{preset_name}' not found!")
        return False
    
    preset = presets[preset_name]
    config.update(preset)
    save_ai_config(config)
    print(f"[AI CONFIG] ✅ Switched to preset: {preset_name}")
    return True

def set_custom(text_provider: str = None, text_model: str = None, 
               image_provider: str = None, image_model: str = None) -> None:
    """Set custom AI configuration."""
    config = load_ai_config()
    
    if text_provider:
        config["text_provider"] = text_provider
    if text_model:
        config["text_model"] = text_model
    if image_provider:
        config["image_provider"] = image_provider
    if image_model:
        config["image_model"] = image_model
    
    save_ai_config(config)

def get_status() -> str:
    """Get human-readable status of current AI configuration."""
    config = load_ai_config()
    
    text_emoji = "🤖" if config["text_provider"] == "openai" else "✨"
    image_emoji = "🎨" if config["image_provider"] == "openai" else "🖼️"
    
    status = (
        f"{text_emoji} **Text Generation**\n"
        f"  Provider: `{config['text_provider']}`\n"
        f"  Model: `{config['text_model']}`\n\n"
        f"{image_emoji} **Image Generation**\n"
        f"  Provider: `{config['image_provider']}`\n"
        f"  Model: `{config['image_model']}`\n\n"
        f"🕐 Last Updated: {config.get('last_updated', 'Unknown')}"
    )
    
    return status

def get_available_presets() -> Dict[str, Dict[str, str]]:
    """Get list of available presets."""
    config = load_ai_config()
    return config.get("available_configs", {})

# Initialize on import
load_ai_config()
print(f"[AI PROVIDER MANAGER] Initialized: {get_text_provider()}/{get_text_model()} (text), {get_image_provider()}/{get_image_model()} (image)")

