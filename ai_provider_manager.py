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
            print("[AI CONFIG] Loading ai_config.json...", flush=True)
            with AI_CONFIG_PATH.open("r", encoding="utf-8") as f:
                config = json.load(f)
            print("[AI CONFIG] Loaded successfully", flush=True)
            _cached_config = config
            _cache_timestamp = current_time
            return config
        except FileNotFoundError:
            # Create default config if missing
            print("[AI CONFIG] File not found, creating default...", flush=True)
            default_config = {
                "text_provider": "gemini",
                "text_model": "gemini-2.5-flash",
                "image_provider": "gemini",
                "image_model": "gemini-3-pro-image-preview",
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "available_configs": {
                    "gemini": {
                        "text_provider": "gemini",
                        "text_model": "gemini-2.5-flash",
                        "image_provider": "gemini",
                        "image_model": "gemini-3-pro-image-preview"
                    },
                    "openai": {
                        "text_provider": "openai",
                        "text_model": "gpt-4o-mini",
                        "image_provider": "openai",
                        "image_model": "gpt-image-1"
                    }
                }
            }
            try:
                save_ai_config(default_config)
                print("[AI CONFIG] Default config saved", flush=True)
            except Exception as e:
                print(f"[AI CONFIG WARN] Could not save default config: {e}", flush=True)
                # Continue anyway with in-memory config
                _cached_config = default_config
                _cache_timestamp = current_time
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

# Lazy initialization flag
_initialized = False

def _ensure_initialized():
    """Lazy initialization - only loads config when first accessed."""
    global _initialized
    if not _initialized:
        # Force initial load to create default config if needed
        try:
            config = load_ai_config()
            print(f"[AI PROVIDER MANAGER] Initialized: {config.get('text_provider')}/{config.get('text_model')} (text), {config.get('image_provider')}/{config.get('image_model')} (image)", flush=True)
            _initialized = True
        except Exception as e:
            print(f"[AI PROVIDER MANAGER] Error during initialization: {e}", flush=True)
            # Set to True anyway to avoid repeated errors
            _initialized = True

def get_text_provider() -> str:
    """Get current text generation provider."""
    _ensure_initialized()
    return load_ai_config().get("text_provider", "gemini")

def get_text_model() -> str:
    """Get current text generation model."""
    _ensure_initialized()
    return load_ai_config().get("text_model", "gemini-2.5-flash")

def get_image_provider() -> str:
    """Get current image generation provider."""
    _ensure_initialized()
    return load_ai_config().get("image_provider", "gemini")

def get_image_model() -> str:
    """Get current image generation model."""
    _ensure_initialized()
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
    print(f"[AI CONFIG] Switched to preset: {preset_name}")
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

    _text_emojis = {"openai": "🤖", "anthropic": "🟠"}
    text_emoji = _text_emojis.get(config["text_provider"], "✨")
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

# ═══════════════════════════════════════════════════════════════════
# OFFLINE / MOCK BACKEND + UNIFIED CALL API
#
# Additive extension (ported from SOMEWHERE_StoryGen's providers.py
# harness, adapted into this module instead of replacing it). None of
# this changes the existing preset/config behavior above — it only
# activates when something explicitly opts in via set_backend_override()
# or the STORYGEN_BACKEND env var. Production engine.py / choices.py /
# bot.py / admin / session API are unaffected unless they choose to call
# into chat()/vision()/generate_image() or check active_backend().
# ═══════════════════════════════════════════════════════════════════

import requests as _requests

# Explicit override set at runtime (e.g. by run_local.py --mock, or tests).
# None means "no override - use ai_config.json as normal".
_backend_override: Optional[str] = None

