"""One bounded reasoning pass over close calls; plain code decides the rest.

Only two kinds of case reach a model: a lineup slot whose starter beats the
best bench alternative by less than `lineup.CLOSE_MARGIN`, and a waiver option
that falls just short of the notification bar. Everything else (clear moves,
ineligible starters, options above the bar, weak options) stays deterministic.

The model sees a brief (question, options, evidence keys) and reads the as-of
evidence through read-only `CaseTools`. Its answer must be structured and
grounded: a recommend/abstain verdict, a choice among the offered options,
every other option addressed as an alternative, a confidence, and citations of
evidence it actually read. A chosen action is then revalidated against current
availability, legality and deadline outside the model. Calls, time and cost
are capped per run, and any failure keeps the deterministic decision: a close
call is never pitched unless the whole pass succeeds. Disabled by default.
"""

from __future__ import annotations

import copy
import os
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Protocol

from .config import ConfigurationError
from .lineup import (
    BENCH_SLOT,
    CLOSE_MARGIN,
    NON_STARTING_SLOTS,
    SLOT_NAMES,
    LineupPlayer,
    LineupRecommendation,
    valuation,
)
from .waivers import NOTIFY_MIN_NEXT, NOTIFY_MIN_ROS, PoolPlayer, WaiverOption

ENGINE_VERSION = "reasoning-1"

NEAR_MISS_FRACTION = 0.75       # share of a notify threshold that makes a waiver option close
CONFIDENCES = ("low", "medium", "high")
MIN_APPLY_CONFIDENCE = "medium"
MAX_RATIONALE_CHARS = 600
DECISION_RANK = {"suppressed": 0, "no_action": 0, "info": 1, "notify": 2, "urgent": 3}


@dataclass(frozen=True)
class Budget:
    max_calls: int = 8
    call_timeout_s: float = 20.0
    total_timeout_s: float = 60.0
    max_cost_usd: float = 0.25
    max_tool_calls: int = 12


DEFAULT_BUDGET = Budget()


@dataclass(frozen=True)
class Option:
    option_id: str
    label: str
    action: Mapping[str, Any]


@dataclass(frozen=True)
class Case:
    case_id: str
    kind: str                                   # 'lineup' | 'waiver'
    record_index: int                           # engine record this case annotates
    question: str
    options: tuple[Option, ...]
    default_option: str                         # what deterministic code already does
    evidence: Mapping[str, Mapping[str, Any]]
    deadline: datetime | None

    def option(self, option_id: str) -> Option:
        return next(o for o in self.options if o.option_id == option_id)

    def brief(self) -> dict[str, Any]:
        """What the model is told up front; facts come only through `CaseTools`."""
        return {
            "case_id": self.case_id,
            "kind": self.kind,
            "question": self.question,
            "options": [{"id": o.option_id, "label": o.label} for o in self.options],
            "default_option": self.default_option,
            "evidence_keys": sorted(self.evidence),
            "deadline": self.deadline.isoformat() if self.deadline else None,
        }


class ToolBudgetExceeded(RuntimeError):
    """The model made more tool calls than the budget allows."""


class CaseTools:
    """Read-only lookups over one case's as-of evidence, with a call cap."""

    def __init__(self, case: Case, max_calls: int):
        self._case = case
        self._max_calls = max_calls
        self.calls: list[str] = []
        self.read: set[str] = set()

    def _count(self, name: str) -> None:
        if len(self.calls) >= self._max_calls:
            raise ToolBudgetExceeded(name)
        self.calls.append(name)

    def list_evidence(self) -> list[str]:
        self._count("list_evidence")
        return sorted(self._case.evidence)

    def evidence(self, key: str) -> dict[str, Any]:
        self._count(f"evidence:{key}")
        if key not in self._case.evidence:
            raise KeyError(key)
        self.read.add(key)
        return copy.deepcopy(dict(self._case.evidence[key]))

    def options(self) -> list[dict[str, Any]]:
        self._count("options")
        return [{"id": o.option_id, "label": o.label, "action": dict(o.action)}
                for o in self._case.options]


class ReasoningModel(Protocol):
    name: str

    def estimate_cost(self, brief: Mapping[str, Any]) -> float:
        """Upper-bound USD cost of one `decide` call for this brief."""

    def decide(self, brief: Mapping[str, Any], tools: CaseTools) -> Mapping[str, Any]:
        """Structured verdict; see `parse_verdict` for the schema."""


