"""Unit tests for transaction normalizer — synthetic payloads, no I/O."""

from __future__ import annotations

from espn_ff_assistant.normalize.txns import transaction_items, transactions


def _tx(txid, tx_type="WAIVER", status="EXECUTED", bid=25, items=None):
    return {
        "id": txid,
        "type": tx_type,
        "status": status,
        "teamId": 3,
        "memberId": "abc123",
        "bidAmount": bid,
        "processDate": 1700000000000,
        "proposedDate": 1699900000000,
        "items": items or [],
    }


def _item(player_id, from_tid, to_tid, item_type="ADD"):
    return {"type": item_type, "playerId": player_id, "fromTeamId": from_tid, "toTeamId": to_tid}


def test_transaction_basic_fields():
    data = {"transactions": [_tx("tx-1", bid=15)]}
    rows = transactions(2024, 5, data)
    assert len(rows) == 1
    row = rows[0]
    assert row["provider_transaction_id"] == "tx-1"
    assert row["scoring_period"] == 5
    assert row["bid_amount"] == 15
    assert row["provider_team_id"] == 3
    assert row["provider_member_id"] == "abc123"


def test_transaction_category_waiver():
    data = {"transactions": [_tx("tx-2", tx_type="WAIVER", status="EXECUTED")]}
    rows = transactions(2024, 5, data)
    assert rows[0]["category"] == "waiver_claim"


def test_transaction_skips_empty_id():
    data = {"transactions": [{"id": None, "type": "WAIVER"}]}
    rows = transactions(2024, 5, data)
    assert rows == []


def test_transaction_timestamps_converted():
    data = {"transactions": [_tx("tx-3")]}
    rows = transactions(2024, 5, data)
    assert rows[0]["process_date"] is not None
    assert rows[0]["proposed_date"] is not None


def test_transaction_items_extracted():
    tx = _tx("tx-4", items=[_item(100, 0, 5)])
    data = {"transactions": [tx]}
    rows = transactions(2024, 5, data)
    items = transaction_items(rows[0])
    assert len(items) == 1
    assert items[0]["provider_player_id"] == 100
    assert items[0]["to_provider_team_id"] == 5


def test_transaction_items_empty():
    items = transaction_items({"items": []})
    assert items == []


def test_no_transactions_key():
    rows = transactions(2024, 5, {})
    assert rows == []
