---
name: cli-debug
description: Use when adding or modifying CLI debug flags in any batch script. Covers the setup_logging pattern, --debug argparse wiring, and log file locations. Trigger on any mention of "debug", "terminal spam", "log level", "verbose", or "--debug flag".
---

# CLI Debug Flag — Skill

## When to use
Any batch script that currently dumps debug output to the terminal, or any new script that should follow the standard debug convention.

## The rule
- Terminal: INFO+ by default. DEBUG only when `--debug` is passed.
- Log file: always DEBUG. Never suppress log file output.
- Library modules (imported by others): never configure loguru at module level. Only entry-point scripts configure sinks.

## Standard pattern — script with `__main__` guard

```python
import argparse
from src.log_utils import setup_logging

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--debug", action="store_true", help="Show DEBUG output in terminal")
    args = p.parse_args()
    setup_logging("my_script", debug=args.debug)
    main()  # or inline code
```

## Standard pattern — script without `__main__` guard (always runs top-to-bottom)

Add immediately after all imports:

```python
import argparse as _ap
from src.log_utils import setup_logging as _setup_logging

_p = _ap.ArgumentParser(add_help=False)
_p.add_argument("--debug", action="store_true")
_setup_logging("my_script", debug=_p.parse_known_args()[0].debug)
del _ap, _p, _setup_logging
```

Use `parse_known_args` (not `parse_args`) so unknown args don't error.
Use `add_help=False` to avoid argparse hijacking `-h` from the script.
Delete the temp names after use to avoid polluting the module namespace.

## Log file locations
`setup_logging` writes to `log/<log_name>.log` relative to project root (via `pyprojroot.here()`). The `log/` dir is created automatically.

| Script | Log file |
|---|---|
| `src/build.py` | `log/build.log` |
| `src/classify.py` | `log/classify.log` |
| `src/derive.py` | `log/derive.log` |
| `src/execute.py` | `log/execute.log` |
| `src/fetch_ohlc.py` / `ohlc.py` | `log/ohlc.log` |

## ib_async stdlib logging (separate from loguru)
ib_async uses Python's stdlib logging, not loguru. To redirect its noise to a file:

```python
util.logToFile(ROOT / "log" / "derive.log", level=40)  # ERROR+ only to file
```

Call this AFTER `setup_logging(...)`, after imports.

## Running with --debug
```bash
uv run python src/derive.py --debug
uv run python src/build.py --debug
```

Subprocess button handlers in `app.py` do NOT pass `--debug` — production runs stay quiet.

## What NOT to do
- Do not call `logger.remove()` or `logger.add()` at module level in a library file.
- Do not use `logger.add(lambda msg: print(msg, end=''), ...)` — use a proper sink.
- Do not add a debug flag to `app.py` (Streamlit) — the dashboard uses its own logging via `IBClient._log_sink_added`.
