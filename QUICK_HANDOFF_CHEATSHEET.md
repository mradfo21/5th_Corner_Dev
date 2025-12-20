# ⚡ Quick Handoff Cheatsheet - For Another Cursor AI

**TASK:** Embed game admin dashboard into main website  
**TIME:** 15 minutes  
**DIFFICULTY:** Easy (just an iframe)

---

## 🎯 What to Do

Create a new page that shows the dashboard in an iframe.

---

## 📋 Info Needed From User

Ask these 2 questions:

1. **"What is your Render URL?"**
   - Example answer: `https://somewhere-game-api.onrender.com`

2. **"What tech stack for your main site?"**
   - HTML, Flask, React, Express, WordPress, etc.

---

## 💻 Implementation (Pick One)

### **HTML (Any Static Site)**

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
    <iframe src="https://YOUR-RENDER-URL.onrender.com/admin"></iframe>
</body>
</html>
```

---

### **Flask**

Add to app:

```python
GAME_API_URL = "https://YOUR-RENDER-URL.onrender.com"

@app.route('/admin')
def admin():
    return f'''
        <iframe src="{GAME_API_URL}/admin" 
                style="width:100%;height:100vh;border:none;">
        </iframe>
    '''
```

---

### **Express**

Add route:

```javascript
const GAME_API_URL = 'https://YOUR-RENDER-URL.onrender.com';

app.get('/admin', (req, res) => {
    res.send(`
        <iframe src="${GAME_API_URL}/admin" 
                style="width:100%;height:100vh;border:none;">
        </iframe>
    `);
});
```

---

### **React/Next.js**

Create `pages/admin.js`:

```jsx
export default function Admin() {
  return (
    <iframe 
      src="https://YOUR-RENDER-URL.onrender.com/admin"
      style={{ width: '100%', height: '100vh', border: 'none' }}
    />
  );
}
```

---

### **WordPress**

Use "Iframe" plugin or add Custom HTML block:

```html
<iframe src="https://YOUR-RENDER-URL.onrender.com/admin" 
        style="width:100%;height:80vh;border:none;">
</iframe>
```

---

## ✅ Test

1. Visit the admin page
2. Dashboard should load in iframe
3. Check browser console (F12) - should have NO errors
4. Try clicking on a session - should work

---

## 🐛 If It Doesn't Work

**Blank iframe?**
- Check URL is correct
- Test API: Visit `https://YOUR-RENDER-URL.onrender.com/api/health`
- Should return: `{"status":"ok"}`

**CORS error?**
- Shouldn't happen (CORS is enabled)
- If it does, contact game API team

---

## 📦 What's Already Done

✅ Game API is deployed and running  
✅ Dashboard endpoint is `/admin`  
✅ CORS is enabled (works from any domain)  
✅ No authentication needed  

❌ Just need to embed it on main site

---

## 🎯 Success = This Works

```
User visits: yoursite.com/admin
They see: Game dashboard with sessions
They can: View sessions, history, tapes
```

---

## 📖 Full Docs (If Needed)

- `HANDOFF_TO_CURSOR.md` - Detailed instructions
- `INTEGRATION_GUIDE_FOR_MAIN_SITE.md` - Complete guide

---

## ⚡ Absolute Simplest Version

**Just 4 lines:**

```html
<iframe 
    src="https://YOUR-RENDER-URL.onrender.com/admin"
    style="width:100%;height:100vh;border:none;">
</iframe>
```

**Replace URL → Add to page → Done!** ✨

