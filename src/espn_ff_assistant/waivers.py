"""Deterministic waiver recommendations: add/drop pairs, FAAB bids, and a gate.

Pure functions, no I/O. Roster value is the lineup optimizer's best achievable
points, so a pickup is worth exactly what it adds to the user's lineup:

    Δ_next = V(R − Y + X, next week)  − V(R, next week)
    Δ_ros  = V(R − Y + X, ROS rates)  × weeks − V(R, ROS rates) × weeks

ROS rates are ESPN rest-of-season projections divided by each player's
remaining games. Preferences only break ties within PREFERENCE_TIE_BAND.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field, replace

from .lineup import IR_SLOT, LineupPlayer, optimal_points

ENGINE_VERSION = "waivers-1"

# Placeholders until calibrated by the backtest (handoff §7).
NOTIFY_MIN_ROS = 10.0
NOTIFY_MIN_NEXT = 3.0
MIN_DELTA = 0.5
SCORE_TIE = 0.25
MAX_RECOMMENDATIONS = 3
PREFERENCE_TIE_BAND = 1.0
# FAAB, from the 2018–2025 backtest: 30% of claims were contested; contested
# winners paid a median $15.5 vs $5.0 uncontested; the position 75th percentile
# of prior winning bids won 75% of contested runs, the 25th percentile 22%.
UNCONTESTED_QUANTILE = 0.25
CONTESTED_QUANTILE = 0.75
DEMAND_STEP = 0.15
ROS_CANDIDATES = 40
NEXT_CANDIDATES_PER_POSITION = 10
STREAM_POSITIONS = frozenset({"DST", "K"})
INJURED_STATUSES = frozenset({"OUT", "INJURY_RESERVE", "IR", "DOUBTFUL", "QUESTIONABLE", "SUSPENSION"})
OWNERSHIP_TIERS = (10.0, 30.0, 60.0)


@dataclass(frozen=True)
class PoolPlayer:
    """A rostered or available player valued for next week and rest of season."""

    lineup: LineupPlayer               # next-week view: projection, injury, bye
    position: str
    ros: float | None                  # rest-of-season projected points
    remaining_games: int
    availability: str = "rostered"     # 'rostered' | 'free_agent' | 'waivers'
    percent_owned: float | None = None
    percent_change: float | None = None

    @property
    def pid(self) -> int:
        return self.lineup.provider_player_id

    @property
    def ros_rate(self) -> float:
        if not self.ros or self.remaining_games <= 0:
            return 0.0
        return self.ros / self.remaining_games


@dataclass(frozen=True)
class Comparable:
    """A past executed claim used to price FAAB bids."""

    position: str
    bid: float
    percent_owned: float | None


@dataclass(frozen=True)
class Competitor:
    provider_team_id: int
    faab_remaining: int | None
    starter_floor: dict[str, float]    # position -> weakest starter's next-week value (0 = hole)
    bid_pct_median: float | None = None


@dataclass
class WaiverContext:
    slot_counts: dict[int, int]
    roster: list[PoolPlayer]
    remaining_weeks: int
    faab_remaining: int | None
    min_bid: int = 0
    comparables: list[Comparable] = field(default_factory=list)
    competitors: list[Competitor] = field(default_factory=list)
    streams_dst: bool = False
    stash_injured: bool = False
    protected_ids: frozenset[int] = frozenset()


@dataclass
class Bid:
    amount: int
    low: int
    high: int
    base: float
    demand: float
    competitors: list[int]
    comparables_used: int
    comparable_scope: str
    confidence: str


@dataclass
class WaiverOption:
    add_id: int
    drop_id: int | None
    position: str
    kind: str                          # 'upgrade' | 'stream' | 'stash'
    availability: str
    delta_next: float
    delta_ros: float
    score: float
    bid: Bid | None = None
    decision: str = "suppressed"
    reasons: list[str] = field(default_factory=list)


def _confidence(n: int) -> str:
    if n >= 20:
        return "high"
    if n >= 5:
        return "medium"
    if n >= 1:
        return "low"
    return "insufficient"


def _ros_view(player: PoolPlayer) -> LineupPlayer:
    """ROS lineup view: per-game rate, no weekly bye/injury adjustment (already in ESPN ROS)."""
    return replace(player.lineup, projection=player.ros_rate, injury_status=None, on_bye=False, locked=False)


def roster_value(players: list[PoolPlayer], slot_counts: dict[int, int], weeks: int) -> tuple[float, float]:
    """(next-week optimal points, ROS optimal points over `weeks`)."""
    next_week = optimal_points([p.lineup for p in players], slot_counts)
    ros = optimal_points([_ros_view(p) for p in players], slot_counts) * weeks
    return next_week, ros


def droppable(ctx: WaiverContext) -> list[PoolPlayer]:
    """Roster players that may be dropped: not on IR, not protected, not a protected stash."""
    rates = [p.ros_rate for p in ctx.roster]
    median_rate = statistics.median(rates) if rates else 0.0
    out = []
    for p in ctx.roster:
        if p.pid in ctx.protected_ids or p.lineup.current_slot == IR_SLOT:
            continue
        injured = (p.lineup.injury_status or "").upper() in INJURED_STATUSES
        if ctx.stash_injured and injured and p.ros_rate >= median_rate:
            continue
        out.append(p)
    return out


def select_candidates(pool: list[PoolPlayer]) -> list[PoolPlayer]:
    usable = [
        p for p in pool
        if p.lineup.projection is not None or (p.ros or 0) > 0
        if (p.percent_owned or 0) >= 1 or (p.ros or 0) > 0
    ]
    chosen = {p.pid: p for p in sorted(usable, key=lambda p: -(p.ros or 0))[:ROS_CANDIDATES]}
    by_position: dict[str, list[PoolPlayer]] = {}
    for p in usable:
        by_position.setdefault(p.position, []).append(p)
    for players in by_position.values():
        for p in sorted(players, key=lambda p: -(p.lineup.projection or 0))[:NEXT_CANDIDATES_PER_POSITION]:
            chosen.setdefault(p.pid, p)
    return list(chosen.values())


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def _tier(percent_owned: float | None) -> int | None:
    if percent_owned is None:
        return None
    return sum(1 for t in OWNERSHIP_TIERS if percent_owned >= t)


def suggest_bid(candidate: PoolPlayer, kind: str, ctx: WaiverContext) -> Bid | None:
    """FAAB bid from league-history comparables, scaled by competing demand."""
    if candidate.availability != "waivers" or ctx.faab_remaining is None:
        return None
    tier = _tier(candidate.percent_owned)
    scopes = [
        ("position+ownership", [c.bid for c in ctx.comparables
                                if c.position == candidate.position and tier is not None
                                and _tier(c.percent_owned) == tier]),
        ("position", [c.bid for c in ctx.comparables if c.position == candidate.position]),
        ("league", [c.bid for c in ctx.comparables]),
    ]
    scope, bids = next(((s, b) for s, b in scopes if len(b) >= 5), scopes[-1])
    if not bids:
        return Bid(ctx.min_bid, ctx.min_bid, ctx.min_bid, 0.0, 1.0, [], 0, "none", "insufficient")

    rivals = [
        c for c in ctx.competitors
        if (c.faab_remaining is None or c.faab_remaining > ctx.min_bid)
        and (candidate.lineup.projection or 0) > c.starter_floor.get(candidate.position, 0.0)
    ]
    low = _quantile(bids, UNCONTESTED_QUANTILE)
    high = _quantile(bids, CONTESTED_QUANTILE)
    if rivals and kind != "stream":
        base = high
        demand = 1 + DEMAND_STEP * (len(rivals) - 1)
    else:
        base = low
        demand = 1.0
    amount = base * demand
    cap = ctx.faab_remaining

    def clamp(x: float) -> int:
        return int(max(ctx.min_bid, min(cap, round(x))))

    return Bid(
        amount=clamp(amount), low=clamp(low), high=clamp(high * demand),
        base=round(base, 2), demand=round(demand, 2),
        competitors=sorted(c.provider_team_id for c in rivals),
        comparables_used=len(bids), comparable_scope=scope, confidence=_confidence(len(bids)),
    )


def evaluate(pool: list[PoolPlayer], ctx: WaiverContext) -> list[WaiverOption]:
    """Best add/drop pair per candidate, ranked, with notify/suppressed decisions."""
    base_next, base_ros = roster_value(ctx.roster, ctx.slot_counts, ctx.remaining_weeks)
    drops = droppable(ctx)
    options: list[WaiverOption] = []

    for cand in select_candidates(pool):
        stream = cand.position in STREAM_POSITIONS
        trials: list[tuple[float, float, PoolPlayer]] = []
        for drop in drops:
            trial = [p for p in ctx.roster if p.pid != drop.pid] + [cand]
            nxt, ros = roster_value(trial, ctx.slot_counts, ctx.remaining_weeks)
            trials.append((nxt - base_next, ros - base_ros, drop))
        if not trials:
            continue
        # Primary score, then the other horizon, then the least valuable drop (keeps depth).
        primary = (lambda t: t[0]) if stream else (lambda t: t[1])
        secondary = (lambda t: t[1]) if stream else (lambda t: t[0])
        top = max(primary(t) for t in trials)
        near = [t for t in trials if primary(t) >= top - SCORE_TIE]
        top2 = max(secondary(t) for t in near)
        near = [t for t in near if secondary(t) >= top2 - SCORE_TIE]
        d_next, d_ros, drop = min(near, key=lambda t: t[2].ros_rate)
        score = d_next if stream else d_ros
        if score <= MIN_DELTA:
            continue
        kind = "stream" if stream else ("stash" if d_next <= 0.1 and d_ros > 0 else "upgrade")
        options.append(WaiverOption(
            add_id=cand.pid, drop_id=drop.pid, position=cand.position, kind=kind,
            availability=cand.availability, delta_next=round(d_next, 2),
            delta_ros=round(d_ros, 2), score=round(score, 2),
        ))

    if options:
        top = max(o.score for o in options)
        for o in options:
            if top - o.score <= PREFERENCE_TIE_BAND:
                if ctx.streams_dst and o.kind == "stream" and o.position == "DST":
                    o.score += PREFERENCE_TIE_BAND / 2
                    o.reasons.append("preference:streams_dst")
                if ctx.stash_injured and o.kind == "stash":
                    o.score += PREFERENCE_TIE_BAND / 2
                    o.reasons.append("preference:stash")

    options.sort(key=lambda o: (-o.score, -o.delta_next))
    by_id = {p.pid: p for p in pool}
    # Recommendations must be jointly executable: each drop used once, one stream per position.
    used_drops: set[int] = set()
    streamed: set[str] = set()
    notified = 0
    for o in options:
        o.bid = suggest_bid(by_id[o.add_id], o.kind, ctx)
        meets = o.delta_ros >= NOTIFY_MIN_ROS or o.delta_next >= NOTIFY_MIN_NEXT
        conflict = o.drop_id in used_drops or (o.kind == "stream" and o.position in streamed)
        if meets and not conflict and notified < MAX_RECOMMENDATIONS:
            o.decision = "notify"
            notified += 1
            if o.drop_id is not None:
                used_drops.add(o.drop_id)
            if o.kind == "stream":
                streamed.add(o.position)
        else:
            o.decision = "suppressed"
            if conflict:
                o.reasons.append("conflicts_with_higher_ranked")
    return options


def starter_floor(players: list[LineupPlayer], positions: dict[int, str], slot_counts: dict[int, int]) -> dict[str, float]:
    """Weakest starting next-week value per position for a team (0 when a slot would go empty)."""
    from .lineup import BENCH_SLOT, optimize, valuation

    free = [replace(p, locked=False, current_slot=BENCH_SLOT) for p in players]
    rec = optimize(free, slot_counts)
    floor: dict[str, float] = {}
    for p in free:
        slot = rec.assignment.get(p.provider_player_id)
        if slot in (BENCH_SLOT, IR_SLOT):
            continue
        pos = positions.get(p.provider_player_id)
        if pos:
            value = valuation(p).value
            floor[pos] = min(floor.get(pos, value), value)
    if rec.unfilled_slots:
        for pos in {"QB", "RB", "WR", "TE", "DST", "K"} - set(floor):
            floor[pos] = 0.0
    return floor
