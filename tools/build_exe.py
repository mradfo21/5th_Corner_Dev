#!/usr/bin/env python3
"""Build the double-click ABYSS app.

    python tools/build_exe.py            # build
    python tools/build_exe.py --clean    # throw away previous output first
    python tools/build_exe.py --run      # build, then launch what came out
    python tools/build_exe.py --out D    # build into D/ABYSS, leaving dist/ (and any
                                         # saves in a build you play from there) alone

Output lands in `dist/ABYSS/`. That whole folder is the app: `ABYSS.exe`
plus the interpreter, the libraries and the game's content. Move the folder, not
just the exe.

Expect several minutes and a large result. mediapipe and OpenCV are most of the
weight; both are needed because the SCAN tool runs its detector on-device.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    from ship_layout import stamp_factory
except ImportError:
    from tools.ship_layout import stamp_factory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app_identity import APP_NAME  # noqa: E402

SPEC = ROOT / "ABYSS.spec"
OUT = ROOT / "dist" / APP_NAME


def _need(mod: str, pip_name: str | None = None) -> None:
    try:
        __import__(mod)
    except ImportError:
        name = pip_name or mod
        print(f"missing {name} - installing")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", name])


def _folder_size(path: Path) -> str:
    n = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    return f"{n / 1024 / 1024:,.0f} MB"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clean", action="store_true", help="Delete build/ and dist/ first.")
    ap.add_argument("--run", action="store_true", help="Launch the result when done.")
    ap.add_argument("--out", default="", help="Build into OUT/ABYSS instead of dist/ABYSS.")
    args = ap.parse_args(argv)
    global OUT
    extra = []
    if args.out:
        dist = Path(args.out).resolve()
        OUT = dist / APP_NAME
        extra = ["--distpath", str(dist), "--workpath", str(dist.parent / (dist.name + "-work"))]

    _need("PyInstaller", "pyinstaller")
    _need("webview", "pywebview")

    if args.clean:
        for d in (ROOT / "build", ROOT / "dist"):
            shutil.rmtree(d, ignore_errors=True)
        print("cleaned build/ and dist/")

    print(f"building from {SPEC.name} - this takes a few minutes")
    started = time.time()
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", *extra, str(SPEC)],
        cwd=ROOT,
    )
    if result.returncode != 0:
        print("\nbuild FAILED", file=sys.stderr)
        return result.returncode

    exe = OUT / f"{APP_NAME}.exe"
    if not exe.exists():
        print(f"\nbuild reported success but {exe} is missing", file=sys.stderr)
        return 1

    # A packaged app that cannot write next to itself loses every save, and the
    # failure only shows up the first time someone tries to keep a game.
    probe = OUT / ".writable"
    try:
        probe.write_text("x")
        probe.unlink()
        writable = True
    except OSError:
        writable = False

    # Keys are the one thing the bundle cannot carry: shipping a .env would put
    # live secrets in a distributable folder. So leave a labelled slot instead,
    # because the failure mode without one is invisible — the app opens, the UI
    # works, and only the generated content is missing.
    example = OUT / ".env.example"
    if not (OUT / ".env").exists():
        example.write_text(
            "# Easiest: open ACCOUNT in the game, choose Gemini or OpenAI, paste the key.\n"
            "# Or rename this file to  .env  and put your key in it (either one works;\n"
            "# with both, ACCOUNT picks which one plays).\n"
            "# ABYSS also looks in %APPDATA%\\ABYSS\\ (where ACCOUNT keeps its keys).\n"
            "# Without a key the game still runs, in offline mode, on canned text.\n"
            "\n"
            "GEMINI_API_KEY=\n"
            "OPENAI_API_KEY=\n"
            "# ANTHROPIC_API_KEY=\n",
            encoding="utf-8",
        )

    factory = stamp_factory(OUT)
    print("  factory: " + (", ".join(factory) if factory else "(none)"))

    print(f"\ndone in {time.time() - started:.0f}s")
    print(f"  {exe}")
    print(f"  folder is {_folder_size(OUT)}")
    print(f"  writable: {'yes' if writable else 'NO - saves will fail here'}")
    print(f"\nShip the whole {APP_NAME} folder, not just the .exe.")

    if args.run:
        subprocess.Popen([str(exe)], cwd=OUT)
        print("launched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
