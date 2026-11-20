"""One scheduled, shadow-mode tick: decide whether a scan is due and run it.

A scheduler (Cloud Scheduler -> Cloud Run Job) calls this every few minutes.
Each tick reads public game status and the opportunity ledger, asks the R1
trigger policy what is due, runs the F1 worker only when a scan is due, and
records a heartbeat row so stale syncs, missed runs and repeated failures can
be alerted on. Nothing is ever sent and no ESPN action is taken.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import requests

from espn_ff_assistant.archive import RawArchive
from espn_ff_assistant.config import ConfigurationError, Settings
from espn_ff_assistant.database import connection
from espn_ff_assistant.espn_client import ESPNClient, FetchResult, fetch_scoreboard
from espn_ff_assistant.game_status import GameStatus, normalize_game_status
from espn_ff_assistant.object_store import from_url
from espn_ff_assistant.repository import Repository, StoredOpportunity, WorkerTick
from espn_ff_assistant.sync import sync_periods
from espn_ff_assistant.triggers import DEFAULT_CONFIG, Trigger, TriggerConfig, due_triggers
from espn_ff_assistant.worker import WorkerResult, WorkerScope, run_once

# Window markers only gate later ticks; outcome scoring belongs to a later phase.
MARKER_KINDS = frozenset({"window_final", "all_final"})
DEFERRED_KINDS = frozenset({"outcome_scoring"})
OK_STATUSES = frozenset({"HOLD", "SCANNED", "SKIPPED_OVERLAP"})
OUTBOUND_FLAGS = ("DELIVERY_ENABLED", "SMS_ENABLED", "ESPN_ACTIONS_ENABLED")
OPERATIONAL_ERRORS = (psycopg.Error, requests.RequestException, OSError, ValueError)


@dataclass(frozen=True)
class TickConfig:
    user_id: str
    team_id: int
    season: int
    refresh_after: timedelta = timedelta(hours=6)
    triggers: TriggerConfig = DEFAULT_CONFIG

    @classmethod
    def from_env(cls, settings: Settings, env: Mapping[str, str] | None = None) -> TickConfig:
        env = os.environ if env is None else env
        for flag in OUTBOUND_FLAGS:
            if env.get(flag, "false").strip().lower() not in ("", "0", "false", "no"):
                raise ConfigurationError(f"{flag} must stay off in shadow mode")
        try:
            user_id = str(UUID(env["WORKER_USER_ID"]))
            team_id = int(env["WORKER_TEAM_ID"])
            season = int(env.get("WORKER_SEASON") or settings.last_season)
            refresh_hours = float(env.get("WORKER_REFRESH_HOURS") or 6)
        except KeyError as exc:
            raise ConfigurationError(f"{exc.args[0]} is required") from None
        except ValueError:
            raise ConfigurationError("WORKER_* settings must be valid numbers/UUIDs") from None
        if team_id <= 0 or refresh_hours <= 0:
            raise ConfigurationError("WORKER_TEAM_ID and WORKER_REFRESH_HOURS must be positive")
        return cls(user_id, team_id, season, timedelta(hours=refresh_hours))


@dataclass(frozen=True)
class TickPlan:
    markers: tuple[Trigger, ...] = ()
    scans: tuple[Trigger, ...] = ()
    deferred: tuple[Trigger, ...] = ()
    refresh: bool = False

    @property
    def scan_due(self) -> bool:
        return bool(self.scans) or self.refresh

    @property
    def reasons(self) -> list[str]:
        return [t.key for t in self.scans] + (["refresh"] if self.refresh else [])


def plan_tick(
    now: datetime,
    games: Sequence[GameStatus],
    opportunities: Sequence[StoredOpportunity],
    fired: Mapping[str, datetime],
    last_scan_at: datetime | None,
    config: TickConfig,
    period: int | None = None,
) -> TickPlan:
    """Split due triggers into markers, scan triggers and deferred kinds."""
    due = due_triggers(now, games, opportunities, fired, config.triggers, period=period)
    return TickPlan(
        markers=tuple(t for t in due if t.kind in MARKER_KINDS),
        scans=tuple(t for t in due if t.kind not in MARKER_KINDS | DEFERRED_KINDS),
        deferred=tuple(t for t in due if t.kind in DEFERRED_KINDS),
        refresh=last_scan_at is None or now - last_scan_at >= config.refresh_after,
    )


@dataclass
class TickResult:
    status: str
    started_at: datetime
    scoring_period: int | None = None
    plan: TickPlan = field(default_factory=TickPlan)
    worker: WorkerResult | None = None
    error: str | None = None
    dry_run: bool = False
    inputs_run_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in OK_STATUSES

    def heartbeat(self, finished_at: datetime) -> WorkerTick:
        return WorkerTick(
            self.started_at, finished_at, self.status, self.ok, self.scoring_period,
            self.plan.reasons, self.worker.status if self.worker else None,
            self.worker.run_id if self.worker else None, self.error,
            {"markers": [t.key for t in self.plan.markers],
             "deferred": [t.key for t in self.plan.deferred],
             "inputs_run_id": self.inputs_run_id},
        )

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status, "ok": self.ok, "dry_run": self.dry_run,
            "started_at": self.started_at.isoformat(), "scoring_period": self.scoring_period,
            "scan_due": self.plan.scan_due, "reasons": self.plan.reasons,
            "markers": [t.key for t in self.plan.markers],
            "deferred": [t.key for t in self.plan.deferred],
            "inputs_run_id": self.inputs_run_id,
        }
        if self.worker is not None:
            out["worker"] = {"status": self.worker.status, "run_id": self.worker.run_id,
                             "error": self.worker.error}
        if self.error:
            out["error"] = self.error
        return out


Worker = Callable[[Settings, WorkerScope, Path], WorkerResult]


def _tick_archive(settings: Settings, raw_root: Path) -> RawArchive:
    """Private archive for the inputs a tick decides on, beside the worker's runs."""
    url = os.environ.get("RAW_ARCHIVE_URL")
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex}-tick"
    return RawArchive(raw_root / f"league-{settings.league_id}", run_id,
                      league_id=settings.league_id, store=from_url(url) if url else None)


