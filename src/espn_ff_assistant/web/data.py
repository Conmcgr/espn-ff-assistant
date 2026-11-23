"""Read models for the dashboard; no ESPN requests, persistence, LLMs or delivery."""

from __future__ import annotations

import os
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import psycopg

from espn_ff_assistant import engine, lineup
from espn_ff_assistant.database import connection
from espn_ff_assistant.manager_stats import POSITION_NAMES
from espn_ff_assistant.repository import Repository
from espn_ff_assistant.rival_signals import compute_rival_signals
from espn_ff_assistant.trade_discovery import (
    DiscoveryContext,
    PartnerSignals,
    RivalTeam,
    discover,
)


def lineup_view(ctx: engine._Context) -> dict[str, Any]:
    result = engine.run_lineup(ctx)
    rec = result.detail
    rows = []
    for player in result.players:
        p = player.lineup
        value = lineup.valuation(p)
        slot = rec.assignment.get(player.pid, lineup.BENCH_SLOT)
        rows.append({
            "name": p.name, "position": player.position,
            "current_slot": lineup.SLOT_NAMES.get(p.current_slot, str(p.current_slot)),
            "slot": lineup.SLOT_NAMES.get(slot, str(slot)), "slot_id": slot,
            "projection": p.projection, "effective_projection": value.value,
            "locked": p.locked, "injury": p.injury_status, "bye": p.on_bye,
            "flags": list(value.flags), "changed": p.current_slot != slot,
        })
    rows.sort(key=lambda r: (r["slot_id"] in lineup.NON_STARTING_SLOTS, r["slot_id"], r["name"]))
    return {
        "summary": result.records[0]["summary"], "decision": rec.decision,
        "current_points": rec.current_points, "optimal_points": rec.optimal_points,
        "gain": rec.gain, "rows": rows,
        "missing_projections": sum(p.lineup.projection is None for p in result.players),
        "close_calls": result.records[0]["payload"]["close_calls"],
        "unfilled_slots": [lineup.SLOT_NAMES.get(s, str(s)) for s in rec.unfilled_slots],
    }


def waivers_view(ctx: engine._Context) -> dict[str, Any]:
    result = engine.run_waivers(ctx)
    return {
        "picks": [r for r in result.records if r["decision"] in {"notify", "urgent"}],
        "other": [r for r in result.records if r["decision"] == "suppressed"],
        "no_action": next((r["summary"] for r in result.records
                           if r["decision"] == "no_action"), None),
    }


