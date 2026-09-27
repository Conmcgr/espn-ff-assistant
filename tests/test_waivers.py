"""Unit tests for the waiver engine — synthetic rosters and pools only."""

from __future__ import annotations

import itertools

from espn_ff_assistant.lineup import BENCH_SLOT, IR_SLOT, LineupPlayer
from espn_ff_assistant.waivers import (
    Comparable,
    Competitor,
    PoolPlayer,
    WaiverContext,
    droppable,
    evaluate,
    suggest_bid,
)

QB, RB, WR, TE, FLEX, DST, K = 0, 2, 4, 6, 23, 16, 17
SLOTS = {QB: 1, RB: 2, WR: 2, TE: 1, FLEX: 1, DST: 1, K: 1, BENCH_SLOT: 5, IR_SLOT: 1}
ELIG = {
    "QB": frozenset({QB, 20, 21}),
    "RB": frozenset({RB, FLEX, 20, 21}),
    "WR": frozenset({WR, FLEX, 20, 21}),
    "TE": frozenset({TE, FLEX, 20, 21}),
    "DST": frozenset({DST, 20, 21}),
    "K": frozenset({K, 20, 21}),
}
WEEKS = 10
_ids = itertools.count(100)


def P(pos, nxt, ros, slot=BENCH_SLOT, availability="rostered", injury=None, owned=None):
    pid = next(_ids)
    lp = LineupPlayer(pid, f"{pos}{pid}", ELIG[pos], nxt, injury_status=injury, current_slot=slot)
    return PoolPlayer(lp, pos, ros, WEEKS, availability=availability, percent_owned=owned)


def roster():
    return [
        P("QB", 18, 180, QB),
        P("RB", 15, 150, RB), P("RB", 12, 120, RB), P("RB", 6, 60),
        P("WR", 14, 140, WR), P("WR", 11, 110, WR), P("WR", 10, 100, FLEX), P("WR", 5, 50),
        P("TE", 8, 80, TE), P("DST", 3, 30, DST), P("K", 8, 80, K), P("TE", 3, 30),
    ]


def ctx(r, **kw):
    return WaiverContext(slot_counts=SLOTS, roster=r, remaining_weeks=WEEKS, faab_remaining=100, **kw)


def test_strong_roster_with_weak_pool_is_empty():
    pool = [P("WR", 4, 40, availability="free_agent"), P("RB", 3, 30, availability="free_agent")]
    assert evaluate(pool, ctx(roster())) == []


def test_injured_starter_replacement_recommended():
    r = roster()
    rb = r[1]
    r[1] = PoolPlayer(LineupPlayer(rb.pid, rb.lineup.name, rb.lineup.eligible_slots, 0.0, injury_status="OUT", current_slot=RB),
                      "RB", 20, WEEKS)
    add = P("RB", 13, 130, availability="free_agent")
    options = evaluate([add], ctx(r))
    assert options and options[0].add_id == add.pid
    assert options[0].decision == "notify"
    assert options[0].kind == "upgrade"
    assert options[0].drop_id == r[1].pid  # lowest-ROS player; no stash preference set


def test_dst_stream_drops_current_dst():
    r = roster()
    dst = P("DST", 9, 40, availability="free_agent")
    options = evaluate([dst], ctx(r))
    assert options[0].kind == "stream"
    assert options[0].drop_id == r[9].pid
    assert options[0].delta_next == 6


def test_only_one_stream_per_position_notifies():
    pool = [P("DST", 9, 40, availability="free_agent"), P("DST", 8.5, 40, availability="free_agent")]
    options = evaluate(pool, ctx(roster()))
    assert [o.decision for o in options] == ["notify", "suppressed"]
    assert "conflicts_with_higher_ranked" in options[1].reasons


def test_stash_injured_preference_protects_high_ros_injured_bench():
    r = roster()
    stash = P("RB", 0.0, 140, injury="OUT")
    r.append(stash)
    c = ctx(r, stash_injured=True)
    assert stash.pid not in {p.pid for p in droppable(c)}
    assert stash.pid in {p.pid for p in droppable(ctx(r))}


def test_protected_and_ir_players_not_droppable():
    r = roster()
    ir = P("RB", 0.0, 100, IR_SLOT, injury="INJURY_RESERVE")
    r.append(ir)
    c = ctx(r, protected_ids=frozenset({r[3].pid}))
    ids = {p.pid for p in droppable(c)}
    assert ir.pid not in ids and r[3].pid not in ids


def test_free_agent_has_no_bid_and_waiver_bid_clamped_to_budget():
    fa = P("RB", 13, 130, availability="free_agent")
    assert suggest_bid(fa, "upgrade", ctx(roster())) is None

    wv = P("RB", 13, 130, availability="waivers", owned=40)
    comps = [Comparable("RB", 80.0, 45.0) for _ in range(10)]
    rivals = [Competitor(t, 150, {"RB": 5.0}) for t in range(1, 6)]
    c = ctx(roster(), comparables=comps, competitors=rivals)
    c.faab_remaining = 50
    bid = suggest_bid(wv, "upgrade", c)
    assert bid.amount == 50
    assert bid.comparable_scope == "position+ownership"
    assert len(bid.competitors) == 5


def test_bid_scales_with_rival_demand_and_falls_back_scope():
    wv = P("WR", 12, 120, availability="waivers", owned=5)
    comps = [Comparable("WR", float(b), 50.0) for b in (4, 6, 8, 10, 12)]
    no_rivals = suggest_bid(wv, "upgrade", ctx(roster(), comparables=comps))
    assert no_rivals.comparable_scope == "position"
    assert no_rivals.amount == 8
    needy = [Competitor(t, 100, {"WR": 0.0}) for t in range(1, 3)]
    with_rivals = suggest_bid(wv, "upgrade", ctx(roster(), comparables=comps, competitors=needy))
    assert with_rivals.amount == round(8 * 1.3)


def test_stream_bid_capped_at_low_quartile():
    wv = P("DST", 9, 40, availability="waivers")
    comps = [Comparable("DST", float(b), None) for b in (1, 2, 3, 10, 20)]
    bid = suggest_bid(wv, "stream", ctx(roster(), comparables=comps))
    assert bid.amount <= bid.low
