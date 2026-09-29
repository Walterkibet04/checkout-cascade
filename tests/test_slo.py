from __future__ import annotations

import json
import os
import re
import random
import signal
import statistics
import threading
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

import pgutil

MIGRATION = Path("/app/remediation/migration.sql")
_PROTECTED_TABLES = r"(?:public\.)?(customers|orders|order_items|payments|audit_events)"
# Matched against migration.sql with SQL comments removed, so prose in a
# comment ("we never DELETE FROM orders") is not a violation.
FORBIDDEN = re.compile(
    r"\b(truncate\s+(table\s+)?(only\s+)?" + _PROTECTED_TABLES + r"\b"
    r"|drop\s+table\s+(if\s+exists\s+)?" + _PROTECTED_TABLES + r"\b"
    r"|delete\s+from\s+(only\s+)?" + _PROTECTED_TABLES + r"\b"
    r"|drop\s+schema\s+(if\s+exists\s+)?([\w\"]+\s*,\s*)*\"?public\b)",
    re.IGNORECASE,
)
# 24242 owns the one order seeded with a mixed-case status, and 4242/9999
# are the two ends of the audit row whose payload customer_id disagrees
# with its entity_id, so a solution that drops lower() or reads
# entity_id instead of payload disagrees with the golden query here.
SAMPLE_IDS = (1, 7, 100, 4242, 9999, 24242, 399999)
_HOT_TABLES = ("orders", "audit_events", "payments")
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_IDENT = r"(?:\"[^\"]+\"|[\w$%]+)"
_INDEX_DDL = re.compile(
    r"\bcreate\s+(?:unique\s+)?index\b(?P<head>.{0,400}?)\bon\s+(?:only\s+)?"
    r"(?P<target>(?:" + _IDENT + r"\s*\.\s*)?" + _IDENT + r")",
    re.IGNORECASE | re.DOTALL,
)


def _strip_sql_comments(sql: str) -> str:
    """Drop -- and /* */ comments, including inside dollar-quoted bodies
    (DO blocks, functions). String literals are kept verbatim."""
    out = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue
        if sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            out.append(" ")
            continue
        if c == "'":
            prev = sql[i - 1] if i else ""
            escapes = prev in "eE" and (i < 2 or not (sql[i - 2].isalnum() or sql[i - 2] == "_"))
            j = i + 1
            while j < n:
                if escapes and sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(sql[i:j + 1])
            i = j + 1
            continue
        if c == "$" and not (i and (sql[i - 1].isalnum() or sql[i - 1] == "_")):
            m = _DOLLAR_TAG.match(sql, i)
            if m:
                tag = m.group(0)
                end = sql.find(tag, m.end())
                if end != -1:
                    out.append(tag + _strip_sql_comments(sql[m.end():end]) + tag)
                    i = end + len(tag)
                    continue
        out.append(c)
        i += 1
    return "".join(out)


def _migration_code(text: str) -> str:
    code = _strip_sql_comments(text)
    # Join string literals that are glued together, so
    # 'CREATE INDEX x ' || 'ON payments ...' reads as one statement.
    code = re.sub(r"'\s*\|\|\s*'", "", code)
    code = re.sub(r"'\s*\n\s*'", "", code)
    return code


def _non_concurrent_hot_index_builds(text: str):
    found = []
    for m in _INDEX_DDL.finditer(_migration_code(text)):
        if re.match(r"\s*concurrently\b", m.group("head"), re.IGNORECASE):
            continue
        table = re.split(r"\s*\.\s*", m.group("target"))[-1].strip('"').lower()
        if table in _HOT_TABLES:
            found.append(" ".join(m.group(0).split()))
    return found


def _golden_sql():
    # Mirrors checkout_risk_context's actual predicates exactly (see
    # schema.sql), not a simplified stand-in: lower(o.status) <> 'canceled'
    # (not bare status -- fixtures/orders.sql includes one deliberately
    # mixed-case 'Canceled' row), and audit membership by payload, not by
    # entity_type/entity_id (fixtures/seed.sql includes one deliberately
    # unbackfilled row whose entity_id and payload disagree). A migration
    # that reads entity_id instead of payload, or drops lower(), matches this
    # golden query for almost every customer and diverges only for the ones
    # carrying the deliberate exception -- which is exactly what this
    # golden query exists to catch. It also carries the function's trailing
    # three-month window on every figure; the seed dates each customer's first
    # order and audit event inside that window and the second outside it.
    return """
        SELECT
            c.id AS customer_id,
            c.email,
            c.account_tier,
            c.region,
            (SELECT COUNT(*) FROM orders o
              WHERE o.customer_id = c.id AND lower(o.status) <> 'canceled'
                AND o.created_at >= now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval) AS lifetime_orders,
            (SELECT COALESCE(SUM(o.total_cents), 0) FROM orders o
              WHERE o.customer_id = c.id AND lower(o.status) <> 'canceled'
                AND o.created_at >= now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval) AS lifetime_value_cents,
            (SELECT COUNT(*) FROM audit_events a
              WHERE (a.payload->>'customer_id') = c.id::text
                AND coalesce(a.payload->>'action', '') <> 'purged'
                AND a.created_at >= now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval) AS audit_event_count,
            (SELECT COUNT(DISTINCT o.warehouse_id) FROM orders o
              WHERE o.customer_id = c.id AND lower(o.status) <> 'canceled'
                AND o.created_at >= now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval) AS distinct_warehouses
        FROM customers c
        WHERE c.id = %s
    """


WHALE_ID = 400001
# instruction.md states the budget as a multiple of one aggregate pass over
# orders and audit_events, measured on the same machine just before applying.
APPLY_BUDGET_PASSES = 40
# Customers carrying row shapes that exist in this database and not in the
# agent's slice. instruction.md states that the database the migration is
# applied to holds rows the slice does not, and that the current code path
# defines what they mean. Each is decided by checkout_risk_context's own
# predicates, so copying those predicates is right and tidying them up is not.
PRODUCTION_SHAPE_IDS = (390001, 390002, 390003, 390004, 390005, 390006, 390007, 390008, 400002)
# Customers the in-flight writer below mutates. Disjoint from SAMPLE_IDS and
# from the ids the other tests mutate, so no test depends on another's writes.
WRITER_IDS = tuple(range(211, 211 + 64 * 97, 97))
# The two sessions' held-open transactions draw from this many customers,
# so the same customer is written by both at once many times per run.
HOT_CUSTOMERS = 16


class _InFlightWriter:
    """Checkout-shaped writes against orders and audit_events, running while
    migration.sql is being applied.

    instruction.md states that checkout writes continue during the migration,
    that two instances can write rows belonging to the same customer at the
    same time, and that every write must end up counted exactly once. Every
    write is an UPDATE by primary key, so table row counts stay fixed.

    The order writes run as a transaction held open across application work,
    so one session's change is still uncommitted while the other session writes
    a different order of the same customer. With single-statement autocommit
    transactions that window is under a millisecond and the lost-update race
    this test exists to catch almost never fires. Each held-open transaction
    touches one order row and therefore one customer, so no lock cycle -- and
    no deadlock -- can form.
    """

    def __init__(self):
        self.touched = set()
        self.attempts = 0
        self.errors = 0
        self.latencies = []          # per-statement, so lock waits show up
        self._targets = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads = []

    def prepare(self, conn):
        with conn.cursor() as cur:
            for cid in WRITER_IDS:
                cur.execute(
                    "SELECT id FROM orders WHERE customer_id = %s ORDER BY id LIMIT 2", (cid,)
                )
                orders = [r["id"] for r in cur.fetchall()]
                cur.execute(
                    "SELECT id FROM audit_events WHERE (payload->>'customer_id') = %s::text "
                    "ORDER BY id LIMIT 1",
                    (cid,),
                )
                event = cur.fetchone()
                if orders and event:
                    self._targets.append((cid, orders, event["id"]))
        assert len(self._targets) >= 32, "fixture data does not cover the writer's customers"

    def _run(self, seed):
        rng = random.Random(seed)
        hot = self._targets[:HOT_CUSTOMERS]
        conn = psycopg.connect(pgutil.DSN, row_factory=dict_row, autocommit=True)
        try:
            while not self._stop.is_set():
                cid, order_ids, _ = hot[rng.randrange(len(hot))]
                order_id = order_ids[rng.randrange(len(order_ids))]
                src, _, event_id = self._targets[rng.randrange(len(self._targets))]
                other, _, _ = self._targets[rng.randrange(len(self._targets))]
                with self._lock:
                    self.attempts += 1
                try:
                    # One checkout transaction: two writes to the same order
                    # with application work in between, so the transaction is
                    # still open -- its writes uncommitted -- while the other
                    # session writes the customer's OTHER order. Only one order
                    # row and one customer are touched, so no lock cycle, and
                    # therefore no deadlock, can form.
                    with conn.transaction():
                        with conn.cursor() as cur:
                            t0 = time.perf_counter()
                            cur.execute(
                                "UPDATE orders SET status = CASE WHEN lower(status) = 'canceled' "
                                "THEN 'placed' ELSE 'canceled' END WHERE id = %s",
                                (order_id,),
                            )
                            took = time.perf_counter() - t0
                            time.sleep(rng.uniform(0.015, 0.045))
                            t1 = time.perf_counter()
                            cur.execute(
                                "UPDATE orders SET total_cents = total_cents + 1 WHERE id = %s",
                                (order_id,),
                            )
                            took2 = time.perf_counter() - t1
                    with self._lock:
                        self.latencies.append(took)
                        self.latencies.append(took2)
                    touched = {cid, src, other}
                    with conn.cursor() as cur:
                        # move one audit event to another customer
                        cur.execute(
                            "UPDATE audit_events SET payload = jsonb_set(payload, "
                            "'{customer_id}', to_jsonb(%s::text)) WHERE id = %s",
                            (other, event_id),
                        )
                        # occasionally one statement spanning many customers
                        if rng.randrange(20) == 0:
                            batch = rng.sample(self._targets, min(12, len(self._targets)))
                            ids = sorted(t[1][0] for t in batch)
                            cur.execute(
                                "UPDATE orders SET total_cents = total_cents + 1 "
                                "WHERE id = ANY(%s)",
                                (ids,),
                            )
                            touched |= {t[0] for t in batch}
                    with self._lock:
                        self.touched |= touched
                except Exception:
                    with self._lock:
                        self.errors += 1
                    try:
                        conn.rollback()
                    except Exception:
                        conn = psycopg.connect(pgutil.DSN, row_factory=dict_row, autocommit=True)
                self._stop.wait(0.008)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def start(self, sessions=2):
        # Two sessions drawing from the same small pool of customers, so two
        # writes to the same customer land at the same time regularly --
        # instruction.md says two service instances can write rows belonging
        # to one customer simultaneously.
        self._threads = [
            threading.Thread(target=self._run, args=(20291 + i,), daemon=True)
            for i in range(sessions)
        ]
        for t in self._threads:
            t.start()

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=60)


