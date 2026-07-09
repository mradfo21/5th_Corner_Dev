# 🌐 Cross-Origin Dashboard Access Guide

## Problem Statement

Your setup:
- **Admin Dashboard & API** → Render (background process) - e.g., `https://game-api.onrender.com`
- **Main Website** → Different server/Flask app - e.g., `https://yoursite.com`
- **Goal**: Access dashboard from main website

---

## ✅ **Solution 1: Direct Link (Simplest)**

### **How It Works**
Provide a link from your main site directly to the dashboard URL.

### **Pros**
- ✅ Zero setup
- ✅ No CORS issues
- ✅ Dashboard stays on its own domain

### **Cons**
- ❌ Opens in new tab (not embedded)
- ❌ Needs authentication if public

### **Implementation**

#### **On Your Main Site**
```html
<!-- Add to your main site -->
<a href="https://game-api.onrender.com/admin" target="_blank" class="btn">
    🎮 Admin Dashboard
</a>
```

#### **On api.py (Render)**
Add a route to serve the dashboard:

```python
@app.route('/admin')
def serve_admin_dashboard():
    """Serve the admin dashboard"""
    return send_file('admin_dashboard.html')
```

---

## ✅ **Solution 2: iframe Embedding (Recommended)**

### **How It Works**
Embed the dashboard in an iframe on your main site.

### **Pros**
- ✅ Seamless integration (looks like one site)
- ✅ Dashboard stays in its own security context
- ✅ Easy to implement

### **Cons**
- ❌ Need to handle CORS for cross-origin iframes
- ❌ Some styling adjustments may be needed

### **Implementation**

#### **Step 1: Update api.py (Render)**

Enable CORS for the dashboard route and set proper headers:

```python
from flask import Flask, send_file, make_response

@app.route('/admin')
def serve_admin_dashboard():
    """Serve the admin dashboard with CORS headers"""
    response = make_response(send_file('admin_dashboard.html'))
    
    # Allow embedding from your main site
    response.headers['Access-Control-Allow-Origin'] = 'https://yoursite.com'  # Or '*' for testing
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    
    # Allow iframe embedding
    response.headers['X-Frame-Options'] = 'ALLOW-FROM https://yoursite.com'
    response.headers['Content-Security-Policy'] = "frame-ancestors https://yoursite.com 'self'"
    
    return response
```

#### **Step 2: Embed on Your Main Site**

Create a dedicated page on your main site:

```html
<!-- /admin on your main site -->
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Game Admin Dashboard</title>
    <style>
        body, html {
            margin: 0;
            padding: 0;
            height: 100%;
            overflow: hidden;
        }
        iframe {
            border: none;
            width: 100%;
            height: 100vh;
        }
    </style>
</head>
<body>
    <iframe src="https://game-api.onrender.com/admin" 
            allow="fullscreen"
            sandbox="allow-same-origin allow-scripts allow-forms">
    </iframe>
</body>
</html>
```

---

## ✅ **Solution 3: API Proxy (Most Secure)**

### **How It Works**
Your main site fetches data from the dashboard API and renders it in its own UI.

### **Pros**
- ✅ Complete control over UI
- ✅ No CORS issues (server-to-server)
- ✅ Can add custom authentication
- ✅ Single unified domain

### **Cons**
- ❌ More complex to implement
- ❌ Need to rebuild dashboard UI on main site
- ❌ Maintenance overhead (two UIs to maintain)

### **Implementation**

#### **On Your Main Flask App**

```python
import requests
from flask import Flask, render_template, jsonify

app = Flask(__name__)

DASHBOARD_API_URL = "https://game-api.onrender.com/api"

@app.route('/admin/dashboard')
def admin_dashboard():
    """Render dashboard page"""
    return render_template('admin_dashboard_proxy.html')

@app.route('/admin/api/sessions')
def proxy_sessions():
    """Proxy API calls to Render backend"""
    try:
        response = requests.get(f"{DASHBOARD_API_URL}/sessions")
        return jsonify(response.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/admin/api/sessions/<session_id>')
def proxy_session_details(session_id):
    """Proxy session details"""
    try:
        response = requests.get(f"{DASHBOARD_API_URL}/sessions/{session_id}")
        return jsonify(response.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500
```

#### **admin_dashboard_proxy.html**

Copy `admin_dashboard.html` but change:

```javascript
// Instead of:
const API_BASE = 'http://localhost:5001/api';

// Use:
const API_BASE = '/admin/api';  // Proxy through your main app
```

---

## 🔒 **Adding Authentication**

### **Basic Auth (Quick & Simple)**

#### **On api.py (Render)**

