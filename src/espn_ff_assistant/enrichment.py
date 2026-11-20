"""Point-in-time external signals for ledger opportunities.

Enrichment is optional context: a failed, missing or future-dated source yields
no signals and a recorded source state, never a changed or blocked opportunity.
Players are matched by ESPN ID only (directly, or through the nflverse player
crosswalk); names are never used to match.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from .repository import Opportunity, Signal

DEPTH_CHART_MAX_AGE = timedelta(days=8)
NEWS_LOOKBACK = timedelta(days=7)
NEWS_PER_PLAYER = 3
USAGE_GAMES = 3
HEADLINE_LIMIT = 280


@dataclass(frozen=True)
class SourceBatch:
    # present, empty, failed, or disabled
    state: str
    rows: Sequence[Mapping[str, Any]] = ()
    retrieved_at: datetime | None = None
    last_modified: datetime | None = None
    url: str | None = None
    error: str | None = None


@dataclass
class EnrichmentResult:
    signals: dict[str, list[Signal]] = field(default_factory=dict)
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)


class Sources(Protocol):
    def players(self, wanted: set[int]) -> SourceBatch: ...
    def depth_charts(self, season: int, wanted: set[int]) -> SourceBatch: ...
    def injuries(self, season: int) -> SourceBatch: ...
    def snap_counts(self, season: int) -> SourceBatch: ...
    def news(self, provider_player_id: int) -> SourceBatch: ...


def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else None


def crosswalk(rows: Iterable[Mapping[str, Any]], wanted: set[int]) -> dict[str, dict[str, int]]:
    """Map nflverse gsis and pfr IDs to wanted ESPN IDs."""
    result: dict[str, dict[str, int]] = {"gsis": {}, "pfr": {}}
    for row in rows:
        espn = _int(row.get("espn_id"))
        if espn not in wanted:
            continue
        for key, column in (("gsis", "gsis_id"), ("pfr", "pfr_id")):
            if row.get(column):
                result[key][row[column]] = espn
    return result


def depth_chart_signals(
    rows: Iterable[Mapping[str, Any]], wanted: set[int], observed_at: datetime,
    max_age: timedelta = DEPTH_CHART_MAX_AGE,
) -> dict[int, Signal]:
    """Latest depth-chart snapshot per player at or before observed_at."""
    latest: dict[int, tuple[datetime, list[Mapping[str, Any]]]] = {}
    for row in rows:
        pid, stamp = _int(row.get("espn_id")), _timestamp(row.get("dt"))
        if pid not in wanted or stamp is None or stamp > observed_at:
            continue
        current = latest.get(pid)
        if current is None or stamp > current[0]:
            latest[pid] = (stamp, [row])
        elif stamp == current[0]:
            current[1].append(row)
    signals = {}
    for pid, (stamp, entries) in latest.items():
        slots = sorted(
            {(_int(r.get("pos_rank")) or 99, r.get("pos_abb") or "", r.get("pos_grp") or "")
             for r in entries}
        )
        team = entries[0].get("team")
        signals[pid] = Signal(
            "depth_chart", "nflverse_depth_charts", pid, f"{team}:{stamp.isoformat()}", stamp,
            "fresh" if observed_at - stamp <= max_age else "stale",
            {"team": team, "player_name": entries[0].get("player_name"),
             "best_rank": slots[0][0],
             "slots": [{"rank": r, "position": p, "group": g} for r, p, g in slots]},
        )
    return signals


def _source_after(batch: SourceBatch, observed_at: datetime) -> bool:
    return batch.last_modified is None or batch.last_modified > observed_at


def injury_signals(
    batch: SourceBatch, gsis: Mapping[str, int], season: int, period: int, observed_at: datetime,
) -> dict[int, Signal]:
    """Latest weekly injury report per player; only the current week is fresh."""
    if _source_after(batch, observed_at):
        return {}
    latest: dict[int, tuple[int, Mapping[str, Any]]] = {}
    for row in batch.rows:
        pid, week = gsis.get(row.get("gsis_id", "")), _int(row.get("week"))
        if (pid is None or week is None or week > period
                or _int(row.get("season")) != season or row.get("season_type") != "REG"):
            continue
        if pid not in latest or week > latest[pid][0]:
            latest[pid] = (week, row)
    return {
        pid: Signal(
            "injury", "nflverse_injuries", pid, f"{season}:{week}", batch.last_modified,
            "fresh" if week == period else "stale",
            {"week": week,
             **{key: row.get(key) or None for key in (
                 "report_status", "report_primary_injury",
                 "practice_status", "practice_primary_injury")}},
        )
        for pid, (week, row) in latest.items()
    }


def usage_signals(
    batch: SourceBatch, pfr: Mapping[str, int], season: int, period: int, observed_at: datetime,
    games: int = USAGE_GAMES,
) -> dict[int, Signal]:
    """Offensive snap share over each player's last completed games before period."""
    if _source_after(batch, observed_at):
        return {}
    by_player: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for row in batch.rows:
        pid, week = pfr.get(row.get("pfr_player_id", "")), _int(row.get("week"))
        pct = _float(row.get("offense_pct"))
        if (pid is None or week is None or pct is None or week >= period
                or _int(row.get("season")) != season or row.get("game_type") != "REG"):
            continue
        by_player[pid].append((week, pct))
    signals = {}
    for pid, rows in by_player.items():
        recent = sorted(rows)[-games:]
        last_week = recent[-1][0]
        signals[pid] = Signal(
            "usage", "nflverse_snap_counts", pid, f"{season}:{last_week}", batch.last_modified,
            "fresh" if last_week >= period - 2 else "stale",
            {"weeks": [w for w, _ in recent],
             "offense_pct": [round(p, 3) for _, p in recent],
             "avg_offense_pct": round(sum(p for _, p in recent) / len(recent), 3)},
        )
    return signals


