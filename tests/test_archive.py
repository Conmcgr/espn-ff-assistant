import json

from espn_ff_assistant.archive import RawArchive
from espn_ff_assistant.espn_client import FetchResult


def test_archive_writes_payload_and_credential_free_manifest(tmp_path) -> None:
    archive = RawArchive(tmp_path, "run-1")
    result = FetchResult(
        season=2026,
        view="mStatus",
        scoring_period=None,
        url="https://example.test/league",
        retrieved_at="2026-01-01T00:00:00+00:00",
        status_code=200,
        payload={"id": 123, "status": {"previousSeasons": [2025]}},
        state="present",
        archive_key="status",
    )

    path = archive.save(result)

    assert path is not None and path.read_text().startswith("{")
    manifest = json.loads(archive.manifest.read_text())
    assert manifest["raw_path"] == "2026/status/response.json"
    assert "payload" not in manifest
    assert "espn_s2" not in archive.manifest.read_text()

