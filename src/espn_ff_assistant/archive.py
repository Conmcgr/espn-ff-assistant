"""Immutable raw response archive and credential-free manifest."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .espn_client import FetchResult


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


class RawArchive:
    def __init__(self, root: Path, run_id: str):
        self.run_dir = root / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.manifest = self.run_dir / "manifest.jsonl"
        self._payloads: dict[tuple[int, str, int | None, str | None], Path] = {}
        if self.manifest.exists():
            for line in self.manifest.read_text().splitlines():
                entry = json.loads(line)
                if entry.get("raw_path"):
                    key = (
                        entry["season"],
                        entry["view"],
                        entry.get("scoring_period"),
                        entry.get("archive_key"),
                    )
                    self._payloads[key] = self.run_dir / entry["raw_path"]

    def load(self, season: int, view: str, scoring_period: int | None = None, archive_key: str | None = None) -> dict | None:
        path = self._payloads.get((season, view, scoring_period, archive_key))
        if not path or not path.exists():
            return None
        return json.loads(path.read_text())

    def save(self, result: FetchResult) -> Path | None:
        relative = Path(str(result.season), _safe(result.archive_key or result.view))
        if result.scoring_period is not None:
            relative = relative / f"scoring-period-{result.scoring_period:02d}"
        path = self.run_dir / relative / "response.json"
        if result.payload is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(result.payload, indent=2, sort_keys=True) + "\n")
            temp.replace(path)
            self._payloads[(result.season, result.view, result.scoring_period, result.archive_key)] = path
        entry = {
            "season": result.season,
            "view": result.view,
            "scoring_period": result.scoring_period,
            "url": result.url,
            "retrieved_at": result.retrieved_at,
            "status_code": result.status_code,
            "state": result.state,
            "error": result.error,
            "archive_key": result.archive_key,
        }
        entry["raw_path"] = (
            str(path.relative_to(self.run_dir)) if result.payload is not None else None
        )
        entry.pop("payload", None)
        with self.manifest.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, sort_keys=True) + "\n")
        return path if result.payload is not None else None
