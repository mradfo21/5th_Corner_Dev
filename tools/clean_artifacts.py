#!/usr/bin/env python3
"""Shrink the generated artefact folders without losing anything unrecoverable.

Two directories grow without bound. Every render writes ~1 GB of PNG stills into
`playtest_results/`, and every playthrough keeps its scene images under
`sessions/`. Together they were 3.1 GB against ~25 MB of actual source.

The stills are not the deliverable. Each run already writes MP4 flipbooks that
contain every frame in order at 0.5s apiece, plus `session.json` (the full
transcript) and `SUMMARY.md` (the write-up). So this keeps those and drops the
PNGs, which is a ~2 GB saving that costs only still-frame resolution.

Sessions are treated differently because `default` is the live game: it is never
touched. Older per-run session folders are dropped whole.

    python tools/clean_artifacts.py                 # show what would go
    python tools/clean_artifacts.py --apply         # actually delete
    python tools/clean_artifacts.py --apply --keep-frames render_20260823_145423
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RENDERS = ROOT / "playtest_results"
SESSIONS = ROOT / "sessions"

# What is worth keeping inside a finished run: the videos, the transcript and
# anything a human wrote or a report generated. All small, all not reproducible
# without paying for the run again.
KEEP_SUFFIXES = {".mp4", ".gif", ".md", ".json", ".txt", ".webm"}

# Deleting `default` would wipe the game you are in the middle of.
PROTECTED_SESSIONS = {"default", ".gitkeep"}


def _size(paths) -> int:
    return sum(p.stat().st_size for p in paths if p.is_file())


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def sweep_renders(keep_frames: set[str]) -> list[Path]:
    """Frame stills and cached zips from every run except the ones named."""
    doomed: list[Path] = []
    if not RENDERS.exists():
        return doomed
    for path in RENDERS.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(RENDERS)
        # Which run does this belong to? First path segment is close enough,
        # and nested layouts (full_run_*/scan_run_*) still match on the parent.
        run = rel.parts[0] if rel.parts else ""
        if any(k in str(rel) for k in keep_frames):
            continue
        if path.suffix.lower() in KEEP_SUFFIXES:
            continue
        # Everything left is a still, a log or a cached export.
        doomed.append(path)
    return doomed


def sweep_sessions(keep_recent: int, max_age_days: float) -> list[Path]:
    """Whole session folders that are neither live nor recent."""
    if not SESSIONS.exists():
        return []
    dirs = [d for d in SESSIONS.iterdir()
            if d.is_dir() and d.name not in PROTECTED_SESSIONS]
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    cutoff = time.time() - max_age_days * 86400
    return [d for d in dirs[keep_recent:] if d.stat().st_mtime < cutoff]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="Actually delete. Without this the run only reports.")
    ap.add_argument("--keep-frames", action="append", default=[], metavar="RUN_ID",
                    help="Keep the stills for this run (repeatable).")
    ap.add_argument("--keep-recent", type=int, default=2,
                    help="Always keep this many newest sessions (default 2).")
    ap.add_argument("--max-age-days", type=float, default=1.0,
                    help="Only prune sessions older than this (default 1).")
    args = ap.parse_args(argv)

    keep = set(args.keep_frames)
    files = sweep_renders(keep)
    sess = sweep_sessions(args.keep_recent, args.max_age_days)
    sess_files = [p for d in sess for p in d.rglob("*") if p.is_file()]

    print(f"playtest_results  {len(files):5d} stills/logs/zips   {_human(_size(files))}")
    print(f"sessions          {len(sess):5d} folders            {_human(_size(sess_files))}")
    if keep:
        print(f"keeping stills for: {', '.join(sorted(keep))}")
    total = _size(files) + _size(sess_files)

    if not args.apply:
        print(f"\nwould free {_human(total)} - rerun with --apply")
        for d in sess[:8]:
            print(f"    would drop session {d.name}")
        return 0

    gone = 0
    for p in files:
        try:
            gone += p.stat().st_size
            p.unlink()
        except OSError as e:
            print(f"  skip {p.name}: {e}", file=sys.stderr)
    for d in sess:
        try:
            gone += _size(list(d.rglob("*")))
            shutil.rmtree(d)
        except OSError as e:
            print(f"  skip {d.name}: {e}", file=sys.stderr)

    # Frame directories left behind empty read as broken runs; drop them.
    for folder in sorted(RENDERS.rglob("*"), reverse=True) if RENDERS.exists() else []:
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()

    print(f"\nfreed {_human(gone)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
