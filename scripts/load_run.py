"""Load one completed ESPN scan into the backend-owned PostgreSQL schema.

The loader is deliberately idempotent: rows are upserted on natural keys and
child rows (roster entries, transaction items) are deleted then reinserted so
a rerun after a migration never leaves stale data.

Usage:
    uv run python scripts/load_run.py <run_dir>

The run directory must contain a manifest.jsonl at its root and season
subdirectories matching the scanner output layout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from espn_ff_assistant.database import connection, load_dotenv
from espn_ff_assistant.normalize import matchups as norm_matchups
from espn_ff_assistant.normalize import rosters as norm_rosters
from espn_ff_assistant.normalize import season as norm_season
from espn_ff_assistant.normalize import txns as norm_txns

# ---------------------------------------------------------------------------
# Archive helpers
# ---------------------------------------------------------------------------


def _load_json(run: Path, season: int, view: str, period: int | None = None) -> dict[str, Any] | None:
    path = (
        run / str(season) / view / f"scoring-period-{period:02d}" / "response.json"
        if period is not None
        else run / str(season) / view / "response.json"
    )
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _all_period_payloads(
    run: Path, view: str
) -> list[tuple[int, int, dict[str, Any], str]]:
    """Return (season, period, payload, raw_uri) for every per-period payload."""
    result = []
    for path in sorted(run.glob(f"*/{view}/**/response.json")):
        parts = path.relative_to(run).parts
        season = int(parts[0])
        period: int | None = None
        if len(parts) > 3 and parts[2].startswith("scoring-period-"):
            period = int(parts[2].split("-")[-1])
        if period is not None:
            result.append((season, period, json.loads(path.read_text()), str(path.relative_to(run))))
    return result


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Main loader
# ---------------------------------------------------------------------------


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="Path to a completed scan run directory")
    args = parser.parse_args()
    run = args.run if args.run.is_absolute() else Path.cwd() / args.run
    if not (run / "manifest.jsonl").exists():
        raise SystemExit(f"Not a completed run (no manifest.jsonl): {run}")

    seasons = sorted(int(p.name) for p in run.iterdir() if p.is_dir() and p.name.isdigit())
    if not seasons:
        raise SystemExit(f"No season directories found in {run}")

    with connection() as conn:
        # ---- User and league -----------------------------------------------
        latest_season = max(seasons)
        settings_payload = _load_json(run, latest_season, "mSettings") or {}
        provider_league_id = int(settings_payload.get("id") or 0)
        if not provider_league_id:
            raise SystemExit("Could not determine provider_league_id from mSettings")
        league_name = (settings_payload.get("settings") or {}).get("name") or "ESPN Fantasy Football"

        user_id = conn.execute(
            "INSERT INTO users (external_key) VALUES ('local-primary') "
            "ON CONFLICT (external_key) DO UPDATE SET external_key=EXCLUDED.external_key RETURNING id"
        ).fetchone()[0]

        league_id = conn.execute(
            """INSERT INTO leagues (user_id, provider, provider_league_id, name)
               VALUES (%s, 'espn', %s, %s)
               ON CONFLICT (user_id, provider, provider_league_id) DO UPDATE SET name=EXCLUDED.name
               RETURNING id""",
            (user_id, provider_league_id, league_name),
        ).fetchone()[0]

        sync_id = conn.execute(
            """INSERT INTO sync_runs (league_id, run_key, status, completed_at)
               VALUES (%s, %s, 'completed', now())
               ON CONFLICT (league_id, run_key)
               DO UPDATE SET status='completed', completed_at=now(), error=NULL
               RETURNING id""",
            (league_id, run.name),
        ).fetchone()[0]

        # ---- Raw payload manifest ------------------------------------------
        for line in (run / "manifest.jsonl").read_text().splitlines():
            entry = json.loads(line)
            raw_uri = entry.get("raw_path")
            if not raw_uri:
                continue
            path = run / raw_uri
            digest = _sha256(path)
            conn.execute(
                """INSERT INTO raw_payloads
                       (sync_run_id, season, view_name, scoring_period, payload_uri,
                        payload_sha256, transport_state, retrieved_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (sync_run_id, season, view_name, scoring_period, payload_sha256)
                   DO NOTHING""",
                (
                    sync_id,
                    entry["season"],
                    entry["view"],
                    entry.get("scoring_period"),
                    raw_uri,
                    digest or "",
                    entry.get("transport_state", "present"),
                    entry.get("retrieved_at"),
                ),
            )

        # ---- Season dimensions ---------------------------------------------
        season_ids: dict[int, Any] = {}

        for season in seasons:
            status_payload = _load_json(run, season, "mStatus") or {}
            settings_payload = _load_json(run, season, "mSettings") or {}
            status_row = norm_season.status_row(season, status_payload)

            season_id = conn.execute(
                """INSERT INTO league_seasons
                       (league_id, season, availability_state,
                        first_scoring_period, final_scoring_period)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (league_id, season) DO UPDATE SET
                       availability_state=EXCLUDED.availability_state,
                       first_scoring_period=EXCLUDED.first_scoring_period,
                       final_scoring_period=EXCLUDED.final_scoring_period
                   RETURNING id""",
                (
                    league_id,
                    season,
                    "present" if status_payload else "unknown",
                    status_row["first_scoring_period"],
                    status_row["final_scoring_period"],
                ),
            ).fetchone()[0]
            season_ids[season] = season_id

            conn.execute(
                """INSERT INTO league_settings (league_season_id, settings)
                   VALUES (%s,%s)
                   ON CONFLICT (league_season_id) DO UPDATE SET settings=EXCLUDED.settings""",
                (season_id, json.dumps(settings_payload.get("settings", {}))),
            )

            for sp_row in norm_season.scoring_periods(season, status_payload):
                conn.execute(
                    """INSERT INTO scoring_periods (league_season_id, scoring_period, state)
                       VALUES (%s,%s,%s)
                       ON CONFLICT (league_season_id, scoring_period)
                       DO UPDATE SET state=EXCLUDED.state""",
                    (season_id, sp_row["scoring_period"], sp_row["state"]),
                )

        # ---- Managers and teams --------------------------------------------
        manager_ids: dict[str, Any] = {}

        for season in seasons:
            season_id = season_ids[season]
            team_payload = _load_json(run, season, "mTeam")
            if not team_payload:
                continue

            for member_row in norm_season.members(team_payload):
                mid = member_row["provider_member_id"]
                manager_ids[mid] = conn.execute(
                    """INSERT INTO managers (user_id, provider, provider_member_id, display_name)
                       VALUES (%s,'espn',%s,%s)
                       ON CONFLICT (user_id, provider, provider_member_id)
                       DO UPDATE SET display_name=EXCLUDED.display_name
                       RETURNING id""",
                    (user_id, mid, member_row["display_name"]),
                ).fetchone()[0]

            for team_row in norm_season.teams(season, team_payload):
                tid = team_row["provider_team_id"]
                conn.execute(
                    """INSERT INTO teams (league_season_id, provider_team_id, name)
                       VALUES (%s,%s,%s)
                       ON CONFLICT (league_season_id, provider_team_id)
                       DO UPDATE SET name=EXCLUDED.name""",
                    (season_id, tid, team_row["name"]),
                )

                # Ensure any member referenced as an owner exists in managers.
                for owner_mid in team_row["owner_member_ids"]:
                    if owner_mid and owner_mid not in manager_ids:
                        manager_ids[owner_mid] = conn.execute(
                            """INSERT INTO managers (user_id, provider, provider_member_id)
                               VALUES (%s,'espn',%s)
                               ON CONFLICT (user_id, provider, provider_member_id)
                               DO UPDATE SET provider_member_id=EXCLUDED.provider_member_id
                               RETURNING id""",
                            (user_id, owner_mid),
                        ).fetchone()[0]

                # Legacy single-owner join (for backwards compat with existing queries).
                primary_mid = team_row["primary_owner_member_id"]
                if primary_mid and primary_mid in manager_ids:
                    conn.execute(
                        """INSERT INTO manager_season_identities
                               (league_season_id, manager_id, provider_team_id, identity_confidence)
                           VALUES (%s,%s,%s,'exact')
                           ON CONFLICT (league_season_id, provider_team_id)
                           DO UPDATE SET manager_id=EXCLUDED.manager_id,
                                         identity_confidence='exact'""",
                        (season_id, manager_ids[primary_mid], tid),
                    )

                # Full co-owner table.
                for owner_mid in team_row["owner_member_ids"]:
                    if not owner_mid or owner_mid not in manager_ids:
                        continue
                    conn.execute(
                        """INSERT INTO team_owners
                               (league_season_id, provider_team_id, manager_id, is_primary)
                           VALUES (%s,%s,%s,%s)
                           ON CONFLICT (league_season_id, provider_team_id, manager_id)
                           DO UPDATE SET is_primary=EXCLUDED.is_primary""",
                        (
                            season_id,
                            tid,
                            manager_ids[owner_mid],
                            owner_mid == team_row["primary_owner_member_id"],
                        ),
                    )

        # ---- Draft picks ----------------------------------------------------
        for season in seasons:
            season_id = season_ids[season]
            draft_payload = _load_json(run, season, "mDraftDetail")
            if not draft_payload:
                continue
            raw_uri = str(Path(str(season)) / "mDraftDetail" / "response.json")
            for pick in norm_season.draft_picks(season, draft_payload):
                conn.execute(
                    """INSERT INTO draft_picks
                           (league_season_id, overall_pick, round, round_pick,
                            provider_team_id, provider_player_id, bid_amount, raw_payload_uri)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (league_season_id, overall_pick) DO UPDATE SET
                           provider_team_id=EXCLUDED.provider_team_id,
                           provider_player_id=EXCLUDED.provider_player_id,
                           bid_amount=EXCLUDED.bid_amount,
                           raw_payload_uri=EXCLUDED.raw_payload_uri""",
                    (
                        season_id,
                        pick["overall_pick"],
                        pick["round"],
                        pick["round_pick"],
                        pick["provider_team_id"],
                        pick["provider_player_id"],
                        pick["bid_amount"],
                        raw_uri,
                    ),
                )

        # ---- Rosters and players --------------------------------------------
        for season, period, data, raw_uri in _all_period_payloads(run, "mRoster"):
            season_id = season_ids.get(season)
            if season_id is None:
                continue

            # Upsert players first.
            for player in norm_rosters.players(data):
                conn.execute(
                    """INSERT INTO players
                           (provider_player_id, full_name, first_name, last_name,
                            default_position_id, pro_team_id, raw_payload_uri, updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,now())
                       ON CONFLICT (provider_player_id) DO UPDATE SET
                           full_name=EXCLUDED.full_name,
                           first_name=EXCLUDED.first_name,
                           last_name=EXCLUDED.last_name,
                           default_position_id=EXCLUDED.default_position_id,
                           pro_team_id=EXCLUDED.pro_team_id,
                           raw_payload_uri=EXCLUDED.raw_payload_uri,
                           updated_at=now()""",
                    (
                        player["provider_player_id"],
                        player["full_name"],
                        player["first_name"],
                        player["last_name"],
                        player["default_position_id"],
                        player["pro_team_id"],
                        raw_uri,
                    ),
                )

            for snap in norm_rosters.roster_snapshots(season, period, data):
                snap_id = conn.execute(
                    """INSERT INTO roster_snapshots
                           (league_season_id, scoring_period, provider_team_id, raw_payload_uri)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT (league_season_id, scoring_period, provider_team_id)
                       DO UPDATE SET raw_payload_uri=EXCLUDED.raw_payload_uri
                       RETURNING id""",
                    (season_id, period, snap["provider_team_id"], raw_uri),
                ).fetchone()[0]

                # Clear and reload entries so reruns don't accumulate duplicates.
                conn.execute("DELETE FROM roster_entries WHERE roster_snapshot_id=%s", (snap_id,))
                entries = norm_rosters.roster_entries(season, period, data)
                team_entries = [e for e in entries if e[0] == snap["provider_team_id"]]
                if team_entries:
                    with conn.cursor() as cur:
                        cur.executemany(
                            """INSERT INTO roster_entries
                                   (roster_snapshot_id, provider_player_id,
                                    lineup_slot_id, acquisition_type, applied_stat_total)
                               VALUES (%s,%s,%s,%s,%s)
                               ON CONFLICT (roster_snapshot_id, provider_player_id, lineup_slot_id)
                               DO UPDATE SET
                                   acquisition_type=EXCLUDED.acquisition_type,
                                   applied_stat_total=EXCLUDED.applied_stat_total""",
                            [(snap_id, pid, slot, acq, stat) for _, pid, slot, acq, stat in team_entries],
                        )

        # ---- Matchups from mBoxscore payloads ------------------------------
        # Collect all (period, boxscore, settings) triples per season.
        boxscore_by_season: dict[int, list[tuple[int, dict[str, Any], dict[str, Any]]]] = {}
        for season, period, data, _ in _all_period_payloads(run, "mBoxscore"):
            settings = _load_json(run, season, "mSettings") or {}
            boxscore_by_season.setdefault(season, []).append((period, data, settings))

        for season, period_payloads in boxscore_by_season.items():
            season_id = season_ids.get(season)
            if season_id is None:
                continue
            for row in norm_matchups.from_boxscore_payloads(season, period_payloads):
                conn.execute(
                    """INSERT INTO matchups
                           (league_season_id, provider_matchup_id, matchup_period,
                            period_type, home_provider_team_id, away_provider_team_id,
                            home_score, away_score, winner, is_bye)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (provider_matchup_id) WHERE provider_matchup_id IS NOT NULL
                       DO UPDATE SET
                           matchup_period=EXCLUDED.matchup_period,
                           period_type=EXCLUDED.period_type,
                           home_provider_team_id=EXCLUDED.home_provider_team_id,
                           away_provider_team_id=EXCLUDED.away_provider_team_id,
                           home_score=EXCLUDED.home_score,
                           away_score=EXCLUDED.away_score,
                           winner=EXCLUDED.winner,
                           is_bye=EXCLUDED.is_bye""",
                    (
                        season_id,
                        row["provider_matchup_id"],
                        row["matchup_period"],
                        row["period_type"],
                        row["home_provider_team_id"],
                        row["away_provider_team_id"],
                        row["home_score"],
                        row["away_score"],
                        row["winner"],
                        row["is_bye"],
                    ),
                )

        # ---- Transactions --------------------------------------------------
        seen: set[tuple[int, str]] = set()
        for season, period, data, raw_uri in _all_period_payloads(run, "mTransactions2"):
            season_id = season_ids.get(season)
            if season_id is None:
                continue
            for tx in norm_txns.transactions(season, period, data):
                key = (season, tx["provider_transaction_id"])
                if key in seen:
                    continue
                seen.add(key)

                tx_id = conn.execute(
                    """INSERT INTO transactions
                           (league_season_id, provider_transaction_id, scoring_period,
                            provider_type, status, category, provider_team_id,
                            provider_member_id, bid_amount, process_date, proposed_date,
                            raw_payload_uri)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (league_season_id, provider_transaction_id) DO UPDATE SET
                           status=EXCLUDED.status,
                           category=EXCLUDED.category,
                           bid_amount=EXCLUDED.bid_amount,
                           raw_payload_uri=EXCLUDED.raw_payload_uri
                       RETURNING id""",
                    (
                        season_id,
                        tx["provider_transaction_id"],
                        tx["scoring_period"],
                        tx["provider_type"],
                        tx["status"],
                        tx["category"],
                        tx["provider_team_id"],
                        tx["provider_member_id"],
                        tx["bid_amount"],
                        tx["process_date"],
                        tx["proposed_date"],
                        raw_uri,
                    ),
                ).fetchone()[0]

                conn.execute("DELETE FROM transaction_items WHERE transaction_id=%s", (tx_id,))
                items = norm_txns.transaction_items(tx)
                if items:
                    with conn.cursor() as cur:
                        cur.executemany(
                            """INSERT INTO transaction_items
                                   (transaction_id, item_type, provider_player_id,
                                    from_provider_team_id, to_provider_team_id)
                               VALUES (%s,%s,%s,%s,%s)""",
                            [
                                (
                                    tx_id,
                                    item["item_type"],
                                    item["provider_player_id"],
                                    item["from_provider_team_id"],
                                    item["to_provider_team_id"],
                                )
                                for item in items
                            ],
                        )

        conn.commit()

        counts = {
            table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "league_seasons",
                "managers",
                "teams",
                "players",
                "roster_snapshots",
                "roster_entries",
                "matchups",
                "draft_picks",
                "transactions",
                "transaction_items",
                "team_owners",
            )
        }
        print(json.dumps({"run": run.name, "counts": counts}, indent=2))


if __name__ == "__main__":
    main()
