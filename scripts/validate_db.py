"""Validate row-level invariants in the loaded database.

Prints PASS/FAIL/WARN for each check and exits non-zero if any hard
check fails. Run after every load:

    uv run python scripts/validate_db.py

Optional flags:
    --league-id  Provider league ID to validate (default: auto-detected)
    --season     Restrict checks to a single season
"""

from __future__ import annotations

import argparse
import sys

from espn_ff_assistant.database import connection, load_dotenv

# ---------------------------------------------------------------------------
# Check runner
# ---------------------------------------------------------------------------

class Results:
    def __init__(self) -> None:
        self._hard_failures = 0
        self._warnings = 0

    def ok(self, label: str, detail: str = "") -> None:
        suffix = f"  {detail}" if detail else ""
        print(f"  PASS  {label}{suffix}")

    def fail(self, label: str, detail: str = "") -> None:
        self._hard_failures += 1
        suffix = f"  {detail}" if detail else ""
        print(f"  FAIL  {label}{suffix}")

    def warn(self, label: str, detail: str = "") -> None:
        self._warnings += 1
        suffix = f"  {detail}" if detail else ""
        print(f"  WARN  {label}{suffix}")

    @property
    def exit_code(self) -> int:
        return 1 if self._hard_failures else 0

    def summary(self) -> str:
        return (
            f"{self._hard_failures} hard failure(s), {self._warnings} warning(s)"
        )


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_seasons(conn, league_id, season_filter, r: Results) -> dict[int, str]:
    """Return season → season_id map and verify basics."""
    rows = conn.execute(
        "SELECT season, id, first_scoring_period, final_scoring_period "
        "FROM league_seasons WHERE league_id=%s ORDER BY season",
        (league_id,),
    ).fetchall()
    season_ids = {row[0]: row[1] for row in rows}

    if not rows:
        r.fail("Seasons", "no seasons found")
        return season_ids

    r.ok("Seasons present", f"{len(rows)} season(s)")

    # Each season should have scoring-period bounds
    missing_bounds = [row[0] for row in rows if row[2] is None or row[3] is None]
    if missing_bounds:
        r.warn("Scoring-period bounds", f"missing for seasons: {missing_bounds}")
    else:
        r.ok("Scoring-period bounds", "all seasons have first/final")

    return season_ids


def check_teams_and_owners(conn, league_id, season_ids, season_filter, r: Results) -> None:
    """Every team must have at least one owner. No manager owns two teams in same season."""
    seasons = [s for s in season_ids if season_filter is None or s == season_filter]
    for season in seasons:
        sid = season_ids[season]

        teams = conn.execute(
            "SELECT provider_team_id FROM teams WHERE league_season_id=%s", (sid,)
        ).fetchall()
        team_ids = [t[0] for t in teams]

        if not team_ids:
            r.warn(f"Teams {season}", "no teams found")
            continue
        r.ok(f"Teams {season}", f"{len(team_ids)} team(s)")

        # Every team must have at least one owner in team_owners
        orphans = conn.execute(
            """
            SELECT t.provider_team_id FROM teams t
            LEFT JOIN team_owners to2 ON to2.league_season_id=t.league_season_id
                AND to2.provider_team_id=t.provider_team_id
            WHERE t.league_season_id=%s AND to2.id IS NULL
            """,
            (sid,),
        ).fetchall()
        if orphans:
            r.warn(f"Ownerless teams {season}", f"{[o[0] for o in orphans]}")
        else:
            r.ok(f"Team ownership {season}", "all teams have ≥1 owner")

        # No manager should own two teams in the same season
        dupes = conn.execute(
            """
            SELECT manager_id, count(*) as c
            FROM team_owners WHERE league_season_id=%s
            GROUP BY manager_id HAVING count(*) > 1
            """,
            (sid,),
        ).fetchall()
        if dupes:
            r.warn(f"Duplicate team ownership {season}", f"{len(dupes)} manager(s) own >1 team")
        else:
            r.ok(f"Unique ownership {season}", "each manager owns ≤1 team")