```python
from flask import Flask, request, Response
from functools import wraps

# Store in environment variable on Render
ADMIN_PASSWORD = os.getenv('ADMIN_DASHBOARD_PASSWORD', 'your-secret-password')

def check_auth(username, password):
    """Check if username/password is valid"""
    return username == 'admin' and password == ADMIN_PASSWORD

def authenticate():
    """Sends a 401 response that enables basic auth"""
    return Response(
        'Could not verify your access level for that URL.\n'
        'You have to login with proper credentials', 401,
        {'WWW-Authenticate': 'Basic realm="Admin Dashboard"'})

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

@app.route('/admin')
@requires_auth
def serve_admin_dashboard():
    """Protected admin dashboard"""
    return send_file('admin_dashboard.html')
```

### **Token-Based Auth (More Secure)**

#### **On api.py (Render)**

```python
import secrets
import os

# Generate on first run or store in env
ADMIN_TOKEN = os.getenv('ADMIN_TOKEN', secrets.token_urlsafe(32))

def requires_token(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.args.get('token') or request.headers.get('X-Admin-Token')
        if token != ADMIN_TOKEN:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

@app.route('/admin')
@requires_token
def serve_admin_dashboard():
    """Token-protected admin dashboard"""
    return send_file('admin_dashboard.html')
```

#### **Access URL**
```
https://game-api.onrender.com/admin?token=YOUR_SECRET_TOKEN
```

---

## 🚀 **Recommended Setup for Production**

### **Architecture**

```
Main Site (yoursite.com)
    ↓
    /admin page (iframe or link)
    ↓
Game API (game-api.onrender.com)
    ↓
    /admin endpoint (token-protected)
```

### **Step-by-Step**

1. **On Render (api.py)**:
   ```python
   @app.route('/admin')
   @requires_token
   def serve_admin_dashboard():
       response = make_response(send_file('admin_dashboard.html'))
       response.headers['Access-Control-Allow-Origin'] = 'https://yoursite.com'
       response.headers['X-Frame-Options'] = 'ALLOW-FROM https://yoursite.com'
       return response
   ```

2. **Set Environment Variable on Render**:
   ```
   ADMIN_TOKEN=your-secret-token-here
   ```

3. **On Your Main Site**:
   ```html
   <iframe src="https://game-api.onrender.com/admin?token=your-secret-token-here">
   </iframe>
   ```

4. **Or as a Link**:
   ```html
   <a href="https://game-api.onrender.com/admin?token=your-secret-token-here">
       Admin Dashboard
   </a>
   ```

---

## 📊 **Comparison Table**

| Solution | Complexity | Security | UX | Maintenance |
|----------|-----------|----------|-----|-------------|
| **Direct Link** | ⭐ Easy | ⚠️ Token needed | Opens new tab | None |
| **iframe Embed** | ⭐⭐ Medium | ✅ Token + CORS | Seamless | Low |
| **API Proxy** | ⭐⭐⭐ Hard | ✅✅ Full control | Best | High |

---

## 🔧 **Testing Locally**

### **Test Cross-Origin Access**

1. **Start dashboard API**:
   ```bash
   python api.py  # Runs on localhost:5001
   ```

2. **Create test HTML file** (`test_embed.html`):
   ```html
   <!DOCTYPE html>
   <html>
   <body>
       <h1>Testing Dashboard Embed</h1>
       <iframe src="http://localhost:5001/admin" width="100%" height="800px"></iframe>
   </body>
   </html>
   ```

3. **Serve on different port** (to simulate different domain):
   ```bash
   python -m http.server 8000
   ```

4. **Open** `http://localhost:8000/test_embed.html`

---

## 🎯 **Quick Start (Choose One)**

### **Option A: Simple Link**
```python
# Add to api.py
@app.route('/admin')
def serve_admin_dashboard():
    return send_file('admin_dashboard.html')
```

### **Option B: iframe Embed**
```python
# Add to api.py
@app.route('/admin')
def serve_admin_dashboard():
    response = make_response(send_file('admin_dashboard.html'))
    response.headers['Access-Control-Allow-Origin'] = '*'  # Your domain in production
    return response
```

Then embed on main site:
```html
<iframe src="https://game-api.onrender.com/admin" style="width:100%;height:100vh;border:none;"></iframe>
```

---

## 📝 **Environment Variables for Render**

Set these in your Render dashboard:

```bash
# Required
FLASK_ENV=production
PORT=5001  # Or whatever Render assigns

# Optional (for auth)
ADMIN_TOKEN=your-secret-token-here
ADMIN_PASSWORD=your-password-here
ALLOWED_ORIGIN=https://yoursite.com
```

---

## ⚡ **Summary**

**For your use case, I recommend:**

1. **iframe Embedding** - Easiest, looks professional
2. **With Token Auth** - Secure without complex setup
3. **CORS headers** - Allow your main site domain

This gives you:
- ✅ Professional look (embedded in main site)
- ✅ Secure (token-protected)
- ✅ Easy to maintain (one dashboard codebase)
- ✅ Works across domains

**Next:** Implement the code in the sections above! 🚀

