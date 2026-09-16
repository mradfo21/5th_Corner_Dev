"""Local API-key store — the product surface for bring-your-own-keys.

Keys the player pastes in the start-menu ACCOUNT pane live in a user-local
file (never the repo, never sessions/, never playtest output). They are
applied into ``os.environ`` so ``ai_provider_manager.available_model_catalogue``
and the existing provider call sites keep working without a second path.

This module never logs a key value, never returns one to a client, and
refuses to write a path inside the repo or a playtest tree.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

TITLE = "SOMEWHERE"

# Providers the live app already calls. Gemini is the default Play/Watch
# path (text + stills + Veo). The others unlock catalogue entries or
# optional layers (voice, realtime).
PROVIDERS: List[Dict[str, Any]] = [
    {
        "id": "gemini",
        "env": "GEMINI_API_KEY",
        "label": "Gemini",
        "blurb": "Play, Watch, and default images. The live path.",
        "roles": ("play", "watch", "image", "text", "voice-live"),
        "required": True,
    },
    {
        "id": "openai",
        "env": "OPENAI_API_KEY",
        "label": "OpenAI",
        "blurb": "Optional narrator.",
        "roles": ("play", "watch", "text"),
        "required": False,
    },
    {
        "id": "anthropic",
        "env": "ANTHROPIC_API_KEY",
        "label": "Anthropic",
        "blurb": "Optional Claude narrator.",
        "roles": ("play", "watch", "text"),
        "required": False,
    },
    {
        "id": "krea",
        "env": "KREA_API_KEY",
        "label": "Krea",
        "blurb": "Optional stills. Style-transfer continuity.",
        "roles": ("image",),
        "required": False,
    },
    {
        "id": "reactor",
        "env": "REACTOR_API_KEY",
        "label": "Reactor",
        "blurb": "Optional live video renderer.",
        "roles": ("realtime",),
        "required": False,
    },
    {
        "id": "elevenlabs",
        "env": "ELEVENLABS_API_KEY",
        "label": "ElevenLabs",
        "blurb": "Voice, music, world sound, and talk agents. Paste the sk_ secret, not the key ID from the dashboard list.",
        "roles": ("voice", "music", "sound"),
        "required": False,
    },
]

PLAY_KEY_ENVS = ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")

_ENV_BY_ID = {p["id"]: p["env"] for p in PROVIDERS}
_KNOWN_ENV = {p["env"] for p in PROVIDERS}

# Module-level caches that were read once at import. After a live save we
# write the same value here so Play/Watch do not need a process restart.
_RUNTIME_ATTRS = (
    ("engine", "GEMINI_API_KEY", "GEMINI_API_KEY"),
    ("engine", "OPENAI_API_KEY", "OPENAI_API_KEY"),
    ("engine", "ELEVENLABS_API_KEY", "ELEVENLABS_API_KEY"),
    ("gemini_image_utils", "GEMINI_API_KEY", "GEMINI_API_KEY"),
    ("krea_image_utils", "KREA_API_KEY", "KREA_API_KEY"),
    ("fal_image_utils", "FAL_API_KEY", "FAL_API_KEY"),
    ("scene_audio", "ELEVENLABS_API_KEY", "ELEVENLABS_API_KEY"),
    ("veo_video_utils", "GEMINI_API_KEY", "GEMINI_API_KEY"),
    ("voice_design", "API_KEY", "ELEVENLABS_API_KEY"),
)

_LOCK = threading.Lock()
_store_path_override: Optional[Path] = None
_explicit_mock = False
_applied_from_store: set[str] = set()

_MIN_KEY_LEN = 8
_MAX_KEY_LEN = 512


def mark_explicit_mock() -> None:
    """Caller asked for --mock. Saving a key must not silently go live."""
    global _explicit_mock
    _explicit_mock = True


def is_explicit_mock() -> bool:
    return _explicit_mock


def set_store_path(path: Optional[os.PathLike | str]) -> None:
    """Tests point the store at a temp file. Production never calls this."""
    global _store_path_override
    _store_path_override = Path(path) if path else None


def default_store_path() -> Path:
    """User-local file, outside the repo and outside playtest output.

    Override with SOMEWHERE_KEYS_PATH (tests / unusual installs).
    """
    if _store_path_override is not None:
        return _store_path_override
    raw = (os.environ.get("SOMEWHERE_KEYS_PATH") or "").strip()
    if raw:
        return Path(raw)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / TITLE / "keys.env"
    return Path.home() / ".somewhere" / "keys.env"


def store_path() -> Path:
    return default_store_path()


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def _is_forbidden_store_path(path: Path) -> bool:
    """Refuse to persist secrets where they would be committed or published."""
    try:
        resolved = path.resolve()
    except OSError:
        return True
    root = _repo_root()
    try:
        resolved.relative_to(root)
    except ValueError:
        return False
    # Inside the repo is only OK if it is clearly not source / artifacts.
    # We never want keys.env next to play.py or under playtest_results/.
    name = resolved.name.lower()
    if name in (".env", "keys.env") and resolved.parent == root:
        return True
    parts = {p.lower() for p in resolved.parts}
    if parts & {"playtest_results", "sessions", "dist", "build", ".git"}:
        return True
    return True  # any other in-repo path: no


def _read_env_file(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in _KNOWN_ENV and value:
            out[key] = value
    return out


def _atomic_write_env(path: Path, values: Dict[str, str]) -> None:
    if _is_forbidden_store_path(path):
        raise ValueError("refusing to write API keys inside the project tree")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# SOMEWHERE local keys — written by the app. Do not commit.",
        "# This file stays on this machine. It is never sent to a hosted server.",
    ]
    for provider in PROVIDERS:
        env = provider["env"]
        val = (values.get(env) or "").strip()
        if val:
            lines.append(f"{env}={val}")
    payload = "\n".join(lines) + "\n"
    fd, tmp = tempfile.mkstemp(prefix="keys.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_into_environ(path: Optional[Path] = None) -> Path:
    """Overlay keys.env onto os.environ. Returns the path that was read."""
    global _applied_from_store
    target = path or store_path()
    loaded = _read_env_file(target)
    with _LOCK:
        _applied_from_store = set(loaded)
        for env, value in loaded.items():
            os.environ[env] = value
    refresh_runtime_keys(only=set(loaded))
    return target


def has_play_key() -> bool:
    return any(os.environ.get(k) for k in PLAY_KEY_ENVS)


def has_provider_key(provider_id: str) -> bool:
    """True when this provider's secret is live in the process or the store.

    ACCOUNT can show a key as green (file + environ) while a later getenv
    on another path misses it — empty process env, a worker that started
    before the save, a cached ``enabled: false``. Re-read the store and
    push the value into environ so Reactor and the keys lamp agree.
    """
    spec = provider_by_id(provider_id)
    if not spec:
        return False
    env = spec["env"]
    if (os.environ.get(env) or "").strip():
        return True
    try:
        stored = _read_env_file(store_path())
    except Exception:
        return False
    val = (stored.get(env) or "").strip()
    if not val:
        return False
    os.environ[env] = val
    try:
        refresh_runtime_keys(only={env})
    except Exception:
        pass
    return True


def mask_hint(value: str) -> str:
    """Last four characters only, and only when the secret is long enough."""
    secret = (value or "").strip()
    if len(secret) <= 8:
        return "set"
    return secret[-4:]


def _validate_value(raw: str, provider_id: str = "") -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError("invalid key")
    if len(value) < _MIN_KEY_LEN or len(value) > _MAX_KEY_LEN:
        raise ValueError("invalid key")
    if provider_id == "elevenlabs":
        if value.startswith("sk_"):
            return value
        if value.startswith("agent_"):
            raise ValueError(
                "that's an agent id. ElevenLabs API keys start with sk_ — "
                "shown only when you create or rotate a key."
            )
        if len(value) in (32, 64) and all(c in "0123456789abcdefABCDEF" for c in value):
            raise ValueError(
                "that's the key ID from the dashboard list, not the key. "
                "Create or rotate a key and copy the sk_ secret."
            )
        raise ValueError("ElevenLabs API keys start with sk_")
    return value


def provider_by_id(provider_id: str) -> Optional[Dict[str, Any]]:
    for p in PROVIDERS:
        if p["id"] == provider_id:
            return p
    return None


def set_key(provider_id: str, value: str, path: Optional[Path] = None) -> Dict[str, Any]:
    """Set or clear one provider key. Empty value clears it.

    Updates the store file, os.environ, and import-time caches. Never
    returns the secret.
    """
    spec = provider_by_id(provider_id)
    if not spec:
        raise KeyError("unknown provider")
    env = spec["env"]
    cleaned = _validate_value(value, provider_id)
    target = path or store_path()
    with _LOCK:
        current = _read_env_file(target)
        if cleaned:
            current[env] = cleaned
            os.environ[env] = cleaned
            _applied_from_store.add(env)
        else:
            current.pop(env, None)
            os.environ.pop(env, None)
            _applied_from_store.discard(env)
        _atomic_write_env(target, current)
    if cleaned:
        refresh_runtime_keys(only={env})
    else:
        refresh_runtime_keys(blank={env})
    lifted = lift_auto_mock_if_possible()
    status = public_status(editable=True)
    status["lifted_mock"] = lifted
    return status


def refresh_runtime_keys(*, only: Optional[set] = None, blank: Optional[set] = None) -> None:
    """Push current environ values into modules that cached keys at import.

    Only touch the names we were asked to. A previous version wrote EVERY
    cached attr from getenv on every save, which blanked keys that still
    lived in config.json / process env because they were not in keys.env —
    including the ElevenLabs secret TALK needs to mint a signed URL.
    """
    blank = blank or set()
    for mod_name, attr, env_name in _RUNTIME_ATTRS:
        if only is not None and env_name not in only and env_name not in blank:
            continue
        mod = sys.modules.get(mod_name)
        if mod is None or not hasattr(mod, attr):
            continue
        if env_name in blank:
            setattr(mod, attr, "")
            continue
        value = (os.environ.get(env_name) or "").strip()
        if value:
            setattr(mod, attr, value)
    engine = sys.modules.get("engine")
    if engine is not None and hasattr(engine, "elevenlabs_key_problem"):
        try:
            engine._ELEVENLABS_KEY_PROBLEM = engine.elevenlabs_key_problem()
        except Exception:
            pass


def lift_auto_mock_if_possible() -> bool:
    """If we only mocked because keys were missing, go live now.

    Explicit ``--mock`` stays offline. A key saved on the start menu should
    not require quitting and relaunching.
    """
    if _explicit_mock:
        return False
    if not has_play_key():
        return False
    try:
        import ai_provider_manager
    except Exception:
        return False
    override = ai_provider_manager.get_backend_override()
    env_backend = os.environ.get("STORYGEN_BACKEND")
    if override != "mock" and env_backend != "mock":
        return False
    ai_provider_manager.set_backend_override(None)
    if env_backend == "mock":
        os.environ.pop("STORYGEN_BACKEND", None)
    engine = sys.modules.get("engine")
    if engine is not None:
        engine.LLM_ENABLED = True
        engine.IMAGE_ENABLED = True
        if hasattr(engine, "WORLD_IMAGE_ENABLED"):
            engine.WORLD_IMAGE_ENABLED = True
    return True


def public_status(*, editable: bool) -> Dict[str, Any]:
    """Presence only. Never includes a raw key."""
    stored = _read_env_file(store_path()) if editable else {}
    providers = []
    for spec in PROVIDERS:
        env = spec["env"]
        present = bool(os.environ.get(env))
        source = "none"
        if present and env in stored:
            source = "app"
        elif present:
            source = "env"
        hint = ""
        if present and editable:
            hint = mask_hint(os.environ.get(env, ""))
        problem = ""
        usable = present
        if spec["id"] == "elevenlabs" and present:
            val = (os.environ.get(env) or "").strip()
            if not val.startswith("sk_"):
                usable = False
                if val.startswith("agent_"):
                    problem = "that's an agent id, not the sk_ secret"
                elif len(val) in (32, 64) and all(c in "0123456789abcdefABCDEF" for c in val):
                    problem = "that's the key ID, not the sk_ secret"
                else:
                    problem = "ElevenLabs keys start with sk_"
        providers.append({
            "id": spec["id"],
            "label": spec["label"],
            "blurb": spec["blurb"],
            "roles": list(spec["roles"]),
            "required": bool(spec["required"]),
            "set": present,
            "usable": usable,
            "problem": problem,
            "source": source,
            "hint": hint,
        })
    play_ready = has_play_key()
    return {
        "editable": bool(editable),
        "play_ready": play_ready,
        "offline": (not play_ready) or (
            sys.modules.get("ai_provider_manager")
            and sys.modules["ai_provider_manager"].is_mock_active("chat")
        ),
        "store": "local account file" if editable else "host",
        "providers": providers,
    }


def iter_provider_ids() -> Iterable[str]:
    return (p["id"] for p in PROVIDERS)
