from __future__ import annotations

import json
import os
import runpy
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from psycopg import IsolationLevel
from psycopg.errors import SerializationFailure

from espn_ff_assistant.config import Settings
from espn_ff_assistant.database import apply_migrations, connection
from espn_ff_assistant.espn_client import ESPNClient, FetchResult
from espn_ff_assistant.loader import load_run
from espn_ff_assistant.opportunities import record_scan
from espn_ff_assistant.repository import Repository
from espn_ff_assistant.sync import sync_current
from espn_ff_assistant.worker import WorkerScope, _check_views

ROOT = Path(__file__).resolve().parents[1]
SCOPE = WorkerScope(79301, "e5f5e3ae-dfa4-4b8a-a18f-a72d249d4585", 4, 2026, 4)


def _player(pid, points):
    return {
        "id": pid, "fullName": f"Synthetic Player {pid}", "defaultPositionId": 1,
        "proTeamId": 1, "eligibleSlots": [0, 20], "injuryStatus": "ACTIVE",
        "stats": [
            {"seasonId": SCOPE.season, "scoringPeriodId": SCOPE.scoring_period,
             "statSourceId": 1, "statSplitTypeId": 1, "appliedTotal": points},
            {"seasonId": SCOPE.season, "scoringPeriodId": 0,
             "statSourceId": 1, "statSplitTypeId": 2, "appliedTotal": points * 10},
        ],
    }


@pytest.fixture
def payloads():
    def entry(pid, points, slot):
        return {"playerId": pid, "lineupSlotId": slot, "playerPoolEntry": {
            "player": _player(pid, points), "lineupLocked": False,
        }}

    return {
        "mStatus": {"status": {"firstScoringPeriod": 1, "latestScoringPeriod": 4, "finalScoringPeriod": 17}},
        "mSettings": {"id": SCOPE.league_id, "settings": {
            "rosterSettings": {"lineupSlotCounts": {"0": 1, "20": 1}},
            "acquisitionSettings": {"isUsingAcquisitionBudget": True, "acquisitionBudget": 100},
        }},
        "mTeam": {"teams": [{"id": 4, "name": "Test Home"}, {"id": 7, "name": "Test Away"}]},
        "mRoster": {"scoringPeriodId": 4, "teams": [
            {"id": 4, "roster": {"entries": [entry(111, 8, 0), entry(112, 26, 20)]}},
            {"id": 7, "roster": {"entries": [entry(113, 10, 0)]}},
        ]},
        "mTransactions2": {"transactions": []},
        "kona_player_info": {"players": [{"player": _player(114, 45), "status": "WAIVERS"}]},
        "proTeamSchedules_wl": {"settings": {"proTeams": [{"id": 1, "proGamesByScoringPeriod": {
            "4": [{"id": 101, "homeProTeamId": 1, "awayProTeamId": 2, "date": 1791133200000}],
        }}]}},
    }


def _fetch(view, payload, now, period=None):
    return FetchResult(SCOPE.season, view, period, "https://example.test", now.isoformat(), 200, payload, "present")