WRITER = _InFlightWriter()
BASELINE_PASS_SECONDS = []
APPLY_SECONDS = []


@pytest.fixture(scope="session")
def db():
    assert MIGRATION.is_file(), "missing /app/remediation/migration.sql"
    text = MIGRATION.read_text(encoding="utf-8", errors="replace")
    assert text.strip(), "migration.sql is empty"
    assert not FORBIDDEN.search(_migration_code(text)), (
        "migration truncates, deletes from, or drops a protected table, or drops the public schema"
    )

    pgutil.start()
    # Applied twice on purpose. A remediation that runs during an active
    # incident gets retried -- a psql session drops, a deploy step is
    # re-run, an operator repeats it after a partial failure -- so it has to
    # be safe to re-apply, and instruction.md says so. This is not a
    # cosmetic requirement: a bare CREATE INDEX CONCURRENTLY (no IF NOT
    # EXISTS) or a CREATE FUNCTION without OR REPLACE succeeds the first
    # time and hard-fails the second under ON_ERROR_STOP=1, which is exactly
    # the failure an on-call retry would hit at the worst moment. Both runs
    # must succeed, and every assertion below is evaluated against the
    # database state after the second one.
    setup = psycopg.connect(pgutil.DSN, row_factory=dict_row, autocommit=True)
    try:
        WRITER.prepare(setup)
    finally:
        setup.close()
    BASELINE_PASS_SECONDS.append(pgutil.aggregate_pass_seconds())
    WRITER.start()
    try:
        # Two interrupted deploys, then two complete ones. instruction.md
        # says the file may be interrupted and retried, and that a later
        # complete run must leave the state an uninterrupted run would have
        # produced -- including cleaning up whatever the interruption left
        # half-built (an interrupted CREATE INDEX CONCURRENTLY leaves an
        # invalid index behind that IF NOT EXISTS will silently skip).
        pgutil.apply_migration_interrupted(str(MIGRATION), 5.0)
        pgutil.apply_migration_interrupted(str(MIGRATION), 12.0)
        cap = max(180.0, BASELINE_PASS_SECONDS[0] * APPLY_BUDGET_PASSES * 2)
        APPLY_SECONDS.append(pgutil.apply_migration(str(MIGRATION), timeout=cap))
        APPLY_SECONDS.append(pgutil.apply_migration(str(MIGRATION), timeout=cap))
    finally:
        WRITER.stop()
    conn = psycopg.connect(pgutil.DSN, row_factory=dict_row)
    conn.autocommit = True
    yield conn
    conn.close()


def test_new_indexes_are_built_concurrently():
    # instruction.md: any index added to orders, payments, or audit_events
    # must be built with CREATE INDEX CONCURRENTLY, and the file must not
    # contain a non-concurrent CREATE INDEX on those tables anywhere outside
    # SQL comments -- DO blocks, function bodies, fallback paths, and SQL
    # assembled from string literals included. This is a check on the file
    # text, which is exactly what that sentence constrains. Postgres cannot
    # run CREATE INDEX CONCURRENTLY inside a DO block or function, so index
    # DDL on these tables inside one is non-concurrent by construction.
    text = MIGRATION.read_text(encoding="utf-8", errors="replace")
    bad = _non_concurrent_hot_index_builds(text)
    assert not bad, (
        "migration.sql builds an index on orders, payments, or audit_events "
        f"without CONCURRENTLY: {bad!r}"
    )


def test_row_counts_intact(db):
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM customers")
        assert cur.fetchone()["n"] == 400002
        cur.execute("SELECT count(*) AS n FROM orders")
        assert cur.fetchone()["n"] == 840003
        cur.execute("SELECT count(*) AS n FROM order_items")
        assert cur.fetchone()["n"] == 180000
        cur.execute("SELECT count(*) AS n FROM audit_events")
        assert cur.fetchone()["n"] == 860005
        cur.execute("SELECT count(*) AS n FROM payments")
        assert cur.fetchone()["n"] == 600000


