"""REST API (stdlib ``http.server``; thin transport layer -- no business logic).

POST /workflows/{name}/requests          submit (header Idempotency-Key optional)
POST /workflows/{name}/simulate          dry-run / what-if (optional ?version=N)
GET  /workflows                          list workflows and versions
GET  /workflows/{name}[?version=N]       view a workflow config
GET  /requests[?status=&workflow=]       list requests
GET  /requests/{id}                      current state
GET  /requests/{id}/history              lifecycle transitions
GET  /requests/{id}/audit                audit log + hash-chain integrity
GET  /requests/{id}/explain              human-readable decision explanation
POST /requests/{id}/retry                retry a RETRY_PENDING request now
POST /requests/{id}/review               {"decision": "approve|reject", "reviewer": "...", "notes": "..."}
POST /admin/reload                       hot-reload configs (atomic; bad config rejected)
POST /admin/workflows/{name}/activate    {"version": N}  pin / roll back active version
POST /admin/retry-due                    run the retry/recovery sweep now
POST /admin/faults/{dependency}          {"fail_next": n, "kind": "...", "outage": bool, "latency_ms": n, "reset": bool}
GET  /admin/dependencies                 circuit breaker + fault state
GET  /health
"""

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import PlatformError, ValidationError

MAX_BODY = 1_000_000


