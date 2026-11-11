"""Simulated external dependencies + resilience (timeouts, retries, circuit breaker).

Real integrations (credit bureau, document OCR, sanctions screening) are
replaced by deterministic simulators with a ``FaultInjector`` so every failure
mode -- transient error, permanent error, timeout, sustained outage -- can be
reproduced on demand in tests and demos.
"""

import concurrent.futures
import hashlib
import random
import threading
import time

from .errors import (DependencyUnavailable, PermanentDependencyError,
                     TransientDependencyError)


class Clock:
    def now(self):
        return time.time()

    def sleep(self, seconds):
        time.sleep(seconds)


class FaultInjector:
    """Scripted failure plan for a simulated dependency.

    * ``fail_next(n, kind)`` -- next n calls fail with kind in
      {"transient", "permanent", "timeout"}
    * ``set_outage(True)``    -- every call fails transiently until cleared
    * ``failure_rate``        -- random transient failures (seeded RNG)
    * ``latency_ms``          -- added latency (exceeding the timeout => timeout)
    """

    def __init__(self, seed=7):
        self._lock = threading.Lock()
        self._queue = []
        self.outage = False
        self.failure_rate = 0.0
        self.latency_ms = 0
        self._rng = random.Random(seed)

    def fail_next(self, n, kind="transient"):
        with self._lock:
            self._queue.extend([kind] * n)

    def set_outage(self, on=True):
        self.outage = on

    def reset(self):
        with self._lock:
            self._queue.clear()
        self.outage, self.failure_rate, self.latency_ms = False, 0.0, 0

    def next_fault(self):
        with self._lock:
            if self._queue:
                return self._queue.pop(0)
        if self.outage:
            return "transient"
        if self.failure_rate and self._rng.random() < self.failure_rate:
            return "transient"
        return None

    def describe(self):
        return {"queued": list(self._queue), "outage": self.outage,
                "failure_rate": self.failure_rate, "latency_ms": self.latency_ms}


class SimulatedDependency:
    name = "base"

    def __init__(self, fixtures=None, faults=None):
        self.fixtures = fixtures or {}
        self.faults = faults or FaultInjector()
        self.calls = 0          # successful + failed invocations (for tests)
        self._lock = threading.Lock()

    def call(self, payload):
        with self._lock:
            self.calls += 1
        if self.faults.latency_ms:
            time.sleep(self.faults.latency_ms / 1000)
        fault = self.faults.next_fault()
        if fault == "transient":
            raise TransientDependencyError(f"{self.name}: 503 service unavailable")
        if fault == "timeout":
            raise TransientDependencyError(f"{self.name}: timed out")
        if fault == "permanent":
            raise PermanentDependencyError(f"{self.name}: 400 subject not found")
        return self.respond(payload)

    def respond(self, payload):
        raise NotImplementedError

    @staticmethod
    def _bucket(key, modulo):
        return int(hashlib.sha256(str(key).encode()).hexdigest(), 16) % modulo


