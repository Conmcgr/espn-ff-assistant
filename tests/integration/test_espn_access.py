"""Read-only smoke test for the current private ESPN league."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import pytest
import requests

from espn_ff_assistant.config import Settings

RUN_LIVE_TESTS = os.getenv("RUN_ESPN_INTEGRATION") == "1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not RUN_LIVE_TESTS,
        reason="set RUN_ESPN_INTEGRATION=1 to make read-only ESPN requests",
    ),
]

ESPN_API_ROOT = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings.from_env()


@pytest.fixture(scope="module")
def fetch_view(settings: Settings) -> Callable[[str], dict[str, Any]]:
    url = f"{ESPN_API_ROOT}/seasons/{settings.last_season}/segments/0/leagues/{settings.league_id}"
    session = requests.Session()
    session.cookies.update({"espn_s2": settings.espn_s2, "SWID": settings.swid})

    def fetch(view: str) -> dict[str, Any]:
        try:
            response = session.get(url, params={"view": view}, timeout=30)
        except requests.RequestException as error:
            pytest.fail(
                f"ESPN request for {view} failed: {type(error).__name__}",
                pytrace=False,
            )

        assert response.status_code == 200, (
            f"ESPN returned HTTP {response.status_code} for {view}; "
            "check the league ID, season, and cookie freshness"
        )
        assert "application/json" in response.headers.get("content-type", ""), (
            f"ESPN returned a non-JSON response for {view}; authentication may have expired"
        )

        payload = response.json()
        assert isinstance(payload, dict), f"Expected an object response for {view}"
        return payload

    return fetch


def test_status_identifies_requested_league(
    settings: Settings,
    fetch_view: Callable[[str], dict[str, Any]],
) -> None:
    payload = fetch_view("mStatus")

    assert payload.get("id") == settings.league_id
    assert payload.get("seasonId") == settings.last_season
    assert payload.get("status"), "mStatus did not contain league status"


def test_settings_are_available(fetch_view: Callable[[str], dict[str, Any]]) -> None:
    payload = fetch_view("mSettings")

    league_settings = payload.get("settings")
    assert isinstance(league_settings, dict) and league_settings
    assert league_settings.get("name"), "League settings did not contain a league name"
    assert league_settings.get("rosterSettings"), "Roster settings were missing"
    assert league_settings.get("scoringSettings"), "Scoring settings were missing"


def test_all_twelve_teams_and_owners_are_available(
    fetch_view: Callable[[str], dict[str, Any]],
) -> None:
    payload = fetch_view("mTeam")

    teams = payload.get("teams")
    assert isinstance(teams, list)
    assert len(teams) == 12, f"Expected 12 teams, received {len(teams)}"
    assert all(team.get("id") for team in teams), "At least one team is missing its ID"
    assert all(team.get("owners") for team in teams), "At least one team is missing owner IDs"


def test_current_rosters_are_available(fetch_view: Callable[[str], dict[str, Any]]) -> None:
    payload = fetch_view("mRoster")

    teams = payload.get("teams")
    assert isinstance(teams, list) and len(teams) == 12
    missing_rosters = [
        team.get("id") for team in teams if not team.get("roster", {}).get("entries")
    ]
    assert not missing_rosters, f"Teams missing roster entries: {missing_rosters}"


def test_matchups_are_available(fetch_view: Callable[[str], dict[str, Any]]) -> None:
    payload = fetch_view("mMatchupScore")

    schedule = payload.get("schedule")
    assert isinstance(schedule, list) and schedule, "No matchup schedule was returned"
    assert any(matchup.get("home") and matchup.get("away") for matchup in schedule), (
        "Schedule did not contain a head-to-head matchup"
    )


def test_draft_is_available(fetch_view: Callable[[str], dict[str, Any]]) -> None:
    payload = fetch_view("mDraftDetail")

    draft = payload.get("draftDetail")
    assert isinstance(draft, dict), "Draft detail was missing"
    assert draft.get("drafted") is True, "ESPN does not mark the current season as drafted"
    assert draft.get("picks"), "Draft detail did not contain any picks"
