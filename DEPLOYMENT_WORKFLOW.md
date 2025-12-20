# 🚀 Deployment Workflow - Auto-Deploy to Render

## ✅ **How It Works Now**

Every time you push to GitHub's `main` branch, Render automatically:
1. Detects the push (within 10 seconds)
2. Starts building (2-3 minutes)
3. Deploys new version (1 minute)
4. Service goes live with changes (total: 3-5 minutes)

**You don't need to do anything on Render!** Just push to GitHub.

---

## 📋 **Standard Workflow**

### **Option 1: Using the Script** (Easiest)

```powershell
# Windows PowerShell
.\push.ps1 "Your commit message here"

# Linux/Mac
./push.sh "Your commit message here"
```

**This script:**
- ✅ Ensures you're on `main` branch
- ✅ Shows what changed
- ✅ Commits with your message
- ✅ Pushes to GitHub
- ✅ Triggers Render auto-deploy

---

### **Option 2: Manual Commands**

```bash
# 1. Make sure you're on main
git checkout main

# 2. Add your changes
git add .

# 3. Commit
git commit -m "Your commit message"

# 4. Push (triggers auto-deploy)
git push origin main
```

---

## ⏱️ **Timeline After Push**

```
Push to GitHub
    ↓ (10 seconds)
Render detects push
    ↓ (2-3 minutes)
Build completes
    ↓ (1 minute)
Deploy completes
    ↓
Service LIVE ✅
```

**Total:** 3-5 minutes from push to live

---

## 🔍 **Monitoring Deployment**

### **Check Deployment Status:**

1. **Go to:** https://dashboard.render.com
2. **Click:** `5th_Corner_Dev` service
3. **See:** Build/Deploy status in real-time

### **Watch the Logs:**

In Render dashboard:
- **Logs tab** → See real-time build/deploy logs
- Look for:
  ```
  ==> Build successful 🎉
  ==> Deploying...
  ==> Your service is live
  ```

---

## 🧪 **Testing After Deploy**

Once Render shows "Live":

**Test 1: API Health**
```
https://fiveth-corner-dev-1a00.onrender.com/api/health
```
Should return: `{"status":"ok"}`

**Test 2: Dashboard**
```
https://fiveth-corner-dev-1a00.onrender.com/admin
```
Should show: Admin dashboard

**Test 3: Discord Bot**
- Try a Discord command
- Bot should respond

---

## ⚠️ **What If Deploy Fails?**

### **Check Build Logs:**

1. **Render Dashboard** → Service → **Logs**
2. **Look for errors** in red
3. **Common issues:**
   - Missing file in repo
   - Syntax error in code
   - Missing dependency in requirements.txt

### **Rollback:**

If new deploy breaks things:
1. **Render Dashboard** → Service
2. **Deploys** tab (left sidebar)
3. **Find last working deploy** → Click "Redeploy"

---

## 🔄 **Branch Strategy**

### **Now: Single Branch (main)**

```
main (only branch)
  ↓
  Push here → Auto-deploys to Render
```

**No more confusion!** ✅

### **Before: Two Branches (confused)**

```
main → Render uses this
master → You were working here
       → Changes not deployed!
```

**Problem solved:** Deleted `master` branch.

---

## 📊 **Auto-Deploy Settings**

### **Verify Auto-Deploy is Enabled:**

1. **Render Dashboard** → `5th_Corner_Dev`
2. **Settings** → **Build & Deploy**
3. **Auto-Deploy:** Should say `Yes`

If it says `No`:
- Click **Edit**
- Enable **Auto-Deploy: Yes**
- Branch: `main`
- Save

---

## 🎯 **Quick Reference**

| Action | Command | Result |
|--------|---------|--------|
| **Push changes** | `.\push.ps1 "message"` | Auto-deploys to Render |
| **Check status** | Visit Render dashboard | See build progress |
| **View logs** | Logs tab in Render | Real-time deployment logs |
| **Test health** | Visit `/api/health` | Verify API is up |
| **Rollback** | Deploys tab → Redeploy old | Revert to previous version |

---

## ✅ **Best Practices**

### **Before Pushing:**

1. ✅ Test locally (run `python api.py` or `python start.py`)
2. ✅ Make sure files are saved
3. ✅ Check you're on `main` branch

### **Commit Messages:**

Use clear, descriptive messages:
- ✅ "Add session management feature"
- ✅ "Fix Unicode errors in start.py"
- ✅ "Update admin dashboard styling"
- ❌ "update"
- ❌ "fix stuff"

### **After Pushing:**

1. ✅ Watch Render logs for errors
2. ✅ Test the endpoints after deploy
3. ✅ Check Discord bot still works

---

## 🚨 **Emergency: Need to Stop Auto-Deploy?**

If you need to push code but DON'T want it deployed:

1. **Render Dashboard** → Settings
2. **Auto-Deploy:** Change to `No`
3. Push your code
4. Re-enable when ready

---

## 📝 **Deployment Checklist**

Before each deployment:

- [ ] Code tested locally
- [ ] On `main` branch (`git branch` to check)
- [ ] All files saved
- [ ] Commit message is clear
- [ ] Requirements.txt updated (if added packages)
- [ ] Environment variables set on Render (if needed)

After deployment:
- [ ] Render shows "Live"
- [ ] No errors in logs
- [ ] API health check passes
- [ ] Dashboard loads
- [ ] Discord bot responds

---

## 🎉 **Summary**

**What changed:**
- ✅ Deleted `master` branch (no more confusion)
- ✅ Only `main` branch exists
- ✅ Auto-deploy enabled on Render
- ✅ Push to `main` → Automatic deployment

**New workflow:**
```bash
# Make changes
# Test locally
.\push.ps1 "Your message"
# Wait 3-5 minutes
# Check Render dashboard
# Done! ✅
```

**No more manual deploys! No more branch confusion!** 🎉

