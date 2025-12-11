#!/usr/bin/env python3
"""Quick test to verify Gemini API key works"""
import requests
import os
import json

# Load config
try:
    with open("config.json") as f:
        config = json.load(f)
except:
    config = {}

# Get API key (same way as engine.py)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", config.get("GEMINI_API_KEY", ""))

print(f"Testing Gemini API key: {GEMINI_API_KEY[:20]}...{GEMINI_API_KEY[-8:]}")
print(f"Key length: {len(GEMINI_API_KEY)} characters")

# Test API call
response = requests.post(
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
    headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
    json={
        "contents": [{"parts": [{"text": "Say 'Hello!' in one word."}]}],
        "generationConfig": {"temperature": 0.5, "maxOutputTokens": 10}
    },
    timeout=15
)

print(f"\nHTTP Status: {response.status_code}")
print(f"Response: {response.json()}")

if response.status_code == 200 and "candidates" in response.json():
    print("\n✅ SUCCESS! Gemini API key is working!")
else:
    print("\n❌ FAILED! Gemini API key is NOT working!")
    if "error" in response.json():
        error = response.json()["error"]
        print(f"Error code: {error.get('code')}")
        print(f"Error message: {error.get('message')}")

