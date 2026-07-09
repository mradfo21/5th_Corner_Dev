# SOMEWHERE: Flask Website Brief
**An Analog Horror Story - Web Presence with Growth Potential**

---

## Project Vision

Build a **Flask-powered website** that:
1. **Phase 1 (MVP)**: Simple landing page (static-style) with VHS aesthetic
2. **Phase 2**: Live stats and gallery from the Discord bot
3. **Phase 3**: Full web UI for playing the game and viewing tapes

**Current Goal**: Phase 1 with architecture ready for Phase 2/3

---

## About the Game

### What is SOMEWHERE?

**SOMEWHERE** is an AI-powered analog horror narrative experience delivered through Discord. Players take on the role of Jason Fleece, an investigative journalist exploring the mysterious Horizon Industries facility in the New Mexico desert in 1993.

**Core Technology:**
- Every image generated in real-time by Google Gemini AI
- Every playthrough is unique, driven by player choices
- Authentic VHS camcorder aesthetic with analog artifacts
- Story branches based on decisions, with real consequences (injury/death)
- Entire playthrough compiled into downloadable VHS-style video

### Key Features
- 🎥 **AI-Generated VHS Footage** - Real-time image generation with 1993 camcorder aesthetic
- 🎮 **Choice-Based Narrative** - Branching story with 3 choices per turn + custom actions
- 📼 **Downloadable Playthrough** - Complete VHS tape of your story
- 💀 **Real Consequences** - Fate system, injuries, and permadeath
- ⏱️ **Time Pressure** - Countdown timer adds urgency, hesitation has consequences
- 🤖 **Powered by Gemini AI** - Text generation, image generation, and world simulation

### Setting & Tone
- **Time Period**: 1993 (pre-internet, pre-smartphone)
- **Location**: New Mexico desert, Four Corners region
- **Genre**: Found footage analog horror, government conspiracy
- **Atmosphere**: Tense, mysterious, dangerous, grounded in 1990s realism
- **Threats**: Guards, creatures, drones, paranormal phenomena, environmental hazards

---

## Phase 1: Landing Page (MVP)

### Goal
Create a simple, impactful landing page that:
- Captures the VHS analog horror aesthetic
- Explains the game in 2-3 sentences
- Directs visitors to Discord
- Showcases 2-4 example images from gameplay

### Design Reference
Think **Midjourney's landing page**:
- Minimal, bold, clean
- Dark theme with striking accents
- One primary CTA (Call To Action)
- Fast loading, mobile-first

### Page Structure

**Single Page Layout:**

1. **Hero Section**
   - Large centered title: `SOMEWHERE`
   - Subtitle: `An Analog Horror Story`
   - Short tagline: `1993. Desert. Secrets.`
   - Primary CTA: Large Discord invite button
   - Subtle VHS scan lines or static effect

2. **About Section**
   - 2-3 sentence description
   - Key features (3-4 bullet points with icons)
   - Format: "AI-Generated" | "Your Choices Matter" | "Free to Play"

3. **Gallery Section**
   - 2-4 example images from actual gameplay
   - VHS-style presentation (CRT TV frame or tape artifacts)
   - Brief captions for context

4. **CTA Section**
   - Discord invite button (prominent)
   - Optional: "Free to play. No download required."

5. **Footer**
   - Copyright
   - Optional: Tech info, credits

### Visual Aesthetic

**VHS Horror Theme:**
- **Colors**:
  - Background: `#0A0A0A` (near black)
  - Primary Text: `#E0E0E0` (off-white)
  - Accent 1: `#FF0033` (VHS red)
  - Accent 2: `#00FF41` (phosphor green)
  
- **Typography**:
  - Headings: Monospace (Courier New, Roboto Mono, JetBrains Mono)
  - Body: Clean sans-serif (Inter, System UI, Segoe UI)
  - Optional retro: VT323 (use sparingly)

- **Effects** (subtle):
  - CSS scan lines overlay
  - Slight CRT curve (optional)
  - VHS noise/grain on hero section
  - Glitch effect on hover (buttons)
  - Red/green chromatic aberration (minimal)

