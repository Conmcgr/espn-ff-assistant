"""Derive a compact coverage matrix from an archive manifest."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

EXPECTED_KEYS = {
    "mStatus": "status",
    "mSettings": "settings",
    "mTeam": "teams",
    "mDraftDetail": "draftDetail",
    "mSchedule": "schedule",
    "mStandings": "teams",
    "mRoster": "teams",
    "mBoxscore": "schedule",
    "mTransactions2": "transactions",
    "kona_league_communication": "topics",
}


def _content_state(item: dict[str, Any], manifest: Path) -> str:
    if item["state"] != "present":
        return item["state"]
    raw_path = item.get("raw_path")
    if not raw_path:
        return item["state"]
    payload = json.loads((manifest.parent / raw_path).read_text())
    key = EXPECTED_KEYS.get(item["view"])
    if key not in payload:
        return "missing"
    if not payload[key]:
        return "empty"
    return "present"


def build_coverage(manifest: Path) -> list[dict[str, Any]]:
    by_season: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"season": None, "views": {}, "periods": set(), "errors": []}
    )
    for line in manifest.read_text().splitlines():
        item = json.loads(line)
        row = by_season[str(item["season"])]
        row["season"] = item["season"]
        state = _content_state(item, manifest)
        view = item["view"]
        row["views"][view] = row["views"].get(view, {"present": 0, "error": 0, "unavailable": 0})
        row["views"][view][state] = row["views"][view].get(state, 0) + 1
        if item.get("scoring_period") is not None:
            row["periods"].add(item["scoring_period"])
        if item.get("error"):
            row["errors"].append(f"{view}:{item['error']}")
    output = []
    for row in sorted(by_season.values(), key=lambda value: value["season"]):
        flat: dict[str, Any] = {
            "season": row["season"],
            "scoring_periods_requested": len(row["periods"]),
            "errors": "; ".join(sorted(set(row["errors"]))),
        }
        for view, counts in sorted(row["views"].items()):
            for state, count in counts.items():
                flat[f"{view}_{state}"] = count
        output.append(flat)
    return output


def write_coverage(rows: list[dict[str, Any]], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    columns = sorted({key for row in rows for key in row})
    with destination.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
