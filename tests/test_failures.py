"""Dependency failures, retries, circuit breaker, timeouts, partial saves, concurrency."""
import unittest

from decision_platform.dependencies import CircuitBreaker, CreditBureau, DependencyGateway
from decision_platform.errors import (ConcurrencyConflict, DependencyUnavailable, InvalidTransition,
                                      StorageError)
from decision_platform.testing import FakeClock
from tests.helpers import VENDOR, loan, make_service


class DependencyFailureTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = make_service()
        self.addCleanup(self.svc.store.close)
        self.bureau = self.svc.gateway.deps["credit_bureau"]

    def submit(self, payload, key=None):
        return self.svc.submit("loan_application", payload, key)[0]

    def events(self, rid):
        return [e["event"] for e in self.svc.store.audit(rid)]

    def test_transient_failures_absorbed_by_in_call_retries(self):
        self.bureau.faults.fail_next(2, "transient")
        rec = self.submit(loan())
        self.assertEqual(rec["status"], "APPROVED")
        call = next(e for e in self.svc.store.audit(rec["id"]) if e["event"] == "dependency.called")
        self.assertEqual([a["outcome"] for a in call["data"]["attempts"]],
                         ["transient_error", "transient_error", "ok"])
        self.assertEqual(len(self.clock.sleeps), 2)                 # backed off twice
        self.assertTrue(all(0 <= s <= 0.2 for s in self.clock.sleeps))  # capped jittered backoff

    def test_outage_parks_request_then_retry_succeeds(self):
        self.bureau.faults.set_outage(True)
        rec = self.submit(loan())
        self.assertEqual(rec["status"], "RETRY_PENDING")
        self.assertEqual(rec["current_stage"], "credit_assessment")  # checkpointed past eligibility
        self.assertEqual(rec["attempts"], 1)
        # not due yet -> sweep does nothing
        self.assertEqual(self.svc.retry_due()["retried"], [])
        self.bureau.faults.set_outage(False)
        self.clock.advance(31)
        self.assertEqual(self.svc.retry_due()["retried"], [rec["id"]])
        done = self.svc.get(rec["id"])
        self.assertEqual(done["status"], "APPROVED")
        evs = self.events(rec["id"])
        self.assertEqual(evs.count("stage.evaluated"), 3)  # eligibility NOT re-evaluated on retry
        self.assertIn("retry.scheduled", evs)
        self.assertIn("retry.started", evs)

    def test_retries_exhausted_goes_to_configured_fallback(self):
        self.bureau.faults.set_outage(True)
        rec = self.submit(loan())
        for backoff in (31, 121):
            self.clock.advance(backoff)
            self.svc.retry_due()
        final = self.svc.get(rec["id"])
        self.assertEqual(final["status"], "MANUAL_REVIEW")   # retry_policy.on_exhausted
        self.assertEqual(final["attempts"], 3)
        self.assertEqual(final["decision_reasons"]["outcome"], "retries_exhausted")

    def test_backoff_schedule_from_config(self):
        self.bureau.faults.set_outage(True)
        rec = self.submit(loan())
        self.assertAlmostEqual(rec["next_retry_at"] - self.clock.now(), 30)

    def test_manual_retry_only_from_retry_pending(self):
        rec = self.submit(loan())
        with self.assertRaises(InvalidTransition):
            self.svc.retry(rec["id"])

    def test_permanent_error_goes_straight_to_review(self):
        self.bureau.faults.fail_next(1, "permanent")
        rec = self.submit(loan())
        self.assertEqual(rec["status"], "MANUAL_REVIEW")
        self.assertEqual(self.bureau.calls, 1)  # permanent errors are not retried

    def test_fail_safe_policy_review(self):
        self.svc.gateway.deps["sanctions_screening"].faults.set_outage(True)
        rec, _ = self.svc.submit("vendor_approval", VENDOR)
        self.assertEqual(rec["status"], "MANUAL_REVIEW")  # on_unavailable: review (never auto-approve)

    def test_dependency_result_reused_not_recalled(self):
        """A successful call is cached per request: a later failure + resume never re-calls it."""
        self.svc.store.faults.arm("checkpoint:credit_assessment", after_statements=1)
        with self.assertRaises(StorageError):
            self.submit(loan(), "k")
        self.assertEqual(self.bureau.calls, 1)
        rec, created = self.svc.submit("loan_application", loan(), "k")  # client retries
        self.assertFalse(created)
        self.assertEqual(rec["status"], "APPROVED")
        self.assertEqual(self.bureau.calls, 1)
        self.assertIn("dependency.reused", self.events(rec["id"]))

    def test_real_timeout_enforced(self):
        self.bureau.faults.latency_ms = 50
        self.svc.registry.get("loan_application").dependencies["credit_bureau"]["timeout_ms"] = 10
        try:
            rec = self.submit(loan())
        finally:
            self.svc.registry.get("loan_application").dependencies["credit_bureau"]["timeout_ms"] = 500
        self.assertEqual(rec["status"], "RETRY_PENDING")
        failed = next(e for e in self.svc.store.audit(rec["id"]) if e["event"] == "dependency.failed")
        self.assertIn("no response within", failed["data"]["attempts"][0]["error"])


