#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from common import Timer, log_event, new_trace_id, write_span

ORDERS_URL = os.environ.get("ORDERS_URL", "http://127.0.0.1:8081")
PORT = int(os.environ.get("GATEWAY_PORT", "8080"))
RETRIES = int(os.environ.get("GATEWAY_RETRIES", "2"))
TIMEOUT = float(os.environ.get("GATEWAY_TIMEOUT_SEC", "3.0"))
_stats = {"requests": 0, "errors": 0, "retries": 0, "latency_ms_sum": 0.0}


def call_orders(path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        ORDERS_URL + path,
        data=data,
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.status, json.loads(resp.read().decode())


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
            self._send(200, {"ok": True, "service": "gateway"})
            return
        if self.path == "/metrics":
            n = max(_stats["requests"], 1)
            self._send(200, {**_stats, "latency_ms_avg": round(_stats["latency_ms_sum"] / n, 2)})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path != "/api/checkout":
            self._send(404, {"error": "not found"})
            return
        _stats["requests"] += 1
        trace_id = new_trace_id()
        body["trace_id"] = trace_id
        last_err = "unknown"
        with Timer() as t:
            for attempt in range(RETRIES + 1):
                try:
                    status, result = call_orders("/checkout", body)
                    if result.get("ok"):
                        # t.ms is only set by Timer.__exit__, which hasn't
                        # run yet on this success path (we're still inside
                        # the with-block) -- compute elapsed time directly
                        # instead of reading the not-yet-populated attribute.
                        elapsed_ms = (time.perf_counter() - t.t0) * 1000.0
                        _stats["latency_ms_sum"] += elapsed_ms
                        write_span(trace_id, "gateway", "POST /api/checkout", elapsed_ms, status="ok")
                        log_event(
                            "gateway", "info", "checkout ok",
                            trace_id=trace_id, attempt=attempt, total_ms=round(elapsed_ms, 1),
                        )
                        self._send(200, result)
                        return
                    last_err = result.get("error") or f"status {status}"
                except Exception as e:
                    last_err = str(e)[:200]
                if attempt < RETRIES:
                    _stats["retries"] += 1
                    log_event(
                        "gateway", "warn", "retrying checkout",
                        trace_id=trace_id, attempt=attempt, err=last_err,
                    )
                    time.sleep(0.05 * (attempt + 1))
        _stats["errors"] += 1
        _stats["latency_ms_sum"] += t.ms
        write_span(trace_id, "gateway", "POST /api/checkout", t.ms, status="error", err=last_err)
        log_event(
            "gateway", "error", "checkout failed after retries",
            trace_id=trace_id, err=last_err, total_ms=round(t.ms, 1),
            suspected="payment-service timeout or inventory stock lock",
        )
        self._send(504, {"ok": False, "error": last_err, "trace_id": trace_id})


if __name__ == "__main__":
    log_event("gateway", "info", "listening", port=PORT, retries=RETRIES, timeout_sec=TIMEOUT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
