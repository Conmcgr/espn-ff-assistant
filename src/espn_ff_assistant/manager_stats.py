"""Compute measured manager behavior features from normalized league data.

All computations are point-in-time: they use only data available at or before
the given (as_of_season, as_of_week). No psychological labels are assigned;
every feature is a measured count, rate, or aggregate with a sample size.

Waiver, FAAB, and churn activity is attributed through the acting team
(season, provider_team_id) and its owners. ESPN stamps executed waiver claims
with a league-level member ID, so member IDs on transactions are not used for
attribution of those categories.

Confidence levels:
  'high'   sample_size >= 20
  'medium' sample_size >= 5
  'low'    sample_size >= 1
  'insufficient' sample_size == 0 or None
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from espn_ff_assistant.repository import (
    DraftPick,
    OwnershipInterval,
    Repository,
    Transaction,
)

VERSION = 2

POSITION_NAMES = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}
LOST_BID_STATUSES = frozenset({"FAILED_INVALIDPLAYERSOURCE"})
EARLY_SEASON_LAST_WEEK = 4


def _confidence(n: int | None) -> str:
    if not n:
        return "insufficient"
    if n >= 20:
        return "high"
    if n >= 5:
        return "medium"
    return "low"


def _feature(
    manager_id: str,
    league_id: str,
    stat_name: str,
    value: float | None,
    sample_size: int,
    season_from: int | None,
    season_to: int | None,
    as_of_season: int,
    as_of_week: int,
    shared_team: bool = False,
) -> dict[str, Any]:
    return {
        "manager_id": manager_id,
        "league_id": league_id,
        "stat_name": stat_name,
        "value": value,
        "sample_size": sample_size,
        "season_from": season_from,
        "season_to": season_to,
        "as_of_season": as_of_season,
        "as_of_week": as_of_week,
        "confidence": _confidence(sample_size),
        "version": VERSION,
        "shared_team": shared_team,
    }


@dataclass(frozen=True)
class Window:
    league_id: str
    as_of_season: int
    as_of_week: int
    seasons: tuple[int, ...]

    def end_week(self, season: int) -> int:
        return self.as_of_week if season == self.as_of_season else 999


@dataclass
class ManagerScope:
    manager_id: str
    provider_member_id: str | None
    teams_by_season: dict[int, set[int]] = field(default_factory=dict)
    shared_team: bool = False

    def owns(self, season: int, team_id: int | None) -> bool:
        return team_id is not None and team_id in self.teams_by_season.get(season, set())


Emit = Callable[[str, float | None, int], None]


def _emitter(scope: ManagerScope, window: Window, seasons: list[int], out: list[dict[str, Any]]) -> Emit:
    season_from = min(seasons) if seasons else None
    season_to = max(seasons) if seasons else None

    def emit(stat: str, value: float | None, n: int) -> None:
        out.append(
            _feature(
                scope.manager_id, window.league_id, stat, value, n,
                season_from, season_to, window.as_of_season, window.as_of_week,
                scope.shared_team,
            )
        )

    return emit


def _to_float(value: Any) -> float | None:
    return float(value) if value is not None else None


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def _added_player(tx: Transaction) -> int | None:
    for item in tx.items:
        if item.get("item_type") == "ADD":
            return item.get("provider_player_id")
    return None


# ---------------------------------------------------------------------------
# Waiver and FAAB features
# ---------------------------------------------------------------------------

def waiver_features(
    scope: ManagerScope,
    window: Window,
    txns_by_season: dict[int, list[Transaction]],
    budgets: dict[int, int | None],
    positions: dict[int, int],
) -> list[dict[str, Any]]:
    """Claim counts, failure/outbid rates, and FAAB spending for one manager's teams."""
    seasons = [s for s in window.seasons if s in txns_by_season]
    out: list[dict[str, Any]] = []
    emit = _emitter(scope, window, seasons, out)

    executed: list[Transaction] = []
    failed: list[Transaction] = []
    fa_moves = 0
    pct_of_remaining: list[float] = []

    for season in seasons:
        season_claims: list[Transaction] = []
        for tx in txns_by_season[season]:
            if not scope.owns(season, tx.provider_team_id):
                continue
            if tx.category == "waiver_claim":
                if tx.status == "EXECUTED":
                    season_claims.append(tx)
                elif tx.status and tx.status.startswith("FAILED"):
                    failed.append(tx)
            elif tx.category == "free_agent_move" and tx.status in ("EXECUTED", None):
                fa_moves += 1
        executed.extend(season_claims)

        budget = budgets.get(season)
        if budget:
            spent = 0.0
            ordered = sorted(season_claims, key=lambda t: (t.process_date is None, t.process_date))
            for tx in ordered:
                bid = _to_float(tx.bid_amount) or 0.0
                remaining = budget - spent
                if remaining > 0:
                    pct_of_remaining.append(bid / remaining)
                spent += bid

    n_exec = len(executed)
    n_decided = n_exec + len(failed)
    lost = sum(1 for tx in failed if tx.status in LOST_BID_STATUSES)

    emit("waiver_claims_total", n_exec, n_exec)
    emit("free_agent_moves_total", fa_moves, fa_moves)
    emit("waiver_fail_rate", len(failed) / n_decided if n_decided else None, n_decided)
    emit("waiver_lost_bid_rate", lost / n_decided if n_decided else None, n_decided)

    faab_seasons = [s for s in seasons if budgets.get(s)]
    bids = [
        (tx, float(tx.bid_amount))
        for tx in executed
        if tx.bid_amount is not None and tx.season in faab_seasons
    ]
    amounts = [b for _, b in bids]
    emit("faab_total_spent", sum(amounts), len(amounts))
    emit("faab_mean_winning_bid", statistics.mean(amounts) if amounts else None, len(amounts))
    emit("faab_median_winning_bid", statistics.median(amounts) if amounts else None, len(amounts))

    total = sum(amounts)
    early = sum(b for tx, b in bids if (tx.scoring_period or 0) <= EARLY_SEASON_LAST_WEEK)
    emit("faab_early_season_share", early / total if total else None, len(amounts))

    emit(
        "faab_bid_pct_of_remaining_median",
        statistics.median(pct_of_remaining) if pct_of_remaining else None,
        len(pct_of_remaining),
    )
    emit(
        "faab_bid_pct_of_remaining_p75",
        _percentile(pct_of_remaining, 0.75) if pct_of_remaining else None,
        len(pct_of_remaining),
    )

    by_position: dict[str, list[float]] = defaultdict(list)
    for tx, bid in bids:
        pid = _added_player(tx)
        name = POSITION_NAMES.get(positions.get(pid)) if pid is not None else None
        if name:
            by_position[name].append(bid)
    for name in POSITION_NAMES.values():
        values = by_position.get(name, [])
        emit(f"faab_median_bid_{name}", statistics.median(values) if values else None, len(values))

    return out


