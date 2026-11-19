import json
import logging
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from espn_ff_assistant.archive import ArchiveIntegrityError, RawArchive
from espn_ff_assistant.espn_client import FetchResult
from espn_ff_assistant.object_store import GCSObjectStore, LocalObjectStore, from_url, prune_runs


class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    def upload_from_string(self, data):
        self.bucket.data[self.name] = data

    def download_as_bytes(self):
        return self.bucket.data[self.name]

    def exists(self):
        return self.name in self.bucket.data

    def delete(self):
        del self.bucket.data[self.name]


class FakeBucket:
    name = "private-synthetic"

    def __init__(self):
        self.data = {}

    def blob(self, name):
        return FakeBlob(self, name)

    def list_blobs(self, prefix):
        return [self.blob(key) for key in self.data if key.startswith(prefix)]


class FakeClient:
    def __init__(self):
        self.instance = FakeBucket()

    def bucket(self, name):
        return self.instance


@pytest.fixture(params=["local", "gcs"])
def store(request, tmp_path):
    if request.param == "local":
        return LocalObjectStore(tmp_path / "objects")
    return GCSObjectStore("private-synthetic", "archive", client=FakeClient())


def result(**kwargs):
    base = FetchResult(2026, "mStatus", None,
                       "https://example.test/fetch?espn_s2=synthetic-secret",
                       "2026-01-01T00:00:00+00:00", 200,
                       {"members": [{"id": "{ABCD-0000-SYNTHETIC}"}]}, "present")
    return replace(base, **kwargs)


def test_round_trip_restart_and_integrity(store, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    archive = RawArchive(tmp_path / "cache", "same-run", league_id=1, store=store)
    path = archive.save(result())
    entry = json.loads(archive.manifest.read_text())
    assert entry["url"] == result().url
    assert entry["error"] is None
    key = "leagues/1/runs/same-run/" + entry["raw_path"]
    assert entry["object_uri"] == store.uri(key)
    assert entry["bytes"] == len(store.get(key))
    assert len(entry["sha256"]) == 64
    assert archive.load(2026, "mStatus") == result().payload
    path.write_text("{}")
    with pytest.raises(ArchiveIntegrityError, match="SHA-256"):
        archive.load(2026, "mStatus")
    shutil.rmtree(archive.run_dir)
    restored = RawArchive(tmp_path / "cache", "same-run", league_id=1, store=store)
    assert restored.load(2026, "mStatus") == result().payload
    assert restored.load(2025, "missing") is None
    path.unlink()
    restored = RawArchive(tmp_path / "cache", "same-run", league_id=1, store=store)
    assert restored.load(2026, "mStatus") == result().payload
    shutil.rmtree(restored.run_dir)
    store.put(key, b"{}")
    with pytest.raises(ArchiveIntegrityError, match="SHA-256"):
        RawArchive(tmp_path / "cache", "same-run", league_id=1, store=store)
    for secret in ("{ABCD-0000-SYNTHETIC}", "synthetic-secret", "espn_s2", "example.test"):
        assert secret not in caplog.text


def test_namespace_and_retention(store, tmp_path):
    now = datetime(2026, 6, 1, tzinfo=UTC)
    for league in (1, 2):
        for index, age in enumerate((600, 500, 1)):
            archive = RawArchive(tmp_path / str(league), f"run-{index}", league_id=league, store=store)
            archive.save(result(payload={"league": league}))
            store.put(f"leagues/{league}/runs/run-{index}/run.json",
                      json.dumps({"created_at": (now - timedelta(days=age)).isoformat()}).encode())
    kwargs = dict(keep_days=400, keep_latest=2, now=now)
    before = store.list("leagues/2/")
    assert prune_runs(store, 1, dry_run=True, **kwargs) == ["run-0"]
    assert store.exists("leagues/1/runs/run-0/run.json")
    assert prune_runs(store, 1, **kwargs) == ["run-0"]
    assert not store.list("leagues/1/runs/run-0/")
    assert store.list("leagues/2/") == before
    assert prune_runs(store, 1, **kwargs) == []
    assert prune_runs(store, 3, **kwargs) == []
    assert prune_runs(store, 2, keep_days=400, keep_latest=0, now=now) == ["run-1", "run-0"]
    assert store.exists("leagues/2/runs/run-2/run.json")
    with pytest.raises(ValueError):
        prune_runs(store, 1, keep_days=-1, keep_latest=0, now=now)


def test_old_manifest_and_empty_failed(tmp_path):
    archive = RawArchive(tmp_path, "legacy")
    path = archive.save(result())
    entry = json.loads(archive.manifest.read_text())
    entry.pop("sha256")
    entry.pop("bytes")
    archive.manifest.write_text(json.dumps(entry) + "\n")
    path.write_text('{"legacy": true}')
    assert RawArchive(tmp_path, "legacy").load(2026, "mStatus") == {"legacy": True}
    archive = RawArchive(tmp_path, "states")
    archive.save(result(payload={}))
    assert archive.load(2026, "mStatus") == {}
    assert archive.save(result(payload=None, state="failed", error="Timeout")) is None
    entry = json.loads(archive.manifest.read_text().splitlines()[-1])
    assert entry["error"] == "Timeout"
    assert entry["url"] == result().url


def test_validation_and_file_factory(tmp_path):
    store = from_url(tmp_path.as_uri())
    store.put("nested/object", b"bytes")
    assert store.get("nested/object") == b"bytes"
    with pytest.raises(ValueError):
        store.put("../escape", b"x")
    with pytest.raises(ValueError):
        RawArchive(tmp_path, "run", store=store)
    with pytest.raises(ValueError):
        from_url("gs://bucket/path?secret=value")