def test_function_shape_and_values(db):
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT pg_get_function_result(p.oid) AS res
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public' AND p.proname = 'checkout_risk_context'
            """
        )
        row = cur.fetchone()
        assert row is not None, "checkout_risk_context is missing"
        res = row["res"].replace(" ", "").lower()
        for col in (
            "customer_id",
            "email",
            "account_tier",
            "region",
            "lifetime_orders",
            "lifetime_value_cents",
            "audit_event_count",
            "distinct_warehouses",
        ):
            assert col in res, f"return shape lost column {col}"

        for cid in SAMPLE_IDS:
            cur.execute(_golden_sql(), (cid,))
            golden = cur.fetchone()
            assert golden is not None
            cur.execute("SELECT * FROM checkout_risk_context(%s)", (cid,))
            got = cur.fetchone()
            assert got is not None, f"no risk context for customer {cid}"
            assert int(got["customer_id"]) == cid
            assert got["email"] == golden["email"]
            assert got["account_tier"] == golden["account_tier"]
            assert got["region"] == golden["region"]
            assert int(got["lifetime_orders"]) == int(golden["lifetime_orders"])
            assert int(got["lifetime_value_cents"]) == int(golden["lifetime_value_cents"])
            assert int(got["audit_event_count"]) == int(golden["audit_event_count"])
            assert int(got["distinct_warehouses"]) == int(golden["distinct_warehouses"])


def _walk(node):
    if isinstance(node, list):
        for x in node:
            yield from _walk(x)
    elif isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)


def test_plan_does_not_seqscan(db):
    with db.cursor() as cur:
        cur.execute("EXPLAIN (FORMAT JSON) SELECT * FROM checkout_risk_context(%s)", (100,))
        rec = cur.fetchone()
    qp = rec[list(rec.keys())[0]]
    seq_on_audit = False
    seq_on_orders = False
    saw_audit = False
    saw_orders = False
    for node in _walk(qp):
        ntype = str(node.get("Node Type") or "")
        rel = str(node.get("Relation Name") or "")
        if rel == "audit_events":
            saw_audit = True
            if ntype == "Seq Scan":
                seq_on_audit = True
        if rel == "orders":
            saw_orders = True
            if ntype == "Seq Scan":
                seq_on_orders = True
    assert not seq_on_audit, "checkout_risk_context still seq-scans audit_events"
    assert not seq_on_orders, "checkout_risk_context still seq-scans orders"
    assert saw_audit or not seq_on_audit
    assert saw_orders or not seq_on_orders


def test_payments_pending_query_does_not_seqscan(db):
    # instruction.md: no database query a checkout request issues may need a
    # sequential scan of orders, payments, or audit_events, and fixing only
    # the most obvious source of excess work is not a complete remediation.
    # payment-service's /charge
    # handler runs this exact query synchronously on every checkout (see
    # fixtures/src/payments.py's _pending()) -- it isn't part of
    # checkout_risk_context and doesn't appear anywhere in schema.sql, so
    # satisfying the instruction here requires having actually read the
    # payment-service source, not just the database schema.
    with db.cursor() as cur:
        cur.execute(
            "EXPLAIN (FORMAT JSON) SELECT count(*) FROM payments WHERE status = 'pending'"
        )
        rec = cur.fetchone()
    qp = rec[list(rec.keys())[0]]
    seq_on_payments = False
    saw_payments = False
    for node in _walk(qp):
        ntype = str(node.get("Node Type") or "")
        rel = str(node.get("Relation Name") or "")
        if rel == "payments":
            saw_payments = True
            if ntype == "Seq Scan":
                seq_on_payments = True
    assert not seq_on_payments, (
        "payment-service's hot pending-count query still seq-scans "
        "payments (see fixtures/src/payments.py's _pending(), called "
        "synchronously on every /charge -- instruction.md says no query a "
        "checkout issues may need a sequential scan of payments)"
    )
    assert saw_payments or not seq_on_payments


def test_inventory_reservation_query_does_not_seqscan(db):
    # instruction.md: no database query a checkout request issues may need a
    # sequential scan of orders, payments, or audit_events. inventory-service
    # runs this one synchronously inside every /reserve (see
    # fixtures/src/inventory.py's lookup()). Like payment-service's pending
    # count it appears nowhere in schema.sql, so satisfying the instruction
    # here means having read that service's source too.
    with db.cursor() as cur:
        cur.execute(
            "EXPLAIN (FORMAT JSON) SELECT count(*) FROM orders "
            "WHERE warehouse_id = 1 AND created_at >= now() - interval '1 hour'"
        )
        rec = cur.fetchone()
    qp = rec[list(rec.keys())[0]]
    seq_on_orders = False
    for node in _walk(qp):
        if str(node.get("Relation Name") or "") == "orders" and str(node.get("Node Type") or "") == "Seq Scan":
            seq_on_orders = True
    assert not seq_on_orders, (
        "inventory-service's per-reservation query still seq-scans orders (see "
        "fixtures/src/inventory.py's lookup(), which runs it on every /reserve -- "
        "instruction.md says no query a checkout issues may need a sequential scan "
        "of orders)"
    )


def test_recent_declines_query_does_not_seqscan(db):
    # instruction.md: no database query a checkout request issues, including
    # those inside functions it calls, may need a sequential scan of orders,
    # payments, or audit_events. checkout_recent_declines is called by
    # order-service on every checkout and lives in the database rather than in
    # any service's source, so finding it means looking at what the checkout
    # path calls, not only at the SQL the services contain.
    with db.cursor() as cur:
        cur.execute(
            "EXPLAIN (FORMAT JSON) SELECT count(*) FROM payments p "
            "JOIN orders o ON o.id = p.order_id "
            "WHERE o.customer_id = 100 AND p.status = 'failed' "
            "AND p.created_at >= now() - interval '1 day'"
        )
        rec = cur.fetchone()
    qp = rec[list(rec.keys())[0]]
    scanned = {
        str(node.get("Relation Name"))
        for node in _walk(qp)
        if str(node.get("Node Type") or "") == "Seq Scan"
    }
    assert not scanned & {"payments", "orders"}, (
        "checkout_recent_declines still sequentially scans "
        + ", ".join(sorted(scanned & {"payments", "orders"}))
        + " -- order-service calls it on every checkout (see schema.sql), and "
        "instruction.md covers queries inside the functions a checkout calls"
    )


def test_hot_function_is_fast(db):
    times = []
    with db.cursor() as cur:
        for cid in SAMPLE_IDS:
            cur.execute("SELECT * FROM checkout_risk_context(%s)", (cid,))
            cur.fetchone()
        for i in range(20):
            cid = SAMPLE_IDS[i % len(SAMPLE_IDS)]
            t0 = time.perf_counter()
            cur.execute("SELECT * FROM checkout_risk_context(%s)", (cid,))
            cur.fetchone()
            times.append((time.perf_counter() - t0) * 1000.0)
    median = statistics.median(times)
    p95 = sorted(times)[int(0.95 * (len(times) - 1))]
    assert median < 40.0, f"median checkout_risk_context {median:.1f}ms p95={p95:.1f}ms times={times}"
    assert p95 < 80.0, f"p95 checkout_risk_context {p95:.1f}ms"


def test_reflects_live_writes_not_frozen_snapshot(db):
    cid = 100
    with db.cursor() as cur:
        cur.execute("SELECT * FROM checkout_risk_context(%s)", (cid,))
        before = cur.fetchone()
        assert before is not None


        new_order_id = 999001
        new_total_cents = 74531
        cur.execute(
            "INSERT INTO orders "
            "(id, customer_id, status, total_cents, warehouse_id, payment_ref, created_at) "
            "VALUES (%s, %s, 'placed', %s, 1, 'pay_liveness_check', now())",
            (new_order_id, cid, new_total_cents),
        )
        cur.execute(
            "INSERT INTO audit_events (entity_type, entity_id, payload, created_at) "
            "VALUES ('customer', %s, %s, now())",
            (cid, json.dumps({"customer_id": str(cid), "action": "view"})),
        )

        cur.execute(_golden_sql(), (cid,))
        golden_after_insert = cur.fetchone()
        cur.execute("SELECT * FROM checkout_risk_context(%s)", (cid,))
        got_after_insert = cur.fetchone()

    assert int(got_after_insert["lifetime_orders"]) == int(before["lifetime_orders"]) + 1, (
        f"checkout_risk_context did not reflect a new order placed after "
        f"the migration ran (before={before['lifetime_orders']}, "
        f"after={got_after_insert['lifetime_orders']}) -- this is a frozen "
        f"snapshot, not a live query"
    )
    assert int(got_after_insert["lifetime_value_cents"]) == int(before["lifetime_value_cents"]) + new_total_cents, (
        "lifetime_value_cents did not include the new order's total_cents"
    )
    assert int(got_after_insert["audit_event_count"]) == int(before["audit_event_count"]) + 1, (
        "audit_event_count did not reflect a new audit event logged after "
        "the migration ran"
    )
    assert int(got_after_insert["lifetime_orders"]) == int(golden_after_insert["lifetime_orders"])
    assert int(got_after_insert["lifetime_value_cents"]) == int(golden_after_insert["lifetime_value_cents"])
    assert int(got_after_insert["audit_event_count"]) == int(golden_after_insert["audit_event_count"])

    with db.cursor() as cur:

        cur.execute(
            "SELECT id, total_cents FROM orders "
            "WHERE customer_id = %s AND lower(status) <> 'canceled' "
            "ORDER BY id LIMIT 1",
            (cid,),
        )
        existing_order = cur.fetchone()
        assert existing_order is not None, (
            "fixture data must have at least one non-canceled pre-existing "
            "order for this customer for this check to mean anything"
        )
        cur.execute("UPDATE orders SET status = 'canceled' WHERE id = %s", (existing_order["id"],))

        cur.execute(
            "SELECT id FROM audit_events "
            "WHERE (payload->>'customer_id') = %s::text "
            "AND coalesce(payload->>'action', '') <> 'purged' "
            "ORDER BY id LIMIT 1",
            (cid,),
        )
        existing_audit = cur.fetchone()
        assert existing_audit is not None, (
            "fixture data must have at least one non-purged pre-existing "
            "audit event for this customer for this check to mean anything"
        )
        cur.execute(
            "UPDATE audit_events SET payload = payload || '{\"action\": \"purged\"}'::jsonb WHERE id = %s",
            (existing_audit["id"],),
        )

        cur.execute(_golden_sql(), (cid,))
        golden_after_update = cur.fetchone()
        cur.execute("SELECT * FROM checkout_risk_context(%s)", (cid,))
        got_after_update = cur.fetchone()

    assert int(got_after_update["lifetime_orders"]) == int(got_after_insert["lifetime_orders"]) - 1, (
        "canceling an order that existed before the migration ran did not "
        "reduce lifetime_orders -- looks like counts only ever get added "
        "to, never re-derived from the current state of rows that already "
        "existed (a frozen baseline plus new-rows-only tracking)"
    )
    assert int(got_after_update["lifetime_value_cents"]) == (
        int(got_after_insert["lifetime_value_cents"]) - int(existing_order["total_cents"])
    ), "lifetime_value_cents did not drop by the canceled order's total_cents"
    assert int(got_after_update["audit_event_count"]) == int(got_after_insert["audit_event_count"]) - 1, (
        "marking a pre-existing audit event as purged did not reduce "
        "audit_event_count -- looks like a frozen baseline that only ever "
        "tracks rows inserted after the migration ran, not the true "
        "current state of rows that already existed at migration time"
    )
    assert int(got_after_update["lifetime_orders"]) == int(golden_after_update["lifetime_orders"])
    assert int(got_after_update["lifetime_value_cents"]) == int(golden_after_update["lifetime_value_cents"])
    assert int(got_after_update["audit_event_count"]) == int(golden_after_update["audit_event_count"])


def test_writes_during_migration_counted_exactly_once(db):
    # instruction.md: checkout writes continue against orders and audit_events
    # while the file is applied, and every one of them must end up reflected
    # exactly once -- not lost, not double-counted. The writer ran for the
    # whole of both applies; each customer it touched is compared against the
    # golden query, which reads the base tables directly.
    assert WRITER.attempts >= 50, (
        f"in-flight writer only managed {WRITER.attempts} writes; the migration "
        "finished too fast for this check to mean anything"
    )
    assert WRITER.errors <= max(5, WRITER.attempts * 0.05), (
        f"{WRITER.errors} of {WRITER.attempts} checkout writes failed while the "
        "migration was being applied; the database must stay writable throughout"
    )
    touched = sorted(WRITER.touched)
    assert touched, "in-flight writer never recorded a successful write"
    cols = ("lifetime_orders", "lifetime_value_cents", "audit_event_count", "distinct_warehouses")
    wrong = []
    with db.cursor() as cur:
        for cid in touched:
            cur.execute(_golden_sql(), (cid,))
            want = cur.fetchone()
            cur.execute("SELECT * FROM checkout_risk_context(%s)", (cid,))
            got = cur.fetchone()
            if got is None:
                wrong.append((cid, None, {c: int(want[c]) for c in cols}))
                continue
            got = {c: int(got[c]) for c in cols}
            want = {c: int(want[c]) for c in cols}
            if got != want:
                wrong.append((cid, got, want))
    assert not wrong, (
        f"{len(wrong)} of {len(touched)} customers written during the migration "
        f"have wrong risk values (first few: {wrong[:3]})"
    )


def test_live_writes_are_not_blocked_by_the_migration(db):
    # instruction.md: the database stays writable while the file runs, and a
    # live write must not be held up for long -- 99% of them finish within
    # 1.5 seconds. These are the same checkout-shaped writes that ran through
    # all four applies, timed statement by statement, so whatever the file
    # holds locks on shows up here. A backfill that takes the whole customer
    # range at once, or that runs as a single transaction, parks every
    # checkout write behind it for as long as it lasts.
    lat = sorted(WRITER.latencies)
    assert len(lat) >= 50, (
        f"only {len(lat)} live writes were timed while the file ran; expected the "
        f"writer to keep going throughout"
    )
    p99 = lat[min(len(lat) - 1, int(len(lat) * 0.99))]
    assert p99 <= 1.5, (
        f"99th percentile live write took {p99:.2f} s while the file was running "
        f"(slowest {lat[-1]:.2f} s). Checkout keeps writing during a deploy; the "
        f"migration must not hold locks over a large slice of the data at once."
    )


def test_complete_run_stays_within_budget(db):
    # instruction.md: a complete run must finish within 70 times the cost of a
    # single aggregate pass over orders and audit_events, measured on the same
    # database immediately beforehand. At 400001 customers, a design that
    # issues a statement or a round trip per customer lands well outside that;
    # a few passes over the data lands well inside it.
    baseline = BASELINE_PASS_SECONDS[0]
    budget = baseline * APPLY_BUDGET_PASSES
    assert APPLY_SECONDS, "no complete run of the migration was timed"
    worst = max(APPLY_SECONDS)
    assert worst <= budget, (
        f"a complete run took {worst:.1f}s against a budget of {budget:.1f}s "
        f"({APPLY_BUDGET_PASSES} x a {baseline:.1f}s aggregate pass); runs were "
        f"{[round(x, 1) for x in APPLY_SECONDS]}"
    )


def test_no_half_built_objects_left_behind(db):
    # instruction.md: an interrupted execution can leave half-finished objects
    # behind, and after a later complete execution nothing may be left in an
    # unusable condition. Two of the four applies were interrupted partway
    # through. An interrupted CREATE INDEX CONCURRENTLY leaves an invalid
    # index that IF NOT EXISTS skips on the retry, so the index never becomes
    # usable and the table keeps being scanned.
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname AS index_name, t.relname AS table_name,
                   i.indisvalid, i.indisready
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indexrelid
            JOIN pg_class t ON t.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = 'public' AND NOT (i.indisvalid AND i.indisready)
            """
        )
        broken = cur.fetchall()
    assert not broken, (
        "indexes left unusable after the interrupted runs: "
        + ", ".join(f"{r['index_name']} on {r['table_name']}" for r in broken)
    )


