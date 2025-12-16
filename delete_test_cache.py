import requests
import os
import json
from pathlib import Path

ROOT = Path(__file__).parent.resolve()

def get_api_key():
    api_key = os.getenv("GEMINI_API_KEY")
    if api_key:
        return api_key
    config_path = ROOT / "config.json"
    if config_path.exists():
        with open(config_path, "r") as f:
            return json.load(f).get("GEMINI_API_KEY")
    return None

api_key = get_api_key()

# List caches
response = requests.get(
    "https://generativelanguage.googleapis.com/v1beta/cachedContents",
    headers={"x-goog-api-key": api_key},
    timeout=10
).json()

caches = response.get("cachedContents", [])
print(f"Found {len(caches)} cache(s)")

for cache in caches:
    cache_id = cache.get("name")
    display = cache.get("displayName")
    tokens = cache.get("usageMetadata", {}).get("totalTokenCount", 0)
    print(f"Deleting: {cache_id.split('/')[-1]} ({display}, {tokens} tokens)")
    
    delete_response = requests.delete(
        f"https://generativelanguage.googleapis.com/v1beta/{cache_id}",
        headers={"x-goog-api-key": api_key},
        timeout=10
    )
    
    if delete_response.status_code in [200, 204]:
        print("  [OK] Deleted")
    else:
        print(f"  [ERROR] {delete_response.status_code}")

print("\nAll test caches cleaned up!")



