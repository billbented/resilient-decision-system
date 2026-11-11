"""DecisionService -- the orchestrator (application layer).

Owns the request lifecycle: intake & validation -> idempotency -> stage loop
(fetch dependencies, evaluate rules, checkpoint) -> decision. It coordinates
the pure rule engine, the config registry, the dependency gateway and the
store, but contains no rule logic and no SQL of its own.
"""

import socket
import threading
import uuid

from . import lifecycle as lc
from . import rules, schema
from .conditions import MISSING, resolve
from .config import TERMINAL_TARGETS, canonical_hash
from .dependencies import Clock
from .errors import (ConcurrencyConflict, DependencyUnavailable, IdempotencyConflict,
                     InvalidTransition, NotFound, PermanentDependencyError, StorageError,
                     ValidationError)
from .store import DuplicateRequest


class _Run:
    """Mutable state of one processing pass over a request."""

    def __init__(self, rec, wf):
        self.rec = rec
        self.wf = wf
        self.id = rec["id"]
        self.status = rec["status"]
        self.version = rec["row_version"]
        self.ctx = {"input": rec["payload"], "deps": {},
                    "meta": {"workflow": wf.name, "version": wf.version, "attempt": rec["attempts"]}}
        self.pending_audit = []   # events written atomically with the next checkpoint