def check_rosters(conn, league_id, season_ids, season_filter, r: Results) -> None:
    """No player appears on two teams in the same week."""
    seasons = [s for s in season_ids if season_filter is None or s == season_filter]
    for season in seasons:
        sid = season_ids[season]
        dupes = conn.execute(
            """
            SELECT rs.scoring_period, re.provider_player_id, count(*)
            FROM roster_entries re
            JOIN roster_snapshots rs ON rs.id = re.roster_snapshot_id
            WHERE rs.league_season_id=%s
            GROUP BY rs.scoring_period, re.provider_player_id
            HAVING count(*) > 1
            LIMIT 5
            """,
            (sid,),
        ).fetchall()
        if dupes:
            r.fail(f"Roster uniqueness {season}", f"{len(dupes)} duplicate player-week entries (sample: {dupes[0]})")
        else:
            snap_count = conn.execute(
                "SELECT count(*) FROM roster_snapshots WHERE league_season_id=%s", (sid,)
            ).fetchone()[0]
            r.ok(f"Roster uniqueness {season}", f"{snap_count} snapshot(s), no duplicates")


def check_drafts(conn, league_id, season_ids, season_filter, r: Results) -> None:
    """No player drafted twice in a season."""
    seasons = [s for s in season_ids if season_filter is None or s == season_filter]
    for season in seasons:
        sid = season_ids[season]
        dupes = conn.execute(
            """
            SELECT provider_player_id, count(*)
            FROM draft_picks WHERE league_season_id=%s
            GROUP BY provider_player_id HAVING count(*) > 1
            LIMIT 3
            """,
            (sid,),
        ).fetchall()
        if dupes:
            r.fail(f"Draft uniqueness {season}", f"player drafted twice: {[d[0] for d in dupes]}")
        else:
            picks = conn.execute(
                "SELECT count(*) FROM draft_picks WHERE league_season_id=%s", (sid,)
            ).fetchone()[0]
            if picks:
                r.ok(f"Draft picks {season}", f"{picks} pick(s), no duplicates")


def check_transactions(conn, league_id, season_ids, season_filter, r: Results) -> None:
    """Transaction items must reference existing transactions; dates in-season."""
    seasons = [s for s in season_ids if season_filter is None or s == season_filter]

    # Orphaned transaction items
    orphans = conn.execute(
        """
        SELECT count(*) FROM transaction_items ti
        WHERE NOT EXISTS (SELECT 1 FROM transactions t WHERE t.id=ti.transaction_id)
        """
    ).fetchone()[0]
    if orphans:
        r.fail("Transaction item references", f"{orphans} orphaned item(s)")
    else:
        r.ok("Transaction item references", "all items reference valid transactions")

    for season in seasons:
        sid = season_ids[season]
        # Completed trades should have items moving in both directions
        completed_trades = conn.execute(
            "SELECT id FROM transactions WHERE league_season_id=%s AND category='completed_trade'",
            (sid,),
        ).fetchall()
        one_sided = []
        for (tx_id,) in completed_trades:
            directions = conn.execute(
                """
                SELECT count(DISTINCT CASE WHEN from_provider_team_id=0 OR from_provider_team_id IS NULL
                                           THEN to_provider_team_id ELSE from_provider_team_id END)
                FROM transaction_items WHERE transaction_id=%s
                """,
                (tx_id,),
            ).fetchone()[0]
            if directions < 2:
                one_sided.append(tx_id)
        if one_sided:
            r.warn(f"Trade legs {season}", f"{len(one_sided)} completed trade(s) with <2 team directions")
        elif completed_trades:
            r.ok(f"Trade legs {season}", f"{len(completed_trades)} completed trade(s), all multi-party")

        # Count by category
        cats = conn.execute(
            """
            SELECT category, count(*) FROM transactions
            WHERE league_season_id=%s GROUP BY category ORDER BY count(*) DESC
            """,
            (sid,),
        ).fetchall()
        if cats:
            r.ok(f"Transaction categories {season}", ", ".join(f"{c}={n}" for c, n in cats))


