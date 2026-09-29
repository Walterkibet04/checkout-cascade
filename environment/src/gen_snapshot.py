#!/usr/bin/env python3
"""Build a frozen incident window so diagnosis is possible before live traffic accumulates."""
from __future__ import annotations

import json
import os
import random
from datetime import datetime, timedelta, timezone

OUT = "/app/telemetry/incident_window"
os.makedirs(OUT, exist_ok=True)
random.seed(7)

start = datetime(2026, 8, 27, 12, 4, 11, tzinfo=timezone.utc)


def iso(s, ms=0):
    return (start + timedelta(seconds=s, milliseconds=ms)).isoformat()


traces = []
svc = []
slow = []

for i in range(180):
    t = i * 0.4
    tid = f"{i:08x}deadbeefcafebabe"
    risk_ms = random.uniform(180, 920)
    inv_ms = random.uniform(8, 25)
    pay_ms = random.uniform(12, 22)
    checkout_ms = risk_ms + inv_ms + pay_ms + random.uniform(3, 15)
    gateway_ms = checkout_ms + random.uniform(2, 8)
    timed_out = checkout_ms > 2500 or risk_ms > 700
    if timed_out:
        gateway_ms = 3000 + random.uniform(0, 80)
        status = "error"
    else:
        status = "ok"

    traces.append({
        "ts": iso(t), "trace_id": tid, "span_id": f"g{i:04x}",
        "parent_span": None, "service": "gateway", "name": "POST /api/checkout",
        "duration_ms": round(gateway_ms, 2), "status": status,
    })
    traces.append({
        "ts": iso(t, 4), "trace_id": tid, "span_id": f"o{i:04x}",
        "parent_span": f"g{i:04x}", "service": "order-service", "name": "checkout",
        "duration_ms": round(checkout_ms, 2),
        "status": "error" if timed_out else "ok",
        "customer_id": 1 + (i * 97) % 400000,
    })
    traces.append({
        "ts": iso(t, int(risk_ms)), "trace_id": tid, "span_id": f"i{i:04x}",
        "parent_span": f"o{i:04x}", "service": "order-service", "name": "inventory.reserve",
        "duration_ms": round(inv_ms, 2), "status": "ok",
    })
    traces.append({
        "ts": iso(t, int(risk_ms + inv_ms)), "trace_id": tid, "span_id": f"p{i:04x}",
        "parent_span": f"o{i:04x}", "service": "order-service", "name": "payments.charge",
        "duration_ms": round(pay_ms, 2) if not timed_out else 0.0,
        "status": "error" if timed_out else "ok",
        "err": "timeout" if timed_out else None,
    })
    traces.append({
        "ts": iso(t, int(risk_ms + inv_ms + 2)), "trace_id": tid, "span_id": f"pv{i:04x}",
        "parent_span": f"p{i:04x}", "service": "payment-service", "name": "provider.charge",
        "duration_ms": round(pay_ms, 2), "status": "ok",
        "provider": "stripe",
    })

    if timed_out:
        svc.append({
            "ts": iso(t, int(gateway_ms)), "service": "gateway", "level": "error",
            "msg": "checkout failed after retries",
            "trace_id": tid, "err": "timed out",
            "suspected": "payment-service timeout or inventory stock lock",
        })
        svc.append({
            "ts": iso(t, int(risk_ms + inv_ms + 20)), "service": "payment-service",
            "level": "error", "msg": "client closed connection before charge response",
            "peer": "order-service", "trace_id": tid,
        })
        svc.append({
            "ts": iso(t, 10), "service": "order-service", "level": "error",
            "msg": "checkout failed", "trace_id": tid,
            "err": "timeout on payments.charge" if i % 2 == 0 else "order-service db pool exhausted",
        })
    if i % 6 == 0:
        svc.append({
            "ts": iso(t), "service": "order-service", "level": "error",
            "msg": "redis cache get failed", "err": "ConnectionRefusedError",
            "host": "cache.meridian.svc:6379",
        })
    if i % 15 == 0:
        svc.append({
            "ts": iso(t), "service": "inventory-service", "level": "warn",
            "msg": "row lock wait on inventory (batch_rebalance)", "wait_ms": 312,
        })

    cid = 1 + (i * 97) % 400000
    # Randomized offsets against a per-line probability, not a fixed modulus.
    jitter = lambda: random.uniform(0, 0.35)
    if random.random() < 0.20:
        slow.append(
            f"{iso(t, int(jitter()*1000))} UTC [7]: duration: {80 + random.randint(0, 40)} ms  "
            f"statement: SELECT count(*) AS n FROM payments WHERE status = 'pending'"
        )
    if random.random() < 0.14:
        slow.append(
            f"{iso(t, int(jitter()*1000))} UTC [7]: duration: {60 + random.randint(0, 25)} ms  "
            f"statement: SELECT * FROM reconcile_provider_payments('2026-08-01 00:00:00+00')"
        )
    if random.random() < 0.09:
        slow.append(
            f"{iso(t, int(jitter()*1000))} UTC [7]: duration: {120 + random.randint(0, 80)} ms  "
            f"statement: SELECT * FROM customer_affinity_preview({cid})"
        )
    if random.random() < 0.08:
        slow.append(
            f"{iso(t, int(jitter()*1000))} UTC [7]: duration: {55 + random.randint(0, 20)} ms  "
            "statement: SELECT p.id, p.price_cents, i.warehouse_id FROM products p "
            "JOIN inventory i ON i.product_id = p.id WHERE p.sku = 'SKU-00012'"
        )

svc.append({
    "ts": iso(0), "service": "payment-service", "level": "warn",
    "msg": "stripe_latency_inject flag is ON",
    "flag": "stripe_latency_inject", "configured_extra_ms": 400,
    "note": "flag currently no-ops against the mock provider",
})

metrics = {
    "captured_at": iso(72),
    "gateway": {"requests": 180, "errors": 74, "retries": 128, "latency_ms_avg": 1860},
    "order-service": {
        "checkouts": 106, "checkout_errors": 74, "pool_wait_timeouts": 41,
        "redis_errors": 180,
    },
    "inventory-service": {"reserves": 140, "stockouts": 2},
    "payment-service": {
        "charges": 106, "client_disconnects": 74, "provider_latency_ms_avg": 16.4,
        "pending_in_db": 35294,
    },
    "notes": [
        "Deploy 2026.08.26.18 cut a new order-service binary; payments and inventory unchanged.",
        "Cache cluster cache.meridian.svc was turned down on 2026-07-12.",
        "Pending payments count is historical seed (status='pending' every 17th row).",
    ],
}

with open(os.path.join(OUT, "traces.jsonl"), "w") as f:
    for rec in traces:
        f.write(json.dumps(rec) + "\n")
with open(os.path.join(OUT, "services.jsonl"), "w") as f:
    for rec in svc:
        f.write(json.dumps(rec) + "\n")
with open(os.path.join(OUT, "postgres_slow.log"), "w") as f:
    f.write("\n".join(slow) + "\n")
with open(os.path.join(OUT, "metrics.json"), "w") as f:
    json.dump(metrics, f, indent=2)
print("wrote", OUT)
