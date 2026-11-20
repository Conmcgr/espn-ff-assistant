import statistics
from datetime import datetime, timedelta

from espn_ff_assistant.manager_stats import ManagerScope, Window, waiver_features
from espn_ff_assistant.repository import RosterEntry, TeamRecord, Transaction
from espn_ff_assistant.rival_signals import (
    LEAGUE_FALLBACK,
    NEUTRAL,
    OBSERVED,
    _Signals,
    completed_matchup_period,
    faab_signals,
    link_trade_responses,
    position_need,
    standings_signals,
    trade_signals,
)

T0 = datetime(2026, 9, 1)
WINDOW = Window("lg", 2026, 6, (2025, 2026))


def _trade(tx_id, category, moves, season=2026, proposer=None, hours=0):
    return Transaction(
        provider_transaction_id=tx_id, season=season, scoring_period=1,
        provider_type=category, status="EXECUTED", category=category,
        provider_team_id=proposer, provider_member_id=None, bid_amount=None,
        process_date=T0 + timedelta(hours=hours), proposed_date=None,
        items=[
            {"item_type": "TRADE", "provider_player_id": p, "from_provider_team_id": f, "to_provider_team_id": t}
            for p, f, t in moves
        ],
    )


def _waiver(tx_id, team, bid, season=2026):
    return Transaction(
        provider_transaction_id=tx_id, season=season, scoring_period=1, provider_type="WAIVER",
        status="EXECUTED", category="waiver_claim", provider_team_id=team, provider_member_id=None,
        bid_amount=bid, process_date=T0, proposed_date=None,
        items=[{"item_type": "ADD", "provider_player_id": 900 + int(bid), "from_provider_team_id": 0, "to_provider_team_id": team}],
    )


def _scope(team, prior=None):
    teams = {2026: {team}}
    if prior:
        teams[2025] = {prior}
    return ManagerScope(f"team:2026:{team}", None, teams)


def _by_name(signals):
    return {s.name: s for s in signals.rows}


def test_position_depth_counts_healthy_players_against_dedicated_slots():
    roster = [
        RosterEntry(1, 1, 0, None, None), RosterEntry(2, 1, 2, None, None),
        RosterEntry(3, 1, 21, None, None), RosterEntry(4, 1, 20, None, None),
    ]
    positions = {1: 1, 2: 2, 3: 2, 4: 3}
    signals = _Signals(1, WINDOW)
    position_need(signals, roster, positions, {0: 1, 2: 2, 4: 2, 20: 6}, roster_week=6)

    rows = _by_name(signals)
    assert rows["position_depth_QB"].value == 0
    assert rows["position_depth_RB"].value == -1  # IR player is not depth
    assert rows["position_depth_WR"].value == -1
    assert "position_depth_TE" not in rows  # no dedicated TE slot configured
    assert rows["position_depth_RB"].provenance == "roster_entries@week6"
    assert rows["position_depth_RB"].confidence == "high"
    assert (rows["position_depth_RB"].season_from, rows["position_depth_RB"].season_to) == (2026, 2026)


def test_position_depth_is_low_confidence_from_stale_roster_and_neutral_without_one():
    stale = _Signals(1, WINDOW)
    position_need(stale, [], {}, {0: 1}, roster_week=4)
    assert _by_name(stale)["position_depth_QB"].confidence == "low"

    missing = _Signals(1, WINDOW)
    position_need(missing, [], {}, {0: 1}, roster_week=None)
    row = _by_name(missing)["position_depth_QB"]
    assert (row.value, row.basis, row.confidence) == (None, NEUTRAL, "insufficient")


def test_proposals_link_only_to_later_response_with_identical_players():
    proposal = _trade("p1", "trade_proposal", [(10, 1, 2), (20, 2, 1)], proposer=1, hours=0)
    earlier = _trade("r0", "completed_trade", [(10, 1, 2), (20, 2, 1)], hours=-1)
    different = _trade("r1", "trade_decline", [(10, 1, 2)], hours=2)
    response = _trade("r2", "trade_decline", [(20, 2, 1), (10, 1, 2)], hours=3)
    other_season = _trade("r3", "completed_trade", [(10, 1, 2), (20, 2, 1)], season=2025, hours=4)

    pairs = link_trade_responses([earlier, proposal, different, response, other_season])
    assert [(p.provider_transaction_id, r and r.provider_transaction_id) for p, r in pairs] == [("p1", "r2")]


