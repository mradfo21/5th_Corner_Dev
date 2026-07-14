"""
krea_image_utils.py - Krea 2 image generation provider

A drop-in alternative image backend to gemini_image_utils.py ("Nano Banana").
Krea 2 is Krea's foundation image model. It exposes an async job API:

    POST https://api.krea.ai/generate/image/krea/krea-2/{medium|large}
        -> { "job_id": ... }
    GET  https://api.krea.ai/jobs/{job_id}
        -> { "status": "completed", "result": { "urls": [...] } }

Unlike Gemini (which returns inline base64), Krea returns hosted image URLs, so
we download the finished image and persist it locally in the exact same shape the
rest of the engine expects (a full-res PNG plus a downsampled `_small.png`
sidecar used as a cheap reference for later frames).

img2img continuity is achieved with Krea's *style transfer* system: the previous
frame(s) are uploaded as assets and passed as `image_style_references`. This
carries the palette / grain / VHS aesthetic forward. Krea style transfer is a
STYLE lock rather than a pixel-level spatial lock, so the heavy spatial-anchor
text in the shared prompt templates still does the compositional work.

Public surface (matches gemini_image_utils so engine._gen_image() can route to
either provider with the same call sites):

    generate_with_krea(prompt, caption, ...) -> str | None
    generate_krea_img2img(prompt, caption, reference_image_path, ...) -> str | None
"""

import io
import os
import json
import time
import base64
from pathlib import Path

import requests

# Reuse the single source of truth for prompt templates + safety sanitizer so
# Krea output stays visually consistent with the Gemini path (same VHS identity,
# same content-filter softening) instead of duplicating that logic here.
from gemini_image_utils import PROMPTS, _sanitize_for_safety

ROOT = Path(__file__).parent
try:
    with open(ROOT / "config.json", "r", encoding="utf-8") as f:
        _config = json.load(f)
except FileNotFoundError:
    _config = {}

# Accept either KREA_API_KEY or the KREA_API_TOKEN name used in Krea's own docs.
KREA_API_KEY = (
    os.getenv("KREA_API_KEY")
    or os.getenv("KREA_API_TOKEN")
    or _config.get("KREA_API_KEY")
    or _config.get("KREA_API_TOKEN")
    or ""
)

KREA_API_BASE = (os.getenv("KREA_API_BASE") or _config.get("KREA_API_BASE") or "https://api.krea.ai").rstrip("/")

# Model tiers. hd_mode picks Large (higher quality, slower/pricier) vs Medium
# (faster, cheaper) — mirroring the Gemini Pro/Flash split.
KREA_MEDIUM = "krea-2/medium"
KREA_LARGE = "krea-2/large"

# "creativity" controls how far Krea expands a prompt (raw|low|medium|high).
# This is a continuity-driven survival-horror game, so we default LOW to keep
# the model faithful to the prompt/reference rather than reinterpreting freely.
KREA_CREATIVITY = (os.getenv("KREA_CREATIVITY") or _config.get("KREA_CREATIVITY") or "low").strip().lower()

# Strength of the previous-frame style reference for img2img (0.0-1.0). Higher =
# stronger carry-over of palette/grain/mood from the prior frame.
try:
    KREA_STYLE_STRENGTH = float(os.getenv("KREA_STYLE_STRENGTH") or _config.get("KREA_STYLE_STRENGTH") or 0.6)
except (TypeError, ValueError):
    KREA_STYLE_STRENGTH = 0.6

KREA_ASPECT_RATIO = (os.getenv("KREA_ASPECT_RATIO") or _config.get("KREA_ASPECT_RATIO") or "4:3").strip()
KREA_RESOLUTION = (os.getenv("KREA_RESOLUTION") or _config.get("KREA_RESOLUTION") or "1K").strip()

