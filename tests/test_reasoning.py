from __future__ import annotations

import runpy
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from espn_ff_assistant import engine, lineup, reasoning
from espn_ff_assistant.config import ConfigurationError
from espn_ff_assistant.lineup import BENCH_SLOT, LineupPlayer
from espn_ff_assistant.reasoning import Budget, CaseTools, CurrentState, InvalidOutput
from espn_ff_assistant.reasoning_offline import OfflineModel
from espn_ff_assistant.repository import PlayerStatus, ProGame, RosterEntry
from espn_ff_assistant.waivers import PoolPlayer, WaiverOption

NOW = datetime(2026, 10, 4, 16, 30, tzinfo=UTC)
KICKOFF = NOW + timedelta(minutes=30)
WR = frozenset({4, 23, 20})
SLOTS = {4: 2, 20: 3}


def lp(pid, proj, slot=BENCH_SLOT, injury=None, name=None, locked=False):
    return LineupPlayer(pid, name or f"P{pid}", WR, proj, injury, current_slot=slot, locked=locked)


def roster():
    return [lp(1, 16.0, 4, "QUESTIONABLE"), lp(2, 20.0, 4), lp(3, 12.5), lp(4, 5.0)]


def state(**overrides):
    base = CurrentState(now=NOW, roster_ids=frozenset({1, 2, 3, 4}), available_ids=frozenset({9}),
                        eligible_slots={pid: WR for pid in (1, 2, 3, 4)})
    return replace(base, **overrides)


def lineup_case():
    players = roster()
    rec = lineup.optimize(players, SLOTS)
    (case,) = reasoning.lineup_cases(rec, players, {pid: KICKOFF for pid in range(1, 5)})
    return case


def option(**kw):
    base = dict(add_id=9, drop_id=4, position="WR", kind="upgrade", availability="free_agent",
                delta_next=0.0, delta_ros=8.0, score=8.0, decision="suppressed")
    return WaiverOption(**{**base, **kw})


def pool_player(pid, ros, change=None, availability="rostered"):
    return PoolPlayer(lp(pid, 6.0), "WR", ros, 10, availability, 20.0, change)


def waiver_case(change=7.0):
    players = {9: pool_player(9, 128.0, change, "free_agent"), 4: pool_player(4, 30.0)}
    (case,) = reasoning.waiver_cases([option()], players, {9: KICKOFF})
    return case


class Scripted:
    """Fake model returning a fixed verdict after reading the evidence it cites."""

    name = "scripted"

    def __init__(self, answer, read=None, cost=0.01):
        self.answer, self.read, self.cost, self.calls = answer, read, cost, 0

    def estimate_cost(self, brief):
        return self.cost

    def decide(self, brief, tools: CaseTools):
        self.calls += 1
        for key in self.read if self.read is not None else tools.list_evidence():
            tools.evidence(key)
        return self.answer(brief) if callable(self.answer) else self.answer


def recommend(choice, confidence="medium", citations=None):
    def answer(brief):
        return {"verdict": "recommend", "choice": choice, "confidence": confidence,
                "rationale": "Grounded in the margin.", "citations": citations or ["margin"],
                "alternatives": [{"option": o["id"], "why_not": "weaker"}
                                 for o in brief["options"] if o["id"] != choice]}
    return answer


# --- routing ---------------------------------------------------------------

def test_only_close_lineup_calls_become_cases():
    case = lineup_case()
    assert case.case_id == "lineup:4:1:3"
    assert [o.option_id for o in case.options] == ["keep", "swap"]
    assert case.default_option == "keep" and case.deadline == KICKOFF


def test_clear_lineup_produces_no_case():
    players = [lp(1, 20.0, 4), lp(2, 18.0, 4), lp(3, 9.0)]
    rec = lineup.optimize(players, SLOTS)
    assert rec.close_calls == []
    assert reasoning.lineup_cases(rec, players, {}) == []