@pytest.mark.parametrize(("view", "variant", "expected"), [
    ("mSettings", "wrong_league", "wrong_scope"),
    ("mStatus", "wrong_week", "wrong_period"),
    ("mTeam", "wrong_team", "wrong_team"),
    ("mRoster", "missing_team", "partial"),
    ("mRoster", "old", "stale"),
    ("mTransactions2", "unchanged", "valid_empty"),
    ("mTransactions2", "omit", "missing"),
    ("mTransactions2", "empty_object", "partial"),
    ("kona_player_info", "empty_pool", "valid_empty"),
    ("kona_player_info", "empty_object", "partial"),
    ("kona_player_info", "http_failure", "failed"),
    ("kona_player_info", "rostered_in_pool", "partial"),
    ("proTeamSchedules_wl", "empty_object", "partial"),
    ("proTeamSchedules_wl", "missing_pro_team", "partial"),
])
def test_required_view_states(payloads, view, variant, expected):
    now = datetime.now(UTC)
    period_inputs = {"mRoster", "mTransactions2", "kona_player_info"}
    replacements = {
        "wrong_league": {"id": 222},
        "wrong_week": {"status": {"firstScoringPeriod": 1, "latestScoringPeriod": 5, "finalScoringPeriod": 17}},
        "wrong_team": {"teams": [{"id": 7}]},
        "missing_team": {"teams": payloads["mRoster"]["teams"][:1]},
        "empty_pool": {"players": []},
        "rostered_in_pool": {"players": [{"player": _player(111, 45), "status": "WAIVERS"}]},
        "missing_pro_team": {"settings": {"proTeams": [{"id": 3, "byeWeek": SCOPE.scoring_period}]}},
        "empty_object": {},
    }
    fetches = []
    for name, payload in payloads.items():
        if name == view and variant == "omit":
            continue
        period = SCOPE.scoring_period if name in period_inputs else None
        fetch = _fetch(name, payload, now, period)
        if name == view:
            if variant in replacements:
                fetch = replace(fetch, payload=replacements[variant])
            elif variant == "old":
                fetch = replace(fetch, retrieved_at=(now - timedelta(minutes=16)).isoformat())
            elif variant == "http_failure":
                fetch = replace(fetch, status_code=401, payload=None, state="error")
        fetches.append(fetch)
    states, _ = _check_views(tuple(fetches), SCOPE, now)
    assert states[view] == expected


def _invoke_worker(monkeypatch, capsys, *, league_id=SCOPE.league_id, team_id=SCOPE.team_id, user_id=SCOPE.user_id):
    argv = ["worker.py", "--league", str(league_id), "--user", user_id, "--team", str(team_id),
            "--season", str(SCOPE.season), "--week", str(SCOPE.scoring_period)]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(ROOT / "scripts" / "worker.py"), run_name="__main__")
    return exc.value.code, json.loads(capsys.readouterr().out)


