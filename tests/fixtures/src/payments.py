#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
from psycopg.rows import dict_row

from common import Timer, log_event, write_span

DSN = os.environ.get("DATABASE_URL", "postgresql://checkout:checkout@127.0.0.1:5432/checkout")
PORT = int(os.environ.get("PAYMENTS_PORT", "8083"))
_stats = {
    "charges": 0,
    "client_disconnects": 0,
    "provider_latency_ms_sum": 0.0,
}


def record_attempt(customer_id, amount, provider_ref, status):
    # payments table is append-mostly historical; we do not insert on every
    # live charge to keep the seed stable. A side table would exist in prod.
    with psycopg.connect(DSN, row_factory=dict_row, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM payments WHERE status = 'pending'"
            )
            return cur.fetchone()["n"]


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
            self._send(200, {"ok": True, "service": "payment-service"})
            return
        if self.path == "/metrics":
            n = max(_stats["charges"], 1)
            self._send(
                200,
                {
                    **_stats,
                    "provider_latency_ms_avg": round(_stats["provider_latency_ms_sum"] / n, 2),
                    "pending_in_db": _pending(),
                },
            )
            return
        self._send(404, {"ok": False})

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path != "/charge":
            self._send(404, {"ok": False})
            return
        trace_id = body.get("trace_id")
        amount = int(body.get("amount_cents") or 0)
        # Simulated provider is local and fast.
        with Timer() as t:
            time.sleep(0.015)
            ref = "tok_" + uuid.uuid4().hex[:12]
            try:
                _pending()
            except Exception as e:
                log_event("payment-service", "error", "pending-payments scan failed", err=str(e)[:160])
        _stats["charges"] += 1
        _stats["provider_latency_ms_sum"] += t.ms
        write_span(
            trace_id or "-", "payment-service", "provider.charge", t.ms, status="ok",
            provider=body.get("provider") or "stripe", amount_cents=amount,
        )
        log_event(
            "payment-service", "info", "charge captured",
            trace_id=trace_id, amount_cents=amount, provider_ref=ref,
            provider_ms=round(t.ms, 1),
        )
        self._send(200, {"ok": True, "provider_ref": ref, "provider": body.get("provider") or "stripe"})


def _pending():
    with psycopg.connect(DSN, row_factory=dict_row, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM payments WHERE status = 'pending'")
            return int(cur.fetchone()["n"])


def noise():
    while True:
        time.sleep(5)
        _stats["client_disconnects"] += 1
        log_event(
            "payment-service",
            "error",
            "client closed connection before charge response",
            peer="order-service",
            hint="typically seen when the caller hits UPSTREAM_TIMEOUT_SEC",
            disconnects=_stats["client_disconnects"],
        )
        if _stats["client_disconnects"] % 3 == 0:
            log_event(
                "payment-service",
                "warn",
                "stripe_latency_inject flag is ON",
                flag="stripe_latency_inject",
                configured_extra_ms=400,
                observed_provider_ms=15,
                note="flag currently no-ops against the mock provider",
            )


if __name__ == "__main__":
    threading.Thread(target=noise, daemon=True).start()
    log_event("payment-service", "info", "listening", port=PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