# ---------------------------------------------------------------------------
# Trade features
# ---------------------------------------------------------------------------

def trade_features(
    scope: ManagerScope,
    window: Window,
    txns_by_season: dict[int, list[Transaction]],
) -> list[dict[str, Any]]:
    """Trade frequency, proposals, multi-player tendency, and partner breadth."""
    seasons = [s for s in window.seasons if s in txns_by_season]
    out: list[dict[str, Any]] = []
    emit = _emitter(scope, window, seasons, out)

    completed: list[Transaction] = []
    proposals_sent = 0
    for season in seasons:
        for tx in txns_by_season[season]:
            mine = scope.owns(season, tx.provider_team_id)
            if tx.category == "completed_trade" and mine:
                completed.append(tx)
            elif tx.category == "trade_proposal" and mine:
                proposals_sent += 1

    n = len(completed)
    emit("trades_completed_total", n, n)
    emit("trade_proposals_sent", proposals_sent, proposals_sent)
    multi = sum(1 for tx in completed if len(tx.items) > 2)
    emit("trade_multi_player_rate", multi / n if n else None, n)

    partners: set[tuple[int, int]] = set()
    for tx in completed:
        own = scope.teams_by_season.get(tx.season, set())
        for item in tx.items:
            for tid in (item.get("from_provider_team_id"), item.get("to_provider_team_id")):
                if tid and tid not in own:
                    partners.add((tx.season, tid))
    emit("trade_unique_partners", len(partners), n)
    return out


# ---------------------------------------------------------------------------
# Roster churn features
# ---------------------------------------------------------------------------

