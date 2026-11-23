"""The read-only API must deduplicate work, preserve freshness and fail privately."""

from concurrent.futures import Future
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("flask")

from espn_ff_assistant.web import app as web  # noqa: E402
from espn_ff_assistant.web import data  # noqa: E402


class ControlledPool:
    def __init__(self, **kwargs):
        self.jobs = []

    def submit(self, loader, section):
        future = Future()
        self.jobs.append((section, future))
        return future


@pytest.fixture
def client(monkeypatch):
    pool = ControlledPool()
    monkeypatch.setattr(web, "ThreadPoolExecutor", lambda **kw: pool)
    app = web.create_app()
    app.config["TESTING"] = True
    return app.test_client(), pool


def test_reads_deduplicate_and_preserve_original_snapshot_time(client):
    client, pool = client
    assert client.get("/api/lineup").status_code == 202
    assert client.get("/api/lineup").status_code == 202
    assert len(pool.jobs) == 1
    pool.jobs[0][1].set_result({"week": 11, "synced_at": "original snapshot", "data": {}})
    response = client.get("/api/lineup")
    assert response.json["synced_at"] == "original snapshot"
    assert response.json["week"] == 11
    assert response.headers["Cache-Control"] == "private, no-store"
    assert "noindex" in response.headers["X-Robots-Tag"]


def test_failed_initial_read_never_exposes_credentials(client, caplog):
    client, pool = client
    client.get("/api/waivers")
    pool.jobs[0][1].set_exception(RuntimeError("postgresql://private-password@private-host"))
    response = client.get("/api/waivers")
    assert response.status_code == 503
    assert "private-password" not in response.text + caplog.text
    assert "private-host" not in response.text + caplog.text


def test_failed_refresh_keeps_old_result_with_explicit_warning(client, monkeypatch):
    client, pool = client
    clock = [10]
    monkeypatch.setattr(web.time, "monotonic", lambda: clock[0])
    client.get("/api/lineup")
    pool.jobs[-1][1].set_result({"synced_at": "old", "data": {}})
    assert not client.get("/api/lineup").json["refreshing"]
    clock[0] += 301
    assert client.get("/api/lineup").json["refreshing"]
    pool.jobs[-1][1].set_exception(ValueError("unavailable"))
    response = client.get("/api/lineup").json
    assert response["synced_at"] == "old"
    assert response["refresh_failed"]
    assert not response["refreshing"]


def test_no_write_routes_or_unbounded_work_names(client):
    client, pool = client
    for path in ("/api/lineup", "/api/trades", "/api/send", "/api/sync"):
        assert client.post(path).status_code == 405
    assert client.get("/api/unknown").status_code == 404
    assert not pool.jobs
    assert client.get("/.env").status_code == 404
    assert client.get("/assets/../../.env").status_code == 404
    assert client.get("/").status_code == 200
    assert client.get("/assets/app.js").status_code == 200


def test_cached_and_refreshing_trades_disappear_at_exact_deadline(client, monkeypatch):
    client, pool = client
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    clock = [deadline.timestamp() - 1]
    monotonic = [10]
    monkeypatch.setattr(web.time, "time", lambda: clock[0])
    monkeypatch.setattr(web.time, "monotonic", lambda: monotonic[0])
    client.get("/api/trades")
    pool.jobs[0][1].set_result({"data": {"deadline": deadline, "ideas": [{"give": ["A"]}]}})
    assert client.get("/api/trades").json["data"]["ideas"]
    clock[0] = deadline.timestamp()
    result = client.get("/api/trades").json
    assert not result["data"]["ideas"]
    assert "passed" in result["data"]["notice"]
    monotonic[0] += 301
    result = client.get("/api/trades").json
    assert result["refreshing"] and not result["data"]["ideas"]


