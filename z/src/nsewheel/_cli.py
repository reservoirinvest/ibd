"""Console entry points for the `nsew` command."""

from __future__ import annotations

import subprocess
import sys

from .paths import ROOT


def main() -> None:
    """`nsew` -> launch the Streamlit dashboard."""
    app = ROOT / "app.py"
    sys.exit(subprocess.call(["streamlit", "run", str(app), *sys.argv[1:]]))


def pipeline() -> None:
    """`python -m nsewheel._cli pipeline` -> run build -> derive -> execute (dry-run offline)."""
    from . import build, derive, execute

    build.run()
    derive.run()
    execute.run()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "pipeline":
        pipeline()
    else:
        main()
