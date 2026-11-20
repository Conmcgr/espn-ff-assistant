"""Attach free public signals (depth chart, injury, snap share, cited news) to
the league's live ledger opportunities and print them.

Read-only by default; --record stores one enrichment run and its signals.
No ESPN action is taken and no credential is sent to any source.

Example:
    uv run python scripts/enrich_opportunities.py --league 123456 --season 2026 --week 11
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from espn_ff_assistant.database import connection, load_dotenv
from espn_ff_assistant.enrichment import enrich
from espn_ff_assistant.enrichment_sources import PublicSources
from espn_ff_assistant.repository import Repository, Signal


def _parse_ts(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{value!r} must be timezone-aware")
    return stamp


def _describe(signal: Signal) -> str:
    a = signal.attrs
    if signal.kind == "depth_chart":
        slots = ", ".join(f"{s['position']}{s['rank']}" for s in a["slots"])
        detail = f"{a['team']} {slots}"
    elif signal.kind == "injury":
        detail = (f"wk{a['week']} report={a['report_status']} "
                  f"practice={a['practice_status']} "
                  f"({a['report_primary_injury'] or a['practice_primary_injury']})")
    elif signal.kind == "usage":
        detail = f"wks {a['weeks']} off% {a['offense_pct']}"
    else:
        detail = f"{a['headline'][:90]} <{signal.citation_url}>"
    return f"    {signal.kind:<11} {signal.freshness:<5} as_of={signal.as_of:%Y-%m-%dT%H:%MZ} {detail}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--league", type=int, required=True)
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--week", type=int, required=True, help="current scoring period")
    parser.add_argument("--now", type=_parse_ts, help="observation time (default: now)")
    parser.add_argument("--news-limit", type=int, default=5)
    parser.add_argument("--no-news", action="store_true")
    parser.add_argument("--record", action="store_true")
    args = parser.parse_args()
    observed_at = args.now or datetime.now(UTC)

    load_dotenv()
    with connection() as conn:
        repo = Repository(conn)
        league = repo.league_id(args.league)
        if league is None:
            print(f"league {args.league} not found")
            return 1
        live = repo.active_opportunities(league, args.season, observed_at)
        sources = PublicSources(news=not args.no_news)
        try:
            result = enrich(live, sources, args.season, args.week, observed_at,
                            news_limit=args.news_limit)
        finally:
            sources.close()
        if args.record and live:
            run_id = repo.insert_enrichment_run(league, args.season, observed_at, result.sources)
            for opportunity_id, signals in result.signals.items():
                for signal in signals:
                    repo.insert_opportunity_signal(run_id, opportunity_id, signal)
            print(f"recorded enrichment run {run_id}")

    print(f"observed_at={observed_at.isoformat()} live opportunities={len(live)}")
    for name, state in result.sources.items():
        print(f"  source {name}: {state}")
    for opportunity_id, opportunity in live:
        signals = result.signals.get(opportunity_id, [])
        print(f"{opportunity.kind} player={opportunity.provider_player_id} "
              f"expires={opportunity.expires_at:%Y-%m-%dT%H:%MZ} signals={len(signals)}")
        for signal in signals:
            print(_describe(signal))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
