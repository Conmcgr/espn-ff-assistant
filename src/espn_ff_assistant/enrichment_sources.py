"""Credential-free public sources for opportunity enrichment.

Each dataset is fetched once per run (news once per shortlisted player). No
fantasy cookies, netrc credentials, or environment proxies are used.
"""

from __future__ import annotations

import csv
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from .enrichment import SourceBatch

NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download"
ESPN_NEWS = "https://site.api.espn.com/apis/fantasy/v2/games/ffl/news/players"


def _last_modified(response: requests.Response) -> datetime | None:
    header = response.headers.get("Last-Modified")
    try:
        return parsedate_to_datetime(header).astimezone(UTC) if header else None
    except (TypeError, ValueError):
        return None


def _error(exc: Exception) -> str:
    # Exception text can echo request URLs; keep only the type and status.
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return f"{type(exc).__name__}" + (f" HTTP {status}" if status else "")


class PublicSources:
    def __init__(self, *, news: bool = True, timeout: float = 60.0) -> None:
        self._news = news
        self._timeout = timeout
        self._session = requests.Session()
        self._session.trust_env = False

    def close(self) -> None:
        self._session.close()

    def _csv(self, url: str, keep: Callable[[Mapping[str, Any]], bool]) -> SourceBatch:
        retrieved_at = datetime.now(UTC)
        try:
            with self._session.get(url, timeout=self._timeout, stream=True) as response:
                response.raise_for_status()
                response.encoding = "utf-8"
                rows = [r for r in csv.DictReader(response.iter_lines(decode_unicode=True))
                        if keep(r)]
                modified = _last_modified(response)
        except (requests.RequestException, csv.Error, UnicodeDecodeError) as exc:
            return SourceBatch("failed", retrieved_at=retrieved_at, url=url, error=_error(exc))
        return SourceBatch("present" if rows else "empty", rows, retrieved_at, modified, url)

    @staticmethod
    def _espn_id(wanted: set[int]) -> Callable[[Mapping[str, Any]], bool]:
        names = {str(pid) for pid in wanted}
        return lambda row: (row.get("espn_id") or "").split(".")[0] in names

    def players(self, wanted: set[int]) -> SourceBatch:
        return self._csv(f"{NFLVERSE}/players/players.csv", self._espn_id(wanted))

    def depth_charts(self, season: int, wanted: set[int]) -> SourceBatch:
        return self._csv(
            f"{NFLVERSE}/depth_charts/depth_charts_{season}.csv", self._espn_id(wanted))

    def injuries(self, season: int) -> SourceBatch:
        return self._csv(f"{NFLVERSE}/injuries/injuries_{season}.csv", lambda _: True)

    def snap_counts(self, season: int) -> SourceBatch:
        return self._csv(f"{NFLVERSE}/snap_counts/snap_counts_{season}.csv", lambda _: True)

    def news(self, provider_player_id: int) -> SourceBatch:
        url = f"{ESPN_NEWS}?playerId={provider_player_id}"
        if not self._news:
            return SourceBatch("disabled", url=url)
        retrieved_at = datetime.now(UTC)
        try:
            response = self._session.get(
                ESPN_NEWS, params={"playerId": provider_player_id, "limit": 10},
                timeout=self._timeout,
            )
            response.raise_for_status()
            feed = response.json().get("feed")
        except (requests.RequestException, ValueError, AttributeError) as exc:
            return SourceBatch("failed", retrieved_at=retrieved_at, url=url, error=_error(exc))
        if not isinstance(feed, list):
            return SourceBatch("failed", retrieved_at=retrieved_at, url=url, error="no feed list")
        items = [item for item in feed if isinstance(item, dict)]
        return SourceBatch("present" if items else "empty", items, retrieved_at, None, url)
