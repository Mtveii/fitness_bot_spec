import asyncio
import time
from collections import defaultdict


class Debouncer:
    def __init__(self, delay: float = 1.0):
        self.delay = delay
        self._tasks = {}

    async def debounce(self, key: str, callback, *args, **kwargs):
        if key in self._tasks:
            self._tasks[key].cancel()
        async def _run():
            await asyncio.sleep(self.delay)
            await callback(*args, **kwargs)
        self._tasks[key] = asyncio.create_task(_run())


class RateLimiter:
    def __init__(self, max_calls: int = 10, interval: float = 60.0):
        self.max_calls = max_calls
        self.interval = interval
        self._buckets = defaultdict(list)

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        bucket = self._buckets[key]
        bucket[:] = [t for t in bucket if now - t < self.interval]
        if len(bucket) >= self.max_calls:
            return False
        bucket.append(now)
        return True
