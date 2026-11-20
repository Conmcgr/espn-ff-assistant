from __future__ import annotations

import json
import os
import runpy
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest

from espn_ff_assistant import engine
from espn_ff_assistant.config import Settings
from espn_ff_assistant.database import apply_migrations, connection
from espn_ff_assistant.espn_client import ESPNClient, FetchResult
from espn_ff_assistant.loader import load_run
from espn_ff_assistant.notifications import evaluate
from espn_ff_assistant.repository import Repository
from espn_ff_assistant.sync import sync_current
from espn_ff_assistant.worker import WorkerScope, _check_views, _notification_deadlines

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


def _invoke_worker(monkeypatch, capsys, *, league_id=SCOPE.league_id, team_id=SCOPE.team_id,
                   user_id=SCOPE.user_id, extra=()):
    argv = ["worker.py", "--league", str(league_id), "--user", user_id, "--team", str(team_id),
            "--season", str(SCOPE.season), "--week", str(SCOPE.scoring_period)]
    argv.extend(extra)
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


def test_notification_deadlines_only_include_action_players():
    now = datetime(2026, 10, 2, 18, tzinfo=UTC)
    sunday = now + timedelta(days=2)
    saturday = now + timedelta(days=1)
    statuses = {
        111: SimpleNamespace(pro_team_id=1),
        112: SimpleNamespace(pro_team_id=2),
        114: SimpleNamespace(waiver_clear_at=saturday),
        115: SimpleNamespace(waiver_clear_at=None),
    }
    games = {1: SimpleNamespace(kickoff_at=sunday), 2: SimpleNamespace(kickoff_at=saturday),
             3: SimpleNamespace(kickoff_at=now - timedelta(days=1))}
    repo = SimpleNamespace(player_status=lambda *a, **kw: statuses, pro_games=lambda *a: games)
    ctx = SimpleNamespace(repo=repo, league_id="league", season=2026, period=4, as_of_ts=now)
    records = [
        {"kind": "lineup", "payload": {"moves": [{"player_id": 111}]}},
        {"kind": "lineup", "payload": {"moves": [{"player_id": 111}, {"player_id": 112}]}},
        {"kind": "lineup", "payload": {"moves": [{"player_id": 999}]}},
        {"kind": "waiver", "payload": {"add": {"player_id": 114}}},
        {"kind": "waiver", "payload": {"add": {"player_id": 115}}},
        {"kind": "waiver", "payload": {}},
    ]
    assert _notification_deadlines(ctx, records) == [sunday, saturday, None, saturday, None, None]


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


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="set TEST_DATABASE_URL")
def test_outbox_sequence_and_concurrency(tmp_path, monkeypatch, capsys, payloads):
    espn_league = 800_000_000 + uuid4().int % 100_000_000
    payloads["mSettings"]["id"] = espn_league
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setenv("ESPN_LEAGUE_ID", str(espn_league))
    monkeypatch.setenv("ESPN_S2", "synthetic-only")
    monkeypatch.setenv("ESPN_SWID", "synthetic-only")
    monkeypatch.chdir(tmp_path)
    apply_migrations(ROOT / "migrations")
    now = datetime(2026, 10, 1, 18, tzinfo=UTC)

    def fetch(self, season, view, *, scoring_period=None, **kwargs):
        return _fetch(view, payloads.get(view, {}), now, scoring_period)

    monkeypatch.setattr(ESPNClient, "fetch", fetch)
    initial = sync_current(Settings.from_env(), Path("data/raw"), season=SCOPE.season,
                           scoring_period=SCOPE.scoring_period, load=False)
    load_run(initial.run_dir)
    with connection() as conn:
        repo = Repository(conn)
        user = repo.default_user_id()
        league = repo.league_id(espn_league, user_id=user)
        repo.set_user_team(user, league, SCOPE.season, SCOPE.team_id)

    def invoke(label):
        code, result = _invoke_worker(monkeypatch, capsys, user_id=user, league_id=espn_league,
                                     extra=["--outbox", "--now", now.isoformat()])
        assert code == 0, result
        with connection() as conn:
            repo = Repository(conn)
            for row in result["outbox"]:
                assert "expires_at" not in repo.recommendation(row["recommendation_id"]).payload
        with capsys.disabled():
            for row in result["outbox"]:
                print(f"{label} | {row['kind']} | {row['disposition']} | {row['reason']}")
        return result["outbox"]

    assert invoke("daytime")[0]["disposition"] == "send"
    assert invoke("retry")[0]["reason"] == "duplicate"
    roster = payloads["mRoster"]["teams"][0]["roster"]["entries"]
    roster[1]["playerId"] = 115
    roster[1]["playerPoolEntry"]["player"]["id"] = 115
    assert invoke("daily cap new move")[0]["reason"] == "daily_cap"
    now = datetime(2026, 10, 2, 13, 55, tzinfo=UTC)
    roster[1]["playerId"] = 116
    roster[1]["playerPoolEntry"]["player"]["id"] = 116
    pool = payloads["kona_player_info"]["players"][0]
    pool["player"]["id"] = 117
    pool["waiverProcessDate"] = int((now + timedelta(days=1)).timestamp() * 1000)
    # An unrelated Thursday game must not expire Friday/Sunday lineup moves.
    payloads["proTeamSchedules_wl"]["settings"]["proTeams"].append({
        "id": 3, "proGamesByScoringPeriod": {"4": [{
            "id": 102, "homeProTeamId": 3, "awayProTeamId": 5,
            "date": int((now - timedelta(hours=12)).timestamp() * 1000),
        }]},
    })
    assert invoke("quiet new move")[0]["disposition"] == "batch"
    roster[0]["playerPoolEntry"]["player"]["injuryStatus"] = "OUT"
    urgent = invoke("quiet urgent")
    assert urgent[0]["disposition"] == "send" and urgent[0]["reason"] == "escalated"
    with connection() as conn:
        history = Repository(conn).notification_history(user, league, SCOPE.season, now)
        assert any(h["dedupe_key"] == urgent[0]["dedupe_key"] and h["urgency"] == "notify"
                   and h["status"] == "superseded" for h in history)
    assert invoke("urgent retry")[0]["reason"] == "duplicate"
    roster[0]["lineupSlotId"], roster[1]["lineupSlotId"] = 20, 0
    pool["player"]["id"] = 118
    pool["waiverProcessDate"] = int((now - timedelta(minutes=1)).timestamp() * 1000)
    expired = invoke("expired waiver")
    assert any(r["kind"] == "waiver" and r["reason"] == "expired" for r in expired)
    payloads["kona_player_info"]["players"] = []
    with connection() as conn:
        before = len(Repository(conn).notification_history(user, league, SCOPE.season, now))
    assert all(r["reason"] == "not_actionable" for r in invoke("no action"))
    with connection() as conn:
        repo = Repository(conn)
        assert len(repo.notification_history(user, league, SCOPE.season, now)) == before
        ctx = engine.context(repo, league, user, SCOPE.season, SCOPE.scoring_period, now)
        batch = engine.run_lineup(ctx)
        rec = batch.records[0]
        rec.update(decision="notify", payload={"moves": [{"player_id": 999, "from": "Bench", "to": "QB"}]})
        choice = evaluate([rec], [], {}, now, user_id=user,
                          league_season_id=repo.league_season_id(league, SCOPE.season))[0]
        # Unknown deadline still allows the digest five minutes from now.
        assert choice["disposition"] == "batch"

    with connection() as conn:
        before_recs = len(Repository(conn).recommendations(user, league, SCOPE.season))

    def fail_enqueue(*args, **kwargs):
        raise ValueError("synthetic enqueue failure")

    with monkeypatch.context() as patch:
        patch.setattr(Repository, "enqueue_notification", fail_enqueue)
        code, failed = _invoke_worker(patch, capsys, user_id=user, league_id=espn_league,
                                     extra=["--outbox", "--now", now.isoformat()])
        assert code == 1 and failed["error"] == "ValueError" and not failed["outbox"]
    with connection() as conn:
        repo = Repository(conn)
        assert len(repo.recommendations(user, league, SCOPE.season)) == before_recs
        assert len(repo.notification_history(user, league, SCOPE.season, now)) == before

    barrier = Barrier(2)

    def enqueue(concurrent=False):
        with connection() as conn:
            repo = Repository(conn)
            rid = repo.save_recommendation(user, league, SCOPE.season, rec, [])
            if concurrent:
                barrier.wait(timeout=10)
            return repo.enqueue_notification(rid, user, league, SCOPE.season,
                                             SCOPE.scoring_period, choice, now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: enqueue(concurrent=True), range(2)))
    assert sum(r is not None for r in results) == 1
    assert enqueue() is None
    with connection() as conn:
        repo = Repository(conn)
        assert sum(h["dedupe_key"] == choice["dedupe_key"]
                   for h in repo.notification_history(user, league, SCOPE.season, now)) == 1
        assert all(r["expires_at"] > now for r in repo.due_notifications(now))
        assert not any(r["id"] == results[0] or r["id"] == results[1]
                       for r in repo.due_notifications(now + timedelta(minutes=16)))
    with capsys.disabled():
        print("concurrent enqueue + retry | exactly one intent; NO_ACTION | zero new intents; enqueue failure | atomic rollback")