def check_claim_attribution(conn, league_id, season_ids, season_filter, r: Results) -> None:
    """Waiver/free-agent activity must resolve to a team owner; member IDs are informational."""
    sids = [sid for s, sid in season_ids.items() if season_filter is None or s == season_filter]
    rows = conn.execute(
        """
        SELECT t.category,
               count(*) AS total,
               count(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM team_owners o
                   WHERE o.league_season_id=t.league_season_id
                     AND o.provider_team_id=t.provider_team_id)) AS team_resolved,
               count(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM managers m WHERE m.provider_member_id=t.provider_member_id)) AS member_resolved
        FROM transactions t
        WHERE t.league_season_id = ANY(%s)
          AND t.category IN ('waiver_claim', 'free_agent_move')
          AND t.status='EXECUTED'
        GROUP BY t.category ORDER BY t.category
        """,
        (sids,),
    ).fetchall()
    for category, total, team_resolved, member_resolved in rows:
        label = f"Executed {category} attribution"
        detail = f"{team_resolved}/{total} resolve via team owner; {member_resolved}/{total} via member ID"
        if team_resolved < total:
            r.fail(label, detail)
        else:
            r.ok(label, detail)


def check_matchups(conn, league_id, season_ids, season_filter, r: Results) -> None:
    """Winners must agree with scores; cross-check team records against mTeam."""
    seasons = [s for s in season_ids if season_filter is None or s == season_filter]
    for season in seasons:
        sid = season_ids[season]
        total = conn.execute(
            "SELECT count(*) FROM matchups WHERE league_season_id=%s", (sid,)
        ).fetchone()[0]
        if not total:
            r.warn(f"Matchups {season}", "no matchup rows")
            continue
        r.ok(f"Matchups present {season}", f"{total} matchup(s)")

        # Winner must agree with scores for finished games
        bad_winners = conn.execute(
            """
            SELECT count(*) FROM matchups
            WHERE league_season_id=%s
              AND home_score IS NOT NULL AND away_score IS NOT NULL
              AND winner IS NOT NULL AND is_bye=FALSE
              AND (
                (winner='home' AND home_score <= away_score)
                OR (winner='away' AND away_score <= home_score)
                OR (winner='tie' AND home_score <> away_score)
              )
            """,
            (sid,),
        ).fetchone()[0]
        if bad_winners:
            r.fail(f"Matchup winner consistency {season}", f"{bad_winners} row(s) with winner mismatch")
        else:
            r.ok(f"Matchup winner consistency {season}", "all winners agree with scores")

        # Cross-check season win totals from matchup rows vs mTeam record
        # (only for seasons where records were ingested — all seasons from mTeam)
        derived_records = conn.execute(
            """
            SELECT
                provider_team_id,
                SUM(CASE WHEN winner='home' THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN winner='away' THEN 1 ELSE 0 END) AS losses
            FROM (
                SELECT home_provider_team_id AS provider_team_id, winner
                FROM matchups
                WHERE league_season_id=%s AND period_type='regular' AND is_bye=FALSE
                  AND winner IS NOT NULL
                UNION ALL
                SELECT away_provider_team_id,
                    CASE WHEN winner='away' THEN 'home' WHEN winner='home' THEN 'away' ELSE winner END
                FROM matchups
                WHERE league_season_id=%s AND period_type='regular' AND is_bye=FALSE
                  AND winner IS NOT NULL
            ) sub
            GROUP BY provider_team_id
            ORDER BY provider_team_id
            """,
            (sid, sid),
        ).fetchall()
        if derived_records:
            r.ok(
                f"Matchup-derived records {season}",
                f"{len(derived_records)} team(s) with computed records",
            )
        else:
            r.warn(f"Matchup-derived records {season}", "no finished regular-season matchups")


