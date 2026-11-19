"""Pure adapter for the existing fantasy ESPN proTeamSchedules_wl response.

Status tokens are an explicit, unverified provider contract; see
docs/game-status.md. No time-based or numeric-status finality inference is made.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class GameState(StrEnum):
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    DELAYED = "delayed"
    POSTPONED = "postponed"
    SUSPENDED = "suspended"
    FINAL = "final"
    UNKNOWN = "unknown"


STATUS_NAMES = {
    "STATUS_SCHEDULED": GameState.SCHEDULED,
    "STATUS_IN_PROGRESS": GameState.IN_PROGRESS,
    "STATUS_DELAYED": GameState.DELAYED,
    "STATUS_POSTPONED": GameState.POSTPONED,
    "STATUS_SUSPENDED": GameState.SUSPENDED,
    "STATUS_FINAL": GameState.FINAL,
}


@dataclass(frozen=True)
class GameStatus:
    provider_game_id: str
    state: GameState
    observed_at: datetime
    kickoff_at: datetime | None
    raw_status: Any

    @property
    def final_observed_at(self) -> datetime | None:
        """Evidence time for this observation, not the actual end of play."""
        return self.observed_at if self.state == GameState.FINAL else None


@dataclass(frozen=True)
class StatusBatch:
    # missing, empty, malformed, partial, present, or failed
    state: str
    games: dict[str, GameStatus]
    issues: tuple[str, ...] = ()


def normalize_game_status(
    payload: object, *, observed_at: datetime, fetch_state: str = "present"
) -> StatusBatch:
    """Normalize one retrieval; duplicates must agree or become unknown.

    Supply FetchResult.retrieved_at (parsed as an aware datetime), never now().
    Keep batches separately to preserve multiple observations of the same ID.
    """
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    observed_at = observed_at.astimezone(UTC)
    if fetch_state != "present":
        return StatusBatch("failed", {}, (f"fetch state: {fetch_state}",))
    if payload is None:
        return StatusBatch("missing", {})
    if not isinstance(payload, dict):
        return StatusBatch("malformed", {}, ("payload must be an object",))
    settings = payload.get("settings")
    if settings is None:
        return StatusBatch("missing", {})
    if not isinstance(settings, dict):
        return StatusBatch("malformed", {}, ("settings must be an object",))
    teams = settings.get("proTeams")
    if teams is None:
        return StatusBatch("missing", {})
    if not isinstance(teams, list):
        return StatusBatch("malformed", {}, ("proTeams must be a list",))
    games: dict[str, GameStatus] = {}
    issues: list[str] = []
    for team in teams:
        if not isinstance(team, dict):
            issues.append("team must be an object")
            continue
        if team.get("id") == 0:
            continue
        periods = team.get("proGamesByScoringPeriod")
        if not isinstance(periods, dict):
            issues.append("missing or malformed proGamesByScoringPeriod")
            continue
        for entries in periods.values():
            if not isinstance(entries, list):
                issues.append("period games must be a list")
                continue
            for game in entries:
                if not isinstance(game, dict) or type(game.get("id")) not in (int, str) or not str(game["id"]).strip():
                    issues.append("game requires a provider ID")
                    continue
                game_id = str(game["id"])
                raw = game.get("status")
                state = STATUS_NAMES.get(raw, GameState.UNKNOWN) if isinstance(raw, str) else GameState.UNKNOWN
                kickoff = None
                date = game.get("date")
                if date is not None:
                    try:
                        if type(date) not in (int, float):
                            raise ValueError
                        kickoff = datetime.fromtimestamp(date / 1000, UTC)
                    except (ValueError, OverflowError, OSError):
                        issues.append(f"invalid kickoff for {game_id}")
                record = GameStatus(game_id, state, observed_at, kickoff, raw)
                if game_id in games and games[game_id] != record:
                    issues.append(f"conflicting duplicate game {game_id}")
                    record = GameStatus(game_id, GameState.UNKNOWN, observed_at, None, None)
                games[game_id] = record
    quality = "partial" if issues and games else "malformed" if issues else "present" if games else "empty"
    return StatusBatch(quality, games, tuple(issues))
