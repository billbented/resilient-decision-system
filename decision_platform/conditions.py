"""Declarative condition language (JSON) with a full evaluation trace.

A condition is one of:

    {"field": "input.age", "op": "gte", "value": 18}
    {"field": "input.amount", "op": "lte", "value": {"ref": "input.annual_income", "multiply": 5}}
    {"all": [<cond>, ...]}     # logical AND
    {"any": [<cond>, ...]}     # logical OR
    {"not": <cond>}

There is deliberately *no* ``eval`` and no embedded scripting: conditions are
data, so they can be validated at load time, diffed between versions, and
explained after the fact. Every evaluation returns ``(result, trace)`` where
``trace`` records the actual values seen -- this is what makes decisions
explainable.
"""

import re


class _Missing:
    def __repr__(self):
        return "<missing>"

    def __bool__(self):
        return False


MISSING = _Missing()


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _ordered(fn):
    def op(a, b):
        if (_is_num(a) and _is_num(b)) or (isinstance(a, str) and isinstance(b, str)):
            return fn(a, b)
        raise TypeError(f"cannot compare {type(a).__name__} with {type(b).__name__}")
    return op


def _between(a, b):
    if not (isinstance(b, list) and len(b) == 2):
        raise TypeError("'between' expects [low, high]")
    return _ordered(lambda x, lo: x >= lo)(a, b[0]) and _ordered(lambda x, hi: x <= hi)(a, b[1])


OPERATORS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": _ordered(lambda a, b: a > b),
    "gte": _ordered(lambda a, b: a >= b),
    "lt": _ordered(lambda a, b: a < b),
    "lte": _ordered(lambda a, b: a <= b),
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
    "contains": lambda a, b: b in a,
    "matches": lambda a, b: re.fullmatch(b, a) is not None,
    "between": _between,
    "exists": None,       # handled specially (no value)
    "not_exists": None,
}

SYMBOLS = {"eq": "==", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
           "in": "in", "not_in": "not in", "contains": "contains", "matches": "matches",
           "between": "between", "exists": "exists", "not_exists": "is missing"}

_VALUE_MODIFIERS = {"multiply", "add"}


def resolve(path, ctx):
    """Resolve a dotted path (``input.applicant.age``) against the context."""
    cur = ctx
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return MISSING
    return cur


def resolve_value(spec, ctx):
    """A literal, or ``{"ref": path, "multiply": k, "add": c}`` (derived threshold)."""
    if isinstance(spec, dict) and "ref" in spec:
        v = resolve(spec["ref"], ctx)
        if v is MISSING:
            return MISSING
        if "multiply" in spec:
            v = v * spec["multiply"]
        if "add" in spec:
            v = v + spec["add"]
        return v
    return spec


def evaluate(cond, ctx):
    """Return ``(bool, trace)``. Evaluates every branch (no short-circuit) so the
    trace is complete -- explainability is worth more than a few microseconds."""
    if "all" in cond:
        parts = [evaluate(c, ctx) for c in cond["all"]]
        result = all(r for r, _ in parts)
        return result, {"all": [t for _, t in parts], "result": result}
    if "any" in cond:
        parts = [evaluate(c, ctx) for c in cond["any"]]
        result = any(r for r, _ in parts)
        return result, {"any": [t for _, t in parts], "result": result}
    if "not" in cond:
        inner, t = evaluate(cond["not"], ctx)
        return (not inner), {"not": t, "result": not inner}

    field, op = cond["field"], cond["op"]
    actual = resolve(field, ctx)
    trace = {"field": field, "op": op, "actual": None if actual is MISSING else actual}

    if op == "exists":
        result = actual is not MISSING and actual is not None
    elif op == "not_exists":
        result = actual is MISSING or actual is None
    else:
        raw = cond.get("value")
        expected = resolve_value(raw, ctx)
        trace["expected"] = None if expected is MISSING else expected
        if isinstance(raw, dict) and "ref" in raw:
            src = resolve(raw["ref"], ctx)
            trace["expected_ref"] = raw
            trace["ref_value"] = None if src is MISSING else src
        if actual is MISSING:
            result = False
            trace["note"] = "field missing"
        elif expected is MISSING:
            result = False
            trace["note"] = f"reference {raw['ref']} missing"
        else:
            try:
                result = bool(OPERATORS[op](actual, expected))
            except (TypeError, re.error) as exc:
                result = False
                trace["note"] = f"type error: {exc}"
    trace["result"] = result
    return result, trace


