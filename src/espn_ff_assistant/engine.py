"""Assemble engine inputs from the repository and produce persisted recommendations.

The lineup and waiver engines are pure; this module is the only glue between
them and the database. Every run produces at least one recommendation row,
including `no_action`, so outcomes can be scored later.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from . import lineup as lineup_engine
from . import waivers as waiver_engine
from .lineup import SLOT_NAMES, LineupPlayer, LineupRecommendation
from .manager_stats import POSITION_NAMES
from .repository import PlayerStatus, Repository
from .waivers import Comparable, Competitor, PoolPlayer, WaiverContext, WaiverOption

DEFAULT_ELIGIBLE = {
    "QB": frozenset({0, 7, 20, 21}),
    "RB": frozenset({2, 3, 23, 7, 20, 21}),
    "WR": frozenset({4, 3, 5, 23, 7, 20, 21}),
    "TE": frozenset({6, 5, 23, 7, 20, 21}),
    "K": frozenset({17, 20, 21}),
    "DST": frozenset({16, 20, 21}),
}
MAX_STORED_WAIVER_OPTIONS = 10


class EngineError(RuntimeError):
    """Raised when required state (settings, roster, identity) is missing."""


@dataclass
class EngineResult:
    records: list[dict[str, Any]]      # rows ready for Repository.save_recommendation
    evidence: list[list[dict[str, Any]]]
    detail: Any


@dataclass
class _Context:
    repo: Repository
    league_id: str
    user_id: str
    season: int
    period: int
    as_of_ts: datetime
    team_id: int
    slot_counts: dict[int, int]
    settings: dict[str, Any]
    final_period: int


def _slot_counts(settings: dict[str, Any]) -> dict[int, int]:
    counts = (settings.get("rosterSettings") or {}).get("lineupSlotCounts") or {}
    return {int(k): int(v) for k, v in counts.items() if int(v) > 0}


def context(
    repo: Repository,
    league_id: str,
    user_id: str,
    season: int,
    period: int,
    as_of_ts: datetime | None = None,
) -> _Context:
    team_id = repo.user_team(user_id, league_id, season)
    if team_id is None:
        raise EngineError(
            f"No team is mapped to this user for {season}; run `recommend.py whoami --team <id>`."
        )
    settings = repo.league_settings(league_id, season)
    if not settings:
        raise EngineError(f"League settings for {season} are not loaded.")
    season_obj = next((s for s in repo.seasons(league_id) if s.season == season), None)
    final = (season_obj.final_scoring_period if season_obj else None) or period
    return _Context(
        repo, league_id, user_id, season, period, as_of_ts or datetime.now(UTC), team_id,
        _slot_counts(settings), settings, final,
    )


# ---------------------------------------------------------------------------
# Input assembly
# ---------------------------------------------------------------------------

def _position(status: PlayerStatus | None, fallback: int | None) -> str:
    pid = status.default_position_id if status and status.default_position_id else fallback
    return POSITION_NAMES.get(pid or -1, "UNK")


def _lineup_player(
    pid: int,
    name: str,
    position: str,
    status: PlayerStatus | None,
    projection: float | None,
    byes: set[int],
    period: int,
    current_slot: int,
) -> LineupPlayer:
    eligible = frozenset(status.eligible_slots) if status and status.eligible_slots else DEFAULT_ELIGIBLE.get(position, frozenset({20}))
    pro_team = status.pro_team_id if status else None
    locked = bool(status and status.lineup_locked and status.snapshot_period == period)
    return LineupPlayer(
        provider_player_id=pid,
        name=name,
        eligible_slots=eligible,
        projection=projection,
        injury_status=status.injury_status if status else None,
        on_bye=pro_team in byes,
        locked=locked,
        current_slot=current_slot,
    )


def _byes(repo: Repository, season: int, period: int) -> set[int]:
    return {tid for tid, g in repo.pro_games(season, period).items() if g.is_bye}


def _remaining_games(repo: Repository, season: int, start: int, final: int) -> dict[int, int]:
    """{pro_team_id: games from start..final}, default = number of weeks when unknown."""
    weeks = max(0, final - start + 1)
    byes = repo.bye_weeks(season)
    return {team: weeks - sum(1 for b in periods if start <= b <= final) for team, periods in byes.items()}


def team_players(ctx: _Context, team_id: int) -> tuple[list[PoolPlayer], dict[int, str]]:
    repo = ctx.repo
    roster_period = repo.latest_roster_period(ctx.league_id, ctx.season, ctx.period)
    if roster_period is None:
        raise EngineError(f"No roster snapshot at or before {ctx.season} week {ctx.period}.")
    entries = repo.roster_at(ctx.league_id, ctx.season, roster_period, team_id, include_players=True)
    ids = [e.provider_player_id for e in entries]
    statuses = repo.player_status(ctx.league_id, ctx.season, ctx.period, ids, ctx.as_of_ts)
    projections = repo.week_projections(ctx.league_id, ctx.season, ctx.period, ids, ctx.as_of_ts)
    ros = repo.ros_projections(ctx.league_id, ctx.season, ctx.period, ids, ctx.as_of_ts)
    byes = _byes(repo, ctx.season, ctx.period)
    games = _remaining_games(repo, ctx.season, ctx.period, ctx.final_period)
    default_games = max(0, ctx.final_period - ctx.period + 1)

    players: list[PoolPlayer] = []
    names: dict[int, str] = {}
    for e in entries:
        status = statuses.get(e.provider_player_id)
        position = _position(status, e.player.default_position_id if e.player else None)
        name = (e.player.full_name if e.player else None) or str(e.provider_player_id)
        names[e.provider_player_id] = name
        lp = _lineup_player(
            e.provider_player_id, name, position, status,
            projections.get(e.provider_player_id), byes, ctx.period,
            e.lineup_slot_id if e.lineup_slot_id is not None else 20,
        )
        pro_team = status.pro_team_id if status else None
        players.append(PoolPlayer(
            lineup=lp, position=position, ros=ros.get(e.provider_player_id),
            remaining_games=games.get(pro_team, default_games) if pro_team else default_games,
            percent_owned=status.percent_owned if status else None,
            percent_change=status.percent_change if status else None,
        ))
    return players, names


def pool_players(ctx: _Context) -> tuple[list[PoolPlayer], dict[int, str]]:
    repo = ctx.repo
    available = repo.available_players(ctx.league_id, ctx.season, ctx.period, ctx.as_of_ts)
    ids = [s.provider_player_id for s in available]
    projections = repo.week_projections(ctx.league_id, ctx.season, ctx.period, ids, ctx.as_of_ts)
    ros = repo.ros_projections(ctx.league_id, ctx.season, ctx.period, ids, ctx.as_of_ts)
    identities = repo.players_by_ids(ids)
    byes = _byes(repo, ctx.season, ctx.period)
    games = _remaining_games(repo, ctx.season, ctx.period, ctx.final_period)
    default_games = max(0, ctx.final_period - ctx.period + 1)

    players: list[PoolPlayer] = []
    names: dict[int, str] = {}
    for s in available:
        ident = identities.get(s.provider_player_id)
        position = _position(s, ident.default_position_id if ident else None)
        name = (ident.full_name if ident else None) or str(s.provider_player_id)
        names[s.provider_player_id] = name
        lp = _lineup_player(s.provider_player_id, name, position, s,
                            projections.get(s.provider_player_id), byes, ctx.period, 20)
        players.append(PoolPlayer(
            lineup=lp, position=position, ros=ros.get(s.provider_player_id),
            remaining_games=games.get(s.pro_team_id, default_games) if s.pro_team_id else default_games,
            availability=s.availability, percent_owned=s.percent_owned,
            percent_change=s.percent_change,
        ))
    return players, names


# ---------------------------------------------------------------------------
# Lineup
# ---------------------------------------------------------------------------

def _slot(slot: int) -> str:
    return SLOT_NAMES.get(slot, str(slot))


def lineup_summary(rec: LineupRecommendation, names: dict[int, str]) -> str:
    if rec.decision == "no_action":
        return "Lineup is already optimal."
    parts = []
    starts = [(pid, to) for pid, frm, to in rec.moves if frm in (20, 21) and to not in (20, 21)]
    sits = [(pid, frm) for pid, frm, to in rec.moves if frm not in (20, 21) and to in (20, 21)]
    for pid, to in starts:
        parts.append(f"Start {names.get(pid, pid)} at {_slot(to)}")
    for pid, _ in sits:
        parts.append(f"bench {names.get(pid, pid)}")
    if not parts and rec.close_calls:
        c = rec.close_calls[0]
        parts.append(f"Close call: {names.get(c.starter_id)} over {names.get(c.alternative_id)} by {c.margin}")
    if rec.unfilled_slots:
        parts.append("no startable player for " + ", ".join(_slot(s) for s in rec.unfilled_slots))
    text = "; ".join(parts) or "Minor slot changes"
    return f"{text} (+{rec.gain} projected)." if rec.gain > 0 else f"{text}."


def run_lineup(ctx: _Context) -> EngineResult:
    players, names = team_players(ctx, ctx.team_id)
    rec = lineup_engine.optimize([p.lineup for p in players], ctx.slot_counts)
    by_id = {p.pid: p for p in players}
    payload = {
        "moves": [
            {"player_id": pid, "name": names.get(pid), "from": _slot(frm), "to": _slot(to)}
            for pid, frm, to in rec.moves
        ],
        "assignment": {str(pid): _slot(slot) for pid, slot in rec.assignment.items()},
        "current_points": rec.current_points,
        "optimal_points": rec.optimal_points,
        "gain": rec.gain,
        "ineligible_starters": [names.get(pid) for pid in rec.ineligible_starters],
        "unfilled_slots": [_slot(s) for s in rec.unfilled_slots],
        "close_calls": [
            {**asdict(c), "starter": names.get(c.starter_id), "alternative": names.get(c.alternative_id),
             "slot": _slot(c.slot)}
            for c in rec.close_calls
        ],
        "needs_review": bool(rec.close_calls),
    }
    evidence: list[dict[str, Any]] = []
    for p in players:
        lp = p.lineup
        evidence.append({
            "kind": "projection", "key": names[p.pid], "source": "espn",
            "value": {"player_id": p.pid, "week_projection": lp.projection, "slot": _slot(lp.current_slot),
                      "injury_status": lp.injury_status, "on_bye": lp.on_bye, "locked": lp.locked,
                      "flags": rec.flags.get(p.pid, [])},
        })
    confidence = "high" if all(by_id[pid].lineup.projection is not None for pid, _, _ in rec.moves) else "medium"
    record = {
        "provider_team_id": ctx.team_id, "kind": "lineup", "scoring_period": ctx.period,
        "as_of_ts": ctx.as_of_ts, "decision": rec.decision, "summary": lineup_summary(rec, names),
        "payload": payload, "alternatives": None, "confidence": confidence,
        "engine_version": lineup_engine.ENGINE_VERSION,
    }
    return EngineResult([record], [evidence], rec)


# ---------------------------------------------------------------------------
# Waivers
# ---------------------------------------------------------------------------

def _preferences(ctx: _Context) -> tuple[bool, bool, frozenset[int]]:
    prefs = ctx.repo.preferences(ctx.user_id, ctx.league_id)

    def enabled(key: str) -> bool:
        p = prefs.get(key)
        return bool(p and p.value and p.weight > 0)

    protected = frozenset(
        int(k.split(":", 1)[1]) for k, p in prefs.items() if k.startswith("protected:") and p.value
    )
    return enabled("streams_dst"), enabled("stash_injured"), protected


def waiver_context(ctx: _Context, roster: list[PoolPlayer]) -> WaiverContext:
    repo = ctx.repo
    acquisition = ctx.settings.get("acquisitionSettings") or {}
    faab = repo.faab_remaining(ctx.league_id, ctx.season, ctx.period)
    comparables = [
        Comparable(POSITION_NAMES.get(pos or -1, "UNK"), bid, owned)
        for pos, bid, owned in repo.faab_comparables(ctx.league_id, ctx.season, ctx.period)
    ]
    competitors = []
    for team in repo.teams(ctx.league_id, ctx.season):
        if team.provider_team_id == ctx.team_id:
            continue
        players, _ = team_players(ctx, team.provider_team_id)
        floor = waiver_engine.starter_floor(
            [p.lineup for p in players], {p.pid: p.position for p in players}, ctx.slot_counts
        )
        competitors.append(Competitor(team.provider_team_id, faab.get(team.provider_team_id), floor))
    streams, stash, protected = _preferences(ctx)
    return WaiverContext(
        slot_counts=ctx.slot_counts,
        roster=roster,
        remaining_weeks=max(1, ctx.final_period - ctx.period + 1),
        faab_remaining=faab.get(ctx.team_id) if acquisition.get("isUsingAcquisitionBudget") else None,
        min_bid=int(acquisition.get("minimumBid") or 0),
        comparables=comparables,
        competitors=competitors,
        streams_dst=streams,
        stash_injured=stash,
        protected_ids=protected,
    )


def waiver_summary(o: WaiverOption, names: dict[int, str]) -> str:
    verb = {"stream": "Stream", "stash": "Stash", "upgrade": "Add"}[o.kind]
    text = f"{verb} {names.get(o.add_id, o.add_id)} ({o.position})"
    if o.drop_id is not None:
        text += f", drop {names.get(o.drop_id, o.drop_id)}"
    if o.bid is not None:
        text += f", bid ${o.bid.amount} (range ${o.bid.low}–${o.bid.high})"
    elif o.availability == "free_agent":
        text += ", free agent: add now"
    return f"{text}. {o.delta_next:+.1f} next week, {o.delta_ros:+.1f} rest of season."


def run_waivers(ctx: _Context) -> EngineResult:
    roster, roster_names = team_players(ctx, ctx.team_id)
    pool, pool_names = pool_players(ctx)
    if not pool:
        raise EngineError(f"No free-agent pool snapshot for {ctx.season} week {ctx.period}; run sync first.")
    names = {**roster_names, **pool_names}
    wctx = waiver_context(ctx, roster)
    options = waiver_engine.evaluate(pool, wctx)
    by_id = {p.pid: p for p in pool + roster}

    def option_payload(o: WaiverOption) -> dict[str, Any]:
        add = by_id[o.add_id]
        drop = by_id.get(o.drop_id) if o.drop_id is not None else None
        return {
            "add": {"player_id": o.add_id, "name": names.get(o.add_id), "position": o.position,
                    "availability": o.availability, "week_projection": add.lineup.projection,
                    "ros": add.ros, "percent_owned": add.percent_owned,
                    "percent_change": add.percent_change, "injury_status": add.lineup.injury_status},
            "drop": None if drop is None else {
                "player_id": drop.pid, "name": names.get(drop.pid), "position": drop.position,
                "week_projection": drop.lineup.projection, "ros": drop.ros,
                "injury_status": drop.lineup.injury_status},
            "kind": o.kind, "delta_next": o.delta_next, "delta_ros": o.delta_ros, "score": o.score,
            "bid": asdict(o.bid) if o.bid else None, "reasons": o.reasons,
        }

    records: list[dict[str, Any]] = []
    evidence: list[list[dict[str, Any]]] = []
    base_evidence = [
        {"kind": "faab", "key": "user_faab_remaining", "value": wctx.faab_remaining, "source": "derived"},
        {"kind": "faab_comparables", "key": "executed_claims", "value": len(wctx.comparables), "source": "league_history"},
        {"kind": "preference", "key": "streams_dst", "value": wctx.streams_dst, "source": "user_preferences"},
        {"kind": "preference", "key": "stash_injured", "value": wctx.stash_injured, "source": "user_preferences"},
    ]
    for o in options[:MAX_STORED_WAIVER_OPTIONS]:
        payload = option_payload(o)
        ev = list(base_evidence)
        if o.bid:
            ev.append({"kind": "competitor", "key": "teams_with_need", "value": o.bid.competitors, "source": "derived"})
        records.append({
            "provider_team_id": ctx.team_id, "kind": "waiver", "scoring_period": ctx.period,
            "as_of_ts": ctx.as_of_ts, "decision": o.decision, "summary": waiver_summary(o, names),
            "payload": payload, "alternatives": None,
            "confidence": o.bid.confidence if o.bid else "medium",
            "engine_version": waiver_engine.ENGINE_VERSION,
        })
        evidence.append(ev)

    if not any(o.decision == "notify" for o in options):
        records.append({
            "provider_team_id": ctx.team_id, "kind": "waiver", "scoring_period": ctx.period,
            "as_of_ts": ctx.as_of_ts, "decision": "no_action",
            "summary": "No waiver move clears the notification bar.",
            "payload": {"candidates_evaluated": len(pool), "options_found": len(options)},
            "alternatives": [option_payload(o) for o in options[:3]],
            "confidence": None, "engine_version": waiver_engine.ENGINE_VERSION,
        })
        evidence.append(list(base_evidence))
    return EngineResult(records, evidence, options)


def save(ctx: _Context, result: EngineResult) -> list[str]:
    return [
        ctx.repo.save_recommendation(ctx.user_id, ctx.league_id, ctx.season, rec, ev)
        for rec, ev in zip(result.records, result.evidence, strict=True)
    ]
