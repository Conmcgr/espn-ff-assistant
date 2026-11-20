"""Rival signals computed end to end from a loaded synthetic run.

Requires TEST_DATABASE_URL (skipped otherwise).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent
LEAGUE = 424242
NEEDS_DB = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="set TEST_DATABASE_URL to run database integration tests",
)
# season -> {team_id: member}; member "a" runs team 4 in 2025 and team 1 in 2026.
OWNERS = {2025: {1: "c", 2: "b", 3: "d", 4: "a"}, 2026: {1: "a", 2: "b", 3: "c", 4: "d"}}
POSITIONS = {101: 1, 102: 2, 103: 2, 104: 3, 105: 3, 106: 4}  # QB, RB, RB, WR, WR, TE


def _write(run: Path, manifest: list, season: int, view: str, payload: dict, period: int | None = None) -> None:
    rel = f"{season}/{view}/" + (f"scoring-period-{period:02d}/" if period else "") + "response.json"
    (run / rel).parent.mkdir(parents=True, exist_ok=True)
    (run / rel).write_text(json.dumps(payload))
    entry = {"season": season, "view": view, "raw_path": rel, "transport_state": "present", "retrieved_at": "2099-01-01T00:00:00Z"}
    if period:
        entry["scoring_period"] = period
    manifest.append(entry)


def _trade(tx_id: str, kind: str, proposer: int, moves: list[tuple[int, int, int]], day: int) -> dict:
    return {
        "id": tx_id, "type": kind, "status": "EXECUTED" if kind == "TRADE_ACCEPT" else "PENDING",
        "teamId": proposer, "processDate": 1_750_000_000_000 + day * 86_400_000,
        "items": [{"type": "TRADE", "playerId": p, "fromTeamId": f, "toTeamId": t} for p, f, t in moves],
    }


def _waiver(tx_id: str, team: int, bid: int, player: int) -> dict:
    return {
        "id": tx_id, "type": "WAIVER", "status": "EXECUTED", "teamId": team, "bidAmount": bid,
        "processDate": 1_750_000_000_000,
        "items": [{"type": "ADD", "playerId": player, "fromTeamId": 0, "toTeamId": team}],
    }


def _roster(team_players: dict[int, list[int]]) -> dict:
    return {"teams": [
        {"id": tid, "roster": {"entries": [
            {"playerId": pid, "lineupSlotId": 20, "acquisitionType": "DRAFT", "playerPoolEntry": {
                "player": {"fullName": f"Synthetic {pid}", "defaultPositionId": POSITIONS[pid], "proTeamId": 1}}}
            for pid in players
        ]}}
        for tid, players in team_players.items()
    ]}


def make_run(base: Path) -> Path:
    run = base / "20990101T000000Z"
    run.mkdir()
    manifest: list = []
    for season, owners in OWNERS.items():
        settings = {
            "name": "Synthetic League",
            "acquisitionSettings": {"isUsingAcquisitionBudget": True, "acquisitionBudget": 100},
            "rosterSettings": {"lineupSlotCounts": {"0": 1, "2": 2, "4": 2, "6": 1, "20": 4}},
            "scheduleSettings": {"matchupPeriodCount": 6, "playoffTeamCount": 2,
                                 "matchupPeriods": {str(w): [w] for w in range(1, 7)}},
        }
        _write(run, manifest, season, "mStatus", {"status": {"firstScoringPeriod": 1, "finalScoringPeriod": 6, "latestScoringPeriod": 5}})
        _write(run, manifest, season, "mSettings", {"id": LEAGUE, "settings": settings})
        _write(run, manifest, season, "mTeam", {
            "members": [{"id": f"member-{m}", "displayName": m.upper()} for m in owners.values()],
            "teams": [{"id": t, "name": f"Team {m.upper()}", "owners": [f"member-{m}"], "primaryOwner": f"member-{m}"}
                      for t, m in owners.items()],
        })
        # Team 1 wins weeks 1-3, then loses weeks 4-5 (after the as-of cutoff).
        schedule = []
        for week in range(1, 6):
            one_wins = week <= 3
            schedule += [
                {"id": season * 100 + week * 2, "matchupPeriodId": week,
                 "home": {"teamId": 1, "totalPoints": 120.0 if one_wins else 80.0}, "away": {"teamId": 2, "totalPoints": 100.0}},
                {"id": season * 100 + week * 2 + 1, "matchupPeriodId": week,
                 "home": {"teamId": 3, "totalPoints": 110.0}, "away": {"teamId": 4, "totalPoints": 90.0 + week}},
            ]
        _write(run, manifest, season, "mBoxscore", {"settings": settings, "schedule": schedule}, period=1)

    # 2025 history: two proposals to team 2 (member b) accepted, one by member a's old team 4.
    _write(run, manifest, 2025, "mTransactions2", {"transactions": [
        _trade("h1", "TRADE_PROPOSAL", 4, [(201, 4, 2), (202, 2, 4)], 1),
        _trade("h1r", "TRADE_ACCEPT", 4, [(201, 4, 2), (202, 2, 4)], 2),
        _trade("h2", "TRADE_PROPOSAL", 3, [(203, 3, 2), (204, 2, 3)], 3),
        _trade("h2r", "TRADE_ACCEPT", 3, [(203, 3, 2), (204, 2, 3)], 4),
    ]}, period=2)
    # 2026 week 2: team 2 accepts one proposal and declines another.
    _write(run, manifest, 2026, "mTransactions2", {"transactions": [
        _waiver("w1", 1, 30, 105), _waiver("w2", 3, 10, 106),
        _trade("c1", "TRADE_PROPOSAL", 1, [(104, 1, 2), (103, 2, 1)], 10),
        _trade("c1r", "TRADE_ACCEPT", 1, [(104, 1, 2), (103, 2, 1)], 11),
        _trade("c2", "TRADE_PROPOSAL", 3, [(301, 3, 2), (302, 2, 3)], 12),
        _trade("c2r", "TRADE_DECLINE", 3, [(301, 3, 2), (302, 2, 3)], 13),
    ]}, period=2)
    # 2026 week 5, after the cutoff: another accepted proposal and a huge bid.
    _write(run, manifest, 2026, "mTransactions2", {"transactions": [
        _waiver("w3", 1, 60, 106),
        _trade("c3", "TRADE_PROPOSAL", 4, [(303, 4, 2), (304, 2, 4)], 30),
        _trade("c3r", "TRADE_ACCEPT", 4, [(303, 4, 2), (304, 2, 4)], 31),
    ]}, period=5)
    # Week 3 roster: team 1 has QB + one RB; by week 5 it has filled every slot.
    _write(run, manifest, 2026, "mRoster", _roster({1: [101, 102], 2: [104], 3: [], 4: []}), period=3)
    _write(run, manifest, 2026, "mRoster", _roster({1: [101, 102, 103, 104, 105, 106], 2: [], 3: [], 4: []}), period=5)

    (run / "manifest.jsonl").write_text("\n".join(json.dumps(e) for e in manifest))
    return run


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setattr("espn_ff_assistant.database.load_dotenv", lambda: None)
    from espn_ff_assistant.database import apply_migrations, connection
    from espn_ff_assistant.repository import Repository

    with connection() as conn:
        for role in ("anon", "authenticated"):
            if not conn.execute("SELECT count(*) FROM pg_roles WHERE rolname=%s", (role,)).fetchone()[0]:
                conn.execute(f'CREATE ROLE "{role}"')
        conn.commit()
    apply_migrations()
    result = subprocess.run(
        [sys.executable, "scripts/load_run.py", str(make_run(tmp_path))],
        capture_output=True, text=True, cwd=ROOT,
        env={**os.environ, "DATABASE_URL": os.environ["TEST_DATABASE_URL"]},
    )
    assert result.returncode == 0, result.stderr
    with connection() as conn:
        yield Repository(conn)


def _signals(repo, week: int) -> dict[tuple[int, str], object]:
    from espn_ff_assistant.rival_signals import compute_rival_signals

    league_id = repo.league_id(LEAGUE)
    return {(s.provider_team_id, s.name): s for s in compute_rival_signals(repo, league_id, 2026, week)}


@NEEDS_DB
def test_rival_signals_respect_the_as_of_week(repo):
    week3 = _signals(repo, 3)

    accept = week3[(2, "trade_received_accept_rate_linked")]
    assert (accept.value, accept.sample_size, accept.basis) == (0.75, 4, "observed")
    assert (accept.season_from, accept.season_to) == (2025, 2026)
    assert week3[(1, "position_depth_RB")].value == -1
    assert week3[(1, "position_depth_WR")].value == -2
    assert week3[(1, "faab_remaining")].value == 70
    assert (week3[(1, "standings_rank")].value, week3[(1, "win_pct")].value) == (1, 1.0)
    # Member a's 2025 trade (as team 4) counts for their 2026 team 1.
    assert week3[(1, "trade_one_for_one_share")].sample_size == 2

    week5 = _signals(repo, 5)
    assert week5[(2, "trade_received_accept_rate_linked")].value == 0.8
    assert week5[(1, "position_depth_WR")].value == 0
    assert week5[(1, "faab_remaining")].value == 10
    assert week5[(1, "win_pct")].value == 0.6
