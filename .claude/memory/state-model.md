# Portfolio / Order / Symbol State Model

Authoritative source: `README.md`. This is a quick lookup.

## DataFrames

`pf` (portfolio), `df_openords` (open orders), `df_unds` (underlyings).
Common fields: `symbol, secType, right, action, position`.

## Portfolio states (from `pf`)

- **zen** — stock + covering + protecting options.
- **exposed** — stock only (no options).
- **unprotected** — stock + only covering option.
- **uncovered** — stock + only protecting option.
- **straddled** — matching call+put, no underlying stock.
- **covering** — short option with underlying stock.
- **protecting** — long option with underlying stock.
- **sowed** — short option, no underlying stock.
- **orphaned** — long option, no underlying stock.

## Order states (from `df_openords`)

- **covering** — option SELL with underlying stock.
- **protecting** — option BUY with underlying stock.
- **sowing** — option SELL, no stock.
- **reaping** — option BUY against existing same-side option.
- **straddling** — two BUYs same symbol, not in pf.
- **de-orphaning** — option SELL, no stock or matching option.

## Symbol states (composite, in `df_unds`)

- **zen**, **unreaped**, **exposed**, **uncovered**, **unprotected**, **virgin**, **orphaned**, **unknown**.

## Dashboard rules

- Color: zen=green, virgin=blue, exposed/orphaned/unreaped=red, others=amber.
- Sort: red first, then amber, then green; ties by `|notional|` desc.
- Counts always rendered as `{state: count}`; notional sums in USD.