class DecisionService:
    def __init__(self, registry, store, gateway, clock=None, worker_id=None, lease_ttl_s=60):
        self.registry = registry
        self.store = store
        self.gateway = gateway
        self.clock = clock or Clock()
        self.worker_id = worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        self.lease_ttl_s = lease_ttl_s

    # ------------------------------------------------------------------ intake
    def submit(self, workflow, payload, idempotency_key=None):
        """Returns ``(record, created)``. ``created`` is False for an idempotent replay."""
        wf = self.registry.get(workflow)
        if not isinstance(payload, dict):
            raise ValidationError("request body must be a JSON object")
        payload_hash = canonical_hash(payload)
        key = idempotency_key or f"auto:{payload_hash}"

        existing = self.store.find_by_key(workflow, key)
        if existing:
            return self._replay(existing, payload_hash), False

        errors = schema.validate(wf.input_schema, payload)
        now = self.clock.now()
        rid = str(uuid.uuid4())
        status = lc.INVALID if errors else lc.IN_PROGRESS
        rec = {
            "id": rid, "workflow": wf.name, "workflow_version": wf.version, "config_hash": wf.hash,
            "idempotency_key": key, "payload_hash": payload_hash, "payload": payload,
            "status": status, "current_stage": None if errors else wf.start,
            "decision": lc.INVALID if errors else None,
            "decision_reasons": {"validation_errors": errors} if errors else None,
            "attempts": 0, "created_at": now, "updated_at": now,
        }
        try:
            with self.store.transaction("intake") as tx:
                self.store.insert_request(tx, rec)
                self.store.add_history(tx, rid, None, lc.RECEIVED, None, "request received", "api", now)
                self.store.append_audit(tx, rid, "request.received", None, {
                    "idempotency_key": key, "payload_hash": payload_hash, "payload": payload,
                    "config": wf.ref}, now)
                lc.check(lc.RECEIVED, status)
                if errors:
                    self.store.add_history(tx, rid, lc.RECEIVED, lc.INVALID, None, "schema validation failed", "api", now)
                    self.store.append_audit(tx, rid, "validation.failed", None, {"errors": errors}, now)
                else:
                    self.store.add_history(tx, rid, lc.RECEIVED, lc.IN_PROGRESS, wf.start, "schema validation passed", "api", now)
                    self.store.append_audit(tx, rid, "validation.passed", None, {"schema_fields": sorted(
                        wf.input_schema.get("properties", {}))}, now)
        except DuplicateRequest:
            # Lost a race with a concurrent identical request: the winner's record is authoritative.
            return self._replay(self.store.find_by_key(workflow, key), payload_hash), False

        if not errors:
            try:
                self.process(rid)
            except ConcurrencyConflict:
                pass  # someone else (retry worker) is already processing it
        return self.store.get_request(rid), True

    def _replay(self, existing, payload_hash):
        if existing["payload_hash"] != payload_hash:
            raise IdempotencyConflict(
                "idempotency key already used with a different payload",
                {"request_id": existing["id"], "original_payload_hash": existing["payload_hash"]})
        try:
            with self.store.transaction("duplicate") as tx:
                self.store.append_audit(tx, existing["id"], "request.duplicate", None,
                                        {"status_at_replay": existing["status"]}, self.clock.now())
        except StorageError:
            pass  # auditing a replay is best-effort; the replay itself must still succeed
        if existing["status"] == lc.IN_PROGRESS:
            # Self-healing: a client retry resumes a request whose processor crashed.
            try:
                self.process(existing["id"])
            except ConcurrencyConflict:
                pass
        return self.store.get_request(existing["id"])

    # -------------------------------------------------------------- processing
    def process(self, request_id):
        owner = f"{self.worker_id}:{threading.get_ident()}"
        if not self.store.acquire_lease(request_id, owner, self.clock.now(), self.lease_ttl_s):
            raise ConcurrencyConflict(f"request {request_id} is being processed by another worker")
        try:
            return self._run(request_id)
        finally:
            try:
                self.store.release_lease(request_id, owner)
            except StorageError:
                pass  # lease expires on its own

    def _run(self, request_id):
        rec = self.store.get_request(request_id)
        if rec is None:
            raise NotFound(f"request {request_id} not found")
        if rec["status"] != lc.IN_PROGRESS:
            return rec
        run = _Run(rec, self.registry.get(rec["workflow"], rec["workflow_version"]))
        stage = rec["current_stage"]
        steps = 0
        while stage not in TERMINAL_TARGETS:
            steps += 1
            if steps > run.wf.max_steps:
                with self.store.transaction("fail") as tx:
                    self._transition(tx, run, lc.FAILED, stage, "max_steps exceeded", "engine",
                                     decision=lc.FAILED, decision_reasons={"summary": "max_steps exceeded"})
                break
            sdef = run.wf.stages[stage]
            if not self._fetch(run, stage, sdef):
                break  # diverted to RETRY_PENDING / MANUAL_REVIEW
            result = rules.evaluate_stage(stage, sdef, run.ctx)
            nxt = result["next"]
            now = self.clock.now()
            reasons = self._decision_reasons(run, result) if nxt in TERMINAL_TARGETS else None
            with self.store.transaction(f"checkpoint:{stage}") as tx:
                for event in run.pending_audit:
                    self.store.append_audit(tx, run.id, event[0], stage, event[1], now)
                self.store.append_audit(tx, run.id, "stage.evaluated", stage, result, now)
                if nxt in TERMINAL_TARGETS:
                    self._transition(tx, run, nxt, stage, f"stage '{stage}' outcome={result['outcome']}",
                                     "engine", decision=nxt, decision_reasons=reasons, current_stage=nxt)
                    self.store.append_audit(tx, run.id, "decision.made", stage,
                                            {"decision": nxt, **reasons}, now)
                else:
                    run.version = self.store.update_request(tx, run.id, run.version,
                                                            current_stage=nxt, updated_at=now)
            run.pending_audit = []
            stage = nxt
        return self.store.get_request(request_id)

    def _fetch(self, run, stage, sdef):
        """Call the stage's external dependencies (or reuse cached results).
        Returns False if the request was diverted."""
        for f in sdef.get("fetch", []):
            name = f["dependency"]
            dep_input = {k: _materialize(v, run.ctx) for k, v in f.get("input", {}).items()}
            input_hash = canonical_hash(dep_input)
            cached = self.store.get_dependency_result(run.id, name, input_hash)
            if cached:
                run.ctx["deps"][f["store_as"]] = cached["output"]
                run.pending_audit.append(("dependency.reused", {"dependency": name, "cached_at": cached["at"]}))
                continue
            try:
                output, attempts = self.gateway.call(name, dep_input, run.wf.dependencies.get(name, {}))
            except (DependencyUnavailable, PermanentDependencyError) as exc:
                if not self._dependency_failed(run, stage, f, exc):
                    return False
                continue
            now = self.clock.now()
            # Persist the result immediately (own tx): if the stage checkpoint later
            # fails, resume reuses this result instead of calling the dependency again.
            with self.store.transaction(f"dependency:{name}") as tx:
                self.store.save_dependency_result(tx, run.id, name, input_hash, output, attempts, now)
                self.store.append_audit(tx, run.id, "dependency.called", stage, {
                    "dependency": name, "input": dep_input, "output": output, "attempts": attempts}, now)
            run.ctx["deps"][f["store_as"]] = output
        return True

    def _dependency_failed(self, run, stage, fetch, exc):
        """Apply the configured ``on_unavailable`` policy. Returns True to continue the stage."""
        name = fetch["dependency"]
        policy = fetch.get("on_unavailable", "retry")
        permanent = isinstance(exc, PermanentDependencyError)
        details = {"dependency": name, "error": exc.message, "error_type": exc.code,
                   "attempts": exc.details.get("attempts", []), "policy": policy}
        if "breaker" in exc.details:
            details["circuit_breaker"] = exc.details["breaker"]

        if policy == "skip":
            run.pending_audit.append(("dependency.skipped", details))
            return True

        now = self.clock.now()
        with self.store.transaction(f"divert:{stage}") as tx:
            for event in run.pending_audit:
                self.store.append_audit(tx, run.id, event[0], stage, event[1], now)
            run.pending_audit = []
            self.store.append_audit(tx, run.id, "dependency.failed", stage, details, now)
            if policy == "review" or permanent:
                why = "permanent dependency error" if permanent else "dependency unavailable (policy=review)"
                reasons = {"decided_at_stage": stage, "outcome": "review", "dependency_failure": details,
                           "summary": f"{name}: {why}; routed to a human"}
                self._transition(tx, run, lc.MANUAL_REVIEW, stage, why, "engine",
                                 decision=lc.MANUAL_REVIEW, decision_reasons=reasons)
                self.store.append_audit(tx, run.id, "decision.made", stage, {"decision": lc.MANUAL_REVIEW, **reasons}, now)
                return False

            attempts = run.rec["attempts"] + 1
            rp = run.wf.retry_policy
            if attempts >= rp["max_attempts"]:
                final = rp["on_exhausted"]
                reasons = {"decided_at_stage": stage, "outcome": "retries_exhausted", "dependency_failure": details,
                           "summary": f"{name} unavailable after {attempts} workflow attempts; on_exhausted={final}"}
                self._transition(tx, run, final, stage, "workflow retries exhausted", "engine",
                                 attempts=attempts, decision=final, decision_reasons=reasons, next_retry_at=None)
                self.store.append_audit(tx, run.id, "decision.made", stage, {"decision": final, **reasons}, now)
            else:
                backoff = rp["backoff_seconds"][min(attempts - 1, len(rp["backoff_seconds"]) - 1)]
                self._transition(tx, run, lc.RETRY_PENDING, stage, f"{name} unavailable; retry #{attempts} scheduled",
                                 "engine", attempts=attempts, next_retry_at=now + backoff)
                self.store.append_audit(tx, run.id, "retry.scheduled", stage, {
                    "attempt": attempts, "max_attempts": rp["max_attempts"],
                    "next_retry_in_s": backoff, "next_retry_at": now + backoff}, now)
        return False

    def _transition(self, tx, run, to_status, stage, reason, actor, **fields):
        lc.check(run.status, to_status)
        now = self.clock.now()
        run.version = self.store.update_request(tx, run.id, run.version, status=to_status, updated_at=now, **fields)
        self.store.add_history(tx, run.id, run.status, to_status, stage, reason, actor, now)
        run.status = to_status

    def _decision_reasons(self, run, final_result):
        prior = [e["data"] for e in self.store.audit(run.id) if e["event"] == "stage.evaluated"]
        flags = [f for r in prior for f in r.get("flags", [])] + final_result["flags"]
        decisive = [
            {k: r.get(k) for k in ("rule_id", "description", "effect", "explanation", "reason")}
            for r in sorted(final_result["rules"], key=lambda r: r["effect"] != "reject")
            if r["effect"] in ("reject", "review")
        ]
        return {
            "decided_at_stage": final_result["stage"],
            "outcome": final_result["outcome"],
            "decisive_rules": decisive,
            "flags": flags,
            "path": [r["stage"] for r in prior] + [final_result["stage"]],
            "branch": final_result.get("branch"),
        }

    # --------------------------------------------------- retries & recovery
    def retry(self, request_id, actor="api"):
        rec = self._get(request_id)
        if rec["status"] != lc.RETRY_PENDING:
            raise InvalidTransition(f"only RETRY_PENDING requests can be retried (status is {rec['status']})")
        wf = self.registry.get(rec["workflow"], rec["workflow_version"])
        run = _Run(rec, wf)
        now = self.clock.now()
        with self.store.transaction("retry") as tx:
            self._transition(tx, run, lc.IN_PROGRESS, rec["current_stage"],
                             f"retry #{rec['attempts']}", actor, next_retry_at=None)
            self.store.append_audit(tx, run.id, "retry.started", rec["current_stage"],
                                    {"attempt": rec["attempts"], "actor": actor}, now)
        return self.process(request_id)

    def retry_due(self):
        """Background sweep: run due retries and resume requests whose processor died."""
        now = self.clock.now()
        done = {"retried": [], "resumed": [], "skipped": []}
        for rec in self.store.due_retries(now):
            try:
                self.retry(rec["id"], actor="retry-worker")
                done["retried"].append(rec["id"])
            except (ConcurrencyConflict, InvalidTransition):
                done["skipped"].append(rec["id"])
        for rec in self.store.stale_in_progress(now):
            try:
                self.process(rec["id"])
                done["resumed"].append(rec["id"])
            except ConcurrencyConflict:
                done["skipped"].append(rec["id"])
        return done

    # --------------------------------------------------------- human review
    def review(self, request_id, decision, reviewer, notes=""):
        mapping = {"approve": lc.APPROVED, "reject": lc.REJECTED}
        if decision not in mapping:
            raise ValidationError("decision must be 'approve' or 'reject'")
        if not reviewer:
            raise ValidationError("reviewer is required (accountability)")
        rec = self._get(request_id)
        wf = self.registry.get(rec["workflow"], rec["workflow_version"])
        run = _Run(rec, wf)
        final = mapping[decision]
        now = self.clock.now()
        reasons = dict(rec.get("decision_reasons") or {})
        reasons["manual_review"] = {"reviewer": reviewer, "decision": final, "notes": notes, "at": now,
                                    "system_recommendation": rec["decision"]}
        with self.store.transaction("review") as tx:
            self._transition(tx, run, final, rec["current_stage"], f"manual review by {reviewer}",
                             f"reviewer:{reviewer}", decision=final, decision_reasons=reasons)
            self.store.append_audit(tx, run.id, "review.completed", None, reasons["manual_review"], now)
        return self.store.get_request(request_id)

    # ----------------------------------------------------------- read side
    def _get(self, request_id):
        rec = self.store.get_request(request_id)
        if rec is None:
            raise NotFound(f"request {request_id} not found")
        return rec

    def get(self, request_id):
        rec = self._get(request_id)
        return {k: v for k, v in rec.items() if k not in ("lease_owner", "lease_expires_at")}

    def history(self, request_id):
        self._get(request_id)
        return self.store.history(request_id)

    def audit(self, request_id):
        self._get(request_id)
        return {"entries": self.store.audit(request_id), "integrity": self.store.verify_audit(request_id)}

    def explain(self, request_id):
        rec = self._get(request_id)
        return build_explanation(rec, self.store.audit(request_id), self.store.history(request_id),
                                 self.store.verify_audit(request_id))

    # ------------------------------------------------------------ what-if
    def simulate(self, workflow, payload, version=None, dependency_outputs=None):
        """Dry run: evaluate a payload against any config version without
        persisting anything. Used to preview the impact of a rule change."""
        wf = self.registry.get(workflow, version)
        errors = schema.validate(wf.input_schema, payload if isinstance(payload, dict) else None)
        if errors:
            return {"config": wf.ref, "decision": lc.INVALID, "validation_errors": errors}
        ctx = {"input": payload, "deps": {}, "meta": {"workflow": wf.name, "version": wf.version, "attempt": 0}}
        dependency_outputs = dependency_outputs or {}
        stages, stage = [], wf.start
        while stage not in TERMINAL_TARGETS and len(stages) < wf.max_steps:
            sdef = wf.stages[stage]
            for f in sdef.get("fetch", []):
                if f["store_as"] in dependency_outputs:
                    ctx["deps"][f["store_as"]] = dependency_outputs[f["store_as"]]
                else:
                    dep_input = {k: _materialize(v, ctx) for k, v in f.get("input", {}).items()}
                    try:
                        ctx["deps"][f["store_as"]], _ = self.gateway.call(
                            f["dependency"], dep_input, wf.dependencies.get(f["dependency"], {}))
                    except (DependencyUnavailable, PermanentDependencyError) as exc:
                        return {"config": wf.ref, "decision": "UNDETERMINED", "stages": stages,
                                "error": exc.message}
            result = rules.evaluate_stage(stage, sdef, ctx)
            stages.append(result)
            stage = result["next"]
        return {"config": wf.ref, "decision": stage, "path": [s["stage"] for s in stages], "stages": stages}