@pytest.mark.parametrize(("opt", "route"), [
    (option(decision="notify", delta_ros=12.0), "deterministic: clears the notification bar"),
    (option(delta_ros=8.0), "reasoning: just short of the notification bar"),
    (option(delta_next=2.4, delta_ros=1.0), "reasoning: just short of the notification bar"),
    (option(delta_ros=8.0, reasons=["conflicts_with_higher_ranked"]),
     "deterministic: conflicts with a higher-ranked move"),
    (option(delta_ros=12.0, reasons=["conflicts_with_higher_ranked"]),
     "deterministic: conflicts with a higher-ranked move"),
    (option(delta_ros=5.0), "deterministic: well short of the notification bar"),
])
def test_waiver_routing(opt, route):
    assert reasoning.waiver_route(opt) == route
    players = {9: pool_player(9, 100.0), 4: pool_player(4, 30.0)}
    assert bool(reasoning.waiver_cases([opt], players, {})) == route.startswith("reasoning")


def test_brief_and_tools_expose_only_case_evidence():
    case = waiver_case()
    assert set(case.brief()) == {"case_id", "kind", "question", "options", "default_option",
                                 "evidence_keys", "deadline"}
    tools = CaseTools(case, max_calls=10)
    assert tools.list_evidence() == ["deltas", "player:4", "player:9"]
    facts = tools.evidence("player:9")
    facts["name"] = "mutated"
    assert case.evidence["player:9"]["name"] == "P9"
    with pytest.raises(KeyError):
        tools.evidence("raw_payload")
    assert tools.read == {"player:9"}


# --- disabled / model down -------------------------------------------------

def test_reasoning_is_off_by_default(monkeypatch):
    monkeypatch.delenv("FF_REASONING_ENABLED", raising=False)
    assert reasoning.load_model() is None
    with pytest.raises(ConfigurationError):
        reasoning.load_model({"FF_REASONING_ENABLED": "true"})


def test_disabled_pass_keeps_deterministic_records():
    case = lineup_case()
    (outcome,) = reasoning.review([case], None, state())
    assert outcome.status == "disabled"
    records = [{"kind": "lineup", "decision": "info", "summary": "Lineup.", "payload": {}}]
    out, ev = reasoning.apply(records, [[]], [outcome])
    assert out[0]["decision"] == "info" and out[0]["summary"] == "Lineup."
    assert out[0]["payload"]["reasoning"][0]["status"] == "disabled"
    assert "reasoned_actions" not in out[0]["payload"] and ev == [[]]
    assert records[0]["payload"] == {}


def test_model_error_fails_closed_and_stops_calling():
    model = OfflineModel("error")
    outcomes = reasoning.review([lineup_case(), waiver_case()], model, state())
    assert [o.status for o in outcomes] == ["error", "model_unavailable"]
    assert not any(o.applied for o in outcomes)


def test_timeout_fails_closed():
    model = OfflineModel("slow", delay_s=0.5)
    (outcome,) = reasoning.review([lineup_case()], model, state(), Budget(call_timeout_s=0.05))
    assert outcome.status == "timeout" and not outcome.applied


# --- structured output -----------------------------------------------------

@pytest.mark.parametrize(("raw", "message"), [
    ("swap", "not an object"),
    ({"verdict": "maybe"}, "verdict"),
    ({"verdict": "recommend", "confidence": "certain"}, "confidence"),
    ({"verdict": "recommend", "confidence": "high", "rationale": ""}, "rationale"),
    ({"verdict": "recommend", "confidence": "high", "rationale": "x", "citations": []}, "citations"),
    ({"verdict": "recommend", "confidence": "high", "rationale": "x", "citations": ["news"]}, "did not read"),
    ({"verdict": "recommend", "confidence": "high", "rationale": "x", "citations": ["margin"],
      "choice": "bench_everyone"}, "offered options"),
    ({"verdict": "recommend", "confidence": "high", "rationale": "x", "citations": ["margin"],
      "choice": "swap", "alternatives": []}, "every other option"),
    ({"verdict": "abstain", "confidence": "low", "rationale": "x", "citations": ["margin"],
      "choice": "swap"}, "no choice"),
])
def test_parse_verdict_rejects_malformed_or_ungrounded(raw, message):
    with pytest.raises(InvalidOutput, match=message):
        reasoning.parse_verdict(raw, lineup_case(), {"margin"})


@pytest.mark.parametrize("mode", ["ungrounded", "off_menu"])
def test_invalid_output_is_never_applied(mode):
    (outcome,) = reasoning.review([lineup_case()], OfflineModel(mode), state())
    assert outcome.status == "invalid_output" and not outcome.applied


