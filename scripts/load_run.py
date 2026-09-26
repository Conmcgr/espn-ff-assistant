"""Load one completed ESPN scan into the backend-owned Supabase schema.

The loader is deliberately idempotent: natural keys are upserted and child
rows are replaced for each parent snapshot/transaction on repeat runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from espn_ff_assistant.database import connection, load_dotenv
from espn_ff_assistant.transactions import category


def millis(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def payload(run: Path, season: int, view: str, period: int | None = None) -> tuple[dict[str, Any], Path] | None:
    path = run / str(season) / view / (f"scoring-period-{period:02d}" if period is not None else "") / "response.json"
    if not path.exists():
        return None
    return json.loads(path.read_text()), path


def all_payloads(run: Path, view: str) -> list[tuple[int, int | None, dict[str, Any], Path]]:
    result = []
    for path in run.glob(f"*/{view}/**/response.json"):
        parts = path.relative_to(run).parts
        season = int(parts[0])
        period = None
        if len(parts) > 3 and parts[2].startswith("scoring-period-"):
            period = int(parts[2].split("-")[-1])
        result.append((season, period, json.loads(path.read_text()), path))
    return sorted(result)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    run = args.run if args.run.is_absolute() else Path.cwd() / args.run
    if not (run / "manifest.jsonl").exists():
        raise SystemExit(f"not a completed run: {run}")

    with connection() as conn:
        # The local league is keyed independently of a display name so reruns
        # and name changes do not create duplicate identities.
        settings0 = payload(run, max(int(p.name) for p in run.iterdir() if p.name.isdigit()), "mSettings")
        settings = settings0[0].get("settings", {}) if settings0 else {}
        provider_league_id = int(settings0[0].get("id") or 944591) if settings0 else 944591
        league_name = settings.get("name") or "ESPN Fantasy Football"
        user = conn.execute("INSERT INTO users (external_key) VALUES ('local-primary') ON CONFLICT (external_key) DO UPDATE SET external_key=EXCLUDED.external_key RETURNING id").fetchone()[0]
        league = conn.execute("""INSERT INTO leagues (user_id, provider, provider_league_id, name)
            VALUES (%s, 'espn', %s, %s)
            ON CONFLICT (user_id, provider, provider_league_id) DO UPDATE SET name=EXCLUDED.name
            RETURNING id""", (user, provider_league_id, league_name)).fetchone()[0]
        sync = conn.execute("""INSERT INTO sync_runs (league_id, run_key, status, completed_at)
            VALUES (%s, %s, 'completed', now())
            ON CONFLICT (league_id, run_key) DO UPDATE SET status='completed', completed_at=now(), error=NULL
            RETURNING id""", (league, run.name)).fetchone()[0]

        season_ids: dict[int, Any] = {}
        team_ids: dict[tuple[int, int], Any] = {}
        manager_ids: dict[str, Any] = {}

        seasons = sorted(int(p.name) for p in run.iterdir() if p.is_dir() and p.name.isdigit())
        for season in seasons:
            status0 = payload(run, season, "mStatus")
            status = status0[0].get("status", {}) if status0 else {}
            first, final = status.get("firstScoringPeriod"), status.get("finalScoringPeriod")
            sid = conn.execute("""INSERT INTO league_seasons (league_id, season, availability_state, first_scoring_period, final_scoring_period)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT (league_id,season) DO UPDATE SET availability_state=EXCLUDED.availability_state,
                first_scoring_period=EXCLUDED.first_scoring_period, final_scoring_period=EXCLUDED.final_scoring_period RETURNING id""",
                (league, season, "present" if status0 else "unknown", first, final)).fetchone()[0]
            season_ids[season] = sid
            conn.execute("""INSERT INTO league_settings (league_season_id, settings) VALUES (%s,%s)
                ON CONFLICT (league_season_id) DO UPDATE SET settings=EXCLUDED.settings""", (sid, json.dumps((payload(run, season, "mSettings") or ({}, None))[0].get("settings", {}))))
            if isinstance(first, int) and isinstance(final, int):
                for period in range(first, final + 1):
                    state = "completed" if period <= (status.get("latestScoringPeriod") or 0) else "scheduled"
                    conn.execute("""INSERT INTO scoring_periods (league_season_id, scoring_period, state) VALUES (%s,%s,%s)
                        ON CONFLICT (league_season_id,scoring_period) DO UPDATE SET state=EXCLUDED.state""", (sid, period, state))

        # Load manifest metadata, retaining the local path and hash for auditability.
        for line in (run / "manifest.jsonl").read_text().splitlines():
            entry = json.loads(line)
            raw = entry.get("raw_path")
            if not raw:
                continue
            path = run / raw
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            retrieved = entry.get("retrieved_at")
            conn.execute("""INSERT INTO raw_payloads (sync_run_id, season, view_name, scoring_period, payload_uri, payload_sha256, transport_state, retrieved_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""", (sync, entry["season"], entry["view"], entry.get("scoring_period"), raw, digest, entry.get("transport_state", "present"), retrieved))

        # Managers and teams are season-scoped; ESPN member ids are the stable
        # identity available in the historical payloads.
        for season in seasons:
            sid = season_ids[season]
            team_payload = payload(run, season, "mTeam")
            if not team_payload:
                continue
            data = team_payload[0]
            for member in data.get("members", []):
                mid = str(member.get("id"))
                if not mid or mid == "None":
                    continue
                manager_ids[mid] = conn.execute("""INSERT INTO managers (user_id, provider, provider_member_id, display_name) VALUES (%s,'espn',%s,%s)
                    ON CONFLICT (user_id,provider,provider_member_id) DO UPDATE SET display_name=EXCLUDED.display_name RETURNING id""", (user, mid, member.get("displayName"))).fetchone()[0]
            for team in data.get("teams", []):
                tid = int(team["id"])
                team_ids[(season, tid)] = conn.execute("""INSERT INTO teams (league_season_id,provider_team_id,name) VALUES (%s,%s,%s)
                    ON CONFLICT (league_season_id,provider_team_id) DO UPDATE SET name=EXCLUDED.name RETURNING id""", (sid, tid, team.get("name"))).fetchone()[0]
                for owner in team.get("owners", []):
                    mid = str(owner)
                    if mid not in manager_ids:
                        manager_ids[mid] = conn.execute("""INSERT INTO managers (user_id,provider,provider_member_id) VALUES (%s,'espn',%s)
                            ON CONFLICT (user_id,provider,provider_member_id) DO UPDATE SET provider_member_id=EXCLUDED.provider_member_id RETURNING id""", (user, mid)).fetchone()[0]
                    conn.execute("""INSERT INTO manager_season_identities (league_season_id,manager_id,provider_team_id,identity_confidence)
                        VALUES (%s,%s,%s,'exact') ON CONFLICT (league_season_id,provider_team_id) DO UPDATE SET manager_id=EXCLUDED.manager_id,identity_confidence='exact'""", (sid, manager_ids[mid], tid))

        for season in seasons:
            sid = season_ids[season]
            draft = payload(run, season, "mDraftDetail")
            if draft:
                for pick in draft[0].get("draftDetail", {}).get("picks", []):
                    conn.execute("""INSERT INTO draft_picks (league_season_id,overall_pick,round,round_pick,provider_team_id,provider_player_id,bid_amount)
                        VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (league_season_id,overall_pick) DO UPDATE SET provider_team_id=EXCLUDED.provider_team_id,provider_player_id=EXCLUDED.provider_player_id,bid_amount=EXCLUDED.bid_amount""",
                        (sid, pick.get("overallPickNumber"), pick.get("roundId"), pick.get("roundPickNumber"), pick.get("teamId"), pick.get("playerId"), pick.get("bidAmount")))

        # Rosters, matchups, and transactions are optional by endpoint/season;
        # load every shape the completed scan actually returned.
        for season, period, data, _ in all_payloads(run, "mRoster"):
            if period is None:
                continue
            sid = season_ids[season]
            for team in data.get("teams", []):
                tid = team.get("id")
                if tid is None:
                    continue
                snap = conn.execute("""INSERT INTO roster_snapshots (league_season_id,scoring_period,provider_team_id) VALUES (%s,%s,%s)
                    ON CONFLICT (league_season_id,scoring_period,provider_team_id) DO UPDATE SET provider_team_id=EXCLUDED.provider_team_id RETURNING id""", (sid, period, tid)).fetchone()[0]
                entry_rows = []
                for entry in (team.get("roster") or {}).get("entries", []):
                    player = entry.get("playerId")
                    if player is None:
                        continue
                    entry_rows.append((snap, player, entry.get("lineupSlotId"), entry.get("acquisitionType"), (entry.get("playerPoolEntry") or {}).get("appliedStatTotal")))
                if entry_rows:
                    with conn.cursor() as cursor:
                        cursor.executemany("""INSERT INTO roster_entries (roster_snapshot_id,provider_player_id,lineup_slot_id,acquisition_type,applied_stat_total)
                        VALUES (%s,%s,%s,%s,%s) ON CONFLICT (roster_snapshot_id,provider_player_id,lineup_slot_id) DO UPDATE SET acquisition_type=EXCLUDED.acquisition_type,applied_stat_total=EXCLUDED.applied_stat_total""", entry_rows)

        seen: set[tuple[int, str]] = set()
        for season, period, data, _ in all_payloads(run, "mTransactions2"):
            sid = season_ids[season]
            for tx in data.get("transactions", []):
                txid = str(tx.get("id"))
                if not txid or (season, txid) in seen:
                    continue
                seen.add((season, txid))
                row = conn.execute("""INSERT INTO transactions (league_season_id,provider_transaction_id,scoring_period,provider_type,status,category,provider_team_id,provider_member_id,bid_amount,process_date,proposed_date)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (league_season_id,provider_transaction_id) DO UPDATE SET status=EXCLUDED.status,category=EXCLUDED.category,bid_amount=EXCLUDED.bid_amount RETURNING id""",
                    (sid, txid, period, tx.get("type"), tx.get("status"), category(tx), tx.get("teamId"), tx.get("memberId"), tx.get("bidAmount"), millis(tx.get("processDate")), millis(tx.get("proposedDate")))).fetchone()[0]
                conn.execute("DELETE FROM transaction_items WHERE transaction_id=%s", (row,))
                for item in tx.get("items") or []:
                    conn.execute("""INSERT INTO transaction_items (transaction_id,item_type,provider_player_id,from_provider_team_id,to_provider_team_id)
                        VALUES (%s,%s,%s,%s,%s)""", (row, item.get("type"), item.get("playerId"), item.get("fromTeamId"), item.get("toTeamId")))
        conn.commit()
        counts = {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("league_seasons", "teams", "roster_snapshots", "roster_entries", "draft_picks", "transactions", "transaction_items")}
        print(json.dumps({"run": run.name, "counts": counts}, indent=2))


if __name__ == "__main__":
    main()
