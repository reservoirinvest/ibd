Run the full project health check: linting, tests, and import smoke tests.

```bash
uv run ruff check .
uv run pytest tests/ -q
uv run python -c "from src.dashboard import settings, ib_client, state, risk, ohlc; print('dashboard ok')"
uv run python -c "from src.flex import fetch, parse, analyze; from src.backtest import score; print('flex/backtest ok')"
```

Report any failures. If ruff finds issues, fix them. If tests fail, investigate root cause before fixing.
