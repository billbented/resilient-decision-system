# Architecture

## 1. Problem framing and boundaries

The platform has to make **business decisions that are correct, repeatable and explainable**, even when
inputs are bad, dependencies fail, clients retry and requirements change. I split the problem into five
concerns and gave each one a single owner:

| Concern | Question it answers | Owner |
|---|---|---|
| **What** to decide | Which checks, thresholds, branches? | Workflow config (JSON data) |
| **How** to evaluate | Does this rule pass for this data? | `rules.py` + `conditions.py` (pure functions) |
| **When / in which order** | Which stage next, when to stop, when to retry? | `service.py` + `lifecycle.py` |
| **Remembering** | What happened, in what order, can we prove it? | `store.py` (transactions, audit chain) |
| **Talking to the outside** | Calling flaky services safely | `dependencies.py` (gateway) |

**Out of scope (by assumption):** authentication/authorisation, multi-tenant isolation, a UI for
editing rules, and real third-party integrations (they are simulated behind the same interface).

## 2. Components

```
            ┌────────────── transport ──────────────┐
  client ─▶ │ api.py (REST)      __main__.py (CLI)   │   no business logic
            └───────────────────┬────────────────────┘
                                ▼
            ┌──────────── application ──────────────┐
            │ service.py  DecisionService           │  intake · idempotency · stage loop ·
            │   uses lifecycle.py (state machine)   │  checkpoints · retries · review · explain
            └──┬───────────┬──────────────┬──────────┘
               ▼           ▼              ▼
     ┌──────────────┐ ┌──────────┐ ┌───────────────────┐
     │ config.py    │ │ rules.py │ │ dependencies.py   │
     │ registry,    │ │ + conds  │ │ gateway: timeout, │
     │ versions,    │ │ (pure)   │ │ retry+jitter,     │
     │ validation   │ └──────────┘ │ circuit breaker   │
     └──────────────┘              └─────────┬─────────┘
               ▼                             ▼
     ┌─────────────────────────┐   ┌─────────────────────┐
     │ store.py (SQLite)       │   │ simulated services  │
     │ requests · history ·    │   │ credit bureau, doc  │
     │ audit chain · dep cache │   │ verifier, sanctions │
     │ · leases · idem. keys   │   │ + FaultInjector     │
     └─────────────────────────┘   └─────────────────────┘
```

`app.py` is the **composition root**. It is the only place that knows the concrete classes, so swapping
SQLite for Postgres or a simulator for a real HTTP client is a one-file change.

## 3. Data flow (one request)

1. **Intake.** The API receives `POST /workflows/{wf}/requests`, plus an optional `Idempotency-Key`.
2. **Idempotency.** The service looks up `(workflow, key)`. If it exists with the same payload hash, the
   stored result is returned (HTTP 200 + `Idempotent-Replay`). If it exists with a different hash, the
   request is refused with **409**. With no key, the canonical payload hash is used as the key.
3. **Validation.** The payload is checked against the workflow's `input_schema`, and *all* errors are
   collected. Invalid requests are still persisted (`INVALID`) and audited, then answered with **422**.
4. **Pinning.** The request records `workflow_version` and `config_hash`. It will always be judged by
   that version, even if the rules change while it waits for a retry.
5. **Insert.** The request row, history and audit entries are written in **one transaction**. A UNIQUE
   constraint on `(workflow, idempotency_key)` settles concurrent duplicates at the database level.
6. **Lease.** A processing lease is acquired with a conditional UPDATE, so only one worker processes a
   request at a time.
7. **Stage loop.** For each stage:
   1. **Fetch dependencies.** Reuse the cached result for this request if one exists. Otherwise call
      through the gateway (timeout → retries with exponential backoff and full jitter → circuit
      breaker), then persist the result and its attempt log in its own transaction.
   2. **Evaluate rules (pure).** Each rule produces a status (`passed`, `failed` or `skipped` when its
      guard is false), an effect, a human-readable explanation and the data it referenced. The stage
      outcome is the most severe effect: `reject` > `review` > `pass`. `flag` effects are recorded but
      never block.
   3. **Resolve the transition.** Either a fixed target or ordered conditional branches.
   4. **Checkpoint.** In one transaction: `current_stage`, the `stage.evaluated` audit entry with the
      full trace, and the history/decision if the target is terminal.
