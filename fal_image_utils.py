"""
fal_image_utils.py - fal.ai "Lightning" image generation provider

A drop-in ULTRA-FAST alternative image backend to gemini_image_utils.py and
krea_image_utils.py. fal.ai serves SDXL Lightning (a 4-step distilled SDXL
checkpoint) on custom-optimized infrastructure:

    POST https://fal.run/fal-ai/fast-lightning-sdxl               (text-to-image)
    POST https://fal.run/fal-ai/fast-lightning-sdxl/image-to-image (img2img)

Both are SYNCHRONOUS - fal queues + polls internally and the HTTP response
only comes back once the image is ready, typically in ~1-2 seconds (vs. ~12s
for Krea Medium or ~15-30s for Gemini Pro). There is no async job/poll dance
to write on our end, which is what makes this the fastest provider to
integrate AND the fastest provider at runtime.

Trade-off: SDXL Lightning is a much smaller/older checkpoint than Gemini or
Krea 2, so per-image fidelity and prompt adherence are lower. This is meant
as a "need it NOW" speed preset, not a quality replacement.

Public surface mirrors gemini_image_utils / krea_image_utils so
engine._gen_image() can route to it through identical call sites:

    generate_with_fal(prompt, caption, ...) -> str | None
    generate_fal_img2img(prompt, caption, reference_image_path, ...) -> str | None
"""

import io
import os
import json
import base64
from pathlib import Path

import requests

# Reuse the single source of truth for prompt templates + safety sanitizer so
# fal output stays visually consistent with the other providers (same VHS
# identity, same content-filter softening) instead of duplicating that logic.
from gemini_image_utils import PROMPTS, _sanitize_for_safety

ROOT = Path(__file__).parent
try:
    with open(ROOT / "config.json", "r", encoding="utf-8") as f:
        _config = json.load(f)
except FileNotFoundError:
    _config = {}

FAL_API_KEY = (
    os.getenv("FAL_API_KEY")
    or os.getenv("FAL_KEY")
    or _config.get("FAL_API_KEY")
    or _config.get("FAL_KEY")
    or ""
)

FAL_API_BASE = (os.getenv("FAL_API_BASE") or _config.get("FAL_API_BASE") or "https://fal.run").rstrip("/")

# SDXL Lightning is distilled for 1/2/4/8-step inference. Default to 8 (the
# checkpoint's max) — it's the best fidelity Lightning can produce and still
# far faster than Krea/Gemini. Drop via env for a faster/blurrier result.
FAL_MODEL = "fal-ai/fast-lightning-sdxl"
try:
    FAL_NUM_INFERENCE_STEPS = int(os.getenv("FAL_NUM_INFERENCE_STEPS") or _config.get("FAL_NUM_INFERENCE_STEPS") or 8)
except (TypeError, ValueError):
    FAL_NUM_INFERENCE_STEPS = 8
if FAL_NUM_INFERENCE_STEPS not in (1, 2, 4, 8):
    FAL_NUM_INFERENCE_STEPS = 8

# fal exposes fixed aspect-ratio presets rather than free-form ratios; this is
# the closest built-in match to the project's 4:3 house style.
FAL_IMAGE_SIZE = (os.getenv("FAL_IMAGE_SIZE") or _config.get("FAL_IMAGE_SIZE") or "landscape_4_3").strip()

# img2img "strength" = how much the output is allowed to diverge from the
# reference (0..1). Lower keeps continuity tighter; SDXL Lightning img2img
# defaults to 0.95 (near-total repaint) so we pull it well down. 0.45 keeps
# frame-to-frame continuity close so the world stops "melting" between turns.
try:
    FAL_IMG2IMG_STRENGTH = float(os.getenv("FAL_IMG2IMG_STRENGTH") or _config.get("FAL_IMG2IMG_STRENGTH") or 0.45)
except (TypeError, ValueError):
    FAL_IMG2IMG_STRENGTH = 0.45
FAL_IMG2IMG_STRENGTH = max(0.05, min(1.0, FAL_IMG2IMG_STRENGTH))

# Data URIs are only recommended for small files - always use the
# downsampled 480x360 sidecar as the img2img reference (never full-res).
USE_DOWNSAMPLED_FOR_IMG2IMG = True

# Requests to fal.run block until the image is ready, so this is a plain HTTP
# timeout, not a job-poll budget. Generation itself is ~1-2s; padding for
# network/cold-start jitter.
_REQUEST_TIMEOUT_SECONDS = 20.0

