"""A captured bug, sent to us — only when the player says so, never with a key.

bug_report.capture() writes a folder on the player's machine. Until the
Distribution MVP that was the end of it: the evidence lived on a stranger's
disk. This is the rest of the trip (docs/plans/DISTRIBUTION_MVP_PLAN.md, M5):

  player's app                                  5th Corner's site
  ────────────                                  ─────────────────
  manifest(id)  what would be sent (the client shows it and asks)
  package(id)   one zip: every text file redacted (safe_log), the frame,
                build.json (version, commit) — capped at MAX_BYTES
  send(id)  ──── POST /api/bug/intake ────▶     receive(): size cap, per-IP
                                                rate limit, stored under
                                                bugs/intake/, a summary posted
                                                to BUG_WEBHOOK_URL (private
                                                Discord), a receipt back

Opt-in every time: nothing here runs unless the player pressed SEND after
seeing the list. The intake never trusts the zip — it stores it and reads only
REPORT.md's first lines for the summary.
"""
from __future__ import annotations

import io
import json
import os
import secrets
import threading
import time
import zipfile
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import app_identity
import safe_log

MAX_BYTES = 10 * 1024 * 1024
TEXT_EXTS = (".md", ".json", ".txt", ".log", ".jsonl", ".html", ".csv")
INTAKE_URL = "https://www.5th-corner.com/api/bug/intake"
RATE_PER_HOUR = 6


def _bugs_dir() -> Path:
    import bug_report
    return Path(bug_report.BUGS_DIR)


def _folder(bug_id: str) -> Optional[Path]:
    name = "".join(c for c in str(bug_id or "") if c.isalnum() or c in "_-")
    if not name:
        return None
    folder = _bugs_dir() / name
    return folder if folder.is_dir() else None


def manifest(bug_id: str) -> Optional[List[Dict[str, Any]]]:
    folder = _folder(bug_id)
    if folder is None:
        return None
    out = []
    for f in sorted(folder.rglob("*")):
        if f.is_file():
            out.append({"name": f.relative_to(folder).as_posix(), "bytes": f.stat().st_size,
                        "redacted": f.suffix.lower() in TEXT_EXTS})
    return out


def package(bug_id: str) -> Tuple[bytes, List[str]]:
    """The zip that would be sent, and the names in it. Largest images are
    dropped first if the whole would exceed MAX_BYTES."""
    folder = _folder(bug_id)
    if folder is None:
        raise FileNotFoundError(bug_id)
    files = [f for f in folder.rglob("*") if f.is_file()]
    texts = [f for f in files if f.suffix.lower() in TEXT_EXTS]
    others = sorted((f for f in files if f not in texts), key=lambda f: f.stat().st_size)
    build = {"app": app_identity.APP_NAME, "version": app_identity.VERSION,
             "commit": getattr(app_identity, "COMMIT", None), "bug_id": folder.name,
             "sent_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    buf = io.BytesIO()
    names: List[str] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("build.json", json.dumps(build, indent=1))
        names.append("build.json")
        for f in texts:
            rel = f.relative_to(folder).as_posix()
            z.writestr(rel, safe_log.redact(f.read_text(encoding="utf-8", errors="replace")))
            names.append(rel)
        budget = MAX_BYTES - buf.tell() - 64 * 1024
        for f in others:
            size = f.stat().st_size
            if size > budget:
                continue
            z.write(f, f.relative_to(folder).as_posix())
            names.append(f.relative_to(folder).as_posix())
            budget -= size
    return buf.getvalue(), names


def send(bug_id: str, note: str = "", url: Optional[str] = None, timeout: float = 60) -> Dict[str, Any]:
    import requests
    data, names = package(bug_id)
    target = (url or os.environ.get("BUG_INTAKE_URL") or INTAKE_URL).strip()
    r = requests.post(target, files={"report": (f"{bug_id}.zip", data, "application/zip")},
                      data={"note": safe_log.redact(note or "")[:2000]},
                      headers={"X-ABYSS-Version": app_identity.VERSION}, timeout=timeout)
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200 or not body.get("ok"):
        raise RuntimeError(body.get("error") or f"intake answered {r.status_code}")
    return {"ok": True, "receipt": body.get("receipt"), "files": names, "bytes": len(data)}


# ── the site's side ─────────────────────────────────────────────────────

_hits: Dict[str, deque] = defaultdict(deque)
_hits_lock = threading.Lock()


def rate_ok(ip: str, now: Optional[float] = None) -> bool:
    now = time.time() if now is None else now
    with _hits_lock:
        q = _hits[ip or "?"]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= RATE_PER_HOUR:
            return False
        q.append(now)
        return True


def _summary(data: bytes, note: str, version: str) -> str:
    head = ""
    build = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if "REPORT.md" in z.namelist():
                head = "\n".join(z.read("REPORT.md").decode("utf-8", "replace").splitlines()[:12])
            if "build.json" in z.namelist():
                build = json.loads(z.read("build.json").decode("utf-8", "replace"))
    except (zipfile.BadZipFile, ValueError, KeyError):
        head = "(not a readable zip)"
    v = build.get("version") or version or "?"
    return safe_log.redact(f"**ABYSS bug** · {v}\n{note[:300]}\n```\n{head[:1200]}\n```")


def receive(data: bytes, note: str, version: str, ip: str, root: Path) -> Dict[str, Any]:
    """Store one report and tell us about it. The caller has already checked
    the size and the rate."""
    receipt = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
    dest = Path(root) / "bugs" / "intake"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"{receipt}.zip").write_bytes(data)
    (dest / f"{receipt}.json").write_text(json.dumps(
        {"receipt": receipt, "note": note[:2000], "version": version, "ip": ip,
         "bytes": len(data)}, indent=1), encoding="utf-8")
    hook = (os.environ.get("BUG_WEBHOOK_URL") or "").strip()
    if hook:
        text = _summary(data, note, version)[:1900]
        threading.Thread(target=_post_webhook, args=(hook, text, receipt), daemon=True).start()
    return {"ok": True, "receipt": receipt}


def _post_webhook(hook: str, text: str, receipt: str) -> None:
    try:
        import requests
        requests.post(hook, json={"content": f"{text}\nreceipt `{receipt}`"}, timeout=15)
    except Exception as e:  # noqa: BLE001
        print(f"[BUG] webhook failed for {receipt}: {e}")
