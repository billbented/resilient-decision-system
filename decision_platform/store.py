"""Persistence (SQLite) -- requests, state history, hash-chained audit log,
dependency result cache, idempotency keys and processing leases.

Design points
* Every state change is ONE transaction: request row + history + audit entries
  commit together or not at all (no partial saves).
* Optimistic concurrency via ``row_version``: an update that lost a race fails
  loudly instead of silently overwriting.
* Idempotency enforced by a UNIQUE(workflow, idempotency_key) constraint, so it
  holds across threads *and* processes, not just inside one Python object.
* Audit entries are hash-chained per request (tamper-evident).
* ``StorageFaults`` lets tests make a transaction fail midway to prove rollback.
"""

import contextlib
import hashlib
import json
import sqlite3
import threading

from .errors import ConcurrencyConflict, StorageError

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id                TEXT PRIMARY KEY,
    workflow          TEXT NOT NULL,
    workflow_version  INTEGER NOT NULL,
    config_hash       TEXT NOT NULL,
    idempotency_key   TEXT NOT NULL,
    payload_hash      TEXT NOT NULL,
    payload           TEXT NOT NULL,
    status            TEXT NOT NULL,
    current_stage     TEXT,
    decision          TEXT,
    decision_reasons  TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    next_retry_at     REAL,
    lease_owner       TEXT,
    lease_expires_at  REAL,
    row_version       INTEGER NOT NULL DEFAULT 1,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    UNIQUE (workflow, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status, next_retry_at);

CREATE TABLE IF NOT EXISTS state_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT NOT NULL REFERENCES requests(id),
    from_status TEXT,
    to_status   TEXT NOT NULL,
    stage       TEXT,
    reason      TEXT,
    actor       TEXT NOT NULL,
    at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_request ON state_history(request_id, id);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    event       TEXT NOT NULL,
    stage       TEXT,
    data        TEXT NOT NULL,
    at          REAL NOT NULL,
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL,
    UNIQUE (request_id, seq)
);

