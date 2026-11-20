"""Trigger policy: decide which ticks should run a worker scan.

A tick is cheap: it reads game-status observations and the opportunity
ledger and emits triggers. Game review fires when a league-local window
(Thursday, Sunday early/late/night, Monday, or another weekday group) is
verified complete plus a settling delay. Non-urgent ledger churn is batched
into the daily and post-window ticks; only urgent pre-lock and waiver
deadlines bypass batching. All times are league-local (America/New_York),
so DST boundaries are handled by zoneinfo, never by hour arithmetic.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .game_status import GameState, GameStatus
from .repository import StoredOpportunity

LEAGUE_TZ = ZoneInfo("America/New_York")
SUNDAY_LATE_HOUR = 16  # local hour splits: <16 early, 16-19 late, >=20 night
SUNDAY_NIGHT_HOUR = 20


@dataclass(frozen=True)
class TriggerConfig:
    settle_delay: timedelta = timedelta(minutes=45)
    outcome_delay: timedelta = timedelta(hours=12)
    daily_drop_hour: int = 9
    pre_waiver_offset: timedelta = timedelta(minutes=30)
    post_waiver_delay: timedelta = timedelta(minutes=30)
    urgent_pre_lock_lead: timedelta = timedelta(minutes=45)


DEFAULT_CONFIG = TriggerConfig()


@dataclass(frozen=True)
class Trigger:
    key: str
    kind: str
    fires_at: datetime
    urgent: bool
    detail: str


@dataclass(frozen=True)
class WindowReport:
    key: str
    game_ids: tuple[str, ...]
    state: str  # settled, waiting, blocked
    settles_at: datetime | None
    detail: str


def _local(kickoff: datetime) -> datetime:
    return kickoff.astimezone(LEAGUE_TZ)


def game_window(kickoff: datetime) -> str:
    """Window key for a kickoff, on the league-local calendar."""
    local = _local(kickoff)
    day = local.strftime("%A").lower()
    if day == "sunday":
        if local.hour < SUNDAY_LATE_HOUR:
            day = "sunday_early"
        elif local.hour < SUNDAY_NIGHT_HOUR:
            day = "sunday_late"
        else:
            day = "sunday_night"
    return f"{local.date().isoformat()}:{day}"


def _waiver_clear_at(opportunity: StoredOpportunity) -> datetime | None:
    raw = opportunity.opportunity.attrs.get("waiver_clear_at")
    if not isinstance(raw, str):
        return None
    try:
        clear = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return clear if clear.tzinfo is not None else None


def window_states(
    games: Sequence[GameStatus], now: datetime, config: TriggerConfig = DEFAULT_CONFIG
) -> dict[str, WindowReport]:
    """Group games into league-local windows and report settle readiness."""
    windows: dict[str, list[GameStatus]] = {}
    for game in games:
        if game.kickoff_at is None:
            continue
        windows.setdefault(game_window(game.kickoff_at), []).append(game)
    reports = {}
    for key in sorted(windows):
        group = windows[key]
        pending = [g for g in group if g.state != GameState.FINAL]
        if pending:
            states = sorted({g.state.value for g in pending})
            reports[key] = WindowReport(
                key, tuple(sorted(g.provider_game_id for g in group)), "waiting",
                None, f"{len(pending)} game(s) not final ({', '.join(states)})",
            )
            continue
        last_final = max(
            g.final_observed_at for g in group
            if g.final_observed_at is not None
        )
        settles_at = last_final + config.settle_delay
        if now >= settles_at:
            reports[key] = WindowReport(
                key, tuple(sorted(g.provider_game_id for g in group)), "settled",
                settles_at, f"final observed {last_final.isoformat()}",
            )
        else:
            reports[key] = WindowReport(
                key, tuple(sorted(g.provider_game_id for g in group)), "blocked",
                settles_at, f"settling until {settles_at.isoformat()}",
            )
    return reports


def due_triggers(
    now: datetime,
    games: Sequence[GameStatus],
    opportunities: Sequence[StoredOpportunity],
    fired: Collection[str] = (),
    config: TriggerConfig = DEFAULT_CONFIG,
) -> list[Trigger]:
    """Triggers due at `now`, minus keys already fired.

    Deterministic and replay-safe: the same inputs produce the same keys,
    and a recorded firing suppresses the trigger on overlapping ticks.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    fired = set(fired)
    triggers: dict[str, Trigger] = {}

    def emit(trigger: Trigger) -> None:
        if trigger.key not in fired and trigger.fires_at <= now:
            triggers[trigger.key] = trigger

    # Game-completion ticks, one per settled window.
    reports = window_states(games, now, config)
    for report in reports.values():
        if report.state == "settled" and report.settles_at is not None:
            emit(Trigger(
                f"game_settled:{report.key}", "game_settled", report.settles_at,
                False, f"{len(report.game_ids)} game(s) final; {report.detail}",
            ))

    # Outcome scoring once the week's last game is verified final.
    live = [g for g in games if g.kickoff_at is not None]
    if live and all(g.state == GameState.FINAL for g in live):
        last_final = max(g.final_observed_at for g in live if g.final_observed_at)
        fires_at = last_final + config.outcome_delay
        emit(Trigger(
            "outcome_scoring", "outcome_scoring", fires_at, False,
            f"all {len(live)} game(s) final; scoring settles {fires_at.isoformat()}",
        ))

    pending: list[StoredOpportunity] = [
        o for o in opportunities
        if o.expired_at is None and o.opportunity.expires_at > now
    ]

    # League-local waiver ticks around each pending clearance time.
    clear_times = {
        clear for o in pending
        if o.opportunity.kind == "player_dropped"
        for clear in [_waiver_clear_at(o)]
        if clear is not None
    }
    for clear in sorted(clear_times):
        stamp = clear.astimezone(UTC).isoformat()
        if now < clear:
            emit(Trigger(
                f"pre_waiver:{stamp}", "pre_waiver", clear - config.pre_waiver_offset,
                True, f"waiver clears {stamp}",
            ))
        emit(Trigger(
            f"post_waiver:{stamp}", "post_waiver", clear + config.post_waiver_delay,
            False, f"waiver cleared {stamp}",
        ))

    # Urgent pre-lock ticks bypass batching for starters about to lock.
    for opp in pending:
        if opp.opportunity.kind != "starter_out":
            continue
        kickoff = opp.opportunity.expires_at
        if now < kickoff <= now + config.urgent_pre_lock_lead:
            emit(Trigger(
                f"urgent_pre_lock:{opp.opportunity.change_key}", "urgent_pre_lock",
                now, True, f"locks at {kickoff.astimezone(UTC).isoformat()}",
            ))

    # Inconsequential churn batches into one league-local daily tick.
    local_now = now.astimezone(LEAGUE_TZ)
    fires_local = datetime.combine(
        local_now.date(), time(config.daily_drop_hour), tzinfo=LEAGUE_TZ
    )
    if fires_local <= local_now and pending:
        emit(Trigger(
            f"daily_drop:{local_now.date().isoformat()}", "daily_drop",
            fires_local, False, f"{len(pending)} pending opportunity(ies)",
        ))

    return list(triggers.values())


def coverage_report(
    now: datetime,
    games: Sequence[GameStatus],
    opportunities: Sequence[StoredOpportunity],
    fired: Collection[str] = (),
    config: TriggerConfig = DEFAULT_CONFIG,
) -> Mapping[str, object]:
    """What the tick would do, for scripts and tests."""
    reports = window_states(games, now, config)
    triggers = due_triggers(now, games, opportunities, fired, config)
    due = {t.key: t for t in triggers}
    skipped = sorted(set(fired) - set(due))
    return {
        "windows": {k: {"state": r.state, "detail": r.detail} for k, r in reports.items()},
        "triggers": {t.key: {"kind": t.kind, "urgent": t.urgent, "detail": t.detail}
                     for t in triggers},
        "already_fired": skipped,
    }
