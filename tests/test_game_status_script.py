import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/game_status.py"


def run_script(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False,
    )


def test_saved_fixture():
    result = run_script(
        "--file", str(Path(__file__).parent / "fixtures/scoreboard.json"),
        "--observed-at", "2026-09-28T02:00:00+00:00",
    )
    assert result.returncode == 0
    assert "batch=present" in result.stdout
    assert "demo-6 espn_scoreboard final final_observed_at=2026-09-28T02:00:00+00:00" in result.stdout


def test_saved_empty_and_malformed(tmp_path):
    path = tmp_path / "scoreboard.json"
    path.write_text(json.dumps({"events": []}))
    args = ("--file", str(path), "--observed-at", "2026-09-28T02:00:00+00:00")
    result = run_script(*args)
    assert result.returncode == 0
    assert "batch=empty" in result.stdout
    path.write_text(json.dumps({"events": {}}))
    result = run_script(*args)
    assert result.returncode == 1
    assert "batch=malformed" in result.stdout


def test_saved_file_requires_retrieval_time():
    result = run_script("--file", "unused.json")
    assert result.returncode == 2
    assert "--file requires --observed-at" in result.stderr
