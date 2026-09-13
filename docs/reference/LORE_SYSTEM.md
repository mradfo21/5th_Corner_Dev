# 📚 Lore Context Caching System

## ✅ **COMPLETE - Ready to Use!**

A flexible lore management system using Gemini's context caching API. Add your 18 pages of lore and the AI will have deep world knowledge at 1/10th the cost!

---

## 📁 **Folder Structure**

```
lore/
├── cache_config.json      ← Configuration
├── text/                  ← Your lore documents
│   ├── 00_README.md      ← Instructions
│   ├── EXAMPLE_world_overview.md
│   ├── 01_your_lore.md   ← Add your files here!
│   ├── 02_timeline.txt
│   └── ...
└── images/                ← Reference images
    ├── facility_map.png
    ├── character_ref.jpg
    └── ...
```

---

## 🚀 **How to Use**

### **Step 1: Enable the System**

Edit `lore/cache_config.json`:
```json
{
  "enabled": true,  ← Set to true
  "ttl_hours": 2,
  "auto_refresh": true
}
```

### **Step 2: Add Your Lore**

Place your 18 pages in `lore/text/`:
```bash
lore/text/01_world_overview.md
lore/text/02_horizon_industries.md
lore/text/03_characters.md
lore/text/04_timeline.txt
...
```

**Supported formats:**
- `.md` - Markdown (recommended)
- `.txt` - Plain text
- Any UTF-8 text file

### **Step 3: Add Reference Images (Optional)**

Place images in `lore/images/`:
```bash
lore/images/facility_map.png
lore/images/character_jason.jpg
lore/images/logo_horizon.png
```

**Supported formats:**
- `.png`, `.jpg`, `.jpeg`, `.webp`

### **Step 4: Start Bot**

```bash
python bot.py
```

The bot will automatically:
1. ✅ Load all lore files
2. ✅ Create Gemini cache
3. ✅ Include cache in every AI call
4. ✅ Auto-refresh when files change

---

## 💬 **Discord Commands**

### **`/lore_status`**
View current cache status:
```
📚 Lore Cache Status
✅ Active
📄 Files: 4 text, 2 images
🔢 Tokens: 12,450
⏰ Expires in: 1:23:45
💰 Storage cost: $0.0125/hour
🆔 Cache ID: cachedContents...
```

### **`/lore_refresh`**
Force immediate cache refresh:
```
✅ Lore Cache Refreshed
[Shows updated status]
```

---

## ⚙️ **Configuration Options**

Edit `lore/cache_config.json`:

```json
{
  "enabled": true,              // Enable/disable caching
  "ttl_hours": 2,               // Cache lifetime (1-24 hours)
  "auto_refresh": true,         // Auto-refresh on file changes
  "check_interval_seconds": 60, // How often to check for changes
  "file_order": [               // Optional: explicit load order
    "text/01_world_overview.md",
    "text/02_timeline.md",
    "images/facility_map.png"
  ]
}
```

### **File Order**

If `file_order` is specified:
- ✅ Files load in exact order listed
- ✅ Useful for logical sequencing

If `file_order` is empty/missing:
- ✅ Auto-loads all `.md`/`.txt` from `text/` (alphabetically)
- ✅ Then loads all images from `images/` (alphabetically)

---

## 🔄 **Hot Reloading**

The system automatically detects file changes:

```
1. You edit: lore/text/05_new_chapter.md
2. Save file [Ctrl+S]
3. Bot detects change (within 60 seconds)
4. Cache refreshes automatically
5. Next turn uses updated lore!
```

**No bot restart needed!** 🎉

---

## 💰 **Cost Breakdown**

### **LAZY LOADING: $0 When Idle!**

The cache is **NOT created when bot starts**. It's only created when someone actually plays.

```
Bot running, no players → No cache → $0 💚
           ↓
Player clicks Play → Cache created → Start charging ⏰
           ↓
Player finishes (2 hours) → Cache expires → Back to $0 💚
           ↓
Next day, new player → Cache recreated → Charged again ⏰
```

