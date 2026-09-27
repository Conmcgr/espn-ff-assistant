"""Deterministic start/sit optimizer.

Pure functions, no I/O. Players are assigned to starting slot instances by
exact maximum-weight assignment over each player's ESPN `eligibleSlots`, so any
slot configuration (FLEX, superflex, OP, IDP) is handled without special cases.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ENGINE_VERSION = "lineup-1"

BENCH_SLOT = 20
IR_SLOT = 21
NON_STARTING_SLOTS = frozenset({BENCH_SLOT, IR_SLOT})
SLOT_NAMES = {
    0: "QB", 1: "TQB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE", 6: "TE", 7: "OP",
    8: "DT", 9: "DE", 10: "LB", 11: "DL", 12: "CB", 13: "S", 14: "DB", 15: "DP",
    16: "D/ST", 17: "K", 18: "P", 19: "HC", 20: "Bench", 21: "IR", 23: "FLEX",
}

# Placeholders until calibrated by the lineup backtest (see handoff §7).
UNAVAILABLE_STATUSES = frozenset({"OUT", "INJURY_RESERVE", "IR", "SUSPENSION", "SUSPENDED"})
INJURY_MULTIPLIERS = {"DOUBTFUL": 0.25, "QUESTIONABLE": 0.85}
CLOSE_MARGIN = 1.5
NOTIFY_MIN_GAIN = 2.0

_STICKY_BONUS = 1e-4
_EMPTY_PENALTY = 1e-3
_FORBIDDEN = 1e9


@dataclass(frozen=True)
class LineupPlayer:
    provider_player_id: int
    name: str
    eligible_slots: frozenset[int]
    projection: float | None
    injury_status: str | None = None
    on_bye: bool = False
    locked: bool = False
    current_slot: int = BENCH_SLOT


@dataclass(frozen=True)
class Valuation:
    value: float
    startable: bool
    flags: tuple[str, ...]


@dataclass(frozen=True)
class CloseCall:
    starter_id: int
    alternative_id: int
    slot: int
    margin: float


@dataclass
class LineupRecommendation:
    assignment: dict[int, int]                 # player -> recommended slot
    moves: list[tuple[int, int, int]]          # (player, from_slot, to_slot)
    current_points: float
    optimal_points: float
    gain: float
    ineligible_starters: list[int]
    unfilled_slots: list[int]
    close_calls: list[CloseCall]
    flags: dict[int, list[str]] = field(default_factory=dict)
    decision: str = "no_action"


def valuation(player: LineupPlayer) -> Valuation:
    """Effective projection after availability and injury adjustments."""
    status = (player.injury_status or "ACTIVE").upper()
    if player.on_bye:
        return Valuation(0.0, False, ("bye",))
    if status in UNAVAILABLE_STATUSES:
        return Valuation(0.0, False, (status.lower(),))
    flags: list[str] = []
    base = player.projection
    if base is None:
        flags.append("no_projection")
        base = 0.0
    multiplier = INJURY_MULTIPLIERS.get(status)
    if multiplier is not None:
        flags.append(f"injury_risk:{status.lower()}")
        base *= multiplier
    return Valuation(float(base), True, tuple(flags))


def starting_slots(slot_counts: dict[int, int]) -> list[int]:
    """Expand {slot_id: count} into slot instances, most restrictive slots first."""
    instances: list[int] = []
    for slot, count in sorted(slot_counts.items()):
        if slot not in NON_STARTING_SLOTS and count > 0:
            instances.extend([slot] * count)
    return instances


def _hungarian(cost: list[list[float]]) -> list[int]:
    """Minimum-cost assignment of every row to a distinct column (rows <= columns)."""
    n, m = len(cost), len(cost[0])
    inf = float("inf")
    u, v = [0.0] * (n + 1), [0.0] * (m + 1)
    p, way = [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], inf, 0
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j], way[j] = cur, j0
                if minv[j] < delta:
                    delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    assignment = [-1] * n
    for j in range(1, m + 1):
        if p[j]:
            assignment[p[j] - 1] = j - 1
    return assignment


def _assign(slots: list[int], players: list[LineupPlayer], values: dict[int, Valuation]) -> list[LineupPlayer | None]:
    """Best player (or None) for each slot instance."""
    if not slots:
        return []
    cost: list[list[float]] = []
    for slot in slots:
        row = []
        for player in players:
            val = values[player.provider_player_id]
            if slot not in player.eligible_slots:
                row.append(_FORBIDDEN)
            elif not val.startable:
                row.append(_EMPTY_PENALTY)
            else:
                sticky = _STICKY_BONUS if player.current_slot == slot else 0.0
                row.append(-(val.value + sticky))
        row.extend([_EMPTY_PENALTY / 2] * len(slots))
        cost.append(row)
    result: list[LineupPlayer | None] = []
    for col in _hungarian(cost):
        if col < len(players) and values[players[col].provider_player_id].startable:
            result.append(players[col])
        else:
            result.append(None)
    return result


def optimize(
    players: list[LineupPlayer],
    slot_counts: dict[int, int],
    *,
    close_margin: float = CLOSE_MARGIN,
    notify_min_gain: float = NOTIFY_MIN_GAIN,
) -> LineupRecommendation:
    values = {p.provider_player_id: valuation(p) for p in players}
    slots = starting_slots(slot_counts)

    # Locked players keep their slot; IR-slot players need a roster move first.
    remaining = list(slots)
    assignment: dict[int, int] = {}
    for p in players:
        if p.locked or p.current_slot == IR_SLOT:
            assignment[p.provider_player_id] = p.current_slot
            if p.current_slot in remaining:
                remaining.remove(p.current_slot)
    movable = [p for p in players if p.provider_player_id not in assignment]

    chosen = _assign(remaining, movable, values)
    unfilled: list[int] = []
    for slot, player in zip(remaining, chosen, strict=True):
        if player is None:
            unfilled.append(slot)
        else:
            assignment[player.provider_player_id] = slot
    for p in movable:
        assignment.setdefault(p.provider_player_id, BENCH_SLOT)

    def points(slot_of: dict[int, int]) -> float:
        return sum(
            values[p.provider_player_id].value
            for p in players
            if slot_of.get(p.provider_player_id) not in NON_STARTING_SLOTS
        )

    current = {p.provider_player_id: p.current_slot for p in players}
    current_points = points(current)
    optimal_points = points(assignment)
    gain = optimal_points - current_points

    moves = [
        (p.provider_player_id, p.current_slot, assignment[p.provider_player_id])
        for p in players
        if assignment[p.provider_player_id] != p.current_slot
    ]
    ineligible = [
        p.provider_player_id
        for p in players
        if p.current_slot not in NON_STARTING_SLOTS and not values[p.provider_player_id].startable
    ]

    close_calls: list[CloseCall] = []
    bench = [
        p for p in movable
        if assignment[p.provider_player_id] == BENCH_SLOT and values[p.provider_player_id].startable
    ]
    for p in movable:
        slot = assignment[p.provider_player_id]
        if slot in NON_STARTING_SLOTS:
            continue
        starter_value = values[p.provider_player_id].value
        alternatives = [b for b in bench if slot in b.eligible_slots]
        if not alternatives:
            continue
        best = max(alternatives, key=lambda b: values[b.provider_player_id].value)
        margin = starter_value - values[best.provider_player_id].value
        if 0 <= margin < close_margin:
            close_calls.append(CloseCall(p.provider_player_id, best.provider_player_id, slot, round(margin, 2)))

    flags = {pid: list(v.flags) for pid, v in values.items() if v.flags}
    fixes_ineligible = any(assignment[pid] in NON_STARTING_SLOTS for pid in ineligible)
    if ineligible and fixes_ineligible and gain > 0:
        decision = "urgent"
    elif gain >= notify_min_gain:
        decision = "notify"
    elif gain > 1e-6 or close_calls or unfilled:
        decision = "info"
    else:
        decision = "no_action"

    return LineupRecommendation(
        assignment=assignment,
        moves=moves,
        current_points=round(current_points, 2),
        optimal_points=round(optimal_points, 2),
        gain=round(gain, 2),
        ineligible_starters=ineligible,
        unfilled_slots=unfilled,
        close_calls=close_calls,
        flags=flags,
        decision=decision,
    )


def optimal_points(players: list[LineupPlayer], slot_counts: dict[int, int]) -> float:
    """Best achievable effective points, ignoring current slots and locks."""
    values = {p.provider_player_id: valuation(p) for p in players}
    free = [LineupPlayer(**{**p.__dict__, "locked": False, "current_slot": BENCH_SLOT}) for p in players]
    chosen = _assign(starting_slots(slot_counts), free, values)
    return sum(values[p.provider_player_id].value for p in chosen if p is not None)