# Prefer the downsampled `_small.png` sidecar when uploading references (faster,
# less bandwidth) — same toggle philosophy as gemini_image_utils.
USE_DOWNSAMPLED_FOR_IMG2IMG = True

# Job polling budget.
_POLL_INTERVAL_SECONDS = 2.0
_POLL_TIMEOUT_SECONDS = 90.0

IMAGE_DIR = Path("images")

if not KREA_API_KEY:
    print("[KREA INIT] WARNING: KREA_API_KEY not set — Krea image generation will be unavailable.")
else:
    print(f"[KREA INIT] KREA_API_KEY loaded ({KREA_API_KEY[:6]}...{KREA_API_KEY[-4:]}); base={KREA_API_BASE}")

# In-memory cache mapping (abs_path, mtime) -> uploaded Krea asset URL, so the
# same reference frame isn't re-uploaded on every turn.
_asset_cache: dict = {}


# ---------------------------------------------------------------------------
# Prompt building (shared VHS identity, same anchors as the Gemini path)
# ---------------------------------------------------------------------------

_ANTI_TIMECODE = (
    "NO TEXT ANYWHERE. Zero text, zero numbers, zero letters, zero symbols. "
    "Do NOT render 'REC', dates, timecodes, timestamps, battery/recording icons, "
    "captions or watermarks. If reference images contain text, remove it. "
    "The output must be 100% visual with no on-screen displays."
)

_ANTI_BORDER = (
    "NO BORDERS OR FRAMES. The image fills the entire canvas edge-to-edge with "
    "zero borders, frames, black bars, white borders, matting or letterboxing. "
    "This is raw footage, not a framed photograph."
)

_ANTI_PERSON = (
    "NO PERSON / PLAYER VISIBLE. This is a fixed first-person / mounted-camera "
    "view. Never show any part of a human body (head, shoulders, arms, hands, "
    "legs, torso or silhouette). Show only the environment."
)

_PHOTOGRAPHIC_ANCHOR = (
    "OPTICAL REALITY - REAL FOOTAGE: real light captured through real glass optics "
    "onto physical magnetic videotape. This is photographic reality, NOT a video "
    "game, 3D render, CGI, or digital art. Messy, irregular, weathered surfaces; "
    "atmospheric depth; soft diffuse natural light; organic texture. Recorded onto "
    "1990s consumer VHS tape: subtle grain, gentle noise, slight color shift. "
    "Looks like early-1990s amateur camcorder / news B-roll / surveillance footage."
)


def _clamp(prompt: str, limit: int = 5000) -> str:
    return prompt if len(prompt) <= limit else prompt[:limit]


# The shared Gemini templates are very long; the critical, Krea-specific
# instructions (scene, continuity, time-of-day, anchors) are placed BEFORE the
# verbose template so they always survive the character clamp — only the
# generic tail of the template is trimmed.

def _time_injection(time_of_day: str) -> str:
    if not time_of_day:
        return ""
    return (
        f"TIME/ATMOSPHERE CONSTRAINTS:\n{time_of_day}\n"
        "The lighting, weather and atmosphere MUST match these exact conditions."
    )


def _build_text2img_prompt(prompt: str, time_of_day: str = "") -> str:
    structured = PROMPTS["gemini_text_to_image_instructions"].format(prompt=prompt)
    head = [_ANTI_TIMECODE, _time_injection(time_of_day), _ANTI_BORDER, _ANTI_PERSON, _PHOTOGRAPHIC_ANCHOR]
    parts = [p for p in head if p] + [structured]
    return _clamp(_sanitize_for_safety("\n\n".join(parts)))


