"""Small, raw-response-preserving ESPN Fantasy API client."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import requests

from .config import Settings


@dataclass(frozen=True)
class FetchResult:
    season: int
    view: str
    scoring_period: int | None
    url: str
    retrieved_at: str
    status_code: int | None
    payload: dict[str, Any] | None
    state: str
    error: str | None = None
    archive_key: str | None = None


class ESPNClient:
    """Read-only ESPN client with conservative retries and no credential logging."""

    root = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"

    def __init__(self, settings: Settings, *, delay: float = 0.25, timeout: float = 30.0):
        self.settings = settings
        self.delay = delay
        self.timeout = timeout
        self.session = requests.Session()
        self.session.cookies.update({"espn_s2": settings.espn_s2, "SWID": settings.swid})
        self._last_request = 0.0

    def _url(self, season: int, extend: str = "") -> str:
        if extend:
            return (
                f"{self.root}/seasons/{season}/segments/0/leagues/{self.settings.league_id}{extend}"
            )
        if season < 2018:
            return f"{self.root}/leagueHistory/{self.settings.league_id}?seasonId={season}"
        return f"{self.root}/seasons/{season}/segments/0/leagues/{self.settings.league_id}"

    def fetch(
        self,
        season: int,
        view: str,
        *,
        scoring_period: int | None = None,
        extend: str = "",
        extra_params: dict[str, Any] | None = None,
        fantasy_filter: dict[str, Any] | None = None,
        archive_key: str | None = None,
        game_level: bool = False,
    ) -> FetchResult:
        params: dict[str, Any] = {"view": view}
        if scoring_period is not None:
            params["scoringPeriodId"] = scoring_period
        if extra_params:
            params.update(extra_params)
        headers = {}
        if fantasy_filter:
            import json

            headers["X-Fantasy-Filter"] = json.dumps(fantasy_filter, separators=(",", ":"))

        url = f"{self.root}/seasons/{season}" if game_level else self._url(season, extend)
        retrieved_at = datetime.now(UTC).isoformat()
        response: requests.Response | None = None
        error: str | None = None
        for attempt in range(3):
            wait = self.delay - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            try:
                response = self.session.get(
                    url, params=params, headers=headers, timeout=self.timeout
                )
                self._last_request = time.monotonic()
            except requests.RequestException as exc:
                error = type(exc).__name__
                if attempt == 2:
                    return FetchResult(
                        season, view, scoring_period, url, retrieved_at, None, None, "error", error, archive_key
                    )
                time.sleep(2**attempt)
                continue
            if response.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
                break
            time.sleep(2**attempt)

        assert response is not None
        if response.status_code == 401 and "/leagueHistory/" in url:
            fallback = f"{self.root}/seasons/{season}/segments/0/leagues/{self.settings.league_id}"
            response = self.session.get(
                fallback, params=params, headers=headers, timeout=self.timeout
            )
        if response.status_code != 200:
            state = "unavailable" if response.status_code in (404, 410) else "error"
            return FetchResult(
                season,
                view,
                scoring_period,
                url,
                retrieved_at,
                response.status_code,
                None,
                state,
                f"HTTP {response.status_code}",
                archive_key,
            )
        if "json" not in response.headers.get("content-type", ""):
            return FetchResult(
                season,
                view,
                scoring_period,
                url,
                retrieved_at,
                response.status_code,
                None,
                "error",
                "non-JSON response",
                archive_key,
            )
        try:
            payload = response.json()
        except ValueError:
            return FetchResult(
                season,
                view,
                scoring_period,
                url,
                retrieved_at,
                response.status_code,
                None,
                "error",
                "invalid JSON",
                archive_key,
            )
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        if not isinstance(payload, dict):
            return FetchResult(
                season,
                view,
                scoring_period,
                url,
                retrieved_at,
                response.status_code,
                None,
                "error",
                "unexpected JSON shape",
                archive_key,
            )
        return FetchResult(
            season,
            view,
            scoring_period,
            url,
            retrieved_at,
            response.status_code,
            payload,
            "present",
            None,
            archive_key,
        )
