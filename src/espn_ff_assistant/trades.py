"""Pure deterministic grading of explicit offers, not partner acceptance odds."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from itertools import combinations
from math import sqrt

from .lineup import BENCH_SLOT, IR_SLOT, optimize
from .waivers import INJURED_STATUSES, PoolPlayer, roster_value, select_candidates

ENGINE_VERSION = "trades-1"

# Placeholders until calibrated by backtests.
ROS_CV = 0.20
INJURED_ROS_CV = 0.40
MIN_GAIN = 2.0


@dataclass(frozen=True)
class TradeOffer:
    user_team: int
    partner_team: int
    give_ids: tuple[int, ...]
    receive_ids: tuple[int, ...]
    user_drops: tuple[int, ...] = ()
    partner_drops: tuple[int, ...] = ()


@dataclass
class TradeContext:
    slot_counts: dict[int, int]
    user_roster: list[PoolPlayer]
    partner_roster: list[PoolPlayer]
    free_agents: list[PoolPlayer]
    remaining_weeks: int
    current_week: int
    trade_deadline_week: int
    roster_size_limit: int
    position_limits: dict[str, int] = field(default_factory=dict)
    protected_ids: frozenset[int] = frozenset()


@dataclass
class SideValue:
    delta_next: float
    delta_ros: float
    before_fills: list[int]
    after_fills: list[int]
    assumed_drops: list[int]


@dataclass
class TradeGrade:
    grade: str
    violations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    user: SideValue | None = None
    partner: SideValue | None = None
    user_ros_sd: float | None = None
    alternative_ros_gain: float | None = None
    alternative_add: int | None = None
    alternative_drop: int | None = None
    net_alternative_ros_gain: float | None = None
    engine_version: str = ENGINE_VERSION


def _within_positions(roster: list[PoolPlayer], ctx: TradeContext) -> bool:
    counts = Counter(p.position for p in roster)
    return all(counts[pos] <= limit for pos, limit in ctx.position_limits.items())


def _droppable(p: PoolPlayer, ctx: TradeContext) -> bool:
    return not p.lineup.locked and p.lineup.current_slot != IR_SLOT and p.pid not in ctx.protected_ids


def _value(roster: list[PoolPlayer], ctx: TradeContext) -> tuple[float, float]:
    return roster_value(roster, ctx.slot_counts, ctx.remaining_weeks)


def _value_key(roster: list[PoolPlayer], ctx: TradeContext) -> tuple[float, float]:
    next_week, ros = _value(roster, ctx)
    return ros, next_week


def _fill(roster: list[PoolPlayer], pool: list[PoolPlayer], ctx: TradeContext
          ) -> tuple[list[PoolPlayer], list[int]]:
    roster = list(roster)
    fills: list[int] = []
    while len(roster) < ctx.roster_size_limit:
        ids = {p.pid for p in roster}
        choices = [p for p in pool if p.pid not in ids
                   and _within_positions([*roster, p], ctx)]
        if not choices:
            break
        best = max(choices, key=lambda p: (*_value_key([*roster, p], ctx), -p.pid))
        if _value_key([*roster, best], ctx) <= _value_key(roster, ctx):
            break
        roster.append(best)
        fills.append(best.pid)
    return roster, fills


def _contributions(roster: list[PoolPlayer], ctx: TradeContext) -> dict[int, float]:
    views = [replace(p.lineup, projection=p.ros_rate, injury_status=None,
                     on_bye=False, locked=False, current_slot=BENCH_SLOT) for p in roster]
    assignment = optimize(views, ctx.slot_counts).assignment
    return {p.pid: p.ros_rate * ctx.remaining_weeks for p in roster
            if assignment.get(p.pid, BENCH_SLOT) not in {BENCH_SLOT, IR_SLOT}}


def _legality(offer: TradeOffer, ctx: TradeContext) -> tuple[list[str], list[str]]:
    violations: list[str] = []
    notes: list[str] = []
    user = {p.pid: p for p in ctx.user_roster}
    partner = {p.pid: p for p in ctx.partner_roster}
    give, receive = set(offer.give_ids), set(offer.receive_ids)
    all_ids = [*offer.give_ids, *offer.receive_ids, *offer.user_drops, *offer.partner_drops]
    if (not give or not receive or len(all_ids) != len(set(all_ids))
            or not give <= user.keys() or not receive <= partner.keys()
            or not set(offer.user_drops) <= user.keys()
            or not set(offer.partner_drops) <= partner.keys()
            or user.keys() & partner.keys() or offer.user_team == offer.partner_team):
        violations.append("ownership")
    if ctx.current_week > ctx.trade_deadline_week:
        violations.append("deadline")
    moving = [user[pid] for pid in give | set(offer.user_drops) if pid in user]
    moving += [partner[pid] for pid in receive | set(offer.partner_drops) if pid in partner]
    if any(p.lineup.locked for p in moving):
        violations.append("locked")
    notes.extend(f"IR player {p.pid} moves; receiving IR placement retained" for p in moving
                 if p.lineup.current_slot == IR_SLOT)
    return violations, notes


def _post_trade_rosters(offer: TradeOffer, ctx: TradeContext
                        ) -> tuple[list[PoolPlayer], list[PoolPlayer], list[int], list[str]]:
    user = {p.pid: p for p in ctx.user_roster}
    partner = {p.pid: p for p in ctx.partner_roster}
    give, receive = set(offer.give_ids), set(offer.receive_ids)
    post_user = [p for p in ctx.user_roster if p.pid not in give | set(offer.user_drops)]
    post_user += [partner[pid] for pid in sorted(receive)]
    post_partner = [p for p in ctx.partner_roster if p.pid not in receive | set(offer.partner_drops)]
    post_partner += [user[pid] for pid in sorted(give)]
    assumed_drops: list[int] = []
    overflow = len(post_user) - ctx.roster_size_limit
    if overflow > 0 and len(receive) > len(give) and not offer.user_drops:
        choices = []
        for drops in combinations(sorted(p.pid for p in post_user if _droppable(p, ctx)), overflow):
            kept = [p for p in post_user if p.pid not in drops]
            if _within_positions(kept, ctx):
                choices.append((kept, drops))
        if choices:
            post_user, drops = max(choices, key=lambda x: (*_value_key(x[0], ctx),
                                                          tuple(-pid for pid in x[1])))
            assumed_drops = list(drops)
    violations: list[str] = []
    if max(len(post_user), len(post_partner)) > ctx.roster_size_limit:
        violations.append("roster_size")
    if not all(_within_positions(r, ctx) for r in (post_user, post_partner)):
        violations.append("position_limit")
    return post_user, post_partner, assumed_drops, violations


def _alternative(roster: list[PoolPlayer], pool: list[PoolPlayer], ctx: TradeContext
                 ) -> tuple[float, int | None, int | None]:
    base_ros = _value(roster, ctx)[1]
    alternative, add_id, drop_id = 0.0, None, None
    ids = {p.pid for p in roster}
    drops = [p for p in roster if _droppable(p, ctx)]
    for add in pool:
        if add.pid in ids:
            continue
        for drop in ([None] if len(roster) < ctx.roster_size_limit else []) + drops:
            changed = [p for p in roster if drop is None or p.pid != drop.pid] + [add]
            if not _within_positions(changed, ctx):
                continue
            gain = _value(changed, ctx)[1] - base_ros
            if gain > alternative:
                alternative, add_id, drop_id = gain, add.pid, drop.pid if drop else None
    return alternative, add_id, drop_id


def _uncertainty(before: list[PoolPlayer], after: list[PoolPlayer], ctx: TradeContext) -> float:
    bc, ac = _contributions(before, ctx), _contributions(after, ctx)
    players = {p.pid: p for p in [*before, *after]}
    variance = 0.0
    for pid in bc.keys() | ac.keys():
        cv = (INJURED_ROS_CV if (players[pid].lineup.injury_status or "").upper()
              in INJURED_STATUSES else ROS_CV)
        variance += ((ac.get(pid, 0) - bc.get(pid, 0)) * cv) ** 2
    return sqrt(variance)


def _side_value(before: list[PoolPlayer], after: list[PoolPlayer], bf: list[int],
                af: list[int], drops: list[int], ctx: TradeContext) -> SideValue:
    bn, br = _value(before, ctx)
    an, ar = _value(after, ctx)
    return SideValue(an - bn, ar - br, bf, af, drops)


def grade_trade(offer: TradeOffer, ctx: TradeContext) -> TradeGrade:
    """Grade both lineup deltas with symmetric FA access and independent ROS risk.

    User overflow from receiving extra players may be auto-dropped; partner
    overflow requires explicit drops. Fills are independent counterfactuals,
    not promises that both teams can acquire the same free agent.
    """
    violations, notes = _legality(offer, ctx)
    if violations:
        return TradeGrade("illegal", violations, notes)
    post_user, post_partner, assumed_drops, violations = _post_trade_rosters(offer, ctx)
    if violations:
        return TradeGrade("illegal", violations, notes)
    occupied = {p.pid for p in [*ctx.user_roster, *ctx.partner_roster]}
    available = sorted({p.pid: p for p in ctx.free_agents if p.pid not in occupied
                        and p.availability == "free_agent" and not p.lineup.locked
                        and p.lineup.current_slot != IR_SLOT}.values(), key=lambda p: p.pid)
    pool = sorted(select_candidates(available), key=lambda p: p.pid)
    before_user, ub = _fill(ctx.user_roster, pool, ctx)
    after_user, ua = _fill(post_user, pool, ctx)
    before_partner, pb = _fill(ctx.partner_roster, pool, ctx)
    after_partner, pa = _fill(post_partner, pool, ctx)
    uv = _side_value(before_user, after_user, ub, ua, assumed_drops, ctx)
    pv = _side_value(before_partner, after_partner, pb, pa, [], ctx)
    alternative, add_id, drop_id = _alternative(before_user, pool, ctx)
    sd = _uncertainty(before_user, after_user, ctx)
    grade = "toss-up" if abs(uv.delta_ros) <= max(sd, MIN_GAIN) else (
        "favorable" if uv.delta_ros > 0 else "unfavorable")
    return TradeGrade(grade, notes=notes, user=uv, partner=pv, user_ros_sd=sd,
                      alternative_ros_gain=alternative, alternative_add=add_id,
                      alternative_drop=drop_id, net_alternative_ros_gain=uv.delta_ros - alternative)
