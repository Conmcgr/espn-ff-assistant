"""Shared helpers for payload normalizers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def millis(value: Any) -> datetime | None:
    """Convert ESPN epoch-millisecond timestamp to UTC datetime, or None."""
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)