def test_loader_enforces_read_only_and_uses_live_snapshot_week(monkeypatch):
    conn = SimpleNamespace(read_only=False, isolation_level=None)
    now = datetime.now(UTC)
    calls = []

    @contextmanager
    def connection():
        yield conn

    class Repo:
        def __init__(self, c, *, player_sync_id=None):
            assert c.read_only
            assert c.isolation_level == data.psycopg.IsolationLevel.REPEATABLE_READ
            self.sync_id = player_sync_id

        def default_user_id(self):
            return "local-user"

        def league_id(self, provider, *, user_id):
            assert (provider, user_id) == (123, "local-user")
            return "scoped-league"

        def demo_snapshot(self, league, season, as_of):
            assert (league, season) == ("scoped-league", 2026)
            return {"week": 11, "synced_at": now, "sync_id": "fresh-empty-sync",
                    "pool_observed": True}

        def teams(self, *args):
            return [SimpleNamespace(provider_team_id=2, name="Test team")]

    monkeypatch.setenv("DEMO_LEAGUE_ID", "123")
    monkeypatch.setenv("DEMO_SEASON", "2026")
    monkeypatch.setattr(data, "connection", connection)
    monkeypatch.setattr(data, "Repository", Repo)
    def context(repo, league, user, season, week, now):
        assert repo.sync_id == "fresh-empty-sync"
        calls.append((league, user, season, week))
        return SimpleNamespace(team_id=2)

    monkeypatch.setattr(data.engine, "context", context)
    monkeypatch.setattr(data, "lineup_view", lambda ctx: {"rows": []})
    result = data.load_section("lineup")
    assert calls == [("scoped-league", "local-user", 2026, 11)]
    assert result["synced_at"] == now
    assert result["team"] == "Test team"
    assert "sync_id" not in result  # Internal database IDs never reach the browser.


def test_waivers_accept_empty_pinned_pool(monkeypatch):
    def run(ctx, *, allow_empty_pool=False):
        assert allow_empty_pool
        return SimpleNamespace(records=[{"decision": "no_action", "summary": "No move."}],
                               detail=[], players=[])

    monkeypatch.setattr(data.engine, "run_waivers", run)
    result = data.waivers_view(SimpleNamespace())
    assert result["picks"] == []
    assert result["no_action"] == "No move."


def test_qualifying_waiver_below_ten_suppressed_options_is_visible(monkeypatch):
    from espn_ff_assistant.lineup import LineupPlayer
    from espn_ff_assistant.waivers import PoolPlayer, WaiverOption

    players = [PoolPlayer(LineupPlayer(pid, f"Candidate {pid}", frozenset({0}), 10),
                          "QB", 100, 10, availability="free_agent") for pid in range(11)]
    options = [WaiverOption(pid, None, "QB", "stash", "free_agent", 0, 1, 30 - pid,
                            decision="notify" if pid == 10 else "suppressed") for pid in range(11)]
    monkeypatch.setattr(data.engine, "team_players", lambda *a: ([], {}))
    monkeypatch.setattr(data.engine, "pool_players", lambda *a: (players, {p.pid: p.lineup.name for p in players}))
    monkeypatch.setattr(data.engine, "waiver_context", lambda *a: SimpleNamespace(
        faab_remaining=0, comparables=[], streams_dst=False, stash_injured=False))
    monkeypatch.setattr(data.engine.waiver_engine, "evaluate", lambda *a: options)
    ctx = SimpleNamespace(team_id=2, period=11, as_of_ts=datetime.now(UTC))
    result = data.waivers_view(ctx)
    assert [p["payload"]["add"]["player_id"] for p in result["picks"]] == [10]
    assert "in Week 11" in result["picks"][0]["summary"]
    assert result["no_action"] is None
    assert len(result["other"]) == 9


def test_unknown_deadline_withholds_trades_without_running_search(monkeypatch):
    monkeypatch.setattr(data, "discover", lambda ctx: pytest.fail("Must not invent deadline"))
    result = data.trades_view(SimpleNamespace(settings={}))
    assert not result["ideas"]
    assert "unavailable" in result["notice"]


def test_passed_deadline_withholds_trades_without_running_search(monkeypatch):
    monkeypatch.setattr(data, "discover", lambda ctx: pytest.fail("Must respect exact deadline"))
    result = data.trades_view(SimpleNamespace(
        settings={"tradeSettings": {"deadlineDate": 1000}}, as_of_ts=datetime.now(UTC)))
    assert not result["ideas"]
    assert "passed" in result["notice"]