**Reference Aesthetic:**
- 1993 VHS tapes and camcorder footage
- Early 1990s desktop computing
- CRT monitors and TV static
- Clean modern web design (Midjourney, Vercel, Stripe)

**Avoid:**
- Overdone glitch effects (tacky)
- Unreadable text
- Slow animations
- Heavy graphics that slow loading

---

## Flask Architecture (Phase 1)

### File Structure

```
website/
├── app.py                      # Main Flask application
├── requirements.txt            # Python dependencies
├── .env.example               # Environment variables template
├── config.py                   # Configuration management
├── static/
│   ├── css/
│   │   ├── style.css          # Main stylesheet (VHS aesthetic)
│   │   └── effects.css        # Optional: scan lines, glitch effects
│   ├── js/
│   │   └── main.js            # Minimal JS (smooth scroll, effects)
│   ├── images/
│   │   ├── logo.png           # SOMEWHERE logo
│   │   ├── favicon.ico        # Browser tab icon
│   │   └── examples/          # Gameplay screenshot examples
│   │       ├── example1.png
│   │       ├── example2.png
│   │       ├── example3.png
│   │       └── example4.png
│   └── fonts/                 # Optional: custom fonts
└── templates/
    ├── base.html              # Base template (header, footer, meta)
    ├── index.html             # Landing page (extends base)
    └── components/            # Reusable components (future)
        ├── hero.html
        ├── gallery.html
        └── footer.html
```

### app.py (Phase 1 - Simple)

```python
"""
SOMEWHERE - Flask Website
Phase 1: Simple landing page with room to grow
"""
from flask import Flask, render_template, jsonify
from pathlib import Path
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-key-change-in-production')

# Discord invite link (set as environment variable)
DISCORD_INVITE = os.environ.get('DISCORD_INVITE', 'https://discord.gg/YOUR_INVITE_CODE')

# Bot integration (for future phases)
BOT_ROOT = Path(__file__).parent.parent
STATE_FILE = BOT_ROOT / "state.json"
CACHE_DIR = BOT_ROOT / "cache"


@app.route('/')
def home():
    """Main landing page"""
    return render_template('index.html', discord_invite=DISCORD_INVITE)


@app.route('/health')
def health():
    """Health check for deployment monitoring"""
    return jsonify({'status': 'ok', 'phase': 1})


@app.errorhandler(404)
def not_found(e):
    """Custom 404 page (VHS themed)"""
    return render_template('index.html', discord_invite=DISCORD_INVITE), 404


if __name__ == '__main__':
    # Development server
    app.run(debug=True, host='0.0.0.0', port=5000)
```

### requirements.txt

```
Flask==3.0.0
gunicorn==21.2.0
python-dotenv==1.0.0
```

### .env.example

```bash
# Discord Configuration
DISCORD_INVITE=https://discord.gg/YOUR_INVITE_CODE

# Flask Configuration
SECRET_KEY=your-secret-key-here
FLASK_ENV=production

# Optional: Bot Integration (Phase 2+)
BOT_STATE_FILE=/path/to/bot/state.json
BOT_CACHE_DIR=/path/to/bot/cache
```

---

## Phase 2: Live Stats & Gallery (Future)

### New Features
- **Live Stats**: Real-time data from the Discord bot
  - Total frames generated
  - Deaths recorded
  - Tapes created
  - Current active sessions
  
- **Gallery**: Recent gameplay images from bot's cache
  - Last 10-20 generated images
  - Filterable by date/death events
  - Click to enlarge with VHS effect

### New Routes

