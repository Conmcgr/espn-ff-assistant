"""Integration test: load a synthetic run into a throwaway local database.

Requires TEST_DATABASE_URL to be set in the environment (skipped otherwise).
The test creates its own tables via the migration system, loads a small
synthetic run twice, and asserts that row counts are identical after both
loads (idempotency) and that co-owners are stored correctly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

NEEDS_DB = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="set TEST_DATABASE_URL to run database integration tests",
)


def _make_run(base: Path) -> Path:
    """Create a minimal synthetic run directory."""
    run = base / "20990101T000000Z"
    run.mkdir()

    manifest_entries = []

    for season in [2024, 2025]:
        season_dir = run / str(season)

        # mStatus
        status_path = season_dir / "mStatus" / "response.json"
        status_path.parent.mkdir(parents=True)
        status_payload = {
            "status": {
                "firstScoringPeriod": 1,
                "finalScoringPeriod": 3,
                "latestScoringPeriod": 3 if season == 2024 else 1,
            }
        }
        status_path.write_text(json.dumps(status_payload))
        manifest_entries.append({"season": season, "view": "mStatus", "raw_path": f"{season}/mStatus/response.json", "transport_state": "present", "retrieved_at": "2099-01-01T00:00:00Z"})

        # mSettings
        settings_path = season_dir / "mSettings" / "response.json"
        settings_path.parent.mkdir(parents=True)
        settings_payload = {
            "id": 99999,
            "settings": {
                "name": "Test League",
                "scheduleSettings": {
                    "matchupPeriodCount": 2,
                    "matchupPeriods": {"1": [1], "2": [2], "3": [3]},
                },
            },
        }
        settings_path.write_text(json.dumps(settings_payload))
        manifest_entries.append({"season": season, "view": "mSettings", "raw_path": f"{season}/mSettings/response.json", "transport_state": "present", "retrieved_at": "2099-01-01T00:00:00Z"})

        # mTeam — two teams, one co-owned
        team_path = season_dir / "mTeam" / "response.json"
        team_path.parent.mkdir(parents=True)
        team_payload = {
            "members": [
                {"id": f"member-a-{season}", "displayName": "Alice"},
                {"id": f"member-b-{season}", "displayName": "Bob"},
                {"id": f"member-c-{season}", "displayName": "Carol"},
            ],
            "teams": [
                {
                    "id": 1,
                    "name": "Team One",
                    "owners": [f"member-a-{season}"],
                    "primaryOwner": f"member-a-{season}",
                    "record": {"overall": {"wins": 2, "losses": 1, "ties": 0, "pointsFor": 300.0, "pointsAgainst": 250.0}},
                },
                {
                    "id": 2,
                    "name": "Team Two",
                    "owners": [f"member-b-{season}", f"member-c-{season}"],
                    "primaryOwner": f"member-b-{season}",
                    "record": {"overall": {"wins": 1, "losses": 2, "ties": 0, "pointsFor": 250.0, "pointsAgainst": 300.0}},
                },
            ],
        }
        team_path.write_text(json.dumps(team_payload))
        manifest_entries.append({"season": season, "view": "mTeam", "raw_path": f"{season}/mTeam/response.json", "transport_state": "present", "retrieved_at": "2099-01-01T00:00:00Z"})

        # mDraftDetail
        draft_path = season_dir / "mDraftDetail" / "response.json"
        draft_path.parent.mkdir(parents=True)
        draft_path.write_text(json.dumps({"draftDetail": {"picks": [
            {"overallPickNumber": 1, "roundId": 1, "roundPickNumber": 1, "teamId": 1, "playerId": 1001, "bidAmount": 0},
            {"overallPickNumber": 2, "roundId": 1, "roundPickNumber": 2, "teamId": 2, "playerId": 1002, "bidAmount": 0},
        ]}}))
        manifest_entries.append({"season": season, "view": "mDraftDetail", "raw_path": f"{season}/mDraftDetail/response.json", "transport_state": "present", "retrieved_at": "2099-01-01T00:00:00Z"})

        # mRoster for period 1
        roster_path = season_dir / "mRoster" / "scoring-period-01" / "response.json"
        roster_path.parent.mkdir(parents=True)
        roster_payload = {
            "teams": [
                {
                    "id": 1,
                    "roster": {
                        "entries": [
                            {
                                "playerId": 1001,
                                "lineupSlotId": 0,
                                "acquisitionType": "DRAFT",
                                "playerPoolEntry": {
                                    "appliedStatTotal": 15.5,
                                    "player": {"fullName": "Player One", "firstName": "Player", "lastName": "One", "defaultPositionId": 2, "proTeamId": 1},
                                },
                            }
                        ]
                    },
                },
                {
                    "id": 2,
                    "roster": {
                        "entries": [
                            {
                                "playerId": 1002,
                                "lineupSlotId": 0,
                                "acquisitionType": "DRAFT",
                                "playerPoolEntry": {
                                    "appliedStatTotal": 20.0,
                                    "player": {"fullName": "Player Two", "firstName": "Player", "lastName": "Two", "defaultPositionId": 2, "proTeamId": 2},
                                },
                            }
                        ]
                    },
                },
            ]
        }
        roster_path.write_text(json.dumps(roster_payload))
        manifest_entries.append({"season": season, "view": "mRoster", "scoring_period": 1, "raw_path": f"{season}/mRoster/scoring-period-01/response.json", "transport_state": "present", "retrieved_at": "2099-01-01T00:00:00Z"})

        # mBoxscore for period 1
        boxscore_path = season_dir / "mBoxscore" / "scoring-period-01" / "response.json"
        boxscore_path.parent.mkdir(parents=True)
        boxscore_payload = {
            "settings": settings_payload["settings"],
            "schedule": [
                {"id": season * 100 + 1, "matchupPeriodId": 1, "home": {"teamId": 1, "totalPoints": 120.0}, "away": {"teamId": 2, "totalPoints": 100.0}},
                {"id": season * 100 + 2, "matchupPeriodId": 2, "home": {"teamId": 2, "totalPoints": 0}, "away": {"teamId": 1, "totalPoints": 0}},
            ],
        }
        boxscore_path.write_text(json.dumps(boxscore_payload))
        manifest_entries.append({"season": season, "view": "mBoxscore", "scoring_period": 1, "raw_path": f"{season}/mBoxscore/scoring-period-01/response.json", "transport_state": "present", "retrieved_at": "2099-01-01T00:00:00Z"})

        # mTransactions2 for period 1
        txns_path = season_dir / "mTransactions2" / "scoring-period-01" / "response.json"
        txns_path.parent.mkdir(parents=True)
        txns_path.write_text(json.dumps({"transactions": [
            {
                "id": f"tx-{season}-1",
                "type": "WAIVER",
                "status": "EXECUTED",
                "teamId": 1,
                "memberId": f"member-a-{season}",
                "bidAmount": 10,
                "processDate": 1700000000000,
                "proposedDate": 1699900000000,
                "items": [{"type": "ADD", "playerId": 1003, "fromTeamId": 0, "toTeamId": 1}],
            }
        ]}))
        manifest_entries.append({"season": season, "view": "mTransactions2", "scoring_period": 1, "raw_path": f"{season}/mTransactions2/scoring-period-01/response.json", "transport_state": "present", "retrieved_at": "2099-01-01T00:00:00Z"})

    (run / "manifest.jsonl").write_text("\n".join(json.dumps(e) for e in manifest_entries))
    return run


@NEEDS_DB
def test_loader_idempotent(tmp_path, monkeypatch):
    """Loading the same run twice must produce identical row counts."""
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setattr("espn_ff_assistant.database.load_dotenv", lambda: None)

    # Apply migrations
    from espn_ff_assistant.database import apply_migrations, connection

    # Ensure the two anon roles exist so the REVOKE in migrations doesn't fail.
    with connection() as conn:
        for role in ("anon", "authenticated"):
            count = conn.execute(
                "SELECT count(*) FROM pg_roles WHERE rolname=%s", (role,)
            ).fetchone()[0]
            if count == 0:
                conn.execute(f'CREATE ROLE "{role}"')
        conn.commit()

    apply_migrations()

    run = _make_run(tmp_path)

    def run_loader():
        result = subprocess.run(
            [sys.executable, "scripts/load_run.py", str(run)],
            capture_output=True,
            text=True,
            env={**os.environ, "DATABASE_URL": os.environ["TEST_DATABASE_URL"]},
            cwd=str(Path(__file__).parent.parent.parent),
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    counts1 = run_loader()["counts"]
    counts2 = run_loader()["counts"]

    assert counts1 == counts2, f"Row counts changed on second load:\n{counts1}\n{counts2}"


@NEEDS_DB
def test_co_owners_stored(tmp_path, monkeypatch):
    """Team with two owners must have two rows in team_owners."""
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setattr("espn_ff_assistant.database.load_dotenv", lambda: None)

    from espn_ff_assistant.database import apply_migrations, connection

    with connection() as conn:
        for role in ("anon", "authenticated"):
            count = conn.execute(
                "SELECT count(*) FROM pg_roles WHERE rolname=%s", (role,)
            ).fetchone()[0]
            if count == 0:
                conn.execute(f'CREATE ROLE "{role}"')
        conn.commit()

    apply_migrations()
    run = _make_run(tmp_path)

    subprocess.run(
        [sys.executable, "scripts/load_run.py", str(run)],
        check=True,
        capture_output=True,
        env={**os.environ, "DATABASE_URL": os.environ["TEST_DATABASE_URL"]},
        cwd=str(Path(__file__).parent.parent.parent),
    )

    with connection() as conn:
        # Team 2 has two owners in every season
        row = conn.execute(
            """
            SELECT count(*) FROM team_owners to2
            JOIN league_seasons ls ON ls.id = to2.league_season_id
            JOIN leagues l ON l.id = ls.league_id
            WHERE to2.provider_team_id = 2
              AND l.provider_league_id = 99999
            """
        ).fetchone()
    assert row[0] >= 2, f"Expected >=2 team_owners rows for co-owned team, got {row[0]}"
