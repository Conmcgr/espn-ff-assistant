"""Print measured rival signals for every team as of a season and week.

Usage:
    uv run python scripts/rival_report.py --season 2026 --week 6
    uv run python scripts/rival_report.py --season 2026 --week 6 --team 3 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from espn_ff_assistant.database import connection, load_dotenv
from espn_ff_assistant.repository import Repository
from espn_ff_assistant.rival_signals import compute_rival_signals


def _fmt(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.3f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--season", type=int, required=True, help="Season to compute as-of")
    parser.add_argument("--week", type=int, required=True, help="Scoring period to compute as-of")
    parser.add_argument("--team", type=int, help="Only this ESPN team ID")
    parser.add_argument("--json", action="store_true", help="Print JSON lines instead of a table")
    parser.add_argument("--league-id", type=int, help="ESPN provider league ID (default: auto-detect)")
    args = parser.parse_args()

    with connection() as conn:
        repo = Repository(conn)
        league_id = repo.league_id(args.league_id) if args.league_id else repo.default_league_id()
        if not league_id:
            print("Error: no ESPN league found in the database.", file=sys.stderr)
            return 1
        signals = compute_rival_signals(repo, league_id, args.season, args.week)
        names = {t.provider_team_id: t.name for t in repo.teams(league_id, args.season)}

    rows = [s for s in signals if args.team is None or s.provider_team_id == args.team]
    if args.json:
        for s in rows:
            print(json.dumps(asdict(s)))
        return 0
    team = None
    for s in rows:
        if s.provider_team_id != team:
            team = s.provider_team_id
            print(f"\nTeam {team} {names.get(team) or ''}".rstrip())
        window = f"{s.season_from}-{s.season_to}" if s.season_from != s.season_to else str(s.season_from)
        print(
            f"  {s.name:<36} {_fmt(s.value):>7}  n={s.sample_size:<3} {s.confidence:<12} "
            f"{s.basis:<15} {window:<9} {s.provenance}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
