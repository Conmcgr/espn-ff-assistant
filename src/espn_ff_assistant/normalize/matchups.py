"""Parse matchup data from mBoxscore payloads.

mBoxscore per-week responses include the full season's schedule array.
We collect entries across all weeks and deduplicate by ESPN matchup ID,
keeping the version from the latest scoring period (most scores are final).

Pure functions; no I/O.
"""

from __future__ import annotations

from typing import Any


def _playoff_periods(settings_data: dict[str, Any]) -> set[int]:
    """Return the set of matchup periods that are playoff weeks."""
    sched = (settings_data.get("settings") or {}).get("scheduleSettings") or {}
    regular_count = sched.get("matchupPeriodCount")
    # matchupPeriods maps period ID -> list of scoring periods
    # periods after the regular-season count are playoffs
    playoff_set: set[int] = set()
    mp = sched.get("matchupPeriods") or {}
    for period_id_str in mp:
        period_id = int(period_id_str)
        if isinstance(regular_count, int) and period_id > regular_count:
            playoff_set.add(period_id)
    return playoff_set


def from_boxscore_payloads(
    season: int,
    # list of (scoring_period, boxscore_payload, settings_payload)
    period_payloads: list[tuple[int, dict[str, Any], dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Deduplicate and normalise matchup rows across all scoring periods.

    Returns one row per unique ESPN matchup ID.
    """
    # Collect per-matchup-id: the latest version wins
    best: dict[int, tuple[int, dict[str, Any], dict[str, Any]]] = {}
    playoff_periods: set[int] = set()

    for scoring_period, boxscore, settings in period_payloads:
        playoff_periods = _playoff_periods(settings)
        for entry in boxscore.get("schedule") or []:
            mid = entry.get("id")
            if mid is None:
                continue
            prev = best.get(int(mid))
            if prev is None or scoring_period > prev[0]:
                best[int(mid)] = (scoring_period, entry, settings)

    rows = []
    for matchup_id, (_scoring_period, entry, settings) in best.items():
        playoff_periods = _playoff_periods(settings)
        matchup_period = entry.get("matchupPeriodId")
        home = entry.get("home") or {}
        away = entry.get("away")  # None for bye weeks

        home_score = home.get("totalPoints")
        away_score = (away or {}).get("totalPoints") if away else None
        is_bye = away is None

        # Determine winner only when both scores are final (both non-None).
        winner: str | None = None
        if not is_bye and home_score is not None and away_score is not None:
            if home_score > away_score:
                winner = "home"
            elif away_score > home_score:
                winner = "away"
            else:
                winner = "tie"

        period_type = (
            "postseason"
            if isinstance(matchup_period, int) and matchup_period in playoff_periods
            else "regular"
        )

        rows.append(
            {
                "season": season,
                "provider_matchup_id": matchup_id,
                "matchup_period": matchup_period,
                "period_type": period_type,
                "home_provider_team_id": home.get("teamId"),
                "away_provider_team_id": (away or {}).get("teamId") if not is_bye else None,
                "home_score": home_score,
                "away_score": away_score,
                "winner": winner,
                "is_bye": is_bye,
            }
        )

    return rows
