# 🔧 Discord Bot Timeout Fix

## 🐛 The Problem You Experienced

When running Discord bot as a Web Service, Render health checks would timeout because:

1. **Bot blocks the main thread** → Port never binds
2. **Render expects HTTP response within 30 seconds** → Doesn't get it
3. **Health check fails** → Service marked as unhealthy
4. **Render kills the service** → Timeout error

---

## ✅ The Fix (Applied)

### **1. Start API Server FIRST**

Updated `start.py` to:
- ✅ Start `api.py` **first** (binds to port immediately)
- ✅ Wait for API to successfully bind
- ✅ **Then** start `bot.py` (can take its time)

**Result:** Render health checks pass immediately, bot connects after.

### **2. Use Render's PORT Variable**

Updated `api.py` to:
- ✅ Read `PORT` from environment (Render sets this)
- ✅ Bind to that port (not hardcoded 5001)

**Result:** API binds to correct port Render expects.

### **3. Keep Service Up Even If Bot Dies**

Updated `start.py` to:
- ✅ Monitor both processes
- ✅ If bot dies → log warning but **keep API running**
- ✅ If API dies → shut down (service needs API)

**Result:** Service stays healthy even if Discord connection drops.

---

## 📋 What Changed

### **start.py** (Startup Script)

**Before:**
```python
# Started bot first → bot blocks → timeout
bot = start_bot()
api = start_api()  # Never gets here
```

**After:**
```python
# Start API first → binds to port → health checks pass
api = start_api()  # Binds to port immediately
wait(3 seconds)     # Give it time to start
bot = start_bot()   # Can take its time
monitor_both()      # Keep both running
```

### **api.py** (Flask Server)

**Before:**
```python
app.run(host='0.0.0.0', port=5001)  # Hardcoded port
```

**After:**
```python
port = int(os.getenv('PORT', 5001))  # Use Render's port
app.run(host='0.0.0.0', port=port)
```

---

## 🚀 How It Works Now

```
Render starts service
    ↓
Runs: python start.py
    ↓
[1/2] Start API (api.py)
    → Binds to PORT immediately
    → Render health check: ✅ PASS
    ↓
[2/2] Start Bot (bot.py)
    → Connects to Discord
    → Takes 5-30 seconds
    → Service already healthy ✅
    ↓
Monitor both processes
    → Bot dies? Log warning, keep API
    → API dies? Shut down service
```

---

## ✅ Now When You Deploy

1. **Service starts** → API binds to port in ~3 seconds
2. **Health check passes** → Render marks service as healthy
3. **Bot connects** → Discord bot comes online
4. **Both run together** → Service stays up

**No more timeouts!** 🎉

---

## 🧪 How to Test Locally

```bash
python start.py
```

**You should see:**
```
==============================================================
Starting SOMEWHERE Game - Combined Service
==============================================================
[1/2] Starting API server (api.py) in background...
       API will bind to port 5001
       API server started (PID: 12345)
       Waiting for API to bind to port...
       ✅ API server is running
[2/2] Starting Discord bot (bot.py)...
       Discord bot started (PID: 12346)
==============================================================
✅ Both services started successfully!
   • API: http://0.0.0.0:5001
   • Dashboard: http://0.0.0.0:5001/admin
==============================================================
[MONITOR] Both services running. Monitoring...
```

**Then test:**
- Visit: `http://localhost:5001/admin` → Dashboard loads ✅
- Discord: Bot should be online and responding ✅

---

## 📊 Summary

**Problem:** Discord bot blocked port binding → timeout  
**Fix:** Start API first, bot second  
**Result:** Service stays healthy, both processes run  

**Now you can deploy as a Web Service without timeouts!** 🚀

