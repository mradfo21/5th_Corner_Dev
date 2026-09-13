# System Reset Instructions

## Quick Reset (Recommended)

### Windows (PowerShell)
```powershell
.\reset_and_restart.ps1
```

### Linux/Mac
```bash
chmod +x reset_and_restart.sh
./reset_and_restart.sh
```

---

## Manual Reset (Step by Step)

If you prefer to do it manually or troubleshoot:

### 1. Stop the Bot
```powershell
# Windows
Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force

# Linux/Mac
pkill -f "python.*bot.py"
```

### 2. Clear All Sessions
```powershell
# Windows
Remove-Item -Path "sessions" -Recurse -Force

# Linux/Mac
rm -rf sessions
```

### 3. Restart the Bot
```powershell
# Windows
python bot.py

# Linux/Mac
python bot.py
```

---

## What Gets Cleared

When you reset, the following are deleted:

- ✅ All game states (`sessions/*/state.json`)
- ✅ All game histories (`sessions/*/history.json`)
- ✅ All generated images (`sessions/*/images/`)
- ✅ All tape animations (`sessions/*/tapes/`)
- ✅ All video segments (`sessions/*/films/segments/`)
- ✅ All final stitched videos (`sessions/*/films/final/`)

## What's Preserved

These files are NOT affected by reset:

- ✅ Source code
- ✅ Configuration files (`ai_config.json`, `.env`)
- ✅ Prompt definitions (`prompts/simulation_prompts.json`)
- ✅ Documentation

---

## Troubleshooting

### Bot won't stop?
```powershell
# Windows - Force kill ALL Python processes
taskkill /F /IM python.exe

# Linux/Mac - Force kill ALL Python processes
killall -9 python
```

### Sessions directory won't delete?
Make sure the bot is stopped first, then try:
```powershell
# Windows
Remove-Item -Path "sessions" -Recurse -Force -ErrorAction Continue

# Linux/Mac
sudo rm -rf sessions
```

### Want to keep a specific session?
Before clearing, back it up:
```powershell
# Windows
Copy-Item -Path "sessions\my_session" -Destination "backup\my_session" -Recurse

# Linux/Mac
cp -r sessions/my_session backup/my_session
```

---

## Status: ✅ System is Clean!

After reset, you'll see:
```
sessions/
└── default/         # Fresh session created on bot startup
    ├── state.json   # New game state
    ├── history.json # Empty history
    └── images/      # Empty directory
```

