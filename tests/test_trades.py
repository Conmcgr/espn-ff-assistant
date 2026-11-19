import importlib.util
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from time import perf_counter

import pytest

from espn_ff_assistant.lineup import IR_SLOT
from espn_ff_assistant.trades import grade_trade

FIXTURES = Path(__file__).parent / "fixtures"
SPEC = importlib.util.spec_from_file_location(
    "grade_trade_cli", Path(__file__).parents[1] / "scripts" / "grade_trade.py")
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


def fixture(name="even_hurts"):
    return CLI.load_fixture(json.loads((FIXTURES / f"trade_{name}.json").read_text()))


def test_even_raw_value_hurts_positional_fit():
    offer, ctx = fixture()
    assert ctx.user_roster[0].ros == ctx.partner_roster[0].ros
    result = grade_trade(offer, ctx)
    assert result.grade == "unfavorable"
    assert result.user.delta_ros == -200
    assert result.partner.delta_ros == -100


def test_more_raw_value_given_but_user_gains():
    offer, ctx = fixture("more_raw_gains")
    assert ctx.user_roster[1].ros > ctx.partner_roster[1].ros
    result = grade_trade(offer, ctx)
    assert result.grade == "favorable"
    assert result.user.delta_ros == 170


@pytest.mark.parametrize("family", ["ownership", "deadline", "roster_size", "position_limit", "locked"])
def test_illegal_fixtures(family):
    offer, ctx = fixture(f"illegal_{family}")
    result = grade_trade(offer, ctx)
    assert result.grade == "illegal"
    assert family in result.violations
    assert result.user is None and result.user_ros_sd is None


@pytest.mark.parametrize("change", ["empty", "duplicate", "overlap", "drop_trade", "foreign_drop"])
def test_ownership_errors(change):
    offer, ctx = fixture()
    changes = {"empty": {"give_ids": ()}, "duplicate": {"give_ids": (1, 1)},
               "overlap": {"receive_ids": (1,)}, "drop_trade": {"user_drops": (1,)},
               "foreign_drop": {"user_drops": (4,)}}
    assert "ownership" in grade_trade(replace(offer, **changes[change]), ctx).violations


def test_deadline_inclusive_and_ir_can_move():
    offer, ctx = fixture()
    ctx.current_week = ctx.trade_deadline_week
    ctx.user_roster[0] = replace(ctx.user_roster[0], lineup=replace(
        ctx.user_roster[0].lineup, current_slot=IR_SLOT))
    result = grade_trade(offer, ctx)
    assert result.grade != "illegal"
    assert result.notes


def test_user_auto_drop_and_protection():
    offer, ctx = fixture()
    offer = replace(offer, receive_ids=(3, 4))
    result = grade_trade(offer, ctx)
    assert result.grade != "illegal"
    assert result.user.assumed_drops == [3]
    ctx.protected_ids = frozenset({2, 3, 4})
    assert "roster_size" in grade_trade(offer, ctx).violations


def test_explicit_drops_and_partner_overflow():
    offer, ctx = fixture()
    offer = replace(offer, give_ids=(1, 2))
    assert "roster_size" in grade_trade(offer, ctx).violations
    result = grade_trade(replace(offer, partner_drops=(4,)), ctx)
    assert result.grade != "illegal"
    assert result.user.after_fills == [5]


def test_fills_are_symmetric_and_alternative_is_separate():
    offer, ctx = fixture()
    ctx.roster_size_limit = 3
    result = grade_trade(offer, ctx)
    assert result.user.before_fills == []
    assert result.user.after_fills == [5]
    assert result.user.delta_ros == -170
    assert result.net_alternative_ros_gain == result.user.delta_ros - result.alternative_ros_gain
    ctx.roster_size_limit = 2
    ctx.free_agents[0] = replace(ctx.free_agents[0], ros=300)
    result = grade_trade(offer, ctx)
    assert result.alternative_ros_gain == 100
    assert result.alternative_add == 5 and result.alternative_drop == 1


