# 🏗️ Architecture Clarification - Where Does the Admin Dashboard Live?

## 🎯 The Confusion

You have:
- **RASTER** (Web Service) → `https://fiveth-corner-operations.onrender.com` → Landing page
- **5th_Corner_Dev** (Background Worker) → Discord bot (no web interface)

I've been writing docs assuming the admin dashboard is hosted somewhere, but **where is it actually?**

---

## 📦 What We Built in This Codebase

In your `5th_Corner_Dev` repository, we have:

### **1. bot.py** → Discord Bot
- Runs as background worker
- No HTTP interface
- Connects to Discord

### **2. api.py** → Flask Web Service ⭐ **THIS IS NEW**
- Flask web server
- HTTP endpoints for game data
- **NEW: `/admin` endpoint** that serves the dashboard
- Port: 5001

### **3. admin_dashboard.html** → Dashboard UI
- The actual admin interface
- Served by `api.py` at `/admin`
- Shows sessions, history, tapes

---

## 🎯 The Solution: You Need TWO Render Services

### **Current Setup (What You Have)**

```
┌─────────────────────────────────────┐
│ RASTER (Web Service)                │
│ Landing page                        │
│ https://fiveth-corner...            │
└─────────────────────────────────────┘

┌─────────────────────────────────────┐
│ 5th_Corner_Dev (Background Worker)  │
│ Discord bot (bot.py)                │
│ No web interface                    │
└─────────────────────────────────────┘
```

### **What You Need (Recommended Setup)**

```
┌─────────────────────────────────────┐
│ RASTER (Web Service)                │
│ Landing page                        │
│ https://fiveth-corner...            │
│                                     │
│ NEW: Embed dashboard via iframe     │
└─────────────────────────────────────┘
          │
          │ iframe embeds
          ↓
┌─────────────────────────────────────┐
│ Game API (Web Service) ⭐ NEW       │
│ api.py (Flask server)               │
│ https://game-api.onrender.com       │
│                                     │
│ Endpoints:                          │
│ • /admin → Dashboard                │
│ • /api/sessions → Session data      │
│ • /api/sessions/{id}/history        │
└─────────────────────────────────────┘
          │
          │ shares data with
          ↓
┌─────────────────────────────────────┐
│ 5th_Corner_Dev (Background Worker)  │
│ bot.py (Discord bot)                │
│ Saves game data to sessions/        │
└─────────────────────────────────────┘
```

---

## 🚀 Implementation: Create Second Render Service

### **Step 1: Create New Web Service on Render**

1. **Go to Render Dashboard** → Click "New +" → "Web Service"

2. **Connect Repository**
   - Select: `5th_Corner_Dev` (same repo as Discord bot)
   - Branch: `main`

3. **Configure Service**
   ```
   Name: somewhere-game-api
   Environment: Python 3
   Build Command: pip install -r requirements.txt
   Start Command: python api.py
   ```

4. **Set Environment Variables**
   ```bash
   GEMINI_API_KEY=<your-key>
   OPENAI_API_KEY=<your-key>
   PORT=5001
   FLASK_ENV=production
   ```

5. **Click "Create Web Service"**

---

### **Step 2: Configure Data Sharing**

**The key issue:** Both services need access to the same game data.

#### **Option A: Shared Persistent Disk** (Render paid feature)

Both services mount the same persistent disk:
- Discord bot writes game data → `/data/sessions/`
- API reads game data → `/data/sessions/`

#### **Option B: Shared Database** (Recommended for scaling)

Both services connect to a shared PostgreSQL/MongoDB database:
- Discord bot writes → Database
- API reads → Database

#### **Option C: File Sharing via S3/Cloud Storage**

Both services read/write to AWS S3 or similar:
- Discord bot writes → S3 bucket
- API reads → S3 bucket

#### **Option D: Same Service** (Simplest for now) ⭐

**Run both bot AND api in the same service!**

Change start command to:
```bash
python -c "import subprocess; subprocess.Popen(['python', 'bot.py']); subprocess.run(['python', 'api.py'])"
```

This runs both:
- `bot.py` (Discord bot) in background
- `api.py` (Web server) in foreground

Both access same local files!

---

## 📋 Recommended Approach

### **For Quick Testing: Combined Service**

**Modify your existing Background Worker:**

1. **Change Service Type**
   - Render Dashboard → 5th_Corner_Dev
   - Settings → Change to "Web Service"

