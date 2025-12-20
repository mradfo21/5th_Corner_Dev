# 🚀 Render Deployment Guide - Admin Dashboard Access

## Overview

This guide shows you how to deploy the game API + admin dashboard to Render and access it from your main website.

---

## 📋 Prerequisites

- ✅ Render account
- ✅ Your main website running on a different server
- ✅ GitHub repo with this codebase

---

## 🎯 Deployment Steps

### Step 1: Create Web Service on Render

1. **Go to Render Dashboard** → Click "New +" → "Web Service"

2. **Connect Repository**
   - Connect your GitHub repo
   - Select the repository with this code

3. **Configure Service**
   ```
   Name: somewhere-game-api
   Environment: Python 3
   Build Command: pip install -r requirements.txt
   Start Command: python api.py
   ```

4. **Set Environment Variables**

   Click "Environment" and add:

   | Key | Value | Description |
   |-----|-------|-------------|
   | `ADMIN_TOKEN` | (generate secure token*) | Dashboard access token |
   | `ALLOWED_ORIGIN` | `https://yoursite.com` | Your main website URL |
   | `GEMINI_API_KEY` | (your key) | Gemini API key |
   | `OPENAI_API_KEY` | (your key) | OpenAI API key |
   | `DISCORD_TOKEN` | (your token) | Discord bot token |
   | `PORT` | `5001` | API server port |

   ***Generate secure token:**
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

5. **Deploy!**
   - Click "Create Web Service"
   - Render will build and deploy automatically
   - Wait for "Live" status

6. **Note Your URL**
   - Render will give you a URL like: `https://somewhere-game-api.onrender.com`
   - Save this - you'll need it!

---

## 🌐 Accessing from Your Main Website

### Method 1: Direct Link (Simplest)

Add a link on your main site:

```html
<a href="https://somewhere-game-api.onrender.com/admin?token=YOUR_TOKEN" target="_blank">
    Admin Dashboard
</a>
```

### Method 2: iframe Embedding (Recommended)

Create a page on your main site:

```html
<!DOCTYPE html>
<html>
<head>
    <title>Admin Dashboard</title>
    <style>
        body, html { margin: 0; padding: 0; height: 100%; overflow: hidden; }
        iframe { border: none; width: 100%; height: 100vh; }
    </style>
</head>
<body>
    <iframe src="https://somewhere-game-api.onrender.com/admin?token=YOUR_TOKEN"></iframe>
</body>
</html>
```

### Method 3: Flask Integration

If your main site uses Flask:

```python
from flask import Flask, render_template_string

app = Flask(__name__)

DASHBOARD_URL = "https://somewhere-game-api.onrender.com/admin"
ADMIN_TOKEN = "your-token-here"  # Load from env in production

@app.route('/admin')
def admin():
    return render_template_string('''
        <iframe src="{{ url }}?token={{ token }}" 
                style="width:100%;height:100vh;border:none;">
        </iframe>
    ''', url=DASHBOARD_URL, token=ADMIN_TOKEN)
```

---

## 🔒 Security Best Practices

### 1. **Protect Your Token**

**DON'T:**
- ❌ Commit token to public GitHub repo
- ❌ Expose in client-side JavaScript
- ❌ Use weak/simple tokens

**DO:**
- ✅ Store in environment variables
- ✅ Use long, random tokens (32+ characters)
- ✅ Rotate regularly

### 2. **Set Correct CORS Origin**

In production, set `ALLOWED_ORIGIN` to your specific domain:

```bash
ALLOWED_ORIGIN=https://yoursite.com
```

**NOT** `*` (wildcard) in production!

### 3. **HTTPS Only**

Render provides HTTPS automatically. Never access the dashboard over HTTP.

### 4. **Token Rotation**

To rotate your admin token:

1. Generate new token:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

2. Update on Render:
   - Go to service → Environment
   - Change `ADMIN_TOKEN` value
   - Save (auto-redeploys)

3. Update on your main site:
   - Update token in iframe/link
   - Deploy changes

---

## 🧪 Testing Your Deployment

### 1. **Health Check**

Visit in browser:
```
https://somewhere-game-api.onrender.com/api/health
```

Should return:
```json
{
  "status": "ok",
  "service": "SOMEWHERE Game Engine API",
  "version": "1.0.0"
}
```

