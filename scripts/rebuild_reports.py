"""Rebuild ignored reports from an existing raw scan run."""

from __future__ import annotations

import argparse
from pathlib import Path

from espn_ff_assistant.coverage import build_coverage, write_coverage
from espn_ff_assistant.transactions import normalize_run, write_analysis


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--raw-root", type=Path, default=Path("data/raw"))
    parser.add_argument("--reports-root", type=Path, default=Path("reports"))
    parser.add_argument("--derived-root", type=Path, default=Path("data/derived"))
    args = parser.parse_args()

    run_dir = args.raw_root / args.run_id
    write_coverage(build_coverage(run_dir / "manifest.jsonl"), args.reports_root / "coverage.csv")
    write_analysis(normalize_run(run_dir), args.derived_root / args.run_id / "transactions")
    print(f"Rebuilt coverage and transaction reports from {args.run_id}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

