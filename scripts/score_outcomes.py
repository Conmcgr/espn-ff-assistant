"""Score saved recommendations for a completed week against actual points.

Usage:
    uv run python scripts/score_outcomes.py --season 2026 --week 4
"""

from __future__ import annotations

import argparse

from espn_ff_assistant.database import connection, load_dotenv
from espn_ff_assistant.evaluation import score_saved
from espn_ff_assistant.repository import Repository


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--week", type=int, required=True)
    parser.add_argument("--horizon", type=int, default=3, help="Weeks of waiver outcome to score")
    args = parser.parse_args()
    with connection() as conn:
        repo = Repository(conn)
        n = score_saved(repo, repo.default_league_id(), repo.default_user_id(), args.season, args.week, args.horizon)
        conn.commit()
    print(f"Scored {n} recommendation(s) for {args.season} week {args.week}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
