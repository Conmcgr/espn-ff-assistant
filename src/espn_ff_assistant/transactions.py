"""Normalize ESPN mTransactions2 records for feasibility analysis."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

TRADE_TYPES = {"TRADE_PROPOSAL", "TRADE_ACCEPT", "TRADE_DECLINE", "TRADE_VETO", "TRADE_UPHOLD"}


def category(transaction: dict[str, Any]) -> str:
    transaction_type = transaction.get("type")
    status = transaction.get("status")
    if transaction_type == "WAIVER":
        return "waiver_claim"
    if transaction_type == "FREEAGENT":
        return "free_agent_move"
    if transaction_type == "TRADE_ACCEPT" and status in {"EXECUTED", None}:
        return "completed_trade"
    if transaction_type == "TRADE_PROPOSAL":
        return "trade_proposal"
    if transaction_type in {"TRADE_DECLINE", "TRADE_VETO"}:
        return "trade_decline"
    if transaction_type in TRADE_TYPES:
        return "trade_event"
    if transaction_type in {"ROSTER", "FUTURE_ROSTER"}:
        return "lineup_change"
    if transaction_type == "DRAFT":
        return "draft_event"
    return "other"


def _row(season: int, period: int, transaction: dict[str, Any]) -> dict[str, Any]:
    items = transaction.get("items") or []
    item_counts = Counter(item.get("type", "UNKNOWN") for item in items)
    player_ids = sorted({item.get("playerId") for item in items if item.get("playerId") is not None})
    bid = transaction.get("bidAmount")
    return {
        "season": season,
        "scoring_period": period,
        "transaction_id": transaction.get("id"),
        "type": transaction.get("type"),
        "status": transaction.get("status") or "UNKNOWN",
        "category": category(transaction),
        "team_id": transaction.get("teamId"),
        "member_id": transaction.get("memberId"),
        "bid_amount": bid if isinstance(bid, (int, float)) else None,
        "process_date": transaction.get("processDate"),
        "proposed_date": transaction.get("proposedDate"),
        "item_count": len(items),
        "add_count": item_counts.get("ADD", 0),
        "drop_count": item_counts.get("DROP", 0),
        "trade_count": item_counts.get("TRADE", 0),
        "lineup_count": item_counts.get("LINEUP", 0),
        "player_ids": ",".join(str(player_id) for player_id in player_ids),
    }


def normalize_run(run_dir: Path) -> list[dict[str, Any]]:
    manifest = run_dir / "manifest.jsonl"
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in manifest.read_text().splitlines():
        entry = json.loads(line)
        if entry.get("view") != "mTransactions2" or not entry.get("raw_path"):
            continue
        payload = json.loads((run_dir / entry["raw_path"]).read_text())
        for transaction in payload.get("transactions", []):
            transaction_id = transaction.get("id")
            if transaction_id and transaction_id in seen:
                continue
            if transaction_id:
                seen.add(transaction_id)
            rows.append(_row(entry["season"], entry.get("scoring_period") or 0, transaction))
    return rows


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["season"], row["category"], row["status"])].append(row)
    output = []
    for (season, category_name, status), records in sorted(grouped.items()):
        bids = [record["bid_amount"] for record in records if isinstance(record["bid_amount"], (int, float))]
        nonzero = [bid for bid in bids if bid > 0]
        output.append(
            {
                "season": season,
                "category": category_name,
                "status": status,
                "record_count": len(records),
                "nonzero_faab_bid_count": len(nonzero),
                "faab_total": sum(nonzero),
                "faab_min": min(nonzero) if nonzero else None,
                "faab_max": max(nonzero) if nonzero else None,
            }
        )
    return output


def write_analysis(rows: list[dict[str, Any]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    row_fields = list(rows[0]) if rows else []
    with (output_dir / "transactions.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=row_fields)
        if row_fields:
            writer.writeheader()
            writer.writerows(rows)
    summary = summarize(rows)
    summary_fields = list(summary[0]) if summary else []
    with (output_dir / "transaction-summary.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=summary_fields)
        if summary_fields:
            writer.writeheader()
            writer.writerows(summary)
    (output_dir / "transaction-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