@dataclass(frozen=True)
class Verdict:
    verdict: str                                # 'recommend' | 'abstain'
    choice: str | None
    confidence: str
    alternatives: tuple[tuple[str, str], ...]   # (option id, why not)
    rationale: str
    citations: tuple[str, ...]


class InvalidOutput(ValueError):
    """Model output is malformed, off-menu, or not grounded in evidence it read."""


def parse_verdict(raw: Any, case: Case, read: set[str]) -> Verdict:
    if not isinstance(raw, Mapping):
        raise InvalidOutput("output is not an object")
    verdict = raw.get("verdict")
    if verdict not in ("recommend", "abstain"):
        raise InvalidOutput("verdict must be recommend or abstain")
    confidence = raw.get("confidence")
    if confidence not in CONFIDENCES:
        raise InvalidOutput("confidence must be low, medium or high")
    rationale = raw.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > MAX_RATIONALE_CHARS:
        raise InvalidOutput("rationale must be non-empty text within the length cap")
    citations = raw.get("citations")
    if (not isinstance(citations, list) or not citations
            or not all(isinstance(c, str) for c in citations)):
        raise InvalidOutput("citations must be a non-empty list of evidence keys")
    ungrounded = sorted(set(citations) - read)
    if ungrounded:
        raise InvalidOutput(f"cites evidence it did not read: {', '.join(ungrounded)}")

    ids = {o.option_id for o in case.options}
    choice = raw.get("choice")
    alternatives: list[tuple[str, str]] = []
    if verdict == "recommend":
        if not isinstance(choice, str) or choice not in ids:
            raise InvalidOutput("choice must be one of the offered options")
        alts = raw.get("alternatives")
        if not isinstance(alts, list):
            raise InvalidOutput("alternatives must be a list")
        for alt in alts:
            if (not isinstance(alt, Mapping) or not isinstance(alt.get("option"), str)
                    or alt["option"] not in ids - {choice}
                    or not isinstance(alt.get("why_not"), str) or not alt["why_not"].strip()):
                raise InvalidOutput("each alternative needs another option id and a reason")
            alternatives.append((alt["option"], alt["why_not"]))
        if {a for a, _ in alternatives} != ids - {choice}:
            raise InvalidOutput("every other option must be addressed as an alternative")
    elif choice is not None:
        raise InvalidOutput("an abstain verdict carries no choice")
    return Verdict(verdict, choice, confidence, tuple(alternatives), rationale.strip(),
                   tuple(dict.fromkeys(citations)))


# ---------------------------------------------------------------------------
# Shortlisting
# ---------------------------------------------------------------------------

def _player_facts(p: LineupPlayer, position: str | None = None, pool: PoolPlayer | None = None) -> dict[str, Any]:
    val = valuation(p)
    facts: dict[str, Any] = {
        "player_id": p.provider_player_id, "name": p.name, "position": position,
        "week_projection": p.projection, "effective_projection": round(val.value, 2),
        "injury_status": p.injury_status, "flags": list(val.flags),
        "on_bye": p.on_bye, "locked": p.locked, "slot": SLOT_NAMES.get(p.current_slot, p.current_slot),
    }
    if pool is not None:
        facts.update({"ros": pool.ros, "availability": pool.availability,
                      "percent_owned": pool.percent_owned, "percent_change": pool.percent_change})
    return facts


def _earliest(*times: datetime | None) -> datetime | None:
    """Soonest deadline, or None when any is unknown so revalidation fails closed."""
    if any(t is None for t in times):
        return None
    return min(times)


def lineup_cases(
    rec: LineupRecommendation,
    players: Sequence[LineupPlayer],
    deadlines: Mapping[int, datetime | None],
    positions: Mapping[int, str] | None = None,
    record_index: int = 0,
) -> list[Case]:
    """One case per close call: keep the optimizer's starter or swap in the alternative."""
    by_id = {p.provider_player_id: p for p in players}
    positions = positions or {}
    cases = []
    for call in rec.close_calls:
        starter, alt = by_id[call.starter_id], by_id[call.alternative_id]
        slot = SLOT_NAMES.get(call.slot, str(call.slot))
        cases.append(Case(
            case_id=f"lineup:{call.slot}:{call.starter_id}:{call.alternative_id}",
            kind="lineup", record_index=record_index,
            question=f"Who should start at {slot}: {starter.name} or {alt.name}?",
            options=(
                Option("keep", f"Start {starter.name} at {slot}",
                       {"type": "keep", "start_id": starter.provider_player_id, "slot": call.slot}),
                Option("swap", f"Start {alt.name} at {slot}, bench {starter.name}",
                       {"type": "lineup_swap", "start_id": alt.provider_player_id,
                        "bench_id": starter.provider_player_id, "slot": call.slot}),
            ),
            default_option="keep",
            evidence={
                "margin": {"slot": slot, "margin": call.margin, "close_margin": CLOSE_MARGIN},
                f"player:{starter.provider_player_id}": _player_facts(
                    starter, positions.get(starter.provider_player_id)),
                f"player:{alt.provider_player_id}": _player_facts(
                    alt, positions.get(alt.provider_player_id)),
            },
            deadline=_earliest(deadlines.get(starter.provider_player_id),
                               deadlines.get(alt.provider_player_id)),
        ))
    return cases