def _current_inputs(
    settings: Settings, season: int, now: datetime,
    save: Callable[[FetchResult], object] | None = None,
) -> tuple[int, list[GameStatus]]:
    status = ESPNClient(settings).fetch(season, "mStatus")
    if save:
        save(status)
    if status.state != "present" or not status.payload:
        raise LookupError("league_status_unavailable")
    period, _ = sync_periods(status.payload, 0)
    board = fetch_scoreboard(season, period)
    if save:
        save(board)
    observed = datetime.fromisoformat(board.retrieved_at) if board.retrieved_at else now
    batch = normalize_game_status(board.payload, observed_at=observed, fetch_state=board.state)
    if batch.state not in ("present", "empty"):
        raise LookupError(f"game_status_{batch.state}")
    return period, list(batch.games.values())


def run_tick(
    settings: Settings,
    config: TickConfig,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    raw_root: Path = Path("data/raw"),
    worker: Worker = run_once,
) -> TickResult:
    """Run one tick. With dry_run, plan only: no worker run and no writes."""
    now = now or datetime.now(UTC)
    result = TickResult("ERROR", now, dry_run=dry_run)
    try:
        with connection() as conn:
            repo = Repository(conn)
            league = repo.league_id(settings.league_id, user_id=config.user_id)
            if league is None:
                result.status, result.error = "SCOPE_REJECTED", "league_not_loaded_for_user"
                return result
            if not dry_run and not repo.try_tick_lock(league):
                result.status = "SKIPPED_OVERLAP"
                return result
            try:
                _tick(repo, conn, league, settings, config, result, now, dry_run, raw_root, worker)
            except (LookupError, *OPERATIONAL_ERRORS) as exc:
                conn.rollback()
                result.status = "INPUT_UNAVAILABLE" if isinstance(exc, LookupError) else "ERROR"
                result.error = str(exc) if isinstance(exc, LookupError) else type(exc).__name__
            if not dry_run:
                repo.record_worker_tick(league, config.season, result.heartbeat(datetime.now(UTC)))
                conn.commit()
                repo.release_tick_lock(league)
    except ConfigurationError as exc:
        result.status, result.error = "CONFIGURATION_ERROR", str(exc)
    except OPERATIONAL_ERRORS as exc:
        result.status, result.error = "ERROR", type(exc).__name__
    return result


def _tick(
    repo: Repository, conn: psycopg.Connection, league: str, settings: Settings,
    config: TickConfig, result: TickResult, now: datetime, dry_run: bool,
    raw_root: Path, worker: Worker,
) -> None:
    archive = None if dry_run else _tick_archive(settings, raw_root)
    if archive is not None:
        result.inputs_run_id = archive.run_dir.name
    period, games = _current_inputs(settings, config.season, now,
                                    archive.save if archive else None)
    result.scoring_period = period
    opportunities = repo.stored_opportunities(league, config.season)
    fired = repo.fired_triggers(league, config.season)
    last_scan = repo.latest_opportunity_observation(league, config.season)
    conn.commit()  # end the read snapshot before the (long) worker run
    result.plan = plan_tick(now, games, opportunities, fired, last_scan, config, period)
    result.status = "HOLD"
    if dry_run:
        return
    for trigger in result.plan.markers:
        repo.record_trigger_firing(league, config.season, trigger.key, now,
                                   {"kind": trigger.kind, "detail": trigger.detail})
    conn.commit()
    if not result.plan.scan_due:
        return
    scope = WorkerScope(settings.league_id, config.user_id, config.team_id, config.season, period)
    result.worker = worker(settings, scope, raw_root)
    if result.worker.status not in ("ACTIONABLE", "NO_ACTION"):
        result.status, result.error = "SCAN_FAILED", result.worker.error
        return
    result.status = "SCANNED"
    for trigger in result.plan.scans:
        repo.record_trigger_firing(league, config.season, trigger.key, now,
                                   {"kind": trigger.kind, "detail": trigger.detail,
                                    "run_id": result.worker.run_id})
    conn.commit()


@dataclass(frozen=True)
class HealthPolicy:
    missed_after: timedelta = timedelta(minutes=45)
    stale_after: timedelta = timedelta(hours=8)
    failure_streak: int = 3


DEFAULT_HEALTH = HealthPolicy()


def assess_health(
    now: datetime,
    ticks: Sequence[WorkerTick],
    last_scan_at: datetime | None,
    policy: HealthPolicy = DEFAULT_HEALTH,
) -> list[str]:
    """Problems to alert on, given the newest ticks first."""
    problems = []
    if not ticks or now - ticks[0].started_at > policy.missed_after:
        problems.append("missed_runs")
    if last_scan_at is None or now - last_scan_at > policy.stale_after:
        problems.append("stale_sync")
    streak = ticks[: policy.failure_streak]
    if len(streak) == policy.failure_streak and not any(t.ok for t in streak):
        problems.append("repeated_failures")
    return problems
