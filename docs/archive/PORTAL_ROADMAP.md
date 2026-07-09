# Multi-Frontend Portal Experience - Roadmap

## Vision

Create a **game engine portal** that can support multiple UI frontends (Discord bot, web app, mobile app) simultaneously, with each frontend controlling its own isolated game session.

---

## ✅ What's Complete (Foundation)

### 1. Session Isolation ✅
- **Status:** Complete & Deployed
- All game data stored per session: `sessions/{session_id}/`
- Images, tapes, videos, state, history all isolated
- Thread-safe architecture with explicit session_id passing
- **Testing:** Comprehensive tests passed

### 2. API Wrapper ✅
- **Status:** Complete & Tested
- Flask REST API exposing all engine functions
- Can switch between direct mode and API mode via environment variable
- Basic endpoints for game flow (intro, actions, state)
- **Testing:** `test_api.py` passing

### 3. API Client Proxy ✅
- **Status:** Complete & Deployed
- Drop-in replacement for `import engine`
- Smart routing between direct calls and HTTP API
- Session ID support built in
- **Testing:** `test_api_client_sessions.py` passing

### 4. Thread-Safety ✅
- **Status:** Complete & Verified
- No global state pollution
- Concurrent sessions tested and working
- File locks for state access
- **Testing:** Concurrent access tests passed

---

## 🔨 What's Needed (Portal Features)

### Phase 1: Session Management API 🎯 **CRITICAL**

**Goal:** Allow frontends to create, list, and manage their own sessions

**Missing APIs:**
```python
POST   /api/sessions                # Create new session, returns session_id
GET    /api/sessions                # List all sessions
GET    /api/sessions/{id}           # Get session info (state, metadata)
DELETE /api/sessions/{id}           # Delete session and all data
GET    /api/sessions/{id}/status    # Check if alive/dead, turn count, etc.
```

**Why Critical:**
- Web UI needs to create sessions on user arrival
- Users need to see their active sessions
- Cleanup of old/abandoned sessions

**Estimated Work:** 4-6 hours

---

### Phase 2: Real-Time Progress Updates 🎯 **HIGH PRIORITY**

**Goal:** Stream progress to frontend during slow operations (image generation, LLM calls)

**Current Problem:**
- Image generation takes 5-15 seconds
- Frontend has no feedback during this time
- User thinks app is frozen

**Solutions (Pick One):**

#### Option A: Server-Sent Events (SSE) ⭐ RECOMMENDED
```python
GET /api/sessions/{id}/stream   # SSE endpoint for progress updates
```

**Advantages:**
- Simple to implement (Flask-SSE library)
- One-way server → client (perfect for progress)
- Works with HTTP/1.1 (no WebSocket needed)
- Auto-reconnect on disconnect

**Example Progress Events:**
```
event: image_generation_started
data: {"frame": 2, "estimated_time": 12}

event: image_generation_progress
data: {"status": "Calling Gemini API..."}

event: image_generation_complete
data: {"image_url": "/api/sessions/abc/images/frame_2.png"}
```

#### Option B: WebSocket
**Advantages:** Two-way communication
**Disadvantages:** More complex, overkill for progress updates

#### Option C: Polling
**Advantages:** Simple
**Disadvantages:** Inefficient, higher latency

**Estimated Work (SSE):** 6-8 hours

---

### Phase 3: Static Asset Serving 🎯 **HIGH PRIORITY**

**Goal:** Serve generated images, tapes, and videos to frontends via HTTP

**Current State:**
- Images saved to disk: `sessions/{id}/images/*.png`
- No HTTP endpoint to retrieve them

**Needed:**
```python
GET /api/sessions/{id}/images/{filename}     # Serve image
GET /api/sessions/{id}/tapes/{filename}      # Serve GIF tape
GET /api/sessions/{id}/videos/{filename}     # Serve video segment
```

**Security Considerations:**
- Validate session_id and filename to prevent path traversal
- Return 404 for non-existent files
- Add basic authentication (if needed)

**Estimated Work:** 2-3 hours

---

### Phase 4: Frontend-Friendly Responses 🎯 **MEDIUM PRIORITY**

**Goal:** Make API responses consistent and easy to consume

**Current State:**
```python
# Inconsistent response formats
return jsonify({"state": state})           # Some endpoints
return jsonify({"success": True, "data": result})  # Other endpoints
```