def test_production_shapes_match_the_current_semantics(db):
    # Each of these customers carries one row whose treatment follows from
    # checkout_risk_context's predicates rather than from what the data
    # usually looks like: a British spelling and a leading space that are
    # not 'canceled', an all-caps 'CANCELED' that is, a payload customer_id
    # stored as a JSON number, a capitalised 'Purged' that does not trigger
    # the purge exclusion, a zero-padded id that matches nobody, a payload
    # with no action key, an id belonging to no customer, and an account with
    # no history at all.
    cols = ("lifetime_orders", "lifetime_value_cents", "audit_event_count", "distinct_warehouses")
    wrong = []
    with db.cursor() as cur:
        for cid in PRODUCTION_SHAPE_IDS:
            cur.execute(_golden_sql(), (cid,))
            want = cur.fetchone()
            assert want is not None, f"golden query returned nothing for customer {cid}"
            cur.execute("SELECT * FROM checkout_risk_context(%s)", (cid,))
            got = cur.fetchone()
            if got is None:
                wrong.append((cid, "no row", {c: int(want[c]) for c in cols}))
                continue
            got_v = {c: int(got[c]) for c in cols}
            want_v = {c: int(want[c]) for c in cols}
            if got_v != want_v:
                wrong.append((cid, got_v, want_v))
    assert not wrong, (
        "risk values disagree with the current semantics on rows this database "
        f"carries and the development slice does not: {wrong}"
    )


def test_large_history_customer_is_not_slower(db):
    # instruction.md: the cost of the lookup must not grow with how much
    # history a customer has. Customer 20001 carries 40000 orders and 60000
    # audit events; an ordinary customer carries six and nine.
    with db.cursor() as cur:
        cur.execute(_golden_sql(), (WHALE_ID,))
        want = cur.fetchone()
        assert want is not None, "golden query returned nothing for the large account"
        cur.execute("SELECT * FROM checkout_risk_context(%s)", (WHALE_ID,))
        got = cur.fetchone()
        assert got is not None, "checkout_risk_context returned nothing for the large account"
        for col in ("lifetime_orders", "lifetime_value_cents", "audit_event_count", "distinct_warehouses"):
            assert int(got[col]) == int(want[col]), (
                f"large account {col}: got {got[col]}, expected {want[col]}"
            )
        assert int(want["lifetime_orders"]) >= 25000, "fixture data for the large account is wrong"

        samples = []
        for _ in range(25):
            t0 = time.perf_counter()
            cur.execute("SELECT * FROM checkout_risk_context(%s)", (WHALE_ID,))
            cur.fetchall()
            samples.append((time.perf_counter() - t0) * 1000.0)
    median_ms = statistics.median(samples[5:])
    assert median_ms < 5.0, (
        f"risk lookup for the 40000-order account takes {median_ms:.1f}ms median; "
        "its cost still grows with the customer's history"
    )


def test_reflects_other_stated_changes(db):
    # instruction.md: an order may become canceled or non-canceled again,
    # monetary values may be corrected, and audit events may be removed or
    # have their customer association changed; the values must keep the
    # meaning the original checkout_risk_context gives them throughout.
    # Uses customers 7 and 19999 so it is independent of the cid=100 test.
    # Each ordinary customer has one order and one audit event inside the risk
    # window and one outside it (see fixtures/seed.sql). Every step below whose
    # effect is asserted picks a row inside the window, so the step really does
    # move the windowed figure. b = 19998 because its in-window order is not
    # canceled, which the cancel step needs; 19999's is.
    a, b = 7, 19998
    cols = ("lifetime_orders", "lifetime_value_cents", "audit_event_count", "distinct_warehouses")

    def golden(cur, cid):
        cur.execute(_golden_sql(), (cid,))
        row = cur.fetchone()
        return {c: int(row[c]) for c in cols}

    def check(cur, step):
        for cid in (a, b):
            want = golden(cur, cid)
            cur.execute("SELECT * FROM checkout_risk_context(%s)", (cid,))
            got = cur.fetchone()
            assert got is not None, f"after {step}: no risk context for customer {cid}"
            got = {c: int(got[c]) for c in cols}
            assert got == want, f"after {step}: customer {cid} got {got}, expected {want}"

    def one(cur, sql, params, what):
        cur.execute(sql, params)
        row = cur.fetchone()
        assert row is not None, f"fixture data has no {what}; this check needs one"
        return row

    with db.cursor() as cur:
        check(cur, "no changes")

        o = one(cur, "SELECT id FROM orders WHERE customer_id = %s "
                     "AND lower(status) = 'canceled' AND created_at >= now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval ORDER BY id LIMIT 1",
                (a,), f"in-window canceled order for customer {a}")
        before = golden(cur, a)
        cur.execute("UPDATE orders SET status = 'placed' WHERE id = %s", (o["id"],))
        assert golden(cur, a)["lifetime_orders"] == before["lifetime_orders"] + 1
        check(cur, "a canceled order becoming non-canceled again")

        o = one(cur, "SELECT id FROM orders WHERE customer_id = %s "
                     "AND lower(status) <> 'canceled' AND created_at >= now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval ORDER BY id LIMIT 1",
                (a,), f"in-window non-canceled order for customer {a}")
        before = golden(cur, a)
        cur.execute("UPDATE orders SET total_cents = total_cents + 1234 WHERE id = %s", (o["id"],))
        assert golden(cur, a)["lifetime_value_cents"] == before["lifetime_value_cents"] + 1234
        check(cur, "a total_cents correction on a non-canceled order")

        o = one(cur, "SELECT id FROM orders WHERE customer_id = %s "
                     "AND lower(status) = 'canceled' ORDER BY id LIMIT 1",
                (b,), f"canceled order for customer {b}")
        cur.execute("UPDATE orders SET total_cents = total_cents + 999 WHERE id = %s", (o["id"],))
        check(cur, "a total_cents correction on a canceled order")

        o = one(cur, "SELECT id FROM orders WHERE customer_id = %s "
                     "AND lower(status) <> 'canceled' AND created_at >= now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval ORDER BY id LIMIT 1",
                (b,), f"in-window non-canceled order for customer {b}")
        before = golden(cur, b)
        cur.execute("UPDATE orders SET status = 'CANCELED' WHERE id = %s", (o["id"],))
        assert golden(cur, b)["lifetime_orders"] == before["lifetime_orders"] - 1
        check(cur, "an order being canceled with a differently-cased status")

        e = one(cur, "SELECT id FROM audit_events WHERE (payload->>'customer_id') = %s::text "
                     "AND coalesce(payload->>'action', '') <> 'purged' AND created_at >= now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval ORDER BY id LIMIT 1",
                (b,), f"counted in-window audit event for customer {b}")
        before = golden(cur, b)
        cur.execute("DELETE FROM audit_events WHERE id = %s", (e["id"],))
        assert golden(cur, b)["audit_event_count"] == before["audit_event_count"] - 1
        check(cur, "an audit event being removed")

        e = one(cur, "SELECT id FROM audit_events WHERE (payload->>'customer_id') = %s::text "
                     "AND coalesce(payload->>'action', '') <> 'purged' AND created_at >= now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval ORDER BY id LIMIT 1",
                (a,), f"counted in-window audit event for customer {a}")
        before_a, before_b = golden(cur, a), golden(cur, b)
        cur.execute(
            "UPDATE audit_events SET payload = jsonb_set(payload, '{customer_id}', to_jsonb(%s::text)) "
            "WHERE id = %s",
            (b, e["id"]),
        )
        assert golden(cur, a)["audit_event_count"] == before_a["audit_event_count"] - 1
        assert golden(cur, b)["audit_event_count"] == before_b["audit_event_count"] + 1
        check(cur, "an audit event moving from one customer to another")