def test_uncertainty_does_not_change_expected_gain():
    offer, ctx = fixture("more_raw_gains")
    before = grade_trade(offer, ctx)
    ctx.partner_roster[1] = replace(ctx.partner_roster[1], lineup=replace(
        ctx.partner_roster[1].lineup, injury_status="QUESTIONABLE"))
    after = grade_trade(offer, ctx)
    assert after.user.delta_ros == before.user.delta_ros
    assert after.user_ros_sd > before.user_ros_sd


def test_empty_pool_and_zero_gain_toss_up():
    offer, ctx = fixture()
    ctx.free_agents = []
    ctx.partner_roster[0] = replace(ctx.partner_roster[0], position="RB", lineup=replace(
        ctx.partner_roster[0].lineup, eligible_slots=frozenset({2})))
    result = grade_trade(offer, ctx)
    assert result.grade == "toss-up"
    assert result.user.delta_ros == result.alternative_ros_gain == 0
    assert result.user_ros_sd == pytest.approx((40**2 + 40**2) ** 0.5)


def test_ir_and_locked_players_not_auto_dropped():
    offer, ctx = fixture()
    offer = replace(offer, receive_ids=(3, 4))
    ctx.user_roster[1] = replace(ctx.user_roster[1], lineup=replace(
        ctx.user_roster[1].lineup, locked=True))
    ctx.partner_roster[0] = replace(ctx.partner_roster[0], lineup=replace(
        ctx.partner_roster[0].lineup, current_slot=IR_SLOT))
    assert grade_trade(offer, ctx).user.assumed_drops == [4]


def test_cli_json_and_missing_fixture():
    script = str(Path(__file__).parents[1] / "scripts" / "grade_trade.py")
    result = subprocess.run([sys.executable, script, "--fixture",
                             str(FIXTURES / "trade_even_hurts.json"), "--json"],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["grade"] == "unfavorable"
    error = subprocess.run([sys.executable, script, "--fixture", str(FIXTURES / "missing.json")],
                           capture_output=True, text=True)
    assert error.returncode == 2
    assert "Invalid fixture" in error.stderr


def test_large_pool_fills_quickly_with_best_marginal_players():
    offer, ctx = fixture()
    ctx.roster_size_limit = 4
    ctx.slot_counts = {2: 2, 4: 2}
    template = ctx.free_agents[0]
    ctx.free_agents = [replace(template, ros=10, lineup=replace(
        template.lineup, provider_player_id=100 + i, projection=1)) for i in range(300)]
    ctx.free_agents[-2] = replace(ctx.free_agents[-2], ros=500, lineup=replace(
        ctx.free_agents[-2].lineup, projection=50))
    ctx.free_agents[-1] = replace(ctx.free_agents[-1], ros=600, position="WR", lineup=replace(
        ctx.free_agents[-1].lineup, projection=60, eligible_slots=frozenset({4})))
    start = perf_counter()
    result = grade_trade(offer, ctx)
    elapsed = perf_counter() - start
    assert elapsed < 2.0
    assert result.user.before_fills == [399, 398]
    assert result.user.after_fills == [398, 399]
    assert result.partner.before_fills == [399, 398]
    assert result.partner.after_fills == [399, 398]


def test_greedy_fills_skip_zero_gain_and_respect_position_limits():
    offer, ctx = fixture()
    ctx.roster_size_limit = 4
    ctx.position_limits = {"RB": 1}
    ctx.partner_roster[1] = replace(ctx.partner_roster[1], position="WR", lineup=replace(
        ctx.partner_roster[1].lineup, eligible_slots=frozenset({4})))
    result = grade_trade(offer, ctx)
    assert result.user.before_fills == []
    assert result.user.after_fills == [5]


def test_fixture_loader_keeps_bye_weeks() -> None:
    data = json.loads((FIXTURES / "trade_even_hurts.json").read_text())
    data["context"]["user_roster"][0]["on_bye"] = True
    offer, ctx = CLI.load_fixture(data)
    assert ctx.user_roster[0].lineup.on_bye