**Standardize to:**
```python
# Success response
{
  "success": true,
  "data": { ... },
  "meta": {
    "timestamp": "2025-12-19T19:47:49Z",
    "session_id": "abc123"
  }
}

# Error response
{
  "success": false,
  "error": {
    "code": "SESSION_NOT_FOUND",
    "message": "Session 'xyz' does not exist",
    "details": { ... }
  },
  "meta": {
    "timestamp": "2025-12-19T19:47:49Z"
  }
}
```

**Estimated Work:** 3-4 hours

---

### Phase 5: CORS & Security 🎯 **MEDIUM PRIORITY**

**Goal:** Allow web frontends to call API from browser

**Needed:**
```python
# Enable CORS for web UI origin
from flask_cors import CORS

CORS(app, origins=[
    "http://localhost:3000",  # Local dev
    "https://yourgame.com"    # Production
])
```

**Security Additions:**
- Basic API key authentication (if public API)
- Rate limiting (flask-limiter)
- Request validation
- Session ownership/access control

**Estimated Work:** 4-5 hours

---

### Phase 6: Session Lifecycle & Cleanup 🎯 **LOW PRIORITY**

**Goal:** Auto-cleanup abandoned sessions, prevent disk bloat

**Features:**
```python
# Track session metadata
sessions/{id}/meta.json:
{
  "created_at": "2025-12-19T19:00:00Z",
  "last_accessed": "2025-12-19T19:45:00Z",
  "owner": "discord_user_123",  # Optional
  "frontend": "web" | "discord"
}

# Cleanup logic
- Delete sessions inactive for > 24 hours
- Delete sessions marked as "completed"
- Archive tapes before deletion (optional)
```

**Estimated Work:** 3-4 hours

---

### Phase 7: Web UI (Separate Project) 🎯 **FUTURE**

**Tech Stack Options:**

#### Option A: Simple HTML/JS (No Framework) ⭐ FASTEST
- Plain HTML + vanilla JavaScript
- Fetch API for HTTP calls
- EventSource API for SSE
- Deploy as static files

**Advantages:**
- No build process
- No dependencies
- Fast to build (~8-12 hours)
- Flask serves static files directly

#### Option B: React/Next.js
- Modern component-based UI
- Rich ecosystem
- Better state management

**Disadvantages:**
- Build process required
- More complex deployment
- Longer development time (~20-30 hours)

**Estimated Work:** 
- Simple HTML/JS: 8-12 hours
- React: 20-30 hours

---

## 📋 Implementation Priority

### Minimum Viable Portal (MVP) - ~20 hours

1. **Session Management API** (6 hours)
   - Create, list, delete sessions
   - Session status endpoint

2. **Static Asset Serving** (3 hours)
   - Serve images, tapes, videos

3. **CORS Support** (2 hours)
   - Enable web UI to call API

4. **Basic Web UI** (8 hours)
   - Simple HTML/JS frontend
   - Intro screen
   - Choice buttons
   - Image display

5. **Testing & Polish** (1 hour)
   - End-to-end testing
   - Documentation

**Result:** Functional web UI that can play the game

---

### Full-Featured Portal - ~35 hours

Everything in MVP, plus:

6. **SSE Progress Updates** (8 hours)
   - Stream progress to frontend
   - Loading states

7. **Frontend-Friendly Responses** (4 hours)
   - Standardize API format
   - Better error handling

8. **Session Lifecycle** (4 hours)
   - Auto-cleanup
   - Session metadata

9. **Enhanced Web UI** (8 hours)
   - Improved UX/styling
   - Tape gallery
   - Game history viewer

10. **Security & Rate Limiting** (3 hours)
    - API authentication
    - Rate limiting
    - Input validation

**Result:** Production-ready multi-frontend portal

---

## 🎯 Recommended Next Steps

### Option 1: Quick MVP (Proof of Concept)

**Goal:** Get a working web UI in one weekend

**Steps:**
1. Add session management API (6 hours)
2. Add static asset serving (3 hours)
3. Enable CORS (1 hour)
4. Build basic HTML/JS UI (8 hours)

**Total:** ~18 hours (2 days)

**Result:** Playable web UI, no real-time updates yet

---

### Option 2: Production-Ready (Full Portal)

**Goal:** Build it right the first time

**Steps:**
1. Session management API (6 hours)
2. SSE progress updates (8 hours)
3. Static asset serving (3 hours)
4. CORS + security (5 hours)
5. Standardized responses (4 hours)
6. Basic web UI (8 hours)
7. Session lifecycle (4 hours)

