"""
Example Flask app showing how to integrate the dashboard on your main site

This represents your MAIN website (different server from the game API)
"""

from flask import Flask, render_template_string, redirect

app = Flask(__name__)

# Your game API URL (deployed on Render)
GAME_API_URL = "https://your-api.onrender.com"  # CHANGE THIS
ADMIN_TOKEN = "your-admin-token-here"  # CHANGE THIS (or load from env)

# ═══════════════════════════════════════════════════════════════════════════
# METHOD 1: Direct Link
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/')
def home():
    """Main site homepage"""
    return render_template_string('''
        <!DOCTYPE html>
        <html>
        <head>
            <title>My Game Website</title>
            <style>
                body {
                    font-family: Arial, sans-serif;
                    max-width: 800px;
                    margin: 50px auto;
                    padding: 20px;
                }
                .btn {
                    display: inline-block;
                    padding: 15px 30px;
                    background: #4ecdc4;
                    color: white;
                    text-decoration: none;
                    border-radius: 5px;
                    font-size: 1.2em;
                    margin: 10px;
                }
                .btn:hover {
                    background: #3db8b0;
                }
            </style>
        </head>
        <body>
            <h1>🎮 SOMEWHERE - Analog Horror Game</h1>
            <p>Welcome to the main site!</p>
            
            <h2>Admin Access</h2>
            
            <!-- Option 1: Direct link (opens in new tab) -->
            <a href="{{ admin_url }}" target="_blank" class="btn">
                🎮 Admin Dashboard (New Tab)
            </a>
            
            <!-- Option 2: Embedded dashboard page -->
            <a href="/admin" class="btn">
                📊 Admin Dashboard (Embedded)
            </a>
        </body>
        </html>
    ''', admin_url=f"{GAME_API_URL}/admin?token={ADMIN_TOKEN}")


# ═══════════════════════════════════════════════════════════════════════════
# METHOD 2: iframe Embedding
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/admin')
def admin_dashboard():
    """Embedded admin dashboard page"""
    return render_template_string('''
        <!DOCTYPE html>
        <html>
        <head>
            <title>Admin Dashboard</title>
            <style>
                body, html {
                    margin: 0;
                    padding: 0;
                    height: 100%;
                    overflow: hidden;
                    background: #1a1a2e;
                }
                
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
                }
                
                .header a:hover {
                    text-decoration: underline;
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
                    font-family: Arial, sans-serif;
                }
            </style>
        </head>
        <body>
            <div class="header">
                <h1>🎮 Game Administration</h1>
                <a href="/">← Back to Main Site</a>
            </div>
            
            <div class="loading" id="loading">Loading dashboard...</div>
            
            <iframe 
                id="dashboard"
                src="{{ dashboard_url }}"
                allow="fullscreen"
                sandbox="allow-same-origin allow-scripts allow-forms allow-popups"
                onload="document.getElementById('loading').style.display='none'">
            </iframe>
            
            <script>
                // Hide loading after 10 seconds even if iframe doesn't load
                setTimeout(() => {
                    const loading = document.getElementById('loading');
                    if (loading) {
                        loading.textContent = 'Dashboard failed to load. Check your connection.';
                        loading.style.color = '#ff6b6b';
                    }
                }, 10000);
            </script>
        </body>
        </html>
    ''', dashboard_url=f"{GAME_API_URL}/admin?token={ADMIN_TOKEN}")


# ═══════════════════════════════════════════════════════════════════════════
# METHOD 3: Redirect to Dashboard
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/admin-direct')
def admin_redirect():
    """Redirect directly to the dashboard"""
    return redirect(f"{GAME_API_URL}/admin?token={ADMIN_TOKEN}")


if __name__ == '__main__':
    print("=" * 60)
    print("MAIN SITE RUNNING")
    print("=" * 60)
    print()
    print("This represents your MAIN website (separate from game API)")
    print()
    print("Pages:")
    print("  • http://localhost:8000/ - Homepage")
    print("  • http://localhost:8000/admin - Embedded dashboard")
    print("  • http://localhost:8000/admin-direct - Direct link")
    print()
    print("Make sure your game API is running on Render!")
    print("=" * 60)
    
    app.run(host='0.0.0.0', port=8000, debug=True)

