"""Find and assess trades: feasible packages, quality vs plausibility, why now.

Generates a small set of legal offers per rival team by enumerating capped
give/receive combinations and grading each with `grade_trade` (T1). User-side
quality (ROS gain net of the best no-trade alternative) is ranked separately
from counterparty plausibility, a heuristic index built from the partner's own
grade delta, their measured position depth, linked trade-response rates and
package tendencies from rival signals (T2), and ledger opportunities (F4).

Plausibility is an ordinal index with named components, never a calibrated
acceptance probability: a package the user clearly wins but the partner clearly
loses surfaces as high-quality and implausible, not as "likely". Nothing here
writes to ESPN.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from .lineup import IR_SLOT
from .trades import TradeContext, TradeGrade, TradeOffer, grade_trade
from .waivers import PoolPlayer

ENGINE_VERSION = "trade-discovery-1"

MAX_SIDE = 2                    # largest give or receive package
MAX_PER_SIDE = 6                # players considered per side per partner
MAX_IDEAS = 15                  # proposals kept per partner team
MIN_QUALITY = 1.0               # minimum user ROS delta to surface

# Plausibility component weights; a heuristic index, not a probability.
W_PARTNER_GAIN = 0.35
W_NEED_FIT = 0.25
W_ACCEPT_RATE = 0.20
W_PACKAGE_FIT = 0.10
W_URGENCY = 0.10
PLAUSIBLE_THRESHOLD = 0.60
MIXED_THRESHOLD = 0.35


@dataclass(frozen=True)
class RivalTeam:
    team_id: int
    roster: list[PoolPlayer]
    name: str = ""


@dataclass(frozen=True)
class PartnerSignals:
    """Distilled rival_signals rows for one team; None means no usable evidence."""

    accept_rate: float | None = None
    accept_rate_basis: str = "neutral"
    one_for_one_share: float | None = None
    position_depth: dict[str, float | None] = field(default_factory=dict)
    games_back_of_playoff_line: float | None = None


@dataclass(frozen=True)
class OpportunityHint:
    """An open ledger opportunity relevant to a team (starter_out, player_dropped)."""

    kind: str
    team_id: int
    player_id: int
    detail: str = ""


@dataclass(frozen=True)
class DiscoveryContext:
    slot_counts: dict[int, int]
    user_team: RivalTeam
    rivals: list[RivalTeam]
    free_agents: list[PoolPlayer]
    remaining_weeks: int
    current_week: int
    trade_deadline_week: int
    roster_size_limit: int
    position_limits: dict[str, int] = field(default_factory=dict)
    protected_ids: frozenset[int] = frozenset()
    signals: dict[int, PartnerSignals] = field(default_factory=dict)
    opportunities: list[OpportunityHint] = field(default_factory=list)


@dataclass
class Plausibility:
    score: float
    label: str                    # plausible | mixed | implausible
    components: dict[str, float] = field(default_factory=dict)


@dataclass
class TradeIdea:
    partner_team: int
    offer: TradeOffer
    grade: TradeGrade
    quality: float                # user ROS delta net of best no-trade alternative
    plausibility: Plausibility
    why_now: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    engine_version: str = ENGINE_VERSION


def _tradeable(players: list[PoolPlayer], ctx: DiscoveryContext) -> list[PoolPlayer]:
    return sorted(
        (p for p in players if not p.lineup.locked and p.pid not in ctx.protected_ids),
        key=lambda p: (-p.ros_rate, p.pid),
    )[:MAX_PER_SIDE]


def _packages(
    give_pool: list[PoolPlayer], receive_pool: list[PoolPlayer]
) -> list[tuple[tuple[int, ...], tuple[int, ...]]]:
    gives = [c for n in range(1, MAX_SIDE + 1) for c in itertools.combinations(give_pool, n)]
    receives = [c for n in range(1, MAX_SIDE + 1) for c in itertools.combinations(receive_pool, n)]
    return [
        (tuple(p.pid for p in give), tuple(p.pid for p in recv))
        for give in gives
        for recv in receives
    ]


def _need_fit(receive_positions: list[str], depth: dict[str, float | None]) -> float:
    """Share of received positions where the partner is short (depth < 0)."""
    if not receive_positions:
        return 0.0
    known = [p for p in receive_positions if depth.get(p) is not None]
    if not known:
        return 0.5  # no measured depth: neutral
    holes = sum(1 for p in known if depth[p] < 0)
    return holes / len(known)


def _score_plausibility(
    grade: TradeGrade,
    offer: TradeOffer,
    receive_positions: list[str],
    signals: PartnerSignals | None,
    ctx: DiscoveryContext,
) -> Plausibility:
    components: dict[str, float] = {}
    partner_gain = grade.partner.delta_ros if grade.partner else 0.0
    components["partner_ros_gain"] = 1.0 if partner_gain > 0 else (
        0.4 if partner_gain == 0 else 0.0
    )
    depth = signals.position_depth if signals else {}
    components["need_fit"] = _need_fit(receive_positions, depth)
    if signals and signals.accept_rate is not None:
        components["accept_rate"] = max(0.0, min(1.0, signals.accept_rate))
    else:
        components["accept_rate"] = 0.5  # unlinked/no evidence: neutral, not declined
    sizes = (len(offer.give_ids), len(offer.receive_ids))
    if signals and signals.one_for_one_share is not None:
        share = signals.one_for_one_share
        components["package_fit"] = share if sizes == (1, 1) else 1.0 - share
    else:
        components["package_fit"] = 0.5
    gb = signals.games_back_of_playoff_line if signals else None
    weeks_left = ctx.trade_deadline_week - ctx.current_week
    urgency = 0.0
    if gb is not None and gb > 0:
        urgency += 0.5
    if 0 < weeks_left <= 3:
        urgency += 0.5
    components["urgency"] = min(1.0, urgency)
    score = (
        W_PARTNER_GAIN * components["partner_ros_gain"]
        + W_NEED_FIT * components["need_fit"]
        + W_ACCEPT_RATE * components["accept_rate"]
        + W_PACKAGE_FIT * components["package_fit"]
        + W_URGENCY * components["urgency"]
    )
    label = (
        "plausible" if score >= PLAUSIBLE_THRESHOLD
        else "mixed" if score >= MIXED_THRESHOLD
        else "implausible"
    )
    return Plausibility(round(score, 3), label, components)


def _why_now(partner: RivalTeam, ctx: DiscoveryContext) -> list[str]:
    reasons: list[str] = []
    for hint in ctx.opportunities:
        if hint.team_id != partner.team_id:
            continue
        if hint.kind == "starter_out":
            reasons.append(f"rival starter {hint.player_id} is out{': ' + hint.detail if hint.detail else ''}")
        elif hint.kind == "player_dropped":
            reasons.append(f"rival dropped {hint.player_id}, now available{': ' + hint.detail if hint.detail else ''}")
        else:
            reasons.append(hint.detail or hint.kind)
    weeks_left = ctx.trade_deadline_week - ctx.current_week
    if weeks_left <= 0:
        pass
    elif weeks_left <= 2:
        reasons.append(f"trade deadline in {weeks_left} week{'s' if weeks_left != 1 else ''}")
    signals = ctx.signals.get(partner.team_id)
    if signals and signals.position_depth:
        holes = [pos for pos, d in signals.position_depth.items() if d is not None and d < 0]
        if holes:
            reasons.append(f"rival short at {', '.join(sorted(holes))}")
    return reasons


def discover(ctx: DiscoveryContext) -> list[TradeIdea]:
    """Rank legal, user-favorable packages; quality and plausibility stay separate."""
    if ctx.current_week > ctx.trade_deadline_week:
        return []
    ideas: list[TradeIdea] = []
    give_pool = _tradeable(ctx.user_team.roster, ctx)
    if not give_pool:
        return []
    for partner in sorted(ctx.rivals, key=lambda r: r.team_id):
        receive_pool = _tradeable(partner.roster, ctx)
        if not receive_pool:
            continue
        trade_ctx = TradeContext(
            slot_counts=ctx.slot_counts,
            user_roster=ctx.user_team.roster,
            partner_roster=partner.roster,
            free_agents=ctx.free_agents,
            remaining_weeks=ctx.remaining_weeks,
            current_week=ctx.current_week,
            trade_deadline_week=ctx.trade_deadline_week,
            roster_size_limit=ctx.roster_size_limit,
            position_limits=dict(ctx.position_limits),
            protected_ids=ctx.protected_ids,
        )
        signals = ctx.signals.get(partner.team_id)
        user_by_id = {p.pid: p for p in ctx.user_team.roster}
        partner_ideas: list[TradeIdea] = []
        for give_ids, receive_ids in _packages(give_pool, receive_pool):
            offers = [TradeOffer(ctx.user_team.team_id, partner.team_id, give_ids, receive_ids)]
            # grade_trade requires explicit partner drops for partner overflow;
            # try the partner's droppable players when they receive extra players.
            overflow = len(partner.roster) + len(give_ids) - len(receive_ids) - ctx.roster_size_limit
            if overflow > 0:
                # Partner gives more than they get on a full roster; legal only
                # with explicit drops. Try each droppable-player combination.
                droppable = [p.pid for p in _tradeable(partner.roster, ctx)
                             if p.pid not in receive_ids and p.lineup.current_slot != IR_SLOT]
                offers += [
                    TradeOffer(ctx.user_team.team_id, partner.team_id, give_ids, receive_ids,
                               partner_drops=tuple(sorted(drop)))
                    for drop in itertools.combinations(droppable, overflow)
                ]
            best: tuple[TradeOffer, TradeGrade] | None = None
            for offer in offers:
                grade = grade_trade(offer, trade_ctx)
                # Only clear user gains surface; toss-ups carry too much uncertainty to pitch.
                if grade.grade != "favorable" or grade.user is None:
                    continue
                quality = grade.net_alternative_ros_gain if grade.net_alternative_ros_gain is not None else grade.user.delta_ros
                if quality < MIN_QUALITY:
                    continue
                # Equal user gains tie-break on partner gain so a legal variant
                # that drops a starter loses to one that drops a bench piece.
                if best is None or (
                    grade.user.delta_ros, grade.partner.delta_ros
                ) > (best[1].user.delta_ros, best[1].partner.delta_ros):
                    best = (offer, grade)
            if best is None:
                continue
            offer, grade = best
            quality = grade.net_alternative_ros_gain if grade.net_alternative_ros_gain is not None else grade.user.delta_ros
            # Need fit scores the positions the partner would acquire (give_ids),
            # not the ones they give up.
            acquire_positions = sorted({user_by_id[pid].position for pid in give_ids if pid in user_by_id})
            partner_ideas.append(TradeIdea(
                partner_team=partner.team_id,
                offer=offer,
                grade=grade,
                quality=round(quality, 2),
                plausibility=_score_plausibility(grade, offer, acquire_positions, signals, ctx),
                why_now=_why_now(partner, ctx),
                notes=list(grade.notes),
            ))
        partner_ideas.sort(key=lambda i: (-i.quality, i.offer.give_ids, i.offer.receive_ids))
        ideas.extend(partner_ideas[:MAX_IDEAS])
    ideas.sort(key=lambda i: (-i.quality, i.partner_team, i.offer.give_ids, i.offer.receive_ids))
    return ideas


def summarize(ideas: list[TradeIdea]) -> str:
    if not ideas:
        return "No favorable, legal packages found."
    lines = []
    for i in ideas:
        give = ", ".join(map(str, i.offer.give_ids))
        recv = ", ".join(map(str, i.offer.receive_ids))
        grade = i.grade
        lines.append(
            f"Team {i.partner_team}: give [{give}] for [{recv}] — "
            f"user ROS {grade.user.delta_ros:+.1f} (net of best waiver move {quality_note(grade)}), "
            f"partner ROS {grade.partner.delta_ros:+.1f}; "
            f"quality {i.quality:+.1f}, plausibility {i.plausibility.label} ({i.plausibility.score})"
        )
        moves = []
        if i.grade.user.assumed_drops:
            moves.append(f"user also drops {', '.join(map(str, i.grade.user.assumed_drops))}")
        if i.offer.partner_drops:
            moves.append(f"partner drops {', '.join(map(str, i.offer.partner_drops))}")
        if i.grade.user.after_fills != i.grade.user.before_fills:
            moves.append(
                f"free-agent fills {i.grade.user.before_fills} -> {i.grade.user.after_fills}"
            )
        if moves:
            lines.append(f"  roster moves: {'; '.join(moves)}")
        if i.why_now:
            lines.append(f"  why now: {'; '.join(i.why_now)}")
        for note in i.notes:
            lines.append(f"  note: {note}")
    return "\n".join(lines)


def quality_note(grade: TradeGrade) -> str:
    if grade.alternative_ros_gain:
        return f"+{grade.alternative_ros_gain:.1f} available without trading"
    return "none better on waivers"
