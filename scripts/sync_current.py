"""Fetch the current ESPN week (read-only) into data/raw and load it.

Usage:
    uv run python scripts/sync_current.py [--season 2026] [--weeks-ahead 1] [--no-load]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from espn_ff_assistant.config import ConfigurationError, Settings
from espn_ff_assistant.database import load_dotenv
from espn_ff_assistant.sync import sync_current


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--season", type=int)
    parser.add_argument("--weeks-ahead", type=int, default=1)
    parser.add_argument("--pool-limit", type=int, default=300)
    parser.add_argument("--raw-root", type=Path, default=Path("data/raw"))
    parser.add_argument("--no-load", action="store_true", help="Archive only; skip the database load")
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
    except ConfigurationError as error:
        print(f"Configuration error: {error}")
        return 2
    result = sync_current(
        settings, args.raw_root, season=args.season, weeks_ahead=args.weeks_ahead,
        pool_limit=args.pool_limit, load=not args.no_load,
    )
    print(json.dumps({
        "run_dir": str(result.run_dir),
        "season": result.season,
        "current_period": result.current_period,
        "periods": result.periods,
        "fetch_states": result.states,
        "counts": result.counts,
    }, indent=2))
    return 0 if set(result.states) <= {"present"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