def roster_churn_features(
    scope: ManagerScope,
    window: Window,
    txns_by_season: dict[int, list[Transaction]],
    weeks_by_season: dict[int, int],
) -> list[dict[str, Any]]:
    """Adds per observed week. Sample size is the number of adds."""
    seasons = [s for s in window.seasons if s in txns_by_season]
    out: list[dict[str, Any]] = []
    emit = _emitter(scope, window, seasons, out)

    adds = 0
    weeks = 0
    for season in seasons:
        if not scope.teams_by_season.get(season):
            continue
        weeks += weeks_by_season.get(season, 0)
        for tx in txns_by_season[season]:
            if tx.category not in ("waiver_claim", "free_agent_move"):
                continue
            if tx.status not in ("EXECUTED", None) or not scope.owns(season, tx.provider_team_id):
                continue
            adds += sum(1 for item in tx.items if item.get("item_type") == "ADD")

    emit("adds_total", adds, adds)
    emit("adds_per_week", adds / weeks if weeks else None, adds)
    emit("weeks_observed", weeks, weeks)
    return out


# ---------------------------------------------------------------------------
# Holding period features (from ownership intervals)
# ---------------------------------------------------------------------------

def holding_period_features(
    scope: ManagerScope,
    window: Window,
    intervals: list[OwnershipInterval],
) -> list[dict[str, Any]]:
    """Holding periods of closed ownership intervals for this manager's teams."""
    mine = [iv for iv in intervals if scope.owns(iv.season, iv.provider_team_id)]
    seasons = sorted({iv.season for iv in mine}) or list(window.seasons)
    out: list[dict[str, Any]] = []
    emit = _emitter(scope, window, seasons, out)

    weeks = [
        iv.end_week - iv.start_week
        for iv in mine
        if iv.end_week is not None and iv.end_week >= iv.start_week
    ]
    drops = sum(1 for iv in mine if iv.end_reason == "drop")
    n = len(weeks)
    emit("holding_period_median_weeks", statistics.median(weeks) if weeks else None, n)
    emit("holding_period_mean_weeks", statistics.mean(weeks) if weeks else None, n)
    emit("players_dropped_total", drops, n)
    return out


# ---------------------------------------------------------------------------
# Draft features
# ---------------------------------------------------------------------------

def draft_features(
    scope: ManagerScope,
    window: Window,
    picks: list[DraftPick],
) -> list[dict[str, Any]]:
    mine = [p for p in picks if scope.owns(p.season, p.provider_team_id)]
    seasons = sorted({p.season for p in mine}) or list(window.seasons)
    out: list[dict[str, Any]] = []
    emit = _emitter(scope, window, seasons, out)

    emit("draft_picks_total", len(mine), len(mine))
    first = [p.round_pick for p in mine if p.round == 1 and p.round_pick is not None]
    emit("draft_avg_first_round_position", statistics.mean(first) if first else None, len(first))
    return out


# ---------------------------------------------------------------------------
# Ownership interval builder
# ---------------------------------------------------------------------------

def build_ownership_intervals(conn: Any, league_id: str) -> int:
    """Derive player_ownership_intervals from roster snapshots.

    For each (season, team, player), finds the first and last week the
    player appears. Matches transaction records from 2018+ to fill in
    acquisition_type and end_reason where possible.

    Returns the number of intervals inserted/updated.
    """
    season_rows = conn.execute(
        "SELECT id, season, final_scoring_period FROM league_seasons WHERE league_id=%s ORDER BY season",
        (league_id,),
    ).fetchall()

    total = 0
    for (season_id, season, final_week) in season_rows:
        rows = conn.execute(
            """
            SELECT rs.provider_team_id, re.provider_player_id,
                   MIN(rs.scoring_period) AS start_week,
                   MAX(rs.scoring_period) AS end_week,
                   MIN(re.acquisition_type) AS acquisition_type
            FROM roster_entries re
            JOIN roster_snapshots rs ON rs.id=re.roster_snapshot_id
            WHERE rs.league_season_id=%s
            GROUP BY rs.provider_team_id, re.provider_player_id
            """,
            (season_id,),
        ).fetchall()

        for team_id, player_id, start_week, end_week, acq_type in rows:
            at_end = final_week is not None and end_week >= final_week
            end_reason = "season_end" if at_end else "drop_or_trade"

            end_tx_id = None
            if season >= 2018 and not at_end:
                tx_row = conn.execute(
                    """
                    SELECT t.id FROM transactions t
                    JOIN transaction_items ti ON ti.transaction_id=t.id
                    WHERE t.league_season_id=%s
                      AND ti.from_provider_team_id=%s
                      AND ti.provider_player_id=%s
                      AND t.category IN ('waiver_claim','free_agent_move','completed_trade')
                    ORDER BY t.process_date NULLS LAST LIMIT 1
                    """,
                    (season_id, team_id, player_id),
                ).fetchone()
                if tx_row:
                    end_tx_id = tx_row[0]
                    end_reason = "drop"

            conn.execute(
                """
                INSERT INTO player_ownership_intervals
                    (league_season_id, provider_team_id, provider_player_id,
                     start_week, end_week, acquisition_type, end_reason,
                     end_transaction_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (league_season_id, provider_team_id, provider_player_id, start_week)
                DO UPDATE SET
                    end_week=EXCLUDED.end_week,
                    acquisition_type=EXCLUDED.acquisition_type,
                    end_reason=EXCLUDED.end_reason,
                    end_transaction_id=EXCLUDED.end_transaction_id
                """,
                (season_id, team_id, player_id, start_week, end_week, acq_type, end_reason, end_tx_id),
            )
            total += 1

    conn.commit()
    return total


