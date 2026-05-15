"""Find which symbol causes get_option_chains to fail."""

from __future__ import annotations

from typing import List, Sequence

# pyrefly: ignore [missing-import]
from src.build import ROOT, delete_pkl_files, get_option_chains, get_qualified_symbols, pickle_me
from rich.progress import Progress


def _fetch_qualified_contracts(weeklies: bool = True) -> List:
    contracts = get_qualified_symbols(weeklies=weeklies, market="SNP", save=False)
    return list(contracts)


def _missing_symbols(subset: Sequence, df) -> set[str]:
    expected = {contract.symbol for contract in subset}

    if df is None or df.empty or "symbol" not in df:
        return expected

    observed = set(df["symbol"].unique())
    return expected - observed


def find_chain_failures(contracts: Sequence, batch_size: int | None = None):
    failures: list[dict[str, str]] = []
    suspects: list[list] = []

    contracts_list = list(contracts)
    if not contracts_list:
        return failures

    effective_batch = batch_size or max(1, len(contracts_list) // 10)

    with Progress() as pbar:
        task_id = pbar.add_task("Checking option chains", total=len(contracts_list))

        def drill_down(subset: Sequence):
            if not subset:
                return

            subset_list = list(subset)
            local_batch = min(effective_batch, len(subset_list))

            if subset_list:
                pbar.update(task_id, description=f"Checking {subset_list[0].symbol} ... {subset_list[-1].symbol}")

        try:
            df = get_option_chains(subset_list, market="SNP", batch_size=local_batch)
        except Exception as exc:  # pragma: no cover - diagnostic script
            if len(subset_list) == 1:
                failures.append({"symbol": subset_list[0].symbol, "error": str(exc)})
                pbar.advance(task_id, 1)
            else:
                mid = len(subset_list) // 2
                drill_down(subset_list[:mid])
                drill_down(subset_list[mid:])
            return

        missing = _missing_symbols(subset_list, df)
        processed = len(subset_list) - len(missing)
        if processed:
            pbar.advance(task_id, processed)

        if not missing:
            return

        if len(subset_list) == 1:
            failures.append(
                {"symbol": subset_list[0].symbol, "error": "No option chain data returned"}
            )
            return

        if len(missing) == len(subset_list):
            mid = len(subset_list) // 2
            drill_down(subset_list[:mid])
            drill_down(subset_list[mid:])
            return

        drill_down([contract for contract in subset_list if contract.symbol in missing])

        # Phase 1: sweep through batches quickly to identify suspect chunks
        for start in range(0, len(contracts_list), effective_batch):
            chunk = contracts_list[start : start + effective_batch]

            if chunk:
                pbar.update(task_id, description=f"Checking {chunk[0].symbol} ... {chunk[-1].symbol}")

        try:
            df = get_option_chains(chunk, market="SNP", batch_size=len(chunk))
        except Exception:
            suspects.append(chunk)
            continue

        missing = _missing_symbols(chunk, df)
        processed = len(chunk) - len(missing)
        if processed:
            pbar.advance(task_id, processed)

        if missing:
            suspects.append([contract for contract in chunk if contract.symbol in missing])

        # Phase 2: targeted drill-down on problematic chunks
        for suspect in suspects:
            drill_down(suspect)

        pbar.update(task_id, description="Complete")

    return failures


def fix_chain_fails(pickle_symbols: bool = False):
    delete_pkl_files(["df_chains.pkl"])
    contracts = _fetch_qualified_contracts(weeklies=True)
    print(f"Total qualified contracts: {len(contracts)}")

    failures = find_chain_failures(contracts)

    if not failures:
        print("All symbols returned option chain data successfully.")
    else:
        print("\nSymbols with option chain failures:")
        for entry in failures:
            print(f" - {entry['symbol']}: {entry['error']}")

    if pickle_symbols:
        failure_symbols = {entry.get("symbol") for entry in failures if entry.get("symbol")}
        filtered_contracts = [c for c in contracts if c.symbol not in failure_symbols]
        symbols_path = ROOT / "data" / "symbols.pkl"
        pickle_me(filtered_contracts, file_path=symbols_path)


if __name__ == "__main__":
    fix_chain_fails(pickle_symbols=True)
