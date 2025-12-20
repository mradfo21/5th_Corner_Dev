# 🚀 Start Command Options for Render

## 📊 Comparison

| Method | Command | Production-Ready? | Use Case |
|--------|---------|-------------------|----------|
| **Development** | `python start.py` | ❌ No | Testing, debugging |
| **Production** | `bash start_production.sh` | ✅ Yes | Live deployment |

---

## 🔧 Option 1: Development Mode

### **Start Command:**
```bash
python start.py
```

### **What It Does:**
- Runs Flask's built-in development server
- Single-threaded (handles one request at a time)
- Auto-reloads on code changes (if debug=True)

### **Pros:**
- ✅ Simple
- ✅ Easy to debug
- ✅ Works immediately

### **Cons:**
- ⚠️ Not production-grade
- ⚠️ Slow under load
- ⚠️ Single-threaded

### **Best For:**
- Testing locally
- Development
- Very low traffic (<10 requests/min)

---

## ⭐ Option 2: Production Mode (Gunicorn)

### **Start Command:**
```bash
bash start_production.sh
```

### **What It Does:**
- Runs Gunicorn (production WSGI server)
- Multi-worker (handles multiple requests simultaneously)
- Proper timeout handling
- Better error logging

### **Configuration:**
```bash
gunicorn api:app \
    --bind 0.0.0.0:$PORT \
    --workers 2 \              # 2 concurrent request handlers
    --timeout 120 \            # 120 second timeout
    --access-logfile - \       # Log to stdout
    --error-logfile - \        # Log errors to stdout
    --log-level info           # Logging verbosity
```

### **Pros:**
- ✅ Production-grade
- ✅ Multi-threaded (2 workers = handle 2 requests at once)
- ✅ Proper timeout handling
- ✅ Better performance
- ✅ Graceful restarts

### **Cons:**
- ⚠️ Slightly more complex
- ⚠️ Requires `gunicorn` in requirements.txt (✅ already added)

### **Best For:**
- Production deployment
- Live traffic
- When multiple users access dashboard simultaneously

---

## 🎯 Recommendation

### **For Your Use Case:**

Since you're deploying to Render for production use:

**Use:** `bash start_production.sh`

**Why:**
- ✅ Admin dashboard may have multiple viewers
- ✅ Better stability
- ✅ Proper production setup
- ✅ Only slightly more complex

---

## 📋 Setup on Render

### **Using Production Mode:**

1. **Ensure these files are in repo:**
   - ✅ `start_production.sh` (just created)
   - ✅ `requirements.txt` (updated with gunicorn)
   - ✅ `api.py`
   - ✅ `bot.py`

2. **On Render:**
   ```
   Build Command: pip install -r requirements.txt
   Start Command: bash start_production.sh
   ```

3. **Environment Variables:**
   ```bash
   DISCORD_TOKEN=<your-token>
   GEMINI_API_KEY=<your-key>
   OPENAI_API_KEY=<your-key>
   FLASK_ENV=production
   ```

4. **Deploy!**

---

## 🔄 Workers Configuration

### **Current:** 2 workers

```bash
--workers 2
```

**This means:**
- 2 concurrent requests handled
- If 3rd request comes in while 2 are processing, it waits

### **Adjust based on usage:**

**Low traffic (<50 requests/min):**
```bash
--workers 2    # Good default
```

**Medium traffic (50-200 requests/min):**
```bash
--workers 4    # More concurrent handling
```

**High traffic (>200 requests/min):**
```bash
--workers 8    # Requires more RAM
```

**Rule of thumb:** `(2 x CPU cores) + 1`

For Render free tier, stick with 2-4 workers.

---

## ⚙️ Timeout Configuration

### **Current:** 120 seconds

```bash
--timeout 120
```

**This is important because:**
- AI image generation can take 30-60 seconds
- Video generation (Veo) can take 60-120 seconds
- Need longer timeout than default (30 seconds)

### **If you get timeout errors:**

Increase timeout:
```bash
--timeout 180   # 3 minutes
```

Or:
```bash
--timeout 300   # 5 minutes (for very slow operations)
```

---

## 🧪 Testing Locally

### **Test Production Mode Locally:**

```bash
export PORT=5001
bash start_production.sh
```

**You should see:**
```
==================================================
Starting SOMEWHERE Game - Production Mode
==================================================
[1/2] Starting Discord bot (bot.py)...
       Discord bot started (PID: 12345)
[2/2] Starting API server with Gunicorn...
       Binding to 0.0.0.0:5001
       Workers: 2
       Timeout: 120 seconds
==================================================
[2024-12-20 10:30:00] [INFO] Starting gunicorn 21.2.0
[2024-12-20 10:30:00] [INFO] Listening at: http://0.0.0.0:5001
[2024-12-20 10:30:00] [INFO] Using worker: sync
[2024-12-20 10:30:00] [INFO] Booting worker with pid: 12346
[2024-12-20 10:30:00] [INFO] Booting worker with pid: 12347
```

**Test dashboard:**
```
http://localhost:5001/admin
```

---

## 🆚 Performance Comparison

### **Flask Dev Server:**
```
Single request:  ~100ms
Concurrent (2):  ~200ms (waits for first to finish)
Concurrent (5):  ~500ms (all wait in queue)
```

### **Gunicorn (2 workers):**
```
Single request:  ~100ms
Concurrent (2):  ~100ms (handled simultaneously)
Concurrent (5):  ~250ms (2 at a time, queue for rest)
```

**Winner:** Gunicorn for any production use!

---

## 📝 Summary

### **Quick Answer:**

**Q:** Do I need gunicorn?  
**A:** For production on Render, **YES**.

**Q:** What start command?  
**A:** `bash start_production.sh`

**Q:** Already added to requirements.txt?  
**A:** ✅ Yes, just added `gunicorn`

---

## ✅ Final Render Configuration

```yaml
Build Command: pip install -r requirements.txt
Start Command: bash start_production.sh

Environment:
  DISCORD_TOKEN: <your-token>
  GEMINI_API_KEY: <your-key>
  OPENAI_API_KEY: <your-key>
  FLASK_ENV: production
```

**That's it!** Production-ready. 🚀

