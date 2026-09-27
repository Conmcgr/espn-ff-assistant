"""Point-in-time backtests for the lineup gate and the FAAB bid rule.

Writes a private report (it names league teams) to data/derived/.

Usage:
    uv run python scripts/backtest.py [--from 2018] [--to 2025]
"""

from __future__ import annotations

import argparse
import statistics
from datetime import UTC, datetime
from pathlib import Path

from espn_ff_assistant import lineup, waivers
from espn_ff_assistant.database import connection, load_dotenv
from espn_ff_assistant.evaluation import backtest_faab, backtest_lineups
from espn_ff_assistant.repository import Repository


def _fmt(values: list[float]) -> str:
    if not values:
        return "n=0"
    return f"n={len(values)}, mean={statistics.mean(values):.2f}, median={statistics.median(values):.2f}"


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="season_from", type=int, default=2018)
    parser.add_argument("--to", dest="season_to", type=int, default=2025)
    parser.add_argument("--out", type=Path, default=Path("data/derived/recommendation-backtest.local.md"))
    args = parser.parse_args()
    seasons = list(range(args.season_from, args.season_to + 1))

    with connection() as conn:
        repo = Repository(conn)
        league_id = repo.default_league_id()
        user_id = repo.default_user_id()
        lb = backtest_lineups(repo, league_id, seasons)
        fb = backtest_faab(repo, league_id, args.season_from, args.season_to)
        user_teams = {s: repo.user_team(user_id, league_id, s) for s in seasons}

    lines = [
        "# Recommendation backtest (private)",
        "",
        f"_Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC; seasons {args.season_from}–{args.season_to}; "
        f"engine {lineup.ENGINE_VERSION}._",
        "",
        "## Lineup optimizer vs lineups actually set",
        "",
        "Injury status is ignored: backfilled historical status is end-of-season, not point-in-time.",
        "",
        f"- Team-weeks evaluated: {lb.team_weeks} (weeks skipped for missing data: {lb.skipped_weeks})",
        f"- Projected gain: {_fmt(lb.projected_gains)}",
        f"- Realized gain (actual points, optimizer − set lineup): {_fmt(lb.realized_gains)}",
        f"- Share of team-weeks where optimizer scored more: "
        f"{sum(r > 0 for r in lb.realized_gains) / max(1, len(lb.realized_gains)):.3f}; "
        f"less: {sum(r < 0 for r in lb.realized_gains) / max(1, len(lb.realized_gains)):.3f}",
        "",
        "| min projected gain | team-weeks | mean realized gain | share realized > 0 |",
        "|---|---|---|---|",
    ]
    for row in lb.threshold_table():
        lines.append(f"| {row['min_projected_gain']} | {row['team_weeks']} | {row['mean_realized_gain']} | "
                     f"{row['share_realized_positive']} |")
    mine = [g for (season, team), gains in lb.by_team.items() if user_teams.get(season) == team for g in gains]
    if mine:
        lines += ["", f"User team only: {_fmt(mine)}"]
    lines += [
        "",
        "## FAAB: quantile of prior winning bids at the position",
        "",
        f"- Contested share of executed claims: "
        f"{len(fb.contested_winning_bids) / max(1, len(fb.contested_winning_bids) + len(fb.uncontested_winning_bids)):.3f}",
        f"- Winning bids, contested: {_fmt(fb.contested_winning_bids)}",
        f"- Winning bids, uncontested: {_fmt(fb.uncontested_winning_bids)}",
        f"- Current rule: q{waivers.UNCONTESTED_QUANTILE} without rival need, "
        f"q{waivers.CONTESTED_QUANTILE} with rival need",
        "",
        "| quantile | contested runs | win rate | median overpay | median uncontested spend |",
        "|---|---|---|---|---|",
    ]
    for q, r in sorted(fb.by_quantile.items()):
        lines.append(
            f"| {q} | {r.contested} | {r.won / max(1, r.contested):.3f} | "
            f"{f'{statistics.median(r.overpay):.1f}' if r.overpay else '-'} | "
            f"{f'{statistics.median(r.uncontested_spend):.1f}' if r.uncontested_spend else '-'} |"
        )
    lines.append("")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines))
    print("\n".join(lines))
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
