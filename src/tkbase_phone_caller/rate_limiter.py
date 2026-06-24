"""Rate limiter with per-minute and daily caps.

Tracks call counts in a local JSON file so limits persist across restarts.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()


class RateLimitExceeded(Exception):
    """Raised when call rate or daily limit is exceeded."""


@dataclass
class _CallRecord:
    timestamps: list[float] = field(default_factory=list)
    daily_count: int = 0
    daily_date: str = ""


class RateLimiter:
    """Track and enforce phone call limits.

    Args:
        rate_limit: Max calls per minute (0 = unlimited).
        daily_limit: Max calls per day (0 = unlimited).
        state_path: File to persist daily counts across restarts.
    """

    def __init__(
        self,
        rate_limit: int = 5,
        daily_limit: int = 50,
        state_path: Optional[Path] = None,
    ):
        self.rate_limit = rate_limit
        self.daily_limit = daily_limit
        self._state_path = state_path
        self._record = _CallRecord()
        self._load_state()

    def _load_state(self) -> None:
        if not self._state_path or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text())
            self._record.daily_count = data.get("daily_count", 0)
            self._record.daily_date = data.get("daily_date", "")
        except (json.JSONDecodeError, OSError):
            pass

    def _save_state(self) -> None:
        if not self._state_path:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps({
            "daily_count": self._record.daily_count,
            "daily_date": self._record.daily_date,
        }))

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    def check(self) -> None:
        """Raise RateLimitExceeded if calling is not allowed right now."""
        now = time.time()
        today = self._today()

        if self._record.daily_date != today:
            self._record.daily_count = 0
            self._record.daily_date = today

        if self.daily_limit > 0 and self._record.daily_count >= self.daily_limit:
            raise RateLimitExceeded(
                f"Daily limit reached: {self._record.daily_count}/{self.daily_limit}"
            )

        if self.rate_limit > 0:
            cutoff = now - 60
            self._record.timestamps = [
                t for t in self._record.timestamps if t > cutoff
            ]
            if len(self._record.timestamps) >= self.rate_limit:
                raise RateLimitExceeded(
                    f"Rate limit reached: {len(self._record.timestamps)}/{self.rate_limit} per minute"
                )

    def record_call(self) -> None:
        """Record a successful call."""
        self._record.timestamps.append(time.time())
        self._record.daily_count += 1
        self._save_state()
        logger.debug(
            "rate_limiter.recorded",
            minute_count=len(self._record.timestamps),
            daily_count=self._record.daily_count,
            daily_limit=self.daily_limit,
        )

    @property
    def remaining_today(self) -> int:
        if self.daily_limit <= 0:
            return -1
        today = self._today()
        if self._record.daily_date != today:
            return self.daily_limit
        return max(0, self.daily_limit - self._record.daily_count)
