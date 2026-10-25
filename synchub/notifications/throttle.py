"""A small in-memory limit on how often one person may ask Pushover to check a key or send a test. Every wrong answer from Pushover counts toward
getting this server's address blocked, so a person mistyping (or leaning on a button) must not be able to cause a run of them."""
import threading
import time
from collections import defaultdict, deque


class Throttle:
    def __init__(self, limit, per_seconds):
        self.limit, self.per, self._seen, self._lock = limit, per_seconds, defaultdict(deque), threading.Lock()

    def allow(self, key, now=None):
        """True and counts the call, or False when `key` has already used its calls in the last `per_seconds`."""
        now = time.monotonic() if now is None else now
        with self._lock:
            calls = self._seen[key]
            while calls and now - calls[0] >= self.per:
                calls.popleft()
            if len(calls) >= self.limit:
                return False
            calls.append(now)
            return True
