"""Inspect any pickle file from data/ or data/master/ in the terminal.

Usage:
    uv run python scripts/peek.py                 # interactive file picker
    uv run python scripts/peek.py data/symbols.pkl
    uv run python scripts/peek.py --rows 50       # show 50 rows (default 30)
    uv run python scripts/peek.py --tail          # show tail instead of head
    uv run python scripts/peek.py --all           # show all rows
    uv run python scripts/peek.py --info          # schema / dtypes only, no data
"""
from __future__ import annotations

import argparse
import pickle
import sys
from pathlib import Path

import pandas as pd
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import print as rprint

ROOT = Path(__file__).resolve().parent.parent
DATA_DIRS = [ROOT / "data", ROOT / "data" / "master"]

console = Console()


# ── file picker ──────────────────────────────────────────────────────────────

def _list_pickles() -> list[Path]:
    files: list[Path] = []
    for d in DATA_DIRS:
        files.extend(sorted(d.glob("*.pkl")))
    return files


def _pick_file() -> Path:
    files = _list_pickles()
    if not files:
        console.print("[red]No .pkl files found in data/ or data/master/[/red]")
        sys.exit(1)

    t = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan")
    t.add_column("#", style="dim", width=4, justify="right")
    t.add_column("File", style="white")
    t.add_column("Size", style="dim", justify="right")

    for i, f in enumerate(files, 1):
        kb = f.stat().st_size / 1024
        size = f"{kb:,.0f} KB" if kb < 1024 else f"{kb / 1024:,.1f} MB"
        label = f"[bold]{f.name}[/bold]  [dim]{f.parent.relative_to(ROOT)}[/dim]"
        t.add_row(str(i), label, size)

    console.print()
    console.print(t)

    while True:
        raw = console.input("[cyan]Pick a file number (or path): [/cyan]").strip()
        if not raw:
            sys.exit(0)
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(files):
                return files[idx]
            console.print(f"[red]Enter 1–{len(files)}[/red]")
        else:
            p = Path(raw)
            if not p.is_absolute():
                p = ROOT / p
            if p.exists():
                return p
            console.print(f"[red]Not found: {raw}[/red]")


# ── rendering ────────────────────────────────────────────────────────────────

def _render_dataframe(df: pd.DataFrame, *, rows: int, tail: bool, show_all: bool, info_only: bool) -> None:
    shape_str = f"{len(df):,} rows × {len(df.columns)} cols"
    console.print(Panel(f"[bold green]DataFrame[/bold green]  {shape_str}", expand=False))

    # schema
    schema = Table(box=box.SIMPLE, show_header=True, header_style="bold magenta")
    schema.add_column("Column", style="cyan")
    schema.add_column("dtype", style="yellow")
    schema.add_column("non-null", justify="right", style="dim")
    schema.add_column("sample", style="white")

    for col in df.columns:
        non_null = df[col].notna().sum()
        sample_val = ""
        notnull = df[col].dropna()
        if not notnull.empty:
            sample_val = str(notnull.iloc[0])
            if len(sample_val) > 40:
                sample_val = sample_val[:37] + "…"
        schema.add_row(col, str(df[col].dtype), f"{non_null:,}", sample_val)

    console.print(schema)

    if info_only:
        return

    # data slice
    if show_all:
        view = df
        label = "all rows"
    elif tail:
        view = df.tail(rows)
        label = f"last {min(rows, len(df))} rows"
    else:
        view = df.head(rows)
        label = f"first {min(rows, len(df))} rows"

    if len(df) > rows and not show_all:
        omitted = len(df) - rows
        console.print(f"[dim]Showing {label}  ·  {omitted:,} rows omitted  (--all to see everything)[/dim]")
    else:
        console.print(f"[dim]Showing {label}[/dim]")

    t = Table(box=box.MARKDOWN, show_header=True, header_style="bold cyan")

    # reserve 6 chars for idx col + separators; split the rest evenly
    idx_w = max(4, len(str(view.index[-1])) + 1) if len(view) else 4
    available = max(console.width - idx_w - (len(view.columns) + 2) * 3, 40)
    col_width = max(10, min(28, available // max(len(view.columns), 1)))

    t.add_column("idx", style="dim", justify="right", no_wrap=True, width=idx_w)
    for col in view.columns:
        justify = "right" if pd.api.types.is_numeric_dtype(view[col]) else "left"
        t.add_column(str(col), no_wrap=True, justify=justify, width=col_width)

    for i, (idx, row) in enumerate(view.iterrows()):
        style = "dim" if i % 2 else ""
        cells = [str(idx)]
        for val in row:
            try:
                is_na = pd.isna(val)
            except (TypeError, ValueError):
                is_na = False
            if is_na:
                s = ""
            elif isinstance(val, float):
                s = f"{val:,.6g}"
            else:
                s = str(val)
            if len(s) > col_width:
                s = s[: col_width - 1] + "…"
            cells.append(s)
        t.add_row(*cells, style=style)

    console.print(t)


def _render_dict(d: dict, *, depth: int = 0) -> None:
    console.print(Panel(f"[bold green]dict[/bold green]  {len(d)} keys", expand=False))
    t = Table(box=box.SIMPLE, show_header=True, header_style="bold magenta")
    t.add_column("Key", style="cyan")
    t.add_column("Type", style="yellow")
    t.add_column("Value", style="white")

    for k, v in d.items():
        type_str = type(v).__name__
        if isinstance(v, pd.DataFrame):
            val_str = f"DataFrame {v.shape}"
        elif isinstance(v, dict):
            val_str = f"dict({len(v)} keys)"
        elif isinstance(v, (list, tuple)):
            val_str = f"{type_str}[{len(v)}]  first={v[0] if v else ''!r}"
        else:
            val_str = repr(v)
        if len(val_str) > 80:
            val_str = val_str[:77] + "…"
        t.add_row(str(k), type_str, val_str)

    console.print(t)


def _render_list(lst: list) -> None:
    console.print(Panel(f"[bold green]list[/bold green]  {len(lst):,} items", expand=False))
    for i, item in enumerate(lst[:20]):
        rprint(f"  [dim]{i:4d}[/dim]  {item!r}")
    if len(lst) > 20:
        console.print(f"  [dim]… {len(lst) - 20:,} more items[/dim]")


def _render_other(obj: object) -> None:
    console.print(Panel(f"[bold green]{type(obj).__name__}[/bold green]", expand=False))
    console.print(repr(obj))


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect a pickle file.")
    parser.add_argument("file", nargs="?", help="Path to .pkl file (omit for picker)")
    parser.add_argument("--rows", type=int, default=30, metavar="N", help="Rows to display (default 30)")
    parser.add_argument("--tail", action="store_true", help="Show tail instead of head")
    parser.add_argument("--all", dest="show_all", action="store_true", help="Show all rows")
    parser.add_argument("--info", action="store_true", help="Schema / dtypes only")
    args = parser.parse_args()

    path = Path(args.file) if args.file else _pick_file()
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        console.print(f"[red]File not found: {path}[/red]")
        sys.exit(1)

    console.print(f"\n[bold]Loading:[/bold] [cyan]{path.relative_to(ROOT)}[/cyan]\n")

    with open(path, "rb") as fh:
        obj = pickle.load(fh)

    if isinstance(obj, pd.DataFrame):
        _render_dataframe(obj, rows=args.rows, tail=args.tail, show_all=args.show_all, info_only=args.info)
    elif isinstance(obj, dict):
        _render_dict(obj)
    elif isinstance(obj, list):
        _render_list(obj)
    else:
        _render_other(obj)


if __name__ == "__main__":
    main()
