"""Apply managed PostgreSQL migrations."""

from __future__ import annotations

from espn_ff_assistant.config import ConfigurationError
from espn_ff_assistant.database import apply_migrations


def main() -> int:
    try:
        count = apply_migrations()
    except ConfigurationError as error:
        print(f"Configuration error: {error}")
        return 2
    print(f"Applied {count} database migration(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

