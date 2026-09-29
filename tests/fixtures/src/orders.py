#!/usr/bin/env python3
"""Order service. Owns checkout orchestration and customer risk context."""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
from psycopg.rows import dict_row

from common import Timer, log_event, new_trace_id, write_span

DSN = os.environ.get("DATABASE_URL", "postgresql://checkout:checkout@127.0.0.1:5432/checkout")
INVENTORY_URL = os.environ.get("INVENTORY_URL", "http://127.0.0.1:8082")
PAYMENTS_URL = os.environ.get("PAYMENTS_URL", "http://127.0.0.1:8083")
PORT = int(os.environ.get("ORDERS_PORT", "8081"))
POOL_SIZE = int(os.environ.get("ORDERS_POOL_SIZE", "8"))
HTTP_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT_SEC", "2.5"))

_pool_lock = threading.Lock()
_pool = []
_in_use = 0
_stats = {
    "checkouts": 0,
    "checkout_errors": 0,
    "pool_wait_timeouts": 0,
    "risk_ms_sum": 0.0,
    "risk_ms_max": 0.0,
    "cache_ms_sum": 0.0,
    "cache_ms_max": 0.0,
    "redis_errors": 0,
}


def _conn():
    # autocommit: each checkout query is its own transaction, so a pooled
    # connection never sits "idle in transaction" between requests.
    return psycopg.connect(DSN, row_factory=dict_row, connect_timeout=5, autocommit=True)


def acquire(timeout=2.0):
    global _in_use
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _pool_lock:
            if _pool:
                _in_use += 1
                return _pool.pop()
            if _in_use < POOL_SIZE:
                _in_use += 1
                return _conn()
        time.sleep(0.01)
    _stats["pool_wait_timeouts"] += 1
    raise TimeoutError("order-service db pool exhausted")


def release(conn):
    global _in_use
    with _pool_lock:
        _in_use -= 1
        if len(_pool) < POOL_SIZE:
            try:
                _pool.append(conn)
                return
            except Exception:
                pass
    try:
        conn.close()
    except Exception:
        pass


def http_json(url, payload=None, method=None):
    data = None
    hdrs = {"content-type": "application/json", "accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode()
        method = method or "POST"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method or "GET")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        body = resp.read().decode()
        return json.loads(body) if body else {}


def redis_get(_key):
    # Cache sidecar was decommissioned last quarter. The client was never removed.
    _stats["redis_errors"] += 1
    log_event(
        "order-service",
        "error",
        "redis cache get failed",
        err="ConnectionRefusedError",
        host="cache.meridian.svc:6379",
        key=_key,
    )
    return None


def load_risk_context(conn, customer_id):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM checkout_risk_context(%s)", (customer_id,))
        row = cur.fetchone()
    return row


def handle_checkout(body):
    customer_id = int(body["customer_id"])
    sku = body["sku"]
    qty = int(body.get("qty", 1))
    trace_id = body.get("trace_id") or new_trace_id()
    t_all = Timer()
    t_all.__enter__()

    with Timer() as t_cache:
        redis_get(f"risk:{customer_id}")
    _stats["cache_ms_sum"] += t_cache.ms
    _stats["cache_ms_max"] = max(_stats["cache_ms_max"], t_cache.ms)

    conn = None
    try:
        conn = acquire()
        with Timer() as t_risk:
            ctx = load_risk_context(conn, customer_id)
        _stats["risk_ms_sum"] += t_risk.ms
        _stats["risk_ms_max"] = max(_stats["risk_ms_max"], t_risk.ms)
        if ctx is None:
            raise ValueError("unknown customer")

        # RISK-151: refuse checkout for a customer whose recent payments keep
        # being declined.
        with conn.cursor() as cur:
            cur.execute("SELECT checkout_recent_declines(%s) AS n", (customer_id,))
            if int(cur.fetchone()["n"]) >= 25:
                raise RuntimeError("payments: too many recent declines")

        with Timer() as t_inv:
            inv = http_json(
                f"{INVENTORY_URL}/reserve",
                {"sku": sku, "qty": qty, "warehouse_hint": ctx["region"], "trace_id": trace_id},
            )
        write_span(
            trace_id, "order-service", "inventory.reserve", t_inv.ms,
            status="ok" if inv.get("ok") else "error",
            warehouse=inv.get("warehouse"),
        )
        if not inv.get("ok"):
            raise RuntimeError(f"inventory: {inv.get('reason', 'unavailable')}")

        amount = int(inv["unit_price_cents"]) * qty
        with Timer() as t_pay:
            pay = http_json(
                f"{PAYMENTS_URL}/charge",
                {
                    "customer_id": customer_id,
                    "amount_cents": amount,
                    "provider": "stripe",
                    "trace_id": trace_id,
                },
            )
        write_span(
            trace_id, "order-service", "payments.charge", t_pay.ms,
            status="ok" if pay.get("ok") else "error",
            provider=pay.get("provider"),
        )
        if not pay.get("ok"):
            raise RuntimeError(f"payments: {pay.get('reason', 'declined')}")

        _stats["checkouts"] += 1
        t_all.__exit__()
        write_span(
            trace_id, "order-service", "checkout", t_all.ms, status="ok",
            customer_id=customer_id, lifetime_orders=int(ctx["lifetime_orders"]),
        )
        log_event(
            "order-service", "info", "checkout accepted",
            trace_id=trace_id, customer_id=customer_id, amount_cents=amount,
            inventory_ms=round(t_inv.ms, 1),
            payment_ms=round(t_pay.ms, 1), total_ms=round(t_all.ms, 1),
        )
        return {
            "ok": True,
            "trace_id": trace_id,
            "amount_cents": amount,
            "payment_ref": pay.get("provider_ref"),
            "tier": ctx["account_tier"],
        }
    except Exception as e:
        _stats["checkout_errors"] += 1
        t_all.__exit__()
        write_span(trace_id, "order-service", "checkout", t_all.ms, status="error", err=str(e)[:200])
        log_event("order-service", "error", "checkout failed", trace_id=trace_id, err=str(e)[:200])
        return {"ok": False, "trace_id": trace_id, "error": str(e)[:200]}
    finally:
        if conn is not None:
            release(conn)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "service": "order-service", "pool_in_use": _in_use})
            return
        if self.path == "/metrics":
            self._send(200, {
                "checkouts": _stats["checkouts"],
                "checkout_errors": _stats["checkout_errors"],
                "pool_wait_timeouts": _stats["pool_wait_timeouts"],
                "redis_errors": _stats["redis_errors"],
                "pool_in_use": _in_use,
            })
            return
        self._send(404, {"ok": False})

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/checkout":
            result = handle_checkout(body)
            self._send(200 if result.get("ok") else 503, result)
            return
        self._send(404, {"ok": False})


def noise_thread():
    while True:
        time.sleep(7)
        log_event(
            "order-service",
            "warn",
            "redis cache miss rate high",
            miss_rate=1.0,
            host="cache.meridian.svc:6379",
        )


if __name__ == "__main__":
    threading.Thread(target=noise_thread, daemon=True).start()
    log_event("order-service", "info", "listening", port=PORT, pool=POOL_SIZE)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
