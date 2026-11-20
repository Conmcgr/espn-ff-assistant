"""Prune private archive runs by age, protecting the newest runs."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from espn_ff_assistant.object_store import from_url, prune_runs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-url", required=True)
    parser.add_argument("--league-id", required=True)
    parser.add_argument("--keep-days", type=int, default=400)
    parser.add_argument("--keep-latest", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    runs = prune_runs(from_url(args.archive_url), args.league_id,
                      keep_days=args.keep_days, keep_latest=args.keep_latest,
                      now=datetime.now(UTC), dry_run=args.dry_run)
    print(json.dumps({"dry_run": args.dry_run, "runs": runs}))


if __name__ == "__main__":
    main()
