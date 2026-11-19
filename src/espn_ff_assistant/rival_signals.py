"""Measured rival-manager signals for trade and waiver reasoning.

Every signal is a number with provenance, sample size, observation window and
confidence. Sparse team history falls back to the league-wide rate, and to a
neutral (None) value when the league has too little evidence as well. Nothing
here infers personality, and no acceptance probability is derived from
completed trades: response rates only use proposals linked to an observed
response, and unlinked proposals stay unresolved rather than declined.

`sample_size` and `confidence` always describe the team's own evidence, so a
league fallback value still reads as thin for that team.

All inputs are point-in-time: data after (as_of_season, as_of_week) is never read.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass
from typing import Any

from espn_ff_assistant.manager_stats import (
    POSITION_NAMES,
    ManagerScope,
    Window,
    _confidence,
    manager_scopes,
    waiver_features,
)
from espn_ff_assistant.repository import Repository, RosterEntry, TeamRecord, Transaction

MIN_TEAM_SAMPLE = 3
IR_SLOT = 21
# Dedicated starting slot per default position; flex slots are not counted.
POSITION_SLOTS = {"QB": 0, "RB": 2, "WR": 4, "TE": 6, "DST": 16, "K": 17}

OBSERVED = "observed"
LEAGUE_FALLBACK = "league_fallback"
NEUTRAL = "neutral"


@dataclass(frozen=True)
class RivalSignal:
    provider_team_id: int
    name: str
    value: float | None
    sample_size: int
    season_from: int | None
    season_to: int | None
    as_of_season: int
    as_of_week: int
    confidence: str
    basis: str        # observed | league_fallback | neutral
    provenance: str   # which normalized records the value was measured from


@dataclass(frozen=True)
class _Rate:
    hits: int
    n: int


class _Signals:
    def __init__(self, team_id: int, window: Window) -> None:
        self.team_id = team_id
        self.window = window
        self.rows: list[RivalSignal] = []

    def add(
        self,
        name: str,
        value: float | None,
        n: int,
        provenance: str,
        seasons: tuple[int, ...] | None = None,
        basis: str = OBSERVED,
        confidence: str | None = None,
    ) -> None:
        seasons = seasons if seasons is not None else self.window.seasons
        self.rows.append(
            RivalSignal(
                self.team_id, name, value, n,
                min(seasons) if seasons else None, max(seasons) if seasons else None,
                self.window.as_of_season, self.window.as_of_week,
                confidence or _confidence(n), basis, provenance,
            )
        )

    def rate(self, name: str, team: _Rate, league: _Rate, provenance: str) -> None:
        """Team rate when it has enough observations, else league rate, else neutral."""
        if team.n >= MIN_TEAM_SAMPLE:
            self.add(name, team.hits / team.n, team.n, provenance)
        elif league.n >= MIN_TEAM_SAMPLE:
            self.add(name, league.hits / league.n, team.n, provenance, basis=LEAGUE_FALLBACK)
        else:
            self.add(name, None, team.n, provenance, basis=NEUTRAL)


# ---------------------------------------------------------------------------
# Position need (current roster)
# ---------------------------------------------------------------------------

def position_need(
    signals: _Signals,
    roster: list[RosterEntry],
    positions: dict[int, int],
    slot_counts: dict[int, int],
    roster_week: int | None,
) -> None:
    """Healthy rostered players minus dedicated starters per position (negative = hole)."""
    stale = roster_week is None or roster_week < signals.window.as_of_week
    provenance = f"roster_entries@week{roster_week}" if roster_week is not None else "roster_entries:missing"
    season = (signals.window.as_of_season,)
    healthy = Counter(
        POSITION_NAMES.get(positions.get(e.provider_player_id, -1))
        for e in roster
        if e.lineup_slot_id != IR_SLOT
    )
    for pos, slot in POSITION_SLOTS.items():
        starters = slot_counts.get(slot, 0)
        if not starters:
            continue
        if roster_week is None:
            signals.add(f"position_depth_{pos}", None, 0, provenance, season, NEUTRAL)
            continue
        signals.add(
            f"position_depth_{pos}", healthy.get(pos, 0) - starters, 1, provenance, season,
            confidence="low" if stale else "high",
        )


# ---------------------------------------------------------------------------
# Trade proposals, responses, packages and partners
# ---------------------------------------------------------------------------

def _item_key(tx: Transaction) -> frozenset[tuple[Any, Any, Any]]:
    return frozenset(
        (i.get("provider_player_id"), i.get("from_provider_team_id"), i.get("to_provider_team_id"))
        for i in tx.items
        if i.get("item_type") == "TRADE"
    )


def _when(tx: Transaction) -> Any:
    return tx.process_date or tx.proposed_date


def link_trade_responses(txns: list[Transaction]) -> list[tuple[Transaction, Transaction | None]]:
    """Pair each proposal with the first later accept/decline that moves exactly the same players."""
    responses = sorted(
        (t for t in txns if t.category in ("completed_trade", "trade_decline") and _item_key(t)),
        key=lambda t: (_when(t) is None, _when(t)),
    )
    used: set[str] = set()
    pairs: list[tuple[Transaction, Transaction | None]] = []
    for proposal in (t for t in txns if t.category == "trade_proposal"):
        key = _item_key(proposal)
        match = next(
            (
                r for r in responses
                if r.provider_transaction_id not in used
                and r.season == proposal.season
                and _item_key(r) == key
                and (_when(r) is None or _when(proposal) is None or _when(r) >= _when(proposal))
            ),
            None,
        ) if key else None
        if match:
            used.add(match.provider_transaction_id)
        pairs.append((proposal, match))
    return pairs


def _counterparties(tx: Transaction, proposer: int | None) -> set[int]:
    return {
        tid
        for i in tx.items
        for tid in (i.get("from_provider_team_id"), i.get("to_provider_team_id"))
        if tid and tid != proposer
    }


def _received_responses(
    scope: ManagerScope, pairs: list[tuple[Transaction, Transaction | None]]
) -> tuple[_Rate, int]:
    """(accepted / linked responses to proposals this manager received, unresolved count)."""
    accepted = linked = unresolved = 0
    for proposal, response in pairs:
        if proposal.provider_team_id is None:
            continue  # without the proposer the recipient is unknown
        targets = _counterparties(proposal, proposal.provider_team_id)
        if not any(scope.owns(proposal.season, t) for t in targets):
            continue
        if response is None:
            unresolved += 1
            continue
        linked += 1
        accepted += response.category == "completed_trade"
    return _Rate(accepted, linked), unresolved


def trade_signals(
    signals: _Signals,
    scope: ManagerScope,
    txns_by_season: dict[int, list[Transaction]],
    owner_map: dict[tuple[int, int], list[str]],
    positions: dict[int, int],
) -> None:
    seasons = tuple(s for s in signals.window.seasons if s in txns_by_season)
    all_txns = [t for s in seasons for t in txns_by_season[s]]
    pairs = link_trade_responses(all_txns)

    team_rate, unresolved = _received_responses(scope, pairs)
    league_responses = [r for p, r in pairs if r is not None and p.provider_team_id is not None]
    league_rate = _Rate(sum(r.category == "completed_trade" for r in league_responses), len(league_responses))
    provenance = "transactions:trade_proposal->completed_trade|trade_decline (matched items)"
    signals.rate("trade_received_accept_rate_linked", team_rate, league_rate, provenance)
    signals.add("trade_received_unresolved", unresolved, unresolved + team_rate.n, provenance, seasons)

    completed = [
        t for t in all_txns
        if t.category == "completed_trade"
        and any(scope.owns(t.season, tid) for tid in _counterparties(t, None))
    ]
    one_for_one = 0
    acquired: Counter[str] = Counter()
    partners: Counter[str] = Counter()
    for tx in completed:
        own = scope.teams_by_season.get(tx.season, set())
        sent = sum(1 for i in tx.items if i.get("item_type") == "TRADE" and i.get("from_provider_team_id") in own)
        got = [i for i in tx.items if i.get("item_type") == "TRADE" and i.get("to_provider_team_id") in own]
        one_for_one += sent == 1 and len(got) == 1
        for i in got:
            pos = POSITION_NAMES.get(positions.get(i.get("provider_player_id"), -1))
            if pos:
                acquired[pos] += 1
        for tid in _counterparties(tx, None) - own:
            for manager_id in owner_map.get((tx.season, tid), [f"team:{tx.season}:{tid}"]):
                partners[manager_id] += 1

    n = len(completed)
    trade_prov = "transactions:completed_trade items"
    league_completed = [t for t in all_txns if t.category == "completed_trade"]
    league_one_for_one = sum(
        1 for t in league_completed
        if len(_item_key(t)) == 2 and len({k[1] for k in _item_key(t)}) == 2
    )
    signals.rate("trade_one_for_one_share", _Rate(one_for_one, n), _Rate(league_one_for_one, len(league_completed)), trade_prov)
    signals.add(
        "trade_top_partner_share",
        max(partners.values(), default=0) / n if n >= MIN_TEAM_SAMPLE else None,
        n, trade_prov, seasons, OBSERVED if n >= MIN_TEAM_SAMPLE else NEUTRAL,
    )
    for pos in POSITION_SLOTS:
        signals.add(f"trade_acquired_{pos}", acquired.get(pos, 0), n, trade_prov, seasons)


# ---------------------------------------------------------------------------
# FAAB behavior
# ---------------------------------------------------------------------------

def faab_signals(
    signals: _Signals,
    scope: ManagerScope,
    league_scopes: list[ManagerScope],
    txns_by_season: dict[int, list[Transaction]],
    budgets: dict[int, int | None],
    remaining: dict[int, int],
) -> None:
    window = signals.window
    current = (window.as_of_season,)
    if budgets.get(window.as_of_season) is None:
        signals.add("faab_remaining", None, 0, "league_settings:no_faab", current, NEUTRAL)
        return
    left = remaining.get(signals.team_id)
    signals.add(
        "faab_remaining", left, 1 if left is not None else 0,
        "league_settings.acquisitionBudget - executed waiver_claim bids", current,
        OBSERVED if left is not None else NEUTRAL, "high" if left is not None else None,
    )

    def aggression(s: ManagerScope) -> tuple[float | None, int]:
        stats = {f["stat_name"]: f for f in waiver_features(s, window, txns_by_season, budgets, {})}
        row = stats["faab_bid_pct_of_remaining_median"]
        return row["value"], row["sample_size"]

    value, n = aggression(scope)
    provenance = "transactions:waiver_claim EXECUTED bid / remaining budget (median)"
    if n >= MIN_TEAM_SAMPLE:
        signals.add("faab_bid_pct_of_remaining_median", value, n, provenance)
        return
    league = [v for s in league_scopes for v, k in [aggression(s)] if v is not None and k]
    if len(league) >= MIN_TEAM_SAMPLE:
        signals.add("faab_bid_pct_of_remaining_median", statistics.median(league), n, provenance, basis=LEAGUE_FALLBACK)
    else:
        signals.add("faab_bid_pct_of_remaining_median", None, n, provenance, basis=NEUTRAL)


# ---------------------------------------------------------------------------
# Standings and playoff context
# ---------------------------------------------------------------------------

def completed_matchup_period(settings: dict[str, Any], as_of_week: int) -> int:
    """Last matchup period whose scoring weeks all fall at or before as_of_week."""
    periods = (settings.get("scheduleSettings") or {}).get("matchupPeriods") or {}
    if not periods:
        return as_of_week
    return max((int(k) for k, weeks in periods.items() if weeks and max(weeks) <= as_of_week), default=0)


def standings_signals(
    signals: _Signals, standings: list[TeamRecord], playoff_seats: int | None
) -> None:
    current = (signals.window.as_of_season,)
    provenance = "matchups:regular finished, as-of week"
    record = next((r for r in standings if r.provider_team_id == signals.team_id), None)
    games = record.wins + record.losses + record.ties if record else 0
    if not record or not games:
        for name in ("standings_rank", "win_pct", "games_back_of_playoff_line"):
            signals.add(name, None, 0, provenance, current, NEUTRAL)
        return

    def wins(r: TeamRecord) -> float:
        return r.wins + 0.5 * r.ties

    signals.add("standings_rank", standings.index(record) + 1, games, provenance, current)
    signals.add("win_pct", wins(record) / games, games, provenance, current)
    if playoff_seats and len(standings) > playoff_seats:
        line = standings[playoff_seats - 1] if standings.index(record) >= playoff_seats else standings[playoff_seats]
        signals.add("games_back_of_playoff_line", wins(line) - wins(record), games, provenance, current)
    else:
        signals.add("games_back_of_playoff_line", None, games, "league_settings:playoff seats unknown", current, NEUTRAL)


# ---------------------------------------------------------------------------
# Top level
# ---------------------------------------------------------------------------

def compute_rival_signals(
    repo: Repository,
    league_id: str,
    as_of_season: int,
    as_of_week: int,
    tx_season_from: int = 2018,
) -> list[RivalSignal]:
    """Signals for every team in the as-of season, using only data at or before the as-of point."""
    seasons = tuple(s.season for s in repo.seasons(league_id) if tx_season_from <= s.season <= as_of_season)
    window = Window(league_id, as_of_season, as_of_week, seasons)
    txns_by_season = {
        s: repo.transactions(league_id, s, as_of_week=window.end_week(s), include_items=True) for s in seasons
    }
    budgets = {s: repo.faab_budget(league_id, s) for s in seasons}
    remaining = repo.faab_remaining(league_id, as_of_season, as_of_week)
    settings = repo.league_settings(league_id, as_of_season) or {}
    slot_counts = {
        int(k): int(v)
        for k, v in ((settings.get("rosterSettings") or {}).get("lineupSlotCounts") or {}).items()
    }
    playoff_seats = (settings.get("scheduleSettings") or {}).get("playoffTeamCount")
    standings = repo.standings_as_of(league_id, as_of_season, completed_matchup_period(settings, as_of_week))

    roster_week = repo.latest_roster_period(league_id, as_of_season, as_of_week)
    roster = repo.roster_at(league_id, as_of_season, roster_week) if roster_week is not None else []
    player_ids = {e.provider_player_id for e in roster} | {
        i["provider_player_id"]
        for txns in txns_by_season.values()
        for t in txns
        for i in t.items
        if i.get("item_type") == "TRADE" and i.get("provider_player_id") is not None
    }
    positions = {
        pid: p.default_position_id
        for pid, p in repo.players_by_ids(sorted(player_ids)).items()
        if p.default_position_id is not None
    }

    owner_map = repo.team_owner_map(league_id)
    member_ids = {m.manager_id: m.provider_member_id for m in repo.league_managers(league_id)}
    by_manager = {s.manager_id: s for s in manager_scopes(owner_map, member_ids, list(seasons))}

    out: list[RivalSignal] = []
    teams = sorted(t.provider_team_id for t in repo.teams(league_id, as_of_season))
    team_scopes = {tid: _team_scope(tid, as_of_season, owner_map, by_manager) for tid in teams}
    for tid in teams:
        signals = _Signals(tid, window)
        scope = team_scopes[tid]
        others = [s for other, s in team_scopes.items() if other != tid]
        position_need(signals, [e for e in roster if e.provider_team_id == tid], positions, slot_counts, roster_week)
        trade_signals(signals, scope, txns_by_season, owner_map, positions)
        faab_signals(signals, scope, others, txns_by_season, budgets, remaining)
        standings_signals(signals, standings, playoff_seats)
        out.extend(signals.rows)
    return out


def _team_scope(
    team_id: int,
    season: int,
    owner_map: dict[tuple[int, int], list[str]],
    by_manager: dict[str, ManagerScope],
) -> ManagerScope:
    """The current team plus every earlier team its current owners ran."""
    owners = owner_map.get((season, team_id), [])
    teams: dict[int, set[int]] = {season: {team_id}}
    for manager_id in owners:
        for s, tids in by_manager[manager_id].teams_by_season.items():
            if s < season:
                teams.setdefault(s, set()).update(tids)
    return ManagerScope(f"team:{season}:{team_id}", None, teams, len(owners) > 1)
