#!/usr/bin/env python
"""Disposable script — debug DeepSeek talk-to-data end to end.

Run:
    uv run python scripts/test_deepseek.py

DeepSeek exposes an OpenAI-compatible REST API, so this script calls it
directly via httpx (already installed as an anthropic transitive dep).
No extra packages needed.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

BASE_URL = "https://api.deepseek.com"
MODEL    = "deepseek-chat"   # DeepSeek-V3; use "deepseek-reasoner" for R1

# ── 1. Key present? ───────────────────────────────────────────────────────────
api_key = os.getenv("DEEPSEEK_API_KEY")
print(f"[1] DEEPSEEK_API_KEY present: {bool(api_key)}")
if not api_key:
    sys.exit("  ✗ Key missing — add DEEPSEEK_API_KEY=... to .env")
print(f"    key prefix: {api_key[:8]}...")

# ── 2. httpx importable? ──────────────────────────────────────────────────────
try:
    import httpx
    print(f"[2] httpx importable: yes  (version: {httpx.__version__})")
except ImportError:
    sys.exit("  ✗ httpx missing — run: uv add httpx")

# ── 3. Models endpoint reachable? ─────────────────────────────────────────────
print("[3] Checking /models endpoint...")
headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
try:
    r = httpx.get(f"{BASE_URL}/models", headers=headers, timeout=15)
    if r.status_code == 200:
        names = [m.get("id") for m in r.json().get("data", [])]
        print(f"    Available models: {names}")
    else:
        print(f"    HTTP {r.status_code}: {r.text[:200]}")
except Exception as e:
    print(f"  ✗ Models endpoint failed: {e}")

# ── 4. Raw chat/completions call ──────────────────────────────────────────────
print(f"\n[4] Calling {MODEL} with a simple question...")
payload = {
    "model": MODEL,
    "messages": [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user",   "content": "What is 2 + 2? Reply in one word."},
    ],
    "max_tokens": 20,
}
try:
    r = httpx.post(f"{BASE_URL}/chat/completions", headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"]
    print(f"    Response: {text!r}")
except httpx.HTTPStatusError as e:
    body = {}
    try:
        body = e.response.json()
    except Exception:
        pass
    code = body.get("error", {}).get("code", "")
    msg  = body.get("error", {}).get("message", str(e))
    print(f"  ✗ HTTP {e.response.status_code} — {code}: {msg[:300]}")
    if e.response.status_code in (401, 403):
        print("    → Invalid or expired API key.")
    elif e.response.status_code == 402:
        print("    → Insufficient balance. Top up at https://platform.deepseek.com/")
    sys.exit(1)
except Exception as e:
    print(f"  ✗ Request failed: {e}")
    sys.exit(1)

# ── 5. Full context call (mirrors dashboard usage) ────────────────────────────
print(f"\n[5] Full context call (system prompt + portfolio data)...")
system_prompt = (
    "You are a quantitative trading assistant analyzing a live portfolio.\n"
    "Answer concisely with specific numbers. Keep responses under 200 words.\n\n"
    "Current data context:\n"
    "=== Greeks Summary ===\n"
    "total_delta: 125.5\ntotal_theta: -32.0\ntotal_vega: 28.5\n"
)
payload2 = {
    "model": MODEL,
    "messages": [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": "What is my net delta exposure and is it balanced?"},
    ],
    "max_tokens": 300,
}
try:
    r = httpx.post(f"{BASE_URL}/chat/completions", headers=headers, json=payload2, timeout=30)
    r.raise_for_status()
    answer = r.json()["choices"][0]["message"]["content"]
    usage  = r.json().get("usage", {})
    print(f"    Answer: {answer[:300]}")
    print(f"    Tokens — prompt: {usage.get('prompt_tokens')}, completion: {usage.get('completion_tokens')}")
    print("\n✓ DeepSeek talk-to-data is working.")
except Exception as e:
    print(f"  ✗ Full context call failed: {e}")
