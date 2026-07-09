# ⚡ Quick Start - Cross-Origin Dashboard Access

## 🎯 Goal
Access your admin dashboard from your main website (different server/domain).

---

## 📋 Setup (5 Minutes)

### **Step 1: Deploy to Render**

1. Create web service on Render
2. Connect this GitHub repo
3. Build command: `pip install -r requirements.txt`
4. Start command: `python api.py`

### **Step 2: Set Environment Variables**

On Render dashboard → Environment:

```bash
ADMIN_TOKEN=8K_Pxv3Q2m9RnLp4YzWj7XfThU6NcVbS  # Generate with command below
ALLOWED_ORIGIN=https://yoursite.com  # Your main website
```

**Generate token:**
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### **Step 3: Get Your Dashboard URL**

Render gives you:
```
https://your-service-name.onrender.com
```

Your dashboard:
```
https://your-service-name.onrender.com/admin?token=YOUR_TOKEN
```

---

## 🌐 Access from Main Website

### **Option A: Direct Link**

Add to your main site:

```html
<a href="https://your-api.onrender.com/admin?token=YOUR_TOKEN" target="_blank">
    🎮 Admin Dashboard
</a>
```

### **Option B: iframe Embed (Recommended)**

Create page on your main site:

```html
<!DOCTYPE html>
<html>
<head>
    <title>Admin Dashboard</title>
    <style>
        body, html { margin: 0; padding: 0; height: 100%; }
        iframe { border: none; width: 100%; height: 100vh; }
    </style>
</head>
<body>
    <iframe src="https://your-api.onrender.com/admin?token=YOUR_TOKEN"></iframe>
</body>
</html>
```

### **Option C: Flask Integration**

If your main site uses Flask:

```python
from flask import Flask, render_template_string

app = Flask(__name__)

@app.route('/admin')
def admin():
    return render_template_string('''
        <iframe src="https://your-api.onrender.com/admin?token=YOUR_TOKEN"
                style="width:100%;height:100vh;border:none;">
        </iframe>
    ''')
```

---

## ✅ Test It

1. **Health Check**
   ```
   Visit: https://your-api.onrender.com/api/health
   Should return: {"status": "ok"}
   ```

2. **Dashboard Direct**
   ```
   Visit: https://your-api.onrender.com/admin?token=YOUR_TOKEN
   Should load: Admin dashboard
   ```

3. **From Main Site**
   ```
   Visit: https://yoursite.com/admin
   Should load: Embedded dashboard
   ```

---

## 🔒 Security

### **Protect Your Token**

✅ **DO:**
- Store in environment variables
- Use long random tokens (32+ chars)
- Set specific `ALLOWED_ORIGIN` in production

❌ **DON'T:**
- Commit token to public repos
- Use simple/guessable tokens
- Set `ALLOWED_ORIGIN=*` in production

### **Rotate Token**

1. Generate new token
2. Update on Render (Environment tab)
3. Update on your main site
4. Old token stops working immediately

---

## 📊 What You Get

Once set up, from your main website you can:

- ✅ View all game sessions
- ✅ Monitor player status (alive/dead)
- ✅ Inspect full history & AI prompts
- ✅ View/download VHS tapes
- ✅ Delete old sessions
- ✅ Real-time auto-refresh

---

## 🐛 Troubleshooting

### **"Unauthorized" Error**
→ Check token is correct in URL
→ Verify `ADMIN_TOKEN` set on Render

### **Dashboard Won't Load**
→ Check API is running: `/api/health`
→ Verify Render service is "Live"
→ Check browser console for errors

### **CORS Error in Console**
→ Set `ALLOWED_ORIGIN` to your domain
→ Make sure it matches exactly (no trailing slash)

### **iframe Blocked**
→ Check `ALLOWED_ORIGIN` includes your domain
→ Verify HTTPS (Render auto-provides)

---

## 📖 Full Documentation

For complete details, see:
- `CROSS_ORIGIN_DASHBOARD_ACCESS.md` - Complete guide
- `RENDER_DEPLOYMENT_GUIDE.md` - Step-by-step deployment
- `CROSS_ORIGIN_SETUP_COMPLETE.md` - Implementation summary

---

## ⚡ TL;DR

```bash
# 1. Deploy to Render
# 2. Set environment variables:
ADMIN_TOKEN=your-secret-token
ALLOWED_ORIGIN=https://yoursite.com

# 3. Embed on main site:
<iframe src="https://your-api.onrender.com/admin?token=TOKEN"></iframe>

# Done! 🎉
```

---

**Questions?** Check the full documentation files or test locally with:
```bash
python api.py  # Start server
open test_embed_local.html  # See visual test
```

