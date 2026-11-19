"""One scoped scan; optionally persist intents, never deliver notifications."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from psycopg import Error, IsolationLevel
from requests import RequestException

from . import engine, notifications
from .config import ConfigurationError, Settings
from .database import connection
from .espn_client import FetchResult
from .loader import load_run
from .normalize.players import pro_schedule, status_rows
from .normalize.rosters import roster_entries
from .repository import Repository
from .sync import sync_current

MAX_INPUT_AGE = timedelta(minutes=15)
SEASON_INPUTS = ("mStatus", "mSettings", "mTeam", "proTeamSchedules_wl")
WEEK_INPUTS = ("mRoster", "mTransactions2", "kona_player_info")
PLAYER_MOVE_TYPES = ("WAIVER", "FREEAGENT", "TRADE_ACCEPT", "ROSTER", "FUTURE_ROSTER")
OPERATIONAL_ERRORS = (
    Error, RequestException, OSError, KeyError, ValueError,
    AttributeError, TypeError, SystemExit, engine.EngineError,
)


@dataclass(frozen=True)
class WorkerScope:
    league_id: int  # ESPN ID
    user_id: str  # Internal users.id
    team_id: int  # Expected ESPN team ID; never inferred from the feed
    season: int
    scoring_period: int


@dataclass
class WorkerResult:
    scope: WorkerScope
    status: str
    alert_eligible: bool = False
    run_id: str | None = None
    observed_at: str | None = None
    views: dict[str, str] = field(default_factory=dict)
    decisions: dict[str, dict[str, int]] = field(default_factory=dict)
    error: str | None = None
    outbox: list[dict[str, Any]] = field(default_factory=list)


def _objects(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _team_ids(payload: dict[str, Any]) -> set[int]:
    teams = payload.get("teams")
    if not _objects(teams):
        return set()
    ids = {team["id"] for team in teams if type(team.get("id")) is int and team["id"] > 0}
    return ids if len(ids) == len(teams) else set()


def _player_complete(player: dict[str, Any]) -> bool:
    return type(player.get("defaultPositionId")) is int and _objects(player.get("stats"))


def _view_state(view: str, payload: dict[str, Any], scope: WorkerScope, team_ids: set[int]) -> str:
    if view == "mStatus":
        status = _object(payload.get("status"))
        first, current, final = (status.get(k) for k in (
            "firstScoringPeriod", "latestScoringPeriod", "finalScoringPeriod",
        ))
        if not all(type(n) is int for n in (first, current, final)):
            return "partial"
        return "present" if 1 <= first <= current == scope.scoring_period <= final else "wrong_period"
    if view == "mSettings":
        settings = _object(payload.get("settings"))
        counts = _object(_object(settings.get("rosterSettings")).get("lineupSlotCounts"))
        acquisition = _object(settings.get("acquisitionSettings"))
        faab = acquisition.get("isUsingAcquisitionBudget")
        if (payload.get("id") == scope.league_id and
                any(i not in {"20", "21"} and type(n) is int and n > 0 for i, n in counts.items()) and
                type(faab) is bool and (not faab or type(acquisition.get("acquisitionBudget")) is int)):
            return "present"
    if view == "mTeam" and team_ids:
        return "present" if scope.team_id in team_ids else "wrong_team"
    if view == "mRoster":
        if not team_ids or _team_ids(payload) != team_ids:
            return "partial"
        players = set()
        for team in payload["teams"]:
            entries = _object(team.get("roster")).get("entries")
            if not entries or not _objects(entries):
                return "partial"
            for entry in entries:
                pid = entry.get("playerId")
                player = _object(_object(entry.get("playerPoolEntry")).get("player"))
                if (type(pid) is not int or pid in players or type(entry.get("lineupSlotId")) is not int
                        or player.get("id", pid) != pid or not _player_complete(player)):
                    return "partial"
                players.add(pid)
        return "present"
    if view == "mTransactions2":
        txns = payload.get("transactions")
        if _objects(txns) and all(
            tx.get("id") and tx.get("status") and tx.get("type") and _objects(tx.get("items"))
            and (tx["type"] not in PLAYER_MOVE_TYPES or tx["items"])
            for tx in txns
        ):
            return "present" if txns else "valid_empty"
    if view == "kona_player_info":
        players = payload.get("players")
        if _objects(players) and all(
            p.get("status") in ("FREEAGENT", "WAIVERS")
            and type(_object(p.get("player")).get("id", p.get("id"))) is int
            and _player_complete(_object(p.get("player")))
            for p in players
        ):
            return "present" if players else "valid_empty"
    if view == "proTeamSchedules_wl" and _object(payload.get("settings")).get("proTeams"):
        return "present"
    return "partial"


def _check_player_coverage(scope: WorkerScope, inputs: dict[str, dict], states: dict[str, str]) -> None:
    roster = status_rows(scope.season, scope.scoring_period, inputs["mRoster"])
    pool_data = inputs["kona_player_info"]
    pool = status_rows(scope.season, scope.scoring_period, pool_data)
    if (len(pool) != len(pool_data["players"]) or
            {p["provider_player_id"] for p in pool} & {p["provider_player_id"] for p in roster}):
        states["kona_player_info"] = "partial"
    pro_teams = {p["pro_team_id"] for p in roster + pool if p["pro_team_id"]}
    try:
        dated = {row["pro_team_id"] for row in pro_schedule(scope.season, inputs["proTeamSchedules_wl"])
                 if row["scoring_period"] == scope.scoring_period
                 and (row["is_bye"] or row["kickoff_at"] is not None)}
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        dated = set()
    if not dated or not pro_teams <= dated:
        states["proTeamSchedules_wl"] = "partial"


def _check_views(
    fetches: tuple[FetchResult, ...], scope: WorkerScope, now: datetime,
) -> tuple[dict[str, str], datetime | None]:
    by_key = {(f.view, f.scoring_period): f for f in fetches}
    team_fetch = by_key.get(("mTeam", None))
    team_ids = _team_ids(_object(team_fetch.payload)) if team_fetch else set()
    required = [(view, None) for view in SEASON_INPUTS] + [
        (view, scope.scoring_period) for view in WEEK_INPUTS
    ]
    states = {}
    inputs = {}
    times = []
    for view, period in required:
        fetch = by_key.get((view, period))
        if fetch is None or fetch.state == "unavailable":
            states[view] = "missing"
        elif fetch.state != "present" or fetch.status_code != 200:
            states[view] = "failed"
        else:
            try:
                retrieved = datetime.fromisoformat(fetch.retrieved_at)
            except (TypeError, ValueError):
                states[view] = "partial"
                continue
            if retrieved.tzinfo is None or not timedelta(0) <= now - retrieved <= MAX_INPUT_AGE:
                states[view] = "stale"
                continue
            data = _object(fetch.payload)
            if (fetch.season != scope.season or data.get("seasonId", scope.season) != scope.season
                    or (view != "proTeamSchedules_wl" and data.get("id", scope.league_id) != scope.league_id)
                    or (period is not None and data.get("scoringPeriodId", period) != period)):
                states[view] = "wrong_scope"
                continue
            states[view] = _view_state(view, data, scope, team_ids)
            inputs[view] = data
            times.append(retrieved)
    if set(states.values()) <= {"present", "valid_empty"}:
        _check_player_coverage(scope, inputs, states)
    return states, min(times, default=None)


def _league_for_scope(repo: Repository, scope: WorkerScope) -> str | None:
    # The existing loader writes only for local-primary. Verify, never default to it.
    if scope.user_id != repo.default_user_id():
        return None
    league = repo.league_id(scope.league_id, user_id=scope.user_id)
    if league is not None and repo.user_team(scope.user_id, league, scope.season) == scope.team_id:
        return league
    return None


def _roster_matches_run(repo: Repository, league: str, scope: WorkerScope, fetch: FetchResult) -> bool:
    if fetch.payload is None:
        return False
    observed = Counter(row[:3] for row in roster_entries(scope.season, scope.scoring_period, fetch.payload))
    loaded = Counter(
        (row.provider_team_id, row.provider_player_id, row.lineup_slot_id)
        for row in repo.roster_at(league, scope.season, scope.scoring_period)
    )
    return observed == loaded


def run_once(settings: Settings, scope: WorkerScope, raw_root: Path = Path("data/raw"),
             *, outbox: bool = False, now: datetime | None = None) -> WorkerResult:
    result = WorkerResult(scope, "SCOPE_REJECTED")
    if settings.league_id != scope.league_id:
        result.error = "configured_league_mismatch"
        return result
    if not settings.first_season <= scope.season <= settings.last_season:
        result.error = "season_outside_configuration"
        return result
    try:
        result.status = "SYNC_FAILED"
        with connection() as conn:
            league = _league_for_scope(Repository(conn), scope)
        if league is None:
            result.status, result.error = "SCOPE_REJECTED", "user_league_team_mapping_mismatch"
            return result
        sync = sync_current(settings, raw_root / f"league-{scope.league_id}", season=scope.season,
                            scoring_period=scope.scoring_period, load=False)
        result.run_id = sync.run_dir.name
        result.views, observed = _check_views(sync.fetches, scope, now or datetime.now(UTC))
        if observed is None or not set(result.views.values()) <= {"present", "valid_empty"}:
            result.error = "required_views_not_ready"
            return result
        result.observed_at = observed.isoformat()
        load_run(sync.run_dir)
        result.status = "EVALUATION_FAILED"
        with connection() as conn:
            conn.isolation_level = IsolationLevel.READ_COMMITTED if outbox else IsolationLevel.REPEATABLE_READ
            repo = Repository(conn)
            if _league_for_scope(repo, scope) != league:
                result.status, result.error = "SCOPE_REJECTED", "mapping_changed"
                return result
            sync_id = repo.completed_sync_id(league, sync.run_dir.name)
            roster = next(f for f in sync.fetches if f.view == "mRoster")
            if sync_id is None or not _roster_matches_run(repo, league, scope, roster):
                result.error = "loaded_snapshot_mismatch"
                return result
            repo = Repository(conn, player_sync_id=sync_id)
            ctx = engine.context(repo, league, scope.user_id, scope.season, scope.scoring_period,
                                 as_of_ts=now)
            results = (
                engine.run_lineup(ctx),
                engine.run_waivers(ctx, allow_empty_pool=result.views["kona_player_info"] == "valid_empty"),
            )
            evaluated_at = now or datetime.now(UTC)
            if evaluated_at - observed > MAX_INPUT_AGE:
                result.views, _ = _check_views(sync.fetches, scope, evaluated_at)
                result.status, result.error = "SYNC_FAILED", "required_views_expired"
                return result
            if outbox:
                # Serialize policy reads and cap accounting across ticks for this user.
                repo.lock_notification_scope(scope.user_id, league)
                evaluated_at = now or datetime.now(UTC)
                if evaluated_at - observed > MAX_INPUT_AGE:
                    result.status, result.error = "SYNC_FAILED", "required_views_expired"
                    return result
                history = repo.notification_history(scope.user_id, league, scope.season, evaluated_at)
                records = [r for batch in results for r in batch.records]
                kickoffs = [g.kickoff_at for g in repo.pro_games(scope.season, scope.scoring_period).values()
                            if g.kickoff_at is not None]
                if kickoffs:
                    for rec in records:
                        if rec["kind"] == "lineup":
                            rec["payload"]["expires_at"] = min(kickoffs).isoformat()
                choices = notifications.evaluate(
                    records, history, {k: p.value for k, p in repo.preferences(scope.user_id, league).items()},
                    evaluated_at, user_id=scope.user_id, league_season_id=f"{league}:{scope.season}",
                )
                ids = [rid for batch in results for rid in engine.save(ctx, batch)]
                for rec, rid, choice in zip(records, ids, choices, strict=True):
                    intent = repo.enqueue_notification(rid, scope.user_id, league, scope.season,
                                                       scope.scoring_period, choice, evaluated_at)
                    if choice["disposition"] != "suppress" and intent is None:
                        choice.update(disposition="suppress", reason="duplicate")
                    result.outbox.append({"recommendation_id": rid, "kind": rec["kind"],
                                          **{k: v.isoformat() if isinstance(v, datetime) else v
                                             for k, v in choice.items()}})
    except ConfigurationError as exc:
        result.status, result.error = "CONFIGURATION_ERROR", str(exc)
        return result
    except OPERATIONAL_ERRORS as exc:
        result.outbox.clear()
        result.error = type(exc).__name__
        return result
    records = [r for batch in results for r in batch.records]
    for record in records:
        counts = result.decisions.setdefault(record["kind"], {})
        decision = record["decision"]
        counts[decision] = counts.get(decision, 0) + 1
    result.alert_eligible = any(r["decision"] in {"notify", "urgent"} for r in records)
    result.status = "ACTIONABLE" if result.alert_eligible else "NO_ACTION"
    return result
