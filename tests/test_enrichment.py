from datetime import UTC, datetime, timedelta

import pytest

from espn_ff_assistant.enrichment import (
    SourceBatch,
    crosswalk,
    depth_chart_signals,
    enrich,
    injury_signals,
    news_signals,
    usage_signals,
)
from espn_ff_assistant.repository import Opportunity

NOW = datetime(2026, 11, 20, 18, tzinfo=UTC)
EARLIER = NOW - timedelta(hours=6)
PLAYERS = [
    {"espn_id": "101", "gsis_id": "00-0000101", "pfr_id": "SyntA00"},
    {"espn_id": "202.0", "gsis_id": "00-0000202", "pfr_id": "SyntB00"},
    {"espn_id": "999", "gsis_id": "00-0000999", "pfr_id": "SyntZ00"},
    {"espn_id": "", "gsis_id": "00-0000303", "pfr_id": "SyntC00"},
]
DEPTH = [
    {"dt": "2026-11-19T12:00:00Z", "team": "AAA", "player_name": "Synthetic A",
     "espn_id": "101", "pos_grp": "Offense", "pos_abb": "RB", "pos_rank": "2"},
    {"dt": "2026-11-19T12:00:00Z", "team": "AAA", "player_name": "Synthetic A",
     "espn_id": "101", "pos_grp": "Special Teams", "pos_abb": "KR", "pos_rank": "1"},
    {"dt": "2026-11-12T12:00:00Z", "team": "AAA", "player_name": "Synthetic A",
     "espn_id": "101", "pos_grp": "Offense", "pos_abb": "RB", "pos_rank": "1"},
    {"dt": "2026-11-21T12:00:00Z", "team": "AAA", "player_name": "Synthetic A",
     "espn_id": "101", "pos_grp": "Offense", "pos_abb": "RB", "pos_rank": "3"},
    {"dt": "2026-11-01T12:00:00Z", "team": "BBB", "player_name": "Synthetic B",
     "espn_id": "202", "pos_grp": "Offense", "pos_abb": "WR", "pos_rank": "1"},
]
INJURIES = [
    {"season": "2026", "season_type": "REG", "week": "10", "gsis_id": "00-0000101",
     "report_status": "Questionable", "report_primary_injury": "Ankle",
     "practice_status": "Limited", "practice_primary_injury": "Ankle"},
    {"season": "2026", "season_type": "REG", "week": "11", "gsis_id": "00-0000101",
     "report_status": "Out", "report_primary_injury": "Ankle",
     "practice_status": "Did Not Participate In Practice", "practice_primary_injury": "Ankle"},
    {"season": "2026", "season_type": "REG", "week": "8", "gsis_id": "00-0000202",
     "report_status": "", "report_primary_injury": "",
     "practice_status": "Full", "practice_primary_injury": "Hamstring"},
    {"season": "2026", "season_type": "REG", "week": "12", "gsis_id": "00-0000202",
     "report_status": "Out", "report_primary_injury": "Knee",
     "practice_status": "", "practice_primary_injury": ""},
]
SNAPS = [
    {"season": "2026", "game_type": "REG", "week": str(week), "pfr_player_id": "SyntA00",
     "offense_pct": str(pct)}
    for week, pct in ((6, 0.4), (7, 0.5), (8, 0.55), (10, 0.7), (11, 0.9))
] + [{"season": "2026", "game_type": "REG", "week": "6", "pfr_player_id": "SyntB00",
      "offense_pct": "0.8"}]
FEED = [
    {"id": 1, "type": "Rotowire", "playerId": 101, "headline": "Synthetic A ruled out",
     "published": "2026-11-20T15:00:00Z", "links": {"web": {"href": "https://example.test/1"}}},
    {"id": 2, "type": "Rotowire", "playerId": 202, "headline": "Wrong player",
     "published": "2026-11-20T15:00:00Z", "links": {}},
    {"id": 3, "type": "Story", "headline": "Untagged story",
     "published": "2026-11-20T15:00:00Z", "links": {}},
    {"id": 4, "type": "Rotowire", "playerId": 101, "headline": "From the future",
     "published": "2026-11-20T19:00:00Z", "links": {}},
    {"id": 5, "type": "Rotowire", "playerId": 101, "headline": "Too old",
     "published": "2026-11-10T15:00:00Z", "links": {}},
    {"id": 6, "type": "Story", "headline": "Athlete-tagged",
     "categories": [{"type": "athlete", "athleteId": 101}],
     "published": "2026-11-19T15:00:00Z", "links": {"mobile": {"href": "https://example.test/6"}}},
]


def _batch(rows, modified=EARLIER):
    return SourceBatch("present", rows, NOW, modified, "https://example.test")


def test_crosswalk_matches_only_wanted_espn_ids():
    ids = crosswalk(PLAYERS, {101, 202, 303})
    assert ids == {"gsis": {"00-0000101": 101, "00-0000202": 202},
                   "pfr": {"SyntA00": 101, "SyntB00": 202}}


