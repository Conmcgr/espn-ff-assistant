"""Deterministic, transport-free notification policy."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _window(settings):
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


def evaluate(records, history, settings, now, *, user_id, league_season_id):
    """Return one audit decision per record; all datetimes must be aware.

    Unknown deadlines expire after 15 minutes: silence is safer than a late text.
    History includes all periods for the local-day cap, scoped to user/season.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    start, end, tz = _window(settings)
    cap = settings.get("max_daily_notifications", 2)
    if type(cap) is not int or cap < 0:
        cap = 2
    local = now.astimezone(tz)
    clock = local.time().replace(tzinfo=None)
    quiet = (clock >= start or clock < end) if start > end else start <= clock < end
    end_day = local.date() + timedelta(days=clock >= end)
    digest = datetime.combine(end_day, end, tz).astimezone(UTC)
    # Round-trip through UTC to normalize nonexistent spring-forward wall times.
    digest = digest.astimezone(tz).astimezone(UTC)
    used = sum(
        h["disposition"] == "send" and h["urgency"] != "urgent"
        and h["status"] != "suppressed"
        and h["created_at"].astimezone(tz).date() == local.date()
        for h in history
    )
    seen = list(history)
    decisions = []
    for rec in records:
        payload = rec["payload"]
        if rec["kind"] == "lineup":
            action = sorted((m["player_id"], m["from"], m["to"]) for m in payload.get("moves", []))
        else:
            action = [(payload.get(k) or {}).get("player_id") for k in ("add", "drop")]
        key = hashlib.sha256(json.dumps([
            str(user_id), str(league_season_id), rec["provider_team_id"], rec["kind"],
            rec["scoring_period"], action,
        ], sort_keys=True).encode()).hexdigest()
        urgency = rec["decision"]
        prior = [h for h in seen if h["dedupe_key"] == key and h["status"] != "suppressed"]
        escalated = bool(prior) and urgency == "urgent" and not any(h["urgency"] == "urgent" for h in prior)
        observed = rec["as_of_ts"]
        if isinstance(observed, str):
            observed = datetime.fromisoformat(observed)
        expiry = observed + timedelta(minutes=15)
        deadline = payload.get("expires_at")
        if deadline:
            try:
                candidate = datetime.fromisoformat(deadline)
                if candidate.tzinfo is not None:
                    expiry = candidate
            except (ValueError, TypeError):
                pass
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
        seen.append({**row, "status": "suppressed" if disposition == "suppress" else "pending", "created_at": now})
    return decisions
