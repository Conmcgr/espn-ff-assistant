"""Scheduled tick planning, shadow-mode guard and health assessment."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from espn_ff_assistant.config import ConfigurationError, Settings
from espn_ff_assistant.game_status import normalize_game_status
from espn_ff_assistant.repository import Opportunity, StoredOpportunity, WorkerTick
from espn_ff_assistant.tick import (
    MARKER_KINDS,
    HealthPolicy,
    TickConfig,
    assess_health,
    plan_tick,
)
from espn_ff_assistant.triggers import due_triggers

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
SETTINGS = Settings(league_id=1, espn_s2="x", swid="y", first_season=2013, last_season=2026)
CONFIG = TickConfig("e5f5e3ae-dfa4-4b8a-a18f-a72d249d4585", 4, 2026)
ENV = {"WORKER_USER_ID": CONFIG.user_id, "WORKER_TEAM_ID": "4"}


def _scenario():
    raw = json.loads((Path(__file__).parent / "fixtures/trigger_scenario.json").read_text())
    now = datetime(2026, 11, 23, 12, 10, tzinfo=UTC)
    batch = normalize_game_status(raw["scoreboard"], observed_at=now - timedelta(hours=1))
    opps = [StoredOpportunity(Opportunity(
        o["kind"], o["change_key"], o["provider_player_id"], o.get("provider_team_id"),
        o.get("source_view", "fixture"), datetime.fromisoformat(o["expires_at"]),
        o.get("attrs", {})), None) for o in raw["opportunities"]]
    return now, list(batch.games.values()), opps


def test_plan_splits_due_triggers_into_markers_and_scans():
    now, games, opps = _scenario()
    plan = plan_tick(now, games, opps, {}, now, CONFIG)
    due = due_triggers(now, games, opps, {}, CONFIG.triggers)
    assert {t.key for t in plan.markers + plan.scans + plan.deferred} == {t.key for t in due}
    assert plan.scans and all(t.kind not in MARKER_KINDS for t in plan.scans)
    assert all(t.kind in MARKER_KINDS for t in plan.markers)
    assert plan.scan_due and not plan.refresh
    assert plan.reasons == [t.key for t in plan.scans]


def test_fired_triggers_hold_and_refresh_after_six_hours():
    now, games, opps = _scenario()
    fired = {t.key: now for t in due_triggers(now, games, opps, {}, CONFIG.triggers)}
    assert not plan_tick(now, games, opps, fired, now - timedelta(hours=5), CONFIG).scan_due
    stale = plan_tick(now, games, opps, fired, now - timedelta(hours=6), CONFIG)
    assert stale.scan_due and stale.reasons == ["refresh"]
    assert plan_tick(now, games, opps, fired, None, CONFIG).refresh


def test_config_from_env_defaults_to_configured_season():
    assert TickConfig.from_env(SETTINGS, ENV) == CONFIG


@pytest.mark.parametrize("flag", ["DELIVERY_ENABLED", "SMS_ENABLED", "ESPN_ACTIONS_ENABLED"])
def test_outbound_flags_must_stay_off(flag):
    assert TickConfig.from_env(SETTINGS, ENV | {flag: "false"}) == CONFIG
    with pytest.raises(ConfigurationError, match=flag):
        TickConfig.from_env(SETTINGS, ENV | {flag: "true"})


@pytest.mark.parametrize("env", [{}, {"WORKER_USER_ID": "not-a-uuid", "WORKER_TEAM_ID": "4"},
                                 ENV | {"WORKER_TEAM_ID": "0"}])
def test_invalid_scope_is_a_configuration_error(env):
    with pytest.raises(ConfigurationError):
        TickConfig.from_env(SETTINGS, env)


def _tick(minutes_ago, ok=True):
    started = NOW - timedelta(minutes=minutes_ago)
    return WorkerTick(started, started, "HOLD" if ok else "SCAN_FAILED", ok)


@pytest.mark.parametrize(("ticks", "last_scan", "expected"), [
    ([_tick(5), _tick(15)], NOW - timedelta(hours=1), []),
    ([], NOW - timedelta(hours=1), ["missed_runs"]),
    ([_tick(50)], NOW - timedelta(hours=1), ["missed_runs"]),
    ([_tick(5)], NOW - timedelta(hours=9), ["stale_sync"]),
    ([_tick(5)], None, ["stale_sync"]),
    ([_tick(5, False), _tick(15, False)], NOW, []),
    ([_tick(5, False), _tick(15, False), _tick(25, False)], NOW, ["repeated_failures"]),
    ([_tick(5, False), _tick(15), _tick(25, False)], NOW, []),
])
def test_health(ticks, last_scan, expected):
    assert assess_health(NOW, ticks, last_scan, HealthPolicy()) == expected
