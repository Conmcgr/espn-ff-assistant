import json

from espn_ff_assistant.coverage import build_coverage, write_coverage


def test_coverage_groups_views_and_periods(tmp_path) -> None:
    manifest = tmp_path / "manifest.jsonl"
    entries = [
        {"season": 2026, "view": "mRoster", "scoring_period": 1, "state": "present"},
        {"season": 2026, "view": "mRoster", "scoring_period": 2, "state": "error", "error": "HTTP 500"},
    ]
    manifest.write_text("".join(json.dumps(entry) + "\n" for entry in entries))

    rows = build_coverage(manifest)
    destination = tmp_path / "coverage.csv"
    write_coverage(rows, destination)

    assert rows[0]["scoring_periods_requested"] == 2
    assert rows[0]["mRoster_present"] == 1
    assert rows[0]["mRoster_error"] == 1
    assert "HTTP 500" in destination.read_text()


def test_coverage_distinguishes_missing_dataset_key(tmp_path) -> None:
    raw = tmp_path / "response.json"
    raw.write_text(json.dumps({"status": {}}))
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "season": 2013,
                "view": "mTransactions2",
                "scoring_period": 1,
                "state": "present",
                "raw_path": "response.json",
            }
        )
        + "\n"
    )

    rows = build_coverage(manifest)

    assert rows[0]["mTransactions2_missing"] == 1