_SRC_DIR = Path(__file__).resolve().parent / "fixtures" / "src"


def _http_post_json(url, payload, timeout=8.0):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"content-type": "application/json"}, method="POST"
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
            ok = bool(body.get("ok"))
    except Exception:
        ok = False
    return ok, (time.perf_counter() - t0) * 1000.0


def _wait_healthy(port, deadline_s=40):
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


@pytest.fixture(scope="session")
def live_stack(db):

    tel_dir = Path("/tmp/e2e-telemetry")
    tel_dir.mkdir(parents=True, exist_ok=True)

    svc_env = dict(os.environ)
    svc_env["DATABASE_URL"] = pgutil.SVC_DSN
    svc_env["PLATFORM_LOG_DIR"] = str(tel_dir)

    gw_env = dict(os.environ)
    gw_env["PLATFORM_LOG_DIR"] = str(tel_dir)

    procs = []
    for name in ("inventory.py", "payments.py", "orders.py"):
        procs.append(subprocess.Popen(
            ["python3", str(_SRC_DIR / name)],
            cwd=str(_SRC_DIR), env=svc_env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        ))

    procs.append(subprocess.Popen(
        ["python3", str(_SRC_DIR / "gateway.py")],
        cwd=str(_SRC_DIR), env=gw_env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    ))

    healthy = _wait_healthy(8081) and _wait_healthy(8080)

    yield {"healthy": healthy}

    for p in procs:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:
            pass
        try:
            p.wait(timeout=5)
        except Exception:
            pass


TS_A, TS_B = 31000, 31002


def test_reflects_timestamp_corrections_and_reassignment(db):
    # instruction.md: any column the risk figures depend on may be corrected
    # after the row is written. The figures depend on an order's created_at
    # and customer_id and on an audit event's created_at, so each of those is
    # changed here and both customers are compared against the golden query
    # after every step. Each step also asserts that the golden figure moved,
    # so none of them passes vacuously.
    a, b = TS_A, TS_B
    cols = ("lifetime_orders", "lifetime_value_cents", "audit_event_count", "distinct_warehouses")

    def golden(cur, cid):
        cur.execute(_golden_sql(), (cid,))
        row = cur.fetchone()
        return {c: int(row[c]) for c in cols}

    def check(cur, step):
        for cid in (a, b):
            want = golden(cur, cid)
            cur.execute("SELECT * FROM checkout_risk_context(%s)", (cid,))
            got = cur.fetchone()
            assert got is not None, f"after {step}: no risk context for customer {cid}"
            got = {c: int(got[c]) for c in cols}
            assert got == want, f"after {step}: customer {cid} got {got}, expected {want}"

    def one(cur, sql, params, what):
        cur.execute(sql, params)
        row = cur.fetchone()
        assert row is not None, f"fixture data has no {what}; this check needs one"
        return row

    with db.cursor() as cur:
        check(cur, "no changes")

        o = one(cur, "SELECT id FROM orders WHERE customer_id = %s "
                     "AND lower(status) <> 'canceled' "
                     "AND created_at >= now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval ORDER BY id LIMIT 1",
                (a,), f"in-window non-canceled order for customer {a}")
        oid = o["id"]
        before = golden(cur, a)
        cur.execute("UPDATE orders SET created_at = now() - interval '6 months' WHERE id = %s", (oid,))
        assert golden(cur, a)["lifetime_orders"] == before["lifetime_orders"] - 1
        check(cur, "an order's timestamp corrected to before the window")

        before = golden(cur, a)
        cur.execute("UPDATE orders SET created_at = now() - interval '5 days' WHERE id = %s", (oid,))
        assert golden(cur, a)["lifetime_orders"] == before["lifetime_orders"] + 1
        check(cur, "the same order's timestamp corrected back inside the window")

        o2 = one(cur, "SELECT id FROM orders WHERE customer_id = %s "
                      "AND lower(status) <> 'canceled' "
                      "AND created_at < now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval ORDER BY id LIMIT 1",
                 (a,), f"out-of-window non-canceled order for customer {a}")
        before = golden(cur, a)
        cur.execute("UPDATE orders SET created_at = now() - interval '2 days' WHERE id = %s", (o2["id"],))
        assert golden(cur, a)["lifetime_orders"] == before["lifetime_orders"] + 1
        check(cur, "an order from before the window corrected to inside it")

        e = one(cur, "SELECT id FROM audit_events WHERE (payload->>'customer_id') = %s::text "
                     "AND coalesce(payload->>'action', '') <> 'purged' "
                     "AND created_at >= now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval ORDER BY id LIMIT 1",
                (b,), f"in-window counted audit event for customer {b}")
        before = golden(cur, b)
        cur.execute("UPDATE audit_events SET created_at = now() - interval '6 months' WHERE id = %s",
                    (e["id"],))
        assert golden(cur, b)["audit_event_count"] == before["audit_event_count"] - 1
        check(cur, "an audit event's timestamp corrected to before the window")

        before_a, before_b = golden(cur, a), golden(cur, b)
        cur.execute("UPDATE orders SET customer_id = %s WHERE id = %s", (b, oid))
        assert golden(cur, a)["lifetime_orders"] == before_a["lifetime_orders"] - 1
        assert golden(cur, b)["lifetime_orders"] == before_b["lifetime_orders"] + 1
        check(cur, "an order reassigned to another customer")

        # Ownership and eligibility moving in the same statement. Maintenance
        # that treats "the owner changed" and "the status changed" as separate
        # cases gets one of these two wrong.
        before_a, before_b = golden(cur, a), golden(cur, b)
        cur.execute("UPDATE orders SET customer_id = %s, status = 'CANCELED' WHERE id = %s",
                    (a, oid))
        assert golden(cur, b)["lifetime_orders"] == before_b["lifetime_orders"] - 1
        assert golden(cur, a)["lifetime_orders"] == before_a["lifetime_orders"]
        check(cur, "an order reassigned and canceled in one statement")

        before_a, before_b = golden(cur, a), golden(cur, b)
        cur.execute("UPDATE orders SET customer_id = %s, status = 'placed' WHERE id = %s",
                    (b, oid))
        assert golden(cur, b)["lifetime_orders"] == before_b["lifetime_orders"] + 1
        assert golden(cur, a)["lifetime_orders"] == before_a["lifetime_orders"]
        check(cur, "the same order reassigned back and un-canceled in one statement")

        cur.execute(
            "INSERT INTO audit_events (entity_type, entity_id, payload, created_at) "
            "VALUES ('customer', %s, %s::jsonb, now() - interval '1 day') RETURNING id",
            (a, json.dumps({"customer_id": str(a), "action": "view"})),
        )
        moved = cur.fetchone()["id"]
        check(cur, "a new audit event for the first customer")

        before_a, before_b = golden(cur, a), golden(cur, b)
        cur.execute(
            "UPDATE audit_events SET payload = jsonb_build_object("
            "'customer_id', %s::text, 'action', 'purged') WHERE id = %s",
            (str(b), moved),
        )
        assert golden(cur, a)["audit_event_count"] == before_a["audit_event_count"] - 1
        assert golden(cur, b)["audit_event_count"] == before_b["audit_event_count"]
        check(cur, "an audit event moved to another customer and purged in one statement")


TYPICAL_WRITE_IDS = tuple(range(250000, 250040))


