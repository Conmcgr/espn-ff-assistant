"""Offline stand-in for a reasoning model: no network, no cost, scripted modes.

Used by tests and fixture dry runs to exercise the reasoning seam end to end.
`heuristic` reads evidence through the tools like a real model would and
answers from simple rules; the other modes reproduce the failures the pass
must survive (abstaining, raising, hanging, citing unread evidence).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from .reasoning import CaseTools

MODES = ("heuristic", "abstain", "error", "slow", "ungrounded", "off_menu")
TREND_MIN = 5.0


class OfflineModel:
    def __init__(self, mode: str = "heuristic", cost_usd: float = 0.01, delay_s: float = 5.0):
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        self.mode = mode
        self.name = f"offline-{mode}"
        self.cost_usd = cost_usd
        self.delay_s = delay_s

    def estimate_cost(self, brief: Mapping[str, Any]) -> float:
        return self.cost_usd

    def decide(self, brief: Mapping[str, Any], tools: CaseTools) -> Mapping[str, Any]:
        if self.mode == "error":
            raise RuntimeError("model unavailable")
        if self.mode == "slow":
            time.sleep(self.delay_s)
        keys = tools.list_evidence()
        facts = {k: tools.evidence(k) for k in keys}
        if self.mode == "abstain":
            return _abstain("Not enough signal to overrule the deterministic call.", keys)
        if self.mode == "ungrounded":
            return {**_answer(brief, brief["default_option"], "medium", "Cites nothing it read.", keys),
                    "citations": ["news:unread"]}
        if self.mode == "off_menu":
            return _answer(brief, "trade_for_him", "high", "Not an offered option.", keys)
        if brief["kind"] == "lineup":
            return _lineup(brief, facts)
        return _waiver(brief, facts)


def _answer(brief: Mapping[str, Any], choice: str, confidence: str, why: str,
            citations: list[str]) -> dict[str, Any]:
    return {
        "verdict": "recommend", "choice": choice, "confidence": confidence, "rationale": why,
        "alternatives": [{"option": o["id"], "why_not": "Weaker on the cited evidence."}
                         for o in brief["options"] if o["id"] != choice],
        "citations": citations,
    }


def _abstain(why: str, citations: list[str]) -> dict[str, Any]:
    return {"verdict": "abstain", "choice": None, "confidence": "low", "rationale": why,
            "alternatives": [], "citations": citations}


def _players(facts: Mapping[str, Mapping[str, Any]]) -> list[tuple[str, Mapping[str, Any]]]:
    return [(k, v) for k, v in facts.items() if k.startswith("player:")]


def _lineup(brief: Mapping[str, Any], facts: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    keep = next(o for o in brief["options"] if o["id"] == "keep")
    players = _players(facts)
    starter = next((k, v) for k, v in players if keep["label"].startswith(f"Start {v['name']} "))
    alt = next((k, v) for k, v in players if k != starter[0])
    risky = any(f.startswith("injury_risk") for f in starter[1]["flags"])
    alt_risky = any(f.startswith("injury_risk") for f in alt[1]["flags"])
    cited = ["margin", starter[0], alt[0]]
    if risky and not alt_risky:
        return _answer(brief, "swap", "medium",
                       f"{starter[1]['name']} is {starter[1]['injury_status']} and the margin is "
                       f"{facts['margin']['margin']}; {alt[1]['name']} carries no injury risk.", cited)
    return _answer(brief, "keep", "medium",
                   f"No injury difference; keep the higher projection ({starter[1]['name']}).", cited)


def _waiver(brief: Mapping[str, Any], facts: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    claim = next(o for o in brief["options"] if o["id"] == "claim")
    add = next((k, v) for k, v in _players(facts) if claim["label"].startswith(f"Add {v['name']},"))
    trend = add[1].get("percent_change") or 0.0
    cited = ["deltas", add[0]]
    if trend >= TREND_MIN:
        return _answer(brief, "claim", "medium",
                       f"{add[1]['name']} is {facts['deltas']['delta_ros']:+.1f} ROS, just under the bar, "
                       f"and rostered share is rising {trend:+.1f} points; claim before rivals react.", cited)
    return _abstain(f"{add[1]['name']} is just under the bar with no rising interest; pass.", cited)