8. **Divert on dependency failure.** The fetch's configured `on_unavailable` policy decides:
   * `retry`: go to `RETRY_PENDING` with a configured backoff schedule, up to `max_attempts`, then
     `on_exhausted`.
   * `review`: go to `MANUAL_REVIEW` (fail-safe).
   * `skip`: continue without the data.

   A permanent error goes straight to review, because retrying cannot fix it.
9. **Background worker.** `retry_due()` resumes due retries *and* any `IN_PROGRESS` request whose lease
   expired (for example after a crash).

## 4. Request lifecycle

```
            ┌──────────── INVALID (terminal)
RECEIVED ───┤
            └──▶ IN_PROGRESS ──▶ APPROVED | REJECTED | FAILED (terminal)
                    │   ▲  └──▶ MANUAL_REVIEW ──(human)──▶ APPROVED | REJECTED
                    ▼   │
                 RETRY_PENDING
```

`lifecycle.check()` enforces this table on every status change. An illegal move, such as
`REJECTED → APPROVED`, raises `InvalidTransition` (HTTP 409). Every transition writes a `state_history`
row with the actor (`api`, `engine`, `retry-worker` or `reviewer:<name>`) and the reason.

## 5. Robustness — failure handling matrix

| Failure | Mechanism | Result |
|---|---|---|
| Malformed / invalid input | Schema validation, all errors at once | 422, persisted as `INVALID`, audited |
| Duplicate request (sequential) | Idempotency key + payload hash | Original result returned, no re-processing |
| Duplicate request (concurrent) | DB UNIQUE constraint + processing lease | Exactly one record, dependency called once |
| Same key, different payload | Payload-hash comparison | 409 `idempotency_conflict` |
| Transient dependency error | Bounded retries, exponential backoff with full jitter (capped) | Usually absorbed inside one call |
| Dependency timeout | Real timeout via a future with a deadline | Treated as transient |
| Sustained outage | Circuit breaker opens; workflow-level `RETRY_PENDING` with a backoff schedule | Fails fast, recovers automatically |
| Retries exhausted | `retry_policy.on_exhausted` (default `MANUAL_REVIEW`) | Never stuck forever |
| Permanent dependency error | Not retried, does not trip the breaker | Straight to manual review |
| Partial save (crash mid-write) | One transaction per checkpoint; rollback | Request stays at last good checkpoint |
| Crash after a dependency call | Dependency results cached per request | Resume never re-calls (exactly-once effect) |
| Worker dies mid-processing | Lease with TTL + recovery sweep | Another worker resumes it |
| Lost update / race | Optimistic concurrency (`row_version`) | Conflict detected, not silently overwritten |
| Bad config deployed | Load-time validation; all-or-nothing reload | Rejected; previous config stays live |
| Infinite workflow | Cycle detection at load + `max_steps` guard | Cannot loop |

**Why resume is safe.** Rule evaluation is a pure function of (config version, input, dependency
results). Those three are all persisted, so re-running a stage after a failure gives the same answer.
The checkpoint guarantees that each stage appears in the audit exactly once.

## 6. Configurability

Workflows are JSON documents (see [CONFIGURATION.md](CONFIGURATION.md)) containing:

* the input schema;
* the declared dependencies and their timeout and retry policy;
* the workflow-level retry policy;
* the stages, each with fetches, rules (`when` guard, `condition`, `on_fail`) and transitions
  (fixed or conditional branches).

* **Change a threshold.** Add `loan_application.v2.json`, then `POST /admin/reload`. New requests use v2.
  In-flight requests stay on v1.
* **Preview the impact first.** `POST /workflows/{wf}/simulate?version=N`.
* **Roll back instantly.** `POST /admin/workflows/{wf}/activate {"version": 1}`.
* **Add a workflow.** Drop a new JSON file. `employee_onboarding` has zero lines of workflow-specific code.
* **Safety.** Conditions are data, not code. There is no `eval`, fields are restricted to the
  `input.` / `deps.` / `meta.` namespaces, and operators come from a whitelist. Editing a published
  version in place is refused (versions are immutable).

## 7. Auditability and explainability

* Each audit entry records the event, stage, data and timestamp. Entries are **hash-chained per
  request** (`hash = sha256(prev_hash | entry)`), and `GET /requests/{id}/audit` re-verifies the chain.
  Editing any row breaks it, which a test demonstrates.