IMAGE_DIR = Path("images")

if not FAL_API_KEY:
    print("[FAL INIT] WARNING: FAL_API_KEY not set — fal.ai image generation will be unavailable.")
else:
    print(
        f"[FAL INIT] FAL_API_KEY loaded ({FAL_API_KEY[:6]}...{FAL_API_KEY[-4:]}); "
        f"base={FAL_API_BASE}; steps={FAL_NUM_INFERENCE_STEPS}; "
        f"img2img_strength={FAL_IMG2IMG_STRENGTH}"
    )


# ---------------------------------------------------------------------------
# Prompt building (shared VHS identity, same anchors as the other providers)
# ---------------------------------------------------------------------------

# IMPORTANT: SDXL is conditioned by a CLIP text encoder that only reads ~77
# tokens (~300-400 chars) and responds to short, comma-separated descriptors —
# NOT the long prose instructions Gemini follows. Reusing the Gemini prompt
# templates here buried the actual scene under ~1.5k chars of POV/cinematography
# prose (and then got truncated), so SDXL effectively never "saw" the scene.
# We now lead with the scene and append tight style tags, and push the
# "no text / no border / no people" constraints into a proper negative prompt.

_SDXL_STYLE_TAGS = (
    "1990s VHS camcorder footage, analog videotape look, photorealistic, "
    "real camera optics, heavy film grain, slight chromatic aberration, "
    "first-person head-mounted camera POV, environment only, edge-to-edge full frame"
)

# fal's SDXL endpoints accept a negative_prompt — the correct place for the
# anti-text / anti-border / anti-person constraints (as tags, not prose).
_SDXL_NEGATIVE_PROMPT = (
    "text, words, letters, numbers, watermark, timecode, timestamp, REC icon, "
    "caption, subtitles, logo, border, frame, black bars, letterboxing, "
    "person, people, human, face, portrait, hands, "
    "3d render, cgi, video game screenshot, illustration, cartoon, anime, painting, "
    "deformed, distorted, blurry, low quality, jpeg artifacts"
)


def _clamp(prompt: str, limit: int = 700) -> str:
    # SDXL/CLIP truncates around ~77 tokens (~300-400 chars). Keep the positive
    # prompt short and scene-first so the subject is never pushed out.
    return prompt if len(prompt) <= limit else prompt[:limit]


def _build_text2img_prompt(prompt: str, time_of_day: str = "") -> str:
    scene = (prompt or "").strip()
    parts = [scene]
    if time_of_day:
        parts.append(f"{time_of_day} lighting")
    parts.append(_SDXL_STYLE_TAGS)
    return _clamp(_sanitize_for_safety(", ".join(p for p in parts if p)))


def _build_img2img_prompt(prompt: str, time_of_day: str = "") -> str:
    scene = (prompt or "").strip()
    parts = [
        scene,
        "direct continuation of the previous frame, same location and camera viewpoint",
    ]
    if time_of_day:
        parts.append(f"{time_of_day} lighting")
    parts.append(_SDXL_STYLE_TAGS)
    return _clamp(_sanitize_for_safety(", ".join(p for p in parts if p)))


# ---------------------------------------------------------------------------
# fal HTTP helpers
# ---------------------------------------------------------------------------

def _auth_headers() -> dict:
    return {"Authorization": f"Key {FAL_API_KEY}", "Content-Type": "application/json"}


def _extract_first_image_url(result: dict) -> str | None:
    images = (result or {}).get("images") or []
    if not images:
        return None
    first = images[0]
    return first.get("url") if isinstance(first, dict) else first


