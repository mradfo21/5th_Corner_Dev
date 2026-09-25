"""Write THIRD-PARTY-NOTICES.txt into a build: every bundled package's licence.

    python tools/third_party_notices.py dist/ABYSS

A distributed build carries other people's code, and most of their licences
(MIT, BSD, Apache-2.0) require their notice to travel with it. This walks the
packages installed from requirements.lock — the same set PyInstaller bundles —
and copies each one's licence files verbatim, with name, version and licence.

The ffmpeg binary imageio-ffmpeg ships is a GPLv3 build. Render/tape export
uses it, so it stays (docs/plans/DISTRIBUTION_MVP_PLAN.md, M3), and the GPL's
terms come with it: the notice below names the exact version and where its
corresponding source is. Replacing it with OpenCV's LGPL ffmpeg is the
post-MVP "size trimming" item.

Standard library only (importlib.metadata), so a build needs nothing extra.
"""
from __future__ import annotations

import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "requirements.lock"
LICENSE_HINTS = ("license", "licence", "copying", "notice", "authors")
# In the lock because they MAKE the build; not in it. Only PyInstaller's
# bootloader ships, under the GPL's bootloader exception (said below).
BUILD_ONLY = {"pyinstaller", "pyinstaller-hooks-contrib"}
BOOTLOADER_NOTICE = """PyInstaller bootloader
======================
ABYSS.exe starts through the PyInstaller bootloader (https://pyinstaller.org),
GPLv2 with the bootloader exception, which permits distributing it with
programs under any licence. PyInstaller itself is a build tool and not included.
"""

FFMPEG_NOTICE = """\
FFmpeg
======
This program includes an FFmpeg executable ({exe}), version {version},
distributed by the imageio-ffmpeg project (https://github.com/imageio/imageio-ffmpeg).
It is a GPLv3 build: https://www.gnu.org/licenses/gpl-3.0.txt
The corresponding source code for this build is available from
https://ffmpeg.org/releases/ and https://github.com/imageio/imageio-binaries,
and on request from 5th Corner for three years from the date you received
this program. FFmpeg is a trademark of Fabrice Bellard.
"""


def locked_names() -> list[str]:
    names = []
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "==" in line:
            names.append(line.split("==", 1)[0].strip())
    return sorted(set(names), key=str.lower)


def licence_of(dist) -> str:
    md = dist.metadata
    expr = md.get("License-Expression")
    if expr:
        return expr
    lic = (md.get("License") or "").strip()
    if lic and len(lic) < 80 and "\n" not in lic:
        return lic
    classifiers = [c.split("::")[-1].strip() for c in md.get_all("Classifier") or []
                   if c.startswith("License ::")]
    return ", ".join(classifiers) or (lic.splitlines()[0][:80] if lic else "see text")


def licence_texts(dist) -> list[tuple[str, str]]:
    out = []
    for f in dist.files or []:
        name = f.name.lower()
        if any(h in name for h in LICENSE_HINTS) and not name.endswith((".py", ".pyc")):
            try:
                text = f.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                continue
            if text and text.strip():
                out.append((str(f), text.strip()))
    if not out:
        body = (dist.metadata.get("License") or "").strip()
        if len(body) > 80:
            out.append(("METADATA License", body))
    return out


def ffmpeg_notice() -> str:
    try:
        import imageio_ffmpeg
        exe = Path(imageio_ffmpeg.get_ffmpeg_exe()).name
        version = imageio_ffmpeg.get_ffmpeg_version()
    except Exception:  # noqa: BLE001
        exe, version = "ffmpeg", "unknown"
    return FFMPEG_NOTICE.format(exe=exe, version=version)


def build(out_dir: Path) -> Path:
    parts = ["ABYSS - third-party notices",
             "=" * 27,
             "ABYSS includes the following third-party software. Each is the",
             "property of its authors and is used under the licence shown.", ""]
    summary, bodies, missing = [], [], []
    for name in locked_names():
        if name.lower() in BUILD_ONLY:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            missing.append(name)
            continue
        lic = licence_of(dist)
        summary.append(f"  {dist.metadata['Name']:<32} {dist.version:<14} {lic}")
        texts = licence_texts(dist)
        head = f"{dist.metadata['Name']} {dist.version} — {lic}"
        bodies.append("\n".join([head, "-" * min(len(head), 78)] +
                                ([f"[{src}]\n{txt}\n" for src, txt in texts] or
                                 ["(no licence file shipped in the package; see its project page)\n"])))
    parts += ["Summary", "-------"] + summary + ["", ffmpeg_notice(), BOOTLOADER_NOTICE, ""]
    parts += ["Licence texts", "=============", ""] + bodies
    out = Path(out_dir) / "THIRD-PARTY-NOTICES.txt"
    out.write_text("\n".join(parts), encoding="utf-8")
    print(f"{out}: {len(summary)} packages" + (f", not installed: {', '.join(missing)}" if missing else ""))
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    build(Path(sys.argv[1]))
