import copy
import json
import os

from decision_platform.app import DEFAULT_CONFIG_DIR, build_service
from decision_platform.testing import FakeClock

LOAN = {"applicant_id": "APP-1001", "full_name": "Asha Rao", "age": 34, "annual_income": 1200000,
        "loan_amount": 1500000, "employment_status": "salaried", "existing_debt": 150000, "kyc_verified": True}

CLAIM = {"claim_id": "CLM-5001", "policy_id": "POL-778", "policy_active": True, "claim_type": "motor",
         "claim_amount": 18000, "coverage_limit": 300000, "incident_date": "2026-10-02",
         "document_id": "DOC-9001", "prior_claims_12m": 0}

VENDOR = {"vendor_id": "VEN-302", "legal_name": "Acme Fasteners Pvt Ltd", "country": "IN",
          "tax_id": "AAACA1234Q", "annual_contract_value": 400000, "years_in_business": 6,
          "bank_account_verified": True, "has_iso_cert": False}


def loan(**kw):
    return {**copy.deepcopy(LOAN), **kw}


def make_service(**kw):
    clock = FakeClock()
    svc = build_service(clock=clock, sleeper=clock.sleep, **kw)
    return svc, clock


def load_config(name):
    with open(os.path.join(DEFAULT_CONFIG_DIR, name)) as fh:
        return json.load(fh)
