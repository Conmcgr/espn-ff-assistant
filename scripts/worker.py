"""Run one scoped shadow scan; print a JSON result without sending alerts."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from uuid import UUID

from espn_ff_assistant.config import ConfigurationError, Settings
from espn_ff_assistant.worker import WorkerResult, WorkerScope, run_once


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--league", type=positive_int, required=True, help="ESPN league ID")
    parser.add_argument("--user", type=UUID, required=True, help="Internal users.id UUID")
    parser.add_argument("--team", type=positive_int, required=True, help="Expected ESPN team ID")
    parser.add_argument("--season", type=positive_int, required=True)
    parser.add_argument("--week", type=positive_int, required=True, help="Current ESPN scoring period")
    args = parser.parse_args()
    scope = WorkerScope(args.league, str(args.user), args.team, args.season, args.week)
    try:
        settings = Settings.from_env()
    except ConfigurationError as exc:
        result = WorkerResult(scope, "CONFIGURATION_ERROR", error=str(exc))
    else:
        result = run_once(settings, scope)
    print(json.dumps(asdict(result), sort_keys=True))
    if result.status in {"ACTIONABLE", "NO_ACTION"}:
        return 0
    return 2 if result.status in {"CONFIGURATION_ERROR", "SCOPE_REJECTED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
