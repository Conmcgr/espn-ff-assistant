"""Print NFL game-status observations from the public ESPN scoreboard or a file."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from espn_ff_assistant.espn_client import fetch_scoreboard
from espn_ff_assistant.game_status import normalize_fantasy_schedule, normalize_game_status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int)
    parser.add_argument("--week", type=int)
    parser.add_argument("--season-type", type=int, default=2)
    parser.add_argument("--file", type=Path)
    parser.add_argument("--observed-at", help="Required retrieval timestamp for saved input")
    parser.add_argument("--fantasy", action="store_true", help="Normalize a saved fantasy schedule")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    if args.file:
        if not args.observed_at:
            parser.error("--file requires --observed-at")
        try:
            observed_at = datetime.fromisoformat(args.observed_at)
            payload = json.loads(args.file.read_text())
        except (OSError, ValueError) as error:
            parser.error(str(error))
        fetch_state = "present"
    else:
        if args.fantasy or args.season is None or args.week is None:
            parser.error("live scoreboard requires --season and --week; --fantasy requires --file")
        result = fetch_scoreboard(args.season, args.week, season_type=args.season_type)
        observed_at = datetime.fromisoformat(result.retrieved_at)
        payload, fetch_state = result.payload, result.state
        print(f"season={result.season} week={result.scoring_period} "
              f"seasontype={result.season_type} http={result.status_code}")
    normalizer = normalize_fantasy_schedule if args.fantasy else normalize_game_status
    try:
        batch = normalizer(payload, observed_at=observed_at, fetch_state=fetch_state)
    except ValueError as error:
        parser.error(str(error))
    print(f"observed_at={observed_at.isoformat()} batch={batch.state}")
    print(json.dumps(dict(sorted(Counter(g.state.value for g in batch.games.values()).items()))))
    if not args.summary:
        for game in batch.games.values():
            final = game.final_observed_at.isoformat() if game.final_observed_at else "-"
            print(f"{game.provider_game_id} {game.source} {game.state.value} final_observed_at={final}")
    for issue in batch.issues:
        print(f"issue: {issue}")
    return 0 if batch.state in ("present", "empty") else 1


if __name__ == "__main__":
    raise SystemExit(main())
