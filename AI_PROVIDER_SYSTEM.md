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

