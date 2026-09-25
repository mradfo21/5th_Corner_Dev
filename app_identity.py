"""Who this program is, once, for everything that names it.

The shipped identity was chosen on 2026-09-25 (docs/plans/DISTRIBUTION_MVP_PLAN.md,
M0). APP_ID and DATA_DIR_NAME are permanent the moment a player installs: the
installer keys updates on APP_ID, and DATA_DIR_NAME is where their keys,
characters, account and saves live. Changing either strands every existing
install, so they are constants here and nowhere else.

SOMEWHERE is still the name *inside* the code (modules, the SOMEWHERE_* env
vars) and was the %APPDATA% folder until the Distribution MVP. That folder is
LEGACY_DATA_DIR_NAME: M2 moves a player's data out of it into DATA_DIR_NAME once,
at first launch, and until that migration ships the stores keep reading the
legacy folder, so nobody's saved keys vanish on an update.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

APP_NAME = "ABYSS"                 # what the player sees: window, dialogs, installer, Start Menu
APP_ID = "5thCorner.ABYSS"         # the installer's package id (Velopack --packId)
DATA_DIR_NAME = "ABYSS"            # %APPDATA%\ABYSS — the player's data from the MVP on
LEGACY_DATA_DIR_NAME = "SOMEWHERE" # %APPDATA%\SOMEWHERE — where it lived before
PUBLISHER = "5th Corner"
# Public repo whose GitHub Releases carry the installers and update packages.
# The source repo goes private (M0); players' updaters and /get read this one.
RELEASES_REPO = "mradfo21/abyss-releases"

# The release pipeline (M3) stamps the git tag into _version.py; a source
# checkout has none and reports the development version.
try:
    from _version import VERSION, COMMIT  # type: ignore
except ImportError:
    VERSION = "0.1.0-dev"
    COMMIT = None


def appdata_root(name: str = DATA_DIR_NAME) -> Path:
    r"""%APPDATA%\<name>, or ~/.<name> where there is no APPDATA."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / name
    return Path.home() / f".{name.lower()}"


def data_dir() -> Path:
    r"""%APPDATA%\ABYSS — and, the first time, everything from %APPDATA%\SOMEWHERE.

    The move is a copy: the legacy folder stays where it was, so an older build
    still finds its keys if the player rolls back, and a failed copy leaves
    nothing half-moved (it lands in a temp sibling and is renamed into place).
    Only when the new folder does not exist yet, so it runs once, ever.
    """
    new = appdata_root(DATA_DIR_NAME)
    if new.exists():
        return new
    old = appdata_root(LEGACY_DATA_DIR_NAME)
    if old.is_dir():
        tmp = new.with_name(new.name + ".migrating")
        try:
            if tmp.exists():
                shutil.rmtree(tmp)
            shutil.copytree(old, tmp)
            os.replace(tmp, new)
            print(f"[identity] moved player data {old} -> {new}")
        except OSError as e:
            print(f"[identity] could not move {old}: {e}")
    return new