class CreditBureau(SimulatedDependency):
    name = "credit_bureau"

    def respond(self, payload):
        sid = payload.get("applicant_id")
        if sid in self.fixtures:
            data = dict(self.fixtures[sid])
        else:
            data = {"score": 300 + self._bucket(sid, 551), "open_defaults": self._bucket(sid + "d", 3) // 2}
        data["bureau_ref"] = "CB-" + hashlib.sha256(str(sid).encode()).hexdigest()[:8].upper()
        return data


class DocumentVerifier(SimulatedDependency):
    name = "document_verifier"

    def respond(self, payload):
        doc = payload.get("document_id")
        if doc in self.fixtures:
            return dict(self.fixtures[doc])
        confidence = round(0.5 + self._bucket(doc, 50) / 100, 2)
        return {"verified": confidence >= 0.7, "confidence": confidence,
                "tamper_detected": self._bucket(str(doc) + "t", 20) == 0}


class SanctionsScreening(SimulatedDependency):
    name = "sanctions_screening"

    def respond(self, payload):
        entity = payload.get("entity_name", "")
        if entity in self.fixtures:
            return dict(self.fixtures[entity])
        hit = "sanction" in entity.lower()
        return {"hit": hit, "lists_checked": ["UN", "OFAC", "EU"], "match_score": 0.97 if hit else 0.0}


SIMULATORS = {cls.name: cls for cls in (CreditBureau, DocumentVerifier, SanctionsScreening)}


class CircuitBreaker:
    """closed -> (N consecutive failures) -> open -> (cooldown) -> half_open
    -> success closes / failure re-opens. Prevents hammering a dead dependency
    and lets requests fail fast into RETRY_PENDING instead of tying up workers."""

    def __init__(self, failure_threshold=5, reset_timeout_s=30.0, clock=None):
        self.failure_threshold = failure_threshold
        self.reset_timeout_s = reset_timeout_s
        self.clock = clock or Clock()
        self.state = "closed"
        self.failures = 0
        self.opened_at = None
        self._lock = threading.Lock()

    def allow(self):
        with self._lock:
            if self.state == "open":
                if self.clock.now() - self.opened_at >= self.reset_timeout_s:
                    self.state = "half_open"
                    return True
                return False
            return True

    def record_success(self):
        with self._lock:
            self.state, self.failures, self.opened_at = "closed", 0, None

    def record_failure(self):
        with self._lock:
            self.failures += 1
            if self.state == "half_open" or self.failures >= self.failure_threshold:
                self.state = "open"
                self.opened_at = self.clock.now()

    def snapshot(self):
        return {"state": self.state, "consecutive_failures": self.failures}


class DependencyGateway:
    """Single entry point for all outbound calls: timeout + bounded retries with
    exponential backoff and full jitter + per-dependency circuit breaker.
    Returns the result *and* the attempt log so it can be written to the audit."""

    _pool = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="dep")

    def __init__(self, dependencies=None, clock=None, sleeper=None, seed=11):
        self.clock = clock or Clock()
        self.sleeper = sleeper or self.clock.sleep
        self.deps = {}
        self.breakers = {}
        self._rng = random.Random(seed)
        for dep in (dependencies or []):
            self.register(dep)

    def register(self, dep, failure_threshold=5, reset_timeout_s=30.0):
        self.deps[dep.name] = dep
        self.breakers[dep.name] = CircuitBreaker(failure_threshold, reset_timeout_s, self.clock)

    def call(self, name, payload, policy=None):
        policy = policy or {}
        retry = policy.get("retry", {})
        max_attempts = retry.get("max_attempts", 3)
        base = retry.get("base_delay_ms", 50) / 1000
        cap = retry.get("max_delay_ms", 1000) / 1000
        timeout = policy.get("timeout_ms", 2000) / 1000
        dep, breaker = self.deps.get(name), self.breakers.get(name)
        if dep is None:
            raise PermanentDependencyError(f"dependency '{name}' is not registered")

        attempts = []
        for attempt in range(1, max_attempts + 1):
            if not breaker.allow():
                attempts.append({"attempt": attempt, "outcome": "circuit_open"})
                raise DependencyUnavailable(f"{name}: circuit breaker open",
                                            {"attempts": attempts, "breaker": breaker.snapshot()})
            started = time.perf_counter()
            try:
                future = self._pool.submit(dep.call, payload)
                try:
                    result = future.result(timeout=timeout)
                except concurrent.futures.TimeoutError:
                    future.cancel()
                    raise TransientDependencyError(f"{name}: no response within {int(timeout * 1000)}ms")
                breaker.record_success()
                attempts.append({"attempt": attempt, "outcome": "ok",
                                 "latency_ms": round((time.perf_counter() - started) * 1000, 1)})
                return result, attempts
            except PermanentDependencyError as exc:
                # Not the dependency's health problem; do not trip the breaker.
                attempts.append({"attempt": attempt, "outcome": "permanent_error", "error": exc.message})
                exc.details = {"attempts": attempts}
                raise
            except TransientDependencyError as exc:
                breaker.record_failure()
                entry = {"attempt": attempt, "outcome": "transient_error", "error": exc.message}
                if attempt < max_attempts:
                    delay = self._rng.uniform(0, min(cap, base * (2 ** (attempt - 1))))
                    entry["backoff_ms"] = round(delay * 1000, 1)
                    attempts.append(entry)
                    self.sleeper(delay)
                else:
                    attempts.append(entry)
        raise DependencyUnavailable(f"{name}: retries exhausted after {max_attempts} attempts",
                                    {"attempts": attempts, "breaker": breaker.snapshot()})
