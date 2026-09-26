"""Put the /get clips where the site can play them, and record where.

    python tools/publish_clips.py            # after tools/refresh_get.py --publish

The mp4s in static/video/get/ are ~100 MB a shoot and re-shot whenever the game
changes, so they are not committed (.gitignore). This uploads them to a new
release in the public media repo (mradfo21/abyss-media, never the builds repo,
so an installed game's updater cannot mistake a shoot for a build) and writes
that release's download base into static/video/get/clips.json — the one file
that IS committed. downloads.load_clips plays a local mp4 when there is one and
the published one otherwise, so the Render service needs no media of its own.

Commit clips.json and the posters (*.jpg) in a PR afterwards.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLIPS = ROOT / "static" / "video" / "get"
REPO = "mradfo21/abyss-media"


def main() -> int:
    mp4s = sorted(CLIPS.glob("*.mp4"))
    if not mp4s:
        print(f"no mp4s in {CLIPS} — cut them first (tools/refresh_get.py)")
        return 1
    # The phone copies go up with every shoot (tools/make_gifs.py): on a phone
    # the page shows the GIF, because the <video> may never be allowed to play.
    sys.path.insert(0, str(ROOT))
    from tools import make_gifs
    make_gifs.main(["--src", str(CLIPS)])
    files = mp4s + sorted(CLIPS.glob("*.gif"))
    tag = "clips-" + time.strftime("%Y-%m-%d-%H%M")
    subprocess.run(["gh", "release", "create", tag, "--repo", REPO, "--title", tag,
                    "--notes", "The /get clips (tools/cut_clips.py) and their phone GIFs (tools/make_gifs.py).",
                    *map(str, files)],
                   check=True)
    manifest = CLIPS / "clips.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["base"] = f"https://github.com/{REPO}/releases/download/{tag}"
    manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"{len(files)} files -> {data['base']}\ncommit {manifest.relative_to(ROOT)} (and the posters)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
