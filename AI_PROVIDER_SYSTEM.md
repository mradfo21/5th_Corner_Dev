# 🤖 AI Provider Management System

## Overview

Flexible AI provider switching system that allows runtime configuration changes without redeployment. Supports multiple AI providers (OpenAI, Gemini) for both text and image generation.

---

## 📁 **Files Created**

### **`ai_config.json`**
Centralized configuration file storing current AI provider settings.

```json
{
  "text_provider": "gemini",
  "text_model": "gemini-2.0-flash",
  "image_provider": "gemini",
  "image_model": "gemini-2.0-flash-exp-imagen",
  "last_updated": "2025-12-12T00:00:00Z",
  "available_configs": {
    "gemini_fast": { ... },
    "openai": { ... },
    "hybrid_fast": { ... }
  }
}
```

### **`ai_provider_manager.py`**
Python module for managing AI provider configuration:
- `load_ai_config()` - Load config with 5s caching
- `save_ai_config()` - Save config to disk
- `get_text_provider()` / `get_text_model()` - Get current text settings
- `get_image_provider()` / `get_image_model()` - Get current image settings
- `set_preset(name)` - Switch to preset configuration
- `get_status()` - Human-readable status string
- `get_available_presets()` - List available presets

---

## 🎛️ **Available Presets**

### **`gemini_fast`** (Default)
```
Text: gemini/gemini-2.0-flash
Image: gemini/gemini-2.0-flash-exp-imagen
```
**Best for:** Speed & cost efficiency

### **`openai`**
```
Text: openai/gpt-4o-mini
Image: openai/gpt-image-1
```
**Best for:** OpenAI's latest image model
**Features:** Full img2img support (up to 16 reference images!)

### **`hybrid_fast`**
```
Text: gemini/gemini-2.0-flash
Image: openai/dall-e-3
```
**Best for:** Fast narrative + quality images

---

## 💬 **Discord Commands**

### **`/ai_status`**
View current AI configuration.

**Output:**
```
🤖 AI Configuration

✨ Text Generation
  Provider: `gemini`
  Model: `gemini-2.0-flash`

🖼️ Image Generation
  Provider: `gemini`
  Model: `gemini-2.0-flash-exp-imagen`

🕐 Last Updated: 2025-12-12T00:00:00Z
```

### **`/ai_presets`**
List all available AI provider presets.

**Output:**
```
🎛️ Available AI Presets

`gemini_fast`
  Text: `gemini/gemini-2.0-flash`
  Image: `gemini/gemini-2.0-flash-exp-imagen`

`openai`
  Text: `openai/gpt-4o-mini`
  Image: `openai/dall-e-3`

`hybrid_fast`
  Text: `gemini/gemini-2.0-flash`
  Image: `openai/dall-e-3`
```

### **`/ai_switch <preset>`**
Switch to a different AI provider preset.

**Example:**
```
/ai_switch openai
```

**Output:**
```
✅ Switched to `openai`

[Shows updated configuration]
```

---

## 🔧 **Code Changes**

### **`engine.py`**

#### **Before:**
```python
def _ask(prompt: str, model="gemini", ...):
    # Hardcoded Gemini call
    response = requests.post(
        "https://...gemini-2.0-flash:generateContent",
        ...
    )
```

#### **After:**
```python
def _ask(prompt: str, model="gemini", ...):
    provider = ai_provider_manager.get_text_provider()
    model_name = ai_provider_manager.get_text_model()
    
    if provider == "gemini":
        return _ask_gemini(prompt, model_name, ...)
    elif provider == "openai":
        return _ask_openai(prompt, model_name, ...)

def _ask_gemini(...):
    # Gemini implementation with dynamic model

def _ask_openai(...):
    # OpenAI implementation with dynamic model
```

#### **Image Generation:**
```python
# Before
if IMAGE_PROVIDER == "gemini":
    ...

# After
active_image_provider = ai_provider_manager.get_image_provider()
if active_image_provider == "gemini":
    ...
```

### **`bot.py`**

Added slash commands:
- `/ai_status`
- `/ai_presets`
- `/ai_switch`

Added `on_ready()` event to sync slash commands:
```python
@bot.event
async def on_ready():
    print(f"[BOT] {bot.user} is ready!")
    synced = await bot.tree.sync()
    print(f"[BOT] Synced {len(synced)} slash command(s)")
```

---

## 🚀 **How It Works**

### **1. Configuration Loading**
```python
# Engine initializes provider manager on import
import ai_provider_manager

# Loads ai_config.json
# Caches for 5 seconds (hot-reloadable)
```

### **2. Runtime Switching**
```
User runs: /ai_switch openai
├─ bot.py receives command
├─ calls ai_provider_manager.set_preset("openai")
├─ updates ai_config.json
└─ next _ask() call uses new provider
```

