# Accounts & Ports (from snp_config.yml)

| Item | Value | Notes |
|---|---|---|
| Live port | 1300 | TWS or Gateway, US LLC |
| Paper port | 1301 | Paper account |
| `clientId` | 10 | reserved for batch jobs |
| Dashboard `clientId` | 11 | use this to avoid clashes |

Account env keys (names only, never values):
- `US_ACCOUNT` — US LLC account
- `SG_ACCOUNT` — Singapore account

The dashboard targets `US_ACCOUNT` on port 1300 by default.

## Risk knobs (from config)

- `MINCUSHION` = 0.20 — red alert below this.
- `MAX_DTE` = 50 — beyond this DTE we don't sow.
- `REAPRATIO` = 0.025 — reap candidate trigger.
- `MINREAPDTE` = 1 — never reap on/under this DTE.
