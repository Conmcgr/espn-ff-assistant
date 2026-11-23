"""Private archive storage; bucket access policies are managed by the operator."""

from __future__ import annotations

import gzip
import json
import logging
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)


def safe_key(key: str) -> str:
    if not key or key.startswith("/") or any(p in {".", ".."} for p in key.split("/")) or "\\" in key:
        raise ValueError("Invalid object key")
    return key


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def list(self, prefix: str) -> list[str]: ...
    def delete(self, key: str) -> None: ...
    def uri(self, key: str) -> str: ...


class LocalObjectStore:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / safe_key(key)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Object key escapes storage root")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list(self, prefix: str) -> list[str]:
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file() and str(p.relative_to(self.root)).startswith(prefix))

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def uri(self, key: str) -> str:
        return self._path(key).as_uri()


class GCSObjectStore:
    def __init__(self, bucket: str, prefix: str = "", client=None):
        if client is None:
            try:
                from google.cloud import storage
            except ImportError:
                raise ImportError("GCS archives require espn-ff-assistant[gcs]") from None
            client = storage.Client()
        self.bucket = client.bucket(bucket)
        self.prefix = prefix.strip("/")
        if self.prefix:
            safe_key(self.prefix)

    def _key(self, key: str) -> str:
        return "/".join(filter(None, (self.prefix, safe_key(key))))

    def put(self, key: str, data: bytes) -> None:
        self.bucket.blob(self._key(key)).upload_from_string(data)

    def get(self, key: str) -> bytes:
        return self.bucket.blob(self._key(key)).download_as_bytes()

    def exists(self, key: str) -> bool:
        return self.bucket.blob(self._key(key)).exists()

    def list(self, prefix: str) -> list[str]:
        offset = len(self.prefix) + 1 if self.prefix else 0
        return sorted(blob.name[offset:] for blob in self.bucket.list_blobs(prefix=self._key(prefix)))

    def delete(self, key: str) -> None:
        self.bucket.blob(self._key(key)).delete()

    def uri(self, key: str) -> str:
        return f"gs://{self.bucket.name}/{self._key(key)}"


class PostgresObjectStore:
    """Gzipped objects in the application database's private `raw_objects` table."""

    def __init__(self, prefix: str = "", connect: Callable[[], AbstractContextManager[Any]] | None = None):
        self.prefix = prefix.strip("/")
        if self.prefix:
            safe_key(self.prefix)
        self._connect = connect

    def _key(self, key: str) -> str:
        return "/".join(filter(None, (self.prefix, safe_key(key))))

    @contextmanager
    def _repository(self) -> Iterator[Any]:
        from .database import connection
        from .repository import Repository

        with (self._connect or connection)() as conn:
            yield Repository(conn)
            conn.commit()

    def put(self, key: str, data: bytes) -> None:
        with self._repository() as repo:
            repo.put_raw_object(self._key(key), gzip.compress(data, mtime=0))

    def get(self, key: str) -> bytes:
        with self._repository() as repo:
            data = repo.raw_object(self._key(key))
        if data is None:
            raise FileNotFoundError(key)
        return gzip.decompress(data)

    def exists(self, key: str) -> bool:
        with self._repository() as repo:
            return repo.raw_object(self._key(key)) is not None

    def list(self, prefix: str) -> list[str]:
        offset = len(self.prefix) + 1 if self.prefix else 0
        with self._repository() as repo:
            return [key[offset:] for key in repo.raw_object_keys(self._key(prefix))]

    def delete(self, key: str) -> None:
        with self._repository() as repo:
            repo.delete_raw_object(self._key(key))

    def uri(self, key: str) -> str:
        return f"db:///{self._key(key)}"


def from_url(url: str) -> ObjectStore:
    parsed = urlsplit(url)
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("Archive URL must not contain credentials, query, or fragment")
    if parsed.scheme == "gs" and parsed.netloc:
        return GCSObjectStore(parsed.netloc, parsed.path)
    if parsed.scheme == "file" and not parsed.netloc and parsed.path.startswith("/"):
        return LocalObjectStore(Path(unquote(parsed.path)))
    if parsed.scheme == "db" and not parsed.netloc:
        return PostgresObjectStore(unquote(parsed.path))
    raise ValueError("Archive URL must be gs://bucket/prefix, file:///absolute/path or db:///prefix")


def prune_runs(store: ObjectStore, league_id: int | str, *, keep_days: int, keep_latest: int, now: datetime, dry_run: bool = False) -> list[str]:
    if keep_days < 0 or keep_latest < 0:
        raise ValueError("Retention limits must be nonnegative")
    if "/" in safe_key(str(league_id)):
        raise ValueError("Invalid league ID")
    prefix = f"leagues/{safe_key(str(league_id))}/runs/"
    keys = store.list(prefix)
    runs = []
    for key in keys:
        relative = PurePosixPath(key.removeprefix(prefix))
        if len(relative.parts) == 2 and relative.name == "run.json":
            created = datetime.fromisoformat(json.loads(store.get(key))["created_at"])
            runs.append((created, relative.parts[0]))
    runs.sort(reverse=True)
    pruned = []
    for created, run_id in runs[keep_latest:]:
        if created < now - timedelta(days=keep_days):
            run_prefix = f"{prefix}{run_id}/"
            if not dry_run:
                for key in keys:
                    if key.startswith(run_prefix):
                        store.delete(key)
            pruned.append(run_id)
    logger.info("Archive retention selected %d runs", len(pruned))
    return pruned
