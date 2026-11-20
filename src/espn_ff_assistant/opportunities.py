"""Deterministic opportunity candidates and terminal, non-resurrecting diffs.

Drops last seven days after processing (or waiver clearance, if later).
Undated drops are ignored rather than made fresh at scan time. OUT and
INJURY_RESERVE qualify; starter opportunities end at the scheduled kickoff.
"""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from .repository import ExecutedDrop, Opportunity, PlayerStatus, ProGame, Repository, RosterEntry


def candidates(
    statuses: Mapping[int, PlayerStatus],
    roster: Sequence[RosterEntry],
    drops: Sequence[ExecutedDrop],
    games: Mapping[int, ProGame],
    period: int,
    observed_at: datetime,
) -> dict[str, Opportunity]:
    result = {}
    for drop in drops:
        txid, pid = drop.provider_transaction_id, drop.provider_player_id
        team, processed = drop.provider_team_id, drop.process_date
        status = statuses.get(pid)
        if (
            not status
            or status.availability not in {"free_agent", "waivers"}
            or processed is None
            or processed > observed_at
        ):
            continue
        expiry = max(processed, status.waiver_clear_at or processed) + timedelta(days=7)
        key = f"player_dropped:{txid}:{pid}"
        result[key] = Opportunity(
            "player_dropped",
            key,
            pid,
            team,
            "mTransactions2",
            expiry,
            {
                "availability": status.availability,
                "waiver_clear_at": status.waiver_clear_at.isoformat()
                if status.waiver_clear_at
                else None,
            },
        )
    for entry in roster:
        status = statuses.get(entry.provider_player_id)
        game = games.get(status.pro_team_id) if status else None
        if (
            entry.lineup_slot_id is None
            or entry.lineup_slot_id in (20, 21)
            or not status
            or status.injury_status not in ("OUT", "INJURY_RESERVE")
            or not game
            or game.kickoff_at is None
            or game.is_bye
        ):
            continue
        key = f"starter_out:{entry.provider_team_id}:{entry.provider_player_id}:{period}"
        result[key] = Opportunity(
            "starter_out",
            key,
            entry.provider_player_id,
            entry.provider_team_id,
            "mRoster",
            game.kickoff_at,
            {
                "slot": entry.lineup_slot_id,
                "injury_status": status.injury_status,
            },
        )
    return result


def diff(
    previous: Mapping[str, tuple[Opportunity, datetime | None]],
    current: Mapping[str, Opportunity],
    observed_at: datetime,
) -> list[tuple[Opportunity, str]]:
    """Return (candidate, change) pairs; expired keys never become fresh again."""
    changes = []
    for key in sorted(previous.keys() | current.keys()):
        old = previous.get(key)
        new = current.get(key)
        if old and old[1]:
            continue
        if old and new and new.kind == "player_dropped":
            attrs = dict(new.attrs)
            if attrs.get("waiver_clear_at") is None:
                attrs["waiver_clear_at"] = old[0].attrs.get("waiver_clear_at")
            new = replace(new, expires_at=max(old[0].expires_at, new.expires_at), attrs=attrs)
        if old and (new is None or new.expires_at <= observed_at):
            changes.append((old[0], "expired"))
        elif new and new.expires_at > observed_at:
            change = "new" if old is None else ("unchanged" if old[0] == new else "changed")
            changes.append((new, change))
    return changes


def record_scan(
    repo: Repository,
    league: str,
    season: int,
    period: int,
    sync_id: str,
    observed_at: datetime,
) -> dict[str, Any]:
    repo.lock_opportunity_season(league, season)
    latest = repo.latest_opportunity_observation(league, season)
    seen = repo.opportunity_scan_processed(league, season, sync_id)
    counts = dict.fromkeys(("new", "changed", "unchanged", "expired"), 0)
    if seen or (latest is not None and observed_at <= latest):
        return {"counts": counts, "skipped_replay": True, "events": []}
    current = candidates(
        repo.player_status(league, season, period),
        repo.roster_at(league, season, period),
        repo.executed_drops(league, season, period),
        repo.pro_games(season, period),
        period,
        observed_at,
    )
    previous = {
        row.opportunity.change_key: (row.opportunity, row.expired_at)
        for row in repo.stored_opportunities(league, season)
    }
    events = []
    for opportunity, change in diff(previous, current, observed_at):
        opportunity_id = repo.upsert_opportunity(
            league,
            season,
            opportunity,
            sync_id,
            observed_at,
            observed_at if change == "expired" else None,
        )
        repo.insert_opportunity_event(opportunity_id, sync_id, observed_at, change)
        counts[change] += 1
        events.append(
            {"kind": opportunity.kind, "change_key": opportunity.change_key, "change": change}
        )
    repo.insert_opportunity_scan(league, season, sync_id, observed_at)
    return {"counts": counts, "skipped_replay": False, "events": events}
