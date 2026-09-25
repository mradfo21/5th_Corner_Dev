"""Write _version.py from a release tag, so the build knows what it is.

    python tools/stamp_version.py v0.1.0          # from the release workflow
    python tools/stamp_version.py v0.2.0-beta.3

app_identity.VERSION reads it; /api/health, the window's log header and bug
reports show it; Velopack packs under it (--packVersion). A source checkout has
no _version.py and reports 0.1.0-dev. The tag must be SemVer — Velopack orders
updates by it, so a malformed tag would ship a build no one is offered.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?$")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    version = argv[0].strip()
    version = version[1:] if version.startswith("v") else version
    if not SEMVER.match(version):
        print(f"not a SemVer tag: {argv[0]!r}")
        return 2
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:  # noqa: BLE001
        commit = ""
    (ROOT / "_version.py").write_text(
        '"""Stamped by tools/stamp_version.py from the release tag. Not committed."""\n'
        f"VERSION = {version!r}\nCOMMIT = {commit or None!r}\n", encoding="utf-8")
    print(f"_version.py: {version} ({commit[:7] or 'no commit'})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
