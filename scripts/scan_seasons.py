"""Archive ESPN league data and emit a season coverage matrix."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from espn_ff_assistant.archive import RawArchive
from espn_ff_assistant.config import ConfigurationError, Settings
from espn_ff_assistant.coverage import build_coverage, write_coverage
from espn_ff_assistant.espn_client import ESPNClient

BASE_VIEWS = ("mStatus", "mSettings", "mTeam", "mDraftDetail", "mSchedule", "mStandings")


def fetch_and_save(client, archive, season, view, **kwargs):
    cached = archive.load(season, view, kwargs.get("scoring_period"), kwargs.get("archive_key"))
    if cached is not None:
        return cached
    result = client.fetch(season, view, **kwargs)
    archive.save(result)
    return result.payload or {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-season", type=int)
    parser.add_argument("--end-season", type=int)
    parser.add_argument("--season", type=int, help="scan one season only")
    parser.add_argument("--resume", help="existing run ID under data/raw")
    parser.add_argument("--request-delay", type=float, default=0.25)
    parser.add_argument("--raw-root", type=Path, default=Path("data/raw"))
    parser.add_argument("--coverage", type=Path, default=Path("reports/coverage.csv"))
    return parser.parse_args()


def scan_season(client: ESPNClient, archive: RawArchive, season: int) -> dict:
    results: list[dict] = []
    for view in BASE_VIEWS:
        results.append(fetch_and_save(client, archive, season, view))
    status = results[0]
    status_data = status.get("status", {})
    first = int(status_data.get("firstScoringPeriod", 1))
    latest = int(
        status_data.get("latestScoringPeriod", status_data.get("finalScoringPeriod", first))
    )
    final = int(status_data.get("finalScoringPeriod", latest))
    end_period = min(final, latest) if season == client.settings.last_season else final
    if end_period < first:
        return {"season": season, "status": "no scoring periods"}
    for period in range(first, end_period + 1):
        for view in ("mRoster", "mBoxscore", "mTransactions2"):
            fetch_and_save(client, archive, season, view, scoring_period=period)
    return {"season": season, "first_period": first, "last_period": end_period}


def main() -> int:
    args = parse_args()
    try:
        settings = Settings.from_env()
    except ConfigurationError as error:
        print(f"Configuration error: {error}")
        return 2
    start = args.start_season or settings.first_season
    end = args.end_season or settings.last_season
    seasons = [args.season] if args.season else list(range(start, end + 1))
    run_id = args.resume or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    archive = RawArchive(args.raw_root, run_id)
    client = ESPNClient(settings, delay=args.request_delay)
    for season in seasons:
        print(f"Scanning {season}...")
        print(scan_season(client, archive, season))
    rows = build_coverage(archive.manifest)
    write_coverage(rows, args.coverage)
    print(f"Saved raw responses under {archive.run_dir}; coverage at {args.coverage}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