### 2. **Dashboard Access**

Visit:
```
https://somewhere-game-api.onrender.com/admin?token=YOUR_TOKEN
```

Should load the admin dashboard.

### 3. **Test from Main Site**

Embed the iframe on your main site and verify:
- ✅ Dashboard loads correctly
- ✅ Sessions are visible
- ✅ Can view session details
- ✅ History inspection works
- ✅ No CORS errors in browser console

---

## 🐛 Troubleshooting

### Problem: "Unauthorized" Error

**Cause:** Invalid or missing token

**Fix:**
1. Check `ADMIN_TOKEN` is set on Render
2. Verify token in URL matches exactly
3. Check for typos/spaces

### Problem: Dashboard Won't Load in iframe

**Cause:** CORS or CSP headers

**Fix:**
1. Check `ALLOWED_ORIGIN` on Render matches your site
2. Open browser console and check for errors
3. Try direct link first (not iframe) to isolate issue

### Problem: "Dashboard file not found"

**Cause:** `admin_dashboard.html` not in deployment

**Fix:**
1. Verify file exists in repo root
2. Check Render build logs
3. Ensure no `.gitignore` rules excluding it

### Problem: Slow Loading

**Cause:** Render cold start (free tier)

**Solution:**
- Render free tier sleeps after 15 min inactivity
- First request wakes it (30-60 sec delay)
- Upgrade to paid tier for always-on

---

## 📊 Example File Structure

Your repo should have:

```
project_root/
├── api.py                          # Main API (deploys to Render)
├── admin_dashboard.html            # Dashboard UI
├── engine.py                       # Game engine
├── bot.py                          # Discord bot (optional)
├── requirements.txt                # Python dependencies
├── .env.example                    # Environment template
├── examples/
│   ├── embed_dashboard.html        # iframe example
│   └── flask_main_site_example.py  # Flask integration example
└── README.md
```

---

## 🔄 Continuous Deployment

Render auto-deploys on every push to your main branch.

**Workflow:**
1. Make changes locally
2. Test with `python api.py`
3. Commit and push to GitHub
4. Render auto-deploys (2-3 minutes)
5. Changes are live!

---

## 💰 Cost Considerations

### Free Tier

- ✅ Free
- ⚠️ Sleeps after 15 min inactivity
- ⚠️ 750 hours/month limit

**Good for:** Development, testing, low-traffic admin dashboard

### Paid Tier ($7/month)

- ✅ Always-on (no sleep)
- ✅ Faster
- ✅ Custom domains
- ✅ More resources

**Good for:** Production use with regular admin access

---

## 🎯 Quick Reference

### Your Dashboard URL
```
https://YOUR-SERVICE-NAME.onrender.com/admin?token=YOUR_TOKEN
```

### Update Environment Variable
1. Render Dashboard → Your Service
2. Environment tab
3. Edit variable
4. Save (auto-redeploys)

### View Logs
1. Render Dashboard → Your Service
2. Logs tab
3. Real-time logs visible

### Redeploy Manually
1. Render Dashboard → Your Service
2. Manual Deploy → Deploy latest commit

---

## 📞 Support & Resources

- **Render Docs:** https://render.com/docs
- **Flask CORS:** https://flask-cors.readthedocs.io/
- **This Project:** See `CROSS_ORIGIN_DASHBOARD_ACCESS.md` for detailed CORS setup

---

## ✅ Deployment Checklist

Before going live:

- [ ] Environment variables set on Render
- [ ] `ALLOWED_ORIGIN` set to your domain (not `*`)
- [ ] Strong admin token generated and set
- [ ] `admin_dashboard.html` in repo root
- [ ] Service is "Live" on Render
- [ ] Health check returns OK
- [ ] Dashboard loads with token
- [ ] Tested iframe embedding on main site
- [ ] No CORS errors in browser console
- [ ] Token stored securely (not in public code)

---

## 🎉 You're Done!

Your admin dashboard is now:
- ✅ Deployed on Render (always-on background process)
- ✅ Accessible from any webpage
- ✅ Secure (token-protected)
- ✅ Cross-origin ready (CORS configured)

**Access it from anywhere:**
```
https://your-api.onrender.com/admin?token=YOUR_TOKEN
```

Happy monitoring! 🚀

