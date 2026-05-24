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
- Live portfolio: current positions, Greeks (delta/theta/vega), and account metrics. \
The positions list is the COMPLETE and DEFINITIVE record of what is held right now — \
if a symbol is absent from the positions list it is NOT currently held, regardless of \
what appears in trade history. Positions may be live (connected) or cached (last known \
state); the header will say which. If positions are absent entirely, do not infer \
holdings from trade history. \
Key metric definitions: \
"Cushion" = ExcessLiquidity / NetLiquidation (already expressed as a percentage in the metrics — \
do NOT recalculate it; a 23.6% Cushion means 23.6%, not 0.01). \
"GrossPositionValue" is NOT a leverage denominator — it is the sum of absolute market values \
of all positions; the relevant leverage metric is NetLiquidation vs InitMarginReq or \
the Cushion percentage directly.
- Trade history: 6 years of closed option trades (OPT asset category only — stock assignment \
trades excluded) with per-symbol trade count (n), win rate, P&L, and strategy fingerprints \
(CSP=cash-secured put, CC=covered call, LP=long put, LC=long call). \
A symbol with both CSP and CC in its strategy column has been traded as a wheel. \
n counts ALL closed OPT trades including assignments (pnl=0); wr% = pnl>0 trades / n. \
An assignment shows pnl=0 because IBKR credits the premium to the stock cost basis at exercise, \
not the option close row — it is neither a win nor a loss in the options ledger.
- Chronological trade log: every individual CLOSED (no longer open) option trade with its exact \
trade date, symbol, strike (formatted as put/call+price, e.g. C4140.0 or P3960.0), expiry, \
quantity (negative=sold/short), and realised P&L in dollars. \
CRITICAL: this log is HISTORICAL — it contains only trades that have already been closed. \
Do NOT use it to infer what is currently held or open. For current positions use the positions \
section exclusively. \
Sorted newest-to-oldest so the most recent trades always appear first. \
Assignment closes show pnl=0 — the premium collected at open was transferred into the stock \
cost basis by IBKR rather than credited to the option close row.
- OHLC price stats: for the top-traded and currently-held symbols — last price, 20/90-day return, \
position within all-time range (pos52w: 0%=at low, 100%=at high), MA trend (UP/DN/MX), \
and 20-day annualised historical volatility (hv20).
- OHLC monthly price history: split-adjusted monthly close prices for the last 24 months per \
symbol. Use these to identify stock splits (sudden ≥40% price gap between months), trend context \
at the time of trades, and price levels relative to option strikes.
- Backtest scores: per-symbol Backtest Expert composite score 0–100 computed from historical \
closed OPT trades (same source as Symbol Deep-Dive). Verdict: DEPLOY ≥70, REFINE 40–69, \
ABANDON <40 or if any CRITICAL flag. Four sub-scores each 0–25: \
sample (trade count/density — CRITICAL if <30 trades), \
expectancy (profit factor — CRITICAL if PF<1.0), \
risk (max drawdown — CRITICAL if >40%), \
robustness (years tested — CRITICAL if <3 yrs, WARNING if <5 yrs). \
Win rate = pnl>0 trades / total closed OPT trades (assignments with pnl=0 count in denominator). \
Profit factor = gross profit / gross loss across all closed OPT trades for that symbol.
- Live open IBKR orders: orders currently pending execution (symbol, secType, right, strike, \
expiry, action, qty, remaining, limit price, status).
- Suggested Orders from the Orders tab: Cover (sell covered calls on assigned stock), \
Sow (sell naked puts/calls to open new positions), Reap (buy-to-close profitable open options), \
and Protect (buy puts for downside hedging). Each order row includes symbol, right (C/P), \
strike, expiry, dte, qty (contracts), undPrice, xPrice (expected execution price), and a \
pre-computed total. xPrice × qty × 100 = dollar value per row. \
IMPORTANT: Reap entries are derived directly from live positions — every row is a currently open \
option. Use the right/strike/expiry/avgCost values from the Reap table (not trade_log) for \
current position details.
- Consolidated NAV: month-end total portfolio value (US + SG accounts combined), sourced from \
IBKR Flex EquitySummaryByReportDateInBase. Reflects full MTM including unrealized P&L, dividends, \
interest, and FX. Provided as month-end values for the full available history (from ~2020) plus \
YTD and since-Jan-2025 returns. Confirmed: Jan 1 2025 = $632,507; May 18 2026 = $954,938. \
Performance KPIs pre-computed from daily NAV (Sharpe ratio full history and since Jan 2025, \
max drawdown full history and since Jan 2025, TWR full history) are provided directly — \
use those numbers as-is and do NOT attempt to re-derive them from the monthly closes.
- SPY & QQQ benchmark monthly closes: price history from Jan 2020 (month-end closes in USD). \
Use these to compare portfolio NAV growth against market benchmarks, compute cumulative returns, \
or answer questions like "how did the portfolio compare to SPY since 2022?"
- Cash transactions: deposits and withdrawals for the last 2 years (SGD amounts = SG account; \
USD = US account; no FX conversion applied), plus dividends and broker interest aggregated by \
year. Use these to understand capital injections, income, and to cross-check NAV changes.
- Symbol classification: the Monthly-only list names every S&P 500 symbol that has ONLY \
monthly/quarterly expiries (gap ≥20 days). Any symbol NOT in that list has weekly or near-weekly \
expiries. Lookup rule: to answer "is X weekly?" — if X is absent from the monthly-only list → \
YES (weekly); if X appears in the list → NO (monthly-only).
- IB commissions: total_commissions_usd in global_stats is the sum of ibCommission for all trades \
in the Flex history (negative = cost paid to IBKR). Taxes are not separately tracked in the Flex \
export.

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

    _pos_live = context.get("positions_is_live", True)
    _pos_as_of = context.get("positions_as_of", "")
    if "positions" in context:
        _pos_label = "Current Positions (LIVE)" if _pos_live else f"Current Positions (CACHED as of {_pos_as_of} — dashboard offline)"
        lines.append(f"=== {_pos_label} ===")
        lines.append("AUTHORITATIVE: for options the right/strike/expiry columns below are definitive.")
        lines.append("Do NOT use trade_log to infer any option's right, strike, or expiry — trade_log is closed history only.")
        if not _pos_live:
            lines.append("WARNING: live IBKR connection unavailable. Positions may be stale. Qualify any recommendations accordingly.")
        positions = context["positions"]
        text = positions.to_string(index=False)
        lines.append(text[:2000])
    else:
        lines.append("=== Current Positions ===")
        lines.append("NO POSITIONS — portfolio is empty or dashboard is not connected to IBKR.")
        lines.append("Do NOT infer current holdings from trade history. The portfolio holds nothing right now.")

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
        _tlog_cap = 200
        _tlog_shown = tlog[:_tlog_cap]
        _tlog_note = (
            f" (showing most recent {_tlog_cap} of {len(tlog)} total)"
            if len(tlog) > _tlog_cap else ""
        )
        lines.append(
            f"\n=== Chronological Trade Log (closed OPT only, newest→oldest{_tlog_note}) ==="
        )
        lines.append("date,sym,strike,expiry,qty,pnl")
        for t in _tlog_shown:
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

    if "backtest_scores" in context:
        bts: list[dict] = context["backtest_scores"]
        lines.append(
            "\n=== Backtest Scores per Symbol "
            "(composite 0-100; sub-scores each 0-25: sample=trade density, "
            "expect=profit factor, risk=drawdown, robust=years tested) ==="
        )
        lines.append("sym,score,verdict,n,wr%,pf,yrs,sample,expect,risk,robust,flags")
        for r in bts:
            lines.append(
                f"{r['sym']},{r['score']},{r['verdict']},{r['n']},{r['wr%']}%,"
                f"{r['pf']},{r['yrs']},{r['sample']},{r['expect']},{r['risk']},{r['robust']},"
                f"{r['flags']}"
            )

    if "nav_summary" in context:
        nav = context["nav_summary"]
        lines.append("\n=== Consolidated NAV (US + SG accounts combined, from IBKR Flex) ===")
        lines.append(f"Latest: ${nav['current']:,.0f} as of {nav['current_date']}")
        if nav.get("ytd_return_pct") is not None:
            lines.append(f"YTD return: {nav['ytd_return_pct']:+.2f}%")
        if nav.get("since_jan2025_pct") is not None:
            lines.append(f"Since 2025-01-01: {nav['since_jan2025_pct']:+.2f}%")
        lines.append("Pre-computed KPIs (daily NAV — use these directly, do not re-derive from monthly closes):")
        if nav.get("twr_full_pct") is not None:
            lines.append(f"  TWR full history: {nav['twr_full_pct']:+.2f}%")
        if nav.get("sharpe_full") is not None:
            lines.append(f"  Sharpe ratio full history: {nav['sharpe_full']:.2f}  (annualised daily returns ×√252, no risk-free rate)")
        if nav.get("max_drawdown_full_pct") is not None:
            lines.append(f"  Max drawdown full history: {nav['max_drawdown_full_pct']:.2f}%")
        if nav.get("sharpe_since_jan2025") is not None:
            lines.append(f"  Sharpe ratio since 2025-01-01: {nav['sharpe_since_jan2025']:.2f}")
        if nav.get("max_drawdown_since_jan2025_pct") is not None:
            lines.append(f"  Max drawdown since 2025-01-01: {nav['max_drawdown_since_jan2025_pct']:.2f}%")
        if nav.get("monthly"):
            lines.append("Month-end NAV (full history):")
            for d, v in nav["monthly"]:
                lines.append(f"  {d}: ${v:,}")

    if "benchmark_prices" in context:
        bp: dict[str, list] = context["benchmark_prices"]
        if bp:
            lines.append("\n=== SPY & QQQ Benchmark Monthly Closes (USD, 2020 onwards) ===")
            lines.append("Format: YYYY-MM: price")
            for bsym, pairs in bp.items():
                price_str = "  ".join(f"{m}: {v}" for m, v in pairs)
                lines.append(f"{bsym}: {price_str}")

    if "cash_summary" in context:
        cash = context["cash_summary"]
        lines.append("\n=== Cash Transactions ===")
        if cash.get("recent_dw"):
            lines.append("Deposits/Withdrawals last 2 years (SGD amounts = SG account; USD = US account):")
            for r in cash["recent_dw"]:
                lines.append(f"  {r['date']}  {r['currency']} {r['amount']:+,.2f}")
        if cash.get("dividends_by_year"):
            div_str = "  ".join(f"{y}: ${v:,}" for y, v in sorted(cash["dividends_by_year"].items()))
            lines.append(f"Dividends by year (USD equivalent): {div_str}")
        if cash.get("interest_by_year"):
            int_str = "  ".join(f"{y}: ${v:,}" for y, v in sorted(cash["interest_by_year"].items()))
            lines.append(f"Broker interest by year (USD equivalent): {int_str}")

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

    if "symbol_categories" in context:
        sc = context["symbol_categories"]
        monthly_only: list[str] = sc.get("monthly_only", [])
        weekly_count = len(sc.get("weekly", []))
        lines.append(
            "\n=== Symbol Classification — Weekly vs Monthly-Only Options ==="
        )
        lines.append(
            "LOOKUP RULE: if a symbol is NOT in the Monthly-only list below → it HAS weekly "
            "options (answer YES to 'is X weekly?'). If it IS in the list → monthly-only "
            "(answer NO to 'is X weekly?')."
        )
        lines.append(
            f"Monthly-only ({len(monthly_only)} symbols — ONLY monthly/quarterly expiries; "
            f"must NOT be sown weekly): {', '.join(monthly_only)}"
        )
        lines.append(
            f"All other ~{weekly_count} S&P 500 symbols in the chain have near-weekly expiries "
            f"and CAN be sown weekly."
        )

    return "\n".join(lines)
