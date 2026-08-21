from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """Async token-bucket rate limiter.

    The bucket starts full and refills at `rate_per_min / 60` tokens per
    second. Each `acquire()` consumes one token; when no tokens remain the
    caller is suspended until the bucket has refilled enough.

    Use as:
        bucket = TokenBucket(rate_per_min=60)
        await bucket.acquire()
        ...call API...
    """

    def __init__(self, rate_per_min: int, capacity: int | None = None) -> None:
        if rate_per_min <= 0:
            raise ValueError("rate_per_min must be > 0")
        self.capacity = float(capacity if capacity is not None else rate_per_min)
        self.tokens = self.capacity
        self.refill_rate = rate_per_min / 60.0
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self._last = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            wait = (1 - self.tokens) / self.refill_rate
        await asyncio.sleep(wait)
        return await self.acquire()