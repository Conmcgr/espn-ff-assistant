import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from espn_ff_assistant.game_status import normalize_fantasy_schedule, normalize_game_status

OBSERVED = datetime(2026, 9, 28, 2, tzinfo=UTC)


@pytest.fixture
def payload():
    return json.loads((Path(__file__).parent / "fixtures/scoreboard.json").read_text())


@pytest.fixture
def fantasy():
    return json.loads((Path(__file__).parent / "fixtures/fantasy_schedule.json").read_text())


def test_states_and_completion_evidence(payload):
    batch = normalize_game_status(payload, observed_at=OBSERVED)
    assert batch.state == "present"
    assert [g.state.value for g in batch.games.values()] == [
        "scheduled", "in_progress", "delayed", "postponed", "suspended", "final",
        "unknown", "delayed", "in_progress", "in_progress", "in_progress", "final",
    ]
    for game in batch.games.values():
        assert game.source == "espn_scoreboard"
        assert game.observed_at == OBSERVED
        assert game.kickoff_at < OBSERVED
        assert game.final_observed_at == (OBSERVED if game.state == "final" else None)


@pytest.mark.parametrize("raw", [
    None, 3, "STATUS_FINAL", {}, [],
    {"type": {"name": "STATUS_FINAL", "state": "post", "completed": False}},
    {"type": {"name": "STATUS_FINAL", "state": "in", "completed": True}},
    {"type": {"name": "STATUS_FINAL", "state": "post", "completed": 1}},
    {"type": {"name": "STATUS_FUTURE", "state": "post", "completed": True}},
    {"type": {"name": "STATUS_CANCELED", "state": "post", "completed": True}},
    {"type": {"name": "STATUS_SCHEDULED", "state": "in", "completed": False}},
    {"type": {"name": "STATUS_HALFTIME", "state": "in", "completed": True}},
    {"type": {"name": "STATUS_DELAYED", "state": "post", "completed": False}},
])
def test_unknown_or_inconsistent_status_is_never_final(payload, raw):
    payload["events"][0]["status"] = raw
    record = normalize_game_status(payload, observed_at=OBSERVED).games["demo-1"]
    assert record.state == "unknown"
    assert record.raw_status == raw
    assert record.final_observed_at is None
    del payload["events"][0]["status"]
    assert normalize_game_status(payload, observed_at=OBSERVED).games["demo-1"].state == "unknown"


def test_observations_do_not_overwrite_each_other(payload):
    before = normalize_game_status(payload, observed_at=OBSERVED)
    payload["events"][0]["status"] = payload["events"][5]["status"]
    later = OBSERVED + timedelta(minutes=5)
    after = normalize_game_status(payload, observed_at=later)
    assert before.games["demo-1"].final_observed_at is None
    assert after.games["demo-1"].final_observed_at == later
    assert before.games["demo-6"].final_observed_at == OBSERVED
    assert after.games["demo-6"].final_observed_at == later


@pytest.mark.parametrize("name", ["STATUS_FINAL", "STATUS_FINAL_OVERTIME", "STATUS_FINAL_2OT", "STATUS_FINAL_OT"])
def test_final_variants_require_all_evidence(payload, name):
    status = payload["events"][0]["status"]["type"]
    status.update(name=name, state="post", completed=True)
    assert normalize_game_status(payload, observed_at=OBSERVED).games["demo-1"].state == "final"
    status["completed"] = False
    assert normalize_game_status(payload, observed_at=OBSERVED).games["demo-1"].state == "unknown"


@pytest.mark.parametrize(("data", "state"), [
    (None, "missing"), ({}, "missing"), ([], "malformed"),
    ({"events": {}}, "malformed"), ({"events": []}, "empty"),
    ({"events": [None, {}]}, "malformed"),
])
def test_payload_quality(data, state):
    assert normalize_game_status(data, observed_at=OBSERVED).state == state


@pytest.mark.parametrize("normalizer", [normalize_game_status, normalize_fantasy_schedule])
def test_transport_failure_and_timestamp_validation(normalizer):
    batch = normalizer({}, observed_at=OBSERVED, fetch_state="error")
    assert batch.state == "failed"
    assert not batch.games
    with pytest.raises(ValueError, match="timezone-aware"):
        normalizer({}, observed_at=datetime(2026, 9, 28))


def test_duplicates_and_conflicts(payload):
    entries = payload["events"]
    entries.append(copy.deepcopy(entries[5]))
    assert len(normalize_game_status(payload, observed_at=OBSERVED).games) == 12
    entries[-1]["status"]["type"]["completed"] = False
    entries.append(copy.deepcopy(entries[5]))
    batch = normalize_game_status(payload, observed_at=OBSERVED)
    assert batch.state == "partial"
    assert batch.games["demo-6"].state == "unknown"
    assert batch.games["demo-6"].final_observed_at is None


@pytest.mark.parametrize("date", ["yesterday", "2026-09-27T17:00", [], 123])
def test_malformed_entries_and_kickoff(payload, date):
    payload["events"].extend([{}, None, {"id": True}])
    payload["events"][0]["date"] = date
    batch = normalize_game_status(payload, observed_at=OBSERVED)
    assert batch.state == "partial"
    assert len(batch.games) == 12
    assert batch.games["demo-1"].kickoff_at is None
    assert batch.issues


@pytest.mark.parametrize("official", [True, False, None, 1, "true"])
def test_fantasy_official_evidence(fantasy, official):
    team = fantasy["settings"]["proTeams"][0]
    game = team["proGamesByScoringPeriod"]["3"][0]
    game["statsOfficial"] = official
    fantasy["settings"]["proTeams"].append(copy.deepcopy(team))
    batch = normalize_fantasy_schedule(fantasy, observed_at=OBSERVED)
    assert batch.state == "present"
    record = batch.games["demo-1"]
    assert record.source == "espn_fantasy_schedule"
    assert record.raw_status == {"statsOfficial": official}
    assert record.final_observed_at == (OBSERVED if official is True else None)
    assert record.kickoff_at < OBSERVED
    assert batch.games["demo-2"].state == "unknown"
    del game["statsOfficial"]
    assert normalize_fantasy_schedule(fantasy, observed_at=OBSERVED).games["demo-1"].state == "unknown"


@pytest.mark.parametrize(("data", "state"), [
    (None, "missing"), ({}, "missing"), ({"settings": {}}, "missing"),
    ({"settings": []}, "malformed"),
    ({"settings": {"proTeams": {}}}, "malformed"),
    ({"settings": {"proTeams": []}}, "empty"),
    ({"settings": {"proTeams": [{"id": 0}]}}, "empty"),
    ({"settings": {"proTeams": [{"id": 1, "proGamesByScoringPeriod": {}}]}}, "empty"),
    ({"settings": {"proTeams": [None]}}, "malformed"),
    ({"settings": {"proTeams": [{"id": 1}]}}, "malformed"),
    ({"settings": {"proTeams": [{"id": 1, "proGamesByScoringPeriod": {"1": None}}]}}, "malformed"),
])
def test_fantasy_quality(data, state):
    assert normalize_fantasy_schedule(data, observed_at=OBSERVED).state == state
