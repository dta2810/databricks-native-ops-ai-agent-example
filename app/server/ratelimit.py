"""Per-user token bucket for chat turns (in-process: one bucket set per app worker)."""
import threading
import time


class TokenBucket:
    def __init__(self, per_minute: float, clock=time.monotonic):
        self.rate = per_minute / 60.0
        self.capacity = max(per_minute, 1.0)
        self.clock = clock
        self._state = {}  # key -> (tokens, last)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self.rate <= 0:
            return True  # 0 = no limit
        now = self.clock()
        with self._lock:
            tokens, last = self._state.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            ok = tokens >= 1.0
            self._state[key] = (tokens - 1.0 if ok else tokens, now)
            return ok