def is_near_miss(option: WaiverOption) -> bool:
    """Suppressed only for falling a little short of the bar, not for a conflict."""
    meets = option.delta_ros >= NOTIFY_MIN_ROS or option.delta_next >= NOTIFY_MIN_NEXT
    near = (option.delta_ros >= NEAR_MISS_FRACTION * NOTIFY_MIN_ROS
            or option.delta_next >= NEAR_MISS_FRACTION * NOTIFY_MIN_NEXT)
    return (option.decision == "suppressed" and not meets and near
            and "conflicts_with_higher_ranked" not in option.reasons)


def waiver_route(option: WaiverOption) -> str:
    if option.decision == "notify":
        return "deterministic: clears the notification bar"
    if is_near_miss(option):
        return "reasoning: just short of the notification bar"
    if "conflicts_with_higher_ranked" in option.reasons:
        return "deterministic: conflicts with a higher-ranked move"
    return "deterministic: well short of the notification bar"


def waiver_cases(
    options: Sequence[WaiverOption],
    players: Mapping[int, PoolPlayer],
    deadlines: Mapping[int, datetime | None],
    limit: int | None = None,
) -> list[Case]:
    """One case per near-miss option: keep passing, or claim it anyway."""
    cases = []
    for index, o in enumerate(options[:limit]):
        if not is_near_miss(o) or o.drop_id is None:
            continue
        add, drop = players[o.add_id], players[o.drop_id]
        claim = f"Add {add.lineup.name}, drop {drop.lineup.name}"
        if o.bid is not None:
            claim += f", bid ${o.bid.amount}"
        evidence: dict[str, Mapping[str, Any]] = {
            "deltas": {"delta_next": o.delta_next, "delta_ros": o.delta_ros, "kind": o.kind,
                       "notify_min_next": NOTIFY_MIN_NEXT, "notify_min_ros": NOTIFY_MIN_ROS},
            f"player:{add.pid}": _player_facts(add.lineup, add.position, add),
            f"player:{drop.pid}": _player_facts(drop.lineup, drop.position, drop),
        }
        if o.bid is not None:
            evidence["bid"] = {"amount": o.bid.amount, "low": o.bid.low, "high": o.bid.high,
                               "confidence": o.bid.confidence,
                               "comparables_used": o.bid.comparables_used,
                               "rivals_with_need": len(o.bid.competitors)}
        cases.append(Case(
            case_id=f"waiver:{o.add_id}:{o.drop_id}", kind="waiver", record_index=index,
            question=f"Is {add.lineup.name} worth claiming although the move is "
                     f"{o.delta_ros:+.1f} ROS / {o.delta_next:+.1f} next week, just under the bar?",
            options=(
                Option("pass", "Pass on this claim", {"type": "pass"}),
                Option("claim", claim, {"type": "waiver_claim", "add_id": o.add_id,
                                        "drop_id": o.drop_id}),
            ),
            default_option="pass",
            evidence=evidence,
            deadline=deadlines.get(o.add_id),
        ))
    return cases


# ---------------------------------------------------------------------------
# Revalidation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CurrentState:
    """Fresh facts read after the model answers; the model never supplies these."""

    now: datetime
    roster_ids: frozenset[int]
    available_ids: frozenset[int]
    locked_ids: frozenset[int] = frozenset()
    unavailable_ids: frozenset[int] = frozenset()   # OUT/IR/suspended/bye now
    protected_ids: frozenset[int] = frozenset()
    ir_ids: frozenset[int] = frozenset()
    eligible_slots: Mapping[int, frozenset[int]] = field(default_factory=dict)


