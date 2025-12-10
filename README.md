# 🎮 SOMEWHERE STORYGEN

**An AI-driven first-person survival horror game that runs in Discord**

Every scene is generated in real-time by AI. Every choice matters. Death is permanent. The world evolves based on your actions.

![Python](https://img.shields.io/badge/python-3.11+-blue.svg)
![Discord.py](https://img.shields.io/badge/discord.py-2.0+-blue.svg)
![Gemini](https://img.shields.io/badge/Gemini-2.0-orange.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)

---

## 🌟 Features

- 🖼️ **Photorealistic AI-Generated Scenes** - Every image created in real-time with Google Gemini Imagen
- 📖 **Dynamic Narrative** - Story evolves based on your choices, powered by Gemini 2.0 Flash
- ⚡ **Real Consequences** - Fair but tense gameplay where risky choices have real outcomes
- ⏱️ **Timeout Penalties** - Hesitate too long and the world punishes you
- 🎬 **Death Replays** - Every run saved as a VHS tape GIF
- 🤖 **Auto-Play Mode** - Watch the AI play itself
- 🎨 **HD Toggle** - Switch between fast/quality image generation
- 👁️ **First-Person POV** - Immersive bodycam-style perspective

---

## 🎯 How It Works

```
Player sees image → Reads dispatch → Makes choice → Consequence calculated → World evolves → New image generated → Repeat
```

- **4 choices per turn** - Always varied, never generic
- **15-second countdown** - Choose quickly or face brutal consequences
- **Vision-grounded choices** - Options based on what's actually visible in the scene
- **Persistent world state** - Location, health, inventory, environment all tracked
- **Fair difficulty** - Normal movement is safe, reckless actions are punished

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- Discord Bot Token ([Get one here](https://discord.com/developers/applications))
- Google Gemini API Key ([Get one here](https://aistudio.google.com/app/apikey))

### Installation

```bash
# 1. Clone repository
git clone https://github.com/yourusername/somewhere-storygen.git
cd somewhere-storygen

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set environment variables
export DISCORD_TOKEN="your_discord_bot_token"
export GEMINI_API_KEY="your_gemini_api_key"

# 4. Run bot
python bot.py
```

### Discord Setup

1. Enable **MESSAGE CONTENT INTENT** in Discord Developer Portal
2. Invite bot with permissions: `Send Messages`, `Embed Links`, `Attach Files`, `Read Message History`
3. Bot will post intro message when it connects

---

## 📖 Documentation

- **[AGENT_GUIDE.md](AGENT_GUIDE.md)** - Comprehensive technical guide for developers
- **[DEPLOYMENT.md](DEPLOYMENT.md)** - Cloud deployment instructions (Render, Railway, AWS, Docker)

---

## 🎮 Gameplay

### Core Loop

1. **See** - AI generates photorealistic first-person image
2. **Read** - Narrative dispatch describes your situation
3. **Choose** - Pick from 4 varied choices (or type custom action)
4. **Survive** - Face consequences, adapt to evolving world

### Game Mechanics

- **Health System** - Injuries accumulate, death is permanent
- **Inventory** - Pick up and use items
- **Environment** - World evolves based on time and actions
- **NPCs** - Characters remember your actions
- **Timeout Penalties** - Hesitation has severe consequences

### Controls

- 🔘 **Choice Buttons** - Pick from 4 AI-generated options
- ⚡ **Custom Action** - Type your own action
- 🔄 **Restart** - Start new game (saves VHS tape)
- 🤖 **Auto-Play** - Enable AI auto-pilot
- 🎨 **HD Toggle** - Switch between quality/speed
- ℹ️ **Info** - View game rules

---

## 🏗️ Architecture

```
bot.py                          # Discord bot, UI, game orchestration
engine.py                       # Game engine, turn processing
choices.py                      # AI choice generation
gemini_image_utils.py           # Image generation pipeline
evolve_prompt_file.py           # World state evolution
prompts/simulation_prompts.json # All LLM prompts (core game logic)
config.json                     # API keys and model configs
```

### Key Technologies

- **Discord.py** - Bot framework
- **Google Gemini 2.0 Flash** - Text generation
- **Google Imagen 3** - Image generation (text-to-image + img2img)
- **Pillow (PIL)** - Image processing, GIF creation
- **asyncio** - Async task management

---

## 🎨 Prompt Engineering

This game is **prompt-driven** - 80% of behavior is controlled by LLM prompts in `prompts/simulation_prompts.json`.

Key prompts:
- `action_consequence_instructions` - How outcomes are determined
- `player_choice_generation_instructions` - How choices are created
- `timeout_penalty_instructions` - What happens when you hesitate
- `world_evolution_instructions` - How the environment changes
- `gemini_image_to_image_instructions` - Visual continuity rules

---

## 💰 Cost Estimates

**Per 30-turn game (Flash mode):**
- Text generation: ~$0.05
- Image generation: ~$1.20
- **Total: ~$1.25**

**Monthly (10 games/day):**
- ~$375/month in API costs
- Hosting: $0-10/month (Render free tier available)

---

## 🔧 Configuration

Edit `prompts/simulation_prompts.json` to modify:
- Difficulty (consequence severity)
- Image style (negative prompts, art direction)
- Choice variety (types of options offered)
- World behavior (environment evolution rules)

Edit `bot.py` constants:
- `AUTO_PLAY_DELAY` - Time between auto-play choices (default: 45s)
- `COUNTDOWN_DURATION` - Timeout penalty timer (default: 15s)
- `HD_MODE_ENABLED` - Default image quality

---

## 🐛 Troubleshooting

### Bot doesn't start
- Check `DISCORD_TOKEN` is set correctly
- Check `GEMINI_API_KEY` is valid
- Enable MESSAGE CONTENT INTENT in Discord Developer Portal

### Image generation fails
- Ensure Gemini API key has Imagen access
- Check for rate limits (429 errors)
- Use Flash mode if hitting safety filters

### Choices seem stale/out of date
- Choices are generated from current image + dispatch only
- If desync occurs, restart game

See [AGENT_GUIDE.md](AGENT_GUIDE.md) for detailed troubleshooting.

---

## 📊 Features in Detail

### Image Generation Pipeline

- **Text-to-Image** for intro shot
- **Image-to-Image** for all subsequent turns (visual continuity)
- **Dynamic reference images** (1 for action, 2 for stationary)
- **Safety filter bypass** via prompt sanitization
- **Anti-border/anti-person instructions** for consistent POV

### Consequence System

- **Fair but tense** - Normal movement is safe, risky choices punished
- **Deadly consequences** for negative outcomes or hesitation
- **Structured death determination** via JSON response
- **Medical terminology** to bypass AI safety filters

### Death Replay System

- Tracks all high-res images during run
- Creates GIF on death or restart
- 500ms per frame (2x speed)
- Saved to `tapes/` with timestamp

---

## 🚀 Deployment

Deploy to **Render** (recommended):

1. Connect GitHub repo to Render
2. Set environment variables: `DISCORD_TOKEN`, `GEMINI_API_KEY`
3. Deploy as **Worker** (not Web Service)
4. `render.yaml` already configured

See [DEPLOYMENT.md](DEPLOYMENT.md) for full instructions.

---

## 🤝 Contributing

This is a personal project, but feedback and suggestions are welcome!

**Want to modify the game?**
1. Read [AGENT_GUIDE.md](AGENT_GUIDE.md) first
2. Most changes are in `prompts/simulation_prompts.json`
3. Test thoroughly by actually playing

---

## 📜 License

MIT License - Feel free to fork and modify for personal use.

---

## 🎬 Example Gameplay

```
[Image: Desert facility exterior, chain-link fence, dust storm approaching]

📍 DISPATCH:
Outside near the fence. The dust storm is getting closer. You can see a maintenance 
shed 20 meters away, but the wind is picking up. Your throat is dry.

CHOICES:
1. 🏃 Sprint to the maintenance shed before the storm hits
2. 🔍 Search along the fence line for another entrance
3. 🛡️ Take cover behind a concrete barrier and wait it out
4. 📞 Try the emergency radio on your belt

[15 second countdown begins...]
```

---

## 🔗 Links

- **Discord Developer Portal**: https://discord.com/developers/applications
- **Google AI Studio**: https://aistudio.google.com/app/apikey
- **Render Deployment**: https://render.com

---

## 🙏 Acknowledgments

- **Google Gemini** for powerful multimodal AI
- **Discord.py** for excellent bot framework
- **Pillow** for image processing
- Built with curiosity and playfulness 🎮

---

**Ready to survive? Deploy the bot and start your story. 📼🔥**

