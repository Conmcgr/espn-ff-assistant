import pytest

from espn_ff_assistant.config import ConfigurationError
from espn_ff_assistant.database import database_url


@pytest.fixture(autouse=True)
def do_not_load_real_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("espn_ff_assistant.database.load_dotenv", lambda: None)


def test_database_url_requires_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ConfigurationError, match="DATABASE_URL"):
        database_url()


def test_database_url_does_not_validate_or_print_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    value = "postgresql://user:secret@example.test/db"
    monkeypatch.setenv("DATABASE_URL", value)

    assert database_url() == value
