# 📦 TRANSFER NOTES - Ready to Deploy

## ✅ PROJECT IS CLEAN AND READY

All cleanup completed. Repository is ready to copy to your deployment folder.

---

## 🔑 IMPORTANT: API Keys Preserved

**`config.json` contains your working API keys:**
- ✅ `DISCORD_TOKEN` - Your bot token
- ✅ `GEMINI_API_KEY` - Your Gemini key
- ✅ `OPENAI_API_KEY` - Legacy key (not used)

**These keys are KEPT in the repo so the bot works when deployed.**

---

## 📁 What Was Cleaned Up

### Deleted:
- ❌ All generated images (`images/`)
- ❌ All VHS tapes (`tapes/`)
- ❌ All logs (`logs/`)
- ❌ Runtime state files (`world_state.json`, `history.json`)
- ❌ Python cache (`__pycache__/`)

### Kept:
- ✅ All source code (`bot.py`, `engine.py`, etc.)
- ✅ All prompts (`prompts/simulation_prompts.json`)
- ✅ Configuration with API keys (`config.json`)
- ✅ Dependencies list (`requirements.txt`)
- ✅ Render config (`render.yaml`)
- ✅ All documentation (see below)

---

## 📚 Documentation Created

### For Users:
- **`README.md`** - Main project overview, features, quick reference
- **`QUICKSTART.md`** - 5-minute setup guide for local testing

### For Developers/AI:
- **`AGENT_GUIDE.md`** - Comprehensive technical guide (MOST IMPORTANT for next agent)
  - Architecture overview
  - How all systems work
  - Prompt engineering philosophy
  - Common issues & solutions
  - Design insights
- **`DEPLOYMENT.md`** - Cloud deployment instructions
  - Render (recommended)
  - Railway, AWS, Docker options
  - Environment variables
  - Cost estimates

### For Deployment:
- **`config.json.example`** - Template for other users (keys removed)
- **`.gitignore`** - Configured to keep `config.json` (for your deployment)

---

## 🚀 Next Steps

### 1. Copy to Deployment Folder
```bash
# Copy entire directory to new location
cp -r SOMEWHERE_StoryGen /path/to/deployment/folder
```

### 2. Verify Files Present
Check these critical files exist:
- ✅ `bot.py`
- ✅ `engine.py`
- ✅ `config.json` (with your keys)
- ✅ `requirements.txt`
- ✅ `render.yaml`
- ✅ `prompts/simulation_prompts.json`

### 3. Deploy to Render
1. Push to GitHub (or connect local folder to Render)
2. Create new **Worker** (not Web Service) on Render
3. Connect repo
4. Render will auto-detect `render.yaml` and deploy
5. If needed, set environment variables in Render dashboard:
   - `DISCORD_TOKEN` (from config.json)
   - `GEMINI_API_KEY` (from config.json)

### 4. Verify Deployment
- Check Render logs for: `BOT | Logged in as SOMEWHERE#...`
- Check Discord for intro message
- Click PLAY and test a turn

---

## 🎯 What the Next Agent Needs to Know

### Start Here:
1. **Read `AGENT_GUIDE.md` first** - Everything is explained there
2. Most game logic is in **`prompts/simulation_prompts.json`**
3. Bot orchestration is in **`bot.py`**
4. Game engine is in **`engine.py`**

### Key Concepts:
- **Prompt-driven architecture** - LLM prompts control 80% of behavior
- **Multimodal AI** - Images are passed to LLMs for better text generation
- **Img2img continuity** - Each scene builds on previous for visual flow
- **Fair but tense difficulty** - Normal actions safe, risky actions punished
- **Timeout penalty system** - Hesitation causes brutal consequences
- **Death replay GIFs** - All images saved as VHS tapes

### If Things Break:
- Check prompts first, code second
- Most issues are prompt engineering problems
- Read "Common Issues & Solutions" in `AGENT_GUIDE.md`

---

## 🔧 Current State

### Bot Configuration:
- **HD Mode:** Toggle button (Flash vs Pro images)
- **Auto-Play:** Toggle button (AI autopilot)
- **Countdown:** 15 seconds per turn
- **Auto-Play Speed:** 45 seconds per turn
- **Tape Generation:** On death + on restart

### Recent Features Implemented:
✅ Fixed auto-play + countdown conflict  
✅ Added tape generation on restart (for overnight runs)  
✅ Sanitized violence prompts for Gemini safety  
✅ Dynamic reference image count (1 for action, 2 for stationary)  
✅ Proper async task cancellation on restart  
✅ Fair consequence system (movement is safe)  
✅ Improved choice variety (BOLD, CLEVER, WEIRD options)  

### Known Working:
- ✅ Image generation (both Flash and Pro)
- ✅ Text generation (narrative, choices, consequences)
- ✅ Death replays (GIF creation)
- ✅ Button UI (all controls working)
- ✅ Game restart (with tape save)
- ✅ Auto-play mode (with proper countdown handling)

---

## 💡 Tips for Next Agent

### User's Vision:
- **"Fair but tense"** survival experience
- **Visually stunning** photorealistic imagery
- **Punishing but not unfair** difficulty
- **First-person immersion** (bodycam aesthetic)

### When User Says "This feels unfun":
- They mean it's either too random, too punishing, or not responsive
- Solution: Adjust prompts in `simulation_prompts.json`
- Test by actually playing the game

### When Images Look Wrong:
- Check `image_negative_prompt` in prompts file
- Check if safety filters blocked (sanitize more terms)
- Try toggling HD mode

### When Choices Feel Stale:
- Ensure choices generated from current image + dispatch only
- Check `player_choice_generation_instructions` prompt
- Vision analysis must be working

---

## 📞 Emergency Reference

### If Bot Won't Start:
1. Check logs for errors
2. Verify `DISCORD_TOKEN` in `config.json`
3. Verify `GEMINI_API_KEY` in `config.json`
4. Check MESSAGE CONTENT INTENT enabled in Discord Portal

### If Images Fail:
1. Check Gemini API quota/rate limits
2. Try Flash mode (more forgiving)
3. Check if prompt sanitization is working
4. Look for "That's not an image I can process" errors

### If Game Feels Broken:
1. Delete `world_state.json` and `history.json`
2. Restart bot
3. Fresh game should work

---

## 🎬 Final Checklist

Before deploying:
- ✅ All code cleaned up
- ✅ Documentation complete
- ✅ API keys preserved in `config.json`
- ✅ Runtime files cleared
- ✅ `.gitignore` configured
- ✅ `render.yaml` configured for Render deployment
- ✅ Bot tested and working locally (if you ran it)

---

## 🌟 What Makes This Special

This isn't just a Discord bot - it's an **emergent storytelling engine** where:
- Every scene is unique (AI-generated in real-time)
- Player choices genuinely matter (consequences are evaluated)
- The world evolves organically (not scripted)
- Death is meaningful (you get a replay tape)
- Tension is real (countdown timer creates urgency)

The user has put a lot of thought into making this **feel like a real survival experience** rather than a random story generator.

---

**Good luck with deployment! The bot is ready to run. 🚀📼**

**Next agent: Read `AGENT_GUIDE.md` and you'll understand everything. Trust the user's instincts - they know what feels fun.**

