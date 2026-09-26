"""Normalize and summarize mTransactions2 records from an archived scan."""

from __future__ import annotations

import argparse
from pathlib import Path

from espn_ff_assistant.transactions import normalize_run, write_analysis


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--raw-root", type=Path, default=Path("data/raw"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    run_dir = args.raw_root / args.run_id
    output = args.output or Path("data/derived") / args.run_id / "transactions"
    rows = normalize_run(run_dir)
    write_analysis(rows, output)
    print(f"Normalized {len(rows)} unique transactions into {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