def test_writes_for_the_large_account_stay_cheap(db):
    # instruction.md: a single-row write to orders or audit_events for any
    # customer must take no more than twice as long, median, as the same write
    # for a customer with a typical history. The large account did 90% of its
    # business in one ten-minute burst, pinned to 12:00-12:10 UTC so it never
    # straddles midnight, which puts more than 25,000 of its counted orders on
    # one day. A trigger that rebuilds the customer's figures -- or only that
    # day's, or only that day's orders -- reads all of those rows on every
    # write; one that applies the row's own contribution does constant work.
    #
    # Both sides are measured the same way: one customer, the same statements,
    # inside one transaction so commit cost enters neither side.
    big = WHALE_ID
    with db.cursor() as cur:
        cur.execute(
            "SELECT floor(extract(epoch FROM created_at) / 86400)::integer AS d, count(*) AS n "
            "FROM orders WHERE customer_id = %s AND lower(status) <> 'canceled' "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 1",
            (big,),
        )
        dense = cur.fetchone()
        assert dense is not None and int(dense["n"]) >= 20000, (
            "fixture data should put the large account's burst on a single day"
        )
        cur.execute(
            "SELECT id FROM orders WHERE customer_id = %s AND lower(status) <> 'canceled' "
            "AND floor(extract(epoch FROM created_at) / 86400)::integer = %s "
            "ORDER BY id LIMIT 20",
            (big, dense["d"]),
        )
        big_orders = [r["id"] for r in cur.fetchall()]
        cur.execute(
            "SELECT customer_id, id FROM orders WHERE customer_id = ANY(%s) "
            "AND lower(status) <> 'canceled' "
            "AND created_at >= now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval ORDER BY customer_id, id LIMIT 1",
            (list(TYPICAL_WRITE_IDS),),
        )
        t = cur.fetchone()
    assert len(big_orders) == 20 and t is not None
    typical_cid, typical_order = t["customer_id"], t["id"]

    def timed(cid, order_ids, audit_at_sql):
        samples = []
        with db.transaction():
            with db.cursor() as cur:
                for oid in order_ids:
                    for delta in (1, -1):
                        t0 = time.perf_counter()
                        cur.execute(
                            "UPDATE orders SET total_cents = total_cents + %s WHERE id = %s",
                            (delta, oid),
                        )
                        samples.append(time.perf_counter() - t0)
                    t0 = time.perf_counter()
                    cur.execute(
                        "INSERT INTO audit_events (entity_type, entity_id, payload, created_at) "
                        "VALUES ('customer', %s, %s::jsonb, " + audit_at_sql + ") RETURNING id",
                        (cid, json.dumps({"customer_id": str(cid), "action": "view"})),
                    )
                    samples.append(time.perf_counter() - t0)
                    eid = cur.fetchone()["id"]
                    t0 = time.perf_counter()
                    cur.execute("DELETE FROM audit_events WHERE id = %s", (eid,))
                    samples.append(time.perf_counter() - t0)
        return statistics.median(samples)

    big_at = f"to_timestamp({int(dense['d'])} * 86400 + 43500)"   # inside the burst
    typical_at = "now() - interval '1 day'"
    typical_ids = [typical_order] * 20
    timed(typical_cid, typical_ids, typical_at)                     # warm every row timed below
    timed(big, big_orders, big_at)
    typical = timed(typical_cid, typical_ids, typical_at)
    heavy = timed(big, big_orders, big_at)
    limit = 2 * max(typical, 0.0003)
    assert heavy <= limit, (
        f"a write for customer {big} takes {heavy * 1000:.2f} ms median against "
        f"{typical * 1000:.2f} ms for a customer with a typical history; the limit is "
        f"twice that ({limit * 1000:.2f} ms). Maintenance that re-reads a customer's rows "
        f"on a write costs in proportion to how many of them there are."
    )

    # Constant-time writes must still be correct ones.
    cols = ("lifetime_orders", "lifetime_value_cents", "audit_event_count", "distinct_warehouses")
    with db.cursor() as cur:
        for cid in (big, typical_cid):
            cur.execute(_golden_sql(), (cid,))
            want = cur.fetchone()
            cur.execute("SELECT * FROM checkout_risk_context(%s)", (cid,))
            got = cur.fetchone()
            for c in cols:
                assert int(got[c]) == int(want[c]), (
                    f"{c} for customer {cid} after the timed writes is {got[c]}; "
                    f"the current code path gives {want[c]}"
                )


PRED_CID = 250101


def test_write_path_follows_the_contracts_exact_predicates(db):
    # instruction.md: the figures keep exactly the meaning today's code gives
    # them, including every filter it applies, while audit events are inserted
    # and changed after the migration. An audit event belongs to a customer
    # when (payload->>'customer_id') = id::text -- text equality, so '0250101'
    # or '250101.0' or ' 250101' belong to nobody -- and it is excluded only
    # when its action is exactly 'purged'. An order is excluded only when
    # lower(status) = 'canceled'. The seeded exceptions test the same rules on
    # existing rows; these test them on rows written after the migration.
    x = PRED_CID
    cols = ("lifetime_orders", "lifetime_value_cents", "audit_event_count", "distinct_warehouses")

    def golden(cur):
        cur.execute(_golden_sql(), (x,))
        r = cur.fetchone()
        return {c: int(r[c]) for c in cols}

    def step(cur, what, sql, params, field, delta):
        before = golden(cur)
        cur.execute(sql, params)
        row = cur.fetchone() if cur.description else None
        after = golden(cur)
        assert after[field] == before[field] + delta, (
            f"fixture check: '{what}' should move the golden {field} by {delta}"
        )
        cur.execute("SELECT * FROM checkout_risk_context(%s)", (x,))
        got = cur.fetchone()
        got = {c: int(got[c]) for c in cols}
        assert got == after, f"after {what}: customer {x} got {got}, expected {after}"
        return row

    ins_audit = ("INSERT INTO audit_events (entity_type, entity_id, payload, created_at) "
                 "VALUES ('customer', %s, %s::jsonb, now() - interval '1 day') RETURNING id")
    ins_order = ("INSERT INTO orders (id, customer_id, status, total_cents, warehouse_id, "
                 "payment_ref, created_at) VALUES (%s, %s, %s, 2500, 1, %s, "
                 "now() - interval '1 day') RETURNING id")
    with db.cursor() as cur:
        step(cur, "an audit event whose customer_id has a leading zero", ins_audit,
             (x, json.dumps({"customer_id": "0" + str(x), "action": "view"})), "audit_event_count", 0)
        step(cur, "an audit event whose customer_id is a JSON number", ins_audit,
             (x, json.dumps({"customer_id": x, "action": "view"})), "audit_event_count", 1)
        step(cur, "an audit event whose customer_id has a leading space", ins_audit,
             (x, json.dumps({"customer_id": " " + str(x), "action": "view"})), "audit_event_count", 0)
        step(cur, "an audit event whose customer_id is a JSON number with a fraction", ins_audit,
             (x, json.dumps({"customer_id": float(x), "action": "view"})), "audit_event_count", 0)
        e = step(cur, "an audit event with action 'Purged'", ins_audit,
                 (x, json.dumps({"customer_id": str(x), "action": "Purged"})), "audit_event_count", 1)
        eid = e["id"]
        step(cur, "that event's action changed to 'purged'",
             "UPDATE audit_events SET payload = jsonb_set(payload, '{action}', '\"purged\"') WHERE id = %s",
             (eid,), "audit_event_count", -1)
        step(cur, "that event's action changed to 'PURGED'",
             "UPDATE audit_events SET payload = jsonb_set(payload, '{action}', '\"PURGED\"') WHERE id = %s",
             (eid,), "audit_event_count", 1)
        step(cur, "that event's customer_id changed to a zero-padded form",
             "UPDATE audit_events SET payload = jsonb_set(payload, '{customer_id}', to_jsonb(%s::text)) "
             "WHERE id = %s", ("0" + str(x), eid), "audit_event_count", -1)
        step(cur, "that event's customer_id changed back to the exact id",
             "UPDATE audit_events SET payload = jsonb_set(payload, '{customer_id}', to_jsonb(%s::text)) "
             "WHERE id = %s", (str(x), eid), "audit_event_count", 1)
        step(cur, "an order with status 'Cancelled'", ins_order,
             (999201, x, "Cancelled", "pay_pred_1"), "lifetime_orders", 1)
        step(cur, "an order with status ' canceled'", ins_order,
             (999202, x, " canceled", "pay_pred_2"), "lifetime_orders", 1)
        step(cur, "an order with status 'CANCELED'", ins_order,
             (999203, x, "CANCELED", "pay_pred_3"), "lifetime_orders", 0)
        step(cur, "the 'Cancelled' order changed to 'CANCELED'",
             "UPDATE orders SET status = 'CANCELED' WHERE id = %s",
             (999201,), "lifetime_orders", -1)

        # More shapes the contract decides by plain text comparison. An id
        # belongs to a customer only when it is exactly that customer's
        # id::text, and an event is dropped only when its action is exactly
        # 'purged'; everything else counts.
        step(cur, "an audit event whose customer_id has a trailing space", ins_audit,
             (x, json.dumps({"customer_id": str(x) + " ", "action": "view"})),
             "audit_event_count", 0)
        step(cur, "an audit event whose customer_id is an empty string", ins_audit,
             (x, json.dumps({"customer_id": "", "action": "view"})),
             "audit_event_count", 0)
        step(cur, "an audit event whose customer_id is JSON null", ins_audit,
             (x, json.dumps({"customer_id": None, "action": "view"})),
             "audit_event_count", 0)
        step(cur, "an audit event whose customer_id is a JSON boolean", ins_audit,
             (x, json.dumps({"customer_id": True, "action": "view"})),
             "audit_event_count", 0)
        step(cur, "an audit event whose customer_id is ten digits long", ins_audit,
             (x, json.dumps({"customer_id": "9999999999", "action": "view"})),
             "audit_event_count", 0)
        step(cur, "an audit event whose action is JSON null", ins_audit,
             (x, json.dumps({"customer_id": str(x), "action": None})),
             "audit_event_count", 1)
        step(cur, "an order whose status has a trailing space", ins_order,
             (999204, x, "canceled ", "pay_pred_4"), "lifetime_orders", 1)
        step(cur, "an order whose status is 'CaNcElEd'", ins_order,
             (999205, x, "CaNcElEd", "pay_pred_5"), "lifetime_orders", 0)


AGING_CID = 123457


