"""Workflow configuration: loading, static validation, versioning, hot reload.

A workflow is pure data (JSON). Adding a workflow or changing a rule is a file
change, not a code change. Configs are validated *at load time* -- unknown
operators, dangling stage references, cycles, duplicate rule ids, undeclared
dependencies -- so a bad edit is rejected before it can affect a decision.

Versions are immutable: a request is pinned to the (workflow, version) that was
active when it arrived, so in-flight requests are never re-judged by rules
that changed underneath them.
"""

import copy
import glob
import hashlib
import json
import os
import threading

from . import conditions, schema
from .errors import ConfigError, NotFound

TERMINAL_TARGETS = ("APPROVED", "REJECTED", "MANUAL_REVIEW")
OUTCOMES = ("pass", "review", "reject")
ON_FAIL = ("reject", "review", "flag")
RULE_TYPES = ("mandatory", "threshold", "conditional", "composite", "dependency", "policy")
ON_UNAVAILABLE = ("retry", "review", "skip")


def canonical_hash(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class WorkflowDefinition:
    def __init__(self, raw, source=None):
        errors = validate_workflow(raw)
        if errors:
            raise ConfigError(f"invalid workflow config {source or raw.get('workflow')}",
                              {"errors": errors, "source": source})
        self.raw = copy.deepcopy(raw)
        self.name = raw["workflow"]
        self.version = raw["version"]
        self.description = raw.get("description", "")
        self.input_schema = raw["input_schema"]
        self.start = raw["start"]
        self.stages = raw["stages"]
        self.dependencies = raw.get("dependencies", {})
        self.max_steps = raw.get("max_steps", 50)
        rp = raw.get("retry_policy", {})
        self.retry_policy = {
            "max_attempts": rp.get("max_attempts", 3),
            "backoff_seconds": rp.get("backoff_seconds", [30, 120, 600]),
            "on_exhausted": rp.get("on_exhausted", "MANUAL_REVIEW"),
        }
        self.hash = canonical_hash(raw)
        self.source = source

    @property
    def ref(self):
        return {"workflow": self.name, "version": self.version, "config_hash": self.hash[:16]}


def validate_workflow(raw):
    """Return a list of human-readable config errors (empty == valid)."""
    errs = []
    if not isinstance(raw, dict):
        return ["config must be a JSON object"]
    for key in ("workflow", "version", "input_schema", "start", "stages"):
        if key not in raw:
            errs.append(f"missing required key '{key}'")
    if errs:
        return errs
    if not isinstance(raw["version"], int) or raw["version"] < 1:
        errs.append("version must be a positive integer")
    errs.extend(schema.check_schema(raw["input_schema"]))

    stages = raw["stages"]
    if not isinstance(stages, dict) or not stages:
        return errs + ["stages must be a non-empty object"]
    for name in stages:
        if name in TERMINAL_TARGETS:
            errs.append(f"stage name '{name}' is reserved")
    if raw["start"] not in stages:
        errs.append(f"start stage '{raw['start']}' is not defined")

    deps = raw.get("dependencies", {})
    rp = raw.get("retry_policy", {})
    if rp.get("on_exhausted", "MANUAL_REVIEW") not in ("MANUAL_REVIEW", "REJECTED", "FAILED"):
        errs.append("retry_policy.on_exhausted must be MANUAL_REVIEW, REJECTED or FAILED")

    seen_rule_ids = set()
    graph = {}
    for sname, stage in stages.items():
        p = f"stages.{sname}"
        graph[sname] = set()
        for i, f in enumerate(stage.get("fetch", [])):
            fp = f"{p}.fetch[{i}]"
            if f.get("dependency") not in deps:
                errs.append(f"{fp}: dependency '{f.get('dependency')}' not declared in 'dependencies'")
            if "store_as" not in f:
                errs.append(f"{fp}: missing 'store_as'")
            if f.get("on_unavailable", "retry") not in ON_UNAVAILABLE:
                errs.append(f"{fp}: on_unavailable must be one of {ON_UNAVAILABLE}")
        for i, rule in enumerate(stage.get("rules", [])):
            rp_ = f"{p}.rules[{i}]"
            rid = rule.get("id")
            if not rid:
                errs.append(f"{rp_}: missing 'id'")
            elif rid in seen_rule_ids:
                errs.append(f"{rp_}: duplicate rule id '{rid}'")
            seen_rule_ids.add(rid)
            if "condition" not in rule:
                errs.append(f"{rp_}: missing 'condition'")
            else:
                errs.extend(conditions.check_condition(rule["condition"], f"{rp_}.condition"))
            if "when" in rule:
                errs.extend(conditions.check_condition(rule["when"], f"{rp_}.when"))
            if rule.get("on_fail", "reject") not in ON_FAIL:
                errs.append(f"{rp_}: on_fail must be one of {ON_FAIL}")
            if rule.get("type", "policy") not in RULE_TYPES:
                errs.append(f"{rp_}: type must be one of {RULE_TYPES}")
        trans = stage.get("transitions", {})
        if "pass" not in trans:
            errs.append(f"{p}.transitions: 'pass' is required")
        for outcome, target in trans.items():
            tp = f"{p}.transitions.{outcome}"
            if outcome not in OUTCOMES:
                errs.append(f"{tp}: unknown outcome (allowed: {OUTCOMES})")
                continue
            for t in _targets(target, tp, errs):
                if t not in stages and t not in TERMINAL_TARGETS:
                    errs.append(f"{tp}: unknown target '{t}'")
                elif t in stages:
                    graph[sname].add(t)

    if not errs:
        cycle = _find_cycle(graph)
        if cycle:
            errs.append(f"stage graph has a cycle: {' -> '.join(cycle)} (workflows must terminate)")
        reachable = _reachable(graph, raw["start"])
        for s in stages:
            if s not in reachable:
                errs.append(f"stage '{s}' is unreachable from start '{raw['start']}'")
    return errs


def _targets(target, path, errs):
    if isinstance(target, str):
        return [target]
    if isinstance(target, list) and target:
        out, has_default = [], False
        for i, branch in enumerate(target):
            if "to" not in branch:
                errs.append(f"{path}[{i}]: branch needs 'to'")
                continue
            if "when" in branch:
                errs.extend(conditions.check_condition(branch["when"], f"{path}[{i}].when"))
            else:
                has_default = True
            out.append(branch["to"])
        if not has_default:
            errs.append(f"{path}: conditional branches need a final default branch without 'when'")
        return out
    errs.append(f"{path}: must be a stage name or a list of branches")
    return []


def _find_cycle(graph):
    WHITE, GREY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}

    def dfs(n, stack):
        color[n] = GREY
        for m in graph[n]:
            if color[m] == GREY:
                return stack + [n, m]
            if color[m] == WHITE:
                found = dfs(m, stack + [n])
                if found:
                    return found
        color[n] = BLACK
        return None

    for n in graph:
        if color[n] == WHITE:
            found = dfs(n, [])
            if found:
                return found
    return None


