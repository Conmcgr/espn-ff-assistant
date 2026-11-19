"""Deterministic opportunity candidates and terminal, non-resurrecting diffs.

Drops last seven days after processing (or waiver clearance, if later).
Undated drops are ignored rather than made fresh at scan time. OUT and
INJURY_RESERVE qualify; starter opportunities end at the scheduled kickoff.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class Opportunity:
    kind: str
    change_key: str
    provider_player_id: int
    provider_team_id: int | None
    source_view: str
    expires_at: datetime
    attrs: dict[str, Any]


def candidates(statuses, roster, drops, games, period, observed_at) -> dict[str, Opportunity]:
    result = {}
    for txid, pid, team, processed in drops:
        status = statuses.get(pid)
        if (not status or status.availability not in {'free_agent', 'waivers'}
                or processed is None or processed > observed_at):
            continue
        expiry = max(processed, status.waiver_clear_at or processed) + timedelta(days=7)
        key = f'player_dropped:{txid}:{pid}'
        result[key] = Opportunity('player_dropped', key, pid, team, 'mTransactions2', expiry, {
            'availability': status.availability,
            'waiver_clear_at': status.waiver_clear_at.isoformat() if status.waiver_clear_at else None,
        })
    for entry in roster:
        status = statuses.get(entry.provider_player_id)
        game = games.get(status.pro_team_id) if status else None
        if (entry.lineup_slot_id is None or entry.lineup_slot_id in (20, 21)
                or not status or status.injury_status not in ('OUT', 'INJURY_RESERVE')
                or not game or game.kickoff_at is None or game.is_bye):
            continue
        key = f'starter_out:{entry.provider_team_id}:{entry.provider_player_id}:{period}'
        result[key] = Opportunity('starter_out', key, entry.provider_player_id,
                                  entry.provider_team_id, 'mRoster', game.kickoff_at, {
                                      'slot': entry.lineup_slot_id, 'injury_status': status.injury_status,
                                  })
    return result


def diff(previous, current, observed_at):
    """Return (candidate, change) pairs; expired keys never become fresh again."""
    changes = []
    for key in sorted(previous.keys() | current.keys()):
        old = previous.get(key)
        new = current.get(key)
        if old and old[1]:
            continue
        if old and (new is None or new.expires_at <= observed_at):
            changes.append((old[0], 'expired'))
        elif new and new.expires_at > observed_at:
            change = 'new' if old is None else (
                'unchanged' if old[0] == new else 'changed')
            changes.append((new, change))
    return changes
