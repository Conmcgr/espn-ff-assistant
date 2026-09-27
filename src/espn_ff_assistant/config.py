"""Local configuration with credential-safe validation."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """Raised when required local configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    league_id: int
    espn_s2: str
    swid: str
    first_season: int = 2013
    last_season: int = 2026

    @classmethod
    def from_env(cls) -> Settings:
        """Load settings without ever including secret values in errors."""
        load_dotenv()
        required = ("ESPN_LEAGUE_ID", "ESPN_S2", "ESPN_SWID")
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            names = ", ".join(missing)
            raise ConfigurationError(f"Missing required environment variables: {names}")

        try:
            league_id = int(os.environ["ESPN_LEAGUE_ID"])
            first_season = int(os.getenv("ESPN_FIRST_SEASON", "2013"))
            last_season = int(os.getenv("ESPN_LAST_SEASON", "2026"))
        except ValueError as error:
            raise ConfigurationError("League ID and season values must be integers") from error

        if first_season > last_season:
            raise ConfigurationError("ESPN_FIRST_SEASON must not exceed ESPN_LAST_SEASON")

        return cls(
            league_id=league_id,
            espn_s2=os.environ["ESPN_S2"],
            swid=os.environ["ESPN_SWID"],
            first_season=first_season,
            last_season=last_season,
        )