2. **Update Start Command**
   ```bash
   python -c "import subprocess; import sys; bot = subprocess.Popen([sys.executable, 'bot.py']); api = subprocess.run([sys.executable, 'api.py'])"
   ```

3. **Set Environment Variables**
   ```bash
   PORT=5001
   DISCORD_TOKEN=<your-token>
   GEMINI_API_KEY=<your-key>
   OPENAI_API_KEY=<your-key>
   ```

4. **Deploy**

**Result:**
- Discord bot runs (connects to Discord)
- API server runs (serves dashboard)
- Both access same `sessions/` directory
- You get a URL: `https://5th-corner-dev.onrender.com`
- Dashboard at: `https://5th-corner-dev.onrender.com/admin`

---

## 🎯 Then: Embed in RASTER

Once you have the Game API service running:

1. **Get the URL**
   - Example: `https://5th-corner-dev.onrender.com`

2. **Add to RASTER website**
   - Create new page: `/admin` or `/dashboard`
   - Add iframe:
   ```html
   <iframe src="https://5th-corner-dev.onrender.com/admin"></iframe>
   ```

3. **Done!**

---

## 🔍 Current State Analysis

Looking at your setup, you have:

```python
# bot.py - Discord bot ✅
# Saves data to: sessions/{session_id}/state.json
# Saves data to: sessions/{session_id}/history.json

# api.py - Flask web server ✅
# Reads data from: sessions/{session_id}/state.json
# Reads data from: sessions/{session_id}/history.json
# Serves dashboard at: /admin

# admin_dashboard.html - Dashboard UI ✅
# Displays session data
# Shows history, tapes, etc.
```

**Everything is built!** Just needs to be deployed correctly.

---

## ✅ Action Plan

### **Option 1: Quick & Simple** (Recommended for testing)

**Convert Background Worker to Web Service (runs both bot + API):**

1. Render Dashboard → 5th_Corner_Dev
2. Settings → Change to Web Service
3. Start Command: 
   ```bash
   bash -c "python bot.py & python api.py"
   ```
4. Deploy
5. Get URL (e.g., `https://5th-corner-dev.onrender.com`)
6. Access dashboard: `https://5th-corner-dev.onrender.com/admin`
7. Embed in RASTER: `<iframe src="https://5th-corner-dev.onrender.com/admin"></iframe>`

---

### **Option 2: Separate Services** (Better for production)

**Keep Discord bot as Background Worker, add new Web Service:**

1. Keep current Background Worker (bot.py)
2. Create NEW Web Service for api.py
3. Set up shared data access (database or persistent disk)
4. Get API URL
5. Embed in RASTER

---

## 📊 Architecture Diagram

### **Option 1: Combined Service** ⭐

```
┌──────────────────────────────────────────────────┐
│ RASTER (Landing Page)                            │
│ https://fiveth-corner-operations.onrender.com    │
│                                                  │
│ Page: /admin                                     │
│ Content: <iframe src="...">                      │
└──────────────────────────────────────────────────┘
                    │
                    │ iframe embeds
                    ↓
┌──────────────────────────────────────────────────┐
│ 5th_Corner_Dev (Web Service)                     │
│ https://5th-corner-dev.onrender.com              │
│                                                  │
│ Process 1: bot.py → Discord bot                  │
│ Process 2: api.py → Web server                   │
│                                                  │
│ Shared directory: sessions/                      │
│ • Bot writes game data                           │
│ • API reads game data                            │
│                                                  │
│ Endpoint: /admin → Dashboard                     │
└──────────────────────────────────────────────────┘
```

---

## 🎯 Summary

**What You Asked:**
> "Where is the admin dashboard actually hosted?"

**Answer:**
It's in your `5th_Corner_Dev` codebase as `api.py` + `admin_dashboard.html`, but you need to deploy it as a **Web Service** (not Background Worker) so it has a public URL.

**Next Step:**
1. Convert `5th_Corner_Dev` to Web Service (or create second service)
2. Run both `bot.py` and `api.py`
3. Get the URL (e.g., `https://5th-corner-dev.onrender.com`)
4. Embed in RASTER: `<iframe src="https://5th-corner-dev.onrender.com/admin"></iframe>`

**All the code is already built!** Just needs correct deployment. 🚀

---

## 📞 Next Steps

**Tell me:**
1. Do you want to convert your existing Background Worker to Web Service (runs both)?
2. Or create a separate new Web Service for the API?

I'll give you exact step-by-step instructions for whichever you choose! 🎯