def revalidate(case: Case, option: Option, state: CurrentState) -> list[str]:
    action = option.action
    kind = action["type"]
    if kind in ("keep", "pass"):
        return []
    problems = []
    if case.deadline is None:
        problems.append("deadline unknown")
    elif state.now >= case.deadline:
        problems.append("deadline passed")
    if kind == "lineup_swap":
        start, bench, slot = action["start_id"], action["bench_id"], action["slot"]
        for pid in (start, bench):
            if pid not in state.roster_ids:
                problems.append(f"player {pid} no longer rostered")
            if pid in state.locked_ids:
                problems.append(f"player {pid} is locked")
        if start in state.unavailable_ids:
            problems.append(f"player {start} is no longer startable")
        if slot in NON_STARTING_SLOTS or slot not in state.eligible_slots.get(start, frozenset()):
            problems.append(f"player {start} is not eligible at slot {slot}")
    elif kind == "waiver_claim":
        add, drop = action["add_id"], action["drop_id"]
        if add not in state.available_ids:
            problems.append(f"player {add} is no longer available")
        if drop not in state.roster_ids:
            problems.append(f"player {drop} no longer rostered")
        if drop in state.protected_ids or drop in state.ir_ids or drop in state.locked_ids:
            problems.append(f"player {drop} cannot be dropped")
    else:
        problems.append(f"unknown action {kind}")
    return problems


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------

@dataclass
class Outcome:
    case: Case
    status: str
    model: str | None = None
    verdict: Verdict | None = None
    problems: list[str] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    elapsed_s: float = 0.0

    @property
    def applied(self) -> bool:
        return self.status == "applied"

    def trace(self) -> dict[str, Any]:
        v = self.verdict
        return {
            "engine_version": ENGINE_VERSION, "case_id": self.case.case_id, "status": self.status,
            "model": self.model, "default_option": self.case.default_option,
            "verdict": None if v is None else {
                "verdict": v.verdict, "choice": v.choice, "confidence": v.confidence,
                "alternatives": [{"option": o, "why_not": w} for o, w in v.alternatives],
                "rationale": v.rationale, "citations": list(v.citations),
            },
            "problems": self.problems, "tool_calls": len(self.tool_calls),
            "cost_usd": round(self.cost_usd, 4), "elapsed_s": round(self.elapsed_s, 3),
        }


def _players_moved(action: Mapping[str, Any]) -> set[int]:
    return {action[k] for k in ("start_id", "bench_id", "add_id", "drop_id") if k in action}


def _by_deadline(case: Case) -> tuple[bool, float]:
    return (case.deadline is None, case.deadline.timestamp() if case.deadline else 0.0)


def review(
    cases: Sequence[Case],
    model: ReasoningModel | None,
    state: CurrentState,
    budget: Budget = DEFAULT_BUDGET,
    clock: Callable[[], float] = time.monotonic,
) -> list[Outcome]:
    """Run each case through the model once, soonest deadline first, within budget."""
    ordered = sorted(cases, key=_by_deadline)
    if model is None:
        return [Outcome(c, "disabled") for c in ordered]
    outcomes: list[Outcome] = []
    committed: set[int] = set()     # players moved by an earlier applied choice
    down = False                    # an error or timeout stops further calls this run
    started = clock()
    spent = 0.0
    calls = 0
    for case in ordered:
        outcome = Outcome(case, "pending", model=model.name)
        outcomes.append(outcome)
        if case.deadline is not None and state.now >= case.deadline:
            outcome.status = "expired"
            continue
        if down:
            outcome.status = "model_unavailable"
            continue
        brief = case.brief()
        remaining = budget.total_timeout_s - (clock() - started)
        try:
            estimate = float(model.estimate_cost(brief))
        except Exception as exc:  # any model failure fails closed
            outcome.status, outcome.problems, down = "error", [type(exc).__name__], True
            continue
        if calls >= budget.max_calls or spent + estimate > budget.max_cost_usd or remaining <= 0:
            outcome.status = "budget_exhausted"
            continue
        calls += 1
        spent += estimate
        outcome.cost_usd = estimate
        tools = CaseTools(case, budget.max_tool_calls)
        t0 = clock()
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(model.decide, brief, tools)
        try:
            raw = future.result(timeout=min(budget.call_timeout_s, remaining))
        except FutureTimeout:
            outcome.status, down = "timeout", True
            continue
        except ToolBudgetExceeded:
            outcome.status = "tool_budget_exhausted"
            continue
        except Exception as exc:  # any model failure fails closed
            outcome.status, outcome.problems, down = "error", [type(exc).__name__], True
            continue
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
            outcome.elapsed_s = clock() - t0
            outcome.tool_calls = list(tools.calls)
        try:
            verdict = parse_verdict(raw, case, set(tools.read))
        except InvalidOutput as exc:
            outcome.status, outcome.problems = "invalid_output", [str(exc)]
            continue
        outcome.verdict = verdict
        if verdict.verdict == "abstain":
            outcome.status = "abstained"
        elif CONFIDENCES.index(verdict.confidence) < CONFIDENCES.index(MIN_APPLY_CONFIDENCE):
            outcome.status = "low_confidence"
        elif verdict.choice == case.default_option:
            outcome.status = "kept_default"
        else:
            action = case.option(verdict.choice).action
            elapsed = timedelta(seconds=clock() - started)
            outcome.problems = revalidate(case, case.option(verdict.choice),
                                          replace(state, now=state.now + elapsed))
            moved = _players_moved(action)
            if moved & committed:
                outcome.problems.append("conflicts with an earlier reviewed move")
            outcome.status = "rejected_revalidation" if outcome.problems else "applied"
            if outcome.applied:
                committed |= moved
    return outcomes


