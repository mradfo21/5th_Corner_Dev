"""Play on the provider the player chose in ACCOUNT: Gemini or OpenAI.

The game speaks Gemini. Every story, vision, choice, look-book and picture
call builds a Gemini ``generateContent`` request and posts it with
``requests`` (engine, choices, goal, look_book, gemini_image_utils,
evolve_prompt_file, ai_provider_manager — some twenty call sites). Rewriting
each of them for a second provider would fork every prompt path in the game.

So the choice is made once, here. When the player has chosen OpenAI (and has
an OpenAI key), a hook on ``requests.Session.request`` answers each Gemini
``generateContent`` POST with the OpenAI call that does the same job, and
hands the caller a Gemini-shaped response:

  * text, with or without images      -> /v1/chat/completions
    (a ``responseSchema`` becomes a JSON reply; the schema rides in the
    system message, because a Gemini schema is not a JSON Schema)
  * a picture (``responseModalities: IMAGE``) -> /v1/images/generations, or
    /v1/images/edits when the request carries reference images (identity
    plates, the previous frame), cropped to the aspect ratio asked for
  * spoken audio (dictation)           -> /v1/audio/transcriptions
  * a spoken line (``responseModalities: AUDIO``, speech.py) -> /v1/audio/speech,
    the Gemini voice mapped to the nearest OpenAI one (OPENAI_VOICE_FOR) and
    the delivery passed as ``instructions``

The same hook sits where cost_tracker already meters Gemini at the wire, so a
call site added later is covered without anyone remembering this file. With
Gemini chosen, nothing here runs.

Which models: the OpenAI key's own /v1/models list, matched against a
preference order (cheapest fast story model; the fast everyday image model).
The choice is stored with the provider in ``account.json`` beside
``keys.env``, in the user's own folder, never the install folder.

This module never logs or returns a key.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROVIDER_IDS = ("gemini", "openai")
PROVIDER_LABELS = {"gemini": "Gemini", "openai": "OpenAI"}

# Where to get a key. Opened in the player's browser from ACCOUNT.
KEY_PAGES = {
    "gemini": "https://aistudio.google.com/apikey",
    "openai": "https://platform.openai.com/api-keys",
}

OPENAI_API = "https://api.openai.com/v1"
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
_GEMINI_HOST = "generativelanguage.googleapis.com"

# Newest first. The first one the key can see is used. A model id that
# appears later than these still works if the player's account.json names it.
OPENAI_TEXT_PREFS = (
    "gpt-6-luna", "gpt-5.6-luna", "gpt-5.4-mini", "gpt-5-mini", "gpt-4.1-mini", "gpt-4o-mini",
)
OPENAI_IMAGE_PREFS = (
    "gpt-image-2.5-flare", "gpt-image-2", "gpt-image-1.5", "gpt-image-1-mini", "gpt-image-1",
)
OPENAI_TRANSCRIBE_PREFS = ("gpt-transcribe", "gpt-4o-mini-transcribe", "gpt-4o-transcribe", "whisper-1")
# The only OpenAI speech model that takes free-text direction (`instructions`);
# tts-1 / tts-1-hd read flat and know 9 of the 13 voices.
OPENAI_TTS_MODEL = "gpt-4o-mini-tts"

# Gemini's 30 voices onto OpenAI's 13, by gender and weight, so a character
# cast on Gemini keeps roughly their register on OpenAI. A designed voice
# (voice_…) has no OpenAI twin; it reads as cedar with its delivery as the
# instruction. OpenAI cannot design a voice from words (2026-09-25).
OPENAI_VOICE_FOR = {
    "Charon": "onyx", "Puck": "verse", "Fenrir": "ash", "Orus": "echo",
    "Enceladus": "cedar", "Iapetus": "echo", "Umbriel": "ballad", "Algieba": "cedar",
    "Algenib": "onyx", "Rasalgethi": "fable", "Alnilam": "ash", "Schedar": "echo",
    "Achird": "verse", "Zubenelgenubi": "ballad", "Sadachbia": "verse", "Sadaltager": "fable",
    "Zephyr": "nova", "Kore": "coral", "Leda": "shimmer", "Aoede": "marin",
    "Callirrhoe": "marin", "Autonoe": "nova", "Despina": "sage", "Erinome": "coral",
    "Laomedeia": "shimmer", "Achernar": "sage", "Gacrux": "coral", "Pulcherrima": "marin",
    "Vindemiatrix": "sage", "Sulafat": "marin",
}
OPENAI_DEFAULT_VOICE = "cedar"

# Picture quality for OpenAI. "medium" matches what Gemini Flash draws for
# the price; "low" is faster. SOMEWHERE_OPENAI_IMAGE_QUALITY overrides.
DEFAULT_IMAGE_QUALITY = "medium"

# Floors on the caller's timeout. The Gemini call sites were tuned for
# Gemini's speed; an OpenAI picture takes longer, and a caller that gives up
# at 30 s would throw away a picture that was about to arrive.
TEXT_TIMEOUT_FLOOR = 60.0
IMAGE_TIMEOUT_FLOOR = 150.0

_LOCK = threading.RLock()
_settings_cache: Optional[Dict[str, Any]] = None
_settings_mtime: float = -1.0
# Per model, parameters the API refused ("temperature", "reasoning_effort"
# ...). Learned on the first 400 and never sent to that model again.
_unsupported: Dict[str, set] = {}
_original_request = None


# ── settings: which provider, which models ─────────────────────────────

def settings_path() -> Path:
    import keys_store
    return keys_store.store_path().with_name("account.json")


def _read_settings() -> Dict[str, Any]:
    global _settings_cache, _settings_mtime
    path = settings_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _settings_cache, _settings_mtime = {}, -1.0
        return {}
    with _LOCK:
        if _settings_cache is not None and mtime == _settings_mtime:
            return dict(_settings_cache)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        _settings_cache, _settings_mtime = data, mtime
        return dict(data)


def _write_settings(data: Dict[str, Any]) -> None:
    global _settings_cache, _settings_mtime
    import keys_store
    path = settings_path()
    if keys_store._is_forbidden_store_path(path):
        raise ValueError("refusing to write account settings inside the project tree")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with _LOCK:
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        _settings_cache = dict(data)
        try:
            _settings_mtime = path.stat().st_mtime
        except OSError:
            _settings_mtime = -1.0


def chosen_provider() -> str:
    """What the player picked in ACCOUNT ("" if they never picked)."""
    p = str(_read_settings().get("provider") or "").strip().lower()
    return p if p in PROVIDER_IDS else ""


def _custom_active() -> bool:
    # The CUSTOM key (any OpenAI-compatible server) rides the OPENAI_API_KEY
    # slot; while it is set, that key is not an api.openai.com key.
    try:
        import keys_store
        return bool(keys_store.custom_status(editable=False).get("set"))
    except Exception:
        return False


def gemini_key() -> str:
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if key:
        return key
    try:
        import engine
        return str(getattr(engine, "GEMINI_API_KEY", "") or "").strip()
    except Exception:
        return ""


def openai_key() -> str:
    if _custom_active():
        return ""
    return (os.environ.get("OPENAI_API_KEY") or "").strip()


def provider_key(provider: str) -> str:
    return gemini_key() if provider == "gemini" else openai_key() if provider == "openai" else ""


def effective_provider() -> str:
    """The provider the game is playing on right now.

    Gemini is the default and the game's home: it plays unless the player
    picked OpenAI in ACCOUNT and has an OpenAI key. An OpenAI key on its own
    (a .env from before ACCOUNT had a dropdown, a narrator set in
    ai_config.json) changes nothing: the game runs exactly as it did, on
    ai_config's providers. "" when Gemini has no key and OpenAI was not
    chosen.
    """
    if chosen_provider() == "openai" and openai_key():
        return "openai"
    if gemini_key():
        return "gemini"
    return ""


def _mock_forced() -> bool:
    # is_mock_active() without asking ai_provider_manager's getters, which
    # ask active() (they would recurse).
    if (os.environ.get("STORYGEN_BACKEND") or "").strip() == "mock":
        return True
    try:
        import ai_provider_manager
        return ai_provider_manager.get_backend_override() == "mock"
    except Exception:
        return False


def active() -> bool:
    """True when Gemini-format calls are to be answered by OpenAI."""
    return effective_provider() == "openai" and not _mock_forced()


def can_call_gemini_api() -> bool:
    """Replaces `if not GEMINI_API_KEY` guards: a Gemini-format call will be
    answered, by Gemini with its key or by OpenAI through this bridge."""
    return bool(gemini_key()) or active()


def set_provider(provider: str) -> Dict[str, Any]:
    provider = str(provider or "").strip().lower()
    if provider not in PROVIDER_IDS:
        raise ValueError("choose Gemini or OpenAI")
    data = _read_settings()
    data["provider"] = provider
    _write_settings(data)
    return describe()


def _stored_models(provider: str) -> Dict[str, str]:
    m = _read_settings().get(provider) or {}
    return m if isinstance(m, dict) else {}


def openai_models() -> Dict[str, str]:
    """{"text": id, "image": id, "transcribe": id} for the OpenAI key."""
    m = _stored_models("openai")
    return {
        "text": str(m.get("text_model") or OPENAI_TEXT_PREFS[0]),
        "image": str(m.get("image_model") or OPENAI_IMAGE_PREFS[0]),
        "transcribe": str(m.get("transcribe_model") or OPENAI_TRANSCRIBE_PREFS[0]),
    }


def _pick(available: List[str], prefs: Tuple[str, ...]) -> str:
    have = set(available)
    for p in prefs:
        if p in have:
            return p
    return ""


def _remember(provider: str, **fields: Any) -> None:
    data = _read_settings()
    block = data.get(provider) if isinstance(data.get(provider), dict) else {}
    block.update({k: v for k, v in fields.items() if v is not None})
    data[provider] = block
    _write_settings(data)


def describe() -> Dict[str, Any]:
    """What ACCOUNT and the bottom-left backend tag show. No secrets."""
    provider = effective_provider()
    out: Dict[str, Any] = {
        "provider": provider,
        "chosen": chosen_provider(),
        "label": PROVIDER_LABELS.get(provider, ""),
        "text_model": "",
        "image_model": "",
        "providers": [],
    }
    mock = _mock_forced()
    if provider == "openai":
        m = openai_models()
        out["text_model"], out["image_model"] = m["text"], m["image"]
    elif provider == "gemini":
        try:
            import ai_provider_manager
            out["text_model"] = ai_provider_manager.get_text_model()
            out["image_model"] = ai_provider_manager.get_image_model()
            out["image_provider"] = ai_provider_manager.get_image_provider()
            out["text_provider"] = ai_provider_manager.get_text_provider()
        except Exception:
            pass
    out["mock"] = bool(mock)
    for pid in PROVIDER_IDS:
        check = _stored_models(pid).get("check") or {}
        out["providers"].append({
            "id": pid,
            "label": PROVIDER_LABELS[pid],
            "set": bool(provider_key(pid)),
            "key_page": KEY_PAGES[pid],
            "check": check if isinstance(check, dict) else {},
        })
    return out


def backend_label() -> str:
    """The bottom-left tag: the provider actually answering, in a word or two."""
    if _mock_forced():
        return "mock"
    d = describe()
    provider = d["provider"]
    if provider == "openai":
        return "openai"
    if provider == "gemini":
        text = d.get("text_provider") or "gemini"
        image = d.get("image_provider") or "gemini"
        if image == "krea":
            model = d.get("image_model") or ""
            tier = "large" if "large" in model else "medium" if "medium" in model else ""
            image = ("krea " + tier).strip()
        if text == image or (text == "gemini" and image == "gemini"):
            return "gemini"
        if text == "gemini":
            return "gemini + " + image
        return text + " + " + image
    try:
        import ai_provider_manager
        if openai_key() or (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
            # No Gemini key and OpenAI not chosen: the old path, ai_config's own.
            return ai_provider_manager.get_text_provider()
    except Exception:
        pass
    return "no key"


# ── the last thing the provider refused ─────────────────────────────────
# A key that runs out of credit mid-run otherwise shows up as nothing more
# than "Signal interrupted" prose and blank pictures. The HUD reads this
# through /api/status and says what happened and where to fix it.
_problem: Dict[str, Any] = {}


def plain_problem(provider: str, status: int, message: str) -> str:
    name = PROVIDER_LABELS.get(provider, provider or "The provider")
    low = (message or "").lower()
    if status == 429 and ("credit" in low or "quota" in low or "billing" in low):
        where = "platform.openai.com → Billing" if provider == "openai" else "aistudio.google.com → Billing"
        return f"{name} says this account is out of credit. Add some at {where}, or switch provider in ACCOUNT."
    if status == 429:
        return f"{name} is rate-limiting this key. Wait a moment, or switch provider in ACCOUNT."
    if status in (401, 403):
        return f"{name} turned this key down. Paste it again in ACCOUNT."
    return f"{name} answered {status}: {message[:160]}" if message else f"{name} answered {status}."


def note_problem(provider: str, status: int, message: str) -> None:
    if status in (401, 403, 429):
        _problem.clear()
        _problem.update(provider=provider, status=int(status), message=plain_problem(provider, int(status), message),
                        at=time.time())


def note_ok(provider: str) -> None:
    if _problem.get("provider") == provider:
        _problem.clear()


def current_problem(max_age_s: float = 600.0) -> Dict[str, Any]:
    if _problem and time.time() - float(_problem.get("at") or 0) <= max_age_s:
        return {k: _problem[k] for k in ("provider", "status", "message")}
    return {}


# ── checking a key for real ─────────────────────────────────────────────

def _err_text(resp: Any) -> str:
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            return str(err.get("message") or err.get("status") or "")[:240]
        if isinstance(err, str):
            return err[:240]
    except Exception:
        pass
    return (getattr(resp, "text", "") or "")[:240]


def check(provider: str) -> Dict[str, Any]:
    """Prove the stored key works: list its models and write one line.

    Returns {"ok", "message", "text_model", "image_model", "ms"} and keeps the
    result (without the key) in account.json so ACCOUNT can show it.
    """
    import requests
    provider = str(provider or "").strip().lower()
    key = provider_key(provider)
    t0 = time.time()
    result: Dict[str, Any] = {"ok": False, "message": "", "text_model": "", "image_model": "",
                              "at": int(time.time())}
    if provider not in PROVIDER_IDS:
        result["message"] = "Unknown provider."
        return result
    if not key:
        result["message"] = "No key yet."
        return result
    try:
        if provider == "openai":
            r = requests.get(OPENAI_API + "/models", headers={"Authorization": "Bearer " + key}, timeout=20)
            if r.status_code == 401:
                result["message"] = "OpenAI turned this key down. Copy it again from platform.openai.com."
            elif not r.ok:
                result["message"] = "OpenAI answered " + str(r.status_code) + ": " + _err_text(r)
            else:
                ids = [str(m.get("id")) for m in (r.json().get("data") or []) if isinstance(m, dict)]
                text = _pick(ids, OPENAI_TEXT_PREFS)
                image = _pick(ids, OPENAI_IMAGE_PREFS)
                transcribe = _pick(ids, OPENAI_TRANSCRIBE_PREFS)
                if not text:
                    result["message"] = "This key can't use any of the story models (" + ", ".join(OPENAI_TEXT_PREFS[:3]) + ")."
                else:
                    reply = _chat_completion(key, {
                        "model": text,
                        "messages": [{"role": "user", "content": "Reply with the single word: ready"}],
                        "max_completion_tokens": 400,
                        "reasoning_effort": _EFFORT_LADDER[0],
                    }, timeout=45)
                    said = ""
                    try:
                        said = (reply.get("choices") or [{}])[0].get("message", {}).get("content") or ""
                    except Exception:
                        said = ""
                    if reply.get("_status", 200) != 200:
                        result["message"] = plain_problem("openai", int(reply.get("_status")), str(reply.get("_error") or ""))
                        result["text_model"], result["image_model"] = text, image or ""
                        _remember("openai", text_model=text, image_model=image or None,
                                  transcribe_model=transcribe or None)
                    else:
                        result.update(ok=True, text_model=text, image_model=image or "",
                                      message="Works." + ("" if image else " This key can't draw pictures; the story will run without them."))
                        _remember("openai", text_model=text, image_model=image or None,
                                  transcribe_model=transcribe or None)
                        result["said"] = said.strip()[:40]
        else:
            r = requests.get(GEMINI_API + "/models", headers={"x-goog-api-key": key}, timeout=20)
            if r.status_code in (400, 401, 403):
                result["message"] = "Google turned this key down. Copy it again from aistudio.google.com."
            elif not r.ok:
                result["message"] = "Google answered " + str(r.status_code) + ": " + _err_text(r)
            else:
                import ai_provider_manager
                text = ai_provider_manager.get_text_model()
                if ai_provider_manager.get_text_provider() != "gemini":
                    text = "gemini-3.1-flash-lite"
                g = _raw_post(f"{GEMINI_API}/models/{text}:generateContent",
                                  headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                                  json={"contents": [{"parts": [{"text": "Reply with the single word: ready"}]}],
                                        "generationConfig": {"maxOutputTokens": 20,
                                                             "thinkingConfig": {"thinkingBudget": 0}}},
                                  timeout=30)
                if not g.ok:
                    result["message"] = plain_problem("gemini", g.status_code, _err_text(g))
                else:
                    result.update(ok=True, text_model=text,
                                  image_model=ai_provider_manager.get_image_model(), message="Works.")
    except requests.RequestException as e:
        result["message"] = "Couldn't reach " + PROVIDER_LABELS[provider] + ": " + type(e).__name__
    result["ms"] = int((time.time() - t0) * 1000)
    if result["ok"]:
        note_ok(provider)
    try:
        _remember(provider, check={k: result[k] for k in ("ok", "message", "text_model", "image_model", "at", "ms")})
    except Exception:
        pass
    return result


# ── the OpenAI side of a Gemini call ────────────────────────────────────

def _raw_post(url: str, **kwargs):
    """POST around this module's hook (a Gemini check while OpenAI is chosen
    must reach Google)."""
    import requests
    if _original_request is None:
        return requests.post(url, **kwargs)
    session = requests.Session()
    try:
        return _original_request(session, "POST", url, **kwargs)
    finally:
        session.close()


def _post_openai(key: str, path: str, *, json_body=None, data=None, files=None, timeout: float = 60.0):
    import requests
    fn = _original_request
    headers = {"Authorization": "Bearer " + key}
    if fn is None:
        return requests.post(OPENAI_API + path, headers=headers, json=json_body, data=data, files=files, timeout=timeout)
    session = requests.Session()
    try:
        return fn(session, "POST", OPENAI_API + path, headers=headers, json=json_body, data=data,
                  files=files, timeout=timeout)
    finally:
        session.close()


def _refused_param(model: str, resp: Any) -> str:
    """The parameter a 400 names, when the fix is to stop sending it."""
    try:
        err = resp.json().get("error") or {}
    except Exception:
        return ""
    param = str(err.get("param") or "")
    msg = str(err.get("message") or "")
    code = str(err.get("code") or "")
    if not param:
        m = re.search(r"[Uu]nsupported (?:parameter|value): '([a-z_\.\[\]]+)'", msg)
        param = m.group(1) if m else ""
    if not param:
        m = re.search(r"'([a-z_]+)' (?:is not supported|does not support)", msg)
        param = m.group(1) if m else ""
    if param and (code in ("unsupported_parameter", "unsupported_value", "invalid_value", "")
                  or "support" in msg.lower() or "unknown parameter" in msg.lower()):
        return param.split(".")[0].split("[")[0]
    return ""


# The story is a fast call: the least thinking each model allows. GPT-6 takes
# "none"; GPT-5 took "minimal"; older ones refuse the parameter outright.
_EFFORT_LADDER = ("none", "minimal", "low")
_effort: Dict[str, int] = {}


def _chat_completion(key: str, body: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    model = str(body.get("model") or "")
    for _ in range(6):
        sent = {k: v for k, v in body.items() if k not in _unsupported.get(model, set())}
        if "reasoning_effort" in sent:
            step = _effort.get(model, 0)
            sent["reasoning_effort"] = _EFFORT_LADDER[min(step, len(_EFFORT_LADDER) - 1)]
        r = _post_openai(key, "/chat/completions", json_body=sent, timeout=timeout)
        if r.status_code == 400:
            param = _refused_param(model, r)
            if param == "reasoning_effort" and "reasoning_effort" in sent:
                step = _effort.get(model, 0) + 1
                if step < len(_EFFORT_LADDER):
                    _effort[model] = step
                else:
                    _unsupported.setdefault(model, set()).add(param)
                continue
            if param and param in sent and param not in ("model", "messages"):
                _unsupported.setdefault(model, set()).add(param)
                continue
        if not r.ok:
            return {"_status": r.status_code, "_error": _err_text(r)}
        try:
            return r.json()
        except Exception:
            return {"_status": 502, "_error": "unreadable reply"}
    return {"_status": 400, "_error": "OpenAI kept refusing the request's parameters"}


def _gemini_schema_to_json_schema(schema: Any) -> Any:
    """Gemini's schema dialect (OBJECT / STRING, nullable, propertyOrdering)
    as a plain JSON Schema."""
    if isinstance(schema, list):
        return [_gemini_schema_to_json_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out: Dict[str, Any] = {}
    for k, v in schema.items():
        if k == "propertyOrdering":
            continue
        if k == "type" and isinstance(v, str):
            out["type"] = v.lower()
        elif k in ("properties",) and isinstance(v, dict):
            out[k] = {pk: _gemini_schema_to_json_schema(pv) for pk, pv in v.items()}
        elif k in ("items", "anyOf", "oneOf", "allOf"):
            out[k] = _gemini_schema_to_json_schema(v)
        elif k == "nullable":
            continue
        else:
            out[k] = v
    if schema.get("nullable") and isinstance(out.get("type"), str):
        out["type"] = [out["type"], "null"]
    return out


def _parts_of(payload: Dict[str, Any]) -> List[Tuple[str, List[Dict[str, Any]]]]:
    turns = []
    for c in payload.get("contents") or []:
        if not isinstance(c, dict):
            continue
        role = "assistant" if c.get("role") == "model" else "user"
        turns.append((role, [p for p in (c.get("parts") or []) if isinstance(p, dict)]))
    return turns


def _system_text(payload: Dict[str, Any]) -> str:
    si = payload.get("systemInstruction") or payload.get("system_instruction") or {}
    parts = si.get("parts") if isinstance(si, dict) else None
    return "\n".join(str(p.get("text") or "") for p in (parts or []) if isinstance(p, dict)).strip()


def _gemini_ok(parts: List[Dict[str, Any]], model: str, usage: Optional[Dict[str, Any]] = None,
               finish: str = "STOP") -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish, "index": 0}],
        "modelVersion": model,
    }
    if usage:
        body["usageMetadata"] = usage
    return body


def _gemini_error(status: int, message: str) -> Dict[str, Any]:
    names = {400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED",
             404: "NOT_FOUND", 429: "RESOURCE_EXHAUSTED", 500: "INTERNAL", 503: "UNAVAILABLE"}
    return {"error": {"code": status, "message": "OpenAI: " + (message or ""), "status": names.get(status, "UNKNOWN")}}


def _record(service: str, model: str, *, ok: bool, latency_ms: int, inp=None, out=None,
            unit: str = "tokens", error: str = "") -> None:
    try:
        import cost_tracker
        try:
            import engine
            sid = engine.get_active_session_id() or "default"
        except Exception:
            sid = "default"
        cost_tracker.record_usage(sid, service, "openai", model, operation="bridge",
                                  input_units=inp if ok else None, output_units=out if ok else None,
                                  unit_type=unit, latency_ms=latency_ms, success=ok,
                                  error_message=(error or None) if not ok else None,
                                  meta={"source": "provider_bridge"})
    except Exception:
        pass


def _text_call(key: str, payload: Dict[str, Any], timeout: float) -> Tuple[int, Dict[str, Any]]:
    models = openai_models()
    model = models["text"]
    gen = payload.get("generationConfig") or {}
    schema = gen.get("responseSchema") or gen.get("response_schema")
    wants_json = (gen.get("responseMimeType") or gen.get("response_mime_type")) == "application/json" or bool(schema)

    messages: List[Dict[str, Any]] = []
    system = _system_text(payload)
    if wants_json:
        rule = "Reply with one JSON object and nothing else."
        if schema:
            rule += " It must match this schema:\n" + json.dumps(_gemini_schema_to_json_schema(schema), separators=(",", ":"))
        system = (system + "\n\n" + rule).strip()
    if system:
        messages.append({"role": "system", "content": system})
    for role, parts in _parts_of(payload):
        content: List[Dict[str, Any]] = []
        for p in parts:
            if "text" in p and p.get("text") is not None:
                content.append({"type": "text", "text": str(p["text"])})
            elif isinstance(p.get("inlineData") or p.get("inline_data"), dict):
                d = p.get("inlineData") or p.get("inline_data")
                mime = str(d.get("mimeType") or d.get("mime_type") or "image/png")
                if mime.startswith("image/") and role == "user":
                    content.append({"type": "image_url",
                                    "image_url": {"url": f"data:{mime};base64,{d.get('data') or ''}"}})
        if not content:
            continue
        if role == "assistant":
            messages.append({"role": "assistant", "content": "\n".join(c["text"] for c in content if c["type"] == "text")})
        else:
            messages.append({"role": "user", "content": content})

    body: Dict[str, Any] = {"model": model, "messages": messages}
    if "temperature" in gen:
        body["temperature"] = gen["temperature"]
    if gen.get("maxOutputTokens"):
        # Headroom: on a reasoning model the budget also pays for its thinking,
        # and a Gemini call sized for a no-thinking model would come back empty.
        body["max_completion_tokens"] = int(gen["maxOutputTokens"]) + 2048
    body["reasoning_effort"] = _EFFORT_LADDER[0]
    if wants_json:
        body["response_format"] = {"type": "json_object"}

    t0 = time.time()
    reply = _chat_completion(key, body, timeout)
    ms = int((time.time() - t0) * 1000)
    status = int(reply.get("_status", 200))
    if status != 200:
        _record("text", model, ok=False, latency_ms=ms, error=str(reply.get("_error")))
        note_problem("openai", status, str(reply.get("_error") or ""))
        return status, _gemini_error(status, str(reply.get("_error") or ""))
    note_ok("openai")
    choice = (reply.get("choices") or [{}])[0] or {}
    msg = choice.get("message") or {}
    text = msg.get("content") or ""
    usage = reply.get("usage") or {}
    _record("text", model, ok=True, latency_ms=ms, inp=usage.get("prompt_tokens"), out=usage.get("completion_tokens"))
    gusage = {"promptTokenCount": usage.get("prompt_tokens", 0),
              "candidatesTokenCount": usage.get("completion_tokens", 0),
              "totalTokenCount": usage.get("total_tokens", 0)}
    if msg.get("refusal") and not text:
        # Gemini's shape for a blocked answer: a candidate with no parts.
        return 200, {"candidates": [{"content": {"role": "model", "parts": []}, "finishReason": "SAFETY"}],
                     "usageMetadata": gusage, "modelVersion": model}
    finish = {"length": "MAX_TOKENS", "content_filter": "SAFETY"}.get(choice.get("finish_reason"), "STOP")
    return 200, _gemini_ok([{"text": text}], model, gusage, finish)


def _transcribe_call(key: str, payload: Dict[str, Any], timeout: float) -> Tuple[int, Dict[str, Any]]:
    model = openai_models()["transcribe"]
    audio = None
    for _, parts in _parts_of(payload):
        for p in parts:
            d = p.get("inlineData") or p.get("inline_data")
            if isinstance(d, dict) and str(d.get("mimeType") or d.get("mime_type") or "").startswith("audio/"):
                audio = d
    if not audio:
        return 400, _gemini_error(400, "no audio")
    mime = str(audio.get("mimeType") or audio.get("mime_type"))
    ext = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/wav": "wav", "audio/x-wav": "wav",
           "audio/mpeg": "mp3", "audio/mp4": "m4a"}.get(mime.split(";")[0], "webm")
    raw = base64.b64decode(audio.get("data") or "")
    t0 = time.time()
    r = _post_openai(key, "/audio/transcriptions", data={"model": model},
                     files={"file": ("speech." + ext, raw, mime.split(";")[0])}, timeout=timeout)
    ms = int((time.time() - t0) * 1000)
    if not r.ok:
        _record("text", model, ok=False, latency_ms=ms, error=_err_text(r))
        note_problem("openai", r.status_code, _err_text(r))
        return r.status_code, _gemini_error(r.status_code, _err_text(r))
    text = ""
    try:
        text = str(r.json().get("text") or "")
    except Exception:
        pass
    _record("text", model, ok=True, latency_ms=ms)
    return 200, _gemini_ok([{"text": text.strip()}], model)


def _speech_voice(payload: Dict[str, Any]) -> str:
    vc = (((payload.get("generationConfig") or {}).get("speechConfig") or {}).get("voiceConfig") or {})
    name = str(((vc.get("prebuiltVoiceConfig") or {}).get("voiceName")) or vc.get("voice") or "")
    return OPENAI_VOICE_FOR.get(name, OPENAI_DEFAULT_VOICE)


def _speech_call(key: str, payload: Dict[str, Any], timeout: float) -> Tuple[int, Dict[str, Any]]:
    import speech
    prompt = ""
    for _, parts in _parts_of(payload):
        for p in parts:
            if isinstance(p.get("text"), str):
                prompt += p["text"]
    style, text = speech.split_prompt(prompt)
    if not text:
        return 400, _gemini_error(400, "nothing to say")
    body: Dict[str, Any] = {"model": OPENAI_TTS_MODEL, "voice": _speech_voice(payload),
                            "input": text[:4096], "response_format": "wav"}
    if style:
        body["instructions"] = style
    t0 = time.time()
    r = _post_openai(key, "/audio/speech", json_body=body, timeout=timeout)
    ms = int((time.time() - t0) * 1000)
    if not r.ok:
        _record("voice", OPENAI_TTS_MODEL, ok=False, latency_ms=ms, error=_err_text(r))
        note_problem("openai", r.status_code, _err_text(r))
        return r.status_code, _gemini_error(r.status_code, _err_text(r))
    raw = r.content or b""
    # 24 kHz 16-bit mono: 48,000 bytes a second after the 44-byte header.
    _record("voice", OPENAI_TTS_MODEL, ok=True, latency_ms=ms,
            out=round(max(0, len(raw) - 44) / 48000.0, 2), unit="seconds")
    return 200, _gemini_ok([{"inlineData": {"mimeType": "audio/wav",
                                            "data": base64.b64encode(raw).decode("ascii")}}],
                           OPENAI_TTS_MODEL)


_SIZES = {"landscape": "1536x1024", "portrait": "1024x1536", "square": "1024x1024"}

# The exact frame for each ratio the game asks Gemini for. GPT Image 2 and
# later take any WIDTHxHEIGHT divisible by 16, so the picture comes back the
# shape the scene is laid out for and nothing is cropped away. A model that
# only knows the three classic sizes refuses these once; after that it gets
# the nearest classic size and a centre crop (_std_sizes_only).
_EXACT = {"16:9": "1536x864", "21:9": "1792x768", "4:3": "1536x1152", "3:4": "1152x1536",
          "3:2": "1536x1024", "2:3": "1024x1536", "1:1": "1024x1024", "9:16": "864x1536",
          "5:4": "1280x1024", "4:5": "1024x1280"}
_std_sizes_only: set = set()
# Models whose /images/edits refused the JSON "images" list: they get the
# multipart image[] form instead.
_multipart_only: set = set()


def _aspect(payload: Dict[str, Any]) -> Tuple[float, str, str]:
    """(ratio, exact size, classic size) for the aspect ratio asked for."""
    cfg = ((payload.get("generationConfig") or {}).get("imageConfig") or {})
    ar = str(cfg.get("aspectRatio") or "16:9").strip()
    try:
        w, h = (float(x) for x in ar.split(":"))
        ratio = w / h if h else 16 / 9
    except Exception:
        ar, ratio = "16:9", 16 / 9
    shape = "square" if abs(ratio - 1) < 0.08 else "landscape" if ratio > 1 else "portrait"
    return ratio, _EXACT.get(ar, _SIZES[shape]), _SIZES[shape]


def _crop_to(png: bytes, ratio: float) -> bytes:
    """Centre-crop a classic 3:2 / 2:3 / 1:1 frame to the ratio the caller
    asked Gemini for, so a 16:9 scene stays 16:9 downstream."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(png))
        w, h = im.size
        if not w or not h or abs((w / h) - ratio) < 0.01:
            return png
        if w / h > ratio:
            nw = int(round(h * ratio))
            box = ((w - nw) // 2, 0, (w - nw) // 2 + nw, h)
        else:
            nh = int(round(w / ratio))
            box = (0, (h - nh) // 2, w, (h - nh) // 2 + nh)
        out = io.BytesIO()
        im.crop(box).save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return png


def _image_call(key: str, payload: Dict[str, Any], timeout: float) -> Tuple[int, Dict[str, Any]]:
    model = openai_models()["image"]
    quality = (os.environ.get("SOMEWHERE_OPENAI_IMAGE_QUALITY") or DEFAULT_IMAGE_QUALITY).strip().lower()
    ratio, exact, classic = _aspect(payload)
    ar = ((payload.get("generationConfig") or {}).get("imageConfig") or {}).get("aspectRatio") or "16:9"
    lines: List[str] = []
    images: List[Tuple[str, bytes]] = []
    for _, parts in _parts_of(payload):
        for p in parts:
            if p.get("text"):
                lines.append(str(p["text"]))
            d = p.get("inlineData") or p.get("inline_data")
            if isinstance(d, dict) and str(d.get("mimeType") or d.get("mime_type") or "").startswith("image/"):
                try:
                    images.append((str(d.get("mimeType") or d.get("mime_type")), base64.b64decode(d.get("data") or "")))
                    lines.append(f"(input image {len(images)})")
                except Exception:
                    pass
    system = _system_text(payload)
    body_prompt = ((system + "\n\n") if system else "") + "\n".join(lines)
    # The Images API caps the prompt at 32k; keep the end, where the instruction is.
    if len(body_prompt) > 31000:
        body_prompt = body_prompt[-31000:]
    images = images[:16]

    def prompt_for(size: str) -> str:
        w, h = (int(x) for x in size.split("x"))
        if abs(w / h - ratio) <= 0.05:
            return body_prompt
        return body_prompt + (f"\n\nFraming: this picture will be cropped to {ar} around its centre. Keep every "
                              f"important element (heads, feet, hands, the subject, any text) inside that central {ar} area.")

    params: Dict[str, Any] = {"model": model, "quality": quality, "n": 1, "moderation": "low"}
    if images:
        params["input_fidelity"] = "high"
    t0 = time.time()
    r = None
    for _ in range(5):
        size = classic if model in _std_sizes_only else exact
        sent = {k: v for k, v in params.items() if k not in _unsupported.get(model, set())}
        sent.update(prompt=prompt_for(size), size=size)
        if images and model not in _multipart_only:
            sent["images"] = [{"image_url": f"data:{m};base64,{base64.b64encode(b).decode('ascii')}"} for m, b in images]
            r = _post_openai(key, "/images/edits", json_body=sent, timeout=timeout)
        elif images:
            files = [("image[]", (f"ref{i + 1}." + ("jpg" if "jpeg" in m else m.split("/")[-1]), b, m))
                     for i, (m, b) in enumerate(images)]
            r = _post_openai(key, "/images/edits", data={k: str(v) for k, v in sent.items()}, files=files, timeout=timeout)
        else:
            r = _post_openai(key, "/images/generations", json_body=sent, timeout=timeout)
        if r.status_code == 400:
            param = _refused_param(model, r)
            if param == "size" and model not in _std_sizes_only:
                _std_sizes_only.add(model)
                continue
            if param in ("images", "image") and images and model not in _multipart_only:
                _multipart_only.add(model)
                continue
            if param and param in sent and param not in ("model", "prompt", "size"):
                _unsupported.setdefault(model, set()).add(param)
                continue
            low = _err_text(r).lower()
            if images and model not in _multipart_only and ("multipart" in low or "content-type" in low or "image[]" in low):
                _multipart_only.add(model)
                continue
            if "size" in low and model not in _std_sizes_only:
                _std_sizes_only.add(model)
                continue
        break
    ms = int((time.time() - t0) * 1000)
    if r is None or not r.ok:
        status = getattr(r, "status_code", 502) or 502
        err = _err_text(r) if r is not None else "no reply"
        _record("image", model, ok=False, latency_ms=ms, unit="images", error=err)
        note_problem("openai", status, err)
        if status == 400 and ("safety" in err.lower() or "moderation" in err.lower()):
            # Gemini's shape for a refused picture: no parts. Callers already
            # treat that as "nothing drawn" and fall back.
            return 200, {"candidates": [{"content": {"role": "model", "parts": []}, "finishReason": "SAFETY"}],
                         "modelVersion": model}
        return status, _gemini_error(status, err)
    try:
        reply = r.json()
        item = (reply.get("data") or [{}])[0]
        usage = reply.get("usage") or {}
    except Exception:
        item, usage = {}, {}
    raw = b""
    if item.get("b64_json"):
        raw = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        import requests
        try:
            raw = requests.get(item["url"], timeout=60).content
        except Exception:
            raw = b""
    if not raw:
        _record("image", model, ok=False, latency_ms=ms, unit="images", error="empty picture")
        return 200, {"candidates": [{"content": {"role": "model", "parts": []}, "finishReason": "OTHER"}],
                     "modelVersion": model}
    raw = _crop_to(raw, ratio)
    if usage.get("output_tokens"):
        # GPT Image bills tokens, and image-input tokens cost 8/5 of text
        # ones; pricing.json holds the text rate, so fold them in at that.
        det = usage.get("input_tokens_details") or {}
        text_in = det.get("text_tokens", usage.get("input_tokens", 0)) or 0
        image_in = det.get("image_tokens", 0) or 0
        _record("image", model, ok=True, latency_ms=ms, inp=text_in + image_in * 8 / 5,
                out=usage.get("output_tokens"), unit="tokens")
    else:
        _record("image", model, ok=True, latency_ms=ms, out=1, unit="images")
    return 200, _gemini_ok([{"inlineData": {"mimeType": "image/png", "data": base64.b64encode(raw).decode("ascii")}}], model)


def _is_image_request(model: str, payload: Dict[str, Any]) -> bool:
    mods = ((payload.get("generationConfig") or {}).get("responseModalities") or [])
    if any(str(m).upper() == "IMAGE" for m in mods):
        return True
    return "image" in (model or "").lower() and "vision" not in (model or "").lower()


def _is_speech_request(payload: Dict[str, Any]) -> bool:
    mods = ((payload.get("generationConfig") or {}).get("responseModalities") or [])
    return any(str(m).upper() == "AUDIO" for m in mods)


def _is_audio_request(payload: Dict[str, Any]) -> bool:
    for _, parts in _parts_of(payload):
        for p in parts:
            d = p.get("inlineData") or p.get("inline_data")
            if isinstance(d, dict) and str(d.get("mimeType") or d.get("mime_type") or "").startswith("audio/"):
                return True
    return False


def answer_gemini_request(url: str, payload: Dict[str, Any], timeout: Optional[float] = None) -> Tuple[int, Dict[str, Any]]:
    """The OpenAI answer to one Gemini generateContent request, Gemini-shaped."""
    key = openai_key()
    if not key:
        return 401, _gemini_error(401, "no OpenAI key")
    model = url.split("/models/", 1)[-1].split(":", 1)[0] if "/models/" in url else ""
    payload = payload if isinstance(payload, dict) else {}
    t = float(timeout) if isinstance(timeout, (int, float)) else 0.0
    if isinstance(timeout, tuple) and timeout:
        t = float(max(x for x in timeout if isinstance(x, (int, float))) or 0)
    try:
        if _is_speech_request(payload):
            return _speech_call(key, payload, max(t, TEXT_TIMEOUT_FLOOR))
        if _is_image_request(model, payload):
            return _image_call(key, payload, max(t, IMAGE_TIMEOUT_FLOOR))
        if _is_audio_request(payload):
            return _transcribe_call(key, payload, max(t, TEXT_TIMEOUT_FLOOR))
        return _text_call(key, payload, max(t, TEXT_TIMEOUT_FLOOR))
    except Exception as e:  # noqa: BLE001
        name = type(e).__name__
        status = 504 if "Timeout" in name else 502
        return status, _gemini_error(status, name)


def _response(status: int, body: Dict[str, Any], url: str):
    import requests
    r = requests.models.Response()
    r.status_code = int(status)
    r._content = json.dumps(body).encode("utf-8")
    r.headers["Content-Type"] = "application/json; charset=UTF-8"
    r.encoding = "utf-8"
    r.url = url
    r.reason = "OK" if status == 200 else "Error"
    r._bridged = True  # cost_tracker's Gemini meter skips these
    return r


def install() -> None:
    """Hook requests once. Idempotent; safe to import from anywhere."""
    global _original_request
    try:
        import requests
    except Exception:
        return
    if getattr(requests.Session, "_provider_bridge", False):
        return
    try:
        import cost_tracker  # noqa: F401  (its wire meter goes on first, so this hook sits outside it)
    except Exception:
        pass
    original = requests.Session.request
    _original_request = original

    def request(self, method, url, *args, **kwargs):
        u = str(url or "")
        if _GEMINI_HOST in u and str(method).upper() == "POST" and active():
            if ":generateContent" in u:
                status, body = answer_gemini_request(u, kwargs.get("json") or {}, kwargs.get("timeout"))
                return _response(status, body, u)
            if "/cachedContents" in u:
                # The lore cache is a Gemini feature; the story carries the
                # lore in the prompt instead (engine._ask's usual path).
                return _response(400, _gemini_error(400, "caching is Gemini-only"), u)
        return original(self, method, url, *args, **kwargs)

    requests.Session.request = request
    requests.Session._provider_bridge = True


install()
