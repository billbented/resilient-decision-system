"""Scripted scenarios -> docs/DECISION_EXAMPLES.md + examples/explanations/*.json.

Each scenario shows: input, rules triggered, output, and audit reasoning.
Run:  python -m decision_platform demo
"""

import json
import os

from .app import ROOT, build_service
from .errors import IdempotencyConflict
from .testing import FakeClock

LOAN = {"applicant_id": "APP-1001", "full_name": "Asha Rao", "age": 34, "annual_income": 1200000,
        "loan_amount": 1500000, "employment_status": "salaried", "existing_debt": 150000, "kyc_verified": True}


def loan(**kw):
    return {**LOAN, **kw}


def run_demo(out=None):
    out = out or os.path.join(ROOT, "docs", "DECISION_EXAMPLES.md")
    exp_dir = os.path.join(ROOT, "examples", "explanations")
    os.makedirs(exp_dir, exist_ok=True)
    clock = FakeClock()
    svc = build_service(clock=clock, sleeper=clock.sleep)
    bureau = svc.gateway.deps["credit_bureau"]
    sections = []

    def record(slug, title, why, rid, payload, extra=None):
        exp = svc.explain(rid)
        with open(os.path.join(exp_dir, f"{slug}.json"), "w") as fh:
            json.dump(exp, fh, indent=2, default=str)
        events = [f"{e['seq']:>2}. {e['event']}" + (f" [{e['stage']}]" if e["stage"] else "")
                  for e in svc.store.audit(rid)]
        md = [f"## {len(sections) + 1}. {title}", "", why, "",
              "**Input**", "", "```json", json.dumps(payload, indent=2), "```", "",
              f"**Output:** `{exp['status']}` (workflow `{exp['workflow']}` v{exp['workflow_version']})", ""]
        if extra:
            md += [extra, ""]
        md += ["**Rules triggered & reasoning** (`GET /requests/{id}/explain`)", "", "```text",
               *exp["narrative"], "```", "", "**Audit trail events**", "", "```text", *events, "```", ""]
        sections.append("\n".join(md))
        print(f"[{exp['status']:>13}] {title}")
        return exp

    # 1 happy path
    p = loan()
    rec, _ = svc.submit("loan_application", p, "demo-1")
    record("01_loan_approved", "Happy path: loan auto-approved",
           "Every eligibility, credit and affordability rule passes; loan <= 20L so no senior stage.", rec["id"], p)

    # 2 manual review + human decision
    p = loan(applicant_id="APP-1002", full_name="Vikram Shah")
    rec, _ = svc.submit("loan_application", p, "demo-2")
    svc.review(rec["id"], "approve", "credit.officer@bank", "Salary slips verified offline; score near threshold.")
    record("02_loan_manual_review", "Threshold miss -> manual review -> human approval",
           "Bureau score 680 is above the reject floor (550) but below the v2 straight-through bar (700).",
           rec["id"], p)

    # 3 reject
    p = loan(applicant_id="APP-1003", full_name="R. Mehta")
    rec, _ = svc.submit("loan_application", p, "demo-3")
    record("03_loan_rejected", "Hard reject: poor credit", "Score 512 < 550 is a reject rule; reject outranks review.",
           rec["id"], p)

    # 4 invalid input
    p = {"applicant_id": "12345", "full_name": "X", "age": "thirty", "loan_amount": -5, "employment_status": "pilot"}
    rec, _ = svc.submit("loan_application", p, "demo-4")
    record("04_invalid_input", "Invalid input rejected at intake (HTTP 422)",
           "All schema violations are reported at once; the request is still persisted and audited as INVALID.",
           rec["id"], p)

    # 5 duplicate
    p = loan(applicant_id="APP-1004", full_name="Neha Iyer")
    rec, _ = svc.submit("loan_application", p, "demo-5")
    again, created = svc.submit("loan_application", p, "demo-5")
    try:
        svc.submit("loan_application", loan(applicant_id="APP-1004", loan_amount=999), "demo-5")
        conflict = "no conflict?!"
    except IdempotencyConflict as exc:
        conflict = exc.message
    record("05_duplicate_request", "Duplicate request (idempotency)",
           "Same Idempotency-Key + same payload returns the original result without re-processing; "
           "same key + different payload is refused.", rec["id"], p,
           extra=f"- Replay returned same id: `{again['id'] == rec['id']}`, created=`{created}`, "
                 f"credit bureau calls for this applicant: 1\n- Same key, different payload -> HTTP 409: _{conflict}_")

    # 6 transient dependency failure, recovered by in-call retries
    bureau.faults.fail_next(2, "transient")
    p = loan(applicant_id="APP-1005", full_name="Imran Khan", existing_debt=100000)
    rec, _ = svc.submit("loan_application", p, "demo-6")
    record("06_transient_dependency_failure", "Dependency flakiness absorbed by retries",
           "Credit bureau fails twice (503); the gateway retries with exponential backoff + jitter and the 3rd "
           "attempt succeeds. Open default -> review.", rec["id"], p)

    # 7 outage -> RETRY_PENDING -> recovery
    bureau.faults.set_outage(True)
    p = loan(applicant_id="APP-1001", full_name="Asha Rao", loan_amount=1400000)
    rec, _ = svc.submit("loan_application", p, "demo-7")
    status_during = svc.get(rec["id"])["status"]
    bureau.faults.set_outage(False)
    svc.gateway.breakers["credit_bureau"].record_success()
    clock.advance(31)
    svc.retry_due()
    record("07_dependency_outage_retry", "Dependency outage -> RETRY_PENDING -> automatic retry succeeds",
           "All 3 in-call attempts fail, so the request is parked in RETRY_PENDING with a scheduled retry "
           "(30s backoff). After the bureau recovers, the retry worker resumes it from the checkpointed stage.",
           rec["id"], p, extra=f"- Status while bureau was down: `{status_during}`")

    # 8 rule change
    p = loan(applicant_id="APP-1002", full_name="Vikram Shah", loan_amount=1450000)
    v1 = svc.simulate("loan_application", p, version=1)
    v2 = svc.simulate("loan_application", p, version=2)
    rec, _ = svc.submit("loan_application", p, "demo-8")
    record("08_rule_change", "Rule change via configuration (v1 -> v2), no code change",
           "loan_application.v2.json raises R-CRD-003 from 650 to 700. The what-if endpoint shows the impact "
           "before rollout; live requests are pinned to the version active at intake.", rec["id"], p,
           extra=f"- `POST /workflows/loan_application/simulate?version=1` -> **{v1['decision']}**\n"
                 f"- `POST /workflows/loan_application/simulate?version=2` -> **{v2['decision']}**")

    # 9 conditional branching
    p = loan(applicant_id="APP-1004", full_name="Neha Iyer", annual_income=6000000, loan_amount=4500000,
             existing_debt=0)
    rec, _ = svc.submit("loan_application", p, "demo-9")
    record("09_conditional_branching", "Conditional branching: large loan routed to senior approval",
           "Loan > 20L takes the conditional branch to `senior_approval` (composite ALL rule).", rec["id"], p)

    # 10 claim fast track, 11 tampered claim
    claim = {"claim_id": "CLM-5001", "policy_id": "POL-778", "policy_active": True, "claim_type": "motor",
             "claim_amount": 18000, "coverage_limit": 300000, "incident_date": "2026-10-02",
             "document_id": "DOC-9001", "prior_claims_12m": 0}
    rec, _ = svc.submit("insurance_claim", claim, "demo-10")
    record("10_claim_fast_track", "Insurance claim fast-tracked (second workflow, same engine)",
           "Small verified motor claim (<= 25,000) skips the adjuster stage.", rec["id"], claim)
    claim2 = {**claim, "claim_id": "CLM-5002", "claim_amount": 240000, "document_id": "DOC-9003"}
    rec, _ = svc.submit("insurance_claim", claim2, "demo-11")
    record("11_claim_tampered", "Insurance claim rejected: tampered document",
           "Document verifier reports tampering -> reject; low confidence also raises a flag.", rec["id"], claim2)

    # 12 vendor sanctions hit, 13 screening down -> fail-safe to review
    vendor = {"vendor_id": "VEN-301", "legal_name": "Sanctioned Metals LLC", "country": "AE",
              "tax_id": "AE1234567890", "annual_contract_value": 400000, "years_in_business": 6,
              "bank_account_verified": True, "has_iso_cert": False}
    rec, _ = svc.submit("vendor_approval", vendor, "demo-12")
    record("12_vendor_sanctions_hit", "Vendor rejected: sanctions-list match", "Screening dependency returns a hit.",
           rec["id"], vendor)
    svc.gateway.deps["sanctions_screening"].faults.fail_next(3, "timeout")
    vendor2 = {**vendor, "vendor_id": "VEN-302", "legal_name": "Acme Fasteners Pvt Ltd", "country": "IN",
               "tax_id": "AAACA1234Q"}
    rec, _ = svc.submit("vendor_approval", vendor2, "demo-13")
    record("13_vendor_screening_down", "Screening unavailable -> fail-safe manual review",
           "This workflow configures `on_unavailable: review`: compliance never auto-approves without screening.",
           rec["id"], vendor2)

    # 14 onboarding
    emp = {"employee_id": "EMP-204", "name": "Kavya Nair", "department": "finance", "start_date": "2026-12-01",
           "documents": ["id_proof", "offer_letter_signed"], "background_check": "clear"}
    rec, _ = svc.submit("employee_onboarding", emp, "demo-14")
    record("14_onboarding_missing_nda", "Employee onboarding: conditional document rule",
           "Finance hires must sign an NDA (guarded rule); missing bank details is only a flag.", rec["id"], emp)

    header = ["# Decision Explanation Examples", "",
              "Generated by `python -m decision_platform demo` (deterministic: fixed clock, fixture-backed "
              "dependencies). Each example shows the input, the rules triggered, the output and the audit "
              "reasoning. Full JSON explanations are in `examples/explanations/`.", ""]
    with open(out, "w") as fh:
        fh.write("\n".join(header) + "\n" + "\n".join(sections))
    svc.store.close()
    print(f"\nwrote {out}")
