"""Make a release on this machine — the same steps as .github/workflows/release.yml.

    python tools/release_local.py v0.1.0-beta.1              # build + pack into Releases/
    python tools/release_local.py v0.1.0-beta.1 --publish    # ...and put it on abyss-releases
    python tools/release_local.py v0.1.0-beta.2 --skip-gate  # rehearsals

The fallback when CI cannot (no RELEASES_TOKEN yet, a runner outage) and the way
an update is rehearsed: pack two versions into the same Releases/ folder, install
the first from its Setup.exe with ABYSS_UPDATE_REPO pointing at that folder, and
watch it offer the second. Publishing uses `gh auth token` — this machine's
GitHub login — and nothing else.

Steps: gate (tools/ci_suite.py) → stamp → build → smoke with the install folder
read-only → notices → vpk download (for deltas, when publishing) → vpk pack →
vpk upload. Unsigned unless ABYSS_SIGN_FILE names an Azure metadata JSON.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import app_identity  # noqa: E402

RELEASES = ROOT / "Releases"
VPK = shutil.which("vpk") or str(Path.home() / ".dotnet" / "tools" / "vpk.exe")


def run(*cmd: str, **kw) -> None:
    print("\n$ " + " ".join(cmd), flush=True)
    subprocess.run(list(cmd), cwd=ROOT, check=True, **kw)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--skip-gate", action="store_true")
    ap.add_argument("--skip-smoke", action="store_true")
    args = ap.parse_args(argv)

    version = args.tag.lstrip("v")
    channel = "beta" if "-" in version else "stable"
    py = sys.executable
    app = ROOT / "dist" / app_identity.APP_NAME
    repo_url = f"https://github.com/{app_identity.RELEASES_REPO}"

    if not args.skip_gate:
        run(py, "tools/ci_suite.py")
    run(py, "tools/stamp_version.py", args.tag)
    try:
        run(py, "tools/build_exe.py", "--clean")
        if not args.skip_smoke:
            run(py, "tools/smoke_exe.py")
        run(py, "tools/third_party_notices.py", str(app))
        RELEASES.mkdir(exist_ok=True)
        token = ""
        if args.publish:
            token = subprocess.check_output(["gh", "auth", "token"], text=True).strip()
            subprocess.run([VPK, "download", "github", "--repoUrl", repo_url, "--channel", channel,
                            "--token", token, "--outputDir", str(RELEASES)]
                           + (["--pre"] if channel == "beta" else []), cwd=ROOT)
        pack = [VPK, "pack", "--packId", app_identity.APP_ID, "--packVersion", version,
                "--packDir", str(app), "--mainExe", f"{app_identity.APP_NAME}.exe",
                "--packTitle", app_identity.APP_NAME, "--packAuthors", app_identity.PUBLISHER,
                "--icon", str(ROOT / "assets" / "icon" / "abyss.ico"), "--channel", channel,
                "--runtime", "win-x64", "--framework", "webview2", "--shortcuts", "StartMenuRoot,Desktop",
                "--outputDir", str(RELEASES)]
        sign = (os.environ.get("ABYSS_SIGN_FILE") or "").strip()
        if sign:
            pack += ["--azureTrustedSignFile", sign]
        else:
            print("\n(unsigned: set ABYSS_SIGN_FILE to an Azure Artifact Signing metadata JSON to sign)")
        run(*pack)
        if args.publish:
            run(VPK, "upload", "github", "--repoUrl", repo_url, "--token", token,
                "--outputDir", str(RELEASES), "--channel", channel, "--tag", args.tag,
                "--releaseName", f"{app_identity.APP_NAME} {version}", "--publish", "--merge",
                *(["--pre"] if channel == "beta" else []))
    finally:
        (ROOT / "_version.py").unlink(missing_ok=True)
    print(f"\n{app_identity.APP_NAME} {version} ({channel}) -> {RELEASES}")
    for f in sorted(RELEASES.glob(f"*{version}*")) + sorted(RELEASES.glob("*Setup.exe")):
        print(f"  {f.name}  {f.stat().st_size // (1024 * 1024)} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
