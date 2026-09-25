#!/usr/bin/env python3
"""Ship a build to friends.

    python tools/build_exe.py --clean       # make dist/<GAME>/
    python tools/publish_build.py           # zip it, put it on GitHub Releases

That is the whole release. The /get page (downloads.py) reads GitHub Releases,
so it shows the new build within a couple of minutes with no Render deploy.

    --dry-run        zip and print what would be published, publish nothing
    --version V      default: YYYY.MM.DD-<short commit>
    --notes-file F   default: the newest section of CHANGELOG.md
    --dist PATH      default: the folder in dist/ that has the game's .exe in it
    --repo O/N       default: $GAME_RELEASES_REPO or app_identity.RELEASES_REPO
    --prerelease     publish without making it "latest" (the page skips nothing
                     either way; use this for a build you are not sure of yet)

Needs the GitHub CLI, signed in once: https://cli.github.com  ->  gh auth login

What never goes in the zip, whatever is lying in the folder: .env and any
other *.env (live API keys), logs, and everything the game writes while you
play it (sessions/, archives/, tapes/, ...). If you have run the build from
dist/ yourself, your saves and keys stay on your machine.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
GITHUB_ASSET_LIMIT = 2 * 1024 ** 3

# Runtime output the game writes beside itself. The directories are kept (empty)
# in case anything expects them; their contents never ship.
RUNTIME_DIRS = {"sessions", "archives", "tapes", "images", "logs", "autotest",
                "test_sessions", "__pycache__"}
SECRET_NAMES = {".env", "keys.env", "billing.json", "config.json", "render.env", ".writable"}


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, **kw)


def _releases_repo() -> str:
    sys.path.insert(0, str(ROOT))
    import app_identity
    return app_identity.RELEASES_REPO


def find_dist(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).resolve()
        if not p.is_dir():
            sys.exit(f"--dist {p} is not a folder")
        return p
    if not DIST.is_dir():
        sys.exit("no dist/ folder. Build first: python tools/build_exe.py --clean")
    found = [d for d in DIST.iterdir() if d.is_dir() and any(d.glob("*.exe"))]
    if not found:
        sys.exit("nothing in dist/ has an .exe in it. Build first: python tools/build_exe.py --clean")
    if len(found) > 1:
        found.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        print(f"several builds in dist/, using the newest: {found[0].name}")
    return found[0]


def default_version() -> str:
    sha = run(["git", "rev-parse", "--short", "HEAD"]).stdout.strip() or "local"
    dirty = run(["git", "status", "--porcelain", "--untracked-files=no"]).stdout.strip()
    return f"{_dt.date.today():%Y.%m.%d}-{sha}" + ("-dirty" if dirty else "")


def changelog_top() -> str:
    """The newest dated section of CHANGELOG.md: from the first `# ` heading
    to the next one (or the first `---`)."""
    p = ROOT / "CHANGELOG.md"
    if not p.exists():
        return ""
    out, started = [], False
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            if started:
                break
            started = True
            continue
        if started and line.strip() == "---":
            break
        if started:
            out.append(line)
    return "\n".join(out).strip()


def shippable(rel: Path) -> bool:
    parts = rel.parts
    if any(p in RUNTIME_DIRS for p in parts[:-1]):
        return False
    name = parts[-1]
    if name in SECRET_NAMES:
        return False
    if name.endswith(".env") or (name.startswith(".env") and name != ".env.example"):
        return False
    if name.endswith((".log", ".pyc")):
        return False
    if len(parts) >= 2 and parts[-2] == "experiences" and name == ".active":
        return False
    return True


def make_zip(src: Path, dest: Path, title: str = "") -> tuple[int, list[str]]:
    """Zip the build as <title>/ with the launcher renamed <title>.exe, so
    the folder and the file a friend double-clicks carry the name the /get
    page tells them to run. (A one-folder PyInstaller launcher finds its
    files beside itself, whatever it is called.)"""
    files = sorted(p for p in src.rglob("*") if p.is_file())
    top = title or src.name
    launcher = src.name + ".exe"
    skipped: list[str] = []
    total = len(files)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".zip.part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for d in sorted(RUNTIME_DIRS - {"__pycache__"}):
            if (src / d).is_dir():
                z.writestr(f"{top}/{d}/", "")
        for i, f in enumerate(files, 1):
            rel = f.relative_to(src)
            if not shippable(rel):
                skipped.append(str(rel))
                continue
            arc = rel.as_posix()
            if arc == launcher:
                arc = top + ".exe"
            z.write(f, f"{top}/{arc}")
            if i % 400 == 0 or i == total:
                print(f"\r  zipping {i}/{total}", end="", flush=True)
    print()
    tmp.replace(dest)
    return dest.stat().st_size, skipped


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None) -> int:
    # The release notes are the CHANGELOG, emoji headings and all; a Windows
    # console in cp1252 cannot print them and the dry run died mid-report.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dist")
    ap.add_argument("--version")
    ap.add_argument("--notes-file")
    ap.add_argument("--repo", default=os.getenv("GAME_RELEASES_REPO") or _releases_repo())
    ap.add_argument("--title", default=os.getenv("GAME_TITLE"))
    ap.add_argument("--prerelease", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    src = find_dist(args.dist)
    exe = next(iter(sorted(src.glob("*.exe"))))
    # The same default as the /get page (downloads.game_title), which tells
    # a friend to run <title>.exe.
    title = args.title or "ABYSS"
    version = args.version or default_version()
    tag = f"build-{version}"
    zip_path = DIST / f"{title}-{version}-windows.zip"

    print(f"build   {src}  ({exe.name})")
    print(f"version {version}")
    size, skipped = make_zip(src, zip_path, title)
    if skipped:
        shown = ", ".join(skipped[:8]) + (f" (+{len(skipped) - 8} more)" if len(skipped) > 8 else "")
        print(f"  left out {len(skipped)} file(s) that must not ship: {shown}")
    if size > GITHUB_ASSET_LIMIT:
        sys.exit(f"{zip_path.name} is {size / 1e9:.2f} GB; GitHub takes 2 GB per file at most")
    digest = sha256(zip_path)
    print(f"zip     {zip_path.name}  {size / 1e6:,.0f} MB")
    print(f"sha256  {digest}")

    if args.notes_file:
        notes = Path(args.notes_file).read_text(encoding="utf-8").strip()
    else:
        notes = changelog_top()
    body = (notes or "Fixes and tuning.") + f"\n\n---\nRun `{title}.exe` inside the folder.\n\nsha256: {digest}\n"

    cmd = ["gh", "release", "create", tag, str(zip_path),
           "--repo", args.repo, "--title", f"{title} {version}", "--notes-file", "<notes>"]
    cmd.append("--prerelease" if args.prerelease else "--latest")

    if args.dry_run:
        print("\n--dry-run: would run\n  " + " ".join(cmd))
        print("\nrelease notes:\n" + "\n".join("  " + l for l in body.splitlines()[:30]))
        return 0

    if not shutil.which("gh"):
        sys.exit("\nthe GitHub CLI isn't installed: https://cli.github.com, then `gh auth login`.\n"
                 f"The zip is ready at {zip_path}; you can also drag it onto a new release at\n"
                 f"https://github.com/{args.repo}/releases/new (tag {tag}).")
    if run(["gh", "auth", "status"]).returncode != 0:
        sys.exit("gh isn't signed in. Run: gh auth login")

    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(body)
        notes_path = f.name
    cmd[cmd.index("<notes>")] = notes_path
    print(f"\nuploading to github.com/{args.repo} ... (a few minutes for a few hundred MB)")
    try:
        result = subprocess.run(cmd, cwd=ROOT)
    finally:
        os.unlink(notes_path)
    if result.returncode != 0:
        print("\npublish FAILED", file=sys.stderr)
        return result.returncode

    print(f"\npublished {tag}")
    print("Your /get page shows it within ~2 minutes. Send friends the /get link.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
