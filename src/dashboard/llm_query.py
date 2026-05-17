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
- Trade history: 6 years of closed option trades (OPT asset category only — stock assignment \
trades excluded) with per-symbol trade count (n), win rate, P&L, and strategy fingerprints \
(CSP=cash-secured put, CC=covered call, LP=long put, LC=long call). \
A symbol with both CSP and CC in its strategy column has been traded as a wheel. \
n counts ALL closed OPT trades including assignments (pnl=0); wr% = pnl>0 trades / n. \
An assignment shows pnl=0 because IBKR credits the premium to the stock cost basis at exercise, \
not the option close row — it is neither a win nor a loss in the options ledger.
- Chronological trade log: every individual closed option trade with its exact trade date, \
symbol, strike (formatted as put/call+price, e.g. C4140.0 or P3960.0), expiry, \
quantity (negative=sold/short), and realised P&L in dollars. \
Sorted newest-to-oldest so the most recent trades always appear first. \
Assignment closes show pnl=0 — the premium collected at open was transferred into the stock \
cost basis by IBKR rather than credited to the option close row.
- OHLC price stats: for the top-traded and currently-held symbols — last price, 20/90-day return, \
position within all-time range (pos52w: 0%=at low, 100%=at high), MA trend (UP/DN/MX), \
and 20-day annualised historical volatility (hv20).
- OHLC monthly price history: split-adjusted monthly close prices for the last 24 months per \
symbol. Use these to identify stock splits (sudden ≥40% price gap between months), trend context \
at the time of trades, and price levels relative to option strikes.
- Live open IBKR orders: orders currently pending execution (symbol, secType, right, strike, \
expiry, action, qty, remaining, limit price, status).
- Suggested Orders from the Orders tab: Cover (sell covered calls on assigned stock), \
Sow (sell naked puts/calls to open new positions), Reap (buy-to-close profitable open options), \
and Protect (buy puts for downside hedging). Each order row includes symbol, right (C/P), \
strike, expiry, dte, qty (contracts), undPrice, xPrice (expected execution price), and a \
pre-computed total. xPrice × qty × 100 = dollar value per row.

You can cross-reference all data sources to answer questions, including max-earning scenarios \
from suggested orders.
You cannot run code or execute backtests — for backtesting use the History tab's Backtest Scoring.
Answer concisely with specific numbers. Never truncate a list, table, or sentence mid-way — \
always complete the final item. Keep responses under 500 words. \
When ranking or listing, show the top 5–10 items.

Current data context:
{context}"""


def _to_messages(history: list[dict]) -> list[dict]:
    """Convert [{q, a}, ...] history pairs to provider message-list format."""
    msgs: list[dict] = []
    for h in history:
        msgs.append({"role": "user",      "content": h["q"]})
        msgs.append({"role": "assistant", "content": h["a"]})
    return msgs


def query_data(question: str, context: dict, history: list[dict] | None = None) -> str:
    """Query portfolio data using Claude Haiku.

    Args:
        question: User question about the portfolio/data.
        context: Dict with keys like 'positions', 'greeks', 'metrics', 'ohlc_sample'.
                 Values can be DataFrames, dicts, or strings.
        history: Prior Q&A pairs as [{q, a}, ...] for multi-turn conversation.

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
    messages = _to_messages(history or []) + [{"role": "user", "content": question}]

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        system=system_prompt,
        messages=messages,
    )

    return message.content[0].text


def query_data_gemini(question: str, context: dict, history: list[dict] | None = None) -> str:
    """Query portfolio data using Gemini Flash.

    Args:
        question: User question about the portfolio/data.
        context: Dict with keys like 'positions', 'greeks', 'metrics', 'ohlc_sample'.
        history: Prior Q&A pairs as [{q, a}, ...] for multi-turn conversation.

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
    contents = []
    for h in (history or []):
        contents.append(types.Content(role="user",  parts=[types.Part(text=h["q"])]))
        contents.append(types.Content(role="model", parts=[types.Part(text=h["a"])]))
    contents.append(types.Content(role="user", parts=[types.Part(text=question)]))
    response = client.models.generate_content(
        model="gemini-flash-latest",
        contents=contents,
        config=types.GenerateContentConfig(system_instruction=system_prompt, max_output_tokens=2000),
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


def query_data_deepseek(question: str, context: dict, history: list[dict] | None = None) -> str:
    """Query portfolio data using DeepSeek-V3 (OpenAI-compatible REST API).

    Args:
        question: User question about the portfolio/data.
        context: Dict with keys like 'positions', 'greeks', 'metrics', 'ohlc_sample'.
        history: Prior Q&A pairs as [{q, a}, ...] for multi-turn conversation.

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
                *_to_messages(history or []),
                {"role": "user", "content": question},
            ],
            "max_tokens": 2000,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


