# IBKR Pacing & Connection Rules

## Hard limits to respect

- ≤ 50 simultaneous market-data lines (default account; can be raised).
- 100 messages / second outbound on the API socket.
- Reconnect: exponential backoff starting 2s, max 60s, jitter ±20%.

## Streaming subscriptions

- One `IB()` per process. One `clientId` per process.
- `reqMktData(contract, genericTickList="106")` for held contracts → gives modelOptionComputation (greeks).
- Cancel via `cancelMktData(contract)` when position closes.
- Snapshot mode (`snapshot=True`) for one-off prices; otherwise stream.

## Errors to handle gracefully

| Code | Meaning | Action |
|---|---|---|
| 162 | historical data farm down | log, retry later |
| 165 | historical data farm OK | informational |
| 200 | no security def | drop subscription |
| 322 | duplicate ticker id | regenerate |
| 354 | not subscribed | check market-data permissions |
| 1100 | connectivity lost | wait for 1102 (restored) |
| 2104/2106 | farm OK | informational |
| 2110 | connectivity restored | resume |

## Don't

- Don't request all chains every tick — use `reqContractDetails` once per session, cache.
- Don't open multiple TWS connections from one IP unnecessarily.
- Don't subscribe to underlyings you don't actually display.
