"""Deterministic, transport-free notification policy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

Record = dict[str, Any]


def _window(settings: Mapping[str, Any]) -> tuple[time, time, ZoneInfo]:
    try:
        quiet = settings.get("quiet_hours", {})
        start = time.fromisoformat(quiet.get("start", "22:00"))
        end = time.fromisoformat(quiet.get("end", "09:00"))
        tz = ZoneInfo(quiet.get("timezone", "America/Chicago"))
        if start == end or start.tzinfo or end.tzinfo:
            raise ValueError
        return start, end, tz
    except (ValueError, TypeError, AttributeError, ZoneInfoNotFoundError):
        return time(22), time(9), ZoneInfo("America/Chicago")


def _quiet_window(settings: Mapping[str, Any], now: datetime) -> tuple[bool, datetime, ZoneInfo]:
    start, end, tz = _window(settings)
    local = now.astimezone(tz)
    clock = local.time().replace(tzinfo=None)
    quiet = (clock >= start or clock < end) if start > end else start <= clock < end
    end_day = local.date() + timedelta(days=clock >= end)
    digest = datetime.combine(end_day, end, tz).astimezone(UTC)
    if digest <= now:
        # Fall-back repeats the wall time; take its second occurrence.
        digest = datetime.combine(end_day, end.replace(fold=1), tz).astimezone(UTC)
    # Round-trip through UTC to normalize nonexistent spring-forward wall times.
    digest = digest.astimezone(tz).astimezone(UTC)
    return quiet, digest, tz


def _cap_count(history: Sequence[Record], now: datetime, tz: ZoneInfo) -> int:
    return sum(
        h["disposition"] == "send" and h["urgency"] != "urgent"
        and h["created_at"].astimezone(tz).date() == now.astimezone(tz).date()
        for h in history
    )


def _dedupe_key(rec: Record, user_id: str, league_season_id: str) -> str:
    payload = rec["payload"]
    if rec["kind"] == "lineup":
        action = sorted((m["player_id"], m["from"], m["to"]) for m in payload.get("moves", []))
    else:
        action = [(payload.get(k) or {}).get("player_id") for k in ("add", "drop")]
    return hashlib.sha256(json.dumps([
        user_id, league_season_id, rec["provider_team_id"], rec["kind"],
        rec["scoring_period"], action,
    ], sort_keys=True).encode()).hexdigest()


def _expiry(rec: Record, deadline: datetime | None) -> tuple[datetime, datetime]:
    observed = rec["as_of_ts"]
    if isinstance(observed, str):
        observed = datetime.fromisoformat(observed)
    if observed.tzinfo is None:
        raise ValueError("as_of_ts must be timezone-aware")
    expiry = deadline if deadline is not None and deadline.tzinfo is not None else observed + timedelta(minutes=15)
    return observed, expiry


def evaluate(
    records: Sequence[Record], history: Sequence[Record], settings: Mapping[str, Any], now: datetime,
    *, user_id: str, league_season_id: str, deadlines: Sequence[datetime | None] | None = None,
) -> list[Record]:
    """Return one decision per record, with independently supplied action deadlines."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if deadlines is not None and len(deadlines) != len(records):
        raise ValueError("one deadline is required per record")
    quiet, digest, tz = _quiet_window(settings, now)
    cap = settings.get("max_daily_notifications", 2)
    if type(cap) is not int or cap < 0:
        cap = 2
    used = _cap_count(history, now, tz)
    seen = list(history)
    decisions = []
    for index, rec in enumerate(records):
        key = _dedupe_key(rec, user_id, league_season_id)
        urgency = rec["decision"]
        prior = [h for h in seen if h["dedupe_key"] == key]
        escalated = bool(prior) and urgency == "urgent" and not any(h["urgency"] == "urgent" for h in prior)
        observed, expiry = _expiry(rec, deadlines[index] if deadlines is not None else None)
        disposition, reason, deliver = "send", "actionable", now
        if urgency not in {"notify", "urgent"}:
            disposition, reason = "suppress", "not_actionable"
        elif observed > now or now - observed > timedelta(minutes=15) or now >= expiry:
            disposition, reason = "suppress", "expired"
        elif prior and not escalated:
            disposition, reason = "suppress", "duplicate"
        elif urgency == "urgent":
            reason = "escalated" if escalated else "urgent_bypasses_quiet_hours" if quiet else "actionable"
        elif quiet or used >= cap:
            disposition, reason, deliver = "batch", "quiet_hours" if quiet else "daily_cap", digest
            if deliver >= expiry:
                disposition, reason = "suppress", "expired"
        if disposition == "send" and urgency != "urgent":
            used += 1
        row = dict(disposition=disposition, reason=reason, urgency=urgency, dedupe_key=key,
                   deliver_after=deliver, expires_at=expiry,
                   body=" ".join(rec["summary"].split())[:300])
        decisions.append(row)
        if disposition != "suppress":
            seen.append({**row, "status": "pending", "created_at": now})
    return decisions