def _build_img2img_prompt(prompt: str, time_of_day: str = "", is_flipbook: bool = False) -> str:
    structured = PROMPTS["gemini_image_to_image_instructions"].format(prompt=prompt)
    continuity = (
        "HOW TO USE THE STYLE REFERENCE(S): the reference image(s) show the "
        "PREVIOUS moment. Carry forward their palette, grain, lighting, time of day "
        "and overall VHS aesthetic. Keep the same camera height and viewpoint unless "
        "the action explicitly moves the camera, then show smooth, natural "
        "progression (handheld continuous recording — no teleporting to a new scene)."
    )
    head = [_ANTI_TIMECODE, continuity, _time_injection(time_of_day)]
    if not is_flipbook:
        head.append(_ANTI_BORDER)
    head.extend([_ANTI_PERSON, _PHOTOGRAPHIC_ANCHOR])
    parts = [p for p in head if p] + [structured]
    return _clamp(_sanitize_for_safety("\n\n".join(parts)))


def _resolve_model(model, hd_mode: bool) -> str:
    """Pick the Krea 2 tier. An explicit model string wins if it names a tier;
    otherwise hd_mode selects Large (HQ) vs Medium (fast)."""
    if model:
        m = str(model).lower()
        if "large" in m:
            return KREA_LARGE
        if "medium" in m:
            return KREA_MEDIUM
    return KREA_LARGE if hd_mode else KREA_MEDIUM


# ---------------------------------------------------------------------------
# Krea HTTP helpers (auth, job submit + poll, asset upload)
# ---------------------------------------------------------------------------

def _auth_headers(json_body: bool = True) -> dict:
    headers = {"Authorization": f"Bearer {KREA_API_KEY}"}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _wait_for_job(job_id: str, timeout_seconds: float = _POLL_TIMEOUT_SECONDS) -> dict | None:
    """Poll GET /jobs/{job_id} until it completes. Returns the completed job
    dict, or None on failure/timeout."""
    deadline = time.time() + timeout_seconds
    url = f"{KREA_API_BASE}/jobs/{job_id}"
    while time.time() < deadline:
        try:
            resp = requests.get(url, headers=_auth_headers(json_body=False), timeout=20)
            resp.raise_for_status()
            job = resp.json()
        except Exception as e:
            print(f"[KREA] Poll error for job {job_id}: {e}", flush=True)
            time.sleep(_POLL_INTERVAL_SECONDS)
            continue

        status = (job.get("status") or "").lower()
        if status == "completed":
            return job
        if status in ("failed", "canceled", "cancelled", "error"):
            print(f"[KREA] Job {job_id} ended with status '{status}': {job.get('error') or job}", flush=True)
            return None
        time.sleep(_POLL_INTERVAL_SECONDS)

    print(f"[KREA] Job {job_id} timed out after {timeout_seconds}s", flush=True)
    return None


def _extract_urls(job: dict) -> list:
    """Krea completed jobs expose image URLs at result.urls; be tolerant of a
    couple of shapes just in case."""
    if not isinstance(job, dict):
        return []
    result = job.get("result") or {}
    if isinstance(result, dict):
        urls = result.get("urls") or result.get("images") or []
        if isinstance(urls, list) and urls:
            # entries may be plain strings or {"url": ...}
            return [u.get("url") if isinstance(u, dict) else u for u in urls]
    top = job.get("urls")
    if isinstance(top, list) and top:
        return [u.get("url") if isinstance(u, dict) else u for u in top]
    return []