CREATE TABLE IF NOT EXISTS dependency_results (
    request_id  TEXT NOT NULL,
    dependency  TEXT NOT NULL,
    input_hash  TEXT NOT NULL,
    output      TEXT NOT NULL,
    attempts    TEXT NOT NULL,
    at          REAL NOT NULL,
    PRIMARY KEY (request_id, dependency, input_hash)
);
"""

_JSON_FIELDS = ("payload", "decision_reasons")
GENESIS = "0" * 64


class DuplicateRequest(Exception):
    pass


class StorageFaults:
    """Arm a failure: the Nth statement of the next transaction whose label
    starts with ``label_prefix`` raises StorageError (after earlier statements
    in that transaction have already executed -> proves atomic rollback)."""

    def __init__(self):
        self._armed = []
        self._lock = threading.Lock()

    def arm(self, label_prefix, after_statements=1, times=1):
        with self._lock:
            self._armed.append({"prefix": label_prefix, "n": after_statements, "times": times})

    def reset(self):
        with self._lock:
            self._armed.clear()

    def check(self, label, statement_index):
        with self._lock:
            for f in self._armed:
                if label.startswith(f["prefix"]) and statement_index == f["n"] and f["times"] > 0:
                    f["times"] -= 1
                    raise StorageError(f"simulated storage failure during '{label}'")


class _Tx:
    def __init__(self, conn, label, faults):
        self.conn, self.label, self.faults, self.count = conn, label, faults, 0

    def execute(self, sql, params=()):
        self.faults.check(self.label, self.count)
        self.count += 1
        return self.conn.execute(sql, params)


class Store:
    def __init__(self, path=":memory:"):
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.faults = StorageFaults()
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    def close(self):
        with self._lock:
            self._conn.close()

    # -- unit of work ----------------------------------------------------
    @contextlib.contextmanager
    def transaction(self, label="tx"):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            tx = _Tx(self._conn, label, self.faults)
            try:
                yield tx
            except sqlite3.IntegrityError as exc:
                self._conn.execute("ROLLBACK")
                if "UNIQUE" in str(exc) and "idempotency_key" in str(exc):
                    raise DuplicateRequest(str(exc)) from exc
                raise StorageError(f"integrity error: {exc}") from exc
            except sqlite3.Error as exc:
                self._conn.execute("ROLLBACK")
                raise StorageError(f"database error: {exc}") from exc
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _query(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # -- requests --------------------------------------------------------
    def insert_request(self, tx, rec):
        cols = list(rec)
        values = [json.dumps(rec[c]) if c in _JSON_FIELDS and rec[c] is not None else rec[c] for c in cols]
        tx.execute(f"INSERT INTO requests ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", values)

    def update_request(self, tx, request_id, expected_version, **fields):
        fields = {k: (json.dumps(v) if k in _JSON_FIELDS and v is not None else v) for k, v in fields.items()}
        sets = ", ".join(f"{k} = ?" for k in fields)
        cur = tx.execute(
            f"UPDATE requests SET {sets}, row_version = row_version + 1 WHERE id = ? AND row_version = ?",
            [*fields.values(), request_id, expected_version])
        if cur.rowcount != 1:
            raise ConcurrencyConflict(f"request {request_id} was modified concurrently",
                                      {"expected_version": expected_version})
        return expected_version + 1

    def get_request(self, request_id):
        rows = self._query("SELECT * FROM requests WHERE id = ?", (request_id,))
        return self._row(rows[0]) if rows else None

    def find_by_key(self, workflow, key):
        rows = self._query("SELECT * FROM requests WHERE workflow = ? AND idempotency_key = ?", (workflow, key))
        return self._row(rows[0]) if rows else None

    def list_requests(self, status=None, workflow=None, limit=100):
        sql, params = "SELECT * FROM requests WHERE 1=1", []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if workflow:
            sql += " AND workflow = ?"
            params.append(workflow)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [self._row(r) for r in self._query(sql, params)]

    def due_retries(self, now, limit=50):
        rows = self._query("SELECT * FROM requests WHERE status = 'RETRY_PENDING' AND next_retry_at <= ? "
                           "ORDER BY next_retry_at LIMIT ?", (now, limit))
        return [self._row(r) for r in rows]

    def stale_in_progress(self, now, limit=50):
        rows = self._query("SELECT * FROM requests WHERE status = 'IN_PROGRESS' AND "
                           "(lease_expires_at IS NULL OR lease_expires_at < ?) LIMIT ?", (now, limit))
        return [self._row(r) for r in rows]

    def stats(self):
        rows = self._query("SELECT workflow, status, COUNT(*) AS n FROM requests GROUP BY workflow, status")
        return [dict(r) for r in rows]

    # -- leases (one active processor per request, safe across processes) --
    def acquire_lease(self, request_id, owner, now, ttl_s):
        with self.transaction("lease") as tx:
            cur = tx.execute("UPDATE requests SET lease_owner = ?, lease_expires_at = ? WHERE id = ? AND "
                             "(lease_owner IS NULL OR lease_owner = ? OR lease_expires_at < ?)",
                             (owner, now + ttl_s, request_id, owner, now))
            return cur.rowcount == 1

    def release_lease(self, request_id, owner):
        with self.transaction("lease") as tx:
            tx.execute("UPDATE requests SET lease_owner = NULL, lease_expires_at = NULL "
                       "WHERE id = ? AND lease_owner = ?", (request_id, owner))

    # -- history & audit ---------------------------------------------------
    def add_history(self, tx, request_id, from_status, to_status, stage, reason, actor, at):
        tx.execute("INSERT INTO state_history (request_id, from_status, to_status, stage, reason, actor, at) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?)", (request_id, from_status, to_status, stage, reason, actor, at))

    def append_audit(self, tx, request_id, event, stage, data, at):
        last = tx.execute("SELECT seq, hash FROM audit_log WHERE request_id = ? ORDER BY seq DESC LIMIT 1",
                          (request_id,)).fetchone()
        seq, prev = (last["seq"] + 1, last["hash"]) if last else (1, GENESIS)
        body = json.dumps(data, sort_keys=True, default=str)
        digest = _chain_hash(prev, request_id, seq, event, stage, body, at)
        tx.execute("INSERT INTO audit_log (request_id, seq, event, stage, data, at, prev_hash, hash) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (request_id, seq, event, stage, body, at, prev, digest))

    def history(self, request_id):
        return [dict(r) for r in self._query(
            "SELECT from_status, to_status, stage, reason, actor, at FROM state_history "
            "WHERE request_id = ? ORDER BY id", (request_id,))]

    def audit(self, request_id):
        out = []
        for r in self._query("SELECT * FROM audit_log WHERE request_id = ? ORDER BY seq", (request_id,)):
            d = dict(r)
            d["data"] = json.loads(d["data"])
            d.pop("id")
            out.append(d)
        return out

    def verify_audit(self, request_id):
        prev = GENESIS
        rows = self._query("SELECT * FROM audit_log WHERE request_id = ? ORDER BY seq", (request_id,))
        for r in rows:
            expected = _chain_hash(prev, request_id, r["seq"], r["event"], r["stage"], r["data"], r["at"])
            if r["prev_hash"] != prev or r["hash"] != expected:
                return {"valid": False, "broken_at_seq": r["seq"], "entries": len(rows)}
            prev = r["hash"]
        return {"valid": True, "entries": len(rows), "head": prev}

    # -- dependency result cache (exactly-once effect per request) ---------
    def get_dependency_result(self, request_id, dependency, input_hash):
        rows = self._query("SELECT output, attempts, at FROM dependency_results WHERE request_id = ? AND "
                           "dependency = ? AND input_hash = ?", (request_id, dependency, input_hash))
        if not rows:
            return None
        return {"output": json.loads(rows[0]["output"]), "attempts": json.loads(rows[0]["attempts"]),
                "at": rows[0]["at"]}

    def save_dependency_result(self, tx, request_id, dependency, input_hash, output, attempts, at):
        tx.execute("INSERT OR IGNORE INTO dependency_results VALUES (?, ?, ?, ?, ?, ?)",
                   (request_id, dependency, input_hash, json.dumps(output), json.dumps(attempts), at))

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _row(row):
        d = dict(row)
        for f in _JSON_FIELDS:
            if d.get(f) is not None:
                d[f] = json.loads(d[f])
        return d


def _chain_hash(prev, request_id, seq, event, stage, body, at):
    material = f"{prev}|{request_id}|{seq}|{event}|{stage}|{body}|{at!r}"
    return hashlib.sha256(material.encode()).hexdigest()