def describe(trace):
    """Render a trace as a one-line human explanation."""
    for key, joiner in (("all", " AND "), ("any", " OR ")):
        if key in trace:
            inner = joiner.join(describe(t) for t in trace[key])
            return f"({inner})"
    if "not" in trace:
        return f"NOT {describe(trace['not'])}"
    actual = "<missing>" if trace.get("note") == "field missing" or (
        trace["actual"] is None and trace["op"] in ("exists", "not_exists")) else repr(trace["actual"])
    text = f"{trace['field']}={actual} {SYMBOLS.get(trace['op'], trace['op'])}"
    if "expected" in trace:
        exp = repr(trace["expected"])
        ref = trace.get("expected_ref")
        if ref:
            mods = "".join(f" x{ref['multiply']}" if k == "multiply" else f" +{ref['add']}"
                           for k in ("multiply", "add") if k in ref)
            exp = f"{exp} ({ref['ref']}{mods})"
        text += f" {exp}"
    text += f" -> {'true' if trace['result'] else 'false'}"
    if trace.get("note"):
        text += f" [{trace['note']}]"
    return text


def data_refs(trace, out=None):
    """Collect every ``path -> value`` the condition looked at (audit data refs)."""
    out = {} if out is None else out
    for key in ("all", "any"):
        if key in trace:
            for t in trace[key]:
                data_refs(t, out)
            return out
    if "not" in trace:
        return data_refs(trace["not"], out)
    out[trace["field"]] = trace["actual"]
    ref = trace.get("expected_ref")
    if ref:
        out[ref["ref"]] = trace.get("ref_value")
    return out


def check_condition(cond, path):
    """Static validation used by the config loader."""
    errors = []
    if not isinstance(cond, dict):
        return [f"{path}: condition must be an object"]
    if "all" in cond or "any" in cond:
        key = "all" if "all" in cond else "any"
        if not isinstance(cond[key], list) or not cond[key]:
            return [f"{path}.{key}: must be a non-empty list"]
        for i, c in enumerate(cond[key]):
            errors.extend(check_condition(c, f"{path}.{key}[{i}]"))
        return errors
    if "not" in cond:
        return check_condition(cond["not"], f"{path}.not")
    if "field" not in cond or "op" not in cond:
        return [f"{path}: leaf condition needs 'field' and 'op'"]
    op = cond["op"]
    if op not in OPERATORS:
        errors.append(f"{path}: unknown operator '{op}' (allowed: {sorted(OPERATORS)})")
    elif op not in ("exists", "not_exists") and "value" not in cond:
        errors.append(f"{path}: operator '{op}' requires 'value'")
    root = str(cond["field"]).split(".")[0]
    if root not in ("input", "deps", "meta"):
        errors.append(f"{path}: field must start with input., deps. or meta. (got '{cond['field']}')")
    val = cond.get("value")
    if isinstance(val, dict):
        if "ref" not in val:
            errors.append(f"{path}: object value must be a {{'ref': ...}} reference")
        extra = set(val) - {"ref"} - _VALUE_MODIFIERS
        if extra:
            errors.append(f"{path}: unknown value modifiers {sorted(extra)}")
    if op == "between" and not (isinstance(val, list) and len(val) == 2):
        errors.append(f"{path}: 'between' expects [low, high]")
    if op == "matches" and isinstance(val, str):
        try:
            re.compile(val)
        except re.error as exc:
            errors.append(f"{path}: invalid regex ({exc})")
    return errors
