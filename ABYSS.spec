# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the double-click build. Driven by tools/build_exe.py.

One-folder, not one-file, on purpose. Every module in this app finds its data
with `Path(__file__).parent`, which under one-file resolves into a temp
directory that is wiped when the process exits -- saved games would vanish. One
folder keeps `__file__` pointing somewhere real and writable, and it starts
faster because nothing has to be unpacked on launch.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files

# Single list of what a player folder may contain. Do not glob worlds/ here.
import sys
sys.path.insert(0, str(Path(SPECPATH).resolve()))
from tools.ship_layout import bundle_datas, runtime_modules
from app_identity import APP_NAME  # the exe and its folder: ABYSS/ABYSS.exe

# Local modules that are imported lazily or by name, so the dependency graph
# walker cannot see them from play.py.
LOCAL = runtime_modules()

# Third-party packages that ship data files or resolve plugins at runtime.
datas, binaries, hiddenimports = [], [], list(LOCAL)
for pkg in ("google.genai", "mediapipe", "cv2", "anthropic", "openai",
            "imageio_ffmpeg", "replicate", "stripe", "flask_sock", "webview",
            "velopack"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as exc:                      # optional at runtime
        print(f"[spec] skipping {pkg}: {exc}")

try:
    datas += collect_data_files("certifi")
except Exception:
    pass

# The app's own content. Factory Worlds / Experiences are explicit files,
# never the operator's authoring directory.
datas += bundle_datas()

a = Analysis(
    ["play.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Server-only and dev-only weight the player never touches.
    #
    # matplotlib is NOT in this list despite looking like an obvious cut:
    # mediapipe imports it, so excluding it killed the on-device SCAN detector
    # in the packaged build only. The failure was one line in the log
    # ("[LOCAL VISION] unavailable: No module named 'matplotlib'") and SCAN
    # then silently fell back to paying Gemini for every detection.
    excludes=[
        "gunicorn", "pytest", "PyInstaller",
        "notebook", "IPython", "jupyter",
        "tkinter", "PySide6", "PyQt5", "PyQt6",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # No console window: the whole point is that it opens like a game, not a
    # script. Crashes still land in the log file play.py writes beside the exe.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon=str(Path(SPECPATH).resolve() / "assets" / "icon" / "abyss.ico"),
    # Flatten the payload next to the exe instead of the default `_internal`
    # subfolder. Every module here resolves its data as "beside me"
    # (`Path(__file__).parent`), so this makes the packaged layout identical to
    # the source layout -- without it, saves land in `_internal/sessions` while
    # play.py creates empty folders at the app root, and the two never meet.
    contents_directory=".",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
