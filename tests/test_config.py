import copy
import json
import os
import shutil
import tempfile
import unittest

from decision_platform.app import DEFAULT_CONFIG_DIR
from decision_platform.config import ConfigRegistry, WorkflowDefinition, validate_workflow
from decision_platform.errors import ConfigError, NotFound
from tests.helpers import load_config


class ConfigValidationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config("loan_application.v1.json")

    def errors_after(self, mutate):
        cfg = copy.deepcopy(self.cfg)
        mutate(cfg)
        return "\n".join(validate_workflow(cfg))

    def test_shipped_configs_are_valid(self):
        reg = ConfigRegistry(DEFAULT_CONFIG_DIR)
        names = {w["workflow"] for w in reg.list()}
        self.assertEqual(names, {"loan_application", "insurance_claim", "vendor_approval", "employee_onboarding"})

    def test_unknown_transition_target(self):
        e = self.errors_after(lambda c: c["stages"]["eligibility"]["transitions"].update({"pass": "nowhere"}))
        self.assertIn("unknown target 'nowhere'", e)

    def test_cycle_rejected(self):
        e = self.errors_after(lambda c: c["stages"]["affordability"]["transitions"].update({"pass": "eligibility"}))
        self.assertIn("cycle", e)

    def test_duplicate_rule_id(self):
        e = self.errors_after(lambda c: c["stages"]["affordability"]["rules"][0].update({"id": "R-ELG-001"}))
        self.assertIn("duplicate rule id", e)

    def test_undeclared_dependency(self):
        e = self.errors_after(lambda c: c.pop("dependencies"))
        self.assertIn("not declared", e)

    def test_branches_need_default(self):
        e = self.errors_after(lambda c: c["stages"]["affordability"]["transitions"]["pass"].pop())
        self.assertIn("default branch", e)

    def test_unreachable_stage(self):
        e = self.errors_after(lambda c: c["stages"].update({"orphan": {"transitions": {"pass": "APPROVED"}}}))
        self.assertIn("unreachable", e)

    def test_bad_operator(self):
        e = self.errors_after(lambda c: c["stages"]["eligibility"]["rules"][0]["condition"].update({"op": "≈"}))
        self.assertIn("unknown operator", e)

    def test_definition_raises(self):
        with self.assertRaises(ConfigError):
            WorkflowDefinition({"workflow": "x"})


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        shutil.copy(os.path.join(DEFAULT_CONFIG_DIR, "loan_application.v1.json"), self.dir)
        self.reg = ConfigRegistry(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write(self, name, obj):
        with open(os.path.join(self.dir, name), "w") as fh:
            fh.write(obj if isinstance(obj, str) else json.dumps(obj))

    def test_hot_reload_adds_version(self):
        self.write("loan_application.v2.json", load_config("loan_application.v2.json"))
        self.reg.reload()
        self.assertEqual(self.reg.get("loan_application").version, 2)
        self.assertEqual(self.reg.get("loan_application", 1).version, 1)  # old version still served

    def test_bad_reload_is_atomic(self):
        self.write("broken.json", "{not json")
        with self.assertRaises(ConfigError):
            self.reg.reload()
        self.assertEqual(self.reg.get("loan_application").version, 1)  # previous config kept

    def test_editing_a_published_version_in_place_is_refused(self):
        cfg = load_config("loan_application.v1.json")
        cfg["stages"]["credit_assessment"]["rules"][0]["condition"]["value"] = 1
        self.write("loan_application.v1.json", cfg)
        with self.assertRaises(ConfigError):
            self.reg.reload()

    def test_activate_rollback(self):
        self.write("loan_application.v2.json", load_config("loan_application.v2.json"))
        self.reg.reload()
        self.reg.activate("loan_application", 1)
        self.assertEqual(self.reg.get("loan_application").version, 1)

    def test_unknown_workflow(self):
        with self.assertRaises(NotFound):
            self.reg.get("nope")


if __name__ == "__main__":
    unittest.main()