def make_handler(service):
    routes = []

    def route(method, pattern):
        def deco(fn):
            routes.append((method, re.compile(f"^{pattern}$"), fn))
            return fn
        return deco

    def _version(q):
        return int(q["version"][0]) if "version" in q else None

    @route("GET", "/health")
    def health(h, q, body):
        return 200, {"status": "ok", "workflows": len(service.registry.list())}

    @route("GET", "/workflows")
    def list_workflows(h, q, body):
        return 200, service.registry.list()

    @route("GET", r"/workflows/(?P<name>[\w-]+)")
    def get_workflow(h, q, body, name):
        wf = service.registry.get(name, _version(q))
        return 200, {**wf.ref, "definition": wf.raw}

    @route("POST", r"/workflows/(?P<name>[\w-]+)/requests")
    def submit(h, q, body, name):
        rec, created = service.submit(name, body, h.headers.get("Idempotency-Key"))
        if rec["status"] == "INVALID":
            return 422, {"error": "validation_failed", "request_id": rec["id"],
                         "errors": rec["decision_reasons"]["validation_errors"], "idempotent_replay": not created}
        out = service.get(rec["id"])
        out["idempotent_replay"] = not created
        return (201 if created else 200), out

    @route("POST", r"/workflows/(?P<name>[\w-]+)/simulate")
    def simulate(h, q, body, name):
        body = body or {}
        payload = body.get("input", body)
        return 200, service.simulate(name, payload, _version(q), body.get("dependency_outputs"))

    @route("GET", "/requests")
    def list_requests(h, q, body):
        rows = service.store.list_requests(q.get("status", [None])[0], q.get("workflow", [None])[0],
                                           int(q.get("limit", [100])[0]))
        return 200, [{k: r[k] for k in ("id", "workflow", "workflow_version", "status", "decision",
                                        "current_stage", "attempts", "created_at")} for r in rows]

    @route("GET", r"/requests/(?P<rid>[\w-]+)")
    def get_request(h, q, body, rid):
        return 200, service.get(rid)

    @route("GET", r"/requests/(?P<rid>[\w-]+)/history")
    def history(h, q, body, rid):
        return 200, service.history(rid)

    @route("GET", r"/requests/(?P<rid>[\w-]+)/audit")
    def audit(h, q, body, rid):
        return 200, service.audit(rid)

    @route("GET", r"/requests/(?P<rid>[\w-]+)/explain")
    def explain(h, q, body, rid):
        return 200, service.explain(rid)

    @route("POST", r"/requests/(?P<rid>[\w-]+)/retry")
    def retry(h, q, body, rid):
        return 200, service.retry(rid)

    @route("POST", r"/requests/(?P<rid>[\w-]+)/review")
    def review(h, q, body, rid):
        body = body or {}
        return 200, service.review(rid, body.get("decision"), body.get("reviewer"), body.get("notes", ""))

    @route("POST", "/admin/reload")
    def reload(h, q, body):
        return 200, service.registry.reload()

    @route("POST", r"/admin/workflows/(?P<name>[\w-]+)/activate")
    def activate(h, q, body, name):
        if not body or not isinstance(body.get("version"), int):
            raise ValidationError("body must be {\"version\": <int>}")
        return 200, service.registry.activate(name, body["version"]).ref

    @route("POST", "/admin/retry-due")
    def retry_due(h, q, body):
        return 200, service.retry_due()

    @route("POST", r"/admin/faults/(?P<dep>[\w-]+)")
    def faults(h, q, body, dep):
        d = service.gateway.deps.get(dep)
        if d is None:
            raise ValidationError(f"unknown dependency '{dep}'")
        body = body or {}
        if body.get("reset"):
            d.faults.reset()
            service.gateway.breakers[dep].record_success()
        if body.get("fail_next"):
            d.faults.fail_next(int(body["fail_next"]), body.get("kind", "transient"))
        if "outage" in body:
            d.faults.set_outage(bool(body["outage"]))
        if "latency_ms" in body:
            d.faults.latency_ms = int(body["latency_ms"])
        return 200, {"dependency": dep, "faults": d.faults.describe()}

    @route("GET", "/admin/dependencies")
    def deps(h, q, body):
        return 200, {n: {"breaker": service.gateway.breakers[n].snapshot(), "faults": d.faults.describe(),
                         "calls": d.calls} for n, d in service.gateway.deps.items()}

    class Handler(BaseHTTPRequestHandler):
        server_version = "DecisionPlatform/1.0"

        def log_message(self, fmt, *args):  # quiet by default; structured log below
            pass

        def _dispatch(self, method):
            started = time.perf_counter()
            url = urlparse(self.path)
            query = parse_qs(url.query)
            status, payload = 404, {"error": "not_found", "message": f"no route for {method} {url.path}"}
            try:
                body = self._body() if method == "POST" else None
                for m, rx, fn in routes:
                    match = rx.match(url.path)
                    if m == method and match:
                        status, payload = fn(self, query, body, **match.groupdict())
                        break
                else:
                    if any(rx.match(url.path) for _, rx, _ in routes):
                        status, payload = 405, {"error": "method_not_allowed"}
            except PlatformError as exc:
                status, payload = exc.http_status, exc.to_dict()
            except Exception as exc:  # never leak a stack trace to clients
                status, payload = 500, {"error": "internal_error", "message": type(exc).__name__}
            self._send(status, payload, replay=isinstance(payload, dict) and payload.get("idempotent_replay"))
            if getattr(self.server, "verbose", False):
                print(json.dumps({"method": method, "path": url.path, "status": status,
                                  "ms": round((time.perf_counter() - started) * 1000, 1)}), flush=True)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValidationError("request body too large")
            if not length:
                return None
            try:
                return json.loads(self.rfile.read(length))
            except json.JSONDecodeError as exc:
                raise ValidationError(f"malformed JSON: {exc}")

        def _send(self, status, payload, replay=False):
            data = json.dumps(payload, indent=2, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if replay:
                self.send_header("Idempotent-Replay", "true")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


def serve(service, host="127.0.0.1", port=8080, retry_interval_s=5.0, verbose=True):
    server = ThreadingHTTPServer((host, port), make_handler(service))
    server.verbose = verbose
    stop = threading.Event()

    def retry_worker():
        while not stop.wait(retry_interval_s):
            try:
                service.retry_due()
            except Exception as exc:  # worker must never die
                print(json.dumps({"retry_worker_error": str(exc)}), flush=True)

    if retry_interval_s:
        threading.Thread(target=retry_worker, daemon=True, name="retry-worker").start()
    return server, stop
