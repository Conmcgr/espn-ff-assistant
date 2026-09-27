"""Recommendation CLI: lineup (start/sit) and waivers, persisted with evidence.

Usage:
    uv run python scripts/recommend.py sync
    uv run python scripts/recommend.py whoami --season 2026 [--team 4]
    uv run python scripts/recommend.py lineup  --season 2026 --week 4 [--save]
    uv run python scripts/recommend.py waivers --season 2026 --week 4 [--save]
    uv run python scripts/recommend.py scan    --season 2026 --week 4 --save
    uv run python scripts/recommend.py explain <recommendation_id>
    uv run python scripts/recommend.py feedback <recommendation_id> --action rejected --reason too_much_faab
    uv run python scripts/recommend.py prefs --set streams_dst=true --set stash_injured=true
    uv run python scripts/recommend.py prefs --protect <player_id>
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from espn_ff_assistant import engine
from espn_ff_assistant.config import ConfigurationError, Settings
from espn_ff_assistant.database import connection, load_dotenv
from espn_ff_assistant.repository import Repository

DECISION_LABEL = {
    "urgent": "URGENT", "notify": "RECOMMEND", "info": "FYI",
    "suppressed": "suppressed", "no_action": "NO_ACTION",
}


def _print_records(result: engine.EngineResult, ids: list[str] | None, verbose: bool) -> None:
    for i, rec in enumerate(result.records):
        if rec["decision"] == "suppressed" and not verbose:
            continue
        label = DECISION_LABEL.get(rec["decision"], rec["decision"])
        rid = f"  [{ids[i]}]" if ids else ""
        print(f"{label:>10}  {rec['kind']:<7} {rec['summary']}{rid}")
        if rec["kind"] == "waiver" and rec["decision"] != "no_action":
            p = rec["payload"]
            add = p["add"]
            why = [f"{add['position']} projected {add['week_projection'] or 0:.1f} next week, ROS {add['ros'] or 0:.0f}"]
            if add.get("percent_owned") is not None:
                why.append(f"{add['percent_owned']:.0f}% owned ({add.get('percent_change') or 0:+.1f})")
            if p.get("bid"):
                b = p["bid"]
                why.append(f"FAAB from {b['comparables_used']} league claims ({b['comparable_scope']}), "
                           f"{len(b['competitors'])} rival team(s) with need; confidence {b['confidence']}")
            for line in why:
                print(f"{'':>12}- {line}")
        if rec["kind"] == "lineup":
            for c in rec["payload"]["close_calls"][:3]:
                print(f"{'':>12}- close call at {c['slot']}: {c['starter']} over {c['alternative']} by {c['margin']}")
    suppressed = sum(1 for r in result.records if r["decision"] == "suppressed")
    if suppressed and not verbose:
        print(f"{'':>12}({suppressed} lower-value option(s) suppressed; --verbose to show)")


def _context(repo: Repository, args: argparse.Namespace) -> engine._Context:
    league_id = repo.default_league_id()
    user_id = repo.default_user_id()
    if not league_id or not user_id:
        raise engine.EngineError("No league/user loaded; run the loader first.")
    as_of = datetime.fromisoformat(args.as_of) if getattr(args, "as_of", None) else None
    return engine.context(repo, league_id, user_id, args.season, args.week, as_of)


def cmd_run(args: argparse.Namespace, kinds: list[str]) -> int:
    with connection() as conn:
        repo = Repository(conn)
        ctx = _context(repo, args)
        for kind in kinds:
            result = engine.run_lineup(ctx) if kind == "lineup" else engine.run_waivers(ctx)
            ids = engine.save(ctx, result) if args.save else None
            _print_records(result, ids, args.verbose)
        if args.save:
            conn.commit()
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    with connection() as conn:
        repo = Repository(conn)
        league_id, user_id = repo.default_league_id(), repo.default_user_id()
        if args.team is None:
            current = repo.user_team(user_id, league_id, args.season)
            for team in repo.teams(league_id, args.season):
                mark = "*" if team.provider_team_id == current else " "
                print(f"{mark} {team.provider_team_id:>3}  {team.name}")
            print("\nSet with: recommend.py whoami --season", args.season, "--team <id>")
            return 0
        repo.set_user_team(user_id, league_id, args.season, args.team)
        conn.commit()
        print(f"Mapped this user to team {args.team} for {args.season}.")
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    with connection() as conn:
        repo = Repository(conn)
        rec = repo.recommendation(args.id)
        if not rec:
            print("No such recommendation.", file=sys.stderr)
            return 1
        print(f"{rec.kind} {rec.season} week {rec.scoring_period}  decision={rec.decision}  "
              f"confidence={rec.confidence}  engine={rec.engine_version}  as_of={rec.as_of_ts:%Y-%m-%d %H:%M}")
        print(rec.summary)
        print(json.dumps(rec.payload, indent=2, default=str))
        if rec.alternatives:
            print("alternatives:", json.dumps(rec.alternatives, indent=2, default=str))
        print("evidence:")
        for e in rec.evidence:
            print(f"  {e['kind']:<18} {e['key']:<28} {json.dumps(e['value'], default=str)}")
        for fb in repo.feedback(args.id):
            print(f"feedback: {fb['action']} {fb['reason'] or ''} {fb['note'] or ''}")
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    with connection() as conn:
        repo = Repository(conn)
        if not repo.recommendation(args.id):
            print("No such recommendation.", file=sys.stderr)
            return 1
        repo.save_feedback(args.id, args.action, args.reason, args.note)
        conn.commit()
        print("Feedback recorded.")
    return 0


def _parse_value(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def cmd_prefs(args: argparse.Namespace) -> int:
    with connection() as conn:
        repo = Repository(conn)
        league_id, user_id = repo.default_league_id(), repo.default_user_id()
        for item in args.set or []:
            key, _, raw = item.partition("=")
            repo.set_preference(user_id, league_id, key, _parse_value(raw), args.weight, args.source)
        for pid in args.protect or []:
            repo.set_preference(user_id, league_id, f"protected:{pid}", True, 1.0, "explicit")
        for pid in args.unprotect or []:
            repo.delete_preference(user_id, league_id, f"protected:{pid}")
        conn.commit()
        for key, p in sorted(repo.preferences(user_id, league_id).items()):
            print(f"  {key:<24} {json.dumps(p.value)}  weight={p.weight}  source={p.source}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from espn_ff_assistant.sync import sync_current

    try:
        settings = Settings.from_env()
    except ConfigurationError as error:
        print(f"Configuration error: {error}")
        return 2
    result = sync_current(settings, Path("data/raw"), season=args.season)
    print(json.dumps({"current_period": result.current_period, "periods": result.periods,
                      "fetch_states": result.states}, indent=2))
    return 0 if set(result.states) <= {"present"} else 1


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("lineup", "waivers", "scan"):
        p = sub.add_parser(name)
        p.add_argument("--season", type=int, required=True)
        p.add_argument("--week", type=int, required=True)
        p.add_argument("--as-of", help="ISO timestamp for point-in-time reads (default: now)")
        p.add_argument("--save", action="store_true")
        p.add_argument("--verbose", action="store_true")

    p = sub.add_parser("sync")
    p.add_argument("--season", type=int)

    p = sub.add_parser("whoami")
    p.add_argument("--season", type=int, required=True)
    p.add_argument("--team", type=int)

    p = sub.add_parser("explain")
    p.add_argument("id")

    p = sub.add_parser("feedback")
    p.add_argument("id")
    p.add_argument("--action", required=True, choices=["accepted", "rejected", "modified", "ignored", "impossible"])
    p.add_argument("--reason", choices=["too_much_faab", "dont_believe_player", "prefer_current", "other"])
    p.add_argument("--note")

    p = sub.add_parser("prefs")
    p.add_argument("--set", action="append", help="key=value (JSON value)")
    p.add_argument("--weight", type=float, default=0.5)
    p.add_argument("--source", choices=["onboarding", "explicit"], default="explicit")
    p.add_argument("--protect", action="append", type=int, help="player id never to drop")
    p.add_argument("--unprotect", action="append", type=int)

    args = parser.parse_args()
    try:
        if args.command == "lineup":
            return cmd_run(args, ["lineup"])
        if args.command == "waivers":
            return cmd_run(args, ["waivers"])
        if args.command == "scan":
            return cmd_run(args, ["lineup", "waivers"])
        return {
            "sync": cmd_sync, "whoami": cmd_whoami, "explain": cmd_explain,
            "feedback": cmd_feedback, "prefs": cmd_prefs,
        }[args.command](args)
    except engine.EngineError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
