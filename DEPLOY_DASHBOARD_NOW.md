# 🚀 Deploy Admin Dashboard - Step by Step

## 🎯 Goal

Make your admin dashboard accessible at a URL so you can embed it in RASTER.

---

## 🏗️ Current Situation

You have:
- ✅ `bot.py` - Discord bot (running as Background Worker)
- ✅ `api.py` - Web server with `/admin` endpoint ⭐ **NEEDS TO BE DEPLOYED**
- ✅ `admin_dashboard.html` - Dashboard UI

**Problem:** Background Workers don't have public URLs!

**Solution:** Deploy `api.py` as a Web Service (has public URL)

---

## 🎯 Simplest Solution: Combined Service

Run BOTH bot and API in the same service.

### **Step 1: Create Start Script**

Create file: `start.sh` in your project root:

```bash
#!/bin/bash
# Start both Discord bot and API server

echo "Starting Discord bot..."
python bot.py &

echo "Starting API server..."
python api.py

# Keep script running
wait
```

Make it executable:
```bash
chmod +x start.sh
```

---

### **Step 2: Update Render Configuration**

#### **Option A: Modify Existing Service**

1. **Go to Render Dashboard**
2. **Click on `5th_Corner_Dev`**
3. **Settings tab**
4. **Change:**
   - Service Type: **Web Service** (not Background Worker)
   - Start Command: `bash start.sh`
   - Port: Keep as is (Render auto-assigns)

5. **Environment Variables** - Add if not already there:
   ```
   DISCORD_TOKEN=<your-token>
   GEMINI_API_KEY=<your-key>
   OPENAI_API_KEY=<your-key>
   PORT=5001
   ```

6. **Save Changes**
7. **Deploy**

#### **Option B: Create New Service**

1. **Render Dashboard** → New + → Web Service
2. **Connect:** Your `5th_Corner_Dev` repo
3. **Settings:**
   - Name: `5th-corner-game-api`
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `bash start.sh`
4. **Environment Variables:** (same as above)
5. **Create Service**

---

### **Step 3: Get Your URL**

After deployment completes:

1. **Go to service dashboard**
2. **Copy the URL** (looks like: `https://5th-corner-dev-xyz.onrender.com`)
3. **Test it:**
   - Visit: `https://your-url.onrender.com/api/health`
   - Should return: `{"status":"ok"}`
   - Visit: `https://your-url.onrender.com/admin`
   - Should show: Admin dashboard!

---

### **Step 4: Embed in RASTER**

On your RASTER website (`https://fiveth-corner-operations.onrender.com`):

**Option A: Simple HTML page**

Create `admin.html`:

```html
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Game Admin</title>
    <style>
        body, html { margin: 0; padding: 0; height: 100%; }
        iframe { width: 100%; height: 100%; border: none; }
    </style>
</head>
<body>
    <iframe src="https://YOUR-GAME-API-URL.onrender.com/admin"></iframe>
</body>
</html>
```

**Option B: Flask route** (if RASTER uses Flask)

Add to your Flask app:

```python
@app.route('/admin')
def admin_dashboard():
    GAME_API_URL = "https://YOUR-GAME-API-URL.onrender.com"
    return f'<iframe src="{GAME_API_URL}/admin" style="width:100%;height:100vh;border:none;"></iframe>'
```

---

## ✅ Verification

After deployment:

1. **Discord bot works?**
   - Test by using Discord commands
   - Should still connect and respond

2. **API accessible?**
   - Visit: `https://your-url.onrender.com/api/health`
   - Returns: `{"status":"ok"}`

3. **Dashboard loads?**
   - Visit: `https://your-url.onrender.com/admin`
   - Shows admin dashboard with sessions

4. **Embedded in RASTER?**
   - Visit: `https://fiveth-corner-operations.onrender.com/admin`
   - Dashboard appears in iframe

---

## 🐛 Troubleshooting

### **Bot doesn't connect to Discord**

Check logs:
- Render Dashboard → Service → Logs
- Look for Discord connection errors
- Verify `DISCORD_TOKEN` is set correctly

### **API returns 404**

- Verify start command is: `bash start.sh`
- Check logs for errors
- Verify `api.py` exists in repo

### **Dashboard shows "file not found"**

- Verify `admin_dashboard.html` is in project root
- Check logs for file path errors
- Try: `ls -la` in Render shell to see files

### **RASTER can't embed dashboard**

- Verify Game API URL is correct
- Check browser console for CORS errors (there shouldn't be any)
- Try accessing dashboard directly first

---

## 📊 Final Architecture

```
User visits RASTER
    ↓
https://fiveth-corner-operations.onrender.com/admin
    ↓
<iframe> embeds dashboard from:
    ↓
https://5th-corner-dev.onrender.com/admin
    ↓
Served by: api.py (Flask)
    ↓
Reads game data from: sessions/
    ↓
Written by: bot.py (Discord bot)
```

---

## 🎯 Quick Checklist

- [ ] Created `start.sh` script
- [ ] Set start command to `bash start.sh`
- [ ] Changed to Web Service (not Background Worker)
- [ ] Set environment variables
- [ ] Deployed service
- [ ] Got public URL
- [ ] Tested `/api/health`
- [ ] Tested `/admin`
- [ ] Embedded in RASTER
- [ ] Verified everything works

---

## ⚡ Alternative: Python Start Script

If `start.sh` doesn't work, use Python:

Create `start.py`:

```python
import subprocess
import sys
import signal

def main():
    print("Starting Discord bot...")
    bot = subprocess.Popen([sys.executable, 'bot.py'])
    
    print("Starting API server...")
    try:
        subprocess.run([sys.executable, 'api.py'])
    except KeyboardInterrupt:
        print("Shutting down...")
        bot.terminate()
        bot.wait()

if __name__ == '__main__':
    main()
```

**Start command:** `python start.py`

---

## 🎉 Success!

Once deployed, you'll have:

- ✅ Discord bot running (bot.py)
- ✅ API server running (api.py)
- ✅ Dashboard accessible at: `https://your-url.onrender.com/admin`
- ✅ Embeddable in RASTER via iframe

**Then just update RASTER with the iframe code and you're done!** 🚀

