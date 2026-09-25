"""The desktop app's server answers only the desktop app.

play.py serves the game on 127.0.0.1 so the native window can load it. Until
2026-09-25 nothing else was asked of a caller: `CORS(app)` allowed every
origin, and the `remote_addr == 127.0.0.1` checks on the key routes pass for
ANY browser tab on the machine, because a web page's fetch to 127.0.0.1 comes
from 127.0.0.1. A page could find the port via /api/health, then
`PUT /api/keys/custom` with a blank key, and keys_store.set_custom handed the
player's stored OpenAI key to the attacker's address along with every prompt.
(docs/plans/DISTRIBUTION_MVP_PLAN.md, M1.)

So a launch now mints a secret, and the server refuses its state without it:

- play.py mints a token per launch (or takes SOMEWHERE_LAUNCH_TOKEN from a
  parent that spawned it — a test, tools/smoke_exe.py) and loads
  `/standalone?launch=<token>`. That response sets an HttpOnly, SameSite=Strict
  cookie; every later request the page makes carries it. A page on another
  site cannot read it, cannot set it, and its requests never carry it.
- A request to /api/, /ws/, /images/ or /audio/ without the cookie (or an
  X-Launch-Token header, for the Python clients: the render child inherits the
  token through its environment, and a harness attaching to a running app
  reads it from logs/launch.json) is answered 403.
- The Host header must name 127.0.0.1 or localhost on our port, which is what
  stops DNS rebinding: a hostile name re-pointed at 127.0.0.1 still arrives
  with its own name in Host.

Armed like api.enable_shutdown(): only the desktop launcher calls arm(), so
hosted gunicorn, run_local.py and the e2e suites are untouched.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Optional

COOKIE = "abyss_launch"
HEADER = "X-Launch-Token"
QUERY = "launch"
ENV = "SOMEWHERE_LAUNCH_TOKEN"

# The paths that read or change the run. Pages and /static are the game's own
# code and carry nothing of the player's; /images and /audio are their frames
# and voices, so they are guarded with the API.
GUARDED_PREFIXES = ("/api/", "/ws/", "/images/", "/audio/")

_token: Optional[str] = None
_port: Optional[int] = None


def mint() -> str:
    """The launch's secret: a parent's, if it handed one down, else fresh."""
    given = (os.environ.get(ENV) or "").strip()
    return given if len(given) >= 32 else secrets.token_urlsafe(32)


def arm(token: str, port: int) -> None:
    global _token, _port
    if not token or len(token) < 32:
        raise ValueError("launch token too short")
    _token, _port = token, int(port)
    # Children (the render harness) inherit it; nothing else needs to ask.
    os.environ[ENV] = token


def disarm() -> None:
    """Tests only."""
    global _token, _port
    _token = _port = None


def armed() -> bool:
    return _token is not None


def _matches(candidate: Optional[str]) -> bool:
    return bool(candidate) and _token is not None and hmac.compare_digest(
        str(candidate).encode("utf-8"), _token.encode("utf-8"))


def host_allowed(host: Optional[str]) -> bool:
    if not armed():
        return True
    host = (host or "").strip().lower()
    return host in (f"127.0.0.1:{_port}", f"localhost:{_port}")


def is_guarded(path: str) -> bool:
    return (path or "").startswith(GUARDED_PREFIXES)


def check(request) -> Optional[str]:
    """None when the request may proceed, else the reason it may not."""
    if not armed():
        return None
    if not host_allowed(request.host):
        return "host"
    if not is_guarded(request.path):
        return None
    if _matches(request.cookies.get(COOKIE)) or _matches(request.headers.get(HEADER)):
        return None
    if _matches(request.args.get(QUERY)):
        return None
    return "token"


def wants_cookie(request) -> bool:
    """The launch URL: hand the page its cookie."""
    return armed() and _matches(request.args.get(QUERY)) and not _matches(
        request.cookies.get(COOKIE))


def set_cookie(response) -> None:
    response.set_cookie(COOKIE, _token, httponly=True, samesite="Strict", path="/")


# ── for the Python clients of a running desktop app ──────────────────────

def launch_file(root: Path) -> Path:
    return Path(root) / "logs" / "launch.json"


def write_launch_file(root: Path, token: str, port: int) -> None:
    """So a harness attached to this app (playtest_app.py over CDP) can make
    its own HTTP calls. Readable by this Windows user only, like keys.env —
    the threat is a web page, not a program already running as the player."""
    p = launch_file(root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"port": int(port), "token": token, "pid": os.getpid()}),
                     encoding="utf-8")
    except OSError:
        pass


def client_headers(root: Optional[Path] = None) -> dict:
    """{X-Launch-Token: …} for a Python client, from the environment (a
    child of the app) or the launch file (a harness beside it); {} when
    neither exists, which is right for run_local.py and hosted servers."""
    token = (os.environ.get(ENV) or "").strip()
    if not token and root is not None:
        try:
            token = json.loads(launch_file(root).read_text(encoding="utf-8")).get("token") or ""
        except Exception:
            token = ""
    return {HEADER: token} if token else {}