```python
@app.route('/gallery')
def gallery():
    """Gallery of recent gameplay images"""
    images = get_recent_images()
    return render_template('gallery.html', images=images)


@app.route('/api/stats')
def api_stats():
    """API endpoint for live stats"""
    stats = get_bot_stats()
    return jsonify(stats)


def get_bot_stats():
    """Read stats from bot's state file"""
    import json
    stats = {
        'total_frames': 0,
        'status': 'offline',
        'last_updated': None
    }
    
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                state = json.load(f)
                stats['status'] = 'online'
                stats['total_frames'] = state.get('frame_idx', 0)
                stats['last_updated'] = state.get('last_updated')
    except Exception as e:
        print(f"Error reading bot state: {e}")
    
    return stats


def get_recent_images():
    """Get recent images from bot's cache"""
    try:
        if CACHE_DIR.exists():
            images = sorted(
                CACHE_DIR.glob("*.png"),
                key=lambda p: p.stat().st_mtime,
                reverse=True
            )
            return [{'name': img.name, 'path': f'/static/cache/{img.name}'} 
                    for img in images[:20]]
    except Exception as e:
        print(f"Error reading cache: {e}")
    
    return []
```

### AJAX Updates (client-side)

```javascript
// Update stats in real-time without page reload
async function updateStats() {
    try {
        const response = await fetch('/api/stats');
        const data = await response.json();
        
        document.getElementById('frame-count').textContent = data.total_frames;
        document.getElementById('status').textContent = data.status;
    } catch (error) {
        console.error('Failed to fetch stats:', error);
    }
}

// Update every 30 seconds
setInterval(updateStats, 30000);
```

---

## Phase 3: Full Web UI (Future Vision)

### Ultimate Goal
Turn the website into a **full web interface** for the game:

**Features:**
- 🎮 **Play from Browser**: Web-based game interface (alternative to Discord)
- 📼 **Tape Library**: View, download, and share VHS tapes
- 👤 **User Accounts**: Save progress, track stats
- 🏆 **Leaderboards**: Longest survivors, most creative deaths
- 📊 **Stats Dashboard**: Personal and global game statistics
- 🎨 **Gallery**: Browse all generated images, vote on favorites
- 💬 **Community**: Comments, sharing, player stories

### Architecture Changes Needed

**Backend:**
- User authentication (Flask-Login or JWT)
- Database (PostgreSQL or SQLite)
- Session management
- WebSocket for real-time game updates (Flask-SocketIO)
- API for game state synchronization

**Frontend:**
- Modern JS framework (React, Vue, or HTMX for simplicity)
- Real-time updates
- Image viewer with VHS effects
- Video player for tapes
- Responsive game interface

**Integration:**
- Shared database between bot and website
- Bot can trigger webhooks for website updates
- Website can send commands to bot
- Unified state management

### Database Schema (Conceptual)

```sql
-- Users (if allowing accounts)
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(255) UNIQUE,
    discord_id VARCHAR(255) UNIQUE,
    created_at TIMESTAMP
);

-- Game Sessions
CREATE TABLE sessions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    frames_generated INTEGER,
    death_cause TEXT,
    tape_url TEXT
);

-- Generated Images
CREATE TABLE images (
    id SERIAL PRIMARY KEY,
    session_id INTEGER REFERENCES sessions(id),
    frame_idx INTEGER,
    image_url TEXT,
    prompt TEXT,
    generated_at TIMESTAMP
);

-- Stats
CREATE TABLE stats (
    id SERIAL PRIMARY KEY,
    metric VARCHAR(100),
    value INTEGER,
    updated_at TIMESTAMP
);
```

---

## Deployment

### Option 1: Render.com (RECOMMENDED - Same as Bot)

**Pros:**
- ✅ Same host as Discord bot
- ✅ Easy to share files/data between services
- ✅ Free tier available (limited)
- ✅ Auto-deploy from GitHub
- ✅ Built-in SSL

**Setup:**

1. Create `render.yaml`:

```yaml
services:
  - type: web
    name: somewhere-website
    env: python
    buildCommand: "pip install -r requirements.txt"
    startCommand: "gunicorn app:app"
    envVars:
      - key: PYTHON_VERSION
        value: 3.11.0
      - key: DISCORD_INVITE
        value: YOUR_INVITE_LINK
      - key: SECRET_KEY
        sync: false  # Set in Render dashboard
```

2. Push to GitHub
3. Connect Render to your repo
4. Deploy automatically