def _download_and_save(image_url: str, caption: str, output_dir: Path) -> str | None:
    """Fetch a finished fal image (either a hosted URL or a data: URI) and
    persist it (full-res PNG + 480x360 `_small.png` sidecar), matching the
    return contract shared by gemini_image_utils / krea_image_utils."""
    try:
        if image_url.startswith("data:"):
            _, _, b64_data = image_url.partition(",")
            image_bytes = base64.b64decode(b64_data)
        else:
            resp = requests.get(image_url, timeout=30)
            resp.raise_for_status()
            image_bytes = resp.content
    except Exception as e:
        print(f"[FAL] Failed to fetch result image: {e}", flush=True)
        return None

    save_dir = output_dir if output_dir is not None else IMAGE_DIR
    save_dir.mkdir(parents=True, exist_ok=True)

    safe_caption = "".join(c if c.isalnum() or c in "_-" else "_" for c in caption[:48])
    filename = f"{hash(caption) & 0xFFFFFFFF}_{safe_caption}.png"
    image_path = save_dir / filename

    try:
        from PIL import Image as PILImage
        img = PILImage.open(io.BytesIO(image_bytes)).convert("RGB")
        img.save(image_path, format="PNG")

        small_path = save_dir / filename.replace(".png", "_small.png")
        small = img.resize((480, 360), PILImage.LANCZOS)
        small.save(small_path, format="PNG", optimize=True, quality=85)
        print(f"[FAL] Image saved: {image_path}", flush=True)
        print(f"[FAL] Downsampled saved: {small_path} (480x360, 4:3 for API refs)", flush=True)
    except Exception as e:
        print(f"[FAL] WARNING: PIL post-process failed ({e}); writing raw bytes", flush=True)
        with open(image_path, "wb") as f:
            f.write(image_bytes)

    return str(image_path)


# Fields that are NOT part of the base fast-lightning-sdxl request contract.
# If fal ever rejects the request for a bad field (validation error), we strip
# these and retry so image generation can never fully break on an optional knob.
_FAL_OPTIONAL_PAYLOAD_KEYS = ("negative_prompt",)


