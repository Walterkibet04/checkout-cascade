#!/usr/bin/env python3
"""Continuous checkout traffic. Same mix the verifier will replay."""
from __future__ import annotations

import json
import os
import random
import threading
import time
import urllib.error
import urllib.request

from common import log_event

GATEWAY = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8080")
RPS = float(os.environ.get("LOADGEN_RPS", "8"))
WORKERS = int(os.environ.get("LOADGEN_WORKERS", "6"))
SKUS = [f"SKU-{i:05d}" for i in range(1, 401)]

_lock = threading.Lock()
_stats = {"ok": 0, "err": 0, "lat_ms": []}


def one():
    cid = random.randint(1, 400000)
    sku = random.choice(SKUS)
    payload = json.dumps({"customer_id": cid, "sku": sku, "qty": 1}).encode()
    req = urllib.request.Request(
        GATEWAY + "/api/checkout",
        data=payload,
        headers={"content-type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = json.loads(resp.read().decode())
            ok = bool(body.get("ok"))
    except Exception:
        ok = False
    ms = (time.perf_counter() - t0) * 1000
    with _lock:
        _stats["ok" if ok else "err"] += 1
        _stats["lat_ms"].append(ms)
        if len(_stats["lat_ms"]) > 500:
            _stats["lat_ms"] = _stats["lat_ms"][-400:]


def worker():
    interval = WORKERS / max(RPS, 0.1)
    while True:
        t0 = time.time()
        one()
        sleep = interval - (time.time() - t0)
        if sleep > 0:
            time.sleep(sleep)


def reporter():
    while True:
        time.sleep(15)
        with _lock:
            lats = list(_stats["lat_ms"])
            ok, err = _stats["ok"], _stats["err"]
        lats.sort()
        p95 = lats[int(0.95 * (len(lats) - 1))] if lats else 0
        total = ok + err
        err_rate = (err / total) if total else 1
        log_event(
            "loadgen",
            "error" if err_rate > 0.05 or p95 > 400 else "info",
            "slo snapshot",
            ok=ok,
            err=err,
            err_rate=round(err_rate, 4),
            p95_ms=round(p95, 1),
            target_rps=RPS,
        )


if __name__ == "__main__":
    log_event("loadgen", "info", "starting", rps=RPS, workers=WORKERS)
    for _ in range(WORKERS):
        threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=reporter, daemon=True).start()
    while True:
        time.sleep(60)
