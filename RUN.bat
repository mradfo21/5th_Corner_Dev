@echo off
REM ============================================================
REM  SOMEWHERE - run the LATEST source, in a window, with logs.
REM
REM  This is the one to double-click when you just want to see
REM  the current build: it opens a resizable window and keeps
REM  this console open so you can read what the engine is doing
REM  (and any errors) while you play or watch.
REM
REM  - RUN.bat   -> windowed + console (this file; for dev/preview)
REM  - PLAY.bat  -> fullscreen, no console (the clean player launch)
REM
REM  Any flags you pass are forwarded to play.py, e.g.:
REM     RUN.bat --mock          (fully offline, no API keys)
REM     RUN.bat --browser       (use your default browser instead)
REM ============================================================
cd /d "%~dp0"
title SOMEWHERE
echo.
echo [SOMEWHERE] Launching LATEST source from:
echo   %CD%
echo Close this window only after you quit the game.
echo.
set PYTHONUNBUFFERED=1
python -B play.py --windowed %*
echo.
echo [SOMEWHERE] The app has closed. Press any key to dismiss this window.
pause >nul
