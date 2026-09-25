"""
downloads.py — the page you send friends to get the latest build.

    /get, /download          the product page (templates/download.html)
    /get/latest              302 to the newest build's file, so a link to the
                             latest build never has to change
    /get/asset/<id>          302 to one specific build's file
    /api/builds/latest       what the page renders from, as JSON

Builds live on GitHub Releases, not on this server. Production runs one
gunicorn worker with a handful of threads; streaming a few-hundred-MB zip
through it would tie a thread up for minutes per friend, and the 1 GB disk
could hold two builds at most. So this module only ever *redirects* to the
file. Publishing a build is `python tools/publish_build.py`, and the page picks
it up within CACHE_SECONDS, with no deploy.

Where the manifest comes from, first hit wins:

  1. BUILDS_MANIFEST  — a URL or file path to JSON in the shape `_empty()`
                        returns. For builds hosted anywhere other than GitHub.
  2. GitHub Releases  — GAME_RELEASES_REPO (owner/name). The newest non-draft
                        release that has a .zip/.exe/.msi attached is "latest".
                        GITHUB_TOKEN is optional for a public repo but worth
                        setting: unauthenticated calls get 60 an hour per IP,
                        and Render's outbound IPs are shared. It is required
                        for a private repo, in which case downloads are handed
                        out as GitHub's short-lived signed links.
  3. builds/latest.json in the repo — a hand-edited fallback.

If every source fails the page still renders; it says the build is on its way
instead of offering a dead button.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from flask import Blueprint, abort, jsonify, redirect, render_template, request

downloads_bp = Blueprint("downloads", __name__)

ROOT = Path(__file__).resolve().parent
CACHE_SECONDS = int(os.getenv("BUILDS_CACHE_SECONDS", "120"))
HTTP_TIMEOUT = 4.0
BUILD_EXTS = (".zip", ".exe", ".msi", ".7z")

_cache_lock = threading.Lock()
_cache: dict = {"at": 0.0, "data": None}
# asset id -> public download URL, filled whenever the release list is read, so
# a public repo's download is one redirect with no API call of its own.
_asset_urls: dict[int, str] = {}


def game_title() -> str:
    import app_identity
    return (os.getenv("GAME_TITLE") or app_identity.APP_NAME).strip() or app_identity.APP_NAME


def releases_repo() -> str:
    import app_identity
    return (os.getenv("GAME_RELEASES_REPO") or app_identity.RELEASES_REPO).strip()


def _empty(source: str = "none") -> dict:
    return {
        "ok": False,
        "source": source,
        "title": game_title(),
        "version": None,
        "name": None,
        "published_at": None,
        "notes": "",
        "highlights": [],
        "download": None,   # {"url", "filename", "size_bytes", "platform"}
        "page": None,       # human page for the release, if any
        "history": [],      # older builds: [{"version", "published_at", "url"}]
    }


# ── GitHub ────────────────────────────────────────────────────────────

def _gh_headers(accept: str = "application/vnd.github+json") -> dict:
    h = {"Accept": accept, "User-Agent": "5th-corner-download-page"}
    tok = os.getenv("GITHUB_TOKEN", "").strip()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _get_json(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "5th-corner-download-page"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _platform_for(filename: str) -> str:
    f = filename.lower()
    if "mac" in f or "osx" in f or "darwin" in f or f.endswith(".dmg"):
        return "macos"
    if "linux" in f or f.endswith(".appimage"):
        return "linux"
    return "windows"


def _pick_asset(release: dict) -> dict | None:
    assets = [a for a in release.get("assets") or []
              if str(a.get("name", "")).lower().endswith(BUILD_EXTS)]
    if not assets:
        return None
    # The installer first (Velopack's *-Setup.exe: installs, updates itself,
    # Start Menu shortcut — M5), then a Windows build, then the biggest file,
    # which is the game rather than a stray checksum or patch. Velopack's
    # *.nupkg update packages are for the updater, never for /get.
    assets = [a for a in assets if not str(a["name"]).lower().endswith(".nupkg")] or assets

    def rank(a):
        name = str(a["name"]).lower()
        return (not name.endswith("-setup.exe"), _platform_for(a["name"]) != "windows",
                "portable" in name, -int(a.get("size") or 0))
    assets.sort(key=rank)
    return assets[0]


_HEADING = re.compile(r"^\s{0,3}#{1,3}\s+(.*\S)\s*$")
_BULLET = re.compile(r"^\s{0,3}[-*]\s+(.*\S)\s*$")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]+")


def _clean(line: str) -> str:
    line = _EMOJI.sub("", line)
    line = re.sub(r"\*\*|__|`", "", line)
    line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
    return re.sub(r"\s+", " ", line).strip(" -—:")


def highlights_from_notes(notes: str, limit: int = 6) -> list[str]:
    """The headlines of a release body. Changelog entries are written as
    `## <what changed>` sections, so the headings ARE the what's-new list; a
    body with no headings falls back to its bullets, then its first lines."""
    lines = (notes or "").splitlines()
    heads = [_clean(m.group(1)) for l in lines if (m := _HEADING.match(l))]
    heads = [h for h in heads if h and not h.upper().startswith("CHANGELOG")]
    if heads:
        return heads[:limit]
    bullets = [_clean(m.group(1)) for l in lines if (m := _BULLET.match(l))]
    if bullets:
        return [b for b in bullets if b][:limit]
    plain = [_clean(l) for l in lines if l.strip() and not l.lstrip().startswith(("sha256", "SHA256"))]
    return [p for p in plain if p][:3]


def _from_github() -> dict | None:
    repo = releases_repo()
    if not repo or "/" not in repo:
        return None
    rels = _get_json(f"https://api.github.com/repos/{repo}/releases?per_page=10", _gh_headers())
    rels = [r for r in rels if not r.get("draft")]
    out = _empty("github")
    history = []
    for rel in rels:
        asset = _pick_asset(rel)
        if not asset:
            continue
        # "build-2026.09.21-c6d8e7f" (publish_build) or "v0.1.0-beta.1" (a release tag).
        tag = rel.get("tag_name") or ""
        version = tag.removeprefix("build-").removeprefix("v") or rel.get("name")
        if asset.get("browser_download_url"):
            _asset_urls[int(asset["id"])] = asset["browser_download_url"]
        entry = {
            "version": version,
            "published_at": rel.get("published_at"),
            "url": f"/get/asset/{asset['id']}",
            "size_bytes": asset.get("size"),
        }
        if out["ok"]:
            history.append(entry)
            continue
        notes = rel.get("body") or ""
        # GitHub publishes each asset's digest ("sha256:<hex>"); older
        # releases carried it in the notes instead.
        sha = re.fullmatch(r"sha256:([0-9a-f]{64})", str(asset.get("digest") or ""), re.I) \
            or re.search(r"sha256[:\s]+([0-9a-f]{64})", notes, re.I)
        out.update({
            "ok": True,
            "version": version,
            "name": rel.get("name") or version,
            "published_at": rel.get("published_at"),
            "notes": notes,
            "highlights": highlights_from_notes(notes),
            "page": rel.get("html_url"),
            "download": {
                "url": "/get/latest",
                "asset_id": asset["id"],
                "filename": asset.get("name"),
                "size_bytes": asset.get("size"),
                "platform": _platform_for(asset.get("name", "")),
                "installer": str(asset.get("name", "")).lower().endswith("setup.exe"),
                "sha256": sha.group(1) if sha else None,
                "downloads": asset.get("download_count"),
            },
        })
    out["history"] = history[:6]
    return out if out["ok"] else None


def _asset_redirect_url(asset_id: int) -> str | None:
    """Where to send a browser for one asset. Public repos: the permanent
    browser_download_url. Private repos: ask the API for the file and hand
    out the short-lived signed link it redirects to, so the token itself
    never leaves this server."""
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token and int(asset_id) in _asset_urls:
        return _asset_urls[int(asset_id)]
    repo = releases_repo()
    api = f"https://api.github.com/repos/{repo}/releases/assets/{int(asset_id)}"
    meta = _get_json(api, _gh_headers())
    if not token:
        return meta.get("browser_download_url")

    class _NoFollow(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(_NoFollow)
    req = urllib.request.Request(api, headers=_gh_headers("application/octet-stream"))
    try:
        opener.open(req, timeout=HTTP_TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
            return e.headers["Location"]
        raise
    return meta.get("browser_download_url")


# ── other sources ─────────────────────────────────────────────────────

def _normalise(data: dict, source: str) -> dict:
    out = _empty(source)
    out.update({k: v for k, v in (data or {}).items() if k in out})
    out["ok"] = bool((out.get("download") or {}).get("url"))
    if not out["highlights"]:
        out["highlights"] = highlights_from_notes(out.get("notes") or "")
    out["title"] = game_title()
    return out


def _from_manifest_env() -> dict | None:
    ref = os.getenv("BUILDS_MANIFEST", "").strip()
    if not ref:
        return None
    if ref.startswith(("http://", "https://")):
        return _normalise(_get_json(ref), "manifest")
    return _normalise(json.loads(Path(ref).read_text(encoding="utf-8")), "manifest")


def _from_repo_file() -> dict | None:
    p = ROOT / "builds" / "latest.json"
    if not p.exists():
        return None
    return _normalise(json.loads(p.read_text(encoding="utf-8")), "file")


def latest_build(force: bool = False) -> dict:
    now = time.time()
    with _cache_lock:
        if not force and _cache["data"] is not None and now - _cache["at"] < CACHE_SECONDS:
            return _cache["data"]
    result, errors = None, []
    for source in (_from_manifest_env, _from_github, _from_repo_file):
        try:
            result = source()
        except Exception as exc:  # network down, rate limit, bad JSON
            errors.append(f"{source.__name__}: {exc}")
            result = None
        if result and result.get("ok"):
            break
    with _cache_lock:
        if not (result and result.get("ok")) and _cache["data"] and _cache["data"].get("ok"):
            # GitHub hiccuped. The last good answer is better than no button.
            _cache["at"] = now - CACHE_SECONDS + 30
            return _cache["data"]
        result = result if result and result.get("ok") else _empty()
        if errors and not result["ok"]:
            print("[downloads] no build found: " + " | ".join(errors), flush=True)
        _cache.update(at=now, data=result)
        return result


# ── clips ─────────────────────────────────────────────────────────────
# Real recordings of the game being played, cut by tools/cut_clips.py into
# static/video/get/ with a clips.json beside them. Read per request so a
# recut shows up without a restart; it's one small file.

CLIPS_DIR = ROOT / "static" / "video" / "get"


def load_clips() -> dict:
    try:
        data = json.loads((CLIPS_DIR / "clips.json").read_text(encoding="utf-8"))
    except Exception:
        data = {}
    # The mp4s are ~100 MB a shoot and are not in git; tools/publish_clips.py
    # puts them on a release in the public media repo and records its download
    # base here, so a server with no local copy (Render) still plays them.
    base = str(data.get("base") or "").rstrip("/")
    if base and not base.startswith("https://github.com/"):
        base = ""

    def where(filename: str) -> str:
        if (CLIPS_DIR / filename).is_file():
            return f"/static/video/get/{filename}"
        return f"{base}/{filename}" if base else ""

    clips = []
    for c in data.get("clips") or []:
        name = str(c.get("name") or "")
        if not re.fullmatch(r"[a-z0-9_-]{1,40}", name):
            continue
        src = where(f"{name}.mp4")
        if not src:
            continue
        clips.append({"name": name, "label": (c.get("label") or "").strip(),
                      "sub": (c.get("sub") or "").strip(),
                      "src": src, "src720": where(f"{name}-720.mp4") if c.get("src720") else "",
                      "poster": f"/static/video/get/{name}.jpg" if (CLIPS_DIR / f"{name}.jpg").is_file() else ""})
    hero = next((c for c in clips if c["name"] == "hero"), None)
    return {"hero": hero, "clips": [c for c in clips if c["name"] != "hero"]}


# ── routes ────────────────────────────────────────────────────────────

@downloads_bp.route("/get", methods=["GET"])
@downloads_bp.route("/get/", methods=["GET"])
@downloads_bp.route("/download", methods=["GET"])
def download_page():
    build = latest_build()
    # Absolute and https, because the link preview (iMessage, Discord, Slack)
    # is the first thing a friend sees, and Render terminates TLS in front of
    # us so request.url_root says http.
    base = request.url_root.rstrip("/")
    if base.startswith("http://") and "localhost" not in base and "127.0.0.1" not in base:
        base = "https://" + base[len("http://"):]
    media = load_clips()
    og = "/static/img/get/og.jpg"
    return render_template(
        "download.html",
        title=game_title(),
        build=build,
        hero=media["hero"],
        clips=media["clips"],
        # Release notes are free text; keep a stray "</script>" in them from
        # closing the inline <script> the page boots from.
        build_json=json.dumps(build).replace("</", "<\\/"),
        page_url=base + "/get",
        og_image=base + og,
    )


@downloads_bp.route("/api/builds/latest", methods=["GET"])
def api_latest_build():
    resp = jsonify(latest_build())
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp


@downloads_bp.route("/get/latest", methods=["GET"])
def get_latest():
    build = latest_build()
    dl = build.get("download") or {}
    if not build.get("ok"):
        return redirect("/get")
    if dl.get("asset_id"):
        return _send_asset(dl["asset_id"])
    return redirect(dl["url"])


@downloads_bp.route("/get/asset/<int:asset_id>", methods=["GET"])
def get_asset(asset_id: int):
    return _send_asset(asset_id)


def _send_asset(asset_id: int):
    try:
        url = _asset_redirect_url(asset_id)
    except Exception as exc:
        print(f"[downloads] asset {asset_id} lookup failed: {exc}", flush=True)
        url = None
    if not url:
        abort(404)
    return redirect(url, code=302)
