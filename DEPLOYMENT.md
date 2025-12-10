# DEPLOYMENT GUIDE - SOMEWHERE STORYGEN

## 🚀 Deploying to Cloud / Web Server

---

## PREREQUISITES

### 1. **Discord Bot Setup**
1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Create new application → Bot → Copy bot token
3. Enable these intents:
   - ✅ MESSAGE CONTENT INTENT
   - ✅ SERVER MEMBERS INTENT
   - ✅ PRESENCE INTENT (optional)
4. Bot Permissions: `378880` (Send Messages, Embed Links, Attach Files, Read Message History)
5. Invite bot to your server: `https://discord.com/api/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=378880&scope=bot`

### 2. **Google Gemini API Setup**
1. Go to [Google AI Studio](https://aistudio.google.com/app/apikey)
2. Create API key for Gemini
3. Enable these models:
   - ✅ `gemini-2.0-flash-exp` (text generation)
   - ✅ `imagen-3.0-generate-001` (image generation - text-to-image)
   - ✅ `imagen-3.0-capability-001` (image generation - img2img)

### 3. **Python Environment**
- Python 3.11 or higher
- Dependencies in `requirements.txt`

---

## 📦 DEPLOYMENT OPTIONS

### Option A: **Render.com** (Recommended - Free Tier, Good Logs)

```bash
# 1. Create render.yaml (already exists)
# 2. Connect GitHub repo to Render
# 3. Set environment variables in Render dashboard:
#    - DISCORD_TOKEN
#    - GEMINI_API_KEY
# 4. Deploy automatically on push
```

**render.yaml:**
```yaml
services:
  - type: worker
    name: somewhere-storygen
    env: python
    buildCommand: "pip install -r requirements.txt"
    startCommand: "python bot.py"
    envVars:
      - key: DISCORD_TOKEN
        sync: false
      - key: GEMINI_API_KEY
        sync: false
```

---

### Option B: **Railway.app** (Easy, Modern)

```bash
# 1. Install Railway CLI
npm install -g @railway/cli

# 2. Login and create project
railway login
railway init

# 3. Set environment variables
railway variables set DISCORD_TOKEN=your_discord_token
railway variables set GEMINI_API_KEY=your_gemini_api_key

# 4. Deploy
railway up

# 5. Monitor
railway logs
```

**railway.json:**
```json
{
  "build": {
    "builder": "NIXPACKS"
  },
  "deploy": {
    "startCommand": "python bot.py",
    "healthcheckPath": null,
    "healthcheckTimeout": 100
  }
}
```

---

### Option C: **AWS EC2** (Full Control)

```bash
# 1. Launch EC2 instance (t2.micro or t3.small recommended)
# 2. SSH into instance
ssh -i your-key.pem ubuntu@your-instance-ip

# 3. Install Python and dependencies
sudo apt update
sudo apt install python3.11 python3-pip git -y

# 4. Clone repository
git clone https://github.com/yourusername/somewhere-storygen.git
cd somewhere-storygen

# 5. Install dependencies
pip3 install -r requirements.txt

# 6. Set environment variables
export DISCORD_TOKEN="your_discord_token"
export GEMINI_API_KEY="your_gemini_api_key"

# 7. Run with systemd (persistent)
sudo nano /etc/systemd/system/storygen.service
```

**storygen.service:**
```ini
[Unit]
Description=SOMEWHERE StoryGen Discord Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/somewhere-storygen
Environment="DISCORD_TOKEN=your_discord_token"
Environment="GEMINI_API_KEY=your_gemini_api_key"
ExecStart=/usr/bin/python3 bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
# Enable and start service
sudo systemctl enable storygen
sudo systemctl start storygen
sudo systemctl status storygen

# View logs
sudo journalctl -u storygen -f
```

---

### Option D: **Docker** (Containerized)

**Dockerfile:**
```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create runtime directories
RUN mkdir -p images tapes logs

# Run bot
CMD ["python", "bot.py"]
```

**docker-compose.yml:**
```yaml
version: '3.8'

services:
  storygen:
    build: .
    container_name: somewhere-storygen
    restart: unless-stopped
    environment:
      - DISCORD_TOKEN=${DISCORD_TOKEN}
      - GEMINI_API_KEY=${GEMINI_API_KEY}
    volumes:
      - ./images:/app/images
      - ./tapes:/app/tapes
      - ./logs:/app/logs
      - ./world_state.json:/app/world_state.json
      - ./history.json:/app/history.json
```

**Deploy:**
```bash
# 1. Create .env file
echo "DISCORD_TOKEN=your_token" > .env
echo "GEMINI_API_KEY=your_key" >> .env

# 2. Build and run
docker-compose up -d

# 3. View logs
docker-compose logs -f
```

---

## 🔒 ENVIRONMENT VARIABLES

**Required:**
```bash
DISCORD_TOKEN=your_discord_bot_token_here
GEMINI_API_KEY=your_gemini_api_key_here
```

**Optional (for advanced configs):**
```bash
# If you want to override config.json values
GEMINI_MODEL_TEXT=gemini-2.0-flash-exp
GEMINI_MODEL_IMAGE_HD=imagen-3.0-generate-001
GEMINI_MODEL_IMAGE_FAST=imagen-3.0-generate-001
```

---

## 📁 FILE PERSISTENCE

### These files/folders are generated at runtime:
- `images/` - Generated scene images (can be cleared on restart)
- `tapes/` - Death replay GIFs (should persist)
- `logs/` - Error logs (optional persistence)
- `world_state.json` - Current game state (clears on restart)
- `history.json` - Narrative history (clears on restart)

### For cloud deployments:
- Use **persistent volumes** for `tapes/` folder (to keep user recordings)
- `images/`, `logs/`, `world_state.json`, `history.json` can be ephemeral

---

## 🐛 TROUBLESHOOTING

### Bot doesn't start
**Check:**
- ✅ `DISCORD_TOKEN` is set correctly
- ✅ `GEMINI_API_KEY` is set correctly
- ✅ Bot has MESSAGE CONTENT INTENT enabled in Discord Developer Portal
- ✅ Python version is 3.11+ (`python --version`)
- ✅ Dependencies installed (`pip install -r requirements.txt`)

### Bot connects but doesn't respond
**Check:**
- ✅ Bot is in your Discord server
- ✅ Bot has Send Messages, Embed Links, Attach Files permissions
- ✅ You're using correct Discord channel
- ✅ Check logs for errors

### Image generation fails
**Check:**
- ✅ Gemini API key has Imagen access enabled
- ✅ Not hitting rate limits (check logs for 429 errors)
- ✅ Prompts aren't too long (max 5000 chars)
- ✅ Safety filters aren't blocking (check for "That's not an image I can process")

### High API costs
**Solutions:**
- Use Flash mode (toggle HD button OFF)
- Reduce auto-play frequency (edit `AUTO_PLAY_DELAY` in `bot.py`)
- Limit concurrent games (one Discord channel at a time)

---

## 📊 MONITORING

### Key Metrics to Watch:
- **API calls per minute** (Gemini rate limits)
- **Image generation latency** (Flash: 10-15s, Pro: 30-60s)
- **Memory usage** (images are cached in RAM)
- **Disk usage** (tapes folder can grow large)

### Logging:
```python
# Check logs/error.log for exceptions
# Check logs/world_evolution.log for state updates
# Check terminal output for real-time events
```

### Recommended Monitoring Tools:
- **Uptime:** UptimeRobot, Better Uptime
- **Logs:** Papertrail, Loggly, CloudWatch
- **Alerts:** Discord webhook for error notifications

---

## 💰 COST ESTIMATES

### Gemini API (Pay-as-you-go):
- **Text generation (Flash):** ~$0.075 per 1M input tokens
- **Image generation (Flash):** ~$0.04 per image
- **Image generation (Pro):** ~$0.08 per image

**Per game session (30 turns, Flash mode):**
- Text: ~$0.05
- Images: ~$1.20
- **Total: ~$1.25 per 30-turn game**

**Monthly estimate (10 games/day, Flash mode):**
- ~$375/month

### Hosting:
- **Render:** Free tier (750 hours/month), $7/month for starter plan
- **Railway:** $5/month for 500 hours
- **AWS EC2:** $3-10/month (t2.micro/t3.small)
- **Docker (self-hosted):** Free (your own hardware)

---

## 🔐 SECURITY BEST PRACTICES

1. **Never commit tokens to git**
   - Use `.env` files (gitignored)
   - Use platform environment variables

2. **Rotate keys regularly**
   - Discord token every 6 months
   - Gemini API key every 6 months

3. **Limit bot permissions**
   - Only grant necessary Discord permissions
   - Use role-based access control

4. **Rate limit API calls**
   - Already implemented in `gemini_image_utils.py`
   - Monitor for abuse

5. **Sanitize user input**
   - Custom actions are already sanitized
   - Don't allow arbitrary code execution

---

## 🚦 HEALTH CHECKS

### Discord Bot Health:
```python
# Bot is healthy if:
# 1. Connected to Discord (check terminal: "Logged in as SOMEWHERE#...")
# 2. Responding to button clicks
# 3. No error spam in logs
```

### API Health:
```python
# Gemini is healthy if:
# 1. Text generation completes in <5 seconds
# 2. Image generation completes in <60 seconds (Pro) or <20 seconds (Flash)
# 3. No 429 (rate limit) or 503 (service unavailable) errors
```

---

## 📞 SUPPORT & MAINTENANCE

### Regular Maintenance:
- **Weekly:** Check disk usage (clear old tapes if needed)
- **Monthly:** Review API costs, optimize if needed
- **Quarterly:** Update dependencies (`pip install --upgrade -r requirements.txt`)

### Emergency Contacts:
- **Discord API Status:** https://discordstatus.com/
- **Google Cloud Status:** https://status.cloud.google.com/
- **Gemini API Issues:** https://issuetracker.google.com/

---

## 🎯 DEPLOYMENT CHECKLIST

Before going live:
- ✅ Discord bot created and token obtained
- ✅ Gemini API key obtained and models enabled
- ✅ Environment variables set
- ✅ Bot invited to Discord server
- ✅ Dependencies installed
- ✅ Test run completed locally
- ✅ Logs configured
- ✅ Persistent storage for tapes folder (optional)
- ✅ Monitoring/alerts set up (optional)
- ✅ Cost tracking enabled (optional)

---

**You're ready to deploy! Good luck! 🚀**

