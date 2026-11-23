"""Integration test: load a synthetic run into a throwaway local database.

Requires TEST_DATABASE_URL to be set in the environment (skipped otherwise).
The test creates its own tables via the migration system, loads a small
synthetic run twice, and asserts that row counts are identical after both
loads (idempotency) and that co-owners are stored correctly.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from espn_ff_assistant.database import apply_migrations, connection
from espn_ff_assistant.loader import load_run
from espn_ff_assistant.repository import Repository

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


@pytest.fixture
def migrated_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setenv("DATABASE_HOSTADDR", "")

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


@NEEDS_DB
def test_demo_snapshot_ignores_later_lookahead_roster(tmp_path, migrated_database):
    run = _make_run(tmp_path)
    run = run.rename(run.with_name(run.name + "-current"))
    for settings in run.glob("*/mSettings/response.json"):
        payload = json.loads(settings.read_text())
        payload["id"] = 99998
        settings.write_text(json.dumps(payload))
    manifest = [json.loads(line) for line in (run / "manifest.jsonl").read_text().splitlines()]
    # Default live sync fetches current + next week. Only mBoxscore marks current.
    current = run / "2025/mRoster/scoring-period-01/response.json"
    lookahead = run / "2025/mRoster/scoring-period-02/response.json"
    lookahead.parent.mkdir(parents=True)
    lookahead.write_text(current.read_text())
    manifest.append({"season": 2025, "view": "mRoster", "scoring_period": 2,
                     "raw_path": "2025/mRoster/scoring-period-02/response.json",
                     "transport_state": "present", "retrieved_at": "2099-01-01T00:00:30Z"})
    (run / "manifest.jsonl").write_text("\n".join(json.dumps(row) for row in manifest))
    load_run(run)
    with connection() as conn:
        repo = Repository(conn)
        snapshot = repo.demo_snapshot(repo.league_id(99998), 2025,
                                      datetime(2099, 1, 1, 0, 1, tzinfo=UTC))
        assert snapshot["week"] == 1
        assert snapshot["synced_at"] == datetime(2099, 1, 1, tzinfo=UTC)


@NEEDS_DB
def test_demo_empty_pool_never_revives_an_older_candidate(tmp_path, migrated_database):
    from espn_ff_assistant import engine
    from espn_ff_assistant.web.data import waivers_view

    run = _make_run(tmp_path)
    run = run.rename(run.with_name("populated-current"))
    for settings in run.glob("*/mSettings/response.json"):
        payload = json.loads(settings.read_text())
        payload["id"] = 99997
        payload["settings"]["rosterSettings"] = {"lineupSlotCounts": {"0": 1}}
        settings.write_text(json.dumps(payload))
    pool = run / "2025/kona_player_info/scoring-period-01/response.json"
    pool.parent.mkdir(parents=True)
    pool.write_text(json.dumps({"players": [{"id": 1005, "status": "FREEAGENT", "player": {
        "id": 1005, "fullName": "Old Candidate", "defaultPositionId": 1,
    }}]}))
    manifest = [json.loads(line) for line in (run / "manifest.jsonl").read_text().splitlines()]
    manifest.append({"season": 2025, "view": "kona_player_info", "scoring_period": 1,
                     "raw_path": pool.relative_to(run).as_posix(), "transport_state": "present",
                     "retrieved_at": "2099-01-01T00:00:00Z"})
    (run / "manifest.jsonl").write_text("\n".join(json.dumps(row) for row in manifest))
    load_run(run)
    empty = shutil.copytree(run, run.with_name("empty-current"))
    (empty / pool.relative_to(run)).write_text('{"players": []}')
    for row in manifest:
        row["retrieved_at"] = "2099-01-01T00:02:00Z"
    (empty / "manifest.jsonl").write_text("\n".join(json.dumps(row) for row in manifest))
    load_run(empty)
    cutoff = datetime(2099, 1, 1, 0, 3, tzinfo=UTC)
    with connection() as conn:
        repo = Repository(conn)
        league = repo.league_id(99997)
        snapshot = repo.demo_snapshot(league, 2025, cutoff)
        assert snapshot["pool_observed"]
        # An unpinned reader would return the old, now absent candidate.
        assert [p.provider_player_id for p in repo.available_players(league, 2025, 1, cutoff)] == [1005]
        pinned = Repository(conn, player_sync_id=snapshot["sync_id"])
        assert pinned.available_players(league, 2025, 1, cutoff) == []
        user = repo.default_user_id()
        repo.set_user_team(user, league, 2025, 1)
        result = waivers_view(engine.context(pinned, league, user, 2025, 1, cutoff))
        assert not result["picks"] and not result["other"]
        assert result["no_action"]


@NEEDS_DB
def test_loader_idempotent(tmp_path, migrated_database):
    """Loading the same run twice must produce identical row counts."""
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
def test_co_owners_stored(tmp_path, migrated_database):
    """Team with two owners must have two rows in team_owners."""
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


def _make_roster_run(base: Path, day: int, rosters: dict[int, dict[int, int | None]]) -> Path:
    """Archive synthetic rosters by week and team (None means an empty roster)."""
    retrieved_at = f"2026-09-{day}T00:00:00Z"
    run = base / f"202609{day}T000000Z-current"
    manifest = []

    def write(view, payload, period=None):
        folder = run / "2026" / view
        if period is not None:
            folder /= f"scoring-period-{period:02d}"
        folder.mkdir(parents=True)
        path = folder / "response.json"
        path.write_text(json.dumps(payload))
        manifest.append({
            "season": 2026, "view": view, "scoring_period": period,
            "raw_path": path.relative_to(run).as_posix(), "retrieved_at": retrieved_at,
        })

    write("mSettings", {"id": 99999, "settings": {"rosterSettings": {"lineupSlotCounts": {"0": 1}}}})
    write("mTeam", {"teams": [{"id": 1}, {"id": 2}]})

    def player_entry(pid):
        return {
            "playerId": pid, "lineupSlotId": 20,
            "playerPoolEntry": {"player": {
                "fullName": f"QB {pid}", "defaultPositionId": 1,
                "stats": [
                    {"seasonId": 2026, "scoringPeriodId": week, "statSourceId": 1,
                     "statSplitTypeId": 1, "appliedTotal": 20}
                    for week in (1, 2)
                ],
            }},
        }

    for week, teams in rosters.items():
        write("mRoster", {"teams": [
            {"id": team, "roster": {"entries": [player_entry(pid)] if pid is not None else []}}
            for team, pid in teams.items()
        ]}, week)
    for week in (1, 2):
        write("kona_player_info", {"players": [
            {"id": 2999, "status": "FREEAGENT", "player": {"defaultPositionId": 1}},
        ]}, week)
    (run / "manifest.jsonl").write_text("\n".join(json.dumps(entry) for entry in manifest))
    return run


def _recommend(*args, expect_success=True):
    result = subprocess.run(
        [sys.executable, "scripts/recommend.py", *args],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parents[2],
    )
    if not expect_success:
        assert result.returncode == 1, result.stdout
        return result.stderr
    assert result.returncode == 0, result.stderr
    return result.stdout


@NEEDS_DB
def test_scan_replays_roster_after_same_week_sync(tmp_path, migrated_database):
    original = _make_roster_run(tmp_path, 24, {1: {1: 2001, 2: 2003}})
    later = _make_roster_run(tmp_path, 25, {1: {1: 2003, 2: 2001}, 2: {1: 2004, 2: 2001}})
    load_run(original)
    _recommend("whoami", "--season", "2026", "--team", "1")

    week_one = ("scan", "--season", "2026", "--week", "1", "--as-of")
    cutoff = "2026-09-24T00:00:00Z"
    first_scan = _recommend(*week_one, cutoff)
    assert "Start QB 2001" in first_scan

    load_run(later)
    assert _recommend(*week_one, cutoff) == first_scan
    assert "Start QB 2003" in _recommend(*week_one, "2026-09-25T00:00:00Z")
    assert "Start QB 2001" in _recommend("scan", "--season", "2026", "--week", "2", "--as-of", cutoff)
    missing = _recommend(*week_one, "2026-09-23T23:59:59Z", expect_success=False)
    assert "No roster snapshot for team 1" in missing

    load_run(original)
    assert "Start QB 2003" in _recommend(*week_one, "2026-09-25T00:00:00Z")
    with connection() as conn:
        repo = Repository(conn)
        league_id = repo.league_id(99999)
        weekly = repo.roster_at(league_id, 2026, 1, 1)
        assert [entry.provider_player_id for entry in weekly] == [2003]

    load_run(_make_roster_run(tmp_path, 26, {1: {1: None}}))
    with connection() as conn:
        repo = Repository(conn)
        empty = repo.roster_as_of(league_id, 2026, 1, 1, datetime.fromisoformat("2026-09-26T00:00:00Z"))
        assert empty == []