# Legacy/OpenAI-style model names -> the model this codebase actually uses.
# Lets old call sites that still pass "gpt-4o" etc. keep working under
# whichever provider is actually configured.
MODEL_MAP: Dict[str, str] = {
    "gpt-4o": "gemini-2.5-flash",
    "gpt-4o-mini": "gemini-2.5-flash",
    "gpt-4o-vision": "gemini-2.5-flash",
    "gpt-4-vision-preview": "gemini-2.5-flash",
    "gpt-image-1": "gemini-2.5-flash-image",
    "dall-e-3": "gemini-2.5-flash-image",
}

# Deterministic, offline responses used by the mock backend so tests and
# `run_local.py --mock` never touch the network and never depend on API keys.
_MOCK_CHOICE_LINES = [
    "Sprint toward the treeline",
    "Pry open the rusted door",
    "Crouch low and scan the area",
]

_MOCK_NARRATIVE = (
    "The dust settles. Somewhere in the distance, metal groans against metal. "
    "You are alive, and outdoors, with the horizon stretching out before you."
)


def set_backend_override(provider: Optional[str]) -> None:
    """Force a specific backend ("mock", "gemini", "openai", "anthropic", ...)
    regardless of ai_config.json. Pass None to clear the override and resume
    reading from config.

    This is the main entry point for offline/hermetic test runs and for
    `run_local.py --mock`.
    """
    global _backend_override
    _backend_override = provider
    if provider:
        print(f"[AI PROVIDER MANAGER] Backend override ENABLED: '{provider}'", flush=True)
    else:
        print("[AI PROVIDER MANAGER] Backend override cleared - using ai_config.json", flush=True)


def get_backend_override() -> Optional[str]:
    """Return the current override, or None if there isn't one."""
    return _backend_override


def active_backend(kind: str = "chat") -> str:
    """Resolve which backend is actually active for `kind` ("chat", "vision",
    or "image"). Precedence, highest first:

      1. set_backend_override(...)         (explicit runtime override)
      2. STORYGEN_BACKEND env var          (process-level override)
      3. ai_config.json provider for kind  (normal operation)
    """
    if _backend_override:
        return _backend_override

    env_backend = os.environ.get("STORYGEN_BACKEND")
    if env_backend:
        return env_backend

    if kind == "image":
        return get_image_provider()
    return get_text_provider()


def is_mock_active(kind: str = "chat") -> bool:
    """Convenience check used by call sites that want to skip network work
    entirely (e.g. choices.py) instead of letting a real HTTP call fail."""
    return active_backend(kind) == "mock"


def resolve_model(model: Optional[str] = None, kind: str = "chat") -> str:
    """Translate a legacy/OpenAI-style model name via MODEL_MAP, or fall back
    to the configured default model for `kind` if none is given."""
    if model:
        return MODEL_MAP.get(model, model)
    return get_image_model() if kind == "image" else get_text_model()


def _flatten_messages(messages) -> str:
    """Turn an OpenAI-style messages list (or a plain string) into a single
    text prompt suitable for Gemini's `generateContent`."""
    if isinstance(messages, str):
        return messages
    parts = []
    for m in messages or []:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
            )
        parts.append(f"[{role}] {content}")
    return "\n".join(parts)


def _mock_chat_response(messages) -> str:
    """Deterministic offline chat response. If the prompt looks like a
    choice-generation request, return newline-separated choice lines;
    otherwise return a short fixed narrative line."""
    prompt_text = _flatten_messages(messages).lower()
    if "choice" in prompt_text or "choose" in prompt_text:
        return "\n".join(_MOCK_CHOICE_LINES)
    return _MOCK_NARRATIVE


def _mock_vision_response(prompt: str = "") -> str:
    """Deterministic offline vision response."""
    return "outdoors, alive, no immediate threats visible"


def _gemini_chat(messages, model: str, temperature: float, max_tokens: int) -> str:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return "Signal interrupted — GEMINI_API_KEY not configured."
    prompt = _flatten_messages(messages)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"thinkingConfig": {"thinkingBudget": 0}, "temperature": temperature, "maxOutputTokens": max_tokens},
    }
    try:
        resp = _requests.post(url, headers=headers, json=payload, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"[AI PROVIDER MANAGER] Gemini chat() error: {e}", flush=True)
        return "Signal interrupted..."


