# Resilient Decision System — Configurable Workflow Decision Platform

A generic engine that takes structured requests, validates them, runs them through **configurable
multi-stage workflows** (rules, thresholds, conditional branches, external dependency calls), keeps a
**full lifecycle history and a tamper-evident audit trail**, explains every decision, and survives
**dependency outages, partial save failures, duplicate requests and concurrent workers**.

Four business workflows ship as pure JSON config, with no workflow-specific code:
`loan_application` (v1 + v2), `insurance_claim`, `vendor_approval` and `employee_onboarding`.

* **Zero third-party dependencies.** It runs on Python 3.10+ standard library only (sqlite3, http.server, unittest).
* **85 automated tests.** They cover the happy path, invalid input, duplicates (including 12-thread races),
  dependency failure, the retry flow, rule changes, partial saves, the circuit breaker, audit tampering and the HTTP API.

```
docs/ARCHITECTURE.md        system design, components, data flow, trade-offs, assumptions, scaling
docs/CONFIGURATION.md       the configuration model (how to add a workflow / change a rule)
docs/DECISION_EXAMPLES.md   14 worked examples: input -> rules triggered -> output -> audit reasoning
docs/Solution_Report.pdf    the full write-up as a PDF (15 pages)
docs/Submission_Summary.pdf two-page submission summary
```

## Quick start

```bash
python3 --version                    # 3.10+
python3 -m unittest discover -s tests -t . -v   # run the test-suite (≈1s)
python3 -m decision_platform demo    # run 14 scenarios, regenerate docs/DECISION_EXAMPLES.md
python3 -m decision_platform serve   # REST API on http://127.0.0.1:8080 (+ background retry worker)
```

### Try the API

```bash
# submit (Idempotency-Key is optional; without it the payload hash is used)
curl -s -X POST localhost:8080/workflows/loan_application/requests \
     -H 'Idempotency-Key: demo-1' -d @examples/payloads/loan_review.json

# same key again -> 200 + header "Idempotent-Replay: true", no re-processing
# same key, different body -> 409 idempotency_conflict

curl -s localhost:8080/requests/<id>/explain     # why was this decided?
curl -s localhost:8080/requests/<id>/audit       # hash-chained audit log + integrity check
curl -s localhost:8080/requests/<id>/history     # lifecycle transitions
curl -s -X POST localhost:8080/requests/<id>/review -d '{"decision":"approve","reviewer":"officer1"}'

# what-if: how would v1 vs v2 rules decide this payload? (nothing persisted)
curl -s -X POST 'localhost:8080/workflows/loan_application/simulate?version=1' -d @examples/payloads/loan_review.json

# chaos: take the credit bureau down, submit, watch RETRY_PENDING -> auto-retry after recovery
curl -s -X POST localhost:8080/admin/faults/credit_bureau -d '{"outage": true}'
curl -s -X POST localhost:8080/admin/faults/credit_bureau -d '{"reset": true}'
```

### CLI

```bash
python3 -m decision_platform submit loan_application examples/payloads/loan_approve.json --key k1
python3 -m decision_platform explain <request_id>
python3 -m decision_platform review <request_id> approve --reviewer officer1
python3 -m decision_platform simulate loan_application examples/payloads/loan_review.json --version 1
python3 -m decision_platform retry-due
python3 -m decision_platform validate-config
```

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/workflows/{name}/requests` | Submit (`Idempotency-Key` header). 201 created · 200 replay · 409 key conflict · 422 invalid |
| POST | `/workflows/{name}/simulate[?version=N]` | Dry-run / impact analysis of a rule change |
| GET | `/workflows`, `/workflows/{name}` | List workflows & versions / view config |
| GET | `/requests[?status=&workflow=]` | List requests |
| GET | `/requests/{id}` · `/history` · `/audit` · `/explain` | State, lifecycle, audit log, explanation |
| POST | `/requests/{id}/retry` | Retry a `RETRY_PENDING` request now |
| POST | `/requests/{id}/review` | Human decision on a `MANUAL_REVIEW` request |
| POST | `/admin/reload` | Hot-reload configs (atomic; invalid config is rejected, old stays live) |
| POST | `/admin/workflows/{name}/activate` | Pin the active version (instant rollback) |
| POST | `/admin/retry-due` | Run the retry / stuck-request recovery sweep |
| POST | `/admin/faults/{dependency}` | Fault injection: `outage`, `fail_next`, `kind`, `latency_ms`, `reset` |
| GET | `/admin/dependencies` · `/health` | Circuit-breaker state, fault state, call counts |

## Project layout

```
decision_platform/
  schema.py        input validation (JSON-Schema subset, all errors at once)
  conditions.py    declarative condition DSL + evaluation trace + static checks (no eval)
  rules.py         pure rule/stage evaluation and transition resolution
  config.py        workflow model, load-time validation, versioned registry, hot reload
  lifecycle.py     request state machine (the only place that knows legal transitions)
  dependencies.py  simulated external services, fault injection, timeout/retry/backoff, circuit breaker
  store.py         SQLite: requests, history, hash-chained audit, dep-result cache, leases, idempotency
  service.py       orchestration: intake, stage loop, checkpoints, retries, review, explain, simulate
  api.py           REST transport (no business logic)
  __main__.py      CLI          demo.py   scripted scenarios          app.py   composition root
configs/           one JSON file per workflow version
tests/             85 unittest cases
examples/          payloads, dependency fixtures, generated JSON explanations
```

## Test coverage map

| Required scenario | Where |
|---|---|
| Happy path | `test_workflow.test_happy_path_approved`, `test_api.test_submit_explain_audit` |
| Invalid input | `test_workflow.test_invalid_input`, `test_api.test_invalid_input_422`, `test_rules_and_schema.SchemaTests` |
| Duplicate requests | `test_idempotency.*` (incl. 12 concurrent threads, key conflict, key-order independence) |
| Dependency failure | `test_failures.DependencyFailureTests`, `CircuitBreakerTests`, real timeout |
| Retry flow | `test_outage_parks_request_then_retry_succeeds`, `test_retries_exhausted_goes_to_configured_fallback` |
| Rule change | `test_rule_change_and_audit.RuleChangeTests` (hot reload, version pinning, simulate, rollback, new workflow) |
| Partial save / consistency | `StorageFailureTests` (mid-transaction failure, resume exactly-once, optimistic locking, leases) |
| Auditability | `AuditTests` (data refs, hash-chain tamper detection, explanation) |