def _download_and_save(image_url: str, caption: str, output_dir: Path) -> str | None:
    """Download a finished Krea image and persist it (full-res PNG + 480x360
    `_small.png` sidecar). Returns the local path string, matching the Gemini
    utility's return contract."""
    try:
        resp = requests.get(image_url, timeout=60)
        resp.raise_for_status()
        image_bytes = resp.content
    except Exception as e:
        print(f"[KREA] Failed to download result image: {e}", flush=True)
        return None

    save_dir = output_dir if output_dir is not None else IMAGE_DIR
    save_dir.mkdir(parents=True, exist_ok=True)

    safe_caption = "".join(c if c.isalnum() or c in "_-" else "_" for c in caption[:48])
    filename = f"{hash(caption) & 0xFFFFFFFF}_{safe_caption}.png"
    image_path = save_dir / filename

    # Normalize to PNG (Krea may return jpg/webp) and write the sidecar.
    try:
        from PIL import Image as PILImage
        img = PILImage.open(io.BytesIO(image_bytes)).convert("RGB")
        img.save(image_path, format="PNG")

        small_path = save_dir / filename.replace(".png", "_small.png")
        small = img.resize((480, 360), PILImage.LANCZOS)
        small.save(small_path, format="PNG", optimize=True, quality=85)
        print(f"[KREA] Image saved: {image_path}", flush=True)
        print(f"[KREA] Downsampled saved: {small_path} (480x360, 4:3 for API refs)", flush=True)
    except Exception as e:
        # Fall back to writing raw bytes if PIL isn't happy — still return a path.
        print(f"[KREA] WARNING: PIL post-process failed ({e}); writing raw bytes", flush=True)
        with open(image_path, "wb") as f:
            f.write(image_bytes)

    return str(image_path)


def _submit_and_fetch(model: str, payload: dict, caption: str, output_dir: Path) -> str | None:
    """POST a generation request, wait for the job, download the first result."""
    if not KREA_API_KEY:
        print("[KREA] FATAL: No API key configured — cannot generate image.", flush=True)
        return None

    endpoint = f"{KREA_API_BASE}/generate/image/krea/{model}"
    try:
        print(f"[KREA] POST {endpoint} (creativity={payload.get('creativity')})", flush=True)
        resp = requests.post(endpoint, headers=_auth_headers(), json=payload, timeout=30)
        resp.raise_for_status()
        created = resp.json()
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        body = e.response.text if e.response is not None else ""
        print(f"[KREA] ERROR: submit HTTP {code}: {body}", flush=True)
        return None
    except Exception as e:
        print(f"[KREA] ERROR: submit failed: {e}", flush=True)
        return None

    job_id = created.get("job_id") or created.get("id")
    if not job_id:
        print(f"[KREA] ERROR: no job_id in response: {created}", flush=True)
        return None

    job = _wait_for_job(job_id)
    if not job:
        return None

    urls = _extract_urls(job)
    if not urls:
        print(f"[KREA] ERROR: completed job {job_id} had no image URLs: {job}", flush=True)
        return None

    return _download_and_save(urls[0], caption, output_dir)


def _upload_asset(image_path: str) -> str | None:
    """Upload a local image to Krea as an asset and return its hosted URL, for
    use as a style reference. Cached by (path, mtime)."""
    try:
        path_obj = Path(image_path)
        if not path_obj.exists():
            print(f"[KREA] Reference image not found: {image_path}", flush=True)
            return None
        cache_key = (str(path_obj.resolve()), path_obj.stat().st_mtime)
    except OSError:
        cache_key = (str(image_path), 0)

    if cache_key in _asset_cache:
        return _asset_cache[cache_key]

    try:
        mime = "image/png"
        name = path_obj.name
        if name.lower().endswith((".jpg", ".jpeg")):
            mime = "image/jpeg"
        with open(path_obj, "rb") as fh:
            files = {"file": (name, fh, mime)}
            data = {"description": "Krea 2 style reference"}
            resp = requests.post(
                f"{KREA_API_BASE}/assets",
                headers=_auth_headers(json_body=False),
                files=files,
                data=data,
                timeout=60,
            )
        resp.raise_for_status()
        asset = resp.json()
    except Exception as e:
        print(f"[KREA] Asset upload failed for {image_path}: {e}", flush=True)
        return None

    asset_url = asset.get("image_url") or asset.get("url")
    if not asset_url:
        print(f"[KREA] Asset upload returned no url: {asset}", flush=True)
        return None

    _asset_cache[cache_key] = asset_url
    return asset_url