def test_abstain_and_low_confidence_keep_default():
    case = lineup_case()
    (abstained,) = reasoning.review([case], OfflineModel("abstain"), state())
    (low,) = reasoning.review([case], Scripted(recommend("swap", "low")), state())
    assert (abstained.status, low.status) == ("abstained", "low_confidence")


# --- revalidation ----------------------------------------------------------

def test_confident_swap_is_applied_after_revalidation():
    (outcome,) = reasoning.review([lineup_case()], OfflineModel(), state())
    assert outcome.status == "applied"
    assert outcome.verdict.choice == "swap"
    assert set(outcome.verdict.citations) <= set(outcome.case.evidence)


@pytest.mark.parametrize(("current", "problem"), [
    (state(now=KICKOFF), "deadline passed"),
    (state(locked_ids=frozenset({3})), "player 3 is locked"),
    (state(unavailable_ids=frozenset({3})), "no longer startable"),
    (state(roster_ids=frozenset({1, 2, 4})), "player 3 no longer rostered"),
    (state(eligible_slots={3: frozenset({20})}), "not eligible at slot 4"),
])
def test_lineup_swap_revalidation(current, problem):
    case = lineup_case()
    assert any(problem in p for p in reasoning.revalidate(case, case.option("swap"), current))


def test_case_past_deadline_is_not_sent():
    model = Scripted(recommend("swap"))
    (outcome,) = reasoning.review([lineup_case()], model, state(now=KICKOFF))
    assert outcome.status == "expired" and model.calls == 0


@pytest.mark.parametrize(("current", "problem"), [
    (state(available_ids=frozenset()), "no longer available"),
    (state(roster_ids=frozenset({1, 2, 3})), "player 4 no longer rostered"),
    (state(protected_ids=frozenset({4})), "cannot be dropped"),
    (state(ir_ids=frozenset({4})), "cannot be dropped"),
])
def test_waiver_claim_revalidation(current, problem):
    case = waiver_case()
    assert any(problem in p for p in reasoning.revalidate(case, case.option("claim"), current))


def test_unknown_deadline_fails_closed():
    case = replace(waiver_case(), deadline=None)
    assert "deadline unknown" in reasoning.revalidate(case, case.option("claim"), state())


def test_second_move_reusing_a_player_is_rejected():
    first, second = waiver_case(), replace(waiver_case(), case_id="waiver:8:4")
    second = replace(second, options=(second.options[0], replace(
        second.options[1], action={"type": "waiver_claim", "add_id": 9, "drop_id": 4})))
    outcomes = reasoning.review([first, second], OfflineModel(), state())
    assert [o.status for o in outcomes] == ["applied", "rejected_revalidation"]
    assert "conflicts with an earlier reviewed move" in outcomes[1].problems


# --- caps ------------------------------------------------------------------

def test_call_and_cost_caps():
    cases = [replace(lineup_case(), case_id=f"c{i}") for i in range(3)]
    by_calls = reasoning.review(cases, OfflineModel(), state(), Budget(max_calls=2))
    by_cost = reasoning.review(cases, OfflineModel(cost_usd=0.1), state(), Budget(max_cost_usd=0.15))
    assert [o.status for o in by_calls][-1] == "budget_exhausted"
    assert [o.status for o in by_cost] == ["applied", "budget_exhausted", "budget_exhausted"]


def test_tool_call_cap():
    (outcome,) = reasoning.review([lineup_case()], OfflineModel(), state(), Budget(max_tool_calls=2))
    assert outcome.status == "tool_budget_exhausted"


def test_total_time_cap():
    ticks = iter([0.0, 100.0, 100.0, 100.0])
    (outcome,) = reasoning.review([lineup_case()], OfflineModel(), state(), Budget(total_timeout_s=60),
                                  clock=lambda: next(ticks))
    assert outcome.status == "budget_exhausted"


# --- applying --------------------------------------------------------------

