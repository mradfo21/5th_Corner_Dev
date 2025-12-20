# 🔌 Integration Guide - Admin Dashboard on Main Website

**Purpose:** This document provides complete instructions for integrating the game admin dashboard into your main website.

**Target Audience:** Developers or AI assistants working on the main website (separate from the game API).

---

## 📋 Overview

### **What You're Integrating**

A real-time admin dashboard that monitors and manages game sessions.

**Features:**
- View all active game sessions
- Monitor player status (alive/dead, turn count, location)
- Inspect complete game history with AI prompts
- View/download VHS tape GIFs
- Create and delete sessions
- Auto-refresh every 5 seconds

### **Architecture**

```
┌─────────────────────────────────────────┐
│   YOUR MAIN WEBSITE                     │  ← You are here
│   (Different server/domain)             │
│                                         │
│   Goal: Embed the dashboard             │
│         using an iframe                 │
└─────────────────────────────────────────┘
                    │
                    │ HTTPS
                    ↓
┌─────────────────────────────────────────┐
│   GAME API SERVER (Render)              │  ← Already deployed
│   https://your-api.onrender.com         │
│                                         │
│   Endpoint: /admin                      │
│   Returns: Admin dashboard HTML         │
│   Auth: NONE (open access)              │
└─────────────────────────────────────────┘
```

---

## 🚀 Quick Integration (3 Options)

### **Option 1: Simple Page with iframe** ⭐ RECOMMENDED

