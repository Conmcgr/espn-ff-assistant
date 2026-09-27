"""Unit tests for player stat/status/schedule normalizers — synthetic payloads."""

from __future__ import annotations

from espn_ff_assistant.normalize.players import identities, pro_schedule, status_rows, week_stats


def _player(pid, stats=None, injury="ACTIVE", slots=(2, 23, 20, 21)):
    return {
        "id": pid,
        "fullName": f"Player {pid}",
        "defaultPositionId": 2,
        "proTeamId": 5,
        "injuryStatus": injury,
        "eligibleSlots": list(slots),
        "ownership": {"percentOwned": 55.0, "percentStarted": 20.0, "percentChange": 3.5},
        "stats": stats or [],
    }


def _stat(season, period, source, split, total):
    return {"seasonId": season, "scoringPeriodId": period, "statSourceId": source,
            "statSplitTypeId": split, "appliedTotal": total}


def _roster_payload(players, team_id=4):
    return {"teams": [{"id": team_id, "roster": {"entries": [
        {"playerId": p["id"], "lineupSlotId": 2, "playerPoolEntry": {"lineupLocked": False, "player": p}}
        for p in players
    ]}}]}


def _pool_payload(entries):
    return {"players": entries}


def test_week_stats_maps_sources_and_splits_and_skips_other_seasons():
    p = _player(1, [
        _stat(2026, 4, 1, 1, 14.2),
        _stat(2026, 3, 0, 1, 20.0),
        _stat(2026, 0, 1, 2, 250.0),
        _stat(2026, 0, 0, 0, 60.0),
        _stat(2025, 0, 0, 0, 300.0),
        _stat(2026, 4, 9, 1, 1.0),
    ])
    rows = {(r["scoring_period"], r["stat_source"], r["stat_split"]): r["applied_total"]
            for r in week_stats(2026, 4, _roster_payload([p]))}
    assert rows == {
        (4, "projected", "week"): 14.2,
        (3, "actual", "week"): 20.0,
        (0, "projected", "season_current"): 250.0,
        (0, "actual", "season"): 60.0,
    }


def test_status_rows_from_roster_and_pool():
    rostered = status_rows(2026, 4, _roster_payload([_player(1, injury="QUESTIONABLE")], team_id=7))
    assert rostered[0]["availability"] == "rostered"
    assert rostered[0]["on_provider_team_id"] == 7
    assert rostered[0]["injury_status"] == "QUESTIONABLE"
    assert rostered[0]["eligible_slots"] == [2, 23, 20, 21]
    assert rostered[0]["lineup_locked"] is False

    pool = status_rows(2026, 4, _pool_payload([
        {"id": 2, "status": "WAIVERS", "onTeamId": 0, "waiverProcessDate": 1790751600000,
         "lineupLocked": False, "player": _player(2)},
        {"id": 3, "status": "FREEAGENT", "onTeamId": 0, "player": _player(3)},
    ]))
    by_id = {r["provider_player_id"]: r for r in pool}
    assert by_id[2]["availability"] == "waivers"
    assert by_id[2]["waiver_clear_at"].year == 2026
    assert by_id[2]["on_provider_team_id"] is None
    assert by_id[3]["availability"] == "free_agent"
    assert by_id[3]["percent_owned"] == 55.0


def test_identities_dedupes_across_shapes():
    data = _roster_payload([_player(1)])
    data["players"] = [{"id": 1, "status": "ONTEAM", "player": _player(1)}, {"id": 2, "status": "FREEAGENT", "player": _player(2)}]
    assert sorted(r["provider_player_id"] for r in identities(data)) == [1, 2]


def test_pro_schedule_games_and_byes():
    data = {"settings": {"proTeams": [
        {"id": 0, "byeWeek": 0},
        {"id": 11, "byeWeek": 13, "proGamesByScoringPeriod": {
            "1": [{"id": 900, "homeProTeamId": 11, "awayProTeamId": 33, "date": 1789318800000, "scoringPeriodId": 1}],
            "2": [{"id": 901, "homeProTeamId": 12, "awayProTeamId": 11, "date": 1789923600000, "scoringPeriodId": 2}],
        }},
    ]}}
    rows = {(r["pro_team_id"], r["scoring_period"]): r for r in pro_schedule(2026, data)}
    assert set(rows) == {(11, 1), (11, 2), (11, 13)}
    assert rows[(11, 1)]["opponent_pro_team_id"] == 33 and rows[(11, 1)]["is_home"] is True
    assert rows[(11, 2)]["opponent_pro_team_id"] == 12 and rows[(11, 2)]["is_home"] is False
    assert rows[(11, 13)]["is_bye"] is True
