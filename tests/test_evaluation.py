"""Unit tests for outcome scoring — synthetic data only."""

from __future__ import annotations

from espn_ff_assistant.evaluation import _quantile, lineup_outcome, waiver_outcome

BENCH = 20


def test_lineup_followed():
    rec = {1: 0, 2: 2, 3: BENCH}
    out = lineup_outcome(rec, {1: 0, 2: BENCH, 3: 2}, rec, {1: 20.0, 2: 15.0, 3: 5.0})
    assert out["observed_action"] == "followed"
    assert out["recommended_points"] == 35.0
    assert out["baseline_points"] == 25.0
    assert out["detail"]["recommended_minus_actual"] == 0


def test_lineup_not_followed_and_modified():
    rec = {1: 0, 2: 2, 3: BENCH}
    base = {1: 0, 2: BENCH, 3: 2}
    actuals = {1: 20.0, 2: 15.0, 3: 5.0, 4: 9.0}
    assert lineup_outcome(rec, base, base, actuals)["observed_action"] == "not_followed"
    modified = lineup_outcome(rec, base, {1: 0, 2: BENCH, 3: BENCH, 4: 2}, actuals)
    assert modified["observed_action"] == "modified"
    assert modified["actual_points"] == 29.0


def test_waiver_outcome_window():
    weeks = [{10: 12.0, 11: 3.0}, {10: 8.0}]
    out = waiver_outcome(10, 11, {10, 99}, weeks)
    assert out["observed_action"] == "followed"
    assert out["recommended_points"] == 20.0
    assert out["baseline_points"] == 3.0
    assert out["detail"] == {"weeks": 2, "add_minus_drop": 17.0}
    assert waiver_outcome(10, None, set(), weeks)["observed_action"] == "not_followed"


def test_quantile_interpolates():
    assert _quantile([4, 6, 8, 10, 12], 0.25) == 6
    assert _quantile([1, 2], 0.5) == 1.5