* `stage.evaluated` entries keep the **full rule trace**: actual values, expected values, derived
  thresholds (e.g. `input.annual_income x5`), guard results, effects and `data_refs`. That is exactly
  the data each rule looked at.
* Dependency calls record their input, output and every attempt (outcome, error, backoff).
* Every decision is tied to `workflow_version` and `config_hash`, so it can be reproduced later with the
  exact rules that were live.
* `GET /requests/{id}/explain` turns this into a chronological narrative. See
  [DECISION_EXAMPLES.md](DECISION_EXAMPLES.md).

## 8. Trade-offs (and why)

| Decision | Alternative | Why this choice |
|---|---|---|
| JSON condition DSL | Embedded scripting (Python/JS expressions), a rules-engine library | Safe (no code execution), statically validated at load, diffable, explainable value by value. It is less expressive, but derived thresholds and composites cover the use cases. |
| Stage graph must be acyclic | Allow loops | Guarantees termination and simple reasoning. "Loops" such as retries are handled by the lifecycle, not the graph. |
| Stage-level checkpoints | Per-rule checkpoints, or none | Rules are pure and cheap, so re-running one stage costs little. Fewer writes. |
| Persist dependency results immediately | Persist only at the checkpoint | Avoids double-calling an external service, which may cost money or have side effects. |
| Synchronous processing on submit, plus a background retry worker | Fully asynchronous queue | The caller gets an answer in one round trip, and slow paths (`RETRY_PENDING`) are handled asynchronously. A queue is the documented scale-out path (§10). |
| SQLite, stdlib only | Postgres + FastAPI + Redis | Zero setup for evaluators. All guarantees (transactions, UNIQUE, conditional UPDATE) map one-to-one onto Postgres. |
| Payload hash as fallback idempotency key | Require a key | Protects naive clients from double submits. The cost is that two genuinely identical requests merge. That is accepted, and documented, because a client can always send its own key. |
| `reject` > `review` > `pass` precedence, every rule evaluated | Short-circuit on first failure | Full explanations (every failing reason is shown), at negligible cost. |
| Fail-safe defaults (exhausted → `MANUAL_REVIEW`, sanctions down → review) | Auto-reject or auto-approve | A human decides when the system lacks information. This is configurable per workflow. |
| Invalid requests are persisted | Reject without storing | Complete audit of what was received. Replaying with the same key is stable. |

## 9. Assumptions

* Requests are JSON objects of at most 1 MB. Decisions are needed in near-real time, with seconds of
  latency acceptable on the retry path.
* External dependencies are read-only lookups; caching their result per request is acceptable.
* A human reviewer is available for `MANUAL_REVIEW`. Reviewer identity is supplied by the caller,
  because auth is out of scope.
* Clock skew between workers is smaller than the lease TTL (60 s).
* Configs are authored by trusted operators, but they are still validated, because typos are the
  common failure.

## 10. Scaling considerations

| Pressure | Approach |
|---|---|
| Throughput | The API and workers are stateless. Run N instances behind a load balancer; leases and the UNIQUE constraint already make concurrent workers safe. Move to Postgres (`SELECT … FOR UPDATE SKIP LOCKED` for the retry sweep). |
| Slow dependencies | Make submit asynchronous: return 202 + `request_id` and process from a queue (SQS/Kafka) partitioned by request id. The stage loop is already resumable, so it maps directly onto a consumer. |
| Dependency protection | Per-dependency circuit breakers already exist. Add bulkheads (separate pools) and rate limits per dependency, and share breaker state via Redis across instances. |
| Audit volume | Audit is append-only, so partition by month and archive to object storage (Parquet). The hash chain is per request, so partitions are independent. |
| Read load (explain/history) | Use read replicas. The explanation is derived from immutable audit rows, so it caches well. |
| Config distribution | Configs are immutable and versioned, so they can be published to a config service or object store and cached by `(workflow, version)`. Reload is atomic. |
| Hot rows / idempotency table growth | TTL on idempotency keys (e.g. 30 days) via a separate key table. |
| Observability | Structured JSON request logs exist. Add metrics per workflow, stage and rule (decision mix, review rate, dependency latency, breaker state) and alerts on retry backlog. |
