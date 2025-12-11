#!/usr/bin/env python3
"""Test if gemini-2.0-flash supports multimodal vision"""
import requests
import json
import os

# Load config
try:
    with open("config.json") as f:
        config = json.load(f)
except:
    config = {}

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", config.get("GEMINI_API_KEY", ""))

print(f"API Key: {GEMINI_API_KEY[:20]}...{GEMINI_API_KEY[-8:]}\n")

# TEST: Multimodal with gemini-2.0-flash
print("=" * 60)
print("TEST: Multimodal (image + text) with gemini-2.0-flash")
print("=" * 60)

# Create a tiny test image (1x1 red pixel PNG)
tiny_png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

response = requests.post(
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
    headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
    json={
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": "image/png", "data": tiny_png}},
                {"text": "What color is this image?"}
            ]
        }],
        "generationConfig": {"temperature": 0.5, "maxOutputTokens": 50}
    },
    timeout=15
)

print(f"Status: {response.status_code}")
print(f"Response:\n{json.dumps(response.json(), indent=2)}\n")

if response.status_code == 200 and "candidates" in response.json():
    print("✅ SUCCESS! gemini-2.0-flash SUPPORTS multimodal vision!")
elif response.status_code == 403:
    print("❌ 403 FORBIDDEN - API key may not have permissions for vision")
    print("   OR gemini-2.0-flash doesn't support vision on free tier")
else:
    print(f"❌ FAILED with status {response.status_code}")

