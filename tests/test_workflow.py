"""Happy path, rejects, manual review, branching, multiple workflows, lifecycle."""
import unittest

from decision_platform.errors import InvalidTransition, NotFound, ValidationError
from tests.helpers import CLAIM, VENDOR, loan, make_service


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = make_service()
        self.addCleanup(self.svc.store.close)

    def submit(self, payload, wf="loan_application", key=None):
        rec, _ = self.svc.submit(wf, payload, key)
        return rec

    def test_happy_path_approved(self):
        rec = self.submit(loan())
        self.assertEqual(rec["status"], "APPROVED")
        self.assertEqual(rec["decision_reasons"]["path"], ["eligibility", "credit_assessment", "affordability"])
        hist = [h["to_status"] for h in self.svc.history(rec["id"])]
        self.assertEqual(hist, ["RECEIVED", "IN_PROGRESS", "APPROVED"])

    def test_invalid_input(self):
        rec = self.submit({"applicant_id": "bad", "age": "x"})
        self.assertEqual(rec["status"], "INVALID")
        errors = rec["decision_reasons"]["validation_errors"]
        self.assertTrue(any("applicant_id" in e for e in errors))
        self.assertTrue(any("kyc_verified: is required" in e for e in errors))
        self.assertEqual(self.svc.gateway.deps["credit_bureau"].calls, 0)  # nothing downstream ran

    def test_non_object_payload(self):
        with self.assertRaises(ValidationError):
            self.svc.submit("loan_application", ["not", "an", "object"])

    def test_reject_on_mandatory_rule(self):
        rec = self.submit(loan(kyc_verified=False))
        self.assertEqual(rec["status"], "REJECTED")
        self.assertEqual(rec["decision_reasons"]["decisive_rules"][0]["rule_id"], "R-ELG-001")
        self.assertEqual(self.svc.gateway.deps["credit_bureau"].calls, 0)  # stopped before the external call

    def test_reject_on_threshold(self):
        rec = self.submit(loan(applicant_id="APP-1003"))
        self.assertEqual(rec["status"], "REJECTED")
        ids = {r["rule_id"] for r in rec["decision_reasons"]["decisive_rules"]}
        self.assertIn("R-CRD-001", ids)

    def test_derived_threshold_reject(self):
        rec = self.submit(loan(loan_amount=7000000))  # > 5x income
        self.assertEqual(rec["status"], "REJECTED")
        self.assertEqual(rec["decision_reasons"]["decisive_rules"][0]["rule_id"], "R-AFF-001")

    def test_conditional_rule_applies_only_when_guard_true(self):
        rec = self.submit(loan(employment_status="self_employed", annual_income=400000, loan_amount=900000))
        self.assertEqual(rec["status"], "MANUAL_REVIEW")
        self.assertEqual(rec["decision_reasons"]["decisive_rules"][0]["rule_id"], "R-ELG-004")

    def test_conditional_branching_to_senior_stage(self):
        rec = self.submit(loan(applicant_id="APP-1004", annual_income=6000000, loan_amount=4500000))
        self.assertEqual(rec["status"], "APPROVED")
        self.assertEqual(rec["decision_reasons"]["path"][-1], "senior_approval")

    def test_senior_stage_composite_rule(self):
        rec = self.submit(loan(applicant_id="APP-1001", annual_income=6000000, loan_amount=4500000))  # score 742
        self.assertEqual(rec["status"], "MANUAL_REVIEW")
        self.assertEqual(rec["decision_reasons"]["decisive_rules"][0]["rule_id"], "R-SNR-001")

    def test_manual_review_then_human_decision(self):
        rec = self.submit(loan(applicant_id="APP-1002"))
        self.assertEqual(rec["status"], "MANUAL_REVIEW")
        done = self.svc.review(rec["id"], "reject", "officer@bank", "income unverifiable")
        self.assertEqual(done["status"], "REJECTED")
        self.assertEqual(done["decision_reasons"]["manual_review"]["system_recommendation"], "MANUAL_REVIEW")
        with self.assertRaises(InvalidTransition):
            self.svc.review(rec["id"], "approve", "someone")  # terminal states are final

    def test_review_requires_reviewer(self):
        rec = self.submit(loan(applicant_id="APP-1002"))
        with self.assertRaises(ValidationError):
            self.svc.review(rec["id"], "approve", "")

    def test_cannot_review_non_review_request(self):
        rec = self.submit(loan())
        with self.assertRaises(InvalidTransition):
            self.svc.review(rec["id"], "reject", "x")

    def test_flags_do_not_block(self):
        rec = self.submit(loan(existing_debt=900000), wf="loan_application")  # v2: review
        self.assertEqual(rec["status"], "MANUAL_REVIEW")
        self.svc.registry.activate("loan_application", 1)                    # v1: flag only
        rec = self.submit(loan(existing_debt=900000, full_name="Other"))
        self.assertEqual(rec["status"], "APPROVED")
        self.assertIn("R-AFF-002", rec["decision_reasons"]["flags"])

    def test_second_workflow_same_engine(self):
        self.assertEqual(self.submit(CLAIM, "insurance_claim")["status"], "APPROVED")
        tampered = self.submit({**CLAIM, "claim_id": "CLM-5002", "claim_amount": 240000, "document_id": "DOC-9003"},
                               "insurance_claim")
        self.assertEqual(tampered["status"], "REJECTED")
        big = self.submit({**CLAIM, "claim_id": "CLM-5003", "claim_amount": 150000, "claim_type": "property"},
                          "insurance_claim")
        self.assertEqual(big["status"], "MANUAL_REVIEW")
        self.assertEqual(big["decision_reasons"]["decisive_rules"][0]["rule_id"], "R-ADJ-002")

    def test_cross_field_reference(self):
        rec = self.submit({**CLAIM, "claim_amount": 400000}, "insurance_claim")  # > coverage_limit
        self.assertEqual(rec["decision_reasons"]["decisive_rules"][0]["rule_id"], "R-CLM-002")

    def test_vendor_and_onboarding(self):
        self.assertEqual(self.submit(VENDOR, "vendor_approval")["status"], "APPROVED")
        hit = self.submit({**VENDOR, "vendor_id": "VEN-303", "legal_name": "Sanctioned Metals LLC"}, "vendor_approval")
        self.assertEqual(hit["status"], "REJECTED")
        emp = {"employee_id": "EMP-204", "name": "Kavya", "department": "engineering", "start_date": "2026-12-01",
               "documents": ["id_proof", "offer_letter_signed", "bank_details"], "background_check": "clear"}
        self.assertEqual(self.submit(emp, "employee_onboarding")["status"], "APPROVED")

    def test_unknown_workflow_and_request(self):
        with self.assertRaises(NotFound):
            self.svc.submit("does_not_exist", {})
        with self.assertRaises(NotFound):
            self.svc.get("nope")


if __name__ == "__main__":
    unittest.main()
