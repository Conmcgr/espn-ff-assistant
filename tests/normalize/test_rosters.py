"""Unit tests for roster normalizer — synthetic payloads, no I/O."""

from __future__ import annotations

from espn_ff_assistant.normalize.rosters import players, roster_entries, roster_snapshots


def _make_payload(teams_data):
    return {"teams": teams_data}


def _make_team(team_id, player_ids):
    entries = [
        {
            "playerId": pid,
            "lineupSlotId": i,
            "acquisitionType": "DRAFT",
            "playerPoolEntry": {
                "appliedStatTotal": 10.5 * pid,
                "player": {
                    "fullName": f"Player {pid}",
                    "firstName": "Player",
                    "lastName": str(pid),
                    "defaultPositionId": 2,
                    "proTeamId": 1,
                },
            },
        }
        for i, pid in enumerate(player_ids)
    ]
    return {"id": team_id, "roster": {"entries": entries}}


def test_players_extracts_unique_players():
    payload = _make_payload([_make_team(1, [100, 200]), _make_team(2, [200, 300])])
    rows = players(payload)
    pids = {r["provider_player_id"] for r in rows}
    assert pids == {100, 200, 300}


def test_players_extracts_name_and_position():
    payload = _make_payload([_make_team(1, [100])])
    rows = players(payload)
    assert rows[0]["full_name"] == "Player 100"
    assert rows[0]["default_position_id"] == 2


def test_roster_snapshots_one_per_team():
    payload = _make_payload([_make_team(1, [100]), _make_team(2, [200])])
    snaps = roster_snapshots(2024, 5, payload)
    assert len(snaps) == 2
    tids = {s["provider_team_id"] for s in snaps}
    assert tids == {1, 2}


def test_roster_entries_returns_all_entries():
    payload = _make_payload([_make_team(1, [100, 200])])
    entries = roster_entries(2024, 5, payload)
    assert len(entries) == 2
    team_ids = {e[0] for e in entries}
    assert team_ids == {1}


def test_roster_entries_skips_missing_player_id():
    payload = _make_payload(
        [
            {
                "id": 1,
                "roster": {
                    "entries": [
                        {"playerId": None, "lineupSlotId": 0, "playerPoolEntry": {}},
                        {"playerId": 100, "lineupSlotId": 1, "playerPoolEntry": {"appliedStatTotal": 5.0, "player": {}}},
                    ]
                },
            }
        ]
    )
    entries = roster_entries(2024, 5, payload)
    pids = [e[1] for e in entries]
    assert 100 in pids
    assert None not in pids