Create a new page on your main site (e.g., `/admin` or `/game-dashboard`):

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Game Admin Dashboard</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body, html {
            height: 100%;
            overflow: hidden;
            background: #1a1a2e;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        }

        /* Optional: Add a header bar */
        .header {
            background: rgba(30, 30, 60, 0.9);
            padding: 15px 20px;
            color: white;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 2px solid #4ecdc4;
        }

        .header h1 {
            margin: 0;
            font-size: 1.5em;
            color: #ff6b6b;
        }

        .header a {
            color: #4ecdc4;
            text-decoration: none;
            padding: 8px 16px;
            border: 1px solid #4ecdc4;
            border-radius: 5px;
            transition: background 0.3s;
        }

        .header a:hover {
            background: rgba(78, 205, 196, 0.2);
        }

        /* Dashboard iframe */
        iframe {
            border: none;
            width: 100%;
            height: calc(100vh - 60px); /* Adjust if you have header */
            display: block;
        }

        /* If no header, use full height */
        .fullscreen iframe {
            height: 100vh;
        }

        /* Loading state */
        .loading {
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            color: #4ecdc4;
            font-size: 1.5em;
            text-align: center;
        }

        .loading.hidden {
            display: none;
        }
    </style>
</head>
<body>
    <!-- OPTIONAL: Header bar with navigation -->
    <div class="header">
        <h1>🎮 Game Administration</h1>
        <a href="/">← Back to Main Site</a>
    </div>

    <!-- Loading indicator -->
    <div class="loading" id="loading">
        Loading dashboard...
    </div>

    <!-- Dashboard iframe -->
    <iframe 
        id="dashboard"
        src="https://your-api.onrender.com/admin"
        onload="document.getElementById('loading').classList.add('hidden')"
        title="Game Admin Dashboard">
    </iframe>

    <script>
        // Optional: Hide loading after timeout if iframe doesn't load
        setTimeout(() => {
            const loading = document.getElementById('loading');
            if (!loading.classList.contains('hidden')) {
                loading.innerHTML = '⚠️ Dashboard failed to load<br><small>Check your connection or API status</small>';
                loading.style.color = '#ff6b6b';
            }
        }, 10000); // 10 second timeout
    </script>
</body>
</html>
```

**Replace:**
- `https://your-api.onrender.com/admin` → Your actual Render URL

---

### **Option 2: Flask Integration**

If your main site uses Flask:

```python
from flask import Flask, render_template, render_template_string

app = Flask(__name__)

# Configuration
GAME_API_URL = "https://your-api.onrender.com"  # CHANGE THIS

@app.route('/admin')
@app.route('/game-dashboard')
def game_admin_dashboard():
    """
    Admin dashboard page - embeds game monitoring interface
    """
    return render_template_string('''
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Game Admin Dashboard</title>
            <style>
                * { margin: 0; padding: 0; box-sizing: border-box; }
                body, html { height: 100%; overflow: hidden; background: #1a1a2e; }
                
                .header {
                    background: rgba(30, 30, 60, 0.9);
                    padding: 15px 20px;
                    color: white;
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    border-bottom: 2px solid #4ecdc4;
                }
                
                .header h1 {
                    margin: 0;
                    font-size: 1.5em;
                    color: #ff6b6b;
                }
                
                .header a {
                    color: #4ecdc4;
                    text-decoration: none;
                    padding: 8px 16px;
                    border: 1px solid #4ecdc4;
                    border-radius: 5px;
                }
                
                .header a:hover {
                    background: rgba(78, 205, 196, 0.2);
                }
                
                iframe {
                    border: none;
                    width: 100%;
                    height: calc(100vh - 60px);
                    display: block;
                }
                
                .loading {
                    position: absolute;
                    top: 50%;
                    left: 50%;
                    transform: translate(-50%, -50%);
                    color: #4ecdc4;
                    font-size: 1.5em;
                }
            </style>
        </head>
        <body>
            <div class="header">
                <h1>🎮 Game Administration</h1>
                <a href="{{ url_for('index') }}">← Back to Main Site</a>
            </div>
            
            <div class="loading" id="loading">Loading dashboard...</div>
            
            <iframe 
                src="{{ dashboard_url }}/admin"
                onload="document.getElementById('loading').style.display='none'">
            </iframe>
        </body>
        </html>
    ''', dashboard_url=GAME_API_URL)

# Optional: Add link to dashboard in your main nav
@app.route('/')
def index():
    return render_template_string('''
        <!DOCTYPE html>
        <html>
        <head><title>Main Site</title></head>
        <body>
            <nav>
                <a href="/">Home</a>
                <a href="/admin">Admin Dashboard</a>
            </nav>
            <h1>Welcome to the main site!</h1>
        </body>
        </html>
    ''')

if __name__ == '__main__':
    app.run(debug=True, port=8000)
```

---

### **Option 3: WordPress / PHP / Other CMS**

Create a new page template or use a page builder:

```html
<!-- Add this HTML to your page -->
<div style="width: 100%; height: 80vh; border: none;">
    <iframe 
        src="https://your-api.onrender.com/admin" 
        style="width: 100%; height: 100%; border: none;"
        title="Game Admin Dashboard">
    </iframe>
</div>
```

**Or use an iframe plugin:**
- WordPress: "Iframe" or "Advanced iFrame" plugin
- Input URL: `https://your-api.onrender.com/admin`

---

## 🔧 Configuration

### **Required Information**

You need this from the game API team:

| Setting | Value | Example |
|---------|-------|---------|
| **API URL** | Your Render deployment URL | `https://somewhere-game-api.onrender.com` |
| **Dashboard Endpoint** | Always `/admin` | `/admin` |
| **Authentication** | Currently NONE | No token required |
| **CORS** | Enabled for all origins | Works from any domain |

### **Full Dashboard URL**

```
https://your-api.onrender.com/admin
```

**Replace `your-api.onrender.com` with the actual Render URL.**

---

## 📊 Dashboard Features

Once integrated, users can access these features:

### **Session Management**
- View all active game sessions
- Create new sessions
- Delete old sessions
- Quick status view (turn count, player alive/dead)

### **Real-Time Monitoring**
- Auto-refresh every 5 seconds
- Live player status updates
- Current location and time tracking

### **History Inspection**
- Full turn-by-turn history
- All AI prompts (narrative, choices, images)
- Player actions and choices
- Generated images with links
- Raw JSON for debugging

### **VHS Tapes**
- View death replay GIFs
- Download tapes
- List all available tapes per session

---

## ✅ Testing Checklist

After integration, test these:

### **1. Basic Access**
- [ ] Dashboard page loads without errors
- [ ] iframe displays content (not blank)
- [ ] No CORS errors in browser console
- [ ] Styling looks correct (not broken layout)

### **2. Dashboard Functionality**
- [ ] Can see list of sessions
- [ ] Can click on a session to view details
- [ ] Auto-refresh works (watch timestamp update)
- [ ] Can view session history
- [ ] Images load (if any exist)

### **3. Browser Compatibility**
- [ ] Works in Chrome/Edge
- [ ] Works in Firefox
- [ ] Works in Safari
- [ ] Works on mobile browsers

### **4. Navigation**
- [ ] Can navigate back to main site
- [ ] Page URL is bookmarkable
- [ ] Browser back button works correctly

---

## 🐛 Troubleshooting

### **Problem: iframe is blank or shows error**

**Causes:**
1. Wrong API URL
2. API server is down
3. Network/firewall blocking

**Solutions:**
1. Verify API URL is correct
2. Test API health: Visit `https://your-api.onrender.com/api/health` (should return `{"status":"ok"}`)
3. Check browser console for errors
4. Try accessing dashboard directly: `https://your-api.onrender.com/admin`

---

### **Problem: CORS errors in console**

**Example error:**
```
Access to XMLHttpRequest blocked by CORS policy
```

**Solution:**
This shouldn't happen as CORS is enabled for all origins. If it does:
1. Check that the API server is running the latest code
2. Verify in browser console what the exact error is
3. Contact game API team to verify CORS settings

---

### **Problem: Dashboard loads but shows "no sessions"**

**Cause:** No game sessions have been created yet, or API is not connected to data.

**Solutions:**
1. This is normal if no games have been played
2. Test by creating a session using the "New Session" button
3. Or play a game via Discord bot to generate sessions

---

### **Problem: Slow loading or timeout**

**Cause:** Render free tier "cold start" (API was sleeping)

**Solution:**
1. First load may take 30-60 seconds (free tier limitation)
2. Subsequent loads are instant
3. Upgrade to paid Render tier for always-on service

---

### **Problem: 404 Not Found**

**Cause:** Wrong URL path

**Solution:**
- Endpoint is `/admin` (not `/dashboard` or `/admin-dashboard`)
- Correct: `https://your-api.onrender.com/admin`
- Wrong: `https://your-api.onrender.com/dashboard`

---

## 🔒 Security Considerations

### **Current Setup (Development)**

- ⚠️ **No authentication** - anyone with URL can access
- ⚠️ **Open CORS** - embeddable from any domain
- ⚠️ **Public data** - all game sessions visible

**Appropriate for:**
- ✅ Development/testing
- ✅ Internal tools (private network)
- ✅ Low-sensitivity data

**NOT appropriate for:**
- ❌ Public production with sensitive data
- ❌ Multi-tenant systems with privacy requirements

### **Adding Authentication (Later)**

If you need to secure the dashboard:

1. Contact game API team to enable token authentication
2. They will provide a secure token
3. Update your iframe URL to include token:
   ```html
   <iframe src="https://your-api.onrender.com/admin?token=SECRET_TOKEN"></iframe>
   ```

**Note:** Authentication is optional and not currently implemented for simplicity.

---

## 📱 Responsive Design

The dashboard is fully responsive. For best mobile experience:

```html
<meta name="viewport" content="width=device-width, initial-scale=1.0">

<style>
    /* Make iframe responsive */
    iframe {
        width: 100%;
        height: 100vh;
        border: none;
    }
    
    /* On mobile, hide your site's navigation to maximize space */
    @media (max-width: 768px) {
        .main-nav {
            display: none;
        }
    }
</style>
```

---

## 🎨 Customization Options

### **Option A: Keep Default Theme**

The dashboard has a VHS horror aesthetic (dark theme, teal accents). No changes needed.

### **Option B: Add Your Branding**

Add a header bar above the iframe:

```html
<div class="custom-header">
    <img src="/your-logo.png" alt="Logo">
    <h1>Your Site Name - Game Admin</h1>
</div>

<iframe src="https://your-api.onrender.com/admin"></iframe>

<style>
    .custom-header {
        background: your-brand-color;
        padding: 20px;
        display: flex;
        align-items: center;
        gap: 20px;
    }
</style>
```

### **Option C: Full-Screen Mode**

Remove all chrome for maximum dashboard space:

```html
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Game Admin</title>
    <style>
        * { margin: 0; padding: 0; }
        body, html { height: 100%; overflow: hidden; }
        iframe { width: 100%; height: 100%; border: none; }
    </style>
</head>
<body>
    <iframe src="https://your-api.onrender.com/admin"></iframe>
</body>
</html>
```

---

## 📖 API Endpoints (Reference)

If you want to build custom UI instead of using iframe:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/sessions` | GET | List all sessions |
| `/api/sessions/{id}` | GET | Get session details + history |
| `/api/sessions/{id}/history` | GET | Full history with prompts |
| `/api/sessions/{id}/tapes` | GET | List VHS tapes |
| `/api/sessions` | POST | Create new session |
| `/api/sessions/{id}` | DELETE | Delete session |
| `/api/health` | GET | Health check |

**Base URL:** `https://your-api.onrender.com`

**Example:**
```javascript
fetch('https://your-api.onrender.com/api/sessions')
  .then(res => res.json())
  .then(data => console.log(data.data)); // Array of sessions
```

---

## 📋 Implementation Checklist

- [ ] Get API URL from game API team
- [ ] Choose integration method (iframe, Flask, etc.)
- [ ] Create admin page on your site
- [ ] Copy code from this document
- [ ] Replace `your-api.onrender.com` with actual URL
- [ ] Test dashboard loads correctly
- [ ] Test all features work (sessions, history, etc.)
- [ ] Check mobile responsiveness
- [ ] Add navigation link to admin page
- [ ] Test in multiple browsers
- [ ] Deploy to production

---

## 🚀 Quick Start Summary

**Fastest way to get it working:**

1. **Get the URL** from game API team (format: `https://something.onrender.com`)

2. **Create a new page** on your site (`/admin` or `/game-dashboard`)

3. **Add this code:**
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
       <iframe src="https://YOUR-API-URL.onrender.com/admin"></iframe>
   </body>
   </html>
   ```

4. **Replace `YOUR-API-URL`** with the actual URL

5. **Test it!** Visit your admin page

**Done!** ✨

---

## 📞 Support

### **Common Questions**

**Q: Do I need to install anything?**  
A: No! It's just an iframe. Works with plain HTML.

**Q: Does it work with my CMS (WordPress, Wix, etc.)?**  
A: Yes! Any CMS that allows custom HTML can embed iframes.

**Q: Can I customize the look?**  
A: The dashboard itself can't be styled (it's in an iframe), but you can add headers/footers around it.

**Q: Is there an API I can use instead?**  
A: Yes! See "API Endpoints (Reference)" section above.

**Q: What if the API URL changes?**  
A: Just update the iframe `src` URL and redeploy.

---

## 📂 Example Files

**Minimal Working Example** (`admin.html`):
```html
<!DOCTYPE html>
<html>
<head><title>Game Admin</title></head>
<body style="margin:0;padding:0;height:100vh;">
    <iframe 
        src="https://your-api.onrender.com/admin" 
        style="width:100%;height:100%;border:none;">
    </iframe>
</body>
</html>
```

**With Loading State** (`admin-with-loading.html`):
```html
<!DOCTYPE html>
<html>
<head>
    <title>Game Admin</title>
    <style>
        body { margin: 0; padding: 0; height: 100vh; background: #1a1a2e; }
        iframe { width: 100%; height: 100%; border: none; }
        .loading {
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            color: #4ecdc4;
            font-size: 24px;
            font-family: sans-serif;
        }
    </style>
</head>
<body>
    <div class="loading" id="loading">Loading dashboard...</div>
    <iframe 
        src="https://your-api.onrender.com/admin"
        onload="document.getElementById('loading').style.display='none'">
    </iframe>
</body>
</html>
```

---

## ✅ Final Checklist

Before considering integration complete:

- [ ] Dashboard loads without errors
- [ ] Can view sessions list
- [ ] Can open session details
- [ ] History viewer works
- [ ] No CORS errors in console
- [ ] Mobile responsive
- [ ] Navigation works (back to main site)
- [ ] Bookmarkable URL
- [ ] Tested in Chrome/Firefox
- [ ] Deployed to production

---

## 🎉 That's It!

**The simplest integration is just 4 lines:**

```html
<iframe 
    src="https://your-api.onrender.com/admin"
    style="width:100%;height:100vh;border:none;">
</iframe>
```

**Replace the URL and you're done!** 🚀

---

## 📞 Contact

If you need help or have questions:
- Check "Troubleshooting" section above
- Contact the game API team for API URL or server issues
- Test API health endpoint: `https://your-api.onrender.com/api/health`

**Last Updated:** 2025-12-20