def _openai_chat(messages, model: str, temperature: float, max_tokens: int) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return "Signal interrupted — OPENAI_API_KEY not configured."
    try:
        import openai as _openai
        _client = _openai.OpenAI(api_key=api_key)
        normalized = messages if not isinstance(messages, str) else [{"role": "user", "content": messages}]
        resp = _client.chat.completions.create(
            model=model, messages=normalized, temperature=temperature, max_tokens=max_tokens
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[AI PROVIDER MANAGER] OpenAI chat() error: {e}", flush=True)
        return "Signal interrupted..."


def chat(messages, model: Optional[str] = None, temperature: float = 0.7, max_tokens: int = 200, **kwargs) -> str:
    """Unified text-completion call. `messages` may be a plain string or an
    OpenAI-style list of {"role", "content"} dicts.

    This is an independent, additive surface intended for the standalone
    harness, tests, and future call sites — engine.py's `_ask()` and
    choices.py's `generate_choices()` keep their own production-hardened
    implementations and are not rewired to use this function.
    """
    backend = active_backend("chat")
    if backend == "mock":
        return _mock_chat_response(messages)

    resolved_model = resolve_model(model, "chat")
    if backend == "openai":
        return _openai_chat(messages, resolved_model, temperature, max_tokens)
    # gemini, ollama-as-gemini-compatible, or unknown -> default to gemini
    return _gemini_chat(messages, resolved_model, temperature, max_tokens)


def vision(image_path: Optional[str] = None, image_data_b64: Optional[str] = None,
           prompt: str = "Describe this image.", model: Optional[str] = None, **kwargs) -> str:
    """Unified image->text call. Accepts either a filesystem path
    (`image_path`) or a pre-encoded base64 string (`image_data_b64`)."""
    backend = active_backend("vision")
    if backend == "mock":
        return _mock_vision_response(prompt)

    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return "Signal interrupted — GEMINI_API_KEY not configured."

    image_b64 = image_data_b64
    if image_b64 is None and image_path:
        try:
            import base64
            with open(image_path, "rb") as f:
                image_b64 = base64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            print(f"[AI PROVIDER MANAGER] vision() could not read image: {e}", flush=True)
            return "Signal interrupted — could not read image."

    resolved_model = resolve_model(model, "vision")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{resolved_model}:generateContent"
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    parts = [{"text": prompt}]
    if image_b64:
        parts.insert(0, {"inlineData": {"mimeType": "image/png", "data": image_b64}})
    payload = {"contents": [{"parts": parts}], "generationConfig": {"thinkingConfig": {"thinkingBudget": 0}, "temperature": 0.4, "maxOutputTokens": 150}}
    try:
        resp = _requests.post(url, headers=headers, json=payload, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"[AI PROVIDER MANAGER] Gemini vision() error: {e}", flush=True)
        return "Signal interrupted..."


def generate_image(prompt: str, caption: Optional[str] = None, model: Optional[str] = None, **kwargs) -> Optional[str]:
    """Unified text-to-image call. Returns a local image path/URL, or None.

    Best-effort passthrough for non-critical/offline use (tests, standalone
    experiments). Production turn-loop image generation continues to go
    through engine.py's `_gen_image`, which has the real continuity/retry
    logic this function intentionally does not duplicate.
    """
    backend = active_backend("image")
    if backend == "mock":
        return None
    try:
        import gemini_image_utils
        return gemini_image_utils.generate_with_gemini(
            prompt=prompt,
            caption=caption or "image",
            model=resolve_model(model, "image"),
        )
    except Exception as e:
        print(f"[AI PROVIDER MANAGER] generate_image() error: {e}", flush=True)
        return None


# Module loaded - lazy initialization avoids file I/O at import time
print("[AI PROVIDER MANAGER] Module loaded (lazy init - config loaded on first use)", flush=True)