def test_received_accept_rate_uses_linked_responses_and_keeps_unlinked_unresolved():
    moves = [[(10 + i, 1, 2), (50 + i, 2, 1)] for i in range(4)]
    txns = []
    for i, m in enumerate(moves):
        txns.append(_trade(f"p{i}", "trade_proposal", m, proposer=1, hours=i * 10))
    txns.append(_trade("a0", "completed_trade", moves[0], hours=1))
    txns.append(_trade("a1", "completed_trade", moves[1], hours=11))
    txns.append(_trade("d2", "trade_decline", moves[2], hours=21))
    # p3 never gets a response: unresolved, not counted as a decline.
    team2 = _scope(2)
    signals = _Signals(2, WINDOW)
    trade_signals(signals, team2, {2026: txns}, {}, {})

    rows = _by_name(signals)
    accept = rows["trade_received_accept_rate_linked"]
    assert (accept.value, accept.sample_size, accept.basis) == (2 / 3, 3, OBSERVED)
    assert rows["trade_received_unresolved"].value == 1
    assert "trade_acceptance_probability" not in rows


def test_sparse_team_history_falls_back_to_league_then_neutral():
    # Team 3 received one linked proposal; teams 1/2 supply a league sample of four.
    txns = []
    for i, (proposer, target, cat) in enumerate(
        [(1, 2, "completed_trade"), (2, 1, "trade_decline"), (1, 2, "trade_decline"), (2, 1, "trade_decline"), (1, 3, "completed_trade")]
    ):
        m = [(100 + i, proposer, target), (200 + i, target, proposer)]
        txns += [_trade(f"p{i}", "trade_proposal", m, proposer=proposer, hours=i), _trade(f"r{i}", cat, m, hours=i)]
    scopes = [_scope(1), _scope(2), _scope(3)]

    sparse = _Signals(3, WINDOW)
    trade_signals(sparse, scopes[2], {2026: txns}, {}, {})
    row = _by_name(sparse)["trade_received_accept_rate_linked"]
    assert (row.value, row.sample_size, row.basis, row.confidence) == (2 / 5, 1, LEAGUE_FALLBACK, "low")

    empty = _Signals(3, WINDOW)
    trade_signals(empty, scopes[2], {2026: []}, {}, {})
    row = _by_name(empty)["trade_received_accept_rate_linked"]
    assert (row.value, row.basis, row.confidence) == (None, NEUTRAL, "insufficient")


def test_package_and_partner_tendencies_follow_owner_across_seasons():
    txns = {
        2025: [
            _trade("c0", "completed_trade", [(1, 7, 2), (2, 2, 7)], season=2025),
            _trade("c1", "completed_trade", [(3, 7, 2), (4, 2, 7), (5, 2, 7)], season=2025),
        ],
        2026: [
            _trade("c2", "completed_trade", [(6, 1, 2), (7, 2, 1)]),
            _trade("c3", "completed_trade", [(8, 3, 4), (9, 4, 3)]),  # not this manager
        ],
    }
    owner_map = {(2025, 2): ["m-b"], (2026, 2): ["m-b"], (2026, 3): ["m-c"]}
    positions = {2: 2, 4: 3, 5: 3, 7: 1}
    signals = _Signals(1, WINDOW)
    trade_signals(signals, _scope(1, prior=7), txns, owner_map, positions)

    rows = _by_name(signals)
    assert rows["trade_one_for_one_share"].value == 2 / 3
    assert rows["trade_one_for_one_share"].sample_size == 3
    assert rows["trade_top_partner_share"].value == 1.0  # every trade was with manager m-b
    acquired = {pos: rows[f"trade_acquired_{pos}"].value for pos in ("QB", "RB", "WR", "TE")}
    assert acquired == {"QB": 1, "RB": 1, "WR": 2, "TE": 0}
    assert (rows["trade_one_for_one_share"].season_from, rows["trade_one_for_one_share"].season_to) == (2025, 2026)


