"""Repository: the only place allowed to issue SQL against application tables.

All read functions that return time-sensitive data require an `as_of` argument
(season: int, week: int) and never return data from after that point. This
ensures that manager stats and backtests do not leak future information.

Usage:
    from espn_ff_assistant.database import connection
    from espn_ff_assistant.repository import Repository

    with connection() as conn:
        repo = Repository(conn)
        teams = repo.teams(2024)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Season:
    season: int
    season_id: str
    first_scoring_period: int | None
    final_scoring_period: int | None
    availability_state: str


@dataclass
class Team:
    season: int
    provider_team_id: int
    name: str | None
    primary_owner_member_id: str | None


@dataclass
class OwnerAssignment:
    provider_team_id: int
    manager_id: str
    provider_member_id: str
    display_name: str | None
    is_primary: bool
    identity_confidence: str


@dataclass
class Player:
    provider_player_id: int
    full_name: str | None
    first_name: str | None
    last_name: str | None
    default_position_id: int | None
    pro_team_id: int | None


@dataclass
class RosterEntry:
    provider_player_id: int
    provider_team_id: int
    lineup_slot_id: int | None
    acquisition_type: str | None
    applied_stat_total: float | None
    player: Player | None = None


@dataclass
class Matchup:
    provider_matchup_id: int
    matchup_period: int
    period_type: str
    home_provider_team_id: int | None
    away_provider_team_id: int | None
    home_score: float | None
    away_score: float | None
    winner: str | None
    is_bye: bool


@dataclass
class Transaction:
    provider_transaction_id: str
    season: int
    scoring_period: int | None
    provider_type: str | None
    status: str | None
    category: str | None
    provider_team_id: int | None
    provider_member_id: str | None
    bid_amount: float | None
    process_date: datetime | None
    proposed_date: datetime | None
    items: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TeamRecord:
    provider_team_id: int
    wins: int
    losses: int
    ties: int
    points_for: float
    points_against: float


@dataclass
class ManagerFeature:
    manager_id: str
    league_id: str
    stat_name: str
    value: float | None
    sample_size: int | None
    season_from: int | None
    season_to: int | None
    as_of_season: int
    as_of_week: int
    confidence: str | None
    version: int
    shared_team: bool = False


@dataclass
class LeagueManager:
    manager_id: str
    provider_member_id: str
    display_name: str | None


@dataclass
class OwnershipInterval:
    season: int
    provider_team_id: int
    provider_player_id: int
    start_week: int
    end_week: int | None
    end_reason: str | None


@dataclass
class DraftPick:
    season: int
    overall_pick: int | None
    round: int | None
    round_pick: int | None
    provider_team_id: int | None
    provider_player_id: int | None


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

class Repository:
    """Typed read/write interface over the normalized PostgreSQL schema.

    Construct with an open psycopg connection. The caller owns the
    transaction boundary; call conn.commit() after any writes.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    # ---- League helpers ----------------------------------------------------

    def league_id(self, provider_league_id: int) -> str | None:
        """Return the internal UUID for an ESPN league, or None."""
        row = self._conn.execute(
            "SELECT id FROM leagues WHERE provider='espn' AND provider_league_id=%s",
            (provider_league_id,),
        ).fetchone()
        return row[0] if row else None

    def default_league_id(self) -> str | None:
        """Return the first ESPN league in the database."""
        row = self._conn.execute(
            "SELECT id FROM leagues WHERE provider='espn' ORDER BY created_at LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def _season_id(self, league_id: str, season: int) -> str | None:
        row = self._conn.execute(
            "SELECT id FROM league_seasons WHERE league_id=%s AND season=%s",
            (league_id, season),
        ).fetchone()
        return row[0] if row else None

    # ---- Seasons -----------------------------------------------------------

    def seasons(self, league_id: str) -> list[Season]:
        """All seasons for a league, ordered chronologically."""
        rows = self._conn.execute(
            """SELECT season, id, first_scoring_period, final_scoring_period, availability_state
               FROM league_seasons WHERE league_id=%s ORDER BY season""",
            (league_id,),
        ).fetchall()
        return [
            Season(
                season=r[0],
                season_id=r[1],
                first_scoring_period=r[2],
                final_scoring_period=r[3],
                availability_state=r[4],
            )
            for r in rows
        ]

    # ---- Teams and ownership -----------------------------------------------

    def teams(self, league_id: str, season: int) -> list[Team]:
        """All teams for a season."""
        sid = self._season_id(league_id, season)
        if not sid:
            return []
        rows = self._conn.execute(
            """SELECT t.provider_team_id, t.name, msi.provider_member_id
               FROM teams t
               LEFT JOIN manager_season_identities msi
                   ON msi.league_season_id=t.league_season_id
                   AND msi.provider_team_id=t.provider_team_id
               WHERE t.league_season_id=%s
               ORDER BY t.provider_team_id""",
            (sid,),
        ).fetchall()
        return [
            Team(
                season=season,
                provider_team_id=r[0],
                name=r[1],
                primary_owner_member_id=r[2],
            )
            for r in rows
        ]

    def owners_for_team(
        self, league_id: str, season: int, provider_team_id: int
    ) -> list[OwnerAssignment]:
        """All owners of a team in a season, including co-owners."""
        sid = self._season_id(league_id, season)
        if not sid:
            return []
        rows = self._conn.execute(
            """SELECT to2.provider_team_id, to2.manager_id, m.provider_member_id,
                      m.display_name, to2.is_primary,
                      COALESCE(msi.identity_confidence, 'unknown')
               FROM team_owners to2
               JOIN managers m ON m.id=to2.manager_id
               LEFT JOIN manager_season_identities msi
                   ON msi.league_season_id=to2.league_season_id
                   AND msi.provider_team_id=to2.provider_team_id
                   AND msi.manager_id=to2.manager_id
               WHERE to2.league_season_id=%s AND to2.provider_team_id=%s
               ORDER BY to2.is_primary DESC""",
            (sid, provider_team_id),
        ).fetchall()
        return [
            OwnerAssignment(
                provider_team_id=r[0],
                manager_id=str(r[1]),
                provider_member_id=r[2],
                display_name=r[3],
                is_primary=r[4],
                identity_confidence=r[5],
            )
            for r in rows
        ]

    def league_managers(self, league_id: str) -> list[LeagueManager]:
        """Every manager who has owned or co-owned a team in the league."""
        rows = self._conn.execute(
            """SELECT DISTINCT m.id, m.provider_member_id, m.display_name
               FROM managers m
               JOIN team_owners to2 ON to2.manager_id=m.id
               JOIN league_seasons ls ON ls.id=to2.league_season_id
               WHERE ls.league_id=%s
               ORDER BY m.display_name NULLS LAST, m.id""",
            (league_id,),
        ).fetchall()
        return [LeagueManager(str(r[0]), r[1], r[2]) for r in rows]

    def team_owner_map(self, league_id: str) -> dict[tuple[int, int], list[str]]:
        """{(season, provider_team_id): [manager_id, ...]} across all seasons."""
        rows = self._conn.execute(
            """SELECT ls.season, to2.provider_team_id, to2.manager_id
               FROM team_owners to2
               JOIN league_seasons ls ON ls.id=to2.league_season_id
               WHERE ls.league_id=%s
               ORDER BY ls.season, to2.provider_team_id, to2.is_primary DESC""",
            (league_id,),
        ).fetchall()
        owners: dict[tuple[int, int], list[str]] = {}
        for season, team_id, manager_id in rows:
            owners.setdefault((season, team_id), []).append(str(manager_id))
        return owners

    # ---- Settings ----------------------------------------------------------

    def league_settings(self, league_id: str, season: int) -> dict[str, Any] | None:
        """Raw ESPN settings object stored for a season."""
        sid = self._season_id(league_id, season)
        if not sid:
            return None
        row = self._conn.execute(
            "SELECT settings FROM league_settings WHERE league_season_id=%s", (sid,)
        ).fetchone()
        return row[0] if row else None

    def faab_budget(self, league_id: str, season: int) -> int | None:
        """Season FAAB budget, or None when the league does not use FAAB."""
        settings = self.league_settings(league_id, season) or {}
        acquisition = settings.get("acquisitionSettings") or {}
        if not acquisition.get("isUsingAcquisitionBudget"):
            return None
        budget = acquisition.get("acquisitionBudget")
        return int(budget) if isinstance(budget, (int, float)) else None

    def faab_remaining(self, league_id: str, season: int, as_of_week: int) -> dict[int, int]:
        """{provider_team_id: budget minus executed claim spend through as_of_week}."""
        budget = self.faab_budget(league_id, season)
        sid = self._season_id(league_id, season)
        if budget is None or not sid:
            return {}
        teams = self._conn.execute(
            "SELECT provider_team_id FROM teams WHERE league_season_id=%s", (sid,)
        ).fetchall()
        spent = dict(
            self._conn.execute(
                """SELECT provider_team_id, COALESCE(SUM(bid_amount), 0)
                   FROM transactions
                   WHERE league_season_id=%s AND category='waiver_claim'
                     AND status='EXECUTED' AND scoring_period<=%s
                   GROUP BY provider_team_id""",
                (sid, as_of_week),
            ).fetchall()
        )
        return {t[0]: budget - int(spent.get(t[0], 0)) for t in teams}

    # ---- Ownership intervals and drafts --------------------------------------

    def ownership_intervals(
        self, league_id: str, seasons: list[int], as_of_season: int, as_of_week: int
    ) -> list[OwnershipInterval]:
        """Closed intervals ending at or before the as-of point."""
        rows = self._conn.execute(
            """SELECT ls.season, poi.provider_team_id, poi.provider_player_id,
                      poi.start_week, poi.end_week, poi.end_reason
               FROM player_ownership_intervals poi
               JOIN league_seasons ls ON ls.id=poi.league_season_id
               WHERE ls.league_id=%s AND ls.season = ANY(%s)
                 AND poi.end_week IS NOT NULL
                 AND (ls.season < %s OR (ls.season=%s AND poi.end_week<=%s))""",
            (league_id, seasons, as_of_season, as_of_season, as_of_week),
        ).fetchall()
        return [OwnershipInterval(*r) for r in rows]

    def draft_picks(self, league_id: str, season: int) -> list[DraftPick]:
        sid = self._season_id(league_id, season)
        if not sid:
            return []
        rows = self._conn.execute(
            """SELECT overall_pick, round, round_pick, provider_team_id, provider_player_id
               FROM draft_picks WHERE league_season_id=%s ORDER BY overall_pick""",
            (sid,),
        ).fetchall()
        return [DraftPick(season, *r) for r in rows]

    # ---- Players -----------------------------------------------------------

    def player(self, provider_player_id: int) -> Player | None:
        row = self._conn.execute(
            """SELECT provider_player_id, full_name, first_name, last_name,
                      default_position_id, pro_team_id
               FROM players WHERE provider_player_id=%s""",
            (provider_player_id,),
        ).fetchone()
        if not row:
            return None
        return Player(
            provider_player_id=row[0],
            full_name=row[1],
            first_name=row[2],
            last_name=row[3],
            default_position_id=row[4],
            pro_team_id=row[5],
        )

    def players_by_ids(self, ids: list[int]) -> dict[int, Player]:
        """Batch-fetch players by provider ID. Returns {player_id: Player}."""
        if not ids:
            return {}
        rows = self._conn.execute(
            """SELECT provider_player_id, full_name, first_name, last_name,
                      default_position_id, pro_team_id
               FROM players WHERE provider_player_id = ANY(%s)""",
            (ids,),
        ).fetchall()
        return {
            r[0]: Player(
                provider_player_id=r[0],
                full_name=r[1],
                first_name=r[2],
                last_name=r[3],
                default_position_id=r[4],
                pro_team_id=r[5],
            )
            for r in rows
        }

    # ---- Rosters -----------------------------------------------------------

    def roster_at(
        self,
        league_id: str,
        season: int,
        week: int,
        provider_team_id: int | None = None,
        include_players: bool = False,
    ) -> list[RosterEntry]:
        """Roster state at a given week. Does not return data from after `week`.

        Pass provider_team_id to restrict to one team.
        Pass include_players=True to join player identity rows.
        """
        sid = self._season_id(league_id, season)
        if not sid:
            return []

        team_filter = "AND rs.provider_team_id=%s" if provider_team_id else ""
        params: list[Any] = [sid, week]
        if provider_team_id:
            params.append(provider_team_id)

        rows = self._conn.execute(
            f"""SELECT re.provider_player_id, rs.provider_team_id,
                       re.lineup_slot_id, re.acquisition_type, re.applied_stat_total
                FROM roster_entries re
                JOIN roster_snapshots rs ON rs.id=re.roster_snapshot_id
                WHERE rs.league_season_id=%s
                  AND rs.scoring_period=%s
                  {team_filter}
                ORDER BY rs.provider_team_id, re.lineup_slot_id""",
            params,
        ).fetchall()

        entries = [
            RosterEntry(
                provider_player_id=r[0],
                provider_team_id=r[1],
                lineup_slot_id=r[2],
                acquisition_type=r[3],
                applied_stat_total=r[4],
            )
            for r in rows
        ]

        if include_players and entries:
            player_map = self.players_by_ids([e.provider_player_id for e in entries])
            for entry in entries:
                entry.player = player_map.get(entry.provider_player_id)

        return entries

    # ---- Matchups ----------------------------------------------------------

    def matchups(
        self,
        league_id: str,
        season: int,
        as_of_week: int | None = None,
        period_type: str | None = None,
    ) -> list[Matchup]:
        """Matchups for a season. Pass as_of_week to exclude future periods.

        as_of_week is a matchup_period (not scoring_period); pass None for all.
        """
        sid = self._season_id(league_id, season)
        if not sid:
            return []

        filters = ["league_season_id=%s"]
        params: list[Any] = [sid]

        if as_of_week is not None:
            filters.append("matchup_period<=%s")
            params.append(as_of_week)
        if period_type:
            filters.append("period_type=%s")
            params.append(period_type)

        where = " AND ".join(filters)
        rows = self._conn.execute(
            f"""SELECT provider_matchup_id, matchup_period, period_type,
                       home_provider_team_id, away_provider_team_id,
                       home_score, away_score, winner, is_bye
                FROM matchups WHERE {where}
                ORDER BY matchup_period, provider_matchup_id""",
            params,
        ).fetchall()

        return [
            Matchup(
                provider_matchup_id=r[0],
                matchup_period=r[1],
                period_type=r[2],
                home_provider_team_id=r[3],
                away_provider_team_id=r[4],
                home_score=r[5],
                away_score=r[6],
                winner=r[7],
                is_bye=r[8],
            )
            for r in rows
        ]

    def standings_as_of(
        self, league_id: str, season: int, as_of_week: int
    ) -> list[TeamRecord]:
        """Derive standings from matchup results up to as_of_week (inclusive).

        Only considers regular-season finished matchups.
        Returns records sorted by wins desc, points_for desc.
        """
        sid = self._season_id(league_id, season)
        if not sid:
            return []

        rows = self._conn.execute(
            """
            WITH sides AS (
                SELECT home_provider_team_id AS team, winner, 'home' AS side, home_score AS scored, away_score AS allowed
                FROM matchups
                WHERE league_season_id=%s AND matchup_period<=%s
                  AND period_type='regular' AND is_bye=FALSE AND winner IS NOT NULL
                UNION ALL
                SELECT away_provider_team_id, winner, 'away', away_score, home_score
                FROM matchups
                WHERE league_season_id=%s AND matchup_period<=%s
                  AND period_type='regular' AND is_bye=FALSE AND winner IS NOT NULL
            )
            SELECT
                team,
                SUM(CASE WHEN (side='home' AND winner='home') OR (side='away' AND winner='away') THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN (side='home' AND winner='away') OR (side='away' AND winner='home') THEN 1 ELSE 0 END) AS losses,
                SUM(CASE WHEN winner='tie' THEN 1 ELSE 0 END) AS ties,
                COALESCE(SUM(scored), 0) AS points_for,
                COALESCE(SUM(allowed), 0) AS points_against
            FROM sides
            GROUP BY team
            ORDER BY wins DESC, points_for DESC
            """,
            (sid, as_of_week, sid, as_of_week),
        ).fetchall()

        return [
            TeamRecord(
                provider_team_id=r[0],
                wins=r[1],
                losses=r[2],
                ties=r[3],
                points_for=float(r[4]),
                points_against=float(r[5]),
            )
            for r in rows
        ]

    # ---- Transactions ------------------------------------------------------

    def transactions(
        self,
        league_id: str,
        season: int,
        as_of_week: int | None = None,
        category: str | None = None,
        provider_team_id: int | None = None,
        include_items: bool = False,
    ) -> list[Transaction]:
        """Transactions for a season, optionally filtered.

        as_of_week limits to scoring_period <= as_of_week.
        """
        sid = self._season_id(league_id, season)
        if not sid:
            return []

        filters = ["t.league_season_id=%s"]
        params: list[Any] = [sid]

        if as_of_week is not None:
            filters.append("t.scoring_period<=%s")
            params.append(as_of_week)
        if category:
            filters.append("t.category=%s")
            params.append(category)
        if provider_team_id:
            filters.append("t.provider_team_id=%s")
            params.append(provider_team_id)

        where = " AND ".join(filters)
        rows = self._conn.execute(
            f"""SELECT t.id, t.provider_transaction_id, t.scoring_period,
                       t.provider_type, t.status, t.category,
                       t.provider_team_id, t.provider_member_id,
                       t.bid_amount, t.process_date, t.proposed_date
                FROM transactions t WHERE {where}
                ORDER BY t.process_date NULLS LAST, t.id""",
            params,
        ).fetchall()

        txns = [
            Transaction(
                provider_transaction_id=r[1],
                season=season,
                scoring_period=r[2],
                provider_type=r[3],
                status=r[4],
                category=r[5],
                provider_team_id=r[6],
                provider_member_id=r[7],
                bid_amount=r[8],
                process_date=r[9],
                proposed_date=r[10],
            )
            for r in rows
        ]

        if include_items and txns:
            tx_ids = [r[0] for r in rows]
            item_rows = self._conn.execute(
                """SELECT transaction_id, item_type, provider_player_id,
                          from_provider_team_id, to_provider_team_id
                   FROM transaction_items WHERE transaction_id = ANY(%s)""",
                (tx_ids,),
            ).fetchall()
            items_by_tx: dict[str, list[dict[str, Any]]] = {}
            for tx_id_raw, itype, pid, ftid, ttid in item_rows:
                items_by_tx.setdefault(str(tx_id_raw), []).append(
                    {
                        "item_type": itype,
                        "provider_player_id": pid,
                        "from_provider_team_id": ftid,
                        "to_provider_team_id": ttid,
                    }
                )
            for txn, row in zip(txns, rows, strict=False):
                txn.items = items_by_tx.get(str(row[0]), [])

        return txns

    # ---- Manager features (read only) -------------------------------------

    def manager_features(
        self,
        league_id: str,
        as_of_season: int,
        as_of_week: int,
        manager_id: str | None = None,
        version: int | None = None,
    ) -> list[ManagerFeature]:
        """Computed manager feature rows as of a given week.

        Never returns rows computed after as_of_season/as_of_week. Defaults to
        the newest feature version present, so superseded rows are not mixed in.
        """
        filters = [
            "mf.league_id=%s",
            "(mf.as_of_season < %s OR (mf.as_of_season=%s AND mf.as_of_week<=%s))",
        ]
        params: list[Any] = [league_id, as_of_season, as_of_season, as_of_week]

        if manager_id:
            filters.append("mf.manager_id=%s")
            params.append(manager_id)
        if version is None:
            filters.append(
                "mf.version=(SELECT MAX(version) FROM manager_features WHERE league_id=%s)"
            )
            params.append(league_id)
        else:
            filters.append("mf.version=%s")
            params.append(version)

        where = " AND ".join(filters)
        rows = self._conn.execute(
            f"""SELECT mf.manager_id, mf.league_id, mf.stat_name, mf.value,
                       mf.sample_size, mf.season_from, mf.season_to,
                       mf.as_of_season, mf.as_of_week, mf.confidence, mf.version,
                       mf.shared_team
                FROM manager_features mf WHERE {where}
                ORDER BY mf.manager_id, mf.stat_name, mf.as_of_season DESC, mf.as_of_week DESC""",
            params,
        ).fetchall()

        return [
            ManagerFeature(
                manager_id=str(r[0]),
                league_id=str(r[1]),
                stat_name=r[2],
                value=r[3],
                sample_size=r[4],
                season_from=r[5],
                season_to=r[6],
                as_of_season=r[7],
                as_of_week=r[8],
                confidence=r[9],
                version=r[10],
                shared_team=bool(r[11]),
            )
            for r in rows
        ]

    # ---- Upsert helpers (write side) --------------------------------------

    def upsert_manager_features(self, features: list[dict[str, Any]]) -> int:
        """Upsert a batch of manager_features rows. Returns count inserted/updated."""
        if not features:
            return 0
        with self._conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO manager_features
                       (manager_id, league_id, stat_name, value, sample_size,
                        season_from, season_to, as_of_season, as_of_week,
                        confidence, version, shared_team)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (manager_id, league_id, stat_name, as_of_season, as_of_week, version)
                   DO UPDATE SET
                       value=EXCLUDED.value,
                       sample_size=EXCLUDED.sample_size,
                       season_from=EXCLUDED.season_from,
                       season_to=EXCLUDED.season_to,
                       confidence=EXCLUDED.confidence,
                       shared_team=EXCLUDED.shared_team,
                       computed_at=now()""",
                [
                    (
                        f["manager_id"],
                        f["league_id"],
                        f["stat_name"],
                        f.get("value"),
                        f.get("sample_size"),
                        f.get("season_from"),
                        f.get("season_to"),
                        f["as_of_season"],
                        f["as_of_week"],
                        f.get("confidence"),
                        f.get("version", 1),
                        f.get("shared_team", False),
                    )
                    for f in features
                ],
            )
        return len(features)