def test_worker_reports_missing_database_configuration(monkeypatch, capsys):
    monkeypatch.setattr("espn_ff_assistant.config.load_dotenv", lambda: None)
    monkeypatch.setattr("espn_ff_assistant.database.load_dotenv", lambda: None)
    monkeypatch.setenv("ESPN_LEAGUE_ID", str(SCOPE.league_id))
    monkeypatch.setenv("ESPN_S2", "synthetic-only")
    monkeypatch.setenv("ESPN_SWID", "synthetic-only")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    code, output = _invoke_worker(monkeypatch, capsys)
    assert code == 2 and output["status"] == "CONFIGURATION_ERROR"
    assert "DATABASE_URL" in output["error"]


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="set TEST_DATABASE_URL for the worker smoke test")
def test_worker_cli_shadow_round_trip(tmp_path, monkeypatch, capsys, payloads):
    """Run the CLI, archive, loader, repository, and engines; only ESPN is simulated."""
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setenv("ESPN_LEAGUE_ID", str(SCOPE.league_id))
    monkeypatch.setenv("ESPN_S2", "synthetic-only")
    monkeypatch.setenv("ESPN_SWID", "synthetic-only")
    monkeypatch.chdir(tmp_path)
    apply_migrations(ROOT / "migrations")
    stale_view = None
    transaction = {"id": "synthetic-waiver", "status": "EXECUTED", "type": "WAIVER", "teamId": 7,
                   "items": [{"type": "ADD", "playerId": 113, "toTeamId": 7}]}
    payloads["mTransactions2"]["transactions"] = [transaction]

    def fetch(self, season, view, *, scoring_period=None, **kwargs):
        assert season == SCOPE.season and self.settings.league_id == SCOPE.league_id
        now = datetime.now(UTC)
        if stale_view == view:
            now -= timedelta(minutes=16)
        return _fetch(view, payloads.get(view, {}), now, scoring_period)

    monkeypatch.setattr(ESPNClient, "fetch", fetch)
    initial = sync_current(Settings.from_env(), Path("data/raw"), season=SCOPE.season,
                           scoring_period=SCOPE.scoring_period, load=False)
    load_run(initial.run_dir)
    with connection() as conn:
        repo = Repository(conn)
        user = repo.default_user_id()
        league = repo.league_id(SCOPE.league_id, user_id=user)
        repo.set_user_team(user, league, SCOPE.season, SCOPE.team_id)

    def invoke(label, *, league_id=SCOPE.league_id, team_id=SCOPE.team_id, user_id=user):
        code, output = _invoke_worker(monkeypatch, capsys, league_id=league_id, team_id=team_id, user_id=user_id)
        with capsys.disabled():
            print(f"{label}: exit={code}, status={output['status']}, "
                  f"alert_eligible={output['alert_eligible']}, decisions={output['decisions']}, "
                  f"roster={output['views'].get('mRoster')}, pool={output['views'].get('kona_player_info')}")
        return code, output

    code, actionable = invoke("fresh")
    assert code == 0 and actionable["status"] == "ACTIONABLE" and actionable["alert_eligible"]
    assert actionable["decisions"]["lineup"] == {"notify": 1}
    assert actionable["decisions"]["waiver"] == {"notify": 1}

    transaction.pop("items")
    code, partial = invoke("partial transaction")
    assert code == 1 and partial["status"] == "SYNC_FAILED" and not partial["alert_eligible"]
    assert partial["views"]["mTransactions2"] == "partial"
    with connection() as conn:
        txns = Repository(conn).transactions(league, SCOPE.season, include_items=True)
        stored = next(tx for tx in txns if tx.provider_transaction_id == transaction["id"])
        assert [item["provider_player_id"] for item in stored.items] == [113]
    payloads["mTransactions2"]["transactions"] = []

    stale_view = "mRoster"
    code, stale = invoke("stale roster")
    assert code == 1 and stale["status"] == "SYNC_FAILED" and not stale["alert_eligible"]
    assert stale["views"]["mRoster"] == "stale" and not stale["decisions"]
    with connection() as conn:
        assert Repository(conn).completed_sync_id(league, stale["run_id"]) is None
    stale_view = None

    for label, overrides in [
        ("wrong league", {"league_id": 999}),
        ("wrong team", {"team_id": 7}),
        ("wrong user", {"user_id": SCOPE.user_id}),
    ]:
        code, rejected = invoke(label, **overrides)
        assert code == 2 and rejected["status"] == "SCOPE_REJECTED"
        assert not rejected["alert_eligible"] and rejected["run_id"] is None

    # Keep the previously loaded attractive waiver candidate in PostgreSQL.
    # The new explicitly empty snapshot must not fall back to it.
    payloads["kona_player_info"]["players"] = []
    roster = payloads["mRoster"]["teams"][0]["roster"]["entries"]
    roster[0]["lineupSlotId"], roster[1]["lineupSlotId"] = 20, 0
    code, quiet = invoke("empty pool after populated run")
    assert code == 0 and quiet["status"] == "NO_ACTION" and not quiet["alert_eligible"]
    assert quiet["views"]["kona_player_info"] == quiet["views"]["mTransactions2"] == "valid_empty"
    assert quiet["decisions"] == {"lineup": {"no_action": 1}, "waiver": {"no_action": 1}}


