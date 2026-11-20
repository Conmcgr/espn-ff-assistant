"""Check scheduled-tick health; exit 1 (and log ERROR) on missed runs,
stale syncs or repeated failures so Cloud Monitoring can alert.

    uv run python scripts/health_check.py
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

from espn_ff_assistant.config import ConfigurationError, Settings
from espn_ff_assistant.database import connection, load_dotenv
from espn_ff_assistant.repository import Repository
from espn_ff_assistant.tick import HealthPolicy, TickConfig, assess_health


def main() -> int:
    load_dotenv()
    try:
        settings = Settings.from_env()
        config = TickConfig.from_env(settings)
        policy = HealthPolicy(
            missed_after=timedelta(minutes=float(os.environ.get("HEALTH_MISSED_MINUTES", 45))),
            stale_after=timedelta(hours=float(os.environ.get("HEALTH_STALE_HOURS", 8))),
            failure_streak=int(os.environ.get("HEALTH_FAILURE_STREAK", 3)),
        )
        with connection() as conn:
            repo = Repository(conn)
            league = repo.league_id(settings.league_id, user_id=config.user_id)
            if league is None:
                raise ConfigurationError("league is not loaded for WORKER_USER_ID")
            ticks = repo.recent_worker_ticks(league, config.season, policy.failure_streak)
            last_scan = repo.latest_opportunity_observation(league, config.season)
    except (ConfigurationError, ValueError) as exc:
        print(json.dumps({"severity": "ERROR", "status": "CONFIGURATION_ERROR",
                          "error": str(exc)}))
        return 2
    problems = assess_health(datetime.now(UTC), ticks, last_scan, policy)
    print(json.dumps({
        "severity": "ERROR" if problems else "INFO",
        "message": "worker unhealthy: " + ", ".join(problems) if problems else "worker healthy",
        "problems": problems,
        "last_tick_at": ticks[0].started_at.isoformat() if ticks else None,
        "last_tick_status": ticks[0].status if ticks else None,
        "last_scan_at": last_scan.isoformat() if last_scan else None,
    }))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
