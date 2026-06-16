# z (NSE Wheel) — Developer Context for Claude

Standalone wheel-strategy trading system for **NSE via Zerodha Kite Connect**, ported from the
parent `ibd` (IBKR) system. Lives under `z/` for now; extraction-ready into its own repo.

## Architecture

```
app.py                       # Streamlit dashboard (OFFLINE-capable)
config/nse_config.yml        # strategy params (lot/settlement aware) + OFFLINE toggle
src/nsewheel/
  paths.py config.py settings.py   # paths, YAML+env config, Kite secrets (pydantic)
  broker/
    kite_client.py           # KiteClient: positions/orders/quotes/margins/place_order; OFFLINE mock toggle
    mock_data.py             # deterministic instruments/quotes/positions for offline + tests
  instruments.py             # lot sizes, settlement (stock=physical/index=cash), expiry, contract resolution
  greeks.py                  # Black-Scholes price/delta/vega + IV solver (Kite gives no greeks)
  ohlc.py                    # yfinance (.NS / ^NSEI) primary; kite historical fallback
  build.py                   # df_chains + df_unds (price, iv, hv, sdev, lot_size, settlement, margin_per_lot)
  classify.py                # parse_positions/orders + state machine (settlement-aware)
  derive.py                  # cover/sow/reap; LOT-MULTIPLE sizing; index = income-only
  execute.py                 # kite.place_order (NFO/NRML/LIMIT/DAY); OFFLINE dry-run; cushion gate
  history.py                 # Zerodha Console tradebook/P&L CSV -> normalized trade schema
  formatting.py util.py      # INR formatting, tick rounding, pickle I/O
  backtest/score.py          # broker-agnostic scoring (ported verbatim from ibd)
scripts/                     # update_instruments / update_trades / run_backtest
tests/                       # mock-based unit tests (-m 'not live')
data/master/                 # gitignored pickles + instruments_nfo.csv
```

## Key invariants & patterns

- **OFFLINE mode** (`config OFFLINE: true`): every broker call returns `mock_data` fixtures, so
  the full build→derive→execute pipeline + tests run with no network/credentials. Live code
  (`kiteconnect`, `yfinance`, `streamlit`) is **lazily imported** so offline never needs them.
- **Lot multiples**: every derived order has `qty = lots × lot_size`, `lots ≥ 1`. Guarded by
  `tests/test_derive_lots.py` — keep it green.
- **Settlement**: index underlyings (`INDEX_SYMBOLS` in config) are cash-settled → state
  `income_short`, income-only strangles, never assigned/covered. Stocks are physical/wheelable.
- **Greeks**: there are none in Kite quotes — always compute via `greeks.py`.
- **Schema parity with ibd**: `df_chains`/`df_unds`/`df_pf` column names mirror the IBKR system
  so `classify`/`derive`/`backtest` stay close to their originals. Normalized trade schema
  (`history.py`) matches ibd's `flex/parse.py` output, so `backtest/score.py` is reused verbatim.
- **Pickles**: always via `util.save_pickle` (atomic). Stored in `data/master/` (gitignored).

## Run

```bash
uv run python -m nsewheel._cli pipeline   # build -> derive -> execute (dry-run offline)
uv run nsew                               # dashboard
uv run pytest -q && uv run ruff check .
```

## Going live
See README "Going live". Set `OFFLINE: false`, supply a daily `KITE_ACCESS_TOKEN`, and address
the live-wiring TODOs (spot symbol mapping, real `order_margins`, expiry weekday).
