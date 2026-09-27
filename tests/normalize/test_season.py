"""Unit tests for season normalizer — all synthetic payloads, no I/O."""

from __future__ import annotations

from espn_ff_assistant.normalize.season import (
    draft_picks,
    members,
    scoring_periods,
    status_row,
    teams,
)


def test_status_row_extracts_scoring_period_bounds():
    data = {"status": {"firstScoringPeriod": 1, "finalScoringPeriod": 17, "latestScoringPeriod": 10}}
    row = status_row(2024, data)
    assert row["first_scoring_period"] == 1
    assert row["final_scoring_period"] == 17
    assert row["latest_scoring_period"] == 10


def test_status_row_tolerates_missing_status():
    row = status_row(2024, {})
    assert row["first_scoring_period"] is None
    assert row["final_scoring_period"] is None


def test_members_extracts_manager_rows():
    data = {
        "members": [
            {"id": "abc123", "displayName": "Alice"},
            {"id": "def456", "displayName": "Bob"},
        ]
    }
    rows = members(data)
    assert len(rows) == 2
    assert rows[0]["provider_member_id"] == "abc123"
    assert rows[0]["display_name"] == "Alice"


def test_members_skips_empty_ids():
    data = {"members": [{"id": None}, {"id": "", "displayName": "Bob"}]}
    rows = members(data)
    assert rows == []


def test_teams_extracts_owners_and_record():
    data = {
        "teams": [
            {
                "id": 1,
                "name": "Team A",
                "owners": ["{abc}", "{def}"],
                "primaryOwner": "{abc}",
                "record": {
                    "overall": {
                        "wins": 7,
                        "losses": 4,
                        "ties": 0,
                        "pointsFor": 1200.5,
                        "pointsAgainst": 1100.0,
                    }
                },
            }
        ]
    }
    rows = teams(2024, data)
    assert len(rows) == 1
    row = rows[0]
    assert row["provider_team_id"] == 1
    assert row["primary_owner_member_id"] == "{abc}"
    assert row["owner_member_ids"] == ["{abc}", "{def}"]
    assert row["wins"] == 7
    assert row["points_for"] == 1200.5


def test_teams_deduplicates_primary_owner():
    """Primary owner should not be duplicated when already in owners list."""
    data = {
        "teams": [
            {
                "id": 2,
                "name": "Team B",
                "owners": ["{abc}"],
                "primaryOwner": "{abc}",
            }
        ]
    }
    rows = teams(2024, data)
    assert rows[0]["owner_member_ids"] == ["{abc}"]


def test_teams_handles_co_owners():
    data = {
        "teams": [
            {
                "id": 3,
                "name": "Team C",
                "owners": ["{abc}", "{xyz}"],
                "primaryOwner": "{abc}",
            }
        ]
    }
    rows = teams(2024, data)
    assert rows[0]["owner_member_ids"] == ["{abc}", "{xyz}"]


def test_draft_picks_extracts_all_fields():
    data = {
        "draftDetail": {
            "picks": [
                {
                    "overallPickNumber": 1,
                    "roundId": 1,
                    "roundPickNumber": 1,
                    "teamId": 5,
                    "playerId": 99,
                    "bidAmount": 0,
                }
            ]
        }
    }
    rows = draft_picks(2024, data)
    assert len(rows) == 1
    pick = rows[0]
    assert pick["overall_pick"] == 1
    assert pick["provider_player_id"] == 99


def test_scoring_periods_correct_states():
    data = {"status": {"firstScoringPeriod": 1, "finalScoringPeriod": 5, "latestScoringPeriod": 3}}
    rows = scoring_periods(2024, data)
    assert len(rows) == 5
    states = {r["scoring_period"]: r["state"] for r in rows}
    assert states[1] == "completed"
    assert states[3] == "completed"
    assert states[4] == "scheduled"


def test_scoring_periods_empty_on_missing_bounds():
    rows = scoring_periods(2024, {})
    assert rows == []