def test_faab_aggression_falls_back_to_league_median_for_thin_history():
    txns = {2026: [
        _waiver("w1", 1, 10), _waiver("w2", 1, 20), _waiver("w3", 1, 30),
        _waiver("w4", 2, 5), _waiver("w5", 2, 5), _waiver("w6", 2, 5),
        _waiver("w7", 3, 50), _waiver("w8", 3, 50), _waiver("w9", 3, 50),
        _waiver("w10", 4, 40),
    ]}
    budgets = {2026: 100}
    scopes = {t: _scope(t) for t in (1, 2, 3, 4)}

    rich = _Signals(1, WINDOW)
    faab_signals(rich, scopes[1], [scopes[2], scopes[3], scopes[4]], txns, budgets, {1: 40})
    rows = _by_name(rich)
    assert rows["faab_remaining"].value == 40
    assert rows["faab_bid_pct_of_remaining_median"].basis == OBSERVED

    thin = _Signals(4, WINDOW)
    faab_signals(thin, scopes[4], [scopes[1], scopes[2], scopes[3]], txns, budgets, {4: 60})
    row = _by_name(thin)["faab_bid_pct_of_remaining_median"]
    assert (row.basis, row.sample_size) == (LEAGUE_FALLBACK, 1)
    per_team = [
        {f["stat_name"]: f["value"] for f in waiver_features(scopes[t], WINDOW, txns, budgets, {})}[
            "faab_bid_pct_of_remaining_median"
        ]
        for t in (1, 2, 3)
    ]
    assert row.value == statistics.median(per_team)

    no_faab = _Signals(1, WINDOW)
    faab_signals(no_faab, scopes[1], [], txns, {2026: None}, {})
    assert [(r.name, r.basis) for r in no_faab.rows] == [("faab_remaining", NEUTRAL)]


def test_standings_rank_and_distance_to_playoff_line():
    standings = [
        TeamRecord(1, 4, 1, 0, 600, 500), TeamRecord(2, 3, 2, 0, 580, 520),
        TeamRecord(3, 2, 3, 0, 550, 560), TeamRecord(4, 1, 4, 0, 500, 600),
    ]
    leader = _Signals(1, WINDOW)
    standings_signals(leader, standings, playoff_seats=2)
    rows = _by_name(leader)
    assert (rows["standings_rank"].value, rows["win_pct"].value) == (1, 0.8)
    assert rows["games_back_of_playoff_line"].value == -2  # two wins clear of the first team out
    assert rows["standings_rank"].sample_size == 5

    chaser = _Signals(4, WINDOW)
    standings_signals(chaser, standings, playoff_seats=2)
    assert _by_name(chaser)["games_back_of_playoff_line"].value == 2

    preseason = _Signals(1, WINDOW)
    standings_signals(preseason, [TeamRecord(1, 0, 0, 0, 0, 0)], playoff_seats=2)
    assert {r.basis for r in preseason.rows} == {NEUTRAL}


def test_top_partner_share_is_zero_when_all_history_is_with_current_co_owners():
    # Two managers who now co-own team 1 traded with each other as teams 5 and 6.
    txns = {2025: [_trade(f"c{i}", "completed_trade", [(i, 5, 6), (10 + i, 6, 5)], season=2025) for i in range(3)]}
    scope = ManagerScope("team:2026:1", None, {2026: {1}, 2025: {5, 6}}, shared_team=True)
    signals = _Signals(1, WINDOW)
    trade_signals(signals, scope, txns, {}, {})
    assert _by_name(signals)["trade_top_partner_share"].value == 0


def test_proposals_without_a_known_proposer_are_not_counted_as_received():
    moves = [(10, 1, 2), (20, 2, 1)]
    txns = [_trade("p", "trade_proposal", moves, proposer=None), _trade("r", "trade_decline", moves, hours=1)]
    signals = _Signals(1, WINDOW)
    trade_signals(signals, _scope(1), {2026: txns}, {}, {})
    rows = _by_name(signals)
    assert rows["trade_received_accept_rate_linked"].sample_size == 0
    assert rows["trade_received_unresolved"].value == 0


def test_league_response_rate_counts_each_linked_response_once_including_departed_teams():
    # Team 9 (a departed manager) received and accepted three proposals; team 1 has none.
    txns = []
    for i in range(3):
        m = [(100 + i, 2, 9), (200 + i, 9, 2)]
        txns += [_trade(f"p{i}", "trade_proposal", m, season=2025, proposer=2, hours=i),
                 _trade(f"r{i}", "completed_trade", m, season=2025, hours=i)]
    signals = _Signals(1, WINDOW)
    trade_signals(signals, _scope(1), {2025: txns}, {}, {})
    row = _by_name(signals)["trade_received_accept_rate_linked"]
    assert (row.value, row.basis) == (1.0, LEAGUE_FALLBACK)


def test_multiweek_matchup_period_counts_only_once_all_its_weeks_are_played():
    settings = {"scheduleSettings": {"matchupPeriods": {"1": [1], "2": [2, 3], "3": [4]}}}
    assert [completed_matchup_period(settings, w) for w in (0, 1, 2, 3, 4)] == [0, 1, 1, 2, 3]
    assert completed_matchup_period({}, 5) == 5
