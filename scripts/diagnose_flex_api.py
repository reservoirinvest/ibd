"""Diagnose IBKR Flex API connectivity and query configuration.

Usage:
    uv run python scripts/diagnose_flex_api.py

Prints:
  - Token / query-ID presence (masked)
  - Raw XML element structure returned by the API
  - Whether Trade or TradeConfirm topics are present
  - Specific fix instructions based on what was found
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))

from src.dashboard.settings import get_settings


def _mask(s: str) -> str:
    return s[:4] + "****" + s[-2:] if len(s) > 6 else "****"


def _last_business_week() -> tuple[str, str]:
    """Return (from_date, to_date) strings covering the previous Mon-Fri week (YYYYMMDD)."""
    today = date.today()
    # roll back to last Friday
    offset = (today.weekday() - 4) % 7 or 7   # days since Friday; at least 1
    friday = today - timedelta(days=offset)
    monday = friday - timedelta(days=4)
    return monday.strftime("%Y%m%d"), friday.strftime("%Y%m%d")


def _print_error_guidance(error_code: str) -> None:
    guidance = {
        "1003": "Query not found. Verify TRADES_FLEXID in .env matches the Query ID shown in the portal.",
        "1004": "Query ID and token do not match. The token belongs to a different user/account than the query.",
        "1005": "Query not found (invalid ID). Check TRADES_FLEXID in .env.",
        "1006": "Account does not have Flex Web Service access enabled.\n"
                "  Portal: Settings -> Account Settings -> Flex Web Service -> enable and generate token.",
        "1009": "Flex query is not scheduled / not enabled for API access.\n"
                "  Portal: edit the query -> ensure it is saved and active.",
        "1010": "Account lacks Flex Web Service privileges. Contact IBKR support.",
        "1011": "Statement is still being prepared by IBKR. Wait 30 seconds and retry.",
        "1012": "Token has expired. Regenerate it in portal: Settings -> Flex Web Service.",
        "1013": "IP address is not in the allowed list for this token.",
        "1014": "Query returned no data for the configured period.",
        "1015": "Token is invalid. Ensure TOKEN in .env was copied exactly from the portal (no spaces/quotes).",
        "1019": "Too many concurrent requests. Wait a minute and retry.",
    }
    msg = guidance.get(error_code)
    if msg:
        print(f"\n  Guidance: {msg}")
    else:
        print(f"\n  (No specific guidance for error code {error_code}. Check IBKR Flex API documentation.)")


def run() -> None:
    s = get_settings()
    token = s.token.get_secret_value()
    qid   = s.trades_flexid.get_secret_value()

    print("=== Flex API Diagnostics ===\n")
    print(f"TOKEN     : {'SET (' + _mask(token) + ')' if token else 'NOT SET'}")
    print(f"FLEXID    : {'SET (' + _mask(qid) + ')' if qid else 'NOT SET'}")

    if not (token and qid):
        print("\nAdd TOKEN and TRADES_FLEXID to .env and re-run.")
        return

    print(f"\nCalling IBKR Flex API for query {qid} ...")
    try:
        from ib_async.flexreport import FlexReport
        report = FlexReport(token=token, queryId=qid)
    except Exception as e:
        print(f"\nFAIL: API call failed: {e}")
        return

    root = report.root
    print(f"\nRoot tag  : {root.tag}")

    # IBKR returns FlexStatementResponse (not FlexQueryResponse) when there is an error.
    # Extract and print the error text before going further.
    if root.tag == "FlexStatementResponse":
        status   = (root.find(".//Status")       or root.find("Status"))
        err_code = (root.find(".//ErrorCode")    or root.find("ErrorCode"))
        err_msg  = (root.find(".//ErrorMessage") or root.find("ErrorMessage"))
        s  = status.text.strip()   if (status   is not None and status.text)   else "?"
        ec = err_code.text.strip() if (err_code is not None and err_code.text) else "?"
        em = err_msg.text.strip()  if (err_msg  is not None and err_msg.text)  else "?"
        print(f"\nFAIL: IBKR returned an error response.")
        print(f"  Status       : {s}")
        print(f"  ErrorCode    : {ec}")
        print(f"  ErrorMessage : {em}")
        _print_error_guidance(ec)
        return

    # Detect date range from FlexStatement
    flex_stmt = root.find(".//FlexStatement")
    from_dt = flex_stmt.get("fromDate", "?") if flex_stmt is not None else "?"
    to_dt   = flex_stmt.get("toDate",   "?") if flex_stmt is not None else "?"
    print(f"Date range: {from_dt} to {to_dt}")

    print(f"\nXML structure (depth <= 4, child count = actual records at that level):")
    def _dump(node, depth=0, max_depth=4):
        indent = "  " * depth
        attrs  = " ".join(f"{k}={v!r}" for k, v in list(node.attrib.items())[:3])
        children = list(node)
        n = len(children)
        # Annotate the key data-bearing nodes so it's obvious what the count means
        note = ""
        if node.tag == "Trades":
            note = f"  <-- trade records ({n} = no data for this period)" if n == 0 else f"  <-- {n} trade records"
        elif node.tag in ("Trade", "TradeConfirm"):
            note = "  <-- one trade record"
        elif node.tag == "FlexStatement":
            note = "  <-- one entry per account"
        print(f"{indent}<{node.tag}{' ' + attrs if attrs else ''}> [{n} children]{note}")
        if depth < max_depth:
            # For Trade/TradeConfirm nodes just show the first 3 as a sample
            for child in (children[:3] if node.tag in ("Trades", "TradeConfirm") else children):
                _dump(child, depth + 1, max_depth)
            if node.tag in ("Trades", "TradeConfirm") and n > 3:
                print(f"{indent}  ... ({n - 3} more)")
    _dump(root)

    # Dump ALL unique element tags in the entire XML — catches non-standard element names
    # returned when Symbol Summary / Order / Execution sub-report options are enabled.
    all_tags = sorted({node.tag for node in root.iter()})
    all_with_attrs = sorted({node.tag for node in root.iter() if node.attrib})
    print(f"\nAll element tags in response : {all_tags}")
    print(f"Tags that carry attributes   : {all_with_attrs}")

    # Count records for every non-wrapper tag
    wrapper_tags = {"FlexQueryResponse", "FlexStatements", "FlexStatement",
                    "Trades", "OpenPositions", "FxPositions", "CashTransactions"}
    data_tags = [t for t in all_tags if t not in wrapper_tags]
    if data_tags:
        print("\nRecord counts per data tag:")
        for tag in data_tags:
            print(f"  <{tag}>: {len(list(root.iter(tag)))}")
    else:
        print("\nNo data-bearing elements found (all tags are wrapper/container tags).")

    # Now decide on outcome
    trade_records   = list(root.iter("Trade"))
    trade_confirm   = list(root.iter("TradeConfirm"))
    trades_node     = root.find(".//Trades")

    if trade_records:
        print(f"\nOK: Found {len(trade_records)} <Trade> records — API is working correctly.")
    elif trade_confirm:
        print(f"\nOK: Found {len(trade_confirm)} <TradeConfirm> records.")
    elif data_tags:
        print(
            f"\nWARN: Data elements present ({data_tags}) but none are <Trade> or <TradeConfirm>.\n"
            "      The 'Symbol Summary' / 'Order' / 'Execution' sub-report options in the Trades\n"
            "      section may be changing the element names IBKR returns.\n"
            "      In IBKR portal edit the query -> Trades section -> click Options and\n"
            "      UNCHECK all sub-report options (Symbol Summary, Order, Execution, etc.).\n"
            "      Only field-level checkboxes (accountId, symbol, dateTime ...) should be on.\n"
            "      Save and re-run this script."
        )
    elif trades_node is not None:
        # Section present but entirely empty
        today_wd = date.today().weekday()  # 0=Mon, 6=Sun
        is_weekend = today_wd >= 5
        fri_from, fri_to = _last_business_week()
        print(
            f"\nWARN: <Trades> section IS present but contains 0 records.\n"
            f"      Date range returned by IBKR: {from_dt} to {to_dt}\n"
        )
        if is_weekend:
            print(
                "      Today is a weekend. IBKR's Flex API often does not surface\n"
                "      Friday's trades until Monday morning.\n"
                f"      Last business week: {fri_from} to {fri_to}\n"
                "      Re-run on Monday to confirm, or check portal manually."
            )
        else:
            print(
                "      Possible causes:\n"
                "        1. No trades executed in the date range above.\n"
                "        2. The Trades section fields are not configured — in IBKR portal\n"
                "           edit the query, expand 'Trades', and enable all required fields.\n"
                "        3. The query has an account or asset-class filter excluding trades.\n"
                "      See project documentation for the full field list."
            )
    else:
        print(
            "\nFAIL: <Trades> section missing from the XML entirely.\n"
            "      In IBKR portal edit your TradeHistory query:\n"
            "        Sections -> enable 'Trades' and add all required fields.\n"
            "        General  -> Period = 'Last 365 Calendar Days'\n"
            "      Save and re-run this script."
        )


if __name__ == "__main__":
    run()