# ---------------------------------------------------------------------------
# Top-level: compute all features for all managers
# ---------------------------------------------------------------------------

def manager_scopes(
    owner_map: dict[tuple[int, int], list[str]],
    member_ids: dict[str, str],
    seasons: list[int],
) -> list[ManagerScope]:
    """Build per-manager team ownership, flagging managers on co-owned teams."""
    scopes: dict[str, ManagerScope] = {}
    for (season, team_id), owners in owner_map.items():
        for manager_id in owners:
            scope = scopes.setdefault(manager_id, ManagerScope(manager_id, member_ids.get(manager_id)))
            scope.teams_by_season.setdefault(season, set()).add(team_id)
            if len(owners) > 1 and season in seasons:
                scope.shared_team = True
    return sorted(scopes.values(), key=lambda s: s.manager_id)


def compute_all_features(
    repo: Repository,
    league_id: str,
    as_of_season: int,
    as_of_week: int,
    tx_season_from: int = 2018,
) -> list[dict[str, Any]]:
    """Compute all manager features and return them as upsert-ready dicts.

    tx_season_from: earliest season with transaction data (default 2018).
    """
    season_objs = [s for s in repo.seasons(league_id) if s.season <= as_of_season]
    all_seasons = tuple(s.season for s in season_objs)
    tx_seasons = tuple(s for s in all_seasons if s >= tx_season_from)
    tx_window = Window(league_id, as_of_season, as_of_week, tx_seasons)
    all_window = Window(league_id, as_of_season, as_of_week, all_seasons)

    txns_by_season = {
        s: repo.transactions(league_id, s, as_of_week=tx_window.end_week(s), include_items=True)
        for s in tx_seasons
    }
    budgets = {s: repo.faab_budget(league_id, s) for s in tx_seasons}
    weeks_by_season: dict[int, int] = {}
    for s in season_objs:
        if s.season in tx_seasons and s.first_scoring_period and s.final_scoring_period:
            last = min(tx_window.end_week(s.season), s.final_scoring_period)
            weeks_by_season[s.season] = max(0, last - s.first_scoring_period + 1)

    added = {
        pid
        for txns in txns_by_season.values()
        for tx in txns
        for item in tx.items
        if item.get("item_type") == "ADD" and (pid := item.get("provider_player_id")) is not None
    }
    positions = {
        pid: p.default_position_id
        for pid, p in repo.players_by_ids(sorted(added)).items()
        if p.default_position_id is not None
    }
    intervals = repo.ownership_intervals(league_id, list(tx_seasons), as_of_season, as_of_week)
    picks = [p for s in all_seasons for p in repo.draft_picks(league_id, s)]

    member_ids = {m.manager_id: m.provider_member_id for m in repo.league_managers(league_id)}
    scopes = manager_scopes(repo.team_owner_map(league_id), member_ids, list(tx_seasons))

    features: list[dict[str, Any]] = []
    for scope in scopes:
        features.extend(waiver_features(scope, tx_window, txns_by_season, budgets, positions))
        features.extend(trade_features(scope, tx_window, txns_by_season))
        features.extend(roster_churn_features(scope, tx_window, txns_by_season, weeks_by_season))
        features.extend(holding_period_features(scope, tx_window, intervals))
        features.extend(draft_features(scope, all_window, picks))
    return features