**Cost:** ~$7/month for basic plan (free tier available but limited)

### Option 2: Heroku

Similar to Render, add `Procfile`:

```
web: gunicorn app:app
```

### Option 3: PythonAnywhere

Good for simple Flask apps, easy setup, free tier available.

---

## Content Guidelines

### Copy Examples

**Tagline Options:**
- "A 1993 analog horror experience. AI-generated. Discord-based."
- "Every frame is unique. Every choice matters. Every death is permanent."
- "Investigate. Survive. Record. Your story on VHS."

**Description (2-3 sentences):**

> "SOMEWHERE is an interactive horror narrative powered by AI. Play as Jason Fleece, a journalist investigating a classified facility in the 1993 New Mexico desert. Every frame is generated in real-time as you explore, make choices, and face the consequences."

**Feature Bullets:**
- 🎥 AI-Generated VHS Footage - Every frame unique
- 🎮 Choice-Based Narrative - Your decisions shape the story
- 📼 Downloadable Playthrough - Keep your VHS tape forever
- 💀 Real Consequences - Injuries, deaths, and permadeath
- 🎲 Fate System - Luck can save or doom you
- ⏱️ Time Pressure - Hesitation has grim consequences

**Call to Action:**
- "Join Discord to Play"
- "Start Your Investigation"
- "Enter SOMEWHERE"

---

## Technical Requirements

### Performance
- **Page Load**: < 2 seconds
- **Mobile First**: Responsive design, touch-friendly
- **Browser Support**: Modern browsers (Chrome, Firefox, Safari, Edge)
- **Accessibility**: WCAG 2.1 AA compliance (good contrast, alt text, keyboard nav)

### SEO
- Meta tags (title, description, og:image)
- Semantic HTML
- Sitemap (when multi-page)
- robots.txt

### Security
- Environment variables for secrets
- HTTPS only (SSL/TLS)
- Content Security Policy
- Rate limiting (for Phase 2+ APIs)

---

## Development Phases Summary

| Phase | Features | Complexity | Timeline |
|-------|----------|------------|----------|
| **Phase 1** | Landing page, Discord link, static examples | Low | 1-2 days |
| **Phase 2** | Live stats, gallery from bot, API | Medium | 1 week |
| **Phase 3** | Full web UI, accounts, play from browser | High | 4-6 weeks |

**Current Goal:** Phase 1 with clean architecture for Phase 2

---

## Instructions for AI Agent

You are building a **Flask website for an AI-powered analog horror game**.

**Your Task:**
1. Create Phase 1 MVP: Simple landing page
2. Use Flask (not static HTML) - foundation for future growth
3. Follow the VHS analog horror aesthetic (dark, monospace, scan lines)
4. Mobile-first, fast loading, conversion-focused
5. Architecture ready for Phase 2 (stats/gallery) and Phase 3 (full web UI)

**Core Message:**
> "Play as a journalist investigating a 1993 facility. AI generates VHS footage in real-time based on your choices. Play free on Discord."

**Primary Goal:** Get visitors to click Discord invite button

**Design Philosophy:**
- **Simplicity**: Clean, minimal, bold (like Midjourney)
- **Atmosphere**: Dark, VHS aesthetic, horror vibes
- **Speed**: Fast loading, no bloat
- **Growth**: Architecture supports adding features later

**Key Differentiators:**
- Every frame AI-generated in real-time
- No two playthroughs identical
- Authentic 1993 VHS aesthetic
- Real consequences (permadeath)
- Downloadable VHS tape of your story

**What to Avoid:**
- Overdone glitch effects
- Slow animations
- Unreadable text
- Feature bloat (Phase 1 is simple!)
- Generic game website look

**Example Images Needed:**
- 4 gameplay screenshots (VHS style, different moments)
- Logo/title treatment
- Favicon

**Environment Variables to Set:**
- `DISCORD_INVITE` - Your Discord server invite link
- `SECRET_KEY` - Flask secret key

Start simple. Build for growth. Focus on conversion. The game is the star—your job is to intrigue people enough to join Discord and experience it.
