Start the IB Monitor dashboard.

```bash
uv run ibd
```

The dashboard connects to IB Gateway/TWS on the port in `config/snp_config.yml` (default 1300). Make sure Gateway is running with API enabled and `127.0.0.1` in the trusted IP list before launching.

After starting, open the browser URL shown in the terminal (typically http://localhost:8501).

Note: changes to `src/` modules require a full terminal restart. Changes to `app.py` only require a browser Rerun.