@pytest.mark.skipif(not os.getenv('TEST_DATABASE_URL'), reason='set TEST_DATABASE_URL')
def test_opportunity_demo(tmp_path, monkeypatch, capsys, payloads):
    monkeypatch.setattr(sys.modules[__name__], 'SCOPE', replace(
        SCOPE, league_id=80000 + uuid4().int % 1000000000,
    ))
    payloads['mSettings']['id'] = SCOPE.league_id
    monkeypatch.setenv('DATABASE_URL', os.environ['TEST_DATABASE_URL'])
    monkeypatch.setenv('ESPN_LEAGUE_ID', str(SCOPE.league_id))
    monkeypatch.setenv('ESPN_S2', 'synthetic-only')
    monkeypatch.setenv('ESPN_SWID', 'synthetic-only')
    monkeypatch.chdir(tmp_path)
    apply_migrations(ROOT / 'migrations')
    now = datetime.now(UTC) - timedelta(minutes=5)
    payloads['mTransactions2']['transactions'] = [{
        'id': 'synthetic-drop-114', 'status': 'EXECUTED', 'type': 'ROSTER',
        'processDate': int((now - timedelta(hours=1)).timestamp() * 1000),
        'items': [{'type': 'DROP', 'playerId': 114, 'fromTeamId': 7}],
    }]
    starter = payloads['mRoster']['teams'][0]['roster']['entries'][0]
    starter['playerPoolEntry']['player']['injuryStatus'] = 'OUT'
    game = payloads['proTeamSchedules_wl']['settings']['proTeams'][0]['proGamesByScoringPeriod']['4'][0]
    game['date'] = int((now + timedelta(hours=2)).timestamp() * 1000)

    def fetch(self, season, view, *, scoring_period=None, **kwargs):
        return _fetch(view, payloads.get(view, {}), now, scoring_period)

    monkeypatch.setattr(ESPNClient, 'fetch', fetch)
    initial = sync_current(Settings.from_env(), Path('data/raw'), season=SCOPE.season,
                           scoring_period=SCOPE.scoring_period, load=False)
    load_run(initial.run_dir)
    with connection() as conn:
        repo = Repository(conn)
        user = repo.default_user_id()
        league = repo.league_id(SCOPE.league_id, user_id=user)
        repo.set_user_team(user, league, SCOPE.season, SCOPE.team_id)

    def scan(label, expected, replay=False):
        code, output = _invoke_worker(monkeypatch, capsys, user_id=user, league_id=SCOPE.league_id)
        assert code == 0, output
        ledger = output['opportunities']
        assert ledger['counts'] == dict(zip(
            ('new', 'changed', 'unchanged', 'expired'), expected, strict=True,
        ))
        assert ledger['skipped_replay'] is replay
        with capsys.disabled():
            print(f"{label}: status={output['status']} replay={replay} events={ledger['events']}")
        return output

    class ExpiringClock(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            cls.calls += 1
            return datetime.now(tz) + timedelta(minutes=16 if cls.calls > 1 else 0)

    with monkeypatch.context() as patch:
        patch.setattr("espn_ff_assistant.worker.datetime", ExpiringClock)
        code, expired = _invoke_worker(
            monkeypatch, capsys, user_id=user, league_id=SCOPE.league_id,
        )
    assert code == 1 and expired["status"] == "SYNC_FAILED"
    assert expired["error"] == "required_views_expired" and not expired["opportunities"]
    with connection() as conn:
        repo = Repository(conn)
        assert repo.latest_opportunity_observation(league, SCOPE.season) is None
        assert repo.stored_opportunities(league, SCOPE.season) == []
        assert not repo.opportunity_scan_processed(
            league, SCOPE.season, repo.completed_sync_id(league, expired["run_id"]),
        )

    first = scan('scan1', (2, 0, 0, 0))
    payloads['mTransactions2']['transactions'] = []
    now += timedelta(seconds=1)
    scan('scan2', (0, 0, 2, 0))
    scan('same observation', (0, 0, 0, 0), True)
    now -= timedelta(seconds=1)
    scan('older observation', (0, 0, 0, 0), True)
    with connection() as conn:
        repo = Repository(conn)
        sync_id = repo.completed_sync_id(league, first['run_id'])
        repeated = record_scan(repo, league, SCOPE.season, SCOPE.scoring_period,
                                             sync_id, now + timedelta(seconds=2))
        assert repeated['skipped_replay'] and not repeated['events']
    now += timedelta(seconds=2)
    payloads['kona_player_info']['players'][0]['status'] = 'FREEAGENT'
    scan('scan3', (0, 1, 1, 0))
    now += timedelta(seconds=1)
    pool = payloads['kona_player_info']['players'].pop()
    payloads['mRoster']['teams'][1]['roster']['entries'].append({
        'playerId': 114, 'lineupSlotId': 20, 'playerPoolEntry': pool,
    })
    starter['lineupSlotId'] = 20
    scan('scan4', (0, 0, 0, 2))


@pytest.fixture
def synthetic_ledger(tmp_path, monkeypatch, payloads):
    if not os.getenv("TEST_DATABASE_URL"):
        pytest.skip("set TEST_DATABASE_URL")
    monkeypatch.setattr(sys.modules[__name__], "SCOPE", replace(
        SCOPE, league_id=80000 + uuid4().int % 1000000000,
    ))
    payloads["mSettings"]["id"] = SCOPE.league_id
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setenv("ESPN_LEAGUE_ID", str(SCOPE.league_id))
    monkeypatch.setenv("ESPN_S2", "synthetic-only")
    monkeypatch.setenv("ESPN_SWID", "synthetic-only")
    monkeypatch.chdir(tmp_path)
    apply_migrations(ROOT / "migrations")
    starter = payloads["mRoster"]["teams"][0]["roster"]["entries"][0]
    starter["playerPoolEntry"]["player"]["injuryStatus"] = "OUT"
    game = payloads["proTeamSchedules_wl"]["settings"]["proTeams"][0]["proGamesByScoringPeriod"]["4"][0]
    game["date"] = int((datetime.now(UTC) + timedelta(hours=2)).timestamp() * 1000)

    def fetch(self, season, view, *, scoring_period=None, **kwargs):
        return _fetch(view, payloads.get(view, {}), datetime.now(UTC), scoring_period)

    monkeypatch.setattr(ESPNClient, "fetch", fetch)

    def load_scan():
        sync = sync_current(Settings.from_env(), Path("data/raw"), season=SCOPE.season,
                            scoring_period=SCOPE.scoring_period, load=False)
        load_run(sync.run_dir)
        with connection() as conn:
            repo = Repository(conn)
            user = repo.default_user_id()
            league = repo.league_id(SCOPE.league_id, user_id=user)
            repo.set_user_team(user, league, SCOPE.season, SCOPE.team_id)
            return league, repo.completed_sync_id(league, sync.run_dir.name)

    return load_scan


@pytest.mark.parametrize("existing_state", [False, True])
def test_concurrent_opportunity_scans(synthetic_ledger, existing_state):
    league, first_sync = synthetic_ledger()
    _, second_sync = synthetic_ledger()
    now = datetime.now(UTC)
    if existing_state:
        with connection() as conn:
            Repository(conn).lock_opportunity_season(league, SCOPE.season)

    with connection() as first, connection() as second:
        first.isolation_level = second.isolation_level = IsolationLevel.REPEATABLE_READ
        first_repo = Repository(first, player_sync_id=first_sync)
        second_repo = Repository(second, player_sync_id=second_sync)
        assert second_repo.latest_opportunity_observation(league, SCOPE.season) is None
        recorded = record_scan(first_repo, league, SCOPE.season, SCOPE.scoring_period, first_sync, now)
        assert recorded["counts"]["new"] == 1
        started = Event()

        def competing_scan():
            started.set()
            return record_scan(second_repo, league, SCOPE.season, SCOPE.scoring_period,
                               second_sync, now + timedelta(seconds=1))

        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(competing_scan)
            assert started.wait(timeout=5)
            first.commit()
            with pytest.raises(SerializationFailure):
                pending.result(timeout=5)
        second.rollback()

    with connection() as conn:
        repo = Repository(conn, player_sync_id=second_sync)
        assert len(repo.stored_opportunities(league, SCOPE.season)) == 1
        assert [event.change for event in repo.opportunity_events_for_sync(
            league, SCOPE.season, first_sync,
        )] == ["new"]
        assert repo.opportunity_events_for_sync(league, SCOPE.season, second_sync) == []
        assert not repo.opportunity_scan_processed(league, SCOPE.season, second_sync)
        retried = record_scan(repo, league, SCOPE.season, SCOPE.scoring_period,
                              second_sync, now + timedelta(seconds=1))
        assert retried["counts"] == {"new": 0, "changed": 0, "unchanged": 1, "expired": 0}

    with connection() as conn:
        repo = Repository(conn)
        assert [event.change for event in repo.opportunity_events_for_sync(
            league, SCOPE.season, second_sync,
        )] == ["unchanged"]


def test_worker_serialization_failure_rolls_back(synthetic_ledger, monkeypatch, capsys):
    league, _ = synthetic_ledger()

    def conflicting_scan(repo, league, season, period, sync_id, observed_at):
        repo.lock_opportunity_season(league, season)
        repo.insert_opportunity_scan(league, season, sync_id, observed_at)
        raise SerializationFailure("synthetic concurrent ledger update")

    monkeypatch.setattr("espn_ff_assistant.opportunities.record_scan", conflicting_scan)
    with connection() as conn:
        user = Repository(conn).default_user_id()
    code, output = _invoke_worker(monkeypatch, capsys, user_id=user, league_id=SCOPE.league_id)
    assert code == 1 and output["status"] == "EVALUATION_FAILED"
    assert output["error"] == "SerializationFailure" and not output["opportunities"]
    with connection() as conn:
        repo = Repository(conn)
        sync_id = repo.completed_sync_id(league, output["run_id"])
        assert not repo.opportunity_scan_processed(league, SCOPE.season, sync_id)
        assert repo.latest_opportunity_observation(league, SCOPE.season) is None
        assert repo.stored_opportunities(league, SCOPE.season) == []


def test_pending_drop_becomes_executed(synthetic_ledger, payloads):
    transaction = {
        "id": "synthetic-pending-drop", "status": "PENDING", "type": "ROSTER",
        "items": [{"type": "DROP", "playerId": 114, "fromTeamId": 7}],
    }
    payloads["mTransactions2"]["transactions"] = [transaction]
    league, pending_sync = synthetic_ledger()
    with connection() as conn:
        repo = Repository(conn, player_sync_id=pending_sync)
        assert repo.executed_drops(league, SCOPE.season, SCOPE.scoring_period) == []
        pending = record_scan(repo, league, SCOPE.season, SCOPE.scoring_period,
                              pending_sync, datetime.now(UTC))
        assert pending["counts"]["new"] == 1

    processed_ms = int((datetime.now(UTC) - timedelta(hours=1)).timestamp() * 1000)
    processed = datetime.fromtimestamp(processed_ms / 1000, UTC)
    transaction.update(status="EXECUTED", processDate=processed_ms)
    _, executed_sync = synthetic_ledger()
    with connection() as conn:
        repo = Repository(conn, player_sync_id=executed_sync)
        drops = repo.executed_drops(league, SCOPE.season, SCOPE.scoring_period)
        assert len(drops) == 1 and drops[0].process_date == processed
        executed = record_scan(repo, league, SCOPE.season, SCOPE.scoring_period,
                               executed_sync, datetime.now(UTC))
        assert executed["counts"] == {"new": 1, "changed": 0, "unchanged": 1, "expired": 0}
        assert any(event["kind"] == "player_dropped" and event["change"] == "new"
                   for event in executed["events"])

    transaction.pop("processDate")
    _, later_sync = synthetic_ledger()
    with connection() as conn:
        repo = Repository(conn, player_sync_id=later_sync)
        drops = repo.executed_drops(league, SCOPE.season, SCOPE.scoring_period)
        assert drops[0].process_date == processed
        later = record_scan(repo, league, SCOPE.season, SCOPE.scoring_period,
                            later_sync, datetime.now(UTC))
        assert later["counts"] == {"new": 0, "changed": 0, "unchanged": 2, "expired": 0}
