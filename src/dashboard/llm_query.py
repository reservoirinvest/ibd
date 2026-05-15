"""Minimal LLM integration for querying dashboard data.

Usage:
    from src.dashboard.llm_query import query_data, query_data_gemini, query_data_deepseek

    response = query_data(
        question="What's my total delta exposure?",
        context={
            "positions": df_positions,
            "greeks": {"delta": 125, "theta": -45, "vega": 30},
        }
    )
    print(response)
"""

from __future__ import annotations

import os

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

_SYSTEM_PROMPT_TEMPLATE = """\
You are a quantitative trading assistant analyzing a live portfolio.
You have access to current positions, Greeks (delta/theta/vega), OHLC data, and risk metrics.
Answer the user's question concisely with specific numbers and actionable insights.
Keep responses under 200 words.

Current data context:
{context}"""


def query_data(question: str, context: dict) -> str:
    """Query portfolio data using Claude Haiku.

    Args:
        question: User question about the portfolio/data.
        context: Dict with keys like 'positions', 'greeks', 'metrics', 'ohlc_sample'.
                 Values can be DataFrames, dicts, or strings.

    Returns:
        Claude's response string.

    Raises:
        ValueError: If ANTHROPIC_API_KEY not in environment.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set in .env")

    client = Anthropic(api_key=api_key)
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(context=_format_context(context))

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        system=system_prompt,
        messages=[{"role": "user", "content": question}],
    )

    return message.content[0].text


def query_data_gemini(question: str, context: dict) -> str:
    """Query portfolio data using Gemini Flash.

    Args:
        question: User question about the portfolio/data.
        context: Dict with keys like 'positions', 'greeks', 'metrics', 'ohlc_sample'.

    Returns:
        Gemini's response string.

    Raises:
        ValueError: If GEMINI_API_KEY not in environment.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set in .env")

    from google import genai  # lazy import — not always installed
    from google.genai import types

    client = genai.Client(api_key=api_key)
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(context=_format_context(context))
    response = client.models.generate_content(
        model="gemini-flash-latest",
        contents=question,
        config=types.GenerateContentConfig(system_instruction=system_prompt, max_output_tokens=500),
    )
    # SDK returns None text when the response is empty or safety-blocked
    text = response.text
    if not text:
        try:
            text = response.candidates[0].content.parts[0].text
        except (IndexError, AttributeError):
            text = None
    if not text:
        reason = "unknown"
        try:
            reason = str(response.candidates[0].finish_reason)
        except (IndexError, AttributeError):
            pass
        raise ValueError(f"Gemini returned empty response (finish_reason={reason})")
    return text


def query_data_deepseek(question: str, context: dict) -> str:
    """Query portfolio data using DeepSeek-V3 (OpenAI-compatible REST API).

    Args:
        question: User question about the portfolio/data.
        context: Dict with keys like 'positions', 'greeks', 'metrics', 'ohlc_sample'.

    Returns:
        DeepSeek's response string.

    Raises:
        ValueError: If DEEPSEEK_API_KEY not in environment.
        httpx.HTTPStatusError: On API error (401 bad key, 402 no balance, 429 rate limit).
    """
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY not set in .env")

    import httpx  # transitive dep via anthropic — always available

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(context=_format_context(context))
    r = httpx.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
            "max_tokens": 500,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _format_context(context: dict) -> str:
    """Format context dict as readable prompt text. Truncates large DataFrames."""
    lines = []

    if "positions" in context:
        lines.append("=== Current Positions ===")
        positions = context["positions"]
        text = positions.to_string() if hasattr(positions, "to_string") else str(positions)
        lines.append(text[:1000])

    if "greeks" in context:
        lines.append("\n=== Greeks Summary ===")
        greeks = context["greeks"]
        if isinstance(greeks, dict):
            for k, v in greeks.items():
                lines.append(f"{k}: {v}")
        else:
            lines.append(str(greeks)[:300])

    if "metrics" in context:
        lines.append("\n=== Account Metrics ===")
        metrics = context["metrics"]
        if isinstance(metrics, dict):
            for k, v in metrics.items():
                lines.append(f"{k}: {v}")
        else:
            lines.append(str(metrics)[:300])

    if "ohlc_sample" in context:
        lines.append("\n=== Recent OHLC (sample) ===")
        ohlc = context["ohlc_sample"]
        text = ohlc.to_string() if hasattr(ohlc, "to_string") else str(ohlc)
        lines.append(text[:500])

    return "\n".join(lines)
