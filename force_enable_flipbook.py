#!/usr/bin/env python3
"""
Emergency script to force-enable flipbook mode for default session
Run this on Render to immediately enable flipbook mode
"""
import json
from pathlib import Path

state_path = Path("sessions/default/state.json")

if state_path.exists():
    with open(state_path, 'r', encoding='utf-8') as f:
        state = json.load(f)
    
    state['flipbook_mode'] = True
    
    with open(state_path, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    
    print(f"[SUCCESS] Flipbook mode enabled in {state_path}")
    print(f"[STATE] flipbook_mode = {state.get('flipbook_mode')}")
else:
    print(f"[ERROR] State file not found: {state_path}")