class CircuitBreakerTests(unittest.TestCase):
    def test_opens_fails_fast_and_half_opens(self):
        clock = FakeClock()
        dep = CreditBureau()
        gw = DependencyGateway(clock=clock, sleeper=clock.sleep)
        gw.register(dep, failure_threshold=3, reset_timeout_s=10)
        dep.faults.set_outage(True)
        policy = {"retry": {"max_attempts": 3, "base_delay_ms": 1}}
        with self.assertRaises(DependencyUnavailable):
            gw.call("credit_bureau", {"applicant_id": "APP-1"}, policy)
        self.assertEqual(gw.breakers["credit_bureau"].state, "open")
        calls = dep.calls
        with self.assertRaises(DependencyUnavailable) as ctx:
            gw.call("credit_bureau", {"applicant_id": "APP-1"}, policy)
        self.assertIn("circuit breaker open", ctx.exception.message)
        self.assertEqual(dep.calls, calls)  # failed fast, dependency not hit
        dep.faults.set_outage(False)
        clock.advance(11)
        out, _ = gw.call("credit_bureau", {"applicant_id": "APP-1"}, policy)  # half-open probe succeeds
        self.assertIn("score", out)
        self.assertEqual(gw.breakers["credit_bureau"].state, "closed")

    def test_half_open_failure_reopens(self):
        clock = FakeClock()
        br = CircuitBreaker(failure_threshold=1, reset_timeout_s=5, clock=clock)
        br.record_failure()
        clock.advance(6)
        self.assertTrue(br.allow())
        br.record_failure()
        self.assertEqual(br.state, "open")


class StorageFailureTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = make_service()
        self.addCleanup(self.svc.store.close)

    def test_partial_save_rolls_back_and_resumes(self):
        # Fail the affordability checkpoint AFTER its first statement has executed.
        self.svc.store.faults.arm("checkpoint:affordability", after_statements=1)
        with self.assertRaises(StorageError):
            self.svc.submit("loan_application", loan(), "k")
        rec = self.svc.store.find_by_key("loan_application", "k")
        self.assertEqual(rec["status"], "IN_PROGRESS")
        self.assertEqual(rec["current_stage"], "affordability")      # last good checkpoint
        stages = [e["stage"] for e in self.svc.store.audit(rec["id"]) if e["event"] == "stage.evaluated"]
        self.assertEqual(stages, ["eligibility", "credit_assessment"])  # no half-written stage
        self.assertTrue(self.svc.store.verify_audit(rec["id"])["valid"])
        # Recovery sweep resumes the stuck request (its lease was released).
        out = self.svc.retry_due()
        self.assertEqual(out["resumed"], [rec["id"]])
        final = self.svc.get(rec["id"])
        self.assertEqual(final["status"], "APPROVED")
        stages = [e["stage"] for e in self.svc.store.audit(rec["id"]) if e["event"] == "stage.evaluated"]
        self.assertEqual(stages, ["eligibility", "credit_assessment", "affordability"])  # exactly once each

    def test_intake_failure_leaves_nothing_behind(self):
        self.svc.store.faults.arm("intake", after_statements=2)
        with self.assertRaises(StorageError):
            self.svc.submit("loan_application", loan(), "k")
        self.assertIsNone(self.svc.store.find_by_key("loan_application", "k"))
        rec, created = self.svc.submit("loan_application", loan(), "k")  # safe to retry
        self.assertTrue(created)
        self.assertEqual(rec["status"], "APPROVED")

    def test_optimistic_lock_detects_lost_update(self):
        rec, _ = self.svc.submit("loan_application", loan(applicant_id="APP-1002"))
        with self.svc.store.transaction("t") as tx:
            self.svc.store.update_request(tx, rec["id"], rec["row_version"], updated_at=1.0)
        with self.assertRaises(ConcurrencyConflict):
            with self.svc.store.transaction("t") as tx:
                self.svc.store.update_request(tx, rec["id"], rec["row_version"], updated_at=2.0)

    def test_processing_lease_blocks_second_worker(self):
        rec, _ = self.svc.submit("loan_application", loan())
        self.assertTrue(self.svc.store.acquire_lease(rec["id"], "worker-A", self.clock.now(), 60))
        with self.assertRaises(ConcurrencyConflict):
            self.svc.process(rec["id"])
        self.clock.advance(61)  # lease expired -> another worker may take over
        self.assertTrue(self.svc.store.acquire_lease(rec["id"], "worker-B", self.clock.now(), 60))


if __name__ == "__main__":
    unittest.main()
