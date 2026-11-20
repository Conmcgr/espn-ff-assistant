"""Run one scheduled shadow tick; print one structured JSON log line.

Meant for Cloud Run Jobs (see docs/deployment.md) but runs anywhere. Scope
comes from the environment: ESPN_LEAGUE_ID, WORKER_USER_ID, WORKER_TEAM_ID and
optionally WORKER_SEASON. Nothing is sent and no ESPN action is taken.

Exit codes: 0 healthy tick (hold, scan, or overlap skip), 1 failed tick,
2 configuration error.

    uv run python scripts/scheduled_tick.py --dry-run
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime

from espn_ff_assistant.config import ConfigurationError, Settings
from espn_ff_assistant.database import load_dotenv
from espn_ff_assistant.tick import TickConfig, run_tick


def _parse_ts(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{value!r} must be timezone-aware")
    return stamp


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="Plan only: no worker run, no trigger or heartbeat writes")
    parser.add_argument("--now", type=_parse_ts, help="Override the tick time (replays)")
    args = parser.parse_args()
    load_dotenv()
    try:
        settings = Settings.from_env()
        config = TickConfig.from_env(settings)
    except ConfigurationError as exc:
        print(json.dumps({"severity": "ERROR", "status": "CONFIGURATION_ERROR",
                          "error": str(exc)}))
        return 2
    result = run_tick(settings, config, now=args.now, dry_run=args.dry_run)
    print(json.dumps({"severity": "INFO" if result.ok else "ERROR",
                      "message": f"tick {result.status}", **result.as_dict()}))
    if result.status == "CONFIGURATION_ERROR":
        return 2
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
