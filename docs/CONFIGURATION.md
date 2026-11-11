# Configuration Model

Every workflow is one JSON file per version in `configs/` (`<workflow>.v<N>.json`). The engine has no
workflow-specific code. Validate all configs with `python3 -m decision_platform validate-config`.

## Top level

```jsonc
{
  "workflow": "loan_application",          // unique name (URL path segment)
  "version": 2,                             // positive int, immutable once published
  "description": "…",
  "changelog": ["R-CRD-003: 650 -> 700"],   // optional, free text
  "input_schema": { … },                    // JSON-Schema subset, validated at intake
  "dependencies": {                         // external services this workflow may call
    "credit_bureau": {"timeout_ms": 500,
                      "retry": {"max_attempts": 3, "base_delay_ms": 20, "max_delay_ms": 200}}
  },
  "retry_policy": {                         // workflow-level retry when a dependency is down
    "max_attempts": 3, "backoff_seconds": [30, 120, 600], "on_exhausted": "MANUAL_REVIEW"
  },
  "max_steps": 50,                          // optional safety guard
  "start": "eligibility",
  "stages": { "<stage_name>": { … } }
}
```

## Stage

```jsonc
"credit_assessment": {
  "description": "Pull bureau data and apply thresholds",
  "fetch": [{                                     // optional: call dependencies before the rules
    "dependency": "credit_bureau",
    "input": {"applicant_id": "$input.applicant_id"},  // "$path" pulls from the context
    "store_as": "credit",                          // result available as deps.credit.*
    "on_unavailable": "retry"                      // retry | review | skip
  }],
  "rules": [ … ],
  "transitions": {
    "pass":   "affordability",                     // required
    "review": "MANUAL_REVIEW",                     // default
    "reject": "REJECTED"                           // default
  }
}
```

A transition target is either a stage name, a terminal (`APPROVED`, `REJECTED`, `MANUAL_REVIEW`), or an
ordered list of **conditional branches**. The first matching branch wins, and the last branch must be a
default with no `when`:

```json
"pass": [
  {"when": {"field": "input.loan_amount", "op": "gt", "value": 2000000}, "to": "senior_approval"},
  {"to": "APPROVED"}
]
```

## Rule

```jsonc
{
  "id": "R-ELG-004",                       // unique across the workflow; appears in audit
  "type": "conditional",                   // mandatory | threshold | conditional | composite | dependency | policy
  "description": "Self-employed applicants need income >= 5,00,000",
  "when": {"field": "input.employment_status", "op": "eq", "value": "self_employed"},  // optional guard
  "condition": {"field": "input.annual_income", "op": "gte", "value": 500000},         // must hold to pass
  "on_fail": "review",                     // reject | review | flag (flag = record, never block)
  "reason": "optional business justification shown in explanations"
}
```

**Stage outcome** is the most severe effect among the failed rules: `reject` > `review` > `pass`.
Rules whose `when` guard is false are `skipped` and do not count.

## Conditions

| Form | Example |
|---|---|
| Comparison | `{"field": "input.age", "op": "between", "value": [21, 65]}` |
| Derived threshold | `{"field": "input.loan_amount", "op": "lte", "value": {"ref": "input.annual_income", "multiply": 5}}` |
| Cross-field | `{"field": "input.claim_amount", "op": "lte", "value": {"ref": "input.coverage_limit"}}` |
| Dependency data | `{"field": "deps.credit.score", "op": "gte", "value": 700}` |
| Composite | `{"all": [ … ]}`, `{"any": [ … ]}`, `{"not": { … }}` |

Operators: `eq ne gt gte lt lte in not_in contains matches between exists not_exists`.
Fields must start with `input.`, `deps.` or `meta.`. A missing field makes the comparison **false**
(and the trace says `field missing`), never an exception.

## What is validated at load time

A file that fails any of these checks is rejected, and the previous configuration stays live:

* required keys are present;
* the schema keywords are known;
* the start stage exists;
* every transition target exists;
* there are no cycles and no unreachable stages;
* conditional branch lists have a default;
* operators are known and have the right arity;
* regexes compile;
* rule ids are unique;
* fetches reference declared dependencies;
* enums (`on_fail`, `on_unavailable`, `on_exhausted`, rule `type`) are valid;
* a published version has not been edited in place.

## Common changes

| Requirement change | What to do |
|---|---|
| Change a threshold | Copy to `vN+1`, edit `value`, then `POST /admin/reload` |
| Add a mandatory check | Add a rule with `on_fail: reject` |
| Soft-launch a rule | Add it with `on_fail: flag`, watch the flags, then switch to `review` / `reject` |
| New routing for high-value cases | Add a stage + a conditional branch on `pass` |
| New external check | Declare it under `dependencies`, add a `fetch`, then use `deps.<store_as>.*` in rules |
| Never auto-approve when a check is down | `on_unavailable: review` |
| Preview impact | `POST /workflows/{wf}/simulate?version=N` with sample payloads |
| Roll back | `POST /admin/workflows/{wf}/activate {"version": N-1}` |
| New business process | New file `configs/<name>.v1.json`, with no code change |
