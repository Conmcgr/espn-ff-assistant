"""Unit tests for manager_stats — synthetic data, no database required."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from espn_ff_assistant.manager_stats import (
    VERSION,
    ManagerScope,
    Window,
    _confidence,
    _feature,
    holding_period_features,
    manager_scopes,
    roster_churn_features,
    trade_features,
    waiver_features,
)
from espn_ff_assistant.repository import OwnershipInterval, Transaction

SYSTEM_MEMBER = "{SYSTEM}"
T0 = datetime(2024, 9, 10, tzinfo=UTC)


def _tx(
    category="waiver_claim",
    status="EXECUTED",
    team=1,
    bid=None,
    week=2,
    season=2024,
    member=SYSTEM_MEMBER,
    items=None,
    order=0,
):
    return Transaction(
        provider_transaction_id=f"tx-{season}-{team}-{week}-{order}-{status}",
        season=season,
        scoring_period=week,
        provider_type=None,
        status=status,
        category=category,
        provider_team_id=team,
        provider_member_id=member,
        bid_amount=bid,
        process_date=T0 + timedelta(days=order),
        proposed_date=None,
        items=items or [],
    )


def _add(pid, to_team=1):
    return {"item_type": "ADD", "provider_player_id": pid, "from_provider_team_id": None, "to_provider_team_id": to_team}


def _stats(rows):
    return {r["stat_name"]: r for r in rows}


SCOPE = ManagerScope("mgr-1", "{REAL}", {2024: {1}})
WINDOW = Window("lg", 2024, 17, (2024,))


def test_confidence_thresholds():
    assert _confidence(None) == "insufficient"
    assert _confidence(0) == "insufficient"
    assert _confidence(1) == "low"
    assert _confidence(4) == "low"
    assert _confidence(5) == "medium"
    assert _confidence(19) == "medium"
    assert _confidence(20) == "high"


def test_feature_shape():
    f = _feature("mgr-1", "lg-1", "waiver_claims_total", 7.0, 7, 2022, 2024, 2024, 10)
    assert f["confidence"] == "medium"
    assert f["version"] == VERSION
    assert f["shared_team"] is False


def test_executed_claims_attributed_by_team_not_member():
    txns = {2024: [
        _tx(bid=10, member=SYSTEM_MEMBER),
        _tx(bid=5, team=2, member="{REAL}"),  # someone else's team, even if member matches
    ]}
    s = _stats(waiver_features(SCOPE, WINDOW, txns, {2024: 200}, {}))
    assert s["waiver_claims_total"]["value"] == 1
    assert s["faab_total_spent"]["value"] == 10


def test_fail_rate_excludes_pending_and_canceled():
    txns = {2024: [
        _tx(status="EXECUTED", bid=1, order=0),
        _tx(status="FAILED_INVALIDPLAYERSOURCE", bid=3, order=1),
        _tx(status="FAILED_ROSTERLIMIT", bid=3, order=2),
        _tx(status="PENDING", order=3),
        _tx(status="CANCELED", order=4),
    ]}
    s = _stats(waiver_features(SCOPE, WINDOW, txns, {2024: 200}, {}))
    assert s["waiver_fail_rate"]["value"] == pytest.approx(2 / 3)
    assert s["waiver_fail_rate"]["sample_size"] == 3
    assert s["waiver_lost_bid_rate"]["value"] == pytest.approx(1 / 3)


def test_bid_pct_of_remaining_uses_running_budget():
    txns = {2024: [_tx(bid=100, order=0), _tx(bid=50, order=1)]}
    s = _stats(waiver_features(SCOPE, WINDOW, txns, {2024: 200}, {}))
    # 100/200 = 0.5, then 50/100 = 0.5
    assert s["faab_bid_pct_of_remaining_median"]["value"] == pytest.approx(0.5)


def test_early_share_and_position_medians():
    txns = {2024: [
        _tx(bid=30, week=2, items=[_add(11)], order=0),
        _tx(bid=10, week=9, items=[_add(22)], order=1),
        _tx(bid=20, week=10, items=[_add(12)], order=2),
    ]}
    positions = {11: 2, 12: 2, 22: 16}
    s = _stats(waiver_features(SCOPE, WINDOW, txns, {2024: 200}, positions))
    assert s["faab_early_season_share"]["value"] == pytest.approx(0.5)
    assert s["faab_median_bid_RB"]["value"] == 25
    assert s["faab_median_bid_RB"]["sample_size"] == 2
    assert s["faab_median_bid_DST"]["value"] == 10
    assert s["faab_median_bid_QB"]["value"] is None


def test_no_faab_stats_when_league_has_no_budget():
    txns = {2024: [_tx(bid=0)]}
    s = _stats(waiver_features(SCOPE, WINDOW, txns, {2024: None}, {}))
    assert s["waiver_claims_total"]["value"] == 1
    assert s["faab_total_spent"]["sample_size"] == 0


def test_adds_per_week_sample_is_adds():
    txns = {2024: [_tx(items=[_add(1)]), _tx(category="free_agent_move", items=[_add(2)], order=1)]}
    s = _stats(roster_churn_features(SCOPE, WINDOW, txns, {2024: 10}))
    assert s["adds_total"]["value"] == 2
    assert s["adds_per_week"]["value"] == pytest.approx(0.2)
    assert s["adds_per_week"]["sample_size"] == 2
    assert s["weeks_observed"]["value"] == 10

    empty = _stats(roster_churn_features(SCOPE, WINDOW, {2024: []}, {2024: 10}))
    assert empty["adds_per_week"]["confidence"] == "insufficient"


def test_trade_partners_exclude_own_team_in_that_season():
    scope = ManagerScope("m", None, {2023: {5}, 2024: {1}})
    window = Window("lg", 2024, 17, (2023, 2024))
    trade = _tx(
        category="completed_trade", team=5, season=2023,
        items=[
            {"item_type": "TRADE", "provider_player_id": 1, "from_provider_team_id": 5, "to_provider_team_id": 7},
            {"item_type": "TRADE", "provider_player_id": 2, "from_provider_team_id": 7, "to_provider_team_id": 5},
        ],
    )
    s = _stats(trade_features(scope, window, {2023: [trade], 2024: []}))
    assert s["trades_completed_total"]["value"] == 1
    assert s["trade_unique_partners"]["value"] == 1


def test_holding_period_only_counts_own_intervals():
    intervals = [
        OwnershipInterval(2024, 1, 10, 1, 5, "drop"),
        OwnershipInterval(2024, 1, 11, 2, 4, "season_end"),
        OwnershipInterval(2024, 2, 12, 1, 9, "drop"),
    ]
    s = _stats(holding_period_features(SCOPE, WINDOW, intervals))
    assert s["holding_period_median_weeks"]["value"] == 3
    assert s["players_dropped_total"]["value"] == 1


def test_manager_scopes_flags_co_owned_teams():
    owner_map = {(2024, 1): ["a", "b"], (2024, 2): ["c"], (2016, 3): ["c", "d"]}
    scopes = {s.manager_id: s for s in manager_scopes(owner_map, {}, [2024])}
    assert scopes["a"].shared_team and scopes["b"].shared_team
    assert not scopes["c"].shared_team  # co-ownership outside the window does not count
    assert scopes["c"].teams_by_season == {2024: {2}, 2016: {3}}


def test_feature_rows_carry_shared_flag():
    scope = ManagerScope("m", None, {2024: {1}}, shared_team=True)
    rows = waiver_features(scope, WINDOW, {2024: []}, {2024: 200}, {})
    assert rows and all(r["shared_team"] for r in rows)
