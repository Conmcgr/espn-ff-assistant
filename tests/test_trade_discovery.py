import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from espn_ff_assistant.trade_discovery import (
    OpportunityHint,
    PartnerSignals,
    RivalTeam,
    discover,
    summarize,
)

FIXTURES = Path(__file__).parent / "fixtures"
SPEC = importlib.util.spec_from_file_location(
    "find_trades_cli", Path(__file__).parents[1] / "scripts" / "find_trades.py")
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


def fixture():
    return CLI.load_fixture(json.loads((FIXTURES / "trade_discovery_league.json").read_text()))


def ideas():
    return discover(fixture())


def test_finds_mutual_gain_package_with_why_now():
    result = ideas()
    mutual = [i for i in result if i.partner_team == 2 and i.grade.partner.delta_ros > 0]
    assert mutual
    # Mutual gain through the rival's measured RB hole: partner receives both RBs,
    # consolidates a bench WR and fills from free agents.
    plausible = [i for i in mutual if i.plausibility.label == "plausible"]
    assert plausible
    best = plausible[0]
    assert best.offer.give_ids == (2, 3)
    assert best.offer.receive_ids == (11,)
    assert best.offer.partner_drops == (13,)
    assert best.grade.user.delta_ros == 240
    assert best.grade.partner.delta_ros == 10
    assert best.plausibility.label == "plausible"
    assert any("starter" in r and "13" in r for r in best.why_now)
    assert any("RB" in r for r in best.why_now)  # rival's measured hole
    assert any("deadline" in r for r in best.why_now)


def test_high_quality_but_implausible_is_not_pitched_as_likely():
    result = ideas()
    # User would gain +500 ROS by taking RB32 for RB1, but the partner loses
    # just as much and has no RB need: quality stays high, plausibility stays low.
    lopsided = [i for i in result if i.partner_team == 3
                and i.offer.give_ids == (1,) and i.offer.receive_ids == (32,)]
    assert lopsided
    idea = lopsided[0]
    assert idea.quality >= 100
    assert idea.plausibility.label == "implausible"
    assert idea.plausibility.components["partner_ros_gain"] == 0.0
    assert idea.plausibility.components["need_fit"] == 0.0


def test_only_legal_favorable_packages_surface():
    result = ideas()
    for i in result:
        assert i.grade.grade == "favorable"
        assert i.grade.user.delta_ros > 0
        assert not i.grade.violations


def test_past_deadline_and_thin_rosters_yield_nothing():
    from dataclasses import replace
    ctx = fixture()
    assert discover(replace(ctx, current_week=ctx.trade_deadline_week + 1)) == []
    assert discover(replace(
        ctx,
        user_team=RivalTeam(1, [ctx.user_team.roster[0]]),
        rivals=[RivalTeam(2, [])],
    )) == []


def test_neutral_signals_never_read_as_declined():
    from dataclasses import replace
    ctx = fixture()
    signals = dict(ctx.signals)
    signals[3] = PartnerSignals()  # no measured history at all
    result = [i for i in discover(replace(ctx, signals=signals)) if i.partner_team == 3]
    assert result
    for i in result:
        assert i.plausibility.components["accept_rate"] == 0.5
        assert i.plausibility.components["package_fit"] == 0.5


def test_ideas_sorted_by_quality_and_capped_per_partner():
    result = ideas()
    assert result == sorted(result, key=lambda i: (-i.quality, i.partner_team,
                                                  i.offer.give_ids, i.offer.receive_ids))
    by_partner = {}
    for i in result:
        by_partner[i.partner_team] = by_partner.get(i.partner_team, 0) + 1
    assert all(n <= 15 for n in by_partner.values())


def test_opportunities_for_other_teams_do_not_leak():
    from dataclasses import replace
    ctx = fixture()
    result = discover(replace(
        ctx,
        opportunities=[*ctx.opportunities, OpportunityHint("starter_out", 3, 33, "RB33 out")],
    ))
    team3 = [i for i in result if i.partner_team == 3]
    assert team3 and any("33" in r for r in team3[0].why_now)
    team2 = [i for i in result if i.partner_team == 2]
    assert all("33" not in r for i in team2 for r in i.why_now)


def test_summarize_and_cli_json():
    text = summarize(ideas())
    assert "plausibility" in text and "why now" in text
    script = str(Path(__file__).parents[1] / "scripts" / "find_trades.py")
    result = subprocess.run([sys.executable, script, "--fixture",
                             str(FIXTURES / "trade_discovery_league.json"), "--json"],
                            capture_output=True, text=True, check=True)
    parsed = json.loads(result.stdout)
    assert parsed and parsed[0]["grade"]["grade"] == "favorable"
    assert parsed[0]["plausibility"]["label"] in {"plausible", "mixed", "implausible"}
    error = subprocess.run([sys.executable, script, "--fixture", str(FIXTURES / "missing.json")],
                           capture_output=True, text=True)
    assert error.returncode == 2 and "Invalid fixture" in error.stderr
