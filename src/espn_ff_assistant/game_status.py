"""Pure timestamped ESPN observations; kickoff is never completion evidence."""

from dataclasses import dataclass, replace
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


FINAL_NAMES = {"STATUS_FINAL", "STATUS_FINAL_OVERTIME", "STATUS_FINAL_2OT", "STATUS_FINAL_OT"}
IN_PROGRESS_NAMES = {
    "STATUS_IN_PROGRESS", "STATUS_HALFTIME", "STATUS_END_PERIOD", "STATUS_OVERTIME",
}
INTERRUPTED_NAMES = {
    "STATUS_DELAYED": GameState.DELAYED,
    "STATUS_RAIN_DELAY": GameState.DELAYED,
    "STATUS_POSTPONED": GameState.POSTPONED,
    "STATUS_SUSPENDED": GameState.SUSPENDED,
}


@dataclass(frozen=True)
class GameStatus:
    provider_game_id: str
    source: str
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


def _input_state(payload: object, observed_at: datetime, fetch_state: str) -> StatusBatch | None:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    if fetch_state != "present":
        return StatusBatch("failed", {}, (f"fetch state: {fetch_state}",))
    if payload is None:
        return StatusBatch("missing", {})
    if not isinstance(payload, dict):
        return StatusBatch("malformed", {}, ("payload must be an object",))
    return None


def _scoreboard_state(raw: object) -> GameState:
    if not isinstance(raw, dict) or not isinstance(raw.get("type"), dict):
        return GameState.UNKNOWN
    status = raw["type"]
    name, phase, completed = status.get("name"), status.get("state"), status.get("completed")
    if not isinstance(name, str):
        return GameState.UNKNOWN
    if name in FINAL_NAMES:
        return GameState.FINAL if completed is True and phase == "post" else GameState.UNKNOWN
    if completed is not False:
        return GameState.UNKNOWN
    if name == "STATUS_SCHEDULED" and phase == "pre":
        return GameState.SCHEDULED
    if name in IN_PROGRESS_NAMES and phase == "in":
        return GameState.IN_PROGRESS
    if name in INTERRUPTED_NAMES and phase in ("pre", "in"):
        return INTERRUPTED_NAMES[name]
    return GameState.UNKNOWN


def _record(
    game: object, observed_at: datetime, source: str, issues: list[str]
) -> GameStatus | None:
    if not isinstance(game, dict):
        issues.append("game must be an object")
        return None
    game_id = game.get("id")
    if type(game_id) not in (int, str) or not str(game_id).strip():
        issues.append("game requires a provider ID")
        return None
    kickoff = None
    date = game.get("date")
    if date is not None:
        try:
            if source == "espn_scoreboard":
                kickoff = datetime.fromisoformat(date)
                if kickoff.tzinfo is None or kickoff.utcoffset() is None:
                    raise ValueError
                kickoff = kickoff.astimezone(UTC)
            else:
                if type(date) not in (int, float):
                    raise ValueError
                kickoff = datetime.fromtimestamp(date / 1000, UTC)
        except (TypeError, ValueError, OverflowError, OSError):
            kickoff = None
            issues.append(f"invalid kickoff for {game_id}")
    if source == "espn_scoreboard":
        raw = game.get("status")
        state = _scoreboard_state(raw)
    else:
        raw = {"statsOfficial": game.get("statsOfficial")}
        state = GameState.FINAL if game.get("statsOfficial") is True else GameState.UNKNOWN
    return GameStatus(str(game_id), source, state, observed_at.astimezone(UTC), kickoff, raw)


def _batch(entries: list, observed_at: datetime, source: str, issues: list[str]) -> StatusBatch:
    games: dict[str, GameStatus] = {}
    for entry in entries:
        record = _record(entry, observed_at, source, issues)
        if record is None:
            continue
        game_id = record.provider_game_id
        if game_id in games and games[game_id] != record:
            issues.append(f"conflicting duplicate game {game_id}")
            record = replace(record, state=GameState.UNKNOWN, kickoff_at=None, raw_status=None)
        games[game_id] = record
    quality = "present" if games else "empty"
    if issues:
        quality = "partial" if games else "malformed"
    return StatusBatch(quality, games, tuple(issues))


def normalize_game_status(
    payload: object, *, observed_at: datetime, fetch_state: str = "present"
) -> StatusBatch:
    """Normalize one scoreboard retrieval, retaining its evidence timestamp."""
    invalid = _input_state(payload, observed_at, fetch_state)
    if invalid is not None:
        return invalid
    events = payload.get("events")
    if events is None:
        return StatusBatch("missing", {})
    if not isinstance(events, list):
        return StatusBatch("malformed", {}, ("events must be a list",))
    return _batch(events, observed_at, "espn_scoreboard", [])


def normalize_fantasy_schedule(
    payload: object, *, observed_at: datetime, fetch_state: str = "present"
) -> StatusBatch:
    """Only literal statsOfficial=true is completion evidence in this feed."""
    invalid = _input_state(payload, observed_at, fetch_state)
    if invalid is not None:
        return invalid
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
    entries, issues = [], []
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
        for games in periods.values():
            if isinstance(games, list):
                entries.extend(games)
            else:
                issues.append("period games must be a list")
    return _batch(entries, observed_at, "espn_fantasy_schedule", issues)
