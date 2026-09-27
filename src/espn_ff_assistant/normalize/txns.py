"""Parse mTransactions2 payloads into transaction and item rows.

Pure functions; no I/O. Wraps the existing category() helper.
"""

from __future__ import annotations

from typing import Any

from espn_ff_assistant.normalize.common import millis
from espn_ff_assistant.transactions import category


def _acting_team(tx: dict[str, Any]) -> int | None:
    """ESPN sometimes sends a negative sentinel teamId; fall back to the team receiving an ADD."""
    team_id = tx.get("teamId")
    if isinstance(team_id, int) and team_id > 0:
        return team_id
    for item in tx.get("items") or []:
        to_team = item.get("toTeamId")
        if item.get("type") == "ADD" and isinstance(to_team, int) and to_team > 0:
            return to_team
    return None


def transactions(
    season: int, period: int, data: dict[str, Any]
) -> list[dict[str, Any]]:
    """Extract transaction rows from one scoring-period mTransactions2 payload."""
    rows = []
    for tx in data.get("transactions") or []:
        txid = str(tx.get("id") or "")
        if not txid:
            continue
        rows.append(
            {
                "season": season,
                "provider_transaction_id": txid,
                "scoring_period": period,
                "provider_type": tx.get("type"),
                "status": tx.get("status"),
                "category": category(tx),
                "provider_team_id": _acting_team(tx),
                "provider_member_id": tx.get("memberId"),
                "bid_amount": tx.get("bidAmount"),
                "process_date": millis(tx.get("processDate")),
                "proposed_date": millis(tx.get("proposedDate")),
                "items": tx.get("items") or [],
            }
        )
    return rows


def transaction_items(tx_row: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract item rows for a single transaction dict (as returned by transactions())."""
    rows = []
    for item in tx_row.get("items") or []:
        rows.append(
            {
                "item_type": item.get("type"),
                "provider_player_id": item.get("playerId"),
                "from_provider_team_id": item.get("fromTeamId"),
                "to_provider_team_id": item.get("toTeamId"),
            }
        )
    return rows
