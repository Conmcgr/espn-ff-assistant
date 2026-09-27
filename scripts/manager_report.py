"""Compute and display manager behavior statistics.

Usage:
    # Compute and print all manager stats for the current season and week
    uv run python scripts/manager_report.py --season 2026 --week 3

    # Save to the database (upserts manager_features rows)
    uv run python scripts/manager_report.py --season 2026 --week 3 --save

    # Print only for one manager (by display name, partial match)
    uv run python scripts/manager_report.py --season 2026 --week 3 --manager alice

    # Also build/rebuild ownership intervals first
    uv run python scripts/manager_report.py --season 2026 --week 3 --rebuild-intervals
"""

from __future__ import annotations

import argparse
import sys

from espn_ff_assistant.database import connection, load_dotenv
from espn_ff_assistant.manager_stats import build_ownership_intervals, compute_all_features
from espn_ff_assistant.repository import Repository


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--season", type=int, required=True, help="Season to compute as-of")
    parser.add_argument("--week", type=int, required=True, help="Scoring period to compute as-of")
    parser.add_argument("--save", action="store_true", help="Upsert computed features into manager_features table")
    parser.add_argument("--manager", help="Filter output to managers whose name matches (case-insensitive)")
    parser.add_argument("--rebuild-intervals", action="store_true", help="Rebuild player_ownership_intervals before computing")
    parser.add_argument("--league-id", type=int, help="ESPN provider league ID (default: auto-detect)")
    args = parser.parse_args()

    with connection() as conn:
        repo = Repository(conn)
        league_id = repo.league_id(args.league_id) if args.league_id else repo.default_league_id()
        if not league_id:
            print("Error: no ESPN league found in the database.", file=sys.stderr)
            return 1

        if args.rebuild_intervals:
            print("Rebuilding player ownership intervals...", flush=True)
            n = build_ownership_intervals(conn, league_id)
            print(f"  {n} intervals inserted/updated.")

        print(f"Computing manager features as of {args.season} week {args.week}...", flush=True)
        features = compute_all_features(repo, conn, league_id, args.season, args.week)
        print(f"  {len(features)} feature rows computed.")

        # Group by manager for display
        managers_meta = conn.execute(
            """
            SELECT DISTINCT m.id, m.display_name, m.provider_member_id
            FROM managers m
            JOIN team_owners to2 ON to2.manager_id=m.id
            JOIN league_seasons ls ON ls.id=to2.league_season_id
            WHERE ls.league_id=%s ORDER BY m.display_name
            """,
            (league_id,),
        ).fetchall()
        mgr_names = {str(r[0]): (r[1] or r[2] or str(r[0])) for r in managers_meta}

        by_manager: dict[str, dict[str, float | None]] = {}
        by_manager_sample: dict[str, dict[str, int]] = {}
        by_manager_confidence: dict[str, dict[str, str]] = {}
        for f in features:
            mid = f["manager_id"]
            by_manager.setdefault(mid, {})[f["stat_name"]] = f.get("value")
            by_manager_sample.setdefault(mid, {})[f["stat_name"]] = f.get("sample_size") or 0
            by_manager_confidence.setdefault(mid, {})[f["stat_name"]] = f.get("confidence") or "?"

        name_filter = args.manager.lower() if args.manager else None
        printed = 0
        for mid, stats in sorted(by_manager.items(), key=lambda kv: mgr_names.get(kv[0], "")):
            name = mgr_names.get(mid, mid)
            if name_filter and name_filter not in name.lower():
                continue
            print(f"\n{'='*60}")
            print(f"  {name}")
            print(f"{'='*60}")
            for stat, value in sorted(stats.items()):
                n = by_manager_sample[mid].get(stat, 0)
                conf = by_manager_confidence[mid].get(stat, "?")
                if value is None:
                    val_str = "—"
                elif isinstance(value, float) and value != int(value):
                    val_str = f"{value:.2f}"
                else:
                    val_str = str(int(value) if value == int(value) else value)
                print(f"  {stat:<45} {val_str:>10}   n={n:>4}  [{conf}]")
            printed += 1

        if printed == 0 and name_filter:
            print(f"No managers matched '{args.manager}'.")

        if args.save:
            n_saved = repo.upsert_manager_features(features)
            conn.commit()
            print(f"\nSaved {n_saved} feature rows to manager_features.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
