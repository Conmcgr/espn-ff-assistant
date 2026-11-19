"""Immutable raw response archive and credential-free manifest."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from .espn_client import FetchResult
from .object_store import ObjectStore, safe_key

logger = logging.getLogger(__name__)


class ArchiveIntegrityError(ValueError):
    """An archived payload does not match its recorded digest."""


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


class RawArchive:
    def __init__(self, root: Path, run_id: str, *, league_id: int | str | None = None, store: ObjectStore | None = None):
        if store is not None and league_id is None:
            raise ValueError("league_id is required with an archive store")
        if "/" in safe_key(run_id):
            raise ValueError("Invalid run ID")
        if league_id is not None and "/" in safe_key(str(league_id)):
            raise ValueError("Invalid league ID")
        self.store = store
        self.prefix = f"leagues/{league_id}/runs/{run_id}/"
        self.run_dir = root / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.manifest = self.run_dir / "manifest.jsonl"
        if store is not None:
            remote_manifest = self.prefix + "manifest.jsonl"
            if store.exists(remote_manifest):
                content = store.get(remote_manifest)
                entries = [json.loads(line) for line in content.splitlines()]
                latest = {entry["raw_path"]: entry for entry in entries if entry.get("raw_path")}
                for entry in latest.values():
                    if not entry.get("raw_path"):
                        continue
                    path = self._path(entry["raw_path"])
                    if not path.exists():
                        data = store.get(self.prefix + entry["raw_path"])
                        self._verify(data, entry)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        temporary = path.with_suffix(".tmp")
                        temporary.write_bytes(data)
                        temporary.replace(path)
                    else:
                        self._verify(path.read_bytes(), entry)
                self.manifest.write_bytes(content)
                logger.info("Hydrated archive: %d entries", len(entries))
            run_key = self.prefix + "run.json"
            if not store.exists(run_key):
                store.put(run_key, json.dumps({"created_at": datetime.now(UTC).isoformat()}).encode())
        self._payloads: dict[tuple[int, str, int | None, str | None], Path] = {}
        self._entries: dict[Path, dict] = {}
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
                    path = self._path(entry["raw_path"])
                    self._payloads[key] = path
                    self._entries[path] = entry

    def _path(self, relative: str) -> Path:
        safe_key(relative)
        path = self.run_dir / relative
        if not path.resolve().is_relative_to(self.run_dir.resolve()):
            raise ArchiveIntegrityError("Archive path escapes run directory")
        return path

    @staticmethod
    def _verify(data: bytes, entry: dict) -> None:
        if entry.get("sha256") and hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ArchiveIntegrityError("Archive payload SHA-256 mismatch")

    def load(self, season: int, view: str, scoring_period: int | None = None, archive_key: str | None = None) -> dict | None:
        path = self._payloads.get((season, view, scoring_period, archive_key))
        if not path or not path.exists():
            return None
        data = path.read_bytes()
        self._verify(data, self._entries[path])
        return json.loads(data)

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
        if result.payload is not None:
            data = path.read_bytes()
            entry.update(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
            if self.store is not None:
                object_key = self.prefix + entry["raw_path"]
                self.store.put(object_key, data)
                entry["object_uri"] = self.store.uri(object_key)
            self._entries[path] = entry
        with self.manifest.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, sort_keys=True) + "\n")
        if self.store is not None:
            self.store.put(self.prefix + "manifest.jsonl", self.manifest.read_bytes())
        logger.info("Archived response: bytes=%s sha256=%s", entry.get("bytes", 0), entry.get("sha256"))
        return path if result.payload is not None else None
