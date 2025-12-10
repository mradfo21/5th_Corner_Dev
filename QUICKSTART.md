# ⚡ QUICKSTART - Get Running in 5 Minutes

This is the fastest way to get SOMEWHERE StoryGen running locally.

---

## 1️⃣ Prerequisites

- ✅ Python 3.11+ installed (`python --version`)
- ✅ Discord Bot created ([Get token here](https://discord.com/developers/applications))
- ✅ Gemini API key ([Get key here](https://aistudio.google.com/app/apikey))

---

## 2️⃣ Discord Bot Setup (2 minutes)

1. Go to https://discord.com/developers/applications
2. Click **"New Application"** → Name it → **Create**
3. Go to **Bot** tab → Click **"Reset Token"** → Copy token
4. Enable **MESSAGE CONTENT INTENT** (scroll down, toggle ON)
5. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`
   - Permissions: `Send Messages`, `Embed Links`, `Attach Files`, `Read Message History`
6. Copy generated URL → Paste in browser → Invite bot to your server

---

## 3️⃣ Gemini API Setup (1 minute)

1. Go to https://aistudio.google.com/app/apikey
2. Click **"Create API key"**
3. Copy the key
4. ✅ Ensure these models are enabled:
   - `gemini-2.0-flash-exp`
   - `imagen-3.0-generate-001`
   - `imagen-3.0-capability-001`

---

## 4️⃣ Install & Run (2 minutes)

### Windows:
```bash
# Clone repo
git clone https://github.com/yourusername/somewhere-storygen.git
cd somewhere-storygen

# Install dependencies
pip install -r requirements.txt

# Set environment variables
$env:DISCORD_TOKEN="your_discord_bot_token_here"
$env:GEMINI_API_KEY="your_gemini_api_key_here"

# Run bot
python bot.py
```

### Mac/Linux:
```bash
# Clone repo
git clone https://github.com/yourusername/somewhere-storygen.git
cd somewhere-storygen

# Install dependencies
pip3 install -r requirements.txt

# Set environment variables
export DISCORD_TOKEN="your_discord_bot_token_here"
export GEMINI_API_KEY="your_gemini_api_key_here"

# Run bot
python3 bot.py
```

---

## 5️⃣ Verify It's Working

You should see in terminal:
```
[STARTUP] Resetting game state (fresh simulation)...
[STARTUP] Game state cleared. Starting fresh.
BOT | Logged in as SOMEWHERE#8805
```

In Discord:
- Bot should post intro message with **▶️ PLAY** button
- Click PLAY → Game starts!

---

## 🎮 Playing the Game

1. **Click PLAY** to start
2. You'll see:
   - 🖼️ First-person POV image
   - 📍 Narrative dispatch
   - 🔘 4 choice buttons
3. **Pick a choice** within 15 seconds
4. Watch the story unfold!

### Controls:
- 🔘 **Choice 1-4** - Pick an option
- ⚡ **Custom Action** - Type your own action
- 🔄 **Restart** - New game (saves VHS tape)
- 🤖 **Auto** - Enable AI autopilot
- 🎨 **HD** - Toggle quality (Flash/Pro)
- ℹ️ **Info** - Game rules

---

## ⚠️ Troubleshooting

### "Bot doesn't connect"
- Check DISCORD_TOKEN is correct
- Check you set environment variable correctly
- Try printing: `echo $env:DISCORD_TOKEN` (Windows) or `echo $DISCORD_TOKEN` (Mac/Linux)

### "Bot connects but doesn't post intro"
- Check bot has permissions in Discord channel
- Check MESSAGE CONTENT INTENT is enabled in Developer Portal
- Try mentioning bot: `@YourBot`

### "Image generation fails"
- Check GEMINI_API_KEY is correct
- Check Gemini API key has Imagen access
- Try toggling HD mode OFF (Flash is more stable)

### "Rate limit errors"
- You're making too many requests
- Wait 1 minute and try again
- Use Flash mode (faster rate limits)

---

## 💡 Tips

- **First playthrough?** Use Flash mode (HD OFF) for faster responses
- **Want quality?** Toggle HD ON for photorealistic images
- **Going AFK?** Enable Auto mode and watch the AI play
- **Died?** Check your VHS tape GIF in the channel!
- **Stuck?** Press Restart to begin fresh

---

## 📖 Next Steps

- **Understand the system:** Read [AGENT_GUIDE.md](AGENT_GUIDE.md)
- **Deploy to cloud:** Read [DEPLOYMENT.md](DEPLOYMENT.md)
- **Modify gameplay:** Edit `prompts/simulation_prompts.json`

---

## 🆘 Still Stuck?

Check the logs:
- Terminal output for errors
- `logs/error.log` for exceptions
- `logs/world_evolution.log` for state updates

Common fixes:
- Restart bot: Kill terminal → Run `python bot.py` again
- Clear state: Delete `world_state.json`, `history.json`
- Fresh install: Delete `__pycache__` folder → Reinstall dependencies

---

**You're ready! Click PLAY and survive. 🎮📼**