# (key, section_header, columns_to_show) — defined once at module level, not per call.
_ORDER_SECTIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "orders_cover",
        "Suggested Orders — Cover (sell covered calls; reward = premium + capital gain if called)",
        ("symbol", "right", "strike", "expiry", "dte", "qty", "undPrice", "avgCost", "xPrice"),
    ),
    (
        "orders_sow",
        "Suggested Orders — Sow (sell naked puts/calls; reward = premium collected)",
        ("symbol", "right", "strike", "expiry", "dte", "qty", "undPrice", "xPrice"),
    ),
    (
        "orders_reap",
        "Suggested Orders — Reap (buy-to-close; negative = cost to close)",
        ("symbol", "right", "strike", "expiry", "dte", "qty", "avgCost", "xPrice"),
    ),
    (
        "orders_protect",
        "Suggested Orders — Protect (buy puts for downside hedge; negative = cost)",
        ("symbol", "right", "strike", "expiry", "dte", "qty", "xPrice", "cost", "protection", "puc"),
    ),
)


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

    if "trade_log" in context:
        tlog: list[dict] = context["trade_log"]
        lines.append(
            "\n=== Chronological Trade Log (closed OPT only, sorted oldest→newest) ==="
        )
        lines.append("date,sym,strike,expiry,qty,pnl")
        for t in tlog:
            lines.append(
                f"{t['date']},{t['sym']},{t['strike']},{t['expiry']},{t['qty']},{t['pnl']}"
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

    if "ohlc_price_history" in context:
        ph: dict[str, list] = context["ohlc_price_history"]
        if ph:
            all_months: list[str] = sorted({m for pairs in ph.values() for m, _ in pairs})
            lines.append(
                "\n=== OHLC Monthly Close Prices (split-adjusted, last 24 months) ==="
            )
            lines.append("sym," + ",".join(all_months))
            for sym, pairs in sorted(ph.items()):
                month_map = {m: f"{v:.2f}" for m, v in pairs}
                lines.append(sym + "," + ",".join(month_map.get(m, "") for m in all_months))

    if "open_orders" in context:
        df_oo = context["open_orders"]
        lines.append("\n=== Live Open IBKR Orders ===")
        oo_cols = ["symbol", "secType", "right", "strike", "expiry",
                   "action", "qty", "remaining", "orderType", "lmtPrice", "status"]
        cols = [c for c in oo_cols if c in df_oo.columns]
        lines.append(df_oo[cols].head(50).to_string(index=False))

    for key, header, want_cols in _ORDER_SECTIONS:
        if key not in context:
            continue
        df = context[key]
        if not hasattr(df, "columns") or df.empty:
            continue
        col_set = set(df.columns)
        cols = [c for c in want_cols if c in col_set]
        lines.append(f"\n=== {header} ===")
        lines.append("xPrice=expected execution price per contract (×qty×100 = $ value per row).")
        lines.append(df[cols].head(100).to_string(index=False))
        if {"xPrice", "qty"} <= col_set:
            prem = float((df["xPrice"] * df["qty"] * 100).sum())
            if key == "orders_cover" and {"strike", "avgCost"} <= col_set:
                cap_gain = float(((df["strike"] - df["avgCost"]) * df["qty"] * 100).sum())
                lines.append(
                    f"Total if all called: ${prem + cap_gain:,.0f}  "
                    f"(premium ${prem:,.0f} + capital gain ${cap_gain:,.0f})"
                )
            elif key == "orders_reap":
                lines.append(f"Total cost to close all: ${prem:,.0f}")
            elif key == "orders_protect" and "cost" in col_set:
                lines.append(f"Total protection cost: ${float(df['cost'].sum()):,.0f}")
            else:
                lines.append(f"Total expected premium: ${prem:,.0f}")

    return "\n".join(lines)
