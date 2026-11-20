"""Trigger policy: window grouping, settle delay, batching, urgent bypass."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from espn_ff_assistant.game_status import GameState, GameStatus, normalize_game_status
from espn_ff_assistant.repository import Opportunity, StoredOpportunity
from espn_ff_assistant.triggers import (
    TriggerConfig,
    due_triggers,
    game_window,
    window_states,
)

NOW = datetime(2026, 11, 23, 12, 10, tzinfo=UTC)  # Monday 07:10 ET


def game(game_id, kickoff, state=GameState.FINAL, observed=NOW - timedelta(hours=12)):
    return GameStatus(game_id, "espn_scoreboard", state, observed, kickoff, None)


def opp(kind, key, expires, attrs=None, expired_at=None):
    return StoredOpportunity(
        Opportunity(kind, key, 777001, 4, "mTransactions2", expires, attrs or {}),
        expired_at,
    )


class TestWindows:
    def test_weekday_grouping(self):
        assert game_window(datetime(2026, 11, 20, 1, 15, tzinfo=UTC)) == "2026-11-19:thursday"
        assert game_window(datetime(2026, 11, 24, 1, 15, tzinfo=UTC)) == "2026-11-23:monday"
        assert game_window(datetime(2026, 11, 21, 22, 0, tzinfo=UTC)) == "2026-11-21:saturday"

    def test_sunday_splits(self):
        assert game_window(datetime(2026, 11, 22, 18, 0, tzinfo=UTC)) == "2026-11-22:sunday_early"
        assert game_window(datetime(2026, 11, 22, 21, 25, tzinfo=UTC)) == "2026-11-22:sunday_late"
        assert game_window(datetime(2026, 11, 23, 1, 20, tzinfo=UTC)) == "2026-11-22:sunday_night"

    def test_dst_spring_forward(self):
        # 2026-03-08 02:00 EST -> EST jumps to EDT; a 18:00Z game is 13:00 EDT.
        assert game_window(datetime(2026, 3, 8, 18, 0, tzinfo=UTC)) == "2026-03-08:sunday_early"

    def test_dst_fall_back(self):
        # 2026-11-01: clocks fall back; 17:30Z is 12:30 EST (still early).
        assert game_window(datetime(2026, 11, 1, 17, 30, tzinfo=UTC)) == "2026-11-01:sunday_early"


class TestWindowStates:
    def test_mixed(self):
        games = [
            game("a", datetime(2026, 11, 22, 18, 0, tzinfo=UTC)),
            game("b", datetime(2026, 11, 22, 18, 0, tzinfo=UTC), GameState.IN_PROGRESS),
            game("c", datetime(2026, 11, 22, 21, 25, tzinfo=UTC), GameState.POSTPONED),
        ]
        states = window_states(games, NOW)
        assert states["2026-11-22:sunday_early"].state == "waiting"
        assert states["2026-11-22:sunday_late"].state == "waiting"
        assert "postponed" in states["2026-11-22:sunday_late"].detail

    def test_blocked_until_delay(self):
        kickoff = datetime(2026, 11, 22, 18, 0, tzinfo=UTC)
        observed = NOW - timedelta(minutes=30)
        states = window_states([game("a", kickoff, GameState.FINAL, observed)], NOW)
        assert states["2026-11-22:sunday_early"].state == "blocked"

    def test_missing_kickoff_ignored(self):
        states = window_states([game("a", None)], NOW)
        assert states == {}


class TestDueTriggers:
    def test_settle_delay_boundary(self):
        kickoff = datetime(2026, 11, 22, 18, 0, tzinfo=UTC)
        window = "2026-11-22:sunday_early"
        games = [game("a", kickoff, GameState.FINAL, NOW)]
        # First verified-final observation stamps a marker, no scan yet.
        first = {t.kind for t in due_triggers(NOW, games, {})}
        assert "window_final" in first and "game_settled" not in first
        # The delay runs from the marker, not the latest retrieval.
        fired = {f"window_final:{window}": NOW - timedelta(minutes=44)}
        assert "game_settled" not in {t.kind for t in due_triggers(NOW, games, {}, fired)}
        fired = {f"window_final:{window}": NOW - timedelta(minutes=46)}
        kinds = {t.kind for t in due_triggers(NOW, games, {}, fired)}
        assert "game_settled" in kinds
        # Re-observing final games later does not restart the delay.
        games = [game("a", kickoff, GameState.FINAL, NOW)]
        assert "game_settled" in {
            t.kind for t in due_triggers(NOW, games, {}, fired)
        }

    def test_outcome_scoring_after_all_final(self):
        kickoff = datetime(2026, 11, 23, 1, 15, tzinfo=UTC)
        games = [game("a", kickoff, GameState.FINAL, NOW)]
        week = "2026-11-22"  # local date of the week's last game
        first = {t.kind for t in due_triggers(NOW, games, {})}
        assert "all_final" in first and "outcome_scoring" not in first
        fired = {f"all_final:{week}": NOW - timedelta(hours=13)}
        keys = {t.key for t in due_triggers(NOW, games, {}, fired)}
        assert f"outcome_scoring:{week}" in keys
        games.append(game("b", kickoff, GameState.IN_PROGRESS, NOW))
        assert "outcome_scoring" not in {t.kind for t in due_triggers(NOW, games, {}, fired)}
        # A different scoring period gets a different key.
        other = {t.key for t in due_triggers(
            NOW, games[:1], {}, {"all_final:9": NOW - timedelta(hours=13)}, period=9
        )}
        assert "outcome_scoring:9" in other

    def test_urgent_pre_lock_bypasses_batching(self):
        soon = NOW + timedelta(minutes=30)
        opps = [opp("starter_out", "starter_out:7:1:11", soon, {"injury_status": "OUT"})]
        kinds = {t.kind for t in due_triggers(NOW, [], opps)}
        assert "urgent_pre_lock" in kinds
        # Already locked/expired starters never fire.
        opps.append(opp("starter_out", "starter_out:7:2:11", NOW - timedelta(minutes=5)))
        keys = {t.key for t in due_triggers(NOW, [], opps)}
        assert "urgent_pre_lock:starter_out:7:2:11" not in keys

    def test_waiver_ticks(self):
        clear = NOW + timedelta(minutes=20)
        opps = [opp("player_dropped", "player_dropped:tx:1", NOW + timedelta(days=7),
                    {"waiver_clear_at": clear.isoformat()})]
        kinds = {t.kind for t in due_triggers(NOW, [], opps)}
        assert "pre_waiver" in kinds and "post_waiver" not in kinds
        after = NOW + timedelta(hours=1)
        assert "post_waiver" in {t.kind for t in due_triggers(after, [], opps)}

    def test_daily_drop_requires_pending(self):
        config = TriggerConfig(daily_drop_hour=6)  # now is 07:00 ET
        assert not due_triggers(NOW, [], [], config=config)
        opps = [opp("player_dropped", "player_dropped:tx:1", NOW + timedelta(days=7))]
        assert "daily_drop" in {t.kind for t in due_triggers(NOW, [], opps, config=config)}
        # Before the league-local hour, nothing fires.
        early = NOW - timedelta(hours=6)  # 01:00 ET
        assert "daily_drop" not in {t.kind for t in due_triggers(early, [], opps, config=config)}

    def test_fired_keys_suppress(self):
        kickoff = datetime(2026, 11, 22, 18, 0, tzinfo=UTC)
        games = [game("a", kickoff, GameState.FINAL, NOW - timedelta(hours=1))]
        fired = {
            "window_final:2026-11-22:sunday_early": NOW - timedelta(hours=1),
            "game_settled:2026-11-22:sunday_early": NOW - timedelta(minutes=30),
        }
        assert "game_settled" not in {t.kind for t in due_triggers(NOW, games, [], fired)}

    def test_deterministic_replay(self):
        kickoff = datetime(2026, 11, 22, 18, 0, tzinfo=UTC)
        games = [game("a", kickoff, GameState.FINAL, NOW - timedelta(hours=1))]
        opps = [opp("starter_out", "starter_out:7:1:11", NOW + timedelta(minutes=20))]
        first = due_triggers(NOW, games, opps)
        second = due_triggers(NOW, games, opps, fired={t.key: NOW for t in first})
        assert second == []

    def test_naive_now_rejected(self):
        with pytest.raises(ValueError):
            due_triggers(datetime(2026, 11, 23, 12, 0), [], [])


class TestFixtureScenario:
    def test_scenario_fixture(self):
        data = json.loads(
            (Path(__file__).parent / "fixtures/trigger_scenario.json").read_text()
        )
        batch = normalize_game_status(
            data["scoreboard"], observed_at=NOW - timedelta(hours=1)
        )
        opps = [
            StoredOpportunity(
                Opportunity(
                    o["kind"], o["change_key"], o["provider_player_id"],
                    o.get("provider_team_id"), o.get("source_view", "fixture"),
                    datetime.fromisoformat(o["expires_at"]), o.get("attrs", {}),
                ),
                None,
            )
            for o in data["opportunities"]
        ]
        states = window_states(list(batch.games.values()), NOW)
        assert states["2026-11-19:thursday"].state == "settled"
        assert states["2026-11-22:sunday_early"].state == "waiting"
        keys = {t.key for t in due_triggers(
            NOW, list(batch.games.values()), opps,
            fired={k: datetime.fromisoformat(v) for k, v in data["fired"].items()},
        )}
        assert "game_settled:2026-11-19:thursday" not in keys  # already fired
        assert "urgent_pre_lock:starter_out:7:777002:11" in keys
        assert "urgent_pre_lock:starter_out:7:777003:11" not in keys  # locked
        assert any(k.startswith("pre_waiver:") for k in keys)
        assert "outcome_scoring" not in keys
