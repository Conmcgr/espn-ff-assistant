"""Compute measured manager behavior features from normalized league data.

All computations are point-in-time: they use only data available at or before
the given (as_of_season, as_of_week). No psychological labels are assigned;
every feature is a measured count, rate, or aggregate with a sample size.

Confidence levels:
  'high'   sample_size >= 20
  'medium' sample_size >= 5
  'low'    sample_size >= 1
  'insufficient' sample_size == 0 or None
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from espn_ff_assistant.repository import Repository, Transaction

_VERSION = 1


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
        "version": _VERSION,
    }


# ---------------------------------------------------------------------------
# Waiver and FAAB features
# ---------------------------------------------------------------------------

def waiver_features(
    repo: Repository,
    league_id: str,
    manager_id: str,
    provider_member_id: str,
    as_of_season: int,
    as_of_week: int,
    season_window: list[int],
) -> list[dict[str, Any]]:
    """Waiver claim counts and FAAB statistics for one manager."""
    all_claims: list[Transaction] = []
    all_fa_moves: list[Transaction] = []

    for season in season_window:
        end_week = as_of_week if season == as_of_season else 999
        txns = repo.transactions(
            league_id, season, as_of_week=end_week, include_items=False
        )
        for tx in txns:
            if tx.provider_member_id != provider_member_id:
                continue
            if tx.category == "waiver_claim" and tx.status == "EXECUTED":
                all_claims.append(tx)
            elif tx.category == "free_agent_move":
                all_fa_moves.append(tx)

    # All failed claims for FAAB stats
    failed_claims: list[Transaction] = []
    for season in season_window:
        end_week = as_of_week if season == as_of_season else 999
        txns = repo.transactions(
            league_id, season, as_of_week=end_week, include_items=False
        )
        for tx in txns:
            if tx.provider_member_id == provider_member_id and tx.category == "waiver_claim" and tx.status != "EXECUTED":
                failed_claims.append(tx)

    season_from = min(season_window) if season_window else None
    season_to = max(season_window) if season_window else None

    features: list[dict[str, Any]] = []

    # Successful waiver claims
    n_claims = len(all_claims)
    features.append(_feature(manager_id, league_id, "waiver_claims_total", n_claims, n_claims, season_from, season_to, as_of_season, as_of_week))

    # Free-agent moves
    n_fa = len(all_fa_moves)
    features.append(_feature(manager_id, league_id, "free_agent_moves_total", n_fa, n_fa, season_from, season_to, as_of_season, as_of_week))

    # FAAB spend from successful claims
    bids = [tx.bid_amount for tx in all_claims if tx.bid_amount is not None]
    if bids:
        features.append(_feature(manager_id, league_id, "faab_total_spent", sum(bids), len(bids), season_from, season_to, as_of_season, as_of_week))
        features.append(_feature(manager_id, league_id, "faab_mean_winning_bid", statistics.mean(bids), len(bids), season_from, season_to, as_of_season, as_of_week))
        features.append(_feature(manager_id, league_id, "faab_median_winning_bid", statistics.median(bids), len(bids), season_from, season_to, as_of_season, as_of_week))
    else:
        features.append(_feature(manager_id, league_id, "faab_total_spent", 0, 0, season_from, season_to, as_of_season, as_of_week))
        features.append(_feature(manager_id, league_id, "faab_mean_winning_bid", None, 0, season_from, season_to, as_of_season, as_of_week))
        features.append(_feature(manager_id, league_id, "faab_median_winning_bid", None, 0, season_from, season_to, as_of_season, as_of_week))

    # Failed claim rate
    n_failed = len(failed_claims)
    n_total_claims = n_claims + n_failed
    fail_rate = n_failed / n_total_claims if n_total_claims else None
    features.append(_feature(manager_id, league_id, "waiver_fail_rate", fail_rate, n_total_claims, season_from, season_to, as_of_season, as_of_week))

    return features


# ---------------------------------------------------------------------------
# Trade features
# ---------------------------------------------------------------------------

def trade_features(
    repo: Repository,
    league_id: str,
    manager_id: str,
    provider_member_id: str,
    provider_team_ids_by_season: dict[int, list[int]],
    as_of_season: int,
    as_of_week: int,
    season_window: list[int],
) -> list[dict[str, Any]]:
    """Trade frequency, acceptance rates, and multi-player tendencies."""
    completed: list[Transaction] = []
    proposals_sent: list[Transaction] = []
    proposals_received: list[Transaction] = []

    for season in season_window:
        team_ids = provider_team_ids_by_season.get(season, [])
        end_week = as_of_week if season == as_of_season else 999
        txns = repo.transactions(league_id, season, as_of_week=end_week, include_items=True)

        for tx in txns:
            is_member = tx.provider_member_id == provider_member_id or tx.provider_team_id in team_ids
            if tx.category == "completed_trade" and is_member:
                completed.append(tx)
            elif tx.category == "trade_proposal":
                if is_member:
                    proposals_sent.append(tx)
                else:
                    # Received: one of my teams is in the items
                    for item in tx.items:
                        if item.get("from_provider_team_id") in team_ids or item.get("to_provider_team_id") in team_ids:
                            proposals_received.append(tx)
                            break

    season_from = min(season_window) if season_window else None
    season_to = max(season_window) if season_window else None

    features: list[dict[str, Any]] = []

    n_completed = len(completed)
    features.append(_feature(manager_id, league_id, "trades_completed_total", n_completed, n_completed, season_from, season_to, as_of_season, as_of_week))
    features.append(_feature(manager_id, league_id, "trade_proposals_sent", len(proposals_sent), len(proposals_sent), season_from, season_to, as_of_season, as_of_week))

    # Multi-player trade rate: trades with >2 total items / 2 players moving
    multi_player = sum(1 for tx in completed if len(tx.items) > 2)
    features.append(_feature(manager_id, league_id, "trade_multi_player_rate", multi_player / n_completed if n_completed else None, n_completed, season_from, season_to, as_of_season, as_of_week))

    # Unique trade partners
    partners: set[int] = set()
    for tx in completed:
        for item in tx.items:
            for tid in [item.get("from_provider_team_id"), item.get("to_provider_team_id")]:
                if tid and tid not in (provider_team_ids_by_season.get(as_of_season) or []):
                    partners.add(tid)
    features.append(_feature(manager_id, league_id, "trade_unique_partners", len(partners), n_completed, season_from, season_to, as_of_season, as_of_week))

    return features


# ---------------------------------------------------------------------------
# Roster churn features
# ---------------------------------------------------------------------------

def roster_churn_features(
    repo: Repository,
    league_id: str,
    manager_id: str,
    provider_member_id: str,
    provider_team_ids_by_season: dict[int, list[int]],
    as_of_season: int,
    as_of_week: int,
    season_window: list[int],
) -> list[dict[str, Any]]:
    """Adds per week and streaming behavior."""
    total_adds = 0
    total_weeks = 0

    for season in season_window:
        team_ids = provider_team_ids_by_season.get(season, [])
        end_week = as_of_week if season == as_of_season else 999
        txns = repo.transactions(league_id, season, as_of_week=end_week, include_items=True)

        season_obj = next((s for s in repo.seasons(league_id) if s.season == season), None)
        if season_obj and season_obj.first_scoring_period and season_obj.final_scoring_period:
            weeks_in_season = min(end_week, season_obj.final_scoring_period) - season_obj.first_scoring_period + 1
            total_weeks += max(0, weeks_in_season)

        for tx in txns:
            if tx.category not in ("waiver_claim", "free_agent_move"):
                continue
            is_member = tx.provider_member_id == provider_member_id or tx.provider_team_id in team_ids
            if not is_member:
                continue
            for item in tx.items:
                if item.get("item_type") == "ADD":
                    total_adds += 1

    season_from = min(season_window) if season_window else None
    season_to = max(season_window) if season_window else None

    features: list[dict[str, Any]] = []
    adds_per_week = total_adds / total_weeks if total_weeks else None
    features.append(_feature(manager_id, league_id, "adds_total", total_adds, total_adds, season_from, season_to, as_of_season, as_of_week))
    features.append(_feature(manager_id, league_id, "adds_per_week", adds_per_week, total_weeks, season_from, season_to, as_of_season, as_of_week))

    return features


# ---------------------------------------------------------------------------
# Holding period features (from ownership intervals)
# ---------------------------------------------------------------------------

def holding_period_features(
    conn: Any,
    league_id: str,
    manager_id: str,
    season_ids: dict[int, str],
    as_of_season: int,
    as_of_week: int,
    season_window: list[int],
) -> list[dict[str, Any]]:
    """Median holding period in weeks from player_ownership_intervals."""
    sid_list = [season_ids[s] for s in season_window if s in season_ids]
    if not sid_list:
        return []

    rows = conn.execute(
        """
        SELECT poi.end_week - poi.start_week AS holding_weeks, poi.end_reason
        FROM player_ownership_intervals poi
        JOIN team_owners to2 ON to2.league_season_id=poi.league_season_id
            AND to2.provider_team_id=poi.provider_team_id
        JOIN managers m ON m.id=to2.manager_id
        WHERE poi.league_season_id = ANY(%s)
          AND m.id=%s
          AND poi.end_week IS NOT NULL
        """,
        (sid_list, manager_id),
    ).fetchall()

    holding_weeks = [r[0] for r in rows if r[0] is not None and r[0] >= 0]
    drops = sum(1 for r in rows if r[1] == "drop")

    season_from = min(season_window) if season_window else None
    season_to = max(season_window) if season_window else None

    features: list[dict[str, Any]] = []
    n = len(holding_weeks)
    features.append(_feature(manager_id, league_id, "holding_period_median_weeks", statistics.median(holding_weeks) if holding_weeks else None, n, season_from, season_to, as_of_season, as_of_week))
    features.append(_feature(manager_id, league_id, "holding_period_mean_weeks", statistics.mean(holding_weeks) if holding_weeks else None, n, season_from, season_to, as_of_season, as_of_week))
    features.append(_feature(manager_id, league_id, "players_dropped_total", drops, n, season_from, season_to, as_of_season, as_of_week))
    return features


# ---------------------------------------------------------------------------
# Draft features
# ---------------------------------------------------------------------------

def draft_features(
    repo: Repository,
    conn: Any,
    league_id: str,
    manager_id: str,
    provider_team_ids_by_season: dict[int, list[int]],
    as_of_season: int,
    as_of_week: int,
    season_window: list[int],
) -> list[dict[str, Any]]:
    """Picks per round, early-round position tendencies."""
    # Build {season_id: [picks]} for this manager's teams
    season_objs = {s.season: s for s in repo.seasons(league_id)}
    all_picks: list[dict[str, Any]] = []

    for season in season_window:
        team_ids = provider_team_ids_by_season.get(season, [])
        if not team_ids:
            continue
        s_obj = season_objs.get(season)
        if not s_obj:
            continue
        rows = conn.execute(
            """SELECT overall_pick, round, round_pick, provider_team_id, provider_player_id
               FROM draft_picks WHERE league_season_id=%s
               ORDER BY overall_pick""",
            (s_obj.season_id,),
        ).fetchall()
        for row in rows:
            if row[3] in team_ids:
                all_picks.append({"season": season, "round": row[1], "round_pick": row[2], "player_id": row[4]})

    season_from = min(season_window) if season_window else None
    season_to = max(season_window) if season_window else None
    n = len(all_picks)

    features: list[dict[str, Any]] = []
    features.append(_feature(manager_id, league_id, "draft_picks_total", n, n, season_from, season_to, as_of_season, as_of_week))

    # Average round of first pick (proxy for draft position)
    first_picks = [p["round_pick"] for p in all_picks if p["round"] == 1]
    features.append(_feature(manager_id, league_id, "draft_avg_first_round_position", statistics.mean(first_picks) if first_picks else None, len(first_picks), season_from, season_to, as_of_season, as_of_week))

    return features


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
    # Get all league_seasons for this league
    season_rows = conn.execute(
        "SELECT id, season FROM league_seasons WHERE league_id=%s ORDER BY season",
        (league_id,),
    ).fetchall()

    total = 0
    for (season_id, season) in season_rows:
        # Get roster presence per (team, player)
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
            # Check whether the player was on this team at the season's final week
            final_week = conn.execute(
                "SELECT final_scoring_period FROM league_seasons WHERE id=%s", (season_id,)
            ).fetchone()
            at_end = final_week and end_week >= final_week[0]
            end_reason = "season_end" if at_end else "drop_or_trade"

            # Try to match a drop/trade transaction if available (2018+)
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

def compute_all_features(
    repo: Repository,
    conn: Any,
    league_id: str,
    as_of_season: int,
    as_of_week: int,
    tx_season_from: int = 2018,
) -> list[dict[str, Any]]:
    """Compute all manager features and return them as upsert-ready dicts.

    tx_season_from: earliest season with transaction data (default 2018).
    """
    all_features: list[dict[str, Any]] = []

    # Seasons in the transaction modeling window
    seasons_obj = repo.seasons(league_id)
    season_ids = {s.season: s.season_id for s in seasons_obj}
    tx_seasons = [
        s.season for s in seasons_obj
        if s.season >= tx_season_from
        and (s.season < as_of_season or (s.season == as_of_season))
    ]

    # Get all managers ever linked to this league
    managers = conn.execute(
        """
        SELECT DISTINCT m.id, m.provider_member_id, m.display_name
        FROM managers m
        JOIN team_owners to2 ON to2.manager_id=m.id
        JOIN league_seasons ls ON ls.id=to2.league_season_id
        WHERE ls.league_id=%s
        ORDER BY m.id
        """,
        (league_id,),
    ).fetchall()

    for (mgr_id, member_id, _display) in managers:
        mgr_id_str = str(mgr_id)

        # Map season → team IDs for this manager
        team_rows = conn.execute(
            """
            SELECT ls.season, to2.provider_team_id
            FROM team_owners to2
            JOIN league_seasons ls ON ls.id=to2.league_season_id
            WHERE ls.league_id=%s AND to2.manager_id=%s
            """,
            (league_id, mgr_id),
        ).fetchall()
        team_ids_by_season: dict[int, list[int]] = defaultdict(list)
        for season, tid in team_rows:
            team_ids_by_season[season].append(tid)

        all_features.extend(waiver_features(repo, league_id, mgr_id_str, member_id, as_of_season, as_of_week, tx_seasons))
        all_features.extend(trade_features(repo, league_id, mgr_id_str, member_id, dict(team_ids_by_season), as_of_season, as_of_week, tx_seasons))
        all_features.extend(roster_churn_features(repo, league_id, mgr_id_str, member_id, dict(team_ids_by_season), as_of_season, as_of_week, tx_seasons))
        all_features.extend(holding_period_features(conn, league_id, mgr_id_str, season_ids, as_of_season, as_of_week, tx_seasons))
        all_features.extend(draft_features(repo, conn, league_id, mgr_id_str, dict(team_ids_by_season), as_of_season, as_of_week, list(season_ids.keys())))

    return all_features
