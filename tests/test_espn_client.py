from unittest.mock import Mock

from espn_ff_assistant.config import Settings
from espn_ff_assistant.espn_client import ESPNClient


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
