from unittest.mock import Mock

import pytest
import requests

from espn_ff_assistant.config import Settings
from espn_ff_assistant.espn_client import ESPNClient, fetch_scoreboard


def make_settings() -> Settings:
    return Settings(league_id=123, espn_s2="cookie", swid="{swid}")


def test_legacy_url_and_array_response(monkeypatch) -> None:
    response = Mock(status_code=200, headers={"content-type": "application/json"})
    response.json.return_value = [{"seasonId": 2017, "teams": []}]
    client = ESPNClient(make_settings(), delay=0)
    get = Mock(return_value=response)
    monkeypatch.setattr(client.session, "get", get)

    result = client.fetch(2017, "mSettings")

    assert result.state == "present"
    assert result.payload == {"seasonId": 2017, "teams": []}
    assert "/leagueHistory/123?seasonId=2017" in result.url


def test_non_json_response_is_safe(monkeypatch) -> None:
    response = Mock(status_code=200, headers={"content-type": "text/html"})
    client = ESPNClient(make_settings(), delay=0)
    monkeypatch.setattr(client.session, "get", Mock(return_value=response))

    result = client.fetch(2026, "mStatus")

    assert result.state == "error"
    assert result.error == "non-JSON response"



def test_game_level_url_omits_league(monkeypatch) -> None:
    response = Mock(status_code=200, headers={"content-type": "application/json"})
    response.json.return_value = {"settings": {"proTeams": []}}
    client = ESPNClient(make_settings(), delay=0)
    monkeypatch.setattr(client.session, "get", Mock(return_value=response))

    result = client.fetch(2026, "proTeamSchedules_wl", game_level=True)

    assert result.url.endswith("/seasons/2026")
    assert "leagues" not in result.url


def scoreboard_transport(monkeypatch, response):
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    session.get.return_value = response
    monkeypatch.setattr(requests, "Session", Mock(return_value=session))
    return session


def test_scoreboard_public_transport_and_metadata(monkeypatch):
    response = Mock(status_code=200, headers={"content-type": "application/json"})
    response.json.return_value = {"events": []}
    session = scoreboard_transport(monkeypatch, response)
    result = fetch_scoreboard(2026, 10, season_type=2)
    assert result.state == "present"
    assert result.payload == {"events": []}
    assert (result.season, result.scoring_period, result.season_type) == (2026, 10, 2)
    assert result.retrieved_at.endswith("+00:00")
    assert result.status_code == 200
    assert result.view == "scoreboard"
    assert result.archive_key == "scoreboard-type-2"
    assert session.trust_env is False
    session.cookies.update.assert_not_called()
    session.get.assert_called_once_with(
        "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
        params={"dates": 2026, "week": 10, "seasontype": 2}, timeout=30.0,
    )


@pytest.mark.parametrize(("status", "content_type", "data", "error", "state"), [
    (404, "application/json", {}, "HTTP 404", "unavailable"),
    (500, "application/json", {}, "HTTP 500", "error"),
    (200, "text/html", {}, "non-JSON response", "error"),
    (200, "application/json", [], "unexpected JSON shape", "error"),
])
def test_scoreboard_response_errors(monkeypatch, status, content_type, data, error, state):
    response = Mock(status_code=status, headers={"content-type": content_type})
    response.json.return_value = data
    scoreboard_transport(monkeypatch, response)
    result = fetch_scoreboard(2026, 10)
    assert result.state == state
    assert result.error == error
    assert result.payload is None


def test_scoreboard_invalid_json_and_transport_error(monkeypatch):
    response = Mock(status_code=200, headers={"content-type": "application/json"})
    response.json.side_effect = ValueError("private response content")
    session = scoreboard_transport(monkeypatch, response)
    assert fetch_scoreboard(2026, 10).error == "invalid JSON"
    session.get.side_effect = requests.Timeout("private transport detail")
    result = fetch_scoreboard(2026, 10)
    assert result.error == "Timeout"
    assert result.state == "error"
    assert result.status_code is None