def _reachable(graph, start):
    seen, todo = set(), [start]
    while todo:
        n = todo.pop()
        if n not in seen:
            seen.add(n)
            todo.extend(graph.get(n, ()))
    return seen


class ConfigRegistry:
    """Thread-safe store of all workflow versions; supports atomic hot reload."""

    def __init__(self, config_dir=None):
        self._lock = threading.RLock()
        self._versions = {}   # name -> {version: WorkflowDefinition}
        self._active = {}     # name -> pinned active version (None => latest)
        self.config_dir = config_dir
        if config_dir:
            self.reload()

    # -- loading ---------------------------------------------------------
    def reload(self):
        """Re-read the config directory. All-or-nothing: if any file is invalid
        the previous configuration stays in force and ConfigError is raised."""
        loaded, errors = {}, []
        for path in sorted(glob.glob(os.path.join(self.config_dir, "*.json"))):
            try:
                with open(path) as fh:
                    raw = json.load(fh)
                wf = WorkflowDefinition(raw, source=os.path.basename(path))
            except json.JSONDecodeError as exc:
                errors.append({"source": os.path.basename(path), "errors": [f"invalid JSON: {exc}"]})
                continue
            except ConfigError as exc:
                errors.append(exc.details)
                continue
            if wf.version in loaded.get(wf.name, {}):
                errors.append({"source": wf.source, "errors": [f"duplicate version {wf.version} of {wf.name}"]})
                continue
            loaded.setdefault(wf.name, {})[wf.version] = wf
        if errors:
            raise ConfigError("config reload rejected; previous configuration kept", {"files": errors})
        with self._lock:
            # Immutability guard: an already-loaded version may not silently change.
            for name, versions in loaded.items():
                for v, wf in versions.items():
                    old = self._versions.get(name, {}).get(v)
                    if old and old.hash != wf.hash:
                        raise ConfigError(
                            f"{name} v{v} changed content; bump the version instead of editing in place",
                            {"workflow": name, "version": v})
            before = {n: sorted(vs) for n, vs in self._versions.items()}
            # Versions are append-only: a version deleted from disk stays loaded so
            # requests pinned to it can still finish. Roll back with activate().
            merged = {n: dict(vs) for n, vs in self._versions.items()}
            for name, versions in loaded.items():
                merged.setdefault(name, {}).update(versions)
            self._versions = merged
            after = {n: sorted(vs) for n, vs in merged.items()}
        return {"before": before, "after": after}

    def register(self, raw):
        """Programmatic registration (used by tests and the admin API)."""
        wf = WorkflowDefinition(raw)
        with self._lock:
            existing = self._versions.setdefault(wf.name, {}).get(wf.version)
            if existing and existing.hash != wf.hash:
                raise ConfigError(f"{wf.name} v{wf.version} already exists with different content")
            self._versions[wf.name][wf.version] = wf
        return wf

    # -- lookup ----------------------------------------------------------
    def get(self, name, version=None):
        with self._lock:
            versions = self._versions.get(name)
            if not versions:
                raise NotFound(f"workflow '{name}' not found")
            if version is None:
                version = self._active.get(name) or max(versions)
            if version not in versions:
                raise NotFound(f"workflow '{name}' version {version} not found")
            return versions[version]

    def activate(self, name, version):
        """Pin the active version (e.g. roll back a bad rule change instantly)."""
        self.get(name, version)
        with self._lock:
            self._active[name] = version
        return self.get(name)

    def list(self):
        with self._lock:
            return [
                {"workflow": n, "versions": sorted(vs), "active_version": self.get(n).version,
                 "description": self.get(n).description}
                for n, vs in sorted(self._versions.items())
            ]
