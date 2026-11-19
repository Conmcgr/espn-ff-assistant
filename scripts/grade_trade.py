"""Grade an explicit synthetic trade fixture without external services."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from espn_ff_assistant.lineup import BENCH_SLOT, LineupPlayer
from espn_ff_assistant.trades import TradeContext, TradeOffer, grade_trade
from espn_ff_assistant.waivers import PoolPlayer


def load_fixture(data: dict) -> tuple[TradeOffer, TradeContext]:
    def player(raw: dict, availability: str) -> PoolPlayer:
        return PoolPlayer(
            LineupPlayer(raw["id"], raw.get("name", f"Synthetic {raw['id']}"),
                         frozenset(raw["slots"]), raw.get("next"),
                         injury_status=raw.get("injury_status"),
                         locked=raw.get("locked", False),
                         current_slot=raw.get("current_slot", BENCH_SLOT)),
            raw["position"], raw.get("ros"), raw.get("remaining_games", 10), availability)

    context = dict(data["context"])
    context["slot_counts"] = {int(k): v for k, v in context["slot_counts"].items()}
    for key in ("user_roster", "partner_roster", "free_agents"):
        context[key] = [player(p, "free_agent" if key == "free_agents" else "rostered")
                        for p in context.get(key, [])]
    context["protected_ids"] = frozenset(context.get("protected_ids", []))
    return TradeOffer(**data["offer"]), TradeContext(**context)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        offer, context = load_fixture(json.loads(args.fixture.read_text()))
        result = grade_trade(offer, context)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(f"Invalid fixture: {exc}")
    if args.json:
        print(json.dumps(asdict(result), indent=2))
        return
    print(f"Trade grade: {result.grade} ({result.engine_version})")
    print(f"Legality: {', '.join(result.violations) or 'legal'}")
    for label, side in (("User", result.user), ("Partner", result.partner)):
        if side is not None:
            print(f"{label}: next {side.delta_next:+.2f}; ROS {side.delta_ros:+.2f}")
            print(f"  Assumed fills before/after: {side.before_fills} / {side.after_fills}; auto-drops: {side.assumed_drops}")
    if result.user is not None:
        print(f"User ROS uncertainty (SD): {result.user_ros_sd:.2f}")
        print(f"Best no-trade alternative: {result.alternative_ros_gain:+.2f} ROS "
              f"(add {result.alternative_add}, drop {result.alternative_drop})")
        print(f"Trade ROS net of alternative: {result.net_alternative_ros_gain:+.2f}")
    for note in result.notes:
        print(f"Note: {note}")
    print("Partner value is not an acceptance probability.")


if __name__ == "__main__":
    main()
