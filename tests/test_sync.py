from espn_ff_assistant.sync import pool_filter, sync_periods


def test_sync_periods_includes_lookahead_within_season():
    status = {"status": {"firstScoringPeriod": 1, "finalScoringPeriod": 17, "latestScoringPeriod": 3}}
    assert sync_periods(status, 1) == (3, [3, 4])
    final = {"status": {"firstScoringPeriod": 1, "finalScoringPeriod": 17, "latestScoringPeriod": 17}}
    assert sync_periods(final, 1) == (17, [17])


def test_sync_periods_clamps_missing_values():
    assert sync_periods({}, 1) == (1, [1])


def test_pool_filter_requests_available_players():
    f = pool_filter(50)["players"]
    assert f["filterStatus"]["value"] == ["FREEAGENT", "WAIVERS"]
    assert f["limit"] == 50