def _materialize(value, ctx):
    if isinstance(value, str) and value.startswith("$"):
        v = resolve(value[1:], ctx)
        return None if v is MISSING else v
    return value


# ---------------------------------------------------------------- explanation
_ICON = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP"}


def build_explanation(rec, audit, history, integrity):
    stages, deps, review, retries = [], [], None, []
    for e in audit:
        d = e["data"]
        if e["event"] == "stage.evaluated":
            stages.append(d)
        elif e["event"] in ("dependency.called", "dependency.failed", "dependency.skipped"):
            deps.append({"event": e["event"], "stage": e["stage"], **{k: d.get(k) for k in (
                "dependency", "output", "error", "attempts", "policy")}})
        elif e["event"] == "review.completed":
            review = d
        elif e["event"] == "retry.scheduled":
            retries.append(d)

    lines = [f"Request {rec['id']} | workflow {rec['workflow']} v{rec['workflow_version']} "
             f"(config {rec['config_hash'][:12]}) | status {rec['status']}"]
    reasons = rec.get("decision_reasons") or {}
    if rec["status"] == lc.INVALID:
        lines.append("Rejected at intake: input failed schema validation:")
        lines.extend(f"  - {err}" for err in reasons.get("validation_errors", []))
    for e in audit:  # chronological narrative
        d = e["data"]
        if e["event"] in ("dependency.called", "dependency.failed", "dependency.skipped"):
            n_att = len(d.get("attempts") or [])
            if e["event"] == "dependency.called":
                lines.append(f"Dependency {d['dependency']} consulted at stage '{e['stage']}' "
                             f"({n_att} attempt{'s' if n_att != 1 else ''}): {d['output']}")
            else:
                lines.append(f"Dependency {d['dependency']} FAILED at stage '{e['stage']}' after {n_att} "
                             f"attempt(s): {d['error']} -> policy '{d['policy']}'")
        elif e["event"] == "dependency.reused":
            lines.append(f"Dependency {d['dependency']} result reused from cache (not called again)")
        elif e["event"] == "retry.scheduled":
            lines.append(f"Workflow retry #{d['attempt']} of {d['max_attempts']} scheduled in {d['next_retry_in_s']}s")
        elif e["event"] == "retry.started":
            lines.append(f"Workflow retry #{d['attempt']} started by {d['actor']} (resumes at the checkpointed stage)")
        elif e["event"] == "stage.evaluated":
            how = ""
            if d.get("branch") and d["branch"].get("explanation"):
                how = f" via branch [{d['branch']['explanation']}]"
            lines.append(f"Stage '{d['stage']}': {d['summary']['passed']} passed, {d['summary']['failed']} failed, "
                         f"{d['summary']['skipped']} skipped -> outcome {d['outcome'].upper()} -> {d['next']}{how}")
            for r in d["rules"]:
                effect = f" => {r['effect'].upper()}" if r.get("effect") else ""
                lines.append(f"   [{_ICON[r['status']]}] {r['rule_id']} {r['description']}: {r['explanation']}{effect}")
    engine_decision = review["system_recommendation"] if review else rec["decision"]
    label = "Engine outcome" if review else "Decision"
    if reasons.get("decisive_rules"):
        why = "; ".join(f"{r['rule_id']} ({r['effect']})" for r in reasons["decisive_rules"])
        lines.append(f"{label} {engine_decision} driven by: {why}")
    elif reasons.get("summary"):
        lines.append(f"{label} {engine_decision}: {reasons['summary']}")
    elif rec["decision"] in (lc.APPROVED,) and not review:
        lines.append(f"Decision {rec['decision']}: every applicable rule passed")
    if reasons.get("flags"):
        lines.append(f"Flags raised (non-blocking): {', '.join(reasons['flags'])}")
    if review:
        lines.append(f"Manual review by {review['reviewer']}: {review['decision']} "
                     f"(system recommended {review['system_recommendation']}). Notes: {review['notes'] or '-'}")
    lines.append(f"Audit trail: {integrity['entries']} entries, hash chain "
                 f"{'VALID' if integrity['valid'] else 'BROKEN at seq ' + str(integrity.get('broken_at_seq'))}")

    return {
        "request_id": rec["id"],
        "workflow": rec["workflow"],
        "workflow_version": rec["workflow_version"],
        "config_hash": rec["config_hash"],
        "status": rec["status"],
        "decision": rec["decision"],
        "input": rec["payload"],
        "path": [s["stage"] for s in stages],
        "rules_triggered": [
            {"stage": s["stage"], "rule_id": r["rule_id"], "status": r["status"], "effect": r.get("effect"),
             "explanation": r["explanation"], "data_refs": r.get("data_refs", {})}
            for s in stages for r in s["rules"]
        ],
        "dependencies": deps,
        "decision_reasons": reasons,
        "manual_review": review,
        "state_history": [f"{h['from_status'] or '-'} -> {h['to_status']} ({h['reason']})" for h in history],
        "audit_integrity": integrity,
        "narrative": lines,
    }
