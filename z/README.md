# z — NSE Wheel Strategy (Zerodha Kite Connect)

A wheel-strategy options trading system for **NSE**, modeled on the IBKR system in the parent
`ibd` repo but built on **Zerodha Kite Connect**. It generates and (optionally) places the
sow → assign → cover → reap legs of the options wheel, sized in **whole NSE lots**.

> This project is a self-contained tree (its own `pyproject.toml`/`src`). It currently lives
> under `z/` inside the `ibd` repo and is structured to be extracted into its own GitHub repo
> later (`git subtree split --prefix z` or a simple move).

## Why NSE is different from IBKR

| Concern | NSE / Zerodha behaviour | Where it's handled |
|---|---|---|
| **Lot sizes** | Every F&O contract trades in lots; order qty must be `n × lot_size` | `instruments.py`, `derive.py` (`lots = floor(budget / margin_per_lot)`) |
| **Settlement** | Single-stock options are **physically settled** (assignment delivers shares → wheelable); index options are **cash-settled** (income-only) | `instruments.settlement_of`, `classify.py` (`income_short` state) |
| **Greeks** | Kite quotes carry **no greeks** — compute them | `greeks.py` (Black-Scholes + IV solve) |
| **Margins** | SPAN + Exposure | `kite_client.order_margins` (offline = `notional × SPAN_MARGIN_PCT`) |
| **Trade history** | Kite API returns only the current day; full history from **Console** CSV exports | `history.py`, `scripts/update_trades.py` |
| **Expiry** | Stock F&O = monthly (last-Thursday cycle); indices add weeklies | `config STOCK_EXPIRY_WEEKDAY`, `instruments._has_weekly` |

## Pipeline

```
update_instruments  ->  build  ->  derive  ->  execute
 (lot sizes/chains)   (chains+   (cover/sow/  (kite.place_order,
                      underlyings) reap, lots)   dry-run offline)
```

## Quick start (OFFLINE — no credentials needed)

```bash
cd z
uv venv && uv pip install -e ".[dev]"

# end-to-end against mock data (dry-run; places nothing)
uv run python -m nsewheel._cli pipeline

# dashboard
uv run nsew          # or: uv run streamlit run app.py
```

`config/nse_config.yml` ships with `OFFLINE: true`, so everything runs against deterministic
mock instruments/positions/quotes.

## Going live (do this locally, not in CI)

1. Create a Kite Connect app at https://kite.trade and copy `.env.example` → `.env` with your
   `KITE_API_KEY` / `KITE_API_SECRET` / `KITE_USER_ID`.
2. Generate a **daily access token** via the Kite login flow and set `KITE_ACCESS_TOKEN`
   (tokens expire each day):
   ```python
   from kiteconnect import KiteConnect
   kite = KiteConnect(api_key="...")
   print(kite.login_url())                       # open, log in, copy request_token
   data = kite.generate_session("REQUEST_TOKEN", api_secret="...")
   print(data["access_token"])
   ```
3. Set `OFFLINE: false` in `config/nse_config.yml`.
4. `uv run python scripts/update_instruments.py` then `... -m nsewheel._cli pipeline`.

### TODO before trading live
- Verify `KiteClient.spots()` underlying-symbol mapping for indices (`NSE:NIFTY 50` etc.).
- Replace the offline SPAN approximation with real `kite.order_margins` / `basket_order_margins`.
- Confirm monthly expiry weekday against current NSE circulars (`STOCK_EXPIRY_WEEKDAY`).
- Review `derive.py` strike/price multipliers against a backtest before placing real orders.

## Tests

```bash
uv run pytest -q            # mock-based; lot-multiples, settlement, classification, greeks
uv run ruff check .
```
