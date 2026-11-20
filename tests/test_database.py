import sys

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


def test_connection_disables_prepared_statements(monkeypatch: pytest.MonkeyPatch) -> None:
    """Supabase's transaction-mode pooler (PgBouncer) cannot serve prepared
    statements; psycopg auto-prepares repeated queries and raises
    DuplicatePreparedStatement. connection() must disable that."""
    import espn_ff_assistant.database as database

    monkeypatch.setenv("DATABASE_URL", "postgresql://user:secret@example.test/db")

    captured: dict = {}

    class FakeConn:
        def __enter__(self) -> "FakeConn":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def fake_connect(url: str, **kwargs: object) -> FakeConn:
        captured["url"] = url
        captured.update(kwargs)
        return FakeConn()

    fake_psycopg = type(sys)("psycopg")
    fake_psycopg.connect = fake_connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    with database.connection():
        pass

    assert "prepare_threshold" in captured
    assert captured["prepare_threshold"] is None