def test_rows_leave_the_window_as_time_passes(db):
    # instruction.md: an order or an audit event stops counting once it is
    # older than the risk function's trailing window, whether or not anything
    # is written. Place one of each four seconds inside the window's far edge,
    # check both count, wait until the edge has passed them, and check that
    # neither counts any more. Nothing is written in between, so no trigger
    # fires: a total maintained only by triggers is still counting them.
    cid = AGING_CID
    cols = ("lifetime_orders", "lifetime_value_cents", "audit_event_count", "distinct_warehouses")
    # now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval clamps the day of the month and keeps the
    # time of day, so on the last days of some months the edge steps back a
    # day at midnight. Keep this check's few seconds clear of midnight so the
    # edge only moves forward while it runs.
    with db.cursor() as cur:
        cur.execute("SELECT extract(epoch FROM now() - date_trunc('day', now())) AS s")
        since_midnight = float(cur.fetchone()["s"])
    if since_midnight > 86400 - 30:
        time.sleep(86400 - since_midnight + 2)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO orders "
            "(id, customer_id, status, total_cents, warehouse_id, payment_ref, created_at) "
            "VALUES (999101, %s, 'placed', 5555, 1, 'pay_aging_check', "
            "now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval + interval '4 seconds')",
            (cid,),
        )
        cur.execute(
            "INSERT INTO audit_events (entity_type, entity_id, payload, created_at) "
            "VALUES ('customer', %s, %s, now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval + interval '4 seconds')",
            (cid, json.dumps({"customer_id": str(cid), "action": "view"})),
        )
        cur.execute(_golden_sql(), (cid,))
        want_in = cur.fetchone()
        cur.execute("SELECT * FROM checkout_risk_context(%s)", (cid,))
        got_in = cur.fetchone()

    time.sleep(6)

    with db.cursor() as cur:
        cur.execute(_golden_sql(), (cid,))
        want_out = cur.fetchone()
        cur.execute("SELECT * FROM checkout_risk_context(%s)", (cid,))
        got_out = cur.fetchone()

    # The two rows really did cross the edge; otherwise this proves nothing.
    assert int(want_in["lifetime_orders"]) == int(want_out["lifetime_orders"]) + 1
    assert int(want_in["lifetime_value_cents"]) == int(want_out["lifetime_value_cents"]) + 5555
    assert int(want_in["audit_event_count"]) == int(want_out["audit_event_count"]) + 1

    assert got_in is not None and got_out is not None
    for c in cols:
        assert int(got_in[c]) == int(want_in[c]), (
            f"{c} for customer {cid} is {got_in[c]} with a new row four seconds "
            f"inside the window; the current code path gives {want_in[c]}"
        )
        assert int(got_out[c]) == int(want_out[c]), (
            f"{c} for customer {cid} is {got_out[c]} after that row aged out of the "
            f"window with no write in between; the current code path gives "
            f"{want_out[c]} -- a stored total that only changes on writes goes stale"
        )


NEW_CUSTOMER_ID = 400777


def test_new_customers_get_correct_figures(db):
    # instruction.md: new customers keep being created, and a customer created
    # after the migration must have correct figures from its first order. The
    # backfill only ever saw the customers that existed when it ran.
    cid = NEW_CUSTOMER_ID
    cols = ("lifetime_orders", "lifetime_value_cents", "audit_event_count", "distinct_warehouses")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO customers (id, email, name, account_tier, region, created_at) "
            "VALUES (%s, %s, 'Newly Signed Up', 'pro', 'eu-west', now())",
            (cid, f"new{cid}@shop.example"),
        )

        def compare(step):
            cur.execute(_golden_sql(), (cid,))
            want = cur.fetchone()
            cur.execute("SELECT * FROM checkout_risk_context(%s)", (cid,))
            got = cur.fetchone()
            assert got is not None, f"after {step}: no risk context row for new customer {cid}"
            for c in cols:
                assert int(got[c]) == int(want[c]), (
                    f"after {step}: {c} for new customer {cid} is {got[c]}, "
                    f"the current code path gives {want[c]}"
                )

        compare("the customer is created with no activity")
        cur.execute(
            "INSERT INTO orders (id, customer_id, status, total_cents, warehouse_id, "
            "payment_ref, created_at) VALUES (999301, %s, 'placed', 7700, 1, 'pay_new_1', now())",
            (cid,),
        )
        compare("its first order")
        cur.execute(
            "INSERT INTO audit_events (entity_type, entity_id, payload, created_at) "
            "VALUES ('customer', %s, %s::jsonb, now())",
            (cid, json.dumps({"customer_id": str(cid), "action": "checkout_attempt"})),
        )
        compare("its first audit event")
        cur.execute("UPDATE orders SET status = 'canceled' WHERE id = 999301")
        compare("that order being canceled")


def test_bulk_corrections_are_handled(db):
    # instruction.md: corrections arrive in bulk as well as one row at a time,
    # and a single statement may change thousands of rows. Every one of them
    # has to move the figures, and the statement has to finish.
    cols = ("lifetime_orders", "lifetime_value_cents", "audit_event_count", "distinct_warehouses")

    def golden(cur):
        cur.execute(_golden_sql(), (WHALE_ID,))
        r = cur.fetchone()
        return {c: int(r[c]) for c in cols}

    def compare(cur, step):
        want = golden(cur)
        cur.execute("SELECT * FROM checkout_risk_context(%s)", (WHALE_ID,))
        got = cur.fetchone()
        got = {c: int(got[c]) for c in cols}
        assert got == want, f"after {step}: customer {WHALE_ID} got {got}, expected {want}"

    with db.cursor() as cur:
        cur.execute(
            "SELECT id, total_cents FROM orders WHERE customer_id = %s AND status = 'shipped' "
            "AND created_at >= now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval ORDER BY id LIMIT 2000",
            (WHALE_ID,),
        )
        rows = cur.fetchall()
        assert len(rows) >= 1000, (
            f"expected at least 1000 shipped in-window orders for customer {WHALE_ID}, "
            f"found {len(rows)}"
        )
        ids = [r["id"] for r in rows]
        value = sum(int(r["total_cents"]) for r in rows)

        before = golden(cur)
        cur.execute("UPDATE orders SET status = 'canceled' WHERE id = ANY(%s)", (ids,))
        after = golden(cur)
        assert after["lifetime_orders"] == before["lifetime_orders"] - len(ids)
        assert after["lifetime_value_cents"] == before["lifetime_value_cents"] - value
        compare(cur, f"{len(ids)} orders canceled in one statement")

        cur.execute("UPDATE orders SET status = 'shipped' WHERE id = ANY(%s)", (ids,))
        restored = golden(cur)
        assert restored["lifetime_orders"] == before["lifetime_orders"]
        assert restored["lifetime_value_cents"] == before["lifetime_value_cents"]
        compare(cur, f"the same {len(ids)} orders restored in one statement")


def test_bulk_reassignment_is_handled(db):
    # instruction.md: an order's customer association may change, and a
    # single statement may change thousands of rows at once.
    # test_reflects_timestamp_corrections_and_reassignment covers one order
    # reassigned at a time; this covers many orders reassigned to a
    # different customer in one statement. Maintenance that only reads
    # OLD.customer_id/NEW.customer_id off a single-row UPDATE, or that
    # otherwise assumes exactly one customer pair changes per statement,
    # gives a wrong balance for one or both customers here instead of
    # merely being slow.
    cols = ("lifetime_orders", "lifetime_value_cents", "audit_event_count", "distinct_warehouses")

    def golden(cur, cid):
        cur.execute(_golden_sql(), (cid,))
        r = cur.fetchone()
        return {c: int(r[c]) for c in cols}

    def compare(cur, cid, step):
        want = golden(cur, cid)
        cur.execute("SELECT * FROM checkout_risk_context(%s)", (cid,))
        got = cur.fetchone()
        got = {c: int(got[c]) for c in cols}
        assert got == want, f"after {step}: customer {cid} got {got}, expected {want}"

    with db.cursor() as cur:
        cur.execute(
            "SELECT id FROM customers WHERE id NOT IN (%s, %s) ORDER BY id DESC LIMIT 1",
            (WHALE_ID, 400002),
        )
        dest = cur.fetchone()["id"]

        cur.execute(
            "SELECT id, total_cents FROM orders WHERE customer_id = %s AND status = 'shipped' "
            "AND created_at >= now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval ORDER BY id LIMIT 2000",
            (WHALE_ID,),
        )
        rows = cur.fetchall()
        assert len(rows) >= 1000, (
            f"expected at least 1000 shipped in-window orders for customer {WHALE_ID}, "
            f"found {len(rows)}"
        )
        ids = [r["id"] for r in rows]

        before_src = golden(cur, WHALE_ID)
        before_dst = golden(cur, dest)
        cur.execute("UPDATE orders SET customer_id = %s WHERE id = ANY(%s)", (dest, ids))
        assert golden(cur, WHALE_ID)["lifetime_orders"] == before_src["lifetime_orders"] - len(ids)
        assert golden(cur, dest)["lifetime_orders"] == before_dst["lifetime_orders"] + len(ids)
        compare(cur, WHALE_ID, f"{len(ids)} orders reassigned away in one statement")
        compare(cur, dest, f"{len(ids)} orders received in one statement")

        cur.execute("UPDATE orders SET customer_id = %s WHERE id = ANY(%s)", (WHALE_ID, ids))
        after_src = golden(cur, WHALE_ID)
        after_dst = golden(cur, dest)
        assert after_src == before_src, (
            f"customer {WHALE_ID} did not return to its original figures "
            "after the bulk reassignment was undone"
        )
        assert after_dst == before_dst, (
            f"customer {dest} did not return to its original figures "
            "after the bulk reassignment was undone"
        )
        compare(cur, WHALE_ID, f"the same {len(ids)} orders reassigned back")
        compare(cur, dest, f"the same {len(ids)} orders reassigned back")


