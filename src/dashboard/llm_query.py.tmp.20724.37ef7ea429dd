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
You are a quantitative trading assistant with access to:
- Live portfolio: current positions, Greeks (delta/theta/vega), and account metrics
- Trade history: 6 years of closed option trades with per-symbol P&L, win rate, profit factor, \
and strategy fingerprints (CSP=cash-secured put, CC=covered call, LP=long put, LC=long call). \
A symbol with both CSP and CC in its strategy column has been traded as a wheel.
- OHLC price stats: for the top-traded and currently-held symbols — last price, 20/90-day return, \
position within all-time range (pos52w: 0%=at low, 100%=at high), MA trend (UP/DN/MX), \
and 20-day annualised historical volatility (hv20).

You can cross-reference all three data sources to answer questions.
You cannot run code or execute backtests — for backtesting use the History tab's Backtest Scoring.
Answer concisely with specific numbers. Keep responses under 250 words. \
When ranking or listing, show the top 5–10 items.

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
    """Format context dict as readable prompt text."""
    lines = []

    if "positions" in context:
        lines.append("=== Current Positions ===")
        positions = context["positions"]
        text = positions.to_string() if hasattr(positions, "to_string") else str(positions)
        lines.append(text[:1500])

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

    if "global_stats" in context:
        lines.append("\n=== Trade History — Global Stats ===")
        for k, v in context["global_stats"].items():
            lines.append(f"{k}: {v}")

    if "per_symbol" in context:
        rows: list[dict] = context["per_symbol"]
        lines.append("\n=== Trade History — Per-Symbol (sym,trades,win%,total_pnl,best_trade,worst_trade,strategies) ===")
        lines.append("sym,n,wr%,pnl,best,worst,strat")
        for r in rows:
            lines.append(
                f"{r['sym']},{r['n']},{r['wr%']},{r['pnl']},{r['best']},{r['worst']},{r['strat']}"
            )

    if "ohlc_stats" in context:
        ohlc_rows: list[dict] = context["ohlc_stats"]
        lines.append(
            "\n=== OHLC Price Stats (sym,last_price,20d_ret%,90d_ret%,"
            "pos_in_range_0to100,ma_trend,hv20_annualised) ==="
        )
        lines.append("sym,price,r20d,r90d,pos52w,trend,hv20")
        for r in ohlc_rows:
            lines.append(
                f"{r['sym']},{r['price']},{r['r20d']},{r['r90d']},"
                f"{r['pos52w']},{r['trend']},{r['hv20']}"
            )

    return "\n".join(lines)
