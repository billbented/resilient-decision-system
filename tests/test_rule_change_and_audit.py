import json
import os
import shutil
import tempfile
import unittest

from decision_platform.app import DEFAULT_CONFIG_DIR, build_service
from decision_platform.testing import FakeClock
from tests.helpers import load_config, loan, make_service


class RuleChangeTests(unittest.TestCase):
    """Requirement changes are config changes: new version file, hot reload, pinning, rollback."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        shutil.copy(os.path.join(DEFAULT_CONFIG_DIR, "loan_application.v1.json"), self.dir)
        clock = FakeClock()
        self.svc = build_service(config_dir=self.dir, clock=clock, sleeper=clock.sleep)
        self.addCleanup(self.svc.store.close)
        self.clock = clock
        self.p = loan(applicant_id="APP-1002")  # score 680

    def tearDown(self):
        shutil.rmtree(self.dir)

    def deploy_v2(self):
        with open(os.path.join(self.dir, "loan_application.v2.json"), "w") as fh:
            json.dump(load_config("loan_application.v2.json"), fh)
        self.svc.registry.reload()

    def test_threshold_change_changes_decision_without_code(self):
        before, _ = self.svc.submit("loan_application", self.p, "a")
        self.assertEqual((before["status"], before["workflow_version"]), ("APPROVED", 1))
        self.deploy_v2()
        after, _ = self.svc.submit("loan_application", {**self.p, "full_name": "Bo Ray"}, "b")
        self.assertEqual((after["status"], after["workflow_version"]), ("MANUAL_REVIEW", 2))

    def test_in_flight_request_stays_pinned_to_its_version(self):
        self.svc.gateway.deps["credit_bureau"].faults.set_outage(True)
        rec, _ = self.svc.submit("loan_application", self.p, "a")
        self.assertEqual((rec["status"], rec["workflow_version"]), ("RETRY_PENDING", 1))
        self.deploy_v2()                      # rules change while it waits
        self.svc.gateway.deps["credit_bureau"].faults.set_outage(False)
        self.clock.advance(31)
        self.svc.retry_due()
        done = self.svc.get(rec["id"])
        self.assertEqual((done["status"], done["workflow_version"]), ("APPROVED", 1))  # judged by v1 rules

    def test_simulate_previews_impact_and_persists_nothing(self):
        self.deploy_v2()
        r1 = self.svc.simulate("loan_application", self.p, version=1)
        r2 = self.svc.simulate("loan_application", self.p, version=2)
        self.assertEqual((r1["decision"], r2["decision"]), ("APPROVED", "MANUAL_REVIEW"))
        self.assertEqual(self.svc.store.list_requests(), [])

    def test_simulate_with_supplied_dependency_outputs(self):
        r = self.svc.simulate("loan_application", self.p, version=1,
                              dependency_outputs={"credit": {"score": 500, "open_defaults": 0}})
        self.assertEqual(r["decision"], "REJECTED")
        self.assertEqual(self.svc.gateway.deps["credit_bureau"].calls, 0)

    def test_rollback_by_activation(self):
        self.deploy_v2()
        self.svc.registry.activate("loan_application", 1)
        rec, _ = self.svc.submit("loan_application", self.p, "c")
        self.assertEqual((rec["status"], rec["workflow_version"]), ("APPROVED", 1))

    def test_new_workflow_is_pure_config(self):
        wf = {"workflow": "document_verification", "version": 1, "start": "check",
              "input_schema": {"type": "object", "required": ["doc_type", "pages"],
                               "properties": {"doc_type": {"type": "string"}, "pages": {"type": "integer"}}},
              "stages": {"check": {"rules": [
                  {"id": "R-1", "condition": {"field": "input.doc_type", "op": "in", "value": ["pan", "aadhaar"]}},
                  {"id": "R-2", "condition": {"field": "input.pages", "op": "lte", "value": 3}, "on_fail": "review"}],
                  "transitions": {"pass": "APPROVED"}}}}
        with open(os.path.join(self.dir, "document_verification.v1.json"), "w") as fh:
            json.dump(wf, fh)
        self.svc.registry.reload()
        rec, _ = self.svc.submit("document_verification", {"doc_type": "pan", "pages": 5})
        self.assertEqual(rec["status"], "MANUAL_REVIEW")


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.svc, _ = make_service()
        self.addCleanup(self.svc.store.close)

    def test_audit_records_full_trace_and_data_refs(self):
        rec, _ = self.svc.submit("loan_application", loan(applicant_id="APP-1002"))
        audit = self.svc.audit(rec["id"])
        self.assertTrue(audit["integrity"]["valid"])
        events = [e["event"] for e in audit["entries"]]
        self.assertEqual(events, ["request.received", "validation.passed", "stage.evaluated", "dependency.called",
                                  "stage.evaluated", "decision.made"])
        received = audit["entries"][0]["data"]
        self.assertEqual(received["config"]["version"], 2)  # config version + hash recorded
        credit = audit["entries"][4]["data"]
        r = next(x for x in credit["rules"] if x["rule_id"] == "R-CRD-003")
        self.assertEqual(r["data_refs"], {"deps.credit.score": 680})
        self.assertIn("680 >= 700 -> false", r["explanation"])

    def test_tampering_breaks_hash_chain(self):
        rec, _ = self.svc.submit("loan_application", loan())
        with self.svc.store.transaction("tamper") as tx:
            tx.execute("UPDATE audit_log SET data = '{\"decision\": \"REJECTED\"}' WHERE request_id = ? AND seq = 3",
                       (rec["id"],))
        check = self.svc.store.verify_audit(rec["id"])
        self.assertFalse(check["valid"])
        self.assertEqual(check["broken_at_seq"], 3)

    def test_explanation_contents(self):
        rec, _ = self.svc.submit("loan_application", loan(applicant_id="APP-1002"))
        self.svc.review(rec["id"], "approve", "officer", "ok")
        exp = self.svc.explain(rec["id"])
        self.assertEqual(exp["path"], ["eligibility", "credit_assessment"])
        text = "\n".join(exp["narrative"])
        self.assertIn("R-CRD-003", text)
        self.assertIn("Manual review by officer: APPROVED", text)
        self.assertIn("Engine outcome MANUAL_REVIEW driven by: R-CRD-003", text)
        self.assertIn("hash chain VALID", text)
        self.assertEqual(exp["state_history"][-1], "MANUAL_REVIEW -> APPROVED (manual review by officer)")
        self.assertTrue(any(r["status"] == "skipped" for r in exp["rules_triggered"]))

    def test_history_is_complete(self):
        self.svc.gateway.deps["credit_bureau"].faults.set_outage(True)
        rec, _ = self.svc.submit("loan_application", loan())
        statuses = [h["to_status"] for h in self.svc.history(rec["id"])]
        self.assertEqual(statuses, ["RECEIVED", "IN_PROGRESS", "RETRY_PENDING"])


if __name__ == "__main__":
    unittest.main()
