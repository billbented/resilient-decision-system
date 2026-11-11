import threading
import unittest

from decision_platform.errors import IdempotencyConflict
from tests.helpers import loan, make_service


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.svc, _ = make_service()
        self.addCleanup(self.svc.store.close)
        self.bureau = self.svc.gateway.deps["credit_bureau"]

    def test_same_key_same_payload_replays(self):
        first, created1 = self.svc.submit("loan_application", loan(), "k1")
        second, created2 = self.svc.submit("loan_application", loan(), "k1")
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["status"], "APPROVED")
        self.assertEqual(self.bureau.calls, 1)  # not processed twice
        events = [e["event"] for e in self.svc.store.audit(first["id"])]
        self.assertIn("request.duplicate", events)

    def test_same_key_different_payload_conflicts(self):
        self.svc.submit("loan_application", loan(), "k1")
        with self.assertRaises(IdempotencyConflict):
            self.svc.submit("loan_application", loan(loan_amount=1), "k1")

    def test_key_order_does_not_matter(self):
        a = loan()
        b = dict(reversed(list(a.items())))
        r1, _ = self.svc.submit("loan_application", a, "k1")
        r2, created = self.svc.submit("loan_application", b, "k1")
        self.assertEqual((r1["id"], created), (r2["id"], False))

    def test_no_key_falls_back_to_payload_hash(self):
        r1, _ = self.svc.submit("loan_application", loan())
        r2, created = self.svc.submit("loan_application", loan())
        self.assertEqual((r1["id"], created), (r2["id"], False))

    def test_keys_are_scoped_per_workflow(self):
        r1, _ = self.svc.submit("loan_application", loan(), "shared")
        emp = {"employee_id": "EMP-1", "name": "Ab", "department": "hr", "start_date": "2026-01-01",
               "documents": [], "background_check": "clear"}
        emp_ok = {**emp, "employee_id": "EMP-100"}
        r2, created = self.svc.submit("employee_onboarding", emp_ok, "shared")
        self.assertTrue(created)
        self.assertNotEqual(r1["id"], r2["id"])

    def test_concurrent_duplicates_create_exactly_one_request(self):
        results, errors = [], []
        barrier = threading.Barrier(12)

        def worker():
            barrier.wait()
            try:
                results.append(self.svc.submit("loan_application", loan(), "race")[0]["id"])
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(self.svc.store.list_requests()), 1)
        self.assertEqual(self.bureau.calls, 1)
        self.assertEqual(self.svc.get(results[0])["status"], "APPROVED")

    def test_invalid_request_replay_is_stable(self):
        r1, _ = self.svc.submit("loan_application", {"age": 1}, "bad")
        r2, created = self.svc.submit("loan_application", {"age": 1}, "bad")
        self.assertEqual((r2["id"], r2["status"], created), (r1["id"], "INVALID", False))


if __name__ == "__main__":
    unittest.main()
