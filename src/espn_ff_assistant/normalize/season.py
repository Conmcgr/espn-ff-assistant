"""Parse season-level payloads (status, settings, teams, draft) into row dicts.

All functions are pure: they accept raw ESPN payload dicts and return lists of
row dicts suitable for upsert. No I/O occurs here.
"""

from __future__ import annotations

from typing import Any


def status_row(season: int, data: dict[str, Any]) -> dict[str, Any]:
    """Extract scoring-period bounds from an mStatus payload."""
    status = data.get("status", {})
    return {
        "season": season,
        "first_scoring_period": status.get("firstScoringPeriod"),
        "final_scoring_period": status.get("finalScoringPeriod"),
        "latest_scoring_period": status.get("latestScoringPeriod"),
    }


def members(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract manager rows from an mTeam payload."""
    rows = []
    for member in data.get("members", []):
        mid = str(member.get("id") or "")
        if not mid:
            continue
        rows.append(
            {
                "provider_member_id": mid,
                "display_name": member.get("displayName"),
            }
        )
    return rows


def teams(season: int, data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract team rows and owner assignments from an mTeam payload.

    Returns a list of dicts, each with:
      provider_team_id, name, primary_owner_member_id, owner_member_ids
    """
    rows = []
    for team in data.get("teams", []):
        tid = team.get("id")
        if tid is None:
            continue
        owners = [str(o) for o in (team.get("owners") or []) if o]
        primary = str(team.get("primaryOwner") or "")
        if primary and primary not in owners:
            owners.insert(0, primary)
        rows.append(
            {
                "season": season,
                "provider_team_id": int(tid),
                "name": team.get("name"),
                "primary_owner_member_id": primary or (owners[0] if owners else None),
                "owner_member_ids": owners,
                # Convenience copy of season record for validation cross-checks.
                "wins": (team.get("record") or {}).get("overall", {}).get("wins"),
                "losses": (team.get("record") or {}).get("overall", {}).get("losses"),
                "ties": (team.get("record") or {}).get("overall", {}).get("ties"),
                "points_for": (team.get("record") or {}).get("overall", {}).get("pointsFor"),
                "points_against": (team.get("record") or {}).get("overall", {}).get("pointsAgainst"),
            }
        )
    return rows


def draft_picks(season: int, data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract draft-pick rows from an mDraftDetail payload."""
    rows = []
    for pick in data.get("draftDetail", {}).get("picks", []):
        rows.append(
            {
                "season": season,
                "overall_pick": pick.get("overallPickNumber"),
                "round": pick.get("roundId"),
                "round_pick": pick.get("roundPickNumber"),
                "provider_team_id": pick.get("teamId"),
                "provider_player_id": pick.get("playerId"),
                "bid_amount": pick.get("bidAmount"),
            }
        )
    return rows


def scoring_periods(season: int, status_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Enumerate scoring periods and their completion state."""
    status = status_data.get("status", {})
    first = status.get("firstScoringPeriod")
    final = status.get("finalScoringPeriod")
    latest = status.get("latestScoringPeriod") or 0
    if not isinstance(first, int) or not isinstance(final, int):
        return []
    rows = []
    for period in range(first, final + 1):
        rows.append(
            {
                "season": season,
                "scoring_period": period,
                "state": "completed" if period <= latest else "scheduled",
            }
        )
    return rows
