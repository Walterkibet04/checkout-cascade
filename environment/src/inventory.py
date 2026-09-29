#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
from psycopg.rows import dict_row

from common import Timer, log_event, write_span

DSN = os.environ.get("DATABASE_URL", "postgresql://checkout:checkout@127.0.0.1:5432/checkout")
PORT = int(os.environ.get("INVENTORY_PORT", "8082"))
_stats = {"reserves": 0, "stockouts": 0}


def lookup(sku, qty):
    with psycopg.connect(DSN, row_factory=dict_row, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id, p.price_cents, i.warehouse_id, i.qty, w.code, w.region
                FROM products p
                JOIN inventory i ON i.product_id = p.id
                JOIN warehouses w ON w.id = i.warehouse_id
                WHERE p.sku = %s AND i.qty >= %s
                ORDER BY i.qty DESC
                LIMIT 1
                """,
                (sku, qty),
            )
            row = cur.fetchone()
            if row is None:
                return None
            # INC-1904: warehouses were over-committing stock they had already
            # promised, so every reservation now checks how much the warehouse
            # has taken on in the last hour before handing out more.
            cur.execute(
                """
                SELECT count(*) AS n
                FROM orders
                WHERE warehouse_id = %s
                  AND created_at >= now() - interval '1 hour'
                """,
                (row["warehouse_id"],),
            )
            row["recent_reservations"] = int(cur.fetchone()["n"])
            return row


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
            self._send(200, {"ok": True, "service": "inventory-service"})
            return
        if self.path == "/metrics":
            self._send(200, _stats)
            return
        self._send(404, {"ok": False})

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path != "/reserve":
            self._send(404, {"ok": False})
            return
        sku = body.get("sku")
        qty = int(body.get("qty") or 1)
        trace_id = body.get("trace_id")
        with Timer() as t:
            row = lookup(sku, qty)
        if row is None:
            _stats["stockouts"] += 1
            write_span(trace_id or "-", "inventory-service", "reserve", t.ms, status="error", sku=sku)
            log_event("inventory-service", "warn", "stockout", sku=sku, qty=qty, trace_id=trace_id)
            self._send(200, {"ok": False, "reason": "stockout"})
            return
        _stats["reserves"] += 1
        write_span(
            trace_id or "-", "inventory-service", "reserve", t.ms, status="ok",
            sku=sku, warehouse=row["code"],
        )
        if _stats["reserves"] % 40 == 0:
            log_event(
                "inventory-service", "warn",
                "row lock wait on inventory (batch_rebalance)",
                wait_ms=312, warehouse=row["code"],
            )
        self._send(
            200,
            {
                "ok": True,
                "warehouse": row["code"],
                "unit_price_cents": int(row["price_cents"]),
            },
        )


def noise():
    while True:
        time.sleep(11)
        log_event(
            "inventory-service", "info",
            "rebalance tick",
            lag_s=2.4,
            note="batch_rebalance holds SHARE ROW EXCLUSIVE on inventory for ~200ms",
        )


if __name__ == "__main__":
    threading.Thread(target=noise, daemon=True).start()
    log_event("inventory-service", "info", "listening", port=PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