def _reference_upload_path(img_path: str) -> str:
    """Prefer the downsampled `_small.png` sidecar when uploading references."""
    if not USE_DOWNSAMPLED_FOR_IMG2IMG:
        return img_path
    p = Path(img_path)
    small = p.parent / p.name.replace(".png", "_small.png")
    return str(small) if small.exists() else img_path


# ---------------------------------------------------------------------------
# Public API (mirrors gemini_image_utils)
# ---------------------------------------------------------------------------

def generate_with_krea(
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
    """Text-to-image with Krea 2. Returns a local image path, or None on failure.

    Signature intentionally matches gemini_image_utils.generate_with_gemini so
    engine._gen_image() can call either identically.
    """
    try:
        caption = caption.encode("ascii", "ignore").decode("ascii") or "image"
    except Exception:
        caption = "image"

    if not KREA_API_KEY:
        print("[KREA IMG] FATAL: No API key! Cannot generate image!", flush=True)
        return None

    selected_model = _resolve_model(model, hd_mode)
    full_prompt = _build_text2img_prompt(prompt, time_of_day=time_of_day)

    payload = {
        "prompt": full_prompt,
        "aspect_ratio": aspect_ratio or KREA_ASPECT_RATIO,
        "resolution": KREA_RESOLUTION,
        "creativity": KREA_CREATIVITY,
    }
    print(f"[KREA IMG] text2img via {selected_model} (hd_mode={hd_mode})", flush=True)
    return _submit_and_fetch(selected_model, payload, caption, output_dir)


def generate_krea_img2img(
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
    """Image-to-image with Krea 2 via style transfer. The reference frame(s) are
    uploaded as assets and passed as `image_style_references`.

    Signature matches gemini_image_utils.generate_gemini_img2img.
    """
    try:
        caption = caption.encode("ascii", "ignore").decode("ascii") or "image"
    except Exception:
        caption = "image"

    if not KREA_API_KEY:
        print("[KREA IMG] FATAL: No API key! Cannot generate image!", flush=True)
        return None

    if isinstance(reference_image_path, str):
        image_paths = [reference_image_path]
    else:
        image_paths = list(reference_image_path or [])[:6]

    ref_strength = KREA_STYLE_STRENGTH if strength is None else float(strength)
    # Krea expects style-reference strength in 0..1; a plain img2img "strength"
    # of ~0.3 would barely carry the aesthetic, so floor it to a sane minimum.
    ref_strength = max(0.1, min(1.0, ref_strength))

    style_refs = []
    for img_path in image_paths:
        upload_path = _reference_upload_path(img_path)
        asset_url = _upload_asset(upload_path)
        if asset_url:
            style_refs.append({"url": asset_url, "strength": ref_strength})
            print(f"[KREA IMG] Style ref uploaded: {Path(img_path).name} (strength={ref_strength})", flush=True)

    if not style_refs:
        # No usable references — degrade gracefully to text-to-image.
        print("[KREA IMG] No style references available; falling back to text2img", flush=True)
        return generate_with_krea(
            prompt=prompt,
            caption=caption,
            world_prompt=world_prompt,
            time_of_day=time_of_day,
            action_context=action_context,
            hd_mode=hd_mode,
            output_dir=output_dir,
            model=model,
        )

    selected_model = _resolve_model(model, hd_mode)
    full_prompt = _build_img2img_prompt(prompt, time_of_day=time_of_day, is_flipbook=is_flipbook)

    payload = {
        "prompt": full_prompt,
        "aspect_ratio": KREA_ASPECT_RATIO,
        "resolution": KREA_RESOLUTION,
        "creativity": KREA_CREATIVITY,
        "image_style_references": style_refs,
    }
    print(f"[KREA IMG] img2img (style transfer) via {selected_model} with {len(style_refs)} ref(s)", flush=True)
    return _submit_and_fetch(selected_model, payload, caption, output_dir)


print("[KREA] Module loaded (krea_image_utils)", flush=True)
