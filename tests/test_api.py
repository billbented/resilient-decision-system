"""End-to-end over real HTTP."""
import json
import threading
import unittest
import urllib.error
import urllib.request

from decision_platform.api import make_handler
from tests.helpers import loan, make_service
from http.server import ThreadingHTTPServer


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc, cls.clock = make_service()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.svc))
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.svc.store.close()

    def call(self, method, path, body=None, headers=None):
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read()), dict(resp.headers)
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read()), dict(err.headers)

    def test_submit_explain_audit(self):
        st, body, _ = self.call("POST", "/workflows/loan_application/requests", loan(), {"Idempotency-Key": "api-1"})
        self.assertEqual((st, body["status"], body["idempotent_replay"]), (201, "APPROVED", False))
        rid = body["id"]
        st, exp, _ = self.call("GET", f"/requests/{rid}/explain")
        self.assertEqual(st, 200)
        self.assertIn("Decision APPROVED", "\n".join(exp["narrative"]))
        st, audit, _ = self.call("GET", f"/requests/{rid}/audit")
        self.assertTrue(audit["integrity"]["valid"])
        st, hist, _ = self.call("GET", f"/requests/{rid}/history")
        self.assertEqual(hist[-1]["to_status"], "APPROVED")

    def test_duplicate_returns_200_with_replay_header(self):
        p = loan(full_name="Dup Test")
        self.call("POST", "/workflows/loan_application/requests", p, {"Idempotency-Key": "api-dup"})
        st, body, headers = self.call("POST", "/workflows/loan_application/requests", p, {"Idempotency-Key": "api-dup"})
        self.assertEqual((st, body["idempotent_replay"], headers.get("Idempotent-Replay")), (200, True, "true"))
        st, body, _ = self.call("POST", "/workflows/loan_application/requests", {**p, "age": 40},
                                {"Idempotency-Key": "api-dup"})
        self.assertEqual((st, body["error"]), (409, "idempotency_conflict"))

    def test_invalid_input_422(self):
        st, body, _ = self.call("POST", "/workflows/loan_application/requests", {"age": "old"})
        self.assertEqual(st, 422)
        self.assertTrue(body["errors"])

    def test_malformed_json_and_unknown_routes(self):
        st, body, _ = self.call("POST", "/workflows/loan_application/requests", b"{oops")
        self.assertEqual((st, body["error"]), (422, "validation_failed"))
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        self.assertEqual(self.call("GET", "/requests/does-not-exist")[0], 404)
        self.assertEqual(self.call("GET", "/workflows/loan_application/requests")[0], 405)

    def test_review_flow_and_faults_admin(self):
        st, body, _ = self.call("POST", "/workflows/loan_application/requests", loan(applicant_id="APP-1002"))
        self.assertEqual(body["status"], "MANUAL_REVIEW")
        st, done, _ = self.call("POST", f"/requests/{body['id']}/review", {"decision": "approve", "reviewer": "qa"})
        self.assertEqual((st, done["status"]), (200, "APPROVED"))
        self.call("POST", "/admin/faults/credit_bureau", {"outage": True})
        st, body, _ = self.call("POST", "/workflows/loan_application/requests", loan(full_name="Outage"))
        self.assertEqual(body["status"], "RETRY_PENDING")
        self.call("POST", "/admin/faults/credit_bureau", {"reset": True})
        st, body, _ = self.call("POST", f"/requests/{body['id']}/retry")
        self.assertEqual(body["status"], "APPROVED")

    def test_workflows_and_simulate(self):
        st, wfs, _ = self.call("GET", "/workflows")
        self.assertIn("loan_application", [w["workflow"] for w in wfs])
        st, sim, _ = self.call("POST", "/workflows/loan_application/simulate?version=1",
                               {"input": loan(applicant_id="APP-1002")})
        self.assertEqual(sim["decision"], "APPROVED")
        self.assertEqual(self.call("GET", "/health")[1]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
