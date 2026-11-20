"""Evaluate the trigger policy for one week; print which ticks fire and why.

Reads game status from the public scoreboard (live) or a saved payload, and
the opportunity ledger from the database or a fixture file. Nothing is sent
and no ESPN action is taken; --record only logs fired trigger keys.

Examples:
    uv run python scripts/trigger_tick.py --season 2026 --week 4 --league 123456
    uv run python scripts/trigger_tick.py --scenario tests/fixtures/trigger_scenario.json \
        --now 2026-11-23T05:00:00+00:00
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from espn_ff_assistant.espn_client import fetch_scoreboard
from espn_ff_assistant.game_status import normalize_game_status
from espn_ff_assistant.triggers import TriggerConfig, coverage_report, due_triggers


def _parse_ts(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{value!r} must be timezone-aware")
    return stamp


def _opportunity(raw: dict[str, Any]) -> Any:
    from espn_ff_assistant.repository import Opportunity, StoredOpportunity

    expires = _parse_ts(raw["expires_at"])
    expired = _parse_ts(raw["expired_at"]) if raw.get("expired_at") else None
    return StoredOpportunity(
        Opportunity(
            raw["kind"], raw["change_key"], raw["provider_player_id"],
            raw.get("provider_team_id"), raw.get("source_view", "fixture"),
            expires, raw.get("attrs", {}),
        ),
        expired,
    )


def _config(args: argparse.Namespace) -> TriggerConfig:
    return TriggerConfig(
        settle_delay=timedelta(minutes=args.settle_minutes),
        daily_drop_hour=args.daily_drop_hour,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--season", type=int)
    parser.add_argument("--week", type=int)
    parser.add_argument("--league", type=int, help="ESPN league ID; reads ledger from DB")
    parser.add_argument("--scenario", type=Path, help="Fixture: scoreboard, opportunities, fired")
    parser.add_argument("--file", type=Path, help="Saved scoreboard payload")
    parser.add_argument("--observed-at", type=_parse_ts, help="Retrieval time for --file")
    parser.add_argument("--now", type=_parse_ts, default=datetime.now(UTC))
    parser.add_argument("--settle-minutes", type=int, default=45)
    parser.add_argument("--daily-drop-hour", type=int, default=9, help="League-local hour")
    parser.add_argument("--record", action="store_true",
                        help="Persist newly fired trigger keys (requires --league)")
    args = parser.parse_args()

    scenario = json.loads(args.scenario.read_text()) if args.scenario else {}
    if args.file or "scoreboard" in scenario:
        payload = json.loads(args.file.read_text()) if args.file else scenario["scoreboard"]
        if args.observed_at:
            observed_at = args.observed_at
        elif scenario.get("observed_at"):
            observed_at = _parse_ts(scenario["observed_at"])
        else:
            observed_at = args.now
        batch = normalize_game_status(payload, observed_at=observed_at)
    elif args.season and args.week:
        result = fetch_scoreboard(args.season, args.week)
        observed_at = datetime.fromisoformat(result.retrieved_at)
        batch = normalize_game_status(result.payload, observed_at=observed_at,
                                      fetch_state=result.state)
        print(f"scoreboard http={result.status_code} batch={batch.state}")
    else:
        parser.error("pass --scenario, --file, or --season/--week")
    if batch.state not in ("present", "empty"):
        print(f"batch={batch.state}; holding all ticks")
        return 1

    fired: set[str] = set(scenario.get("fired", []))
    if "opportunities" in scenario:
        opportunities = [_opportunity(o) for o in scenario["opportunities"]]
    elif args.league:
        from espn_ff_assistant.database import connection, load_dotenv
        from espn_ff_assistant.repository import Repository

        load_dotenv()
        with connection() as conn:
            repo = Repository(conn)
            league = repo.league_id(args.league)
            if league is None:
                parser.error(f"league {args.league} not loaded")
            opportunities = repo.stored_opportunities(league, args.season)
            fired |= repo.fired_trigger_keys(league, args.season)
    else:
        opportunities = []

    config = _config(args)
    report = coverage_report(args.now, list(batch.games.values()), opportunities, fired, config)
    for key, window in sorted(report["windows"].items()):
        print(f"window {key}: {window['state']} — {window['detail']}")
    triggers = due_triggers(args.now, list(batch.games.values()), opportunities, fired, config)
    if not triggers:
        print("HOLD: no triggers due")
    for trigger in sorted(triggers, key=lambda t: t.fires_at):
        flag = "URGENT " if trigger.urgent else ""
        print(f"FIRE {flag}{trigger.kind} key={trigger.key} — {trigger.detail}")
    for key in report["already_fired"]:
        print(f"skip {key} (already fired)")

    if args.record and triggers:
        if not args.league:
            parser.error("--record requires --league")
        from espn_ff_assistant.database import connection, load_dotenv
        from espn_ff_assistant.repository import Repository

        load_dotenv()
        with connection() as conn:
            repo = Repository(conn)
            league = repo.league_id(args.league)
            recorded = sum(
                repo.record_trigger_firing(league, args.season, t.key, args.now,
                                           {"kind": t.kind, "detail": t.detail})
                for t in triggers
            )
            conn.commit()
        print(f"recorded {recorded} new firing(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
