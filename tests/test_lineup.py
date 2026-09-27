"""Unit tests for the lineup optimizer — synthetic rosters only."""

from __future__ import annotations

import itertools
import random

import pytest

from espn_ff_assistant.lineup import (
    BENCH_SLOT,
    IR_SLOT,
    LineupPlayer,
    optimal_points,
    optimize,
    starting_slots,
    valuation,
)

QB, RB, WR, TE, FLEX, DST, K = 0, 2, 4, 6, 23, 16, 17
SLOTS = {QB: 1, RB: 2, WR: 2, TE: 1, FLEX: 1, DST: 1, K: 1, BENCH_SLOT: 6, IR_SLOT: 1}
ELIG = {
    "QB": frozenset({QB, 7, 20, 21}),
    "RB": frozenset({RB, 3, FLEX, 7, 20, 21}),
    "WR": frozenset({WR, 3, 5, FLEX, 7, 20, 21}),
    "TE": frozenset({TE, 5, FLEX, 7, 20, 21}),
    "DST": frozenset({DST, 20, 21}),
    "K": frozenset({K, 20, 21}),
}

_ids = itertools.count(1)


def P(pos, proj, slot=BENCH_SLOT, **kw):
    pid = next(_ids)
    return LineupPlayer(pid, f"{pos}{pid}", ELIG[pos], proj, current_slot=slot, **kw)


def full_roster():
    return [
        P("QB", 20, QB), P("QB", 12),
        P("RB", 15, RB), P("RB", 12, RB), P("RB", 7),
        P("WR", 14, WR), P("WR", 11, WR), P("WR", 10, FLEX), P("WR", 6),
        P("TE", 8, TE), P("DST", 7, DST), P("K", 8, K), P("TE", 4),
    ]


def test_already_optimal_is_no_action():
    rec = optimize(full_roster(), SLOTS)
    assert rec.decision == "no_action"
    assert rec.close_calls == []
    assert rec.moves == []
    assert rec.gain == 0


def test_bye_starter_is_urgent_and_replaced():
    roster = full_roster()
    qb = roster[0]
    roster[0] = LineupPlayer(qb.provider_player_id, qb.name, qb.eligible_slots, 20, on_bye=True, current_slot=QB)
    rec = optimize(roster, SLOTS)
    assert rec.decision == "urgent"
    assert rec.ineligible_starters == [qb.provider_player_id]
    assert rec.assignment[roster[1].provider_player_id] == QB
    assert rec.gain == 12


def test_out_starter_urgent():
    roster = full_roster()
    rb = roster[2]
    roster[2] = LineupPlayer(rb.provider_player_id, rb.name, rb.eligible_slots, 15, injury_status="OUT", current_slot=RB)
    rec = optimize(roster, SLOTS)
    assert rec.decision == "urgent"
    assert rec.assignment[rb.provider_player_id] == BENCH_SLOT


def test_doubtful_discount_changes_flex():
    roster = full_roster()
    wr = roster[7]  # FLEX WR projected 10
    roster[7] = LineupPlayer(wr.provider_player_id, wr.name, wr.eligible_slots, 10, injury_status="DOUBTFUL", current_slot=FLEX)
    rec = optimize(roster, SLOTS)
    # 10 * 0.25 = 2.5 < RB bench 7
    assert rec.assignment[roster[4].provider_player_id] == FLEX
    assert rec.assignment[wr.provider_player_id] == BENCH_SLOT
    assert rec.decision == "notify"
    assert "injury_risk:doubtful" in rec.flags[wr.provider_player_id]


def test_large_gain_notifies():
    roster = full_roster()
    roster.append(P("RB", 25))
    rec = optimize(roster, SLOTS)
    assert rec.decision == "notify"
    assert rec.gain == pytest.approx(25 - 10)


def test_locked_players_stay_put():
    roster = full_roster()
    bench_rb = roster[4]
    roster[4] = LineupPlayer(bench_rb.provider_player_id, bench_rb.name, bench_rb.eligible_slots, 30, locked=True, current_slot=BENCH_SLOT)
    rec = optimize(roster, SLOTS)
    assert rec.assignment[bench_rb.provider_player_id] == BENCH_SLOT
    assert rec.decision == "no_action"


def test_ir_slot_player_not_started():
    roster = full_roster()
    roster.append(P("RB", 40, IR_SLOT))
    rec = optimize(roster, SLOTS)
    assert rec.assignment[roster[-1].provider_player_id] == IR_SLOT


def test_unfilled_slot_when_no_startable_player():
    roster = [p for p in full_roster() if p.name[:1] != "K"]
    rec = optimize(roster, SLOTS)
    assert rec.unfilled_slots == [K]
    assert rec.decision == "info"


def test_close_call_reported_without_notifying():
    roster = full_roster()
    roster.append(P("WR", 9.5))
    rec = optimize(roster, SLOTS)
    assert rec.decision == "info"
    assert rec.moves == []
    assert [c.margin for c in rec.close_calls] == [0.5]


def test_missing_projection_flagged():
    assert valuation(P("RB", None)).flags == ("no_projection",)


def test_superflex_slot_uses_eligibility():
    slots = {QB: 1, 7: 1, BENCH_SLOT: 3}
    qb1, qb2, rb = P("QB", 20), P("QB", 18), P("RB", 15)
    rec = optimize([qb1, qb2, rb], slots)
    assert {rec.assignment[qb1.provider_player_id], rec.assignment[qb2.provider_player_id]} == {QB, 7}
    assert rec.assignment[rb.provider_player_id] == BENCH_SLOT


def _brute_force(players, slot_counts):
    slots = starting_slots(slot_counts)
    values = {p.provider_player_id: valuation(p) for p in players}
    best = 0.0
    candidates = [p for p in players if values[p.provider_player_id].startable]
    for k in range(0, min(len(slots), len(candidates)) + 1):
        for chosen in itertools.permutations(candidates, k):
            for slot_idx in itertools.combinations(range(len(slots)), k):
                if all(slots[i] in p.eligible_slots for i, p in zip(slot_idx, chosen, strict=True)):
                    best = max(best, sum(values[p.provider_player_id].value for p in chosen))
    return best


def test_matches_brute_force_on_random_rosters():
    rng = random.Random(7)
    slot_counts = {QB: 1, RB: 1, WR: 1, FLEX: 1, BENCH_SLOT: 4}
    statuses = [None, None, None, "QUESTIONABLE", "DOUBTFUL", "OUT"]
    for _ in range(500):
        roster = [
            LineupPlayer(
                i, f"p{i}", ELIG[rng.choice(["QB", "RB", "WR", "TE"])],
                rng.choice([None, round(rng.uniform(0, 25), 1)]),
                injury_status=rng.choice(statuses), on_bye=rng.random() < 0.1,
            )
            for i in range(rng.randint(2, 6))
        ]
        assert optimal_points(roster, slot_counts) == pytest.approx(_brute_force(roster, slot_counts))