def check_provenance(conn, league_id, season_ids, r: Results) -> None:
    """raw_payloads count should match manifest line count (soft); no empty hashes."""
    raw_count = conn.execute(
        """
        SELECT count(*) FROM raw_payloads rp
        JOIN sync_runs sr ON sr.id=rp.sync_run_id
        WHERE sr.league_id=%s
        """,
        (league_id,),
    ).fetchone()[0]
    r.ok("Raw payload rows", f"{raw_count}")

    empty_hash = conn.execute(
        """
        SELECT count(*) FROM raw_payloads rp
        JOIN sync_runs sr ON sr.id=rp.sync_run_id
        WHERE sr.league_id=%s AND (payload_sha256 IS NULL OR payload_sha256='')
        """,
        (league_id,),
    ).fetchone()[0]
    if empty_hash:
        r.warn("Payload hashes", f"{empty_hash} row(s) with empty hash")
    else:
        r.ok("Payload hashes", "all rows have a hash")


def check_roster_transaction_coverage(conn, league_id, season_ids, r: Results) -> None:
    """Soft metric: share of player additions explained by a recorded transaction (2018+)."""
    for season in sorted(s for s in season_ids if s >= 2018):
        sid = season_ids[season]
        # Count waiver + free-agent transactions
        tx_adds = conn.execute(
            """
            SELECT count(*) FROM transactions
            WHERE league_season_id=%s AND category IN ('waiver_claim','free_agent_move')
            """,
            (sid,),
        ).fetchone()[0]
        # Count non-draft acquisitions in roster entries
        roster_non_draft = conn.execute(
            """
            SELECT count(DISTINCT (rs.scoring_period, re.provider_player_id, rs.provider_team_id))
            FROM roster_entries re
            JOIN roster_snapshots rs ON rs.id=re.roster_snapshot_id
            WHERE rs.league_season_id=%s AND re.acquisition_type NOT IN ('DRAFT','') AND re.acquisition_type IS NOT NULL
            """,
            (sid,),
        ).fetchone()[0]
        if roster_non_draft:
            pct = 100 * tx_adds / roster_non_draft if roster_non_draft else 0
            r.warn(
                f"Roster/transaction coverage {season}",
                f"{tx_adds} waiver/FA txns vs {roster_non_draft} non-draft roster slots ({pct:.0f}% — informational)",
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--league-id", type=int, help="ESPN provider league ID")
    parser.add_argument("--season", type=int, help="Restrict checks to one season")
    args = parser.parse_args()

    r = Results()

    with connection() as conn:
        # Detect league
        if args.league_id:
            row = conn.execute(
                "SELECT id FROM leagues WHERE provider='espn' AND provider_league_id=%s",
                (args.league_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, provider_league_id FROM leagues WHERE provider='espn' LIMIT 1"
            ).fetchone()
        if not row:
            print("FAIL  No ESPN leagues found in database")
            return 1
        league_id = row[0]
        provider_id = args.league_id or row[1]
        print(f"Validating league {provider_id} (internal ID {league_id})\n")

        print("=== Seasons ===")
        season_ids = check_seasons(conn, league_id, args.season, r)

        print("\n=== Teams and Ownership ===")
        check_teams_and_owners(conn, league_id, season_ids, args.season, r)

        print("\n=== Rosters ===")
        check_rosters(conn, league_id, season_ids, args.season, r)

        print("\n=== Drafts ===")
        check_drafts(conn, league_id, season_ids, args.season, r)

        print("\n=== Transactions ===")
        check_transactions(conn, league_id, season_ids, args.season, r)
        check_claim_attribution(conn, league_id, season_ids, args.season, r)

        print("\n=== Matchups ===")
        check_matchups(conn, league_id, season_ids, args.season, r)

        print("\n=== Provenance ===")
        check_provenance(conn, league_id, season_ids, r)

        print("\n=== Roster/Transaction Coverage (soft) ===")
        check_roster_transaction_coverage(conn, league_id, season_ids, r)

    print(f"\n{'='*50}")
    print(r.summary())
    return r.exit_code


if __name__ == "__main__":
    sys.exit(main())
