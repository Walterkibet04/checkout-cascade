import json
import os
import time
import uuid
from datetime import datetime, timezone

LOG_DIR = os.environ.get("PLATFORM_LOG_DIR", "/app/telemetry")
TRACE_PATH = os.path.join(LOG_DIR, "traces.jsonl")
SERVICE_LOG = os.path.join(LOG_DIR, "services.jsonl")
METRICS_PATH = os.path.join(LOG_DIR, "metrics.json")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def ensure_log_dir():
    os.makedirs(LOG_DIR, exist_ok=True)


def log_event(service, level, msg, **fields):
    ensure_log_dir()
    rec = {
        "ts": now_iso(),
        "service": service,
        "level": level,
        "msg": msg,
        **fields,
    }
    with open(SERVICE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def write_span(trace_id, service, name, duration_ms, status="ok", parent=None, **attrs):
    ensure_log_dir()
    rec = {
        "ts": now_iso(),
        "trace_id": trace_id,
        "span_id": uuid.uuid4().hex[:16],
        "parent_span": parent,
        "service": service,
        "name": name,
        "duration_ms": round(duration_ms, 2),
        "status": status,
        **attrs,
    }
    with open(TRACE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def new_trace_id():
    return uuid.uuid4().hex


class Timer:
    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.ms = (time.perf_counter() - self.t0) * 1000.0