def _matches(item: Mapping[str, Any], pid: int) -> bool:
    if _int(item.get("playerId")) == pid:
        return True
    return any(
        isinstance(c, dict) and c.get("type") == "athlete" and _int(c.get("athleteId")) == pid
        for c in item.get("categories") or ()
    )


def news_signals(
    batch: SourceBatch, pid: int, observed_at: datetime, lookback: timedelta = NEWS_LOOKBACK,
    limit: int = NEWS_PER_PLAYER,
) -> list[Signal]:
    """Most recent cited player-tagged news in the lookback window before observed_at."""
    signals = []
    for item in batch.rows:
        published = _timestamp(item.get("published"))
        if (published is None or not observed_at - lookback <= published <= observed_at
                or not _matches(item, pid) or item.get("id") is None):
            continue
        links = item.get("links") or {}
        url = next((links[k]["href"] for k in ("web", "mobile")
                    if isinstance(links.get(k), dict) and links[k].get("href")), None)
        headline = str(item.get("headline") or "")[:HEADLINE_LIMIT]
        signals.append(Signal(
            "news", "espn_fantasy_news", pid, str(item["id"]), published, "fresh",
            {"headline": headline, "type": item.get("type")}, url,
        ))
    return sorted(signals, key=lambda s: s.as_of, reverse=True)[:limit]


def _state(batch: SourceBatch, wanted: set[int], matched: Iterable[int], observed_at: datetime,
           *, point_in_time_rows: bool = False) -> dict[str, Any]:
    state = batch.state
    if state == "present" and not point_in_time_rows:
        if batch.last_modified is None:
            state = "undated"
        elif batch.last_modified > observed_at:
            state = "after_observation"
    hits = set(matched) & wanted
    return {
        "state": state,
        "retrieved_at": batch.retrieved_at.isoformat() if batch.retrieved_at else None,
        "last_modified": batch.last_modified.isoformat() if batch.last_modified else None,
        "url": batch.url, "error": batch.error,
        "matched": len(hits), "unmatched": len(wanted - hits),
    }


def enrich(
    opportunities: Sequence[tuple[str, Opportunity]], sources: Sources, season: int,
    period: int, observed_at: datetime, *, news_limit: int = 5,
) -> EnrichmentResult:
    """Attach signals to active opportunities; one fetch per dataset, news for a shortlist."""
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    result = EnrichmentResult()
    wanted = {o.provider_player_id for _, o in opportunities}
    if not wanted:
        return result
    players = sources.players(wanted)
    ids = crosswalk(players.rows, wanted)
    result.sources["nflverse_players"] = _state(
        players, wanted, set(ids["gsis"].values()) | set(ids["pfr"].values()), observed_at,
        point_in_time_rows=True,
    )
    depth_batch = sources.depth_charts(season, wanted)
    depth = depth_chart_signals(depth_batch.rows, wanted, observed_at)
    injury_batch, usage_batch = sources.injuries(season), sources.snap_counts(season)
    injury = injury_signals(injury_batch, ids["gsis"], season, period, observed_at)
    usage = usage_signals(usage_batch, ids["pfr"], season, period, observed_at)
    result.sources["nflverse_depth_charts"] = _state(
        depth_batch, wanted, depth, observed_at, point_in_time_rows=True)
    result.sources["nflverse_injuries"] = _state(injury_batch, wanted, injury, observed_at)
    result.sources["nflverse_snap_counts"] = _state(usage_batch, wanted, usage, observed_at)

    shortlist = []
    for _, o in sorted(opportunities, key=lambda item: item[1].expires_at):
        if o.provider_player_id not in shortlist and len(shortlist) < news_limit:
            shortlist.append(o.provider_player_id)
    news: dict[int, list[Signal]] = {}
    news_states = defaultdict(int)
    for pid in shortlist:
        batch = sources.news(pid)
        news_states[batch.state] += 1
        news[pid] = news_signals(batch, pid, observed_at)
    result.sources["espn_fantasy_news"] = {
        "requested": len(shortlist), "states": dict(news_states),
        "with_items": sum(1 for items in news.values() if items),
    }

    for opportunity_id, o in opportunities:
        pid = o.provider_player_id
        result.signals[opportunity_id] = [
            s for s in (depth.get(pid), injury.get(pid), usage.get(pid)) if s
        ] + news.get(pid, [])
    return result