def _call_fal(path: str, payload: dict, caption: str, output_dir: Path) -> str | None:
    if not FAL_API_KEY:
        print("[FAL] FATAL: No API key configured — cannot generate image.", flush=True)
        return None

    endpoint = f"{FAL_API_BASE}/{path}"

    def _post(body: dict):
        resp = requests.post(endpoint, headers=_auth_headers(), json=body, timeout=_REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return resp.json()

    try:
        print(f"[FAL] POST {endpoint} (steps={payload.get('num_inference_steps')})", flush=True)
        result = _post(payload)
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        body = e.response.text if e.response is not None else ""
        # Self-heal: a 4xx validation error may be an unsupported optional field
        # (e.g. negative_prompt isn't in the fast-lightning-sdxl schema). Strip
        # optional keys and retry once with the known-good minimal contract.
        strippable = [k for k in _FAL_OPTIONAL_PAYLOAD_KEYS if k in payload]
        if code in (400, 422) and strippable:
            minimal = {k: v for k, v in payload.items() if k not in _FAL_OPTIONAL_PAYLOAD_KEYS}
            print(f"[FAL] HTTP {code} with optional fields {strippable}; retrying without them", flush=True)
            try:
                result = _post(minimal)
            except Exception as e2:
                print(f"[FAL] ERROR: retry without optional fields failed: {e2}", flush=True)
                return None
        else:
            print(f"[FAL] ERROR: HTTP {code}: {body}", flush=True)
            return None
    except Exception as e:
        print(f"[FAL] ERROR: request failed: {e}", flush=True)
        return None

    image_url = _extract_first_image_url(result)
    if not image_url:
        print(f"[FAL] ERROR: no image URL in response: {result}", flush=True)
        return None

    return _download_and_save(image_url, caption, output_dir)


def _reference_data_uri(img_path: str) -> str | None:
    """Prefer the downsampled `_small.png` sidecar and inline it as a base64
    data URI (fal accepts data URIs directly - no separate upload step
    needed at this file size, which is what keeps this integration this
    simple)."""
    p = Path(img_path)
    if USE_DOWNSAMPLED_FOR_IMG2IMG:
        small = p.parent / p.name.replace(".png", "_small.png")
        use_path = small if small.exists() else p
    else:
        use_path = p

    if not use_path.exists():
        print(f"[FAL] Reference image not found: {img_path}", flush=True)
        return None

    try:
        with open(use_path, "rb") as f:
            data = f.read()
        mime = "image/jpeg" if str(use_path).lower().endswith((".jpg", ".jpeg")) else "image/png"
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    except Exception as e:
        print(f"[FAL] Failed to read reference image {img_path}: {e}", flush=True)
        return None


# ---------------------------------------------------------------------------
# Public API (mirrors gemini_image_utils / krea_image_utils)
# ---------------------------------------------------------------------------

def generate_with_fal(
    prompt: str,
    caption: str,
    world_prompt: str = None,
    aspect_ratio: str = None,
    model: str = None,
    time_of_day: str = "",
    is_first_frame: bool = False,
    action_context: str = "",
    hd_mode: bool = True,
    output_dir: Path = None,
) -> str | None:
    """Text-to-image with fal.ai SDXL Lightning. Returns a local image path,
    or None on failure. Typically completes in ~1-2 seconds.

    Signature intentionally matches gemini_image_utils.generate_with_gemini /
    krea_image_utils.generate_with_krea so engine._gen_image() can call any
    provider identically. `hd_mode` is accepted for interface parity but
    Lightning has no HQ tier - it always runs the fast 4-step checkpoint.
    """
    try:
        caption = caption.encode("ascii", "ignore").decode("ascii") or "image"
    except Exception:
        caption = "image"

    if not FAL_API_KEY:
        print("[FAL IMG] FATAL: No API key! Cannot generate image!", flush=True)
        return None

    full_prompt = _build_text2img_prompt(prompt, time_of_day=time_of_day)
    payload = {
        "prompt": full_prompt,
        "negative_prompt": _SDXL_NEGATIVE_PROMPT,
        "image_size": FAL_IMAGE_SIZE,
        "num_inference_steps": FAL_NUM_INFERENCE_STEPS,
        "format": "png",
    }
    print(f"[FAL IMG] text2img via {FAL_MODEL} (steps={FAL_NUM_INFERENCE_STEPS})", flush=True)
    print(f"[FAL IMG] scene prompt: {full_prompt[:160]}", flush=True)
    return _call_fal(FAL_MODEL, payload, caption, output_dir)


def generate_fal_img2img(
    prompt: str,
    caption: str,
    reference_image_path,
    strength: float = None,
    world_prompt: str = None,
    time_of_day: str = "",
    action_context: str = "",
    hd_mode: bool = True,
    output_dir: Path = None,
    is_flipbook: bool = False,
    model: str = None,
) -> str | None:
    """Image-to-image with fal.ai SDXL Lightning. Only a single reference
    image is supported by this endpoint (unlike Gemini/Krea's multi-ref
    continuity), so the most recent frame is used.

    Signature matches gemini_image_utils.generate_gemini_img2img /
    krea_image_utils.generate_krea_img2img.
    """
    try:
        caption = caption.encode("ascii", "ignore").decode("ascii") or "image"
    except Exception:
        caption = "image"

    if not FAL_API_KEY:
        print("[FAL IMG] FATAL: No API key! Cannot generate image!", flush=True)
        return None

    if isinstance(reference_image_path, str):
        ref_path = reference_image_path
    else:
        refs = list(reference_image_path or [])
        ref_path = refs[0] if refs else None

    if not ref_path:
        print("[FAL IMG] No reference image available; falling back to text2img", flush=True)
        return generate_with_fal(
            prompt=prompt, caption=caption, world_prompt=world_prompt,
            time_of_day=time_of_day, action_context=action_context,
            hd_mode=hd_mode, output_dir=output_dir, model=model,
        )

    data_uri = _reference_data_uri(ref_path)
    if not data_uri:
        print("[FAL IMG] Reference image unreadable; falling back to text2img", flush=True)
        return generate_with_fal(
            prompt=prompt, caption=caption, world_prompt=world_prompt,
            time_of_day=time_of_day, action_context=action_context,
            hd_mode=hd_mode, output_dir=output_dir, model=model,
        )

    img2img_strength = FAL_IMG2IMG_STRENGTH if strength is None else float(strength)
    img2img_strength = max(0.05, min(1.0, img2img_strength))

    full_prompt = _build_img2img_prompt(prompt, time_of_day=time_of_day)
    payload = {
        "image_url": data_uri,
        "prompt": full_prompt,
        "negative_prompt": _SDXL_NEGATIVE_PROMPT,
        "image_size": FAL_IMAGE_SIZE,
        "num_inference_steps": FAL_NUM_INFERENCE_STEPS,
        "strength": img2img_strength,
        "format": "png",
    }
    print(f"[FAL IMG] img2img via {FAL_MODEL} (strength={img2img_strength}, steps={FAL_NUM_INFERENCE_STEPS}, ref={Path(ref_path).name})", flush=True)
    print(f"[FAL IMG] scene prompt: {full_prompt[:160]}", flush=True)
    return _call_fal(f"{FAL_MODEL}/image-to-image", payload, caption, output_dir)


print("[FAL] Module loaded (fal_image_utils)", flush=True)
