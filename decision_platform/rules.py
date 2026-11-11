"""Pure rule & stage evaluation -- no I/O, no clock, no randomness.

Because evaluation is a pure function of (stage config, context), it is
deterministic: re-running a stage after a crash or a retry yields the same
result, which is what makes stage-level checkpoint/resume safe.
"""

from . import conditions

SEVERITY = {"pass": 0, "review": 1, "reject": 2}


def evaluate_rule(rule, ctx):
    result = {
        "rule_id": rule["id"],
        "description": rule.get("description", ""),
        "type": rule.get("type", "policy"),
        "on_fail": rule.get("on_fail", "reject"),
    }
    refs = {}
    if "when" in rule:
        guard_ok, guard_trace = conditions.evaluate(rule["when"], ctx)
        result["guard"] = {"result": guard_ok, "explanation": conditions.describe(guard_trace)}
        conditions.data_refs(guard_trace, refs)
        if not guard_ok:
            result.update(status="skipped", effect=None, data_refs=refs,
                          explanation=f"not applicable: {conditions.describe(guard_trace)}")
            return result
    ok, trace = conditions.evaluate(rule["condition"], ctx)
    conditions.data_refs(trace, refs)
    result.update(
        status="passed" if ok else "failed",
        effect=None if ok else result["on_fail"],
        explanation=conditions.describe(trace),
        trace=trace,
        data_refs=refs,
    )
    if not ok and rule.get("reason"):
        result["reason"] = rule["reason"]
    return result


def resolve_transition(target, ctx):
    """A target is a stage name or an ordered list of ``{when, to}`` branches
    (first match wins; the last branch is the default)."""
    if isinstance(target, str):
        return target, None
    for i, branch in enumerate(target):
        if "when" not in branch:
            return branch["to"], {"branch": i, "default": True}
        ok, trace = conditions.evaluate(branch["when"], ctx)
        if ok:
            return branch["to"], {"branch": i, "explanation": conditions.describe(trace)}
    raise AssertionError("unreachable: config validation guarantees a default branch")


def evaluate_stage(stage_name, stage, ctx):
    rule_results = [evaluate_rule(r, ctx) for r in stage.get("rules", [])]
    outcome = "pass"
    for r in rule_results:
        if r["effect"] in SEVERITY and SEVERITY[r["effect"]] > SEVERITY[outcome]:
            outcome = r["effect"]
    transitions = stage.get("transitions", {})
    defaults = {"reject": "REJECTED", "review": "MANUAL_REVIEW"}
    target_spec = transitions.get(outcome, defaults.get(outcome))
    target, branch = resolve_transition(target_spec, ctx)
    return {
        "stage": stage_name,
        "outcome": outcome,
        "next": target,
        "branch": branch,
        "rules": rule_results,
        "flags": [r["rule_id"] for r in rule_results if r["effect"] == "flag"],
        "summary": {
            "passed": sum(r["status"] == "passed" for r in rule_results),
            "failed": sum(r["status"] == "failed" for r in rule_results),
            "skipped": sum(r["status"] == "skipped" for r in rule_results),
        },
    }
