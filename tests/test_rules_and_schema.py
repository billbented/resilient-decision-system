import unittest

from decision_platform import conditions, rules, schema


class SchemaTests(unittest.TestCase):
    S = {"type": "object", "required": ["a", "b"], "additionalProperties": False,
         "properties": {"a": {"type": "integer", "minimum": 1}, "b": {"type": "string", "pattern": "^X-[0-9]+$"},
                        "c": {"type": "array", "items": {"type": "string", "enum": ["p", "q"]}},
                        "d": {"type": "string", "format": "date"}}}

    def test_valid(self):
        self.assertEqual(schema.validate(self.S, {"a": 3, "b": "X-1", "c": ["p"], "d": "2026-01-31"}), [])

    def test_reports_all_errors_at_once(self):
        errs = schema.validate(self.S, {"a": 0, "c": ["z"], "d": "31/01/2026", "zz": 1})
        joined = "\n".join(errs)
        for frag in ("$.b: is required", "$.a: must be >= 1", "$.c[0]: must be one of", "not a valid ISO date",
                     "$.zz: unexpected property"):
            self.assertIn(frag, joined)

    def test_bool_is_not_integer(self):
        self.assertTrue(schema.validate({"type": "integer"}, True))

    def test_schema_self_check(self):
        self.assertTrue(schema.check_schema({"type": "strng"}))
        self.assertTrue(schema.check_schema({"type": "string", "patern": "x"}))


class ConditionTests(unittest.TestCase):
    CTX = {"input": {"age": 30, "income": 100, "tags": ["a", "b"], "name": "Bob"}, "deps": {"c": {"score": 700}}}

    def ev(self, cond):
        return conditions.evaluate(cond, self.CTX)[0]

    def test_operators(self):
        cases = [("gte", "input.age", 30, True), ("gt", "input.age", 30, False), ("lt", "input.age", 31, True),
                 ("between", "input.age", [18, 65], True), ("in", "input.age", [1, 30], True),
                 ("not_in", "input.age", [30], False), ("contains", "input.tags", "b", True),
                 ("matches", "input.name", "^B.b$", True), ("ne", "deps.c.score", 700, False)]
        for op, field, value, expected in cases:
            with self.subTest(op=op):
                self.assertEqual(self.ev({"field": field, "op": op, "value": value}), expected)

    def test_missing_field_is_false_not_error(self):
        ok, trace = conditions.evaluate({"field": "input.nope", "op": "gte", "value": 1}, self.CTX)
        self.assertFalse(ok)
        self.assertEqual(trace["note"], "field missing")
        self.assertTrue(self.ev({"field": "input.nope", "op": "not_exists"}))

    def test_type_mismatch_is_false_with_note(self):
        ok, trace = conditions.evaluate({"field": "input.name", "op": "gt", "value": 3}, self.CTX)
        self.assertFalse(ok)
        self.assertIn("type error", trace["note"])

    def test_derived_threshold_reference(self):
        cond = {"field": "input.age", "op": "lte", "value": {"ref": "input.income", "multiply": 0.5}}
        ok, trace = conditions.evaluate(cond, self.CTX)
        self.assertTrue(ok)
        self.assertEqual(conditions.data_refs(trace), {"input.age": 30, "input.income": 100})
        self.assertIn("input.income x0.5", conditions.describe(trace))

    def test_composites_trace_every_branch(self):
        cond = {"all": [{"field": "input.age", "op": "gte", "value": 18},
                        {"any": [{"field": "deps.c.score", "op": "gt", "value": 800},
                                 {"not": {"field": "input.name", "op": "eq", "value": "Eve"}}]}]}
        ok, trace = conditions.evaluate(cond, self.CTX)
        self.assertTrue(ok)
        self.assertEqual(len(trace["all"][1]["any"]), 2)  # no short-circuit: full trace

    def test_static_check_rejects_bad_conditions(self):
        self.assertTrue(conditions.check_condition({"field": "input.a", "op": "approx", "value": 1}, "c"))
        self.assertTrue(conditions.check_condition({"field": "input.a", "op": "gt"}, "c"))
        self.assertTrue(conditions.check_condition({"field": "os.system", "op": "eq", "value": 1}, "c"))
        self.assertTrue(conditions.check_condition({"field": "input.a", "op": "matches", "value": "("}, "c"))


class StageEvaluationTests(unittest.TestCase):
    STAGE = {"rules": [
        {"id": "A", "condition": {"field": "input.x", "op": "gte", "value": 10}, "on_fail": "review"},
        {"id": "B", "condition": {"field": "input.x", "op": "gte", "value": 5}, "on_fail": "reject"},
        {"id": "C", "when": {"field": "input.kind", "op": "eq", "value": "vip"},
         "condition": {"field": "input.x", "op": "gte", "value": 100}, "on_fail": "reject"},
        {"id": "D", "condition": {"field": "input.x", "op": "lt", "value": 3}, "on_fail": "flag"}],
        "transitions": {"pass": [{"when": {"field": "input.x", "op": "gt", "value": 50}, "to": "big"},
                                 {"to": "APPROVED"}]}}

    def run_stage(self, x, kind="std"):
        return rules.evaluate_stage("s", self.STAGE, {"input": {"x": x, "kind": kind}})

    def test_reject_outranks_review(self):
        r = self.run_stage(4)
        self.assertEqual((r["outcome"], r["next"]), ("reject", "REJECTED"))

    def test_review_default_target(self):
        r = self.run_stage(7)
        self.assertEqual((r["outcome"], r["next"]), ("review", "MANUAL_REVIEW"))
        self.assertEqual(r["flags"], ["D"])  # flag never blocks

    def test_guard_skips_rule(self):
        r = self.run_stage(20)
        self.assertEqual([x["status"] for x in r["rules"]], ["passed", "passed", "skipped", "failed"])
        self.assertEqual(r["next"], "APPROVED")

    def test_guard_applies_rule(self):
        self.assertEqual(self.run_stage(20, "vip")["outcome"], "reject")

    def test_conditional_branch(self):
        r = self.run_stage(60)
        self.assertEqual(r["next"], "big")
        self.assertEqual(r["branch"]["branch"], 0)

    def test_deterministic(self):
        self.assertEqual(self.run_stage(7), self.run_stage(7))


if __name__ == "__main__":
    unittest.main()
