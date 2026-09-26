import pytest

from espn_ff_assistant.config import ConfigurationError, Settings

SECRET_ENV_VARS = (
    "ESPN_LEAGUE_ID",
    "ESPN_S2",
    "ESPN_SWID",
    "ESPN_FIRST_SEASON",
    "ESPN_LAST_SEASON",
)


@pytest.fixture(autouse=True)
def do_not_load_real_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("espn_ff_assistant.config.load_dotenv", lambda: None)


def test_reports_missing_names_without_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ConfigurationError, match="ESPN_LEAGUE_ID, ESPN_S2, ESPN_SWID"):
        Settings.from_env()


def test_loads_valid_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ESPN_LEAGUE_ID", "123")
    monkeypatch.setenv("ESPN_S2", "secret-cookie")
    monkeypatch.setenv("ESPN_SWID", "{abc}")
    monkeypatch.setenv("ESPN_FIRST_SEASON", "2014")
    monkeypatch.setenv("ESPN_LAST_SEASON", "2026")

    settings = Settings.from_env()

    assert settings.league_id == 123
    assert settings.first_season == 2014
    assert settings.last_season == 2026


def test_rejects_reversed_season_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ESPN_LEAGUE_ID", "123")
    monkeypatch.setenv("ESPN_S2", "secret-cookie")
    monkeypatch.setenv("ESPN_SWID", "{abc}")
    monkeypatch.setenv("ESPN_FIRST_SEASON", "2026")
    monkeypatch.setenv("ESPN_LAST_SEASON", "2013")

    with pytest.raises(ConfigurationError, match="must not exceed"):
        Settings.from_env()