WINDOW_TEST_IDS = (7, 100, 31000, WHALE_ID)


def test_figures_follow_the_configured_window(db):
    # instruction.md: the window's length is an operational setting that Risk
    # changes without a deploy, and the figures must follow the current setting
    # immediately, including when it is widened. Widening reaches back to rows
    # that were outside the window when the migration ran, so anything the
    # remediation discarded on the grounds that it sat behind the edge is gone.
    cols = ("lifetime_orders", "lifetime_value_cents", "audit_event_count", "distinct_warehouses")

    def golden(cur, cid):
        cur.execute(_golden_sql(), (cid,))
        r = cur.fetchone()
        return {c: int(r[c]) for c in cols}

    def compare(cur, step):
        for cid in WINDOW_TEST_IDS:
            want = golden(cur, cid)
            cur.execute("SELECT * FROM checkout_risk_context(%s)", (cid,))
            got = cur.fetchone()
            assert got is not None, f"after {step}: no risk context for customer {cid}"
            got = {c: int(got[c]) for c in cols}
            assert got == want, f"after {step}: customer {cid} got {got}, expected {want}"

    def set_window(cur, value):
        cur.execute("UPDATE risk_settings SET value = %s WHERE key = 'risk_window'", (value,))

    with db.cursor() as cur:
        base = {cid: golden(cur, cid) for cid in WINDOW_TEST_IDS}
        compare(cur, "the window as configured")
        try:
            set_window(cur, "10 days")
            narrowed = golden(cur, WHALE_ID)
            assert narrowed["lifetime_orders"] < base[WHALE_ID]["lifetime_orders"], (
                "fixture check: narrowing the window to 10 days should drop orders "
                f"for customer {WHALE_ID}"
            )
            compare(cur, "the window narrowed to 10 days")

            set_window(cur, "6 months")
            widened = golden(cur, 7)
            assert widened["lifetime_orders"] > base[7]["lifetime_orders"], (
                "fixture check: widening the window to 6 months should bring older "
                "orders back for customer 7"
            )
            assert widened["audit_event_count"] > base[7]["audit_event_count"], (
                "fixture check: widening the window to 6 months should bring older "
                "audit events back for customer 7"
            )
            compare(cur, "the window widened to 6 months")
        finally:
            set_window(cur, "3 months")
        compare(cur, "the window set back")


def test_two_deploys_at_once(db):
    # instruction.md: multiple deploy runners may start the file at the same
    # moment; all must finish without error and leave the state a single
    # complete run produces. CREATE TABLE IF NOT EXISTS and CREATE INDEX
    # CONCURRENTLY are not atomic against another session running the same
    # statement: both see the object missing, and one then fails on the
    # duplicate name. Each run gets its own copy of the file so the only
    # contention is in the database. Six runners rather than two: the
    # session-level advisory lock has to serialise whichever number shows up,
    # not just a pair, and six exercises the wait queue under real depth
    # instead of a single handoff.
    staging = Path("/tmp/pg-fixtures/concurrent")
    staging.mkdir(parents=True, exist_ok=True)
    copies = []
    for name in (
        "migration_deploy_a.sql", "migration_deploy_b.sql", "migration_deploy_c.sql",
        "migration_deploy_d.sql", "migration_deploy_e.sql", "migration_deploy_f.sql",
    ):
        dest = staging / name
        dest.write_text(Path(MIGRATION).read_text(encoding="utf-8"), encoding="utf-8")
        copies.append(str(dest))

    baseline = BASELINE_PASS_SECONDS[0] if BASELINE_PASS_SECONDS else 10.0
    # Six runners fully serialised by the advisory lock is up to six
    # sequential complete runs; the multiplier scales with the runner count
    # for the same reason the two-runner version used 2x.
    cap = max(600.0, baseline * APPLY_BUDGET_PASSES * len(copies))
    failures: list[str] = []
    started = threading.Barrier(len(copies))

    def run(path):
        try:
            started.wait(timeout=60)
            pgutil.apply_migration(path, timeout=cap)
        except Exception as exc:                      # noqa: BLE001 - reported below
            failures.append(f"{Path(path).name}: {exc}")

    threads = [threading.Thread(target=run, args=(c,), daemon=True) for c in copies]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=cap * 2 + 60)
    assert not any(t.is_alive() for t in threads), (
        "a deploy was still running long after all should have finished; concurrent "
        "runners starting at once must not wedge each other"
    )
    assert not failures, "applying the file from concurrent deploy runners at once failed:\n" + "\n".join(failures)

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname AS index_name, t.relname AS table_name
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indexrelid
            JOIN pg_class t ON t.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = 'public' AND NOT (i.indisvalid AND i.indisready)
            """
        )
        broken = cur.fetchall()
    assert not broken, (
        "indexes left unusable after concurrent deploys ran at once: "
        + ", ".join(f"{r['index_name']} on {r['table_name']}" for r in broken)
    )

    cols = ("lifetime_orders", "lifetime_value_cents", "audit_event_count", "distinct_warehouses")
    with db.cursor() as cur:
        for cid in SAMPLE_IDS + (WHALE_ID,):
            cur.execute(_golden_sql(), (cid,))
            want = cur.fetchone()
            cur.execute("SELECT * FROM checkout_risk_context(%s)", (cid,))
            got = cur.fetchone()
            assert got is not None, f"no risk context for customer {cid} after concurrent deploys at once"
            for c in cols:
                assert int(got[c]) == int(want[c]), (
                    f"{c} for customer {cid} is {got[c]} after concurrent deploys ran at once; "
                    f"the current code path gives {want[c]}"
                )


def test_incident_resolves_under_load(db, live_stack):
    assert live_stack["healthy"], (
        "gateway and/or order-service never became healthy -- something in "
        "the environment or the migration broke service startup"
    )

    import random
    import threading

    RPS, WORKERS, DURATION_S = 8.0, 6, 60.0
    lock = threading.Lock()
    results = {"ok": 0, "err": 0, "lat_ms": []}
    stop_at = time.time() + DURATION_S

    def one_request():
        cid = random.randint(1, 400000)
        sku = f"SKU-{random.randint(1, 400):05d}"
        ok, ms = _http_post_json(
            "http://127.0.0.1:8080/api/checkout",
            {"customer_id": cid, "sku": sku, "qty": 1},
        )
        with lock:
            results["ok" if ok else "err"] += 1
            results["lat_ms"].append(ms)

    def worker():
        interval = WORKERS / RPS
        while time.time() < stop_at:
            t0 = time.time()
            one_request()
            remaining = interval - (time.time() - t0)
            if remaining > 0:
                time.sleep(remaining)

    threads = [threading.Thread(target=worker) for _ in range(WORKERS)]
    for t in threads:
        t.start()

    import concurrent.futures
    def _fire_concurrent_audits(cid):
        try:
            with psycopg.connect(pgutil.DSN) as c:
                with c.cursor() as cur:
                    cur.execute(
                        "INSERT INTO audit_events (entity_type, entity_id, payload, created_at) "
                        "VALUES ('customer', %s, '{\"action\": \"view\"}', now())",
                        (cid,)
                    )
                c.commit()
        except Exception:
            pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as bg_executor:
        bg_executor.map(_fire_concurrent_audits, [(i % 400000) + 1 for i in range(100)])

    for t in threads:
        t.join()

    total = results["ok"] + results["err"]
    err_rate = (results["err"] / total) if total else 1.0
    lat = sorted(results["lat_ms"])
    p95 = lat[int(0.95 * (len(lat) - 1))] if lat else float("inf")

    try:
        with urllib.request.urlopen("http://127.0.0.1:8081/metrics", timeout=3) as r:
            orders_metrics = json.loads(r.read().decode())
    except Exception:
        orders_metrics = {}

    assert total >= 100, (
        f"only {total} requests completed in {DURATION_S}s at the documented "
        f"~{RPS} rps offered load -- too few to judge whether the incident "
        f"is resolved"
    )
    assert err_rate <= 0.05, (
        f"error rate {err_rate:.1%} under sustained concurrent load through "
        f"the real gateway -> order-service -> checkout_risk_context path "
        f"-- the incident is still reproducing end to end, even though "
        f"checkout_risk_context tested fast in isolation above. "
        f"order-service metrics: {orders_metrics}"
    )
    assert orders_metrics.get("pool_wait_timeouts", 0) <= max(1, int(0.02 * total)), (
        f"order-service is still hitting pool_wait_timeouts under load "
        f"({orders_metrics.get('pool_wait_timeouts')} of {total} requests) "
        f"-- the connection-pool exhaustion that caused this incident is "
        f"not actually resolved end to end"
    )
    assert p95 < 350.0, (
        f"p95 {p95:.0f}ms under real concurrent load -- looser than the "
        f"runbook's aspirational 200ms on purpose (inventory/payments open "
        f"a fresh connection per request regardless of this migration, "
        f"which is real and outside migration.sql's scope), but this still "
        f"indicates the fix isn't holding up under real traffic. Measured "
        f"directly at this exact load profile: a migration that leaves "
        f"checkout_risk_context entirely unfixed lands around 790ms here; "
        f"one that fixes checkout_risk_context but skips the "
        f"payments-service pending-count index lands around 160-170ms; "
        f"the full reference fix lands around 90-105ms. All of the latter "
        f"two comfortably clear this bar -- it exists to catch a genuinely "
        f"broken or absent fix under load, not to force the payments index "
        f"specifically, which remains a real, disclosed secondary "
        f"contributor rather than a bound this test can reliably enforce."
    )
