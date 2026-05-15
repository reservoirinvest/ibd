#!/usr/bin/env python
"""Disposable script — debug Gemini talk-to-data end to end.

Run:
    uv run python scripts/test_gemini.py
"""
import os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

# ── 1. Key present? ──────────────────────────────────────────────────────────
api_key = os.getenv("GEMINI_API_KEY")
print(f"[1] GEMINI_API_KEY present: {bool(api_key)}")
if not api_key:
    sys.exit("  ✗ Key missing — add GEMINI_API_KEY=... to .env")
print(f"    key prefix: {api_key[:8]}...")

# ── 2. Package importable? ────────────────────────────────────────────────────
try:
    from google import genai
    from google.genai import types
    print(f"[2] google-genai importable: yes  (version: {genai.__version__})")
except ImportError as e:
    sys.exit(f"  ✗ Import failed: {e}\n  Run: uv add google-genai")

# ── 3. Client constructs? ─────────────────────────────────────────────────────
try:
    client = genai.Client(api_key=api_key)
    print("[3] Client constructed: yes")
except Exception as e:
    sys.exit(f"  ✗ Client error: {e}")

# ── 4. List available models ──────────────────────────────────────────────────
print("[4] Available models (google-genai v2 SDK):")
try:
    found = []
    for m in client.models.list():
        name = getattr(m, "name", str(m))
        # v2 SDK: supported_actions or supported_generation_methods
        actions = (
            getattr(m, "supported_actions", None)
            or getattr(m, "supported_generation_methods", None)
            or []
        )
        if any("generate" in str(a).lower() for a in actions):
            found.append(name)
    if found:
        for n in found:
            print(f"    {n}")
    else:
        print("    (none matched — listing all names instead)")
        for m in client.models.list():
            print(f"    {getattr(m, 'name', m)}")
except Exception as e:
    print(f"  ✗ ListModels failed: {e}")

# ── 5. Raw generate_content call ──────────────────────────────────────────────
for MODEL in ("gemini-flash-latest", "gemini-2.0-flash"):
    print(f"\n[5] Calling {MODEL}...")
    try:
        resp = client.models.generate_content(
            model=MODEL,
            contents="What is 2 + 2? Reply in one word.",
            config=types.GenerateContentConfig(max_output_tokens=20),
        )
        text = resp.text
        if not text:
            try:
                text = resp.candidates[0].content.parts[0].text
            except Exception:
                text = None
        print(f"    Response text: {text!r}")
        if text:
            print(f"  → Working model: '{MODEL}'")
            break
        else:
            finish = "unknown"
            try:
                finish = str(resp.candidates[0].finish_reason)
            except Exception:
                pass
            print(f"  ✗ Empty response (finish_reason={finish})")
    except Exception as e:
        msg = str(e)
        if "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower():
            print(f"  ✗ Quota/billing error — free-tier limit is 0 for this key.")
            print("    Fix: get an AI Studio key at https://aistudio.google.com/app/apikey")
        else:
            print(f"  ✗ Failed: {e}")

# ── 6. Full query_data_gemini path ────────────────────────────────────────────
print("\n[6] Testing query_data_gemini() (the actual dashboard function)...")
try:
    from src.dashboard.llm_query import query_data_gemini
    ans = query_data_gemini(
        "What is my net delta exposure?",
        {"greeks": {"total_delta": 125.5, "total_theta": -32.0}},
    )
    print(f"    Answer: {ans[:200]}")
    print("\n✓ Gemini talk-to-data is working.")
except Exception as e:
    print(f"  ✗ query_data_gemini failed: {e}")