### **3. Cache Invalidation**
Config is cached for 5 seconds, then reloaded:
- Allows runtime changes without restart
- Minimal disk I/O overhead
- Thread-safe with locks

---

## ✅ **Benefits**

1. **Technology Agnostic** - Abstracted above specific providers
2. **Runtime Switching** - No redeploy needed
3. **Preset System** - Quick switching between configs
4. **Discord Integration** - Change models mid-game
5. **Extensible** - Easy to add new providers (Anthropic, etc.)

---

## 🖼️ **Krea 2 Image Backend**

Krea 2 is a foundation image model that plugs in as a **drop-in alternative to
Gemini ("Nano Banana")** for image generation — the same way world models are
swapped, but for stills. Text generation stays on Gemini in the Krea presets.

**Krea 2 Medium is the shipped production default** (`ai_config.json` →
`image_provider: "krea"`, `image_model: "krea-2/medium"`) because it renders
the world faster than Gemini Pro (~12s vs ~15-30s) while keeping strong quality.

### Switching between tiers / providers

Runtime (no redeploy), via Discord: `/ai_switch krea` (Medium, fast — default)
or `/ai_switch krea_large` (Large, higher quality). Fall back to Gemini/OpenAI/
Veo anytime with `/ai_switch gemini` etc. The engine routes on
`ai_provider_manager.get_image_provider()`, so nothing else changes.

| Preset | Image model | Approx. latency | Notes |
|--------|-------------|-----------------|-------|
| `krea` | `krea-2/medium` | ~12s | **Default.** Fast, faster than Gemini Pro |
| `krea_large` | `krea-2/large` | ~24s | Higher quality / more textured |

The tier is driven by the **configured `image_model`** (the preset is
authoritative), decoupled from the Gemini quality toggle so speed is
predictable. `hd_mode` is only a fallback when the config names no tier.

If a Krea job fails or times out on a given turn and `GEMINI_API_KEY` is set,
the engine automatically renders that single frame with Gemini so the world
never goes blank.

### Config / secrets

| Env var | Default | Purpose |
|---------|---------|---------|
| `KREA_API_KEY` (or `KREA_API_TOKEN`) | — | API token from krea.ai/settings/api-tokens (**required** for Krea) |
| `KREA_CREATIVITY` | `low` | `raw`/`low`/`medium`/`high` — how far Krea expands the prompt |
| `KREA_STYLE_STRENGTH` | `0.6` | 0–1 carry-over strength of the previous frame as a style reference |
| `KREA_API_BASE` | `https://api.krea.ai` | Override for testing |
| `KREA_ASPECT_RATIO` | `4:3` | Output aspect ratio |
| `KREA_RESOLUTION` | `1K` | Output resolution |

### How it works (`krea_image_utils.py`)

Krea uses an **async job API** (unlike Gemini's inline base64):

1. `POST /generate/image/krea/krea-2/{medium|large}` → `{ job_id }`
2. Poll `GET /jobs/{job_id}` until `status == "completed"`
3. Download `result.urls[0]`, normalize to PNG, and write the same
   `<name>.png` + `<name>_small.png` sidecar the rest of the engine expects.

**img2img = style transfer.** For continuity frames the previous frame(s) are
uploaded to `POST /assets` (cached by path+mtime) and passed as
`image_style_references: [{url, strength}]`. This carries palette/grain/mood
forward. Krea style transfer is a **style** lock, not a pixel-perfect spatial
lock, so the shared spatial-anchor prompt text still does the compositional
work. If no reference is usable it degrades gracefully to text-to-image.

The public functions mirror `gemini_image_utils` exactly
(`generate_with_krea`, `generate_krea_img2img`), so `engine._gen_image()` calls
either provider through identical call sites.

## 🔮 **Future Extensions**

### **Easy to add:**
- Anthropic Claude
- Mistral
- Custom endpoints
- Per-function overrides (e.g., always use GPT-4o for death detection)

### **Example:**
```json
"available_configs": {
  "anthropic_fast": {
    "text_provider": "anthropic",
    "text_model": "claude-3-5-sonnet-20241022",
    "image_provider": "gemini",
    "image_model": "gemini-2.0-flash-exp-imagen"
  }
}
```

Just implement `_ask_anthropic()` in engine.py!

---

## 🎮 **Usage in Demo**

```
🎬 Demo Scenario:

1. Start with gemini_fast (cheap, fast)
2. Player reaches climactic moment
3. Admin runs: /ai_switch openai
4. Next turn uses GPT-4o + DALL-E
5. High-quality cinematic scene
6. Switch back to gemini_fast for normal play
```

**Cost optimization + quality when it counts!**

