"""Outcome scoring for saved recommendations and point-in-time backtests.

Scoring functions are pure. The backtest runners read through the repository
and only use data at or before each evaluated week: the week's archived
projection and roster, then that week's actuals for scoring.

Historical backfilled injury status and ownership are end-of-season values, so
the lineup backtest ignores injury status and the FAAB backtest does not use
ownership tiers.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .lineup import BENCH_SLOT, NON_STARTING_SLOTS, SLOT_NAMES, LineupPlayer, optimize
from .manager_stats import POSITION_NAMES
from .repository import Repository

_SLOT_BY_NAME = {v: k for k, v in SLOT_NAMES.items()}


# ---------------------------------------------------------------------------
# Outcome scoring (pure)
# ---------------------------------------------------------------------------

def starters(slots: dict[int, int]) -> set[int]:
    return {pid for pid, slot in slots.items() if slot not in NON_STARTING_SLOTS}


def points(players: set[int], actuals: dict[int, float]) -> float:
    return round(sum(actuals.get(pid, 0.0) for pid in players), 2)


def lineup_outcome(
    recommended: dict[int, int],
    baseline: dict[int, int],
    final: dict[int, int],
    actuals: dict[int, float],
) -> dict[str, Any]:
    """Compare the recommended, as-of-recommendation, and finally-set lineups on actual points."""
    rec, base, fin = starters(recommended), starters(baseline), starters(final)
    if fin == rec:
        observed = "followed"
    elif fin == base and rec != base:
        observed = "not_followed"
    else:
        observed = "modified"
    return {
        "observed_action": observed,
        "recommended_points": points(rec, actuals),
        "actual_points": points(fin, actuals),
        "baseline_points": points(base, actuals),
        "detail": {"recommended_minus_actual": round(points(rec, actuals) - points(fin, actuals), 2)},
    }


def waiver_outcome(
    add_id: int,
    drop_id: int | None,
    later_roster: set[int],
    actuals_by_week: list[dict[int, float]],
) -> dict[str, Any]:
    """Added player's actual points over the evaluation window minus the dropped player's."""
    add_pts = sum(week.get(add_id, 0.0) for week in actuals_by_week)
    drop_pts = sum(week.get(drop_id, 0.0) for week in actuals_by_week) if drop_id is not None else 0.0
    return {
        "observed_action": "followed" if add_id in later_roster else "not_followed",
        "recommended_points": round(add_pts, 2),
        "actual_points": None,
        "baseline_points": round(drop_pts, 2),
        "detail": {"weeks": len(actuals_by_week), "add_minus_drop": round(add_pts - drop_pts, 2)},
    }


def score_saved(repo: Repository, league_id: str, user_id: str, season: int, week: int, horizon: int = 3) -> int:
    """Score every saved recommendation for a completed week. Returns rows written."""
    recs = repo.recommendations(user_id, league_id, season, scoring_period=week)
    if not recs:
        return 0
    last = repo.latest_roster_period(league_id, season, 99) or week
    written = 0
    for rec in recs:
        if rec.decision == "no_action":
            continue
        full = repo.recommendation(rec.id)
        if rec.kind == "lineup":
            actuals = repo.week_actuals(league_id, season, week)
            if not actuals:
                continue
            final = {e.provider_player_id: e.lineup_slot_id for e in
                     repo.roster_at(league_id, season, week, rec.provider_team_id)}
            baseline = {
                e["value"]["player_id"]: _SLOT_BY_NAME.get(e["value"]["slot"], BENCH_SLOT)
                for e in full.evidence if e["kind"] == "projection"
            }
            recommended = {int(pid): _SLOT_BY_NAME.get(slot, BENCH_SLOT)
                           for pid, slot in rec.payload["assignment"].items()}
            outcome = lineup_outcome(recommended, baseline, final, actuals)
            outcome["evaluated_through_period"] = week
        else:
            through = min(week + horizon - 1, last)
            weeks = [repo.week_actuals(league_id, season, w) for w in range(week, through + 1)]
            weeks = [w for w in weeks if w]
            if not weeks:
                continue
            later = {e.provider_player_id for e in repo.roster_at(league_id, season, min(week, last), rec.provider_team_id)}
            drop = rec.payload.get("drop") or {}
            outcome = waiver_outcome(rec.payload["add"]["player_id"], drop.get("player_id"), later, weeks)
            outcome["evaluated_through_period"] = week + len(weeks) - 1
        repo.save_outcome(rec.id, outcome)
        written += 1
    return written


# ---------------------------------------------------------------------------
# Lineup backtest
# ---------------------------------------------------------------------------

@dataclass
class LineupBacktest:
    team_weeks: int = 0
    projected_gains: list[float] = field(default_factory=list)
    realized_gains: list[float] = field(default_factory=list)
    by_team: dict[tuple[int, int], list[float]] = field(default_factory=lambda: defaultdict(list))
    skipped_weeks: int = 0

    def threshold_table(self, thresholds: tuple[float, ...] = (0.5, 1.0, 2.0, 3.0, 5.0)) -> list[dict[str, Any]]:
        rows = []
        for t in thresholds:
            pairs = [(p, r) for p, r in zip(self.projected_gains, self.realized_gains, strict=True) if p >= t]
            realized = [r for _, r in pairs]
            rows.append({
                "min_projected_gain": t,
                "team_weeks": len(pairs),
                "mean_realized_gain": round(statistics.mean(realized), 2) if realized else None,
                "share_realized_positive": round(sum(r > 0 for r in realized) / len(realized), 3) if realized else None,
            })
        return rows


def backtest_lineups(repo: Repository, league_id: str, seasons: list[int]) -> LineupBacktest:
    result = LineupBacktest()
    for season in seasons:
        settings = repo.league_settings(league_id, season) or {}
        counts = {int(k): int(v) for k, v in
                  ((settings.get("rosterSettings") or {}).get("lineupSlotCounts") or {}).items() if int(v) > 0}
        season_obj = next((s for s in repo.seasons(league_id) if s.season == season), None)
        if not counts or not season_obj or not season_obj.final_scoring_period:
            continue
        for week in range(season_obj.first_scoring_period or 1, season_obj.final_scoring_period + 1):
            entries = repo.roster_at(league_id, season, week)
            projections = repo.week_projections(league_id, season, week)
            actuals = repo.week_actuals(league_id, season, week)
            if not entries or not projections or not actuals:
                result.skipped_weeks += 1
                continue
            statuses = repo.player_status(league_id, season, week, [e.provider_player_id for e in entries])
            by_team: dict[int, list[LineupPlayer]] = defaultdict(list)
            for e in entries:
                status = statuses.get(e.provider_player_id)
                if not status or not status.eligible_slots:
                    continue
                by_team[e.provider_team_id].append(LineupPlayer(
                    e.provider_player_id, str(e.provider_player_id), frozenset(status.eligible_slots),
                    projections.get(e.provider_player_id, 0.0),
                    current_slot=e.lineup_slot_id if e.lineup_slot_id is not None else BENCH_SLOT,
                ))
            for team_id, players in by_team.items():
                rec = optimize(players, counts)
                realized = points(starters(rec.assignment), actuals) - points(
                    starters({p.provider_player_id: p.current_slot for p in players}), actuals)
                result.team_weeks += 1
                result.projected_gains.append(rec.gain)
                result.realized_gains.append(round(realized, 2))
                result.by_team[(season, team_id)].append(round(realized, 2))
    return result


# ---------------------------------------------------------------------------
# FAAB backtest
# ---------------------------------------------------------------------------

@dataclass
class FaabBacktest:
    quantile: float
    contested: int = 0
    won: int = 0
    overpay: list[float] = field(default_factory=list)
    shortfall: list[float] = field(default_factory=list)
    uncontested_spend: list[float] = field(default_factory=list)


@dataclass
class FaabHistory:
    by_quantile: dict[float, FaabBacktest]
    contested_winning_bids: list[float]
    uncontested_winning_bids: list[float]


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def backtest_faab(
    repo: Repository,
    league_id: str,
    season_from: int,
    season_to: int,
    quantiles: tuple[float, ...] = (0.25, 0.5, 0.75, 0.9),
) -> FaabHistory:
    """For each quantile of prior winning bids at the position, would it have won each claim run?

    Failed claims in the same run are the runner-up bids. Only claims processed
    earlier feed the quantile, so the replay is point-in-time.
    """
    claims = repo.waiver_claim_bids(league_id, season_from, season_to)
    runs: dict[tuple[int, int, str], list[tuple[float, str, int | None]]] = defaultdict(list)
    for season, period, processed, player, pos, _team, bid, status in claims:
        stamp = processed.strftime("%Y%m%d%H") if processed else f"p{period}"
        runs[(season, player, stamp)].append((bid, status, pos))

    results = {q: FaabBacktest(q) for q in quantiles}
    history: dict[str, list[float]] = defaultdict(list)
    contested_bids: list[float] = []
    uncontested_bids: list[float] = []
    for _key, bids in sorted(runs.items(), key=lambda kv: (kv[0][0], kv[0][2])):
        winner = next((b for b in bids if b[1] == "EXECUTED"), None)
        if winner is None:
            continue
        losers = [b[0] for b in bids if b[1] != "EXECUTED"]
        (contested_bids if losers else uncontested_bids).append(winner[0])
        position = POSITION_NAMES.get(winner[2] or -1, "UNK")
        prior = history[position]
        if len(prior) >= 5:
            for q, r in results.items():
                suggested = _quantile(prior, q)
                if not losers:
                    r.uncontested_spend.append(suggested)
                    continue
                r.contested += 1
                runner_up = max(losers)
                # Ties are decided by waiver priority, which is not modelled: count as lost.
                if suggested > runner_up:
                    r.won += 1
                    r.overpay.append(suggested - runner_up)
                else:
                    r.shortfall.append(runner_up - suggested)
        prior.append(winner[0])
    return FaabHistory(results, contested_bids, uncontested_bids)
