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
from pathlib import Path

APP_NAME = "ABYSS"                 # what the player sees: window, dialogs, installer, Start Menu
APP_ID = "5thCorner.ABYSS"         # the installer's package id (Velopack --packId)
DATA_DIR_NAME = "ABYSS"            # %APPDATA%\ABYSS — the player's data from the MVP on
LEGACY_DATA_DIR_NAME = "SOMEWHERE" # %APPDATA%\SOMEWHERE — where it lived before
PUBLISHER = "5th Corner"

# The release pipeline (M3) stamps the git tag into _version.py; a source
# checkout has none and reports the development version.
try:
    from _version import VERSION  # type: ignore
except ImportError:
    VERSION = "0.1.0-dev"


def appdata_root(name: str = LEGACY_DATA_DIR_NAME) -> Path:
    """%APPDATA%\\<name>, or ~/.<name> where there is no APPDATA.

    Defaults to the LEGACY folder on purpose: callers switch to DATA_DIR_NAME
    in the same change that adds the migration (M2), never before."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / name
    return Path.home() / f".{name.lower()}"
