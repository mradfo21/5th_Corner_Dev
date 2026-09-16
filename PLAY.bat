@echo off
REM Double-click to play from source, no terminal window left behind.
REM pythonw runs without a console; if it is missing, fall back to python.
cd /d "%~dp0"
where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw play.py %*
) else (
    start "" python play.py %*
)