def test_depth_chart_takes_latest_snapshot_before_observation():
    signals = depth_chart_signals(DEPTH, {101, 202}, NOW)
    a, b = signals[101], signals[202]
    assert a.as_of == datetime(2026, 11, 19, 12, tzinfo=UTC) and a.freshness == "fresh"
    assert a.attrs["best_rank"] == 1
    assert [(s["position"], s["rank"]) for s in a.attrs["slots"]] == [("KR", 1), ("RB", 2)]
    assert b.freshness == "stale"


def test_injury_uses_latest_week_at_or_before_period():
    ids = crosswalk(PLAYERS, {101, 202})["gsis"]
    signals = injury_signals(_batch(INJURIES), ids, 2026, 11, NOW)
    assert signals[101].freshness == "fresh"
    assert signals[101].attrs["report_status"] == "Out"
    assert signals[202].freshness == "stale" and signals[202].attrs["week"] == 8
    assert signals[202].attrs["report_status"] is None


def test_usage_uses_completed_weeks_before_period():
    ids = crosswalk(PLAYERS, {101, 202})["pfr"]
    signals = usage_signals(_batch(SNAPS), ids, 2026, 11, NOW)
    assert signals[101].attrs == {"weeks": [7, 8, 10], "offense_pct": [0.5, 0.55, 0.7],
                                  "avg_offense_pct": 0.583}
    assert signals[101].freshness == "fresh" and signals[202].freshness == "stale"


@pytest.mark.parametrize("modified", [None, NOW + timedelta(minutes=1)])
def test_undated_or_future_datasets_are_not_used(modified):
    ids = crosswalk(PLAYERS, {101})
    assert injury_signals(_batch(INJURIES, modified), ids["gsis"], 2026, 11, NOW) == {}
    assert usage_signals(_batch(SNAPS, modified), ids["pfr"], 2026, 11, NOW) == {}


def test_news_requires_player_id_match_and_window():
    signals = news_signals(_batch(FEED), 101, NOW)
    assert [(s.source_ref, s.citation_url) for s in signals] == [
        ("1", "https://example.test/1"), ("6", "https://example.test/6"),
    ]


class FakeSources:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def _batch(self, rows):
        return SourceBatch("failed", error="ConnectionError") if self.fail else _batch(rows)

    def players(self, wanted):
        self.calls.append("players")
        return self._batch(PLAYERS)

    def depth_charts(self, season, wanted):
        self.calls.append("depth")
        return self._batch(DEPTH)

    def injuries(self, season):
        self.calls.append("injuries")
        return self._batch(INJURIES)

    def snap_counts(self, season):
        self.calls.append("snaps")
        return self._batch(SNAPS)

    def news(self, pid):
        self.calls.append(f"news:{pid}")
        return self._batch(FEED)


def _opportunity(pid, hours):
    return Opportunity("player_dropped", f"drop:{pid}", pid, None, "mTransactions2",
                       NOW + timedelta(hours=hours), {})


LIVE = [("a", _opportunity(101, 5)), ("b", _opportunity(202, 1)), ("c", _opportunity(404, 9))]


def test_enrich_fetches_each_dataset_once_and_news_for_shortlist():
    sources = FakeSources()
    result = enrich(LIVE, sources, 2026, 11, NOW, news_limit=2)
    assert sources.calls == ["players", "depth", "injuries", "snaps", "news:202", "news:101"]
    assert [s.kind for s in result.signals["a"]] == [
        "depth_chart", "injury", "usage", "news", "news"]
    assert [(s.kind, s.source_ref) for s in result.signals["b"]] == [
        ("depth_chart", "BBB:2026-11-01T12:00:00+00:00"),
        ("injury", "2026:8"), ("usage", "2026:6"), ("news", "2"),
    ]
    assert result.signals["c"] == []
    assert result.sources["nflverse_injuries"]["matched"] == 2
    assert result.sources["nflverse_injuries"]["unmatched"] == 1
    assert result.sources["espn_fantasy_news"] == {
        "requested": 2, "states": {"present": 2}, "with_items": 2}


def test_failed_sources_fall_back_to_no_signals():
    result = enrich(LIVE, FakeSources(fail=True), 2026, 11, NOW)
    assert result.signals == {"a": [], "b": [], "c": []}
    assert {s["state"] for n, s in result.sources.items() if n != "espn_fantasy_news"} == {
        "failed"}
    assert result.sources["espn_fantasy_news"]["states"] == {"failed": 3}


def test_enrich_without_opportunities_fetches_nothing():
    sources = FakeSources()
    assert enrich([], sources, 2026, 11, NOW).signals == {}
    assert sources.calls == []
    with pytest.raises(ValueError):
        enrich(LIVE, sources, 2026, 11, NOW.replace(tzinfo=None))
