#!/usr/bin/env python3
"""Test Gemini API - text only vs. multimodal"""
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

# TEST 1: Text-only with gemini-2.0-flash
print("=" * 60)
print("TEST 1: Text-only with gemini-2.0-flash")
print("=" * 60)
response1 = requests.post(
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
    headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
    json={
        "contents": [{"parts": [{"text": "Say hello"}]}],
        "generationConfig": {"temperature": 0.5, "maxOutputTokens": 10}
    }
)
print(f"Status: {response1.status_code}")
print(f"Response: {json.dumps(response1.json(), indent=2)}\n")

# TEST 2: Text-only with gemini-1.5-flash (known working model)
print("=" * 60)
print("TEST 2: Text-only with gemini-1.5-flash")
print("=" * 60)
response2 = requests.post(
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent",
    headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
    json={
        "contents": [{"parts": [{"text": "Say hello"}]}],
        "generationConfig": {"temperature": 0.5, "maxOutputTokens": 10}
    }
)
print(f"Status: {response2.status_code}")
print(f"Response: {json.dumps(response2.json(), indent=2)}\n")

# TEST 3: Multimodal with gemini-1.5-flash (if text works)
if response2.status_code == 200:
    print("=" * 60)
    print("TEST 3: Multimodal (image) with gemini-1.5-flash")
    print("=" * 60)
    
    # Create a tiny test image (1x1 pixel PNG)
    import base64
    tiny_png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    
    response3 = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent",
        headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
        json={
            "contents": [{
                "parts": [
                    {"inlineData": {"mimeType": "image/png", "data": tiny_png}},
                    {"text": "What color is this image?"}
                ]
            }],
            "generationConfig": {"temperature": 0.5, "maxOutputTokens": 50}
        }
    )
    print(f"Status: {response3.status_code}")
    print(f"Response: {json.dumps(response3.json(), indent=2)}\n")

