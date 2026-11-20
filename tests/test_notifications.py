from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from espn_ff_assistant.notifications import evaluate

NOW = datetime(2026, 10, 1, 18, tzinfo=UTC)


def record(now=NOW, decision="notify", pid=1):
    return dict(kind="lineup", decision=decision, provider_team_id=4, scoring_period=4,
                summary="Start Synthetic Player", as_of_ts=now,
                payload={"moves": [{"player_id": pid, "from": "Bench", "to": "QB"}]})


def policy(records, history=(), settings=None, now=NOW, deadlines=None):
    return evaluate(records, history, settings or {}, now, user_id="user", league_season_id="season", deadlines=deadlines)


def test_dedupe_and_escalation():
    first = policy([record()])[0]
    history = [{**first, "status": "pending", "created_at": NOW}]
    changed = record()
    changed["summary"] = "Different words"
    changed["payload"]["gain"] = 100
    assert policy([changed], history)[0]["reason"] == "duplicate"
    urgent = policy([record(decision="urgent")], history)[0]
    assert urgent["reason"] == "escalated" and urgent["disposition"] == "send"
    assert policy([record(decision="urgent")], history + [{**urgent, "status": "sent", "created_at": NOW}])[0]["reason"] == "duplicate"


@pytest.mark.parametrize("decision", ["no_action", "info", "suppressed"])
def test_not_actionable(decision):
    assert policy([record(decision=decision)])[0]["reason"] == "not_actionable"


def test_quiet_cross_midnight_and_expiry():
    now = datetime(2026, 10, 2, 13, 55, tzinfo=UTC)  # 08:55 Chicago
    row = policy([record(now)], now=now)[0]
    assert row["disposition"] == "batch" and row["deliver_after"].hour == 14
    late = now.replace(hour=4)
    assert policy([record(late)], now=late)[0]["reason"] == "expired"
    urgent = policy([record(late, "urgent")], now=late)[0]
    assert urgent["reason"] == "urgent_bypasses_quiet_hours"


@pytest.mark.parametrize("now", [datetime(2026, 3, 8, 7, 30, tzinfo=UTC), datetime(2026, 11, 1, 6, 30, tzinfo=UTC)])
def test_dst(now):
    rec = record(now)
    row = policy([rec], now=now, deadlines=[now + timedelta(days=1)])[0]
    assert row["disposition"] == "batch"
    assert row["deliver_after"].astimezone(ZoneInfo("America/Chicago")).hour == 9
    assert row["deliver_after"] > now


def test_cap_bad_preferences_empty_and_stale():
    records = [record(pid=i) for i in range(3)]
    deadlines = [NOW + timedelta(days=1)] * len(records)
    rows = policy(records, settings={"quiet_hours": "bad", "max_daily_notifications": -1}, deadlines=deadlines)
    assert [r["disposition"] for r in rows] == ["send", "send", "batch"]
    assert rows[-1]["reason"] == "daily_cap"
    history = [{**rows[0], "created_at": NOW, "status": "sent"}]
    assert policy(records[1:], history, deadlines=deadlines[1:])[-1]["reason"] == "daily_cap"
    assert policy([]) == []
    assert policy([record(NOW - timedelta(minutes=16))])[0]["reason"] == "expired"
    with pytest.raises(ValueError, match="timezone-aware"):
        policy([], now=NOW.replace(tzinfo=None))


def test_deadlines_do_not_mutate_engine_records():
    rec = record()
    payload = {**rec["payload"]}
    assert policy([rec], deadlines=[NOW])[0]["reason"] == "expired"
    assert rec["payload"] == payload
    assert policy([rec], deadlines=[None])[0]["expires_at"] == NOW + timedelta(minutes=15)
    with pytest.raises(ValueError, match="one deadline"):
        policy([rec], deadlines=[])


def test_fall_back_repeated_quiet_end_stays_in_future():
    now = datetime(2026, 11, 1, 7, 30, tzinfo=UTC)  # second 01:30 Chicago
    settings = {"quiet_hours": {"start": "22:00", "end": "01:45", "timezone": "America/Chicago"}}
    row = policy([record(now)], settings=settings, now=now, deadlines=[now + timedelta(days=1)])[0]
    assert row["disposition"] == "batch"
    assert row["deliver_after"] == datetime(2026, 11, 1, 7, 45, tzinfo=UTC)
