"""Local request budgets, far below the provider ceilings.

The point is not to use the provider's allowance to the last call. Analytics must never starve
publishing, and a single misbehaving account must not use up the app's hourly allowance for every
other account. The numbers here are deliberately conservative:

| Provider | Local budget | Provider ceiling |
| --- | --- | --- |
| Facebook | 60 calls / hour / account | BUC-scored per app and Page |
| Instagram | 60 calls / hour / account | same family as Graph |
| TikTok | 6 calls / minute / account | 600 requests / minute / endpoint |

The state is in-process (a single local backend owns the queue), and the outcome of a run records the
remaining allowance so a human can see when the ceiling is being approached. A throttle that the
provider sends anyway is honoured by the adapter's retry policy and by `block_until`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ...db.enums import Platform


class BudgetExceeded(RuntimeError):
    """The local allowance for this account is gone; try again after `retry_after_s`."""

    code = "ANALYTICS_BUDGET_EXCEEDED"

    def __init__(self, platform: Platform, account_id: str, retry_after_s: float) -> None:
        super().__init__(
            f"{platform.value} budget for account {account_id} is exhausted; retry in {retry_after_s:.0f}s"
        )
        self.platform = platform
        self.account_id = account_id
        self.retry_after_s = retry_after_s


@dataclass(frozen=True, slots=True)
class Limit:
    calls: int
    window_s: int


LOCAL_LIMITS: dict[Platform, Limit] = {
    Platform.FACEBOOK: Limit(calls=60, window_s=3600),
    Platform.INSTAGRAM: Limit(calls=60, window_s=3600),
    Platform.TIKTOK: Limit(calls=6, window_s=60),
}


@dataclass(slots=True)
class _Bucket:
    window_started: float
    used: int = 0
    blocked_until: float = 0.0


@dataclass(slots=True)
class BudgetGovernor:
    """Token accounting per (platform, account). Injectable clock for tests."""

    clock: callable = time.monotonic
    limits: dict[Platform, Limit] = field(default_factory=lambda: dict(LOCAL_LIMITS))
    _buckets: dict[tuple[Platform, str], _Bucket] = field(default_factory=dict)

    def remaining(self, platform: Platform, account_id: str) -> int:
        limit = self.limits.get(platform) or Limit(calls=1, window_s=60)
        bucket = self._bucket(platform, account_id)
        self._roll(bucket, limit)
        return max(0, limit.calls - bucket.used)

    def acquire(self, platform: Platform, account_id: str, *, calls: int = 1) -> None:
        limit = self.limits.get(platform) or Limit(calls=1, window_s=60)
        bucket = self._bucket(platform, account_id)
        now = self.clock()
        if bucket.blocked_until > now:
            raise BudgetExceeded(platform, account_id, bucket.blocked_until - now)
        self._roll(bucket, limit)
        if bucket.used + calls > limit.calls:
            retry_after = max(1.0, limit.window_s - (now - bucket.window_started))
            raise BudgetExceeded(platform, account_id, retry_after)
        bucket.used += calls

    def block_until(self, platform: Platform, account_id: str, seconds: float) -> None:
        """Honour a provider `retry-after` for this account without sleeping in the worker."""
        bucket = self._bucket(platform, account_id)
        bucket.blocked_until = max(bucket.blocked_until, self.clock() + max(0.0, seconds))

    def reset(self, platform: Platform | None = None) -> None:
        for key in list(self._buckets):
            if platform is None or key[0] is platform:
                self._buckets.pop(key, None)

    # ------------------------------------------------------------------ internals
    def _bucket(self, platform: Platform, account_id: str) -> _Bucket:
        return self._buckets.setdefault((platform, account_id), _Bucket(window_started=self.clock()))

    def _roll(self, bucket: _Bucket, limit: Limit) -> None:
        now = self.clock()
        if now - bucket.window_started >= limit.window_s:
            bucket.window_started = now
            bucket.used = 0
