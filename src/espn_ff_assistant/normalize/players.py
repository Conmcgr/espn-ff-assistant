"""Parse player stats, status, availability, and pro schedules.

Pure functions; no I/O. Handles both roster payloads (`mRoster`: teams → roster
entries) and player-pool payloads (`kona_player_info`: players[]).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from espn_ff_assistant.normalize.common import millis

STAT_SOURCES = {0: "actual", 1: "projected"}
# 2 is ESPN's current full-season projection (actual to date + rest of season).
STAT_SPLITS = {0: "season", 1: "week", 2: "season_current"}
POOL_AVAILABILITY = {"FREEAGENT": "free_agent", "WAIVERS": "waivers", "ONTEAM": "rostered"}


@dataclass(frozen=True)
class _PoolRecord:
    player: dict[str, Any]
    pool_entry: dict[str, Any]
    availability: str
    on_team_id: int | None


def _records(data: dict[str, Any]) -> Iterator[_PoolRecord]:
    for team in data.get("teams") or []:
        team_id = team.get("id")
        for entry in (team.get("roster") or {}).get("entries") or []:
            pool_entry = entry.get("playerPoolEntry") or {}
            player = pool_entry.get("player") or {}
            if player.get("id") is None and entry.get("playerId") is not None:
                player = {**player, "id": entry.get("playerId")}
            if player.get("id") is not None:
                yield _PoolRecord(player, pool_entry, "rostered", team_id)
    for pool_entry in data.get("players") or []:
        player = pool_entry.get("player") or {}
        if player.get("id") is None and pool_entry.get("id") is not None:
            player = {**player, "id": pool_entry.get("id")}
        if player.get("id") is None:
            continue
        availability = POOL_AVAILABILITY.get(pool_entry.get("status"), "unknown")
        on_team = pool_entry.get("onTeamId")
        yield _PoolRecord(player, pool_entry, availability, on_team if on_team else None)


def identities(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Unique player identity rows (same shape as normalize.rosters.players)."""
    rows: dict[int, dict[str, Any]] = {}
    for rec in _records(data):
        pid = int(rec.player["id"])
        rows.setdefault(
            pid,
            {
                "provider_player_id": pid,
                "full_name": rec.player.get("fullName"),
                "first_name": rec.player.get("firstName"),
                "last_name": rec.player.get("lastName"),
                "default_position_id": rec.player.get("defaultPositionId"),
                "pro_team_id": rec.player.get("proTeamId"),
            },
        )
    return list(rows.values())


def week_stats(season: int, snapshot_period: int, data: dict[str, Any]) -> list[dict[str, Any]]:
    """Applied fantasy totals by (player, stat period, source, split) seen in one payload."""
    rows: dict[tuple[int, int, str, str], dict[str, Any]] = {}
    for rec in _records(data):
        pid = int(rec.player["id"])
        for stat in rec.player.get("stats") or []:
            if stat.get("seasonId") != season:
                continue
            source = STAT_SOURCES.get(stat.get("statSourceId"))
            split = STAT_SPLITS.get(stat.get("statSplitTypeId"))
            period = stat.get("scoringPeriodId")
            if source is None or split is None or not isinstance(period, int):
                continue
            total = stat.get("appliedTotal")
            rows[(pid, period, source, split)] = {
                "provider_player_id": pid,
                "snapshot_period": snapshot_period,
                "scoring_period": period,
                "stat_source": source,
                "stat_split": split,
                "applied_total": float(total) if isinstance(total, (int, float)) else None,
            }
    return list(rows.values())


def status_rows(season: int, snapshot_period: int, data: dict[str, Any]) -> list[dict[str, Any]]:
    """Injury, eligibility, ownership, and availability per player in one payload."""
    rows: dict[int, dict[str, Any]] = {}
    for rec in _records(data):
        pid = int(rec.player["id"])
        ownership = rec.player.get("ownership") or {}
        slots = rec.player.get("eligibleSlots")
        rows[pid] = {
            "provider_player_id": pid,
            "snapshot_period": snapshot_period,
            "injury_status": rec.player.get("injuryStatus"),
            "eligible_slots": [int(s) for s in slots] if isinstance(slots, list) else None,
            "default_position_id": rec.player.get("defaultPositionId"),
            "pro_team_id": rec.player.get("proTeamId"),
            "percent_owned": ownership.get("percentOwned"),
            "percent_started": ownership.get("percentStarted"),
            "percent_change": ownership.get("percentChange"),
            "availability": rec.availability,
            "on_provider_team_id": rec.on_team_id,
            "waiver_clear_at": millis(rec.pool_entry.get("waiverProcessDate")),
            "lineup_locked": rec.pool_entry.get("lineupLocked"),
        }
    return list(rows.values())


def pro_schedule(season: int, data: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per (pro team, scoring period): the game or the bye."""
    rows: dict[tuple[int, int], dict[str, Any]] = {}
    pro_teams = (data.get("settings") or {}).get("proTeams") or []
    for team in pro_teams:
        team_id = team.get("id")
        if not team_id:
            continue
        for period_key, games in (team.get("proGamesByScoringPeriod") or {}).items():
            for game in games or []:
                home, away = game.get("homeProTeamId"), game.get("awayProTeamId")
                opponent = away if home == team_id else home
                period = int(game.get("scoringPeriodId") or period_key)
                rows[(team_id, period)] = {
                    "season": season,
                    "scoring_period": period,
                    "pro_team_id": team_id,
                    "opponent_pro_team_id": opponent,
                    "is_home": home == team_id,
                    "kickoff_at": millis(game.get("date")),
                    "is_bye": False,
                    "provider_game_id": game.get("id"),
                }
        bye = team.get("byeWeek")
        if isinstance(bye, int) and bye > 0 and (team_id, bye) not in rows:
            rows[(team_id, bye)] = {
                "season": season,
                "scoring_period": bye,
                "pro_team_id": team_id,
                "opponent_pro_team_id": None,
                "is_home": None,
                "kickoff_at": None,
                "is_bye": True,
                "provider_game_id": None,
            }
    return sorted(rows.values(), key=lambda r: (r["scoring_period"], r["pro_team_id"]))
