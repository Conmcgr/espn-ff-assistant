"""Parse mRoster payloads into roster snapshot and entry rows.

Pure functions; no I/O.
"""

from __future__ import annotations

from typing import Any


def players(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract player identity rows from an mRoster payload.

    Returns one dict per unique player seen in the payload.
    """
    seen: set[int] = set()
    rows = []
    for team in data.get("teams", []):
        for entry in (team.get("roster") or {}).get("entries", []):
            pid = entry.get("playerId")
            if pid is None or pid in seen:
                continue
            seen.add(pid)
            player = (entry.get("playerPoolEntry") or {}).get("player") or {}
            rows.append(
                {
                    "provider_player_id": int(pid),
                    "full_name": player.get("fullName"),
                    "first_name": player.get("firstName"),
                    "last_name": player.get("lastName"),
                    "default_position_id": player.get("defaultPositionId"),
                    "pro_team_id": player.get("proTeamId"),
                }
            )
    return rows


def roster_snapshots(season: int, period: int, data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract roster snapshot rows from one scoring-period mRoster payload."""
    rows = []
    for team in data.get("teams", []):
        tid = team.get("id")
        if tid is None:
            continue
        rows.append(
            {
                "season": season,
                "scoring_period": period,
                "provider_team_id": int(tid),
            }
        )
    return rows


def roster_entries(
    season: int, period: int, data: dict[str, Any]
) -> list[tuple[int, int, int, str | None, float | None]]:
    """Extract roster entry tuples (team_id, player_id, slot, acq_type, stat_total)."""
    rows = []
    for team in data.get("teams", []):
        tid = team.get("id")
        if tid is None:
            continue
        for entry in (team.get("roster") or {}).get("entries", []):
            pid = entry.get("playerId")
            if pid is None:
                continue
            stat_total = (entry.get("playerPoolEntry") or {}).get("appliedStatTotal")
            rows.append(
                (
                    int(tid),
                    int(pid),
                    entry.get("lineupSlotId"),
                    entry.get("acquisitionType"),
                    float(stat_total) if stat_total is not None else None,
                )
            )
    return rows
