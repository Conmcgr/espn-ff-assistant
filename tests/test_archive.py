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


def test_scoreboard_archive_preserves_season_type(tmp_path):
    archive = RawArchive(tmp_path, "scoreboard-run")
    result = FetchResult(
        season=2026, view="scoreboard", scoring_period=10,
        url="https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
        retrieved_at="2026-11-19T20:48:00+00:00", status_code=200,
        payload={"events": []}, state="present",
        archive_key="scoreboard-type-2", season_type=2,
    )
    archive.save(result)
    manifest = json.loads(archive.manifest.read_text())
    assert manifest["season_type"] == 2
    assert manifest["scoring_period"] == 10
