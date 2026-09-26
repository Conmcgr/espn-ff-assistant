"""Validate local configuration without contacting ESPN or printing secrets."""

from espn_ff_assistant.config import ConfigurationError, Settings


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigurationError as error:
        print(f"Configuration error: {error}")
        return 1

    print(
        "Configuration is valid for league "
        f"{settings.league_id} and seasons {settings.first_season}-{settings.last_season}."
    )
    print("ESPN credentials are present and were not displayed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
