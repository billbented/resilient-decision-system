"""Deterministic test doubles shared by the test-suite and the demo."""

import threading


class FakeClock:
    """Controllable clock: ``sleep`` advances time instantly (no real waiting)."""

    def __init__(self, start=1_700_000_000.0):
        self._t = start
        self._lock = threading.Lock()
        self.sleeps = []

    def now(self):
        with self._lock:
            return self._t

    def sleep(self, seconds):
        with self._lock:
            self.sleeps.append(seconds)
            self._t += seconds

    def advance(self, seconds):
        with self._lock:
            self._t += seconds
