#!/usr/bin/env python3
"""See the /get page on this machine, served by the real app.

    python tools/serve_get.py            # http://localhost:5077/get
    python tools/serve_get.py --port 5080 --no-sample

Boots the same `api.app` production serves, with /get mounted on it, and
nothing else of the game touched:

  * the authoring sandbox is engaged first, so nothing here can write the
    live prompt file, a World or tunables (CLAUDE.md, "Anything that boots
    the server ... must carry the sandbox's environment");
  * the backend is mock, so no key is spent;
  * it listens on 127.0.0.1 only.

Until a build is published to GitHub Releases the page would say FIRST BUILD
SOON. --sample (the default here) fills the build section from this repo
instead: the version publish_build.py would give HEAD, and the headings of
the newest CHANGELOG section. DOWNLOAD then explains there is no file yet.
The frames are whatever tools/pick_frames.py last wrote.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def sample_build() -> dict:
    def git(*a):
        try:
            return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            return ""
    sha = git("rev-parse", "--short", "HEAD") or "local"
    when = git("log", "-1", "--format=%cI") or None
    day = (when or "")[:10].replace("-", ".") or "local"
    notes, started = [], False
    log = ROOT / "CHANGELOG.md"
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                if started:
                    break
                started = True
                continue
            if started and line.strip() == "---":
                break
            if started:
                notes.append(line)
    return {
        "version": f"{day}-{sha}",
        "name": f"{sha} (not published yet)",
        "published_at": when,
        "notes": "\n".join(notes),
        "download": {"url": "/get/not-yet", "filename": None, "size_bytes": None, "platform": "windows"},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=5077)
    ap.add_argument("--no-sample", dest="sample", action="store_false",
                    help="show what the live page shows today (GitHub Releases only)")
    args = ap.parse_args(argv)

    import authoring_sandbox
    authoring_sandbox.engage("serve_get: /get preview")

    import keys_store
    keys_store.mark_explicit_mock()
    os.environ.setdefault("FLASK_DEBUG", "0")
    import run_local
    run_local.configure_backend(argparse.Namespace(backend="mock", mock=True))

    if args.sample and not os.getenv("BUILDS_MANIFEST"):
        path = Path(tempfile.gettempdir()) / "somewhere_get_sample_build.json"
        path.write_text(json.dumps(sample_build(), indent=2), encoding="utf-8")
        os.environ["BUILDS_MANIFEST"] = str(path)

    from api import app  # noqa: E402  (after the sandbox and the backend)
    import downloads
    if "downloads" not in app.blueprints:     # api.py mounts it once that line lands
        app.register_blueprint(downloads.downloads_bp)

    @app.route("/get/not-yet")
    def _get_not_yet():
        return ("<body style='margin:0;background:#020504;color:#d8f0e4;font:16px system-ui;padding:48px'>"
                "<p style='max-width:32em'>No build is published yet, so there is nothing to download "
                "here. Once <code>tools/publish_build.py</code> has run, this button goes to the newest "
                "GitHub release.</p><p><a style='color:#8effc1' href='/get'>Back</a></p></body>")

    url = f"http://localhost:{args.port}/get"
    print("=" * 60)
    print(f"  /get preview: {url}")
    print(f"  sample build: {'yes' if args.sample else 'no'}   backend: mock   sandbox: on")
    print("  close this window to stop it")
    print("=" * 60, flush=True)
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
