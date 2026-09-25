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
                "text_model": "gemini-3.1-flash-lite",
                "image_provider": "gemini",
                "image_model": "gemini-3.1-flash-lite-image",
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "available_configs": {
                    "gemini": {
                        "text_provider": "gemini",
                        "text_model": "gemini-3.1-flash-lite",
                        "image_provider": "gemini",
                        "image_model": "gemini-3.1-flash-lite-image"
                    },
                    "gemini_pro": {
                        "text_provider": "gemini",
                        "text_model": "gemini-3.1-flash-lite",
                        "image_provider": "gemini",
                        "image_model": "gemini-3-pro-image"
                    },
                    "krea": {
                        "text_provider": "gemini",
                        "text_model": "gemini-3.1-flash-lite",
                        "image_provider": "krea",
                        "image_model": "krea-2/medium"
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

def _openai_chosen() -> bool:
    """The player chose OpenAI in ACCOUNT and has its key.

    Then the game builds Gemini-format requests for everything (story and
    pictures) and provider_bridge answers them with OpenAI, so the getters
    below report the Gemini wire whatever ai_config.json says. What the
    player sees named is provider_bridge.describe(), not these.
    """
    try:
        import provider_bridge
        return provider_bridge.active()
    except Exception:
        return False


DEFAULT_TEXT_MODEL = "gemini-3.1-flash-lite"
DEFAULT_IMAGE_MODEL = "gemini-3.1-flash-lite-image"


def get_text_provider() -> str:
    """Get current text generation provider."""
    _ensure_initialized()
    if _openai_chosen():
        return "gemini"
    return load_ai_config().get("text_provider", "gemini")

def get_text_model() -> str:
    """Get current text generation model."""
    _ensure_initialized()
    cfg = load_ai_config()
    if _openai_chosen() and cfg.get("text_provider", "gemini") != "gemini":
        return DEFAULT_TEXT_MODEL
    return cfg.get("text_model", DEFAULT_TEXT_MODEL)

def get_image_provider() -> str:
    """Get current image generation provider."""
    _ensure_initialized()
    if _openai_chosen():
        return "gemini"
    return load_ai_config().get("image_provider", "gemini")

def get_image_model() -> str:
    """Get current image generation model."""
    _ensure_initialized()
    cfg = load_ai_config()
    if _openai_chosen() and cfg.get("image_provider", "gemini") != "gemini":
        return DEFAULT_IMAGE_MODEL
    return cfg.get("image_model", DEFAULT_IMAGE_MODEL)


# The resolutions the image APIs accept. Anything else is rejected rather than
# passed through, because an unknown value fails inside the provider call where
# the error surfaces as a blank frame rather than a bad setting.
IMAGE_SIZES = ("1K", "2K", "4K")

# Aspect ratios Gemini (and Krea) actually accept on the wire. 21:9 is the
# closest official preset to a landscape phone (~19.5:9 / 20:9); we advertise
# that one as "phone horizontal" rather than inventing a ratio the APIs refuse.
# Live play still defaults to 4:3 when nothing is configured; Watch defaults
# to 16:9 on its own form (see render_jobs.options).
IMAGE_ASPECT_RATIOS = ("16:9", "21:9", "4:3", "1:1", "9:16")
PHONE_HORIZONTAL_ASPECT = "21:9"
_ASPECT_ALIASES = {
    "phone_horizontal": PHONE_HORIZONTAL_ASPECT,
    "phone-horizontal": PHONE_HORIZONTAL_ASPECT,
    "phone": PHONE_HORIZONTAL_ASPECT,
}


def get_image_size() -> str:
    """Output resolution for the image model — "1K", "2K" or "4K".

    Split from the model because they trade off independently: the same model
    at 4K is a different wait and a different bill from the same model at 1K,
    and a render is the one context where paying that is the point.
    """
    _ensure_initialized()
    size = str(load_ai_config().get("image_size", "1K") or "1K").upper()
    return size if size in IMAGE_SIZES else "1K"


def normalize_aspect_ratio(value: str) -> Optional[str]:
    """Map a form id or alias onto an API aspect ratio, or None if unknown."""
    raw = str(value or "").strip().lower().replace(" ", "_")
    if raw in _ASPECT_ALIASES:
        return _ASPECT_ALIASES[raw]
    # Accept "16:9" / "16-9" / "16x9" as the same id.
    compact = raw.replace("-", ":").replace("x", ":")
    for allowed in IMAGE_ASPECT_RATIOS:
        if compact == allowed.lower():
            return allowed
    return None


def get_image_aspect_ratio() -> str:
    """Output aspect ratio for stills — one of IMAGE_ASPECT_RATIOS.

    Unset or garbage reads as 4:3 so live play keeps the house style it has
    always used. Watch's form defaults to 16:9 independently.
    """
    _ensure_initialized()
    got = normalize_aspect_ratio(load_ai_config().get("aspect_ratio", "4:3"))
    return got or "4:3"


def aspect_ratio_options() -> list:
    """The tight list the Watch desk draws: id is what goes on the wire."""
    return [
        {"id": "16:9", "label": "16:9", "title": "Widescreen"},
        {"id": "21:9", "label": "PHONE",
         "title": "Phone horizontal · 21:9 landscape",
         "alias": "phone_horizontal"},
        {"id": "4:3", "label": "4:3", "title": "Classic"},
        {"id": "1:1", "label": "1:1", "title": "Square"},
        {"id": "9:16", "label": "9:16", "title": "Portrait"},
    ]


def model_catalogue(kind: str = "image") -> list:
    """Every model this build knows about, whether or not it can actually run
    right now — used to recognize/apply an id (find_model, apply_models) so a
    model already selected in ai_config.json is never "unknown" just because
    its key got unset later. Pickers should use available_model_catalogue()
    instead, which is the subset worth offering.

    Lives in ai_config.json rather than in code so adding a model is an edit to
    data: the id is what goes on the wire, and the provider rides along with it
    so choosing a model implies its backend and nothing has to be picked twice.
    """
    cat = load_ai_config().get("model_catalogue", {})
    entries = cat.get(kind, [])
    return [e for e in entries if isinstance(e, dict) and e.get("id")]


# Env var each provider needs its key from, for the ones that need one at all.
# Gemini has no entry — every build already needs GEMINI_API_KEY just to run
# the base game, so gating on it here would hide the whole catalogue instead
# of the handful of entries actually missing a key. Veo rides on the same
# Gemini credentials (it's a Google model), not a key of its own.
_PROVIDER_KEY_ENV: Dict[str, str] = {
    "krea": "KREA_API_KEY",
    "fal": "FAL_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "veo": "GEMINI_API_KEY",
}


def _provider_available(provider: str) -> bool:
    """Whether calling this provider would actually do something right now,
    rather than fail on the first turn for want of a key nobody's set."""
    env_var = _PROVIDER_KEY_ENV.get(provider)
    return bool(os.environ.get(env_var)) if env_var else True


def available_model_catalogue(kind: str = "image") -> list:
    """The models a picker should actually offer: model_catalogue(kind),
    minus entries whose provider needs a key that isn't configured.

    Mock mode is the one exception — nothing there calls a real API, so the
    whole catalogue is fair game for testing regardless of which keys are set.
    """
    entries = model_catalogue(kind)
    if is_mock_active(kind):
        return entries
    return [e for e in entries if _provider_available(e.get("provider", ""))]


def find_model(kind: str, model_id: str) -> Optional[Dict[str, Any]]:
    """The catalogue entry for `model_id`, or None if this build doesn't know it."""
    for entry in model_catalogue(kind):
        if entry.get("id") == model_id:
            return entry
    return None


def model_settings() -> Dict[str, Any]:
    """The values that decide what a frame costs and how good it looks."""
    return {
        "text_provider": get_text_provider(),
        "text_model": get_text_model(),
        "image_provider": get_image_provider(),
        "image_model": get_image_model(),
        "image_size": get_image_size(),
        "aspect_ratio": get_image_aspect_ratio(),
    }


def apply_models(image_model: str = None, image_size: str = None,
                 text_model: str = None, aspect_ratio: str = None) -> Dict[str, Any]:
    """Point the renderer at specific models. Returns the settings now in force.

    Providers are derived from the catalogue rather than accepted from the
    caller: a model belongs to exactly one backend, so asking for both is an
    invitation to send krea-2/large to Gemini. Unknown ids raise instead of
    being written, because the failure would otherwise land much later, inside
    a provider call, looking like a broken renderer.
    """
    config = load_ai_config()
    if image_model:
        entry = find_model("image", image_model)
        if not entry:
            raise ValueError(f"unknown image model: {image_model}")
        config["image_model"] = image_model
        config["image_provider"] = entry.get("provider") or config.get("image_provider")
    if image_size:
        size = str(image_size).upper()
        if size not in IMAGE_SIZES:
            raise ValueError(f"unknown image size: {image_size}")
        config["image_size"] = size
    if aspect_ratio:
        mapped = normalize_aspect_ratio(aspect_ratio)
        if not mapped:
            raise ValueError(f"unknown aspect_ratio: {aspect_ratio}")
        config["aspect_ratio"] = mapped
    if text_model:
        entry = find_model("text", text_model)
        if not entry:
            raise ValueError(f"unknown text model: {text_model}")
        config["text_model"] = text_model
        config["text_provider"] = entry.get("provider") or config.get("text_provider")
    save_ai_config(config)
    return model_settings()

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
# admin / session API are unaffected unless they choose to call
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
    "gpt-4o": "gemini-3.1-flash-lite",
    "gpt-4o-mini": "gemini-3.1-flash-lite",
    "gpt-4o-vision": "gemini-3.1-flash-lite",
    "gpt-4-vision-preview": "gemini-3.1-flash-lite",
    "gpt-image-1": "gemini-3.1-flash-lite-image",
    "dall-e-3": "gemini-3.1-flash-lite-image",
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


# Marker the identity-sheet filler puts in its prompt so mock vision can return
# a structured draft instead of the one-line scene caption.
IDENTITY_DRAFT_MARKER = "IDENTITY_DRAFT_JSON"

_MOCK_CHARACTER_DRAFT = {
    "name": "Mock Wren",
    "role": "field researcher",
    "appearance": "adult in a weathered coat, dark hair, standing on open ground",
    "wardrobe": "weathered field coat, dark trousers, scuffed boots",
    "signature_gear": "battered notebook",
    "demeanor": "quiet, watchful",
    "backstory": "Came outdoors to see what the horizon was hiding.",
}

_MOCK_SETTING_DRAFT = {
    "name": "The Open Ground",
    "summary": "Outdoors and alive, a wide stretch of ground with no immediate threats visible.",
    "landmarks": "The far horizon, a distant metal structure",
    "opening_shot": "A wide view of open ground under a pale sky.",
    "era": "present day",
    "palette": "dust, pale sky, worn metal",
}


def _mock_vision_response(prompt: str = "") -> str:
    """Deterministic offline vision response."""
    text = prompt or ""
    if IDENTITY_DRAFT_MARKER in text:
        draft = _MOCK_SETTING_DRAFT if "setting_reference" in text else _MOCK_CHARACTER_DRAFT
        return json.dumps(draft)
    return "outdoors, alive, no immediate threats visible"


def _gemini_chat(messages, model: str, temperature: float, max_tokens: int) -> str:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key and not _openai_chosen():
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
           prompt: str = "Describe this image.", model: Optional[str] = None,
           max_tokens: int = 150, **kwargs) -> str:
    """Unified image->text call. Accepts either a filesystem path
    (`image_path`) or a pre-encoded base64 string (`image_data_b64`)."""
    backend = active_backend("vision")
    if backend == "mock":
        return _mock_vision_response(prompt)

    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key and not _openai_chosen():
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
    token_cap = max(32, min(int(max_tokens or 150), 2048))
    payload = {"contents": [{"parts": parts}], "generationConfig": {"thinkingConfig": {"thinkingBudget": 0}, "temperature": 0.4, "maxOutputTokens": token_cap}}
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
        if backend == "krea":
            import krea_image_utils
            return krea_image_utils.generate_with_krea(
                prompt=prompt,
                caption=caption or "image",
                model=get_image_model(),
            )
        if backend == "fal":
            import fal_image_utils
            return fal_image_utils.generate_with_fal(
                prompt=prompt,
                caption=caption or "image",
            )
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

