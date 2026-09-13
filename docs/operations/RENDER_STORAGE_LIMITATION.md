# ⚠️ Render Storage — Persistent Disk Status

## 🔎 **UPDATE (2026-07-21): Disk declared in render.yaml — verify it actually attached**

`render.yaml` declares a 1GB persistent disk mounted at `sessions/` (see the
`disk:` block on the `somewhere-game` service). **Whether that alone is
enough depends on how the Render service was originally created:**

- If it was created via **Render's "New → Blueprint" flow** and is
  Blueprint-synced to this repo, Render should pick up the `disk:` block on
  the next deploy — though Render sometimes still shows a **"pending
  changes" banner in the dashboard that a human has to approve** before an
  infra change (like adding a disk) actually takes effect. Check for that
  banner after deploying this.
- If the service was created manually (**"New → Web Service"**, pointed at
  this repo) — which is the more common path and is very possibly what
  happened here — **Render does NOT read `disk:` out of a plain
  `render.yaml` for an existing, non-Blueprint service.** In that case the
  disk has to be added by hand:
  1. Render Dashboard → the `somewhere-game` service → **Settings → Disks**
  2. **Add Disk** → Name `game-data` → Mount Path
     `/opt/render/project/src/sessions` → Size `1 GB` → Save
  3. Render redeploys the service with the disk attached.

**Don't guess — check the admin dashboard.** The Cost Analytics tab now
shows a live storage-health banner (`GET /api/admin/analytics/storage_health`)
that tells you definitively:
- 🟢 green = confirmed — a ledger row has already survived a process
  restart, so persistence is provably working.
- 🟡 yellow = the mount looks present but hasn't been proven across a
  restart yet (check back after the next deploy).
- 🔴 red = **not mounted** — `sessions/` is still on ephemeral storage, and
  this doc's manual steps above are what's needed.

The rest of this document describes the PRIOR ephemeral-storage state, kept
for context on why this mattered.

---

## 🔥 **Previously: Ephemeral Storage**

Render Free Tier uses **ephemeral storage** - ALL files are **deleted** on:
- Every deploy/redeploy
- Service restarts
- Instance reboot

---

## 📁 **What Gets Deleted:**

```
sessions/
├── default/
│   ├── images/          ❌ WIPED on restart
│   ├── tapes/           ❌ WIPED on restart
│   ├── films/           ❌ WIPED on restart
│   ├── state.json       ❌ WIPED on restart
│   └── history.json     ❌ WIPED on restart
```

**This means:**
- ❌ Game state resets on every deploy
- ❌ Images from previous sessions are lost
- ❌ VHS tapes are lost
- ❌ Video segments are lost

---

## ✅ **Solutions:**

### **Option 1: Paid Plan with Persistent Disk** (Recommended)

**Cost:** $7/month (Starter plan) + $1/GB persistent disk

**Setup:**
1. Upgrade to Render Starter plan
2. Add persistent disk in Render dashboard
3. Mount to `/opt/render/project/src/sessions`
4. Files persist across deploys! ✅

**Pros:**
- Simple setup
- Fast access
- No code changes needed

**Cons:**
- Costs money (~$8-10/month)

---

### **Option 2: Cloud Storage (S3, Cloudinary, etc.)**

**Cost:** Free tier available (AWS S3 free tier: 5GB)

**Changes needed:**
1. Install `boto3` (AWS) or `cloudinary`
2. Modify `gemini_image_utils.py` to upload images
3. Modify `bot.py` to fetch images from URL
4. Store URLs in state instead of file paths

**Pros:**
- Free tier available
- Unlimited storage (pay as you grow)
- Images have public URLs

**Cons:**
- Requires code changes
- Slightly slower (network latency)
- More complex setup

---

### **Option 3: Accept Ephemeral Storage** (Current)

**Cost:** Free

**What happens:**
- Game resets on every deploy
- Players can play, but progress doesn't persist
- Good for testing/demos only

**Pros:**
- Free
- No changes needed

**Cons:**
- No persistence
- Not production-ready

---

## 🎯 **Current Status:**

**You are currently on Option 3 (Ephemeral Storage)**

**This means:**
- ✅ Game works for single sessions
- ✅ Images generate and display during play
- ❌ Everything resets on redeploy
- ❌ Players lose progress

---

## 📊 **Recommended Path Forward:**

### **For Testing/Development:**
- Option 3 (current) is fine ✅

### **For Production:**
- **Best:** Option 1 (Persistent Disk) - Simple, fast, reliable
- **Alternative:** Option 2 (Cloud Storage) - Free tier, scalable

---

## 🔧 **How to Add Persistent Disk (Option 1):**

1. **Upgrade to Starter Plan:**
   - Render Dashboard → Billing → Upgrade ($7/month)

2. **Add Persistent Disk:**
   - Service → Settings → Disks
   - Click "Add Disk"
   - Name: `game-sessions`
   - Mount Path: `/opt/render/project/src/sessions`
   - Size: 1 GB (enough for hundreds of sessions)
   - Click "Create Disk"

3. **Deploy:**
   - Service will restart with persistent disk
   - All files in `sessions/` now persist! ✅

4. **Verify:**
   - Play a game
   - Redeploy
   - Check if images still exist: `ls sessions/default/images/`
   - Should see files! ✅

---

## 🚨 **Important Notes:**

### **Disk is NOT Backed Up:**
- Even with persistent disk, **back up important data**!
- Disk failures can happen
- Use cloud storage for critical/long-term data

### **Disk is Per-Service:**
- Each Render service has its own disk
- If you delete the service, disk is deleted too
- Can't share disk between services

### **Disk Performance:**
- SSD-backed
- Fast reads/writes
- Good for image/video storage

---

## 📝 **Summary:**

| Option | Cost | Persistence | Setup Difficulty |
|--------|------|-------------|------------------|
| **Ephemeral (current)** | Free | ❌ No | ✅ None |
| **Persistent Disk** | ~$8/mo | ✅ Yes | ⚡ Easy |
| **Cloud Storage** | Free tier | ✅ Yes | 🔧 Medium |

**For production, upgrade to Persistent Disk.** It's worth $8/month for a stable game experience.

---

## 🎮 **Testing Without Persistence:**

Even without persistent storage, you can still:
- ✅ Test gameplay
- ✅ Test image generation
- ✅ Test choices and actions
- ✅ Test Discord bot
- ✅ Test admin dashboard

Just know that everything resets on redeploy.

---

## 🔍 **Check Current Storage:**

To see what's currently saved (before next restart):

```bash
# On Render shell
ls -lah sessions/default/images/
ls -lah sessions/default/tapes/
```

This shows what exists **right now**, but will be **gone** on next restart (unless you have persistent disk).

