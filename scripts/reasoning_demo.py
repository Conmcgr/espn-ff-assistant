"""Dry run of the close-call reasoning pass on a synthetic fixture (no database, no network).

Runs the deterministic lineup and waiver engines, shows which calls are routed
to the bounded reasoning pass and which stay deterministic, then what the pass
did with each one. The model is the offline stand-in; nothing is sent anywhere.

Usage:
    uv run python scripts/reasoning_demo.py [--model heuristic|abstain|error|slow|off]
                                            [--max-calls N] [--fixture tests/fixtures/close_calls.json]
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from espn_ff_assistant import lineup, reasoning, waivers
from espn_ff_assistant.lineup import SLOT_NAMES, LineupPlayer
from espn_ff_assistant.reasoning_offline import MODES, OfflineModel
from espn_ff_assistant.waivers import PoolPlayer, WaiverContext

ELIGIBLE = {
    "QB": {0, 7, 20, 21}, "RB": {2, 3, 23, 7, 20, 21}, "WR": {4, 3, 5, 23, 7, 20, 21},
    "TE": {6, 5, 23, 7, 20, 21}, "D/ST": {16, 20, 21}, "K": {17, 20, 21},
}
DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "close_calls.json"


def _player(row: dict, slot: int, kickoffs: dict[int, datetime], now: datetime) -> PoolPlayer:
    return PoolPlayer(
        lineup=LineupPlayer(
            provider_player_id=row["id"], name=row["name"], eligible_slots=frozenset(ELIGIBLE[row["pos"]]),
            projection=row["proj"], injury_status=row.get("injury"), locked=bool(row.get("locked")),
            current_slot=slot,
        ),
        position=row["pos"], ros=row["ros"], remaining_games=10,
        availability=row.get("availability", "rostered"),
        percent_owned=row.get("owned"), percent_change=row.get("change"),
    )


def load(path: Path):
    data = json.loads(path.read_text())
    now = datetime.fromisoformat(data["now"])
    kickoffs = {int(k): datetime.fromisoformat(v) for k, v in data["kickoffs"].items()}
    roster = [_player(r, r["slot"], kickoffs, now) for r in data["roster"]]
    pool = [_player(r, 20, kickoffs, now) for r in data["pool"]]
    team = {r["id"]: r["team"] for r in data["roster"] + data["pool"]}
    deadlines = {pid: kickoffs.get(t) for pid, t in team.items()}
    slot_counts = {int(k): v for k, v in data["slot_counts"].items()}
    state = reasoning.CurrentState(
        now=now,
        roster_ids=frozenset(p.pid for p in roster),
        available_ids=frozenset(p.pid for p in pool) - set(data.get("claimed_since_snapshot", [])),
        locked_ids=frozenset(p.pid for p in roster if p.lineup.locked),
        unavailable_ids=frozenset(p.pid for p in roster if not lineup.valuation(p.lineup).startable),
        eligible_slots={p.pid: p.lineup.eligible_slots for p in roster},
    )
    wctx = WaiverContext(slot_counts=slot_counts, roster=roster, remaining_weeks=data["remaining_weeks"],
                         faab_remaining=data["faab_remaining"])
    return roster, pool, slot_counts, wctx, deadlines, state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", choices=[*MODES, "off"], default="heuristic")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--max-calls", type=int, default=reasoning.DEFAULT_BUDGET.max_calls)
    args = parser.parse_args()

    roster, pool, slot_counts, wctx, deadlines, state = load(args.fixture)
    names = {p.pid: p.lineup.name for p in roster + pool}
    rec = lineup.optimize([p.lineup for p in roster], slot_counts)
    options = waivers.evaluate(pool, wctx)
    by_id = {p.pid: p for p in roster + pool}
    cases = (reasoning.lineup_cases(rec, [p.lineup for p in roster], deadlines, {p.pid: p.position for p in roster})
             + reasoning.waiver_cases(options, by_id, deadlines))

    print(f"Fixture {args.fixture.name}: now {state.now:%a %H:%M} UTC, model {args.model}\n")
    print(f"Lineup: deterministic decision {rec.decision}, gain {rec.gain:+.2f}")
    for pid, src, dst in rec.moves:
        print(f"  move  {names[pid]}: {SLOT_NAMES[src]} -> {SLOT_NAMES[dst]}  (deterministic)")
    for c in rec.close_calls:
        print(f"  close {SLOT_NAMES[c.slot]:<5} {names[c.starter_id]} over {names[c.alternative_id]} "
              f"by {c.margin:.2f}  -> reasoning")
    starters = [p for p in roster if rec.assignment[p.pid] not in lineup.NON_STARTING_SLOTS]
    close = {c.starter_id for c in rec.close_calls}
    for p in starters:
        if p.pid not in close:
            print(f"  clear {SLOT_NAMES[rec.assignment[p.pid]]:<5} {p.lineup.name}  (deterministic)")
    print("\nWaivers:")
    for o in options:
        print(f"  {o.decision:<10} add {names[o.add_id]}, drop {names.get(o.drop_id)}: "
              f"{o.delta_ros:+.1f} ROS / {o.delta_next:+.1f} next  -> {reasoning.waiver_route(o)}")

    model = None if args.model == "off" else OfflineModel(args.model, delay_s=2.0)
    budget = reasoning.Budget(max_calls=args.max_calls, call_timeout_s=1.0)
    outcomes = reasoning.review(cases, model, state, budget)
    print(f"\nReasoning pass ({len(cases)} case(s), cap {budget.max_calls} calls / "
          f"${budget.max_cost_usd:.2f} / {budget.total_timeout_s:.0f}s):")
    for o in outcomes:
        final = o.case.option(o.verdict.choice if o.applied else o.case.default_option)
        v = o.verdict
        said = f"{v.verdict} {v.choice or ''} ({v.confidence})".replace("  ", " ") if v else "-"
        print(f"  {o.case.case_id:<20} {o.status:<22} model: {said:<28} final: {final.label}")
        if v:
            print(f"  {'':<20} why: {v.rationale}  [cites {', '.join(v.citations)}]")
        for problem in o.problems:
            print(f"  {'':<20} problem: {problem}")
    spent = sum(o.cost_usd for o in outcomes)
    print(f"\nModel calls {sum(1 for o in outcomes if o.cost_usd)}, est. cost ${spent:.2f}; "
          f"nothing was sent to ESPN or by SMS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
