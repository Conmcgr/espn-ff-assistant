"""Discover feasible trades for a synthetic league fixture without external services.

Usage:
    uv run python scripts/find_trades.py --fixture tests/fixtures/trade_discovery_league.json
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from espn_ff_assistant.lineup import BENCH_SLOT, LineupPlayer
from espn_ff_assistant.trade_discovery import (
    DiscoveryContext,
    OpportunityHint,
    PartnerSignals,
    RivalTeam,
    discover,
    summarize,
)
from espn_ff_assistant.waivers import PoolPlayer


def _player(raw: dict, availability: str) -> PoolPlayer:
    return PoolPlayer(
        LineupPlayer(raw["id"], raw.get("name", f"Synthetic {raw['id']}"),
                     frozenset(raw["slots"]), raw.get("next"),
                     injury_status=raw.get("injury_status"),
                     on_bye=raw.get("on_bye", False),
                     locked=raw.get("locked", False),
                     current_slot=raw.get("current_slot", BENCH_SLOT)),
        raw["position"], raw.get("ros"), raw.get("remaining_games", 10), availability)


def _team(raw: dict) -> RivalTeam:
    return RivalTeam(raw["team_id"], [_player(p, "rostered") for p in raw["roster"]],
                     raw.get("name", ""))


def load_fixture(data: dict) -> DiscoveryContext:
    context = dict(data["context"])
    context["slot_counts"] = {int(k): v for k, v in context["slot_counts"].items()}
    context["user_team"] = _team(context["user_team"])
    context["rivals"] = [_team(t) for t in context["rivals"]]
    context["free_agents"] = [_player(p, "free_agent") for p in context.get("free_agents", [])]
    context["protected_ids"] = frozenset(context.get("protected_ids", []))
    context["signals"] = {
        int(k): PartnerSignals(**v) for k, v in data.get("signals", {}).items()
    }
    context["opportunities"] = [
        OpportunityHint(**o) for o in data.get("opportunities", [])
    ]
    return DiscoveryContext(**context)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--partner", type=int, help="Only this partner team ID")
    args = parser.parse_args()
    try:
        context = load_fixture(json.loads(args.fixture.read_text()))
        ideas = discover(context)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(f"Invalid fixture: {exc}")
    if args.partner is not None:
        ideas = [i for i in ideas if i.partner_team == args.partner]
    if args.json:
        print(json.dumps([asdict(i) for i in ideas], indent=2))
        return
    print(summarize(ideas))
    print("Plausibility is a heuristic index of fit, not an acceptance probability.")


if __name__ == "__main__":
    main()