def trades_view(ctx: engine._Context) -> dict[str, Any]:
    settings = ctx.settings
    deadline_ms = (settings.get("tradeSettings") or {}).get("deadlineDate")
    deadline = datetime.fromtimestamp(deadline_ms / 1000, UTC) if deadline_ms else None
    base = {"deadline": deadline, "ideas": [], "signals": [], "missing_teams": []}
    if deadline is None:
        return {**base, "notice": "Trade deadline is unavailable. Ideas are withheld until it can be verified."}
    if ctx.as_of_ts >= deadline:
        return {**base, "notice": "Your league's trade deadline has passed. No actionable trades are shown."}
    repo = ctx.repo
    teams = {t.provider_team_id: t.name or f"Team {t.provider_team_id}"
             for t in repo.teams(ctx.league_id, ctx.season)}
    user, names = engine.team_players(ctx, ctx.team_id)
    rivals = []
    for team_id, name in teams.items():
        if team_id == ctx.team_id:
            continue
        try:
            roster, player_names = engine.team_players(ctx, team_id)
        except engine.EngineError:
            base["missing_teams"].append(name)
            continue
        names.update(player_names)
        rivals.append(RivalTeam(team_id, roster, name))
    pool, _ = engine.pool_players(ctx)
    measured = compute_rival_signals(repo, ctx.league_id, ctx.season, ctx.period)
    grouped = {}
    for signal in measured:
        grouped.setdefault(signal.provider_team_id, {})[signal.name] = signal
    partner_signals = {}
    for team_id, signals in grouped.items():
        accept = signals.get("trade_received_accept_rate_linked")
        packages = signals.get("trade_one_for_one_share")
        back = signals.get("games_back_of_playoff_line")
        partner_signals[team_id] = PartnerSignals(
            accept_rate=accept.value if accept else None,
            accept_rate_basis=accept.basis if accept else "neutral",
            one_for_one_share=packages.value if packages else None,
            games_back_of_playoff_line=back.value if back else None,
            position_depth={k.removeprefix("position_depth_"): v.value
                            for k, v in signals.items() if k.startswith("position_depth_")},
        )
    roster_settings = settings.get("rosterSettings") or {}
    limits = {POSITION_NAMES[int(k)]: int(v)
              for k, v in (roster_settings.get("positionLimits") or {}).items()
              if int(k) in POSITION_NAMES and int(v) > 0}
    prefs = repo.preferences(ctx.user_id, ctx.league_id)
    protected = frozenset(int(k.split(":", 1)[1]) for k, v in prefs.items()
                          if k.startswith("protected:") and v.value)
    discovery = DiscoveryContext(
        slot_counts=ctx.slot_counts, user_team=RivalTeam(ctx.team_id, user, teams[ctx.team_id]),
        rivals=rivals, free_agents=pool, remaining_weeks=max(1, ctx.final_period - ctx.period + 1),
        current_week=ctx.period,
        # The actual ESPN timestamp is checked above. Do not invent a deadline week.
        trade_deadline_week=ctx.final_period,
        roster_size_limit=sum(int(v) for v in roster_settings["lineupSlotCounts"].values()),
        position_limits=limits, protected_ids=protected, signals=partner_signals,
    )
    ideas = discover(discovery)
    # Give each rival a place in the short list, rather than showing 15 near-duplicates.
    chosen = []
    seen = set()
    for idea in ideas:
        if idea.partner_team in seen:
            continue
        seen.add(idea.partner_team)
        chosen.append({
            "partner": teams[idea.partner_team],
            "give": [names.get(pid, str(pid)) for pid in idea.offer.give_ids],
            "receive": [names.get(pid, str(pid)) for pid in idea.offer.receive_ids],
            "user_gain": idea.grade.user.delta_ros,
            "partner_gain": idea.grade.partner.delta_ros,
            "net_gain": idea.quality, "fit": idea.plausibility.label,
            "why": [s for s in idea.why_now if not s.startswith("trade deadline")],
            "notes": idea.notes,
            "user_drops": [names.get(pid, str(pid)) for pid in
                           (*idea.offer.user_drops, *idea.grade.user.assumed_drops)],
            "partner_drops": [names.get(pid, str(pid)) for pid in idea.offer.partner_drops],
            "evidence": [asdict(s) for s in measured if s.provider_team_id == idea.partner_team],
        })
    return {**base, "ideas": chosen, "notice": None if chosen else
            "No legal trade clears the model's gain and uncertainty thresholds with current locks and projections."}


def load_section(section: str) -> dict[str, Any]:
    """Each calculation uses one consistent, database-enforced read-only transaction."""
    season = int(os.environ.get("DEMO_SEASON", datetime.now(UTC).year))
    provider_id = int(os.environ["DEMO_LEAGUE_ID"])
    with connection() as conn:
        conn.read_only = True
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        repo = Repository(conn)
        user_id = repo.default_user_id()
        league_id = repo.league_id(provider_id, user_id=user_id)
        if not user_id or not league_id:
            raise engine.EngineError("Demo league/user is not configured.")
        now = datetime.now(UTC)
        snapshot = repo.demo_snapshot(league_id, season, now)
        if snapshot is None:
            raise engine.EngineError("No completed live snapshot is available.")
        ctx = engine.context(repo, league_id, user_id, season, snapshot["week"], now)
        team = next((t.name for t in repo.teams(league_id, season)
                     if t.provider_team_id == ctx.team_id), None)
        loaders = {"lineup": lineup_view, "waivers": waivers_view, "trades": trades_view}
        return {
            "season": season, **snapshot, "team": team or "Your team",
            "calculated_at": now, "data": loaders[section](ctx),
        }
