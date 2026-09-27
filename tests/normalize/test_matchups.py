"""Unit tests for matchup normalizer — synthetic payloads, no I/O."""

from __future__ import annotations

from espn_ff_assistant.normalize.matchups import from_boxscore_payloads


def _settings(regular_count=14):
    periods = {str(i): [i] for i in range(1, regular_count + 4)}
    return {
        "settings": {
            "scheduleSettings": {
                "matchupPeriodCount": regular_count,
                "matchupPeriods": periods,
            }
        }
    }


def _entry(mid, home_tid, away_tid, home_pts, away_pts, matchup_period=1):
    return {
        "id": mid,
        "matchupPeriodId": matchup_period,
        "home": {"teamId": home_tid, "totalPoints": home_pts},
        "away": {"teamId": away_tid, "totalPoints": away_pts},
    }


def _bye(mid, home_tid, home_pts, matchup_period=1):
    return {
        "id": mid,
        "matchupPeriodId": matchup_period,
        "home": {"teamId": home_tid, "totalPoints": home_pts},
    }


def test_basic_home_win():
    boxscore = {"schedule": [_entry(1, 1, 2, 100.0, 90.0)]}
    settings = _settings()
    rows = from_boxscore_payloads(2024, [(1, boxscore, settings)])
    assert len(rows) == 1
    assert rows[0]["winner"] == "home"
    assert rows[0]["home_score"] == 100.0
    assert rows[0]["away_score"] == 90.0


def test_away_win():
    boxscore = {"schedule": [_entry(2, 1, 2, 80.0, 110.0)]}
    rows = from_boxscore_payloads(2024, [(1, boxscore, _settings())])
    assert rows[0]["winner"] == "away"


def test_tie():
    boxscore = {"schedule": [_entry(3, 1, 2, 95.5, 95.5)]}
    rows = from_boxscore_payloads(2024, [(1, boxscore, _settings())])
    assert rows[0]["winner"] == "tie"


def test_bye_week():
    boxscore = {"schedule": [_bye(4, 1, 88.0, matchup_period=6)]}
    rows = from_boxscore_payloads(2024, [(6, boxscore, _settings())])
    assert len(rows) == 1
    row = rows[0]
    assert row["is_bye"] is True
    assert row["away_provider_team_id"] is None
    assert row["winner"] is None


def test_deduplication_keeps_latest_period():
    """When the same matchup appears in multiple weeks, keep the version from the latest period."""
    early = {"schedule": [_entry(10, 1, 2, 80.0, 70.0)]}
    late = {"schedule": [_entry(10, 1, 2, 120.0, 115.0)]}
    settings = _settings()
    rows = from_boxscore_payloads(2024, [(1, early, settings), (5, late, settings)])
    assert len(rows) == 1
    assert rows[0]["home_score"] == 120.0


def test_postseason_period_type():
    boxscore = {"schedule": [_entry(20, 1, 2, 130.0, 120.0, matchup_period=15)]}
    rows = from_boxscore_payloads(2024, [(15, boxscore, _settings(regular_count=14))])
    assert rows[0]["period_type"] == "postseason"


def test_regular_season_period_type():
    boxscore = {"schedule": [_entry(21, 1, 2, 110.0, 100.0, matchup_period=7)]}
    rows = from_boxscore_payloads(2024, [(7, boxscore, _settings(regular_count=14))])
    assert rows[0]["period_type"] == "regular"


def test_empty_schedule():
    rows = from_boxscore_payloads(2024, [(1, {}, _settings())])
    assert rows == []


def test_no_periods():
    rows = from_boxscore_payloads(2024, [])
    assert rows == []


def test_same_id_different_seasons_stays_separate():
    """ESPN uses sequential IDs per season; id=0 in 2015 and 2020 are different matchups."""
    boxscore = {"schedule": [_entry(0, 1, 2, 100.0, 90.0)]}
    settings = _settings()
    rows_2015 = from_boxscore_payloads(2015, [(1, boxscore, settings)])
    rows_2020 = from_boxscore_payloads(2020, [(1, boxscore, settings)])
    # Both have provider_matchup_id=0 but they belong to different seasons;
    # that's handled by using (league_season_id, provider_matchup_id) in the DB.
    assert rows_2015[0]["provider_matchup_id"] == 0
    assert rows_2020[0]["provider_matchup_id"] == 0
    assert rows_2015[0]["winner"] == "home"
    assert rows_2020[0]["winner"] == "home"