### **Cost During Active Gameplay**

Assume:
- 18 pages ≈ 12,000 tokens
- 100 AI calls per 2-hour session
- Cache expires after session

#### **Without Caching:**
```
100 calls × 12k tokens = 1.2M tokens
Cost: 1.2M × $0.075/1M = $0.090 per session
```

#### **With Caching (Lazy):**
```
Cache creation: 12k tokens × $0.075/1M = $0.0009 (once per session)
Cache storage: 12k tokens × $1.00/1M × 2 hours = $0.024 (only while playing)
100 calls: 12k cached tokens × $0.01/1M × 100 = $0.012 (during session)
Total: $0.037 per session
```

**Savings: 59% cheaper per session!**

**Idle cost: $0** (cache doesn't exist when no one is playing) 🎉

---

## 🎯 **How It Works Technically**

### **1. Cache Creation**
```python
# Load all lore files
parts = []
for file in lore_files:
    if text_file:
        parts.append({"text": file_content})
    elif image_file:
        parts.append({"inlineData": {...}})

# Create cache via Gemini API
cache = gemini.cachedContents.create(
    model="gemini-2.0-flash-exp",
    contents=[{"role": "user", "parts": parts}],
    ttl="7200s"  # 2 hours
)
```

### **2. Using Cache**
```python
# Every AI call includes cache reference
response = gemini.generateContent(
    model="gemini-2.0-flash-exp",
    cachedContent=cache.name,  # ← Reference cached lore
    contents="Player choice..."
)
```

### **3. File Monitoring**
```python
# Check file modification times every 60s
if files_modified():
    refresh_cache()
```

---

## 📊 **Features**

| Feature | Status |
|---------|--------|
| Text files (`.md`, `.txt`) | ✅ |
| Image files (`.png`, `.jpg`, `.webp`) | ✅ |
| Hot reloading | ✅ |
| Auto-refresh on changes | ✅ |
| Manual refresh command | ✅ |
| Status monitoring | ✅ |
| Custom file ordering | ✅ |
| Cost optimization | ✅ |
| Multi-provider (Gemini only) | ⚠️ |

**Note:** Context caching is Gemini-only. When using OpenAI provider, lore is not cached (but still works, just costs more).

---

## 🎮 **Use Cases**

### **World Consistency**
```
Lore: "Horizon Industries was founded in 1987"
Player: "When was this place built?"
AI: "According to records, Horizon Industries established 
     this facility in 1987..."
```

### **Character Knowledge**
```
Lore: "Jason Fleece is 34 years old, journalist"
Player: "How old am I?"
AI: "You're 34, though the stress of recent events 
     has aged you beyond your years."
```

### **Location Details**
```
Lore Image: [facility_map.png showing Building C-7]
Player: "Where am I?"
AI: "You're standing outside Building C-7, the main 
     research wing according to the facility map."
```

---

## 🐛 **Troubleshooting**

### **Cache not creating?**
- Check `lore/cache_config.json` has `"enabled": true`
- Ensure lore files exist in `lore/text/`
- Check logs for API errors
- Verify `GEMINI_API_KEY` is set

### **Files not detected?**
- Make sure files are in `lore/text/` or `lore/images/`
- Check file extensions (`.md`, `.txt`, `.png`, etc.)
- Wait up to 60 seconds for auto-detection
- Use `/lore_refresh` for immediate update

### **Cache expired?**
- Default TTL is 2 hours
- Increase `ttl_hours` in config
- Max TTL is 24 hours per Gemini API

### **High costs?**
- Cache storage: $1/M tokens/hour
- 12k tokens = $0.012/hour
- If too high, reduce lore or use shorter TTL

---

## 🚀 **Next Steps**

1. ✅ **Add your 18 pages** to `lore/text/`
2. ✅ **Add reference images** to `lore/images/` (optional)
3. ✅ **Enable caching** in `cache_config.json`
4. ✅ **Start bot** and check `/lore_status`
5. ✅ **Test it** - AI should reference your lore!

**Your lore is now part of the AI's knowledge base!** 📚✨

