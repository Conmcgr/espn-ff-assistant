import json

from espn_ff_assistant.transactions import _row, category, normalize_run, summarize


def test_categories_distinguish_requested_event_types() -> None:
    assert category({"type": "WAIVER"}) == "waiver_claim"
    assert category({"type": "FREEAGENT"}) == "free_agent_move"
    assert category({"type": "TRADE_ACCEPT", "status": "EXECUTED"}) == "completed_trade"
    assert category({"type": "TRADE_ACCEPT", "status": None}) == "completed_trade"
    assert category({"type": "TRADE_PROPOSAL"}) == "trade_proposal"
    assert category({"type": "TRADE_DECLINE"}) == "trade_decline"
    assert category({"type": "ROSTER"}) == "lineup_change"
    assert category({"type": "DRAFT"}) == "draft_event"


def test_row_extracts_items_and_faab() -> None:
    row = _row(2026, 3, {"id": "tx-1", "type": "WAIVER", "status": "EXECUTED", "bidAmount": 7, "items": [{"type": "ADD", "playerId": 10}, {"type": "DROP", "playerId": 11}]})
    assert row["category"] == "waiver_claim"
    assert row["add_count"] == 1
    assert row["drop_count"] == 1
    assert row["bid_amount"] == 7
    assert row["player_ids"] == "10,11"


def test_normalize_deduplicates_transaction_ids_and_summarizes(tmp_path) -> None:
    run = tmp_path / "run"
    payload_dir = run / "2026" / "mTransactions2" / "scoring-period-03"
    payload_dir.mkdir(parents=True)
    payload = {"transactions": [{"id": "tx-1", "type": "WAIVER", "status": "EXECUTED", "bidAmount": 7, "items": []}, {"id": "tx-1", "type": "WAIVER", "status": "EXECUTED", "bidAmount": 7, "items": []}]}
    (payload_dir / "response.json").write_text(json.dumps(payload))
    (run / "manifest.jsonl").write_text(json.dumps({"season": 2026, "view": "mTransactions2", "scoring_period": 3, "raw_path": "2026/mTransactions2/scoring-period-03/response.json"}) + "\n")
    rows = normalize_run(run)
    summary = summarize(rows)
    assert len(rows) == 1
    assert summary[0]["record_count"] == 1
    assert summary[0]["nonzero_faab_bid_count"] == 1
    assert summary[0]["faab_total"] == 7
