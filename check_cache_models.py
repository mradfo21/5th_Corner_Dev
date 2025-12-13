#!/usr/bin/env python3
"""
Check which Gemini models support context caching
"""

import requests
import os
import json
from pathlib import Path

ROOT = Path(__file__).parent.resolve()

def get_api_key():
    """Get Gemini API key."""
    api_key = os.getenv("GEMINI_API_KEY")
    if api_key:
        return api_key
    
    config_path = ROOT / "config.json"
    if config_path.exists():
        with open(config_path, "r") as f:
            config = json.load(f)
            return config.get("GEMINI_API_KEY")
    
    return None

def check_models():
    """List all available models and check caching support."""
    api_key = get_api_key()
    if not api_key:
        print("ERROR: No GEMINI_API_KEY found")
        return
    
    print("Checking available Gemini models...\n")
    
    try:
        response = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": api_key},
            timeout=10
        )
        
        if response.status_code != 200:
            print(f"ERROR: {response.status_code}")
            print(response.text)
            return
        
        data = response.json()
        models = data.get("models", [])
        
        print(f"Found {len(models)} models:\n")
        print("=" * 80)
        
        caching_models = []
        
        for model in models:
            name = model.get("name", "").replace("models/", "")
            display_name = model.get("displayName", "")
            methods = model.get("supportedGenerationMethods", [])
            
            # Check if createCachedContent is supported
            supports_caching = "createCachedContent" in methods
            
            if supports_caching:
                caching_models.append(name)
                print(f"[CACHE OK] {name}")
                print(f"           {display_name}")
                print(f"           Methods: {', '.join(methods)}")
                print()
            else:
                print(f"[NO CACHE] {name}")
                print(f"           {display_name}")
                print()
        
        print("=" * 80)
        print(f"\nModels supporting caching ({len(caching_models)}):")
        if caching_models:
            for model in caching_models:
                print(f"  - {model}")
        else:
            print("  NONE - Caching API not available for your account")
            print("\n  To enable:")
            print("  1. Upgrade to paid Google Cloud account")
            print("  2. Enable billing")
            print("  3. Request access to Context Caching API")
            print("  4. https://console.cloud.google.com/")
        
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    check_models()