**Total:** ~38 hours (1 week)

**Result:** Production-ready portal

---

### Option 3: Enhance Discord Bot First

**Goal:** Improve existing UI before building new ones

**Focus:**
- Perfect the Discord experience
- Add more game features
- Build content/lore
- **Then** add web UI when game is polished

**Rationale:** Better to have one great frontend than two mediocre ones

---

## 🗂️ Current Architecture

```
┌─────────────────────────────────────────────────────┐
│                  FRONTENDS (UI)                     │
├─────────────────────────────────────────────────────┤
│  Discord Bot (✅ Working)   │   Web UI (🔨 Needed)  │
│  - Commands                 │   - Browser-based     │
│  - Buttons                  │   - Responsive        │
│  - Embeds                   │   - Modern UX         │
└──────────────┬──────────────┴──────────┬────────────┘
               │                         │
               ▼                         ▼
        ┌──────────────────────────────────────┐
        │      API CLIENT (✅ Complete)         │
        │  - Direct mode / API mode            │
        │  - Session ID support                │
        └──────────────┬───────────────────────┘
                       │
                       ▼
               ┌───────────────┐
               │  API LAYER    │  🔨 Needs enhancement
               │  (Flask)      │  - Session mgmt
               │               │  - SSE progress
               │               │  - Asset serving
               └───────┬───────┘
                       │
                       ▼
          ┌─────────────────────────┐
          │   GAME ENGINE           │  ✅ Complete
          │   - Session isolation   │
          │   - Thread-safe         │
          │   - Pure logic          │
          └──────────┬──────────────┘
                     │
                     ▼
        ┌────────────────────────────┐
        │    SESSION STORAGE          │  ✅ Complete
        │  sessions/                  │
        │  ├─ default/                │
        │  │  ├─ state.json           │
        │  │  ├─ history.json         │
        │  │  ├─ images/              │
        │  │  ├─ tapes/               │
        │  │  └─ films/               │
        │  └─ session_abc123/         │
        └─────────────────────────────┘
```

---

## 💡 Key Insights

### What You Already Have ✅
- **Solid foundation:** Session isolation is the hardest part, and it's done
- **API framework:** Flask API exists, just needs enhancement
- **Flexible architecture:** Can add frontends without touching engine
- **Tested & stable:** Current system works reliably

### What's Actually Needed 🔨
- **Session CRUD API:** ~6 hours
- **Static file serving:** ~3 hours
- **Basic web UI:** ~8 hours

**Total for MVP:** ~17 hours of work

### The Gap Is Small!
You're 80% of the way there. The remaining 20% is mostly:
1. A few new API endpoints
2. A simple HTML/JS frontend

---

## 🚀 My Recommendation

### Start with Session Management API

**Why:**
- Required for any frontend
- Small, focused task
- Can test immediately with curl/Postman
- Doesn't require building full UI yet

**Implementation:**
```python
# api.py additions

@app.route('/api/sessions', methods=['POST'])
def create_session():
    """Create a new game session"""
    import uuid
    session_id = str(uuid.uuid4())
    # Initialize session
    engine.reset_state(session_id)
    return jsonify({
        "success": True,
        "data": {
            "session_id": session_id,
            "created_at": datetime.utcnow().isoformat()
        }
    })

@app.route('/api/sessions', methods=['GET'])
def list_sessions():
    """List all active sessions"""
    sessions_dir = Path("sessions")
    sessions = []
    for session_path in sessions_dir.iterdir():
        if session_path.is_dir():
            sessions.append({
                "session_id": session_path.name,
                "created_at": ...  # From meta.json or file mtime
            })
    return jsonify({"success": True, "data": sessions})
```

**Testing:**
```bash
# Create session
curl -X POST http://localhost:5001/api/sessions

# List sessions
curl http://localhost:5001/api/sessions
```

**Estimated Time:** 4-6 hours

---

## Summary

### You Have:
✅ Session isolation (the hard part)
✅ Basic API structure
✅ Thread-safe architecture
✅ Working Discord frontend

### You Need:
🔨 Session management API (~6 hours)
🔨 Static asset serving (~3 hours)
🔨 Simple web UI (~8 hours)

### Total Work to MVP:
**~17 hours** (2 weekend days)

### Decision Point:
1. **Build web UI now?** → Follow MVP roadmap above
2. **Polish Discord bot first?** → Delay web UI, add game features
3. **Production portal?** → Follow full-featured roadmap (~38 hours)

**What do you want to tackle first?**


