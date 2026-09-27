"""Managed PostgreSQL connection and migration helpers."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .config import ConfigurationError


def database_url() -> str:
    """Return the configured database URL without exposing it in errors."""
    load_dotenv()
    value = os.getenv("DATABASE_URL")
    if not value:
        raise ConfigurationError("Missing required environment variable: DATABASE_URL")
    # Some managed Postgres endpoints currently publish only an AAAA record,
    # while the local resolver may not return it.  libpq supports an explicit
    # hostaddr while retaining the hostname for TLS/SNI.
    hostaddr = os.getenv("DATABASE_HOSTADDR")
    if hostaddr and "hostaddr=" not in value:
        separator = "&" if "?" in value else "?"
        value = f"{value}{separator}hostaddr={hostaddr}"
    return value


@contextmanager
def connection() -> Iterator[Any]:
    """Open a managed connection; callers own transaction boundaries."""
    try:
        import psycopg
    except ImportError as error:
        raise ConfigurationError(
            "PostgreSQL support is not installed; run `uv sync --extra dev`"
        ) from error
    with psycopg.connect(database_url()) as conn:
        yield conn


def apply_migrations(migrations_dir: Path = Path("migrations")) -> int:
    """Apply ordered SQL migrations exactly once and return applied count."""
    migrations = sorted(migrations_dir.glob("*.sql"))
    with connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        count = 0
        for migration in migrations:
            if migration.name in applied:
                continue
            conn.execute(migration.read_text())
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (migration.name,))
            count += 1
        conn.commit()
    return count
