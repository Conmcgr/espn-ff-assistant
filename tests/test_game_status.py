import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from espn_ff_assistant.game_status import GameState, normalize_game_status

OBSERVED = datetime(2026, 9, 28, 2, tzinfo=UTC)


@pytest.fixture
def payload():
    return json.loads((Path(__file__).parent / "fixtures/game_status.json").read_text())


def test_states_and_completion_evidence(payload):
    batch = normalize_game_status(payload, observed_at=OBSERVED)
    assert batch.state == "present"
    assert [g.state for g in batch.games.values()] == list(GameState)
    for game in batch.games.values():
        assert game.observed_at == OBSERVED
        assert game.kickoff_at < OBSERVED
        assert game.final_observed_at == (OBSERVED if game.state == GameState.FINAL else None)


@pytest.mark.parametrize("raw", [None, 3, "FINAL", "STATUS_UNRECOGNIZED", {}, [], True])
def test_missing_unmapped_status_is_never_final(payload, raw):
    game = payload["settings"]["proTeams"][0]["proGamesByScoringPeriod"]["3"][0]
    game["status"] = raw
    record = normalize_game_status(payload, observed_at=OBSERVED).games["demo-1"]
    assert record.state == GameState.UNKNOWN
    assert record.raw_status == raw
    assert record.final_observed_at is None
    del game["status"]
    assert normalize_game_status(payload, observed_at=OBSERVED).games["demo-1"].state == "unknown"


def test_observations_do_not_overwrite_each_other(payload):
    before = normalize_game_status(payload, observed_at=OBSERVED)
    payload["settings"]["proTeams"][0]["proGamesByScoringPeriod"]["3"][0]["status"] = "STATUS_FINAL"
    later = OBSERVED + timedelta(minutes=5)
    after = normalize_game_status(payload, observed_at=later)
    assert before.games["demo-1"].final_observed_at is None
    assert after.games["demo-1"].final_observed_at == later
    assert before.games["demo-6"].final_observed_at == OBSERVED
    assert after.games["demo-6"].final_observed_at == later


@pytest.mark.parametrize(("data", "state"), [
    (None, "missing"), ({}, "missing"), ({"settings": {}}, "missing"),
    ([], "malformed"), ({"settings": []}, "malformed"),
    ({"settings": {"proTeams": {}}}, "malformed"),
    ({"settings": {"proTeams": []}}, "empty"),
    ({"settings": {"proTeams": [{"id": 1, "proGamesByScoringPeriod": {}}]}}, "empty"),
    ({"settings": {"proTeams": [None]}}, "malformed"),
    ({"settings": {"proTeams": [{"id": 1}]}}, "malformed"),
])
def test_payload_quality(data, state):
    assert normalize_game_status(data, observed_at=OBSERVED).state == state


def test_transport_failure_and_timestamp_validation(payload):
    batch = normalize_game_status(payload, observed_at=OBSERVED, fetch_state="error")
    assert batch.state == "failed"
    assert not batch.games
    with pytest.raises(ValueError, match="timezone-aware"):
        normalize_game_status(payload, observed_at=datetime(2026, 9, 28))


def test_duplicate_teams_and_conflicts(payload):
    teams = payload["settings"]["proTeams"]
    teams.append(copy.deepcopy(teams[0]))
    assert len(normalize_game_status(payload, observed_at=OBSERVED).games) == 7
    teams[1]["proGamesByScoringPeriod"]["3"][5]["status"] = "STATUS_SUSPENDED"
    teams.append(copy.deepcopy(teams[0]))
    batch = normalize_game_status(payload, observed_at=OBSERVED)
    assert batch.state == "partial"
    assert batch.games["demo-6"].state == "unknown"
    assert batch.games["demo-6"].final_observed_at is None


def test_malformed_entries_and_kickoff(payload):
    entries = payload["settings"]["proTeams"][0]["proGamesByScoringPeriod"]["3"]
    entries.extend([{}, None, {"id": True}])
    entries[0]["date"] = "yesterday"
    batch = normalize_game_status(payload, observed_at=OBSERVED)
    assert batch.state == "partial"
    assert len(batch.games) == 7
    assert batch.games["demo-1"].kickoff_at is None
    assert batch.issues
