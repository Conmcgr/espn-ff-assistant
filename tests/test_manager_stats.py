"""Unit tests for manager_stats — synthetic data, no database required."""

from __future__ import annotations

from espn_ff_assistant.manager_stats import _confidence, _feature


def test_confidence_thresholds():
    assert _confidence(None) == "insufficient"
    assert _confidence(0) == "insufficient"
    assert _confidence(1) == "low"
    assert _confidence(4) == "low"
    assert _confidence(5) == "medium"
    assert _confidence(19) == "medium"
    assert _confidence(20) == "high"
    assert _confidence(100) == "high"


def test_feature_shape():
    f = _feature(
        manager_id="mgr-1",
        league_id="lg-1",
        stat_name="waiver_claims_total",
        value=7.0,
        sample_size=7,
        season_from=2022,
        season_to=2024,
        as_of_season=2024,
        as_of_week=10,
    )
    assert f["stat_name"] == "waiver_claims_total"
    assert f["value"] == 7.0
    assert f["sample_size"] == 7
    assert f["confidence"] == "medium"
    assert f["version"] == 1
    assert f["as_of_season"] == 2024
    assert f["as_of_week"] == 10


def test_feature_confidence_propagated():
    """_feature should derive confidence from sample_size automatically."""
    f = _feature("m", "l", "stat", 1.0, 25, 2020, 2024, 2024, 1)
    assert f["confidence"] == "high"

    f2 = _feature("m", "l", "stat", 1.0, 3, 2020, 2024, 2024, 1)
    assert f2["confidence"] == "low"

    f3 = _feature("m", "l", "stat", None, 0, None, None, 2024, 1)
    assert f3["confidence"] == "insufficient"
