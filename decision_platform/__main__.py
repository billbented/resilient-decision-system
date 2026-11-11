"""CLI.  python -m decision_platform <command>

  serve            start the REST API (+ background retry worker)
  submit           submit a request from a JSON file
  show / explain / audit / history <request_id>
  review <id> approve|reject --reviewer NAME
  retry <id> | retry-due
  simulate         dry-run a payload (what-if), optionally against --version
  validate-config  statically validate all workflow configs
  workflows        list workflows and versions
  demo             run the scripted scenarios and write docs/DECISION_EXAMPLES.md
"""

import argparse
import json
import sys

from .app import DEFAULT_CONFIG_DIR, build_service
from .errors import PlatformError


def _print(obj):
    print(json.dumps(obj, indent=2, default=str))


def main(argv=None):
    p = argparse.ArgumentParser(prog="decision_platform", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="decision_platform.db", help="SQLite file (default: %(default)s)")
    p.add_argument("--config-dir", default=DEFAULT_CONFIG_DIR)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--retry-interval", type=float, default=5.0)

    s = sub.add_parser("submit")
    s.add_argument("workflow")
    s.add_argument("file", help="JSON payload file ('-' for stdin)")
    s.add_argument("--key", help="idempotency key")

    for name in ("show", "explain", "audit", "history", "retry"):
        sub.add_parser(name).add_argument("request_id")

    s = sub.add_parser("review")
    s.add_argument("request_id")
    s.add_argument("decision", choices=["approve", "reject"])
    s.add_argument("--reviewer", required=True)
    s.add_argument("--notes", default="")

    s = sub.add_parser("simulate")
    s.add_argument("workflow")
    s.add_argument("file")
    s.add_argument("--version", type=int)

    sub.add_parser("retry-due")
    sub.add_parser("validate-config")
    sub.add_parser("workflows")
    s = sub.add_parser("demo")
    s.add_argument("--out", default=None, help="markdown output path")

    args = p.parse_args(argv)

    if args.cmd == "demo":
        from .demo import run_demo
        run_demo(args.out)
        return 0
    if args.cmd == "validate-config":
        from .config import ConfigRegistry
        try:
            reg = ConfigRegistry(args.config_dir)
        except PlatformError as exc:
            _print(exc.to_dict())
            return 1
        _print({"valid": True, "workflows": reg.list()})
        return 0

    svc = build_service(args.config_dir, args.db)
    try:
        if args.cmd == "serve":
            from .api import serve
            server, stop = serve(svc, args.host, args.port, args.retry_interval)
            print(f"Decision platform listening on http://{args.host}:{args.port}  (db={args.db})", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                stop.set()
                server.shutdown()
        elif args.cmd == "submit":
            payload = json.load(sys.stdin if args.file == "-" else open(args.file))
            rec, created = svc.submit(args.workflow, payload, args.key)
            _print({"request_id": rec["id"], "status": rec["status"], "decision": rec["decision"],
                    "idempotent_replay": not created, "reasons": rec["decision_reasons"]})
        elif args.cmd == "show":
            _print(svc.get(args.request_id))
        elif args.cmd == "explain":
            print("\n".join(svc.explain(args.request_id)["narrative"]))
        elif args.cmd == "audit":
            _print(svc.audit(args.request_id))
        elif args.cmd == "history":
            _print(svc.history(args.request_id))
        elif args.cmd == "retry":
            _print(svc.retry(args.request_id))
        elif args.cmd == "retry-due":
            _print(svc.retry_due())
        elif args.cmd == "review":
            _print(svc.review(args.request_id, args.decision, args.reviewer, args.notes))
        elif args.cmd == "simulate":
            res = svc.simulate(args.workflow, json.load(open(args.file)), args.version)
            _print({"config": res["config"], "decision": res["decision"], "path": res.get("path"),
                    "failed_rules": [r["rule_id"] for s in res.get("stages", []) for r in s["rules"]
                                     if r["status"] == "failed"]})
        elif args.cmd == "workflows":
            _print(svc.registry.list())
    except PlatformError as exc:
        _print(exc.to_dict())
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
