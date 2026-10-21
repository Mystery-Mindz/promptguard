"""A minimal in-process token-bucket rate limiter, keyed per caller (an
API key, or a client IP where no key exists yet -- e.g. before login).
Used by `POST /guard/check` (keyed by `X-PromptGuard-Key`) and
`POST /auth/login` (keyed by client IP) -- see `config.py`'s
`RATE_LIMIT_*` values and `app/guard.py`/`app/main.py` for where each
limiter is wired in.

In-process only: state lives in a plain dict on this module, so this is
correct for a single backend process but would need a shared store
(Redis, etc.) behind multiple instances.
"""

import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class TokenBucketLimiter:
    """`capacity` tokens are available immediately per new caller (the
    burst size), refilling at `refill_per_second` tokens/sec afterward, up
    to `capacity`. Each `allow(key)` call costs one token if available;
    returns `False` (costing nothing) if the caller's bucket is empty.
    """

    def __init__(self, capacity: float, refill_per_second: float) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self._buckets: dict[str, _Bucket] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=self.capacity, last_refill=now)
            self._buckets[key] = bucket
        else:
            elapsed = now - bucket.last_refill
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_per_second)
            bucket.last_refill = now

        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return True
        return False

    def reset(self) -> None:
        """Clears every caller's bucket. Test isolation only -- a real
        caller's budget should never reset mid-run."""
        self._buckets.clear()