def _apply_swap(payload: dict[str, Any], case: Case, action: Mapping[str, Any]) -> None:
    """Rewrite the stored lineup so assignment and moves describe the reviewed choice."""
    new_slots = {action["start_id"]: SLOT_NAMES.get(action["slot"], str(action["slot"])),
                 action["bench_id"]: SLOT_NAMES[BENCH_SLOT]}
    moves = [m for m in payload.get("moves", []) if m["player_id"] not in new_slots]
    for pid, slot in new_slots.items():
        payload["assignment"][str(pid)] = slot
        facts = case.evidence[f"player:{pid}"]
        if facts["slot"] != slot:
            moves.append({"player_id": pid, "name": facts["name"], "from": facts["slot"], "to": slot})
    payload["moves"] = moves


def apply(
    records: Sequence[Mapping[str, Any]],
    evidence: Sequence[Sequence[Mapping[str, Any]]],
    outcomes: Sequence[Outcome],
) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    """Annotate engine records with reasoning traces; promote only applied choices.

    Records keep their deterministic decision unless an outcome applied, so a
    disabled or failing model leaves the engine's output unchanged apart from
    the trace. A promoted waiver option replaces the kind's NO_ACTION record.
    """
    out = [copy.deepcopy(dict(r)) for r in records]
    ev = [[dict(e) for e in group] for group in evidence]
    promoted_kinds = set()
    for o in outcomes:
        rec = out[o.case.record_index]
        payload = rec.setdefault("payload", {})
        payload.setdefault("reasoning", []).append(o.trace())
        if not o.applied or o.verdict is None:
            continue
        chosen = o.case.option(o.verdict.choice)
        if DECISION_RANK.get(rec["decision"], 0) < DECISION_RANK["notify"]:
            rec["decision"] = "notify"
        rec["confidence"] = o.verdict.confidence
        rec["summary"] = f"{rec['summary']} Close call reviewed: {chosen.label}."
        payload.setdefault("reasoned_actions", []).append(dict(chosen.action))
        if chosen.action["type"] == "lineup_swap" and "assignment" in payload:
            _apply_swap(payload, o.case, chosen.action)
        ev[o.case.record_index].extend(
            {"kind": "reasoning_citation", "key": key, "value": o.case.evidence[key],
             "source": ENGINE_VERSION}
            for key in o.verdict.citations
        )
        promoted_kinds.add(rec["kind"])
    keep = [i for i, r in enumerate(out)
            if not (r["decision"] == "no_action" and r["kind"] == "waiver" and "waiver" in promoted_kinds)]
    return [out[i] for i in keep], [ev[i] for i in keep]


def load_model(environ: Mapping[str, str] = os.environ) -> ReasoningModel | None:
    """The configured model, or None while reasoning is off (the default).

    No live provider is wired in yet: paid calls need an approved account and
    budget. Turning reasoning on without one is a configuration error rather
    than a silent fallback; `reasoning_offline.OfflineModel` stands in for
    dry runs and tests.
    """
    if environ.get("FF_REASONING_ENABLED", "").strip().lower() not in ("1", "true", "yes"):
        return None
    raise ConfigurationError(
        "FF_REASONING_ENABLED is set but no reasoning provider is configured"
    )