def test_applied_waiver_claim_replaces_no_action():
    (outcome,) = reasoning.review([waiver_case()], OfflineModel(), state())
    assert outcome.status == "applied"
    records = [
        {"kind": "waiver", "decision": "suppressed", "summary": "Add P9.", "payload": {}, "confidence": "low"},
        {"kind": "waiver", "decision": "no_action", "summary": "Nothing.", "payload": {}},
    ]
    out, ev = reasoning.apply(records, [[], []], [outcome])
    assert len(out) == 1 and out[0]["decision"] == "notify" and out[0]["confidence"] == "medium"
    assert out[0]["payload"]["reasoned_actions"] == [{"type": "waiver_claim", "add_id": 9, "drop_id": 4}]
    assert {e["key"] for e in ev[0]} == {"deltas", "player:9"}


def test_flat_interest_near_miss_abstains():
    (outcome,) = reasoning.review([waiver_case(change=0.2)], OfflineModel(), state())
    assert outcome.status == "abstained"


# --- engine wiring ---------------------------------------------------------

class StubRepo:
    """Point-in-time reads for one team; ignores as_of because the data never changes."""

    def __init__(self, entries, statuses, pool):
        self.entries, self.statuses, self.pool = entries, statuses, pool

    def roster_as_of(self, *args):
        return self.entries

    def available_players(self, *args):
        return self.pool

    def player_status(self, league_id, season, period, ids, as_of):
        return {pid: s for pid, s in self.statuses.items() if pid in ids}

    def pro_games(self, season, period):
        return {7: ProGame(7, period, 8, KICKOFF, False)}

    def preferences(self, *args):
        return {}


def status(pid, injury=None, availability="rostered", locked=False):
    return PlayerStatus(pid, 5, injury, sorted(WR), 3, 7, 20.0, 0.0, availability, None, None, locked, NOW)


def test_engine_review_close_calls_wires_state_and_keeps_order():
    entries = [RosterEntry(1, 1, 4, None, None), RosterEntry(2, 1, 4, None, None), RosterEntry(3, 1, 20, None, None)]
    statuses = {1: status(1, "QUESTIONABLE"), 2: status(2), 3: status(3)}
    ctx = engine._Context(StubRepo(entries, statuses, []), "L", "U", 2026, 5, NOW, 1, SLOTS, {}, 14)
    players = roster()[:3]
    rec = lineup.optimize(players, SLOTS)
    lineup_result = engine.EngineResult(
        [{"kind": "lineup", "decision": rec.decision, "summary": "Lineup.", "payload": {}}], [[]], rec,
        [PoolPlayer(p, "WR", 100.0, 10) for p in players],
    )
    (reviewed,), (outcome,) = engine.review_close_calls(ctx, [lineup_result], OfflineModel(), now=NOW)
    assert outcome.case.deadline == KICKOFF and outcome.status == "applied"
    assert reviewed.records[0]["decision"] == "notify"

    statuses[3] = status(3, locked=True)
    _, (outcome,) = engine.review_close_calls(ctx, [lineup_result], OfflineModel(), now=NOW)
    assert outcome.status == "rejected_revalidation" and "player 3 is locked" in outcome.problems

    (unchanged,), (outcome,) = engine.review_close_calls(ctx, [lineup_result], None, now=NOW)
    assert outcome.status == "disabled" and unchanged.records[0]["decision"] == rec.decision


# --- fixture dry run -------------------------------------------------------

def run_demo(monkeypatch, capsys, *args):
    script = Path(__file__).resolve().parents[1] / "scripts" / "reasoning_demo.py"
    monkeypatch.setattr(sys, "argv", [str(script), *args])
    with pytest.raises(SystemExit) as done:
        runpy.run_path(str(script), run_name="__main__")
    assert done.value.code == 0
    return capsys.readouterr().out


def test_fixture_dry_run_routes_and_outcomes(monkeypatch, capsys):
    out = run_demo(monkeypatch, capsys)
    assert "clear QB    Quinn Ardent  (deterministic)" in out
    assert "Hal Vance, drop Abe Quill: +5.0 ROS / +0.0 next  -> deterministic" in out
    for line in ("lineup:17:171:172    expired", "lineup:4:301:304     applied",
                 "lineup:6:601:602     kept_default", "waiver:403:205       abstained",
                 "waiver:402:205       applied", "waiver:405:205       rejected_revalidation"):
        assert line in out


def test_fixture_dry_run_with_model_down(monkeypatch, capsys):
    out = run_demo(monkeypatch, capsys, "--model", "error")
    assert "applied" not in out and "model_unavailable" in out
    assert "final: Start Wes Harrow at WR" in out
