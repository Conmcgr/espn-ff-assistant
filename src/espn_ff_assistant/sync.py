"""Read-only current-week sync: fetch ESPN views into a private raw run, then load.

The run layout matches the historical scanner, so the same loader handles both.
Every response (including failures) is recorded in the run manifest with its
transport state; credentials are never written.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .archive import RawArchive
from .config import Settings
from .espn_client import ESPNClient, FetchResult
from .loader import load_run
from .repository import LIVE_RUN_SUFFIX

SEASON_VIEWS = ("mStatus", "mSettings", "mTeam", "mStandings")
PERIOD_VIEWS = ("mRoster", "mTransactions2")
POOL_SLOT_IDS = (0, 2, 4, 6, 16, 17, 23)


def pool_filter(limit: int) -> dict[str, Any]:
    """X-Fantasy-Filter for available (free agent + waivers) players, most-owned first."""
    return {
        "players": {
            "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
            "filterSlotIds": {"value": list(POOL_SLOT_IDS)},
            "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
            "limit": limit,
        }
    }


@dataclass
class SyncResult:
    run_dir: Path
    season: int
    current_period: int
    periods: list[int]
    states: dict[str, int]
    counts: dict[str, int]


def sync_periods(status_payload: dict[str, Any], weeks_ahead: int) -> tuple[int, list[int]]:
    """Current scoring period and the periods to fetch (current plus look-ahead)."""
    status = status_payload.get("status") or {}
    first = int(status.get("firstScoringPeriod") or 1)
    final = int(status.get("finalScoringPeriod") or first)
    current = int(status.get("latestScoringPeriod") or first)
    current = max(first, min(current, final))
    return current, [p for p in range(current, current + weeks_ahead + 1) if p <= final]


def sync_current(
    settings: Settings,
    raw_root: Path,
    *,
    season: int | None = None,
    weeks_ahead: int = 1,
    pool_limit: int = 300,
    load: bool = True,
) -> SyncResult:
    season = season or settings.last_season
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + LIVE_RUN_SUFFIX
    archive = RawArchive(raw_root, run_id)
    client = ESPNClient(settings)
    results: list[FetchResult] = []

    def fetch(view: str, **kwargs: Any) -> FetchResult:
        result = client.fetch(season, view, **kwargs)
        archive.save(result)
        results.append(result)
        return result

    status = fetch("mStatus")
    for view in SEASON_VIEWS[1:]:
        fetch(view)
    current, periods = sync_periods(status.payload or {}, weeks_ahead)

    fetch("mBoxscore", scoring_period=current)
    for period in periods:
        for view in PERIOD_VIEWS:
            fetch(view, scoring_period=period)
        fetch("kona_player_info", scoring_period=period, fantasy_filter=pool_filter(pool_limit))
    fetch("proTeamSchedules_wl", game_level=True)

    states = dict(Counter(r.state for r in results))
    counts = load_run(archive.run_dir) if load else {}
    return SyncResult(archive.run_dir, season, current, periods, states, counts)
