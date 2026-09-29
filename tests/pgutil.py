"""Start a throwaway Postgres and load fixtures."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

FIXTURES = Path("/tests/fixtures")
if not (FIXTURES / "schema.sql").exists():
    FIXTURES = Path(__file__).resolve().parent / "fixtures"

PGDATA = Path("/tmp/pgdata-verifier")
PGPORT = "55432"
DSN = "postgresql://checkout@127.0.0.1:55432/checkout"
SVC_DSN = "postgresql://checkout_svc:checkout_svc@127.0.0.1:55432/checkout"


def _pg_bin() -> Path:
    versions = sorted(Path("/usr/lib/postgresql").glob("*"), key=lambda p: p.name)
    if not versions:
        raise RuntimeError("postgresql server binaries not installed")
    return versions[-1] / "bin"


def _shell_quote(s: str) -> str:
    return "'" + str(s).replace("'", "'\"'\"'") + "'"


def _as_postgres(args, **kwargs):
    cmd = ["su", "-s", "/bin/bash", "postgres", "-c", " ".join(_shell_quote(a) for a in args)]
    return subprocess.run(cmd, **kwargs)


def _check_postgres(args, **kwargs):
    r = _as_postgres(args, **kwargs)
    if r.returncode != 0:
        raise subprocess.CalledProcessError(r.returncode, args)
    return r


def _alive() -> bool:
    r = _as_postgres(
        [str(_pg_bin() / "pg_isready"), "-h", "127.0.0.1", "-p", PGPORT],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


def _stage(src: Path) -> Path:
    dest_dir = Path("/tmp/pg-fixtures")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    dest.write_text(src.read_text(encoding="utf-8"))
    dest.chmod(0o644)
    return dest


def start():
    bin_dir = _pg_bin()
    if _alive():
        return DSN
    PGDATA.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(["chown", "-R", "postgres:postgres", str(PGDATA)])
    if not (PGDATA / "PG_VERSION").exists():
        for leftover in list(PGDATA.iterdir()):
            subprocess.check_call(["rm", "-rf", str(leftover)])
        _check_postgres(
            [str(bin_dir / "initdb"), "-D", str(PGDATA), "--auth-local=trust", "--auth-host=trust"],
            stdout=subprocess.DEVNULL,
        )
        conf = PGDATA / "postgresql.conf"
        conf.write_text(
            conf.read_text()
            + "\nlisten_addresses = '127.0.0.1'\n"
            + f"port = {PGPORT}\n"
            + "shared_buffers = 128MB\n"
            + "fsync = off\n"
            + "full_page_writes = off\n"
            + "synchronous_commit = off\n"
        )
        subprocess.check_call(["chown", "postgres:postgres", str(conf)])
    _check_postgres(
        [str(bin_dir / "pg_ctl"), "-D", str(PGDATA), "-w", "-o", f"-p {PGPORT}", "start"],
        stdout=subprocess.DEVNULL,
    )
    deadline = time.time() + 30
    while time.time() < deadline and not _alive():
        time.sleep(0.2)
    psql = str(bin_dir / "psql")
    _as_postgres(
        [psql, "-h", "127.0.0.1", "-p", PGPORT, "-d", "postgres", "-v", "ON_ERROR_STOP=1",
         "-c", "CREATE USER checkout CREATEDB;"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _as_postgres(
        [psql, "-h", "127.0.0.1", "-p", PGPORT, "-d", "postgres", "-v", "ON_ERROR_STOP=1",
         "-c", "CREATE DATABASE checkout OWNER checkout;"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
  
    _as_postgres(
        [psql, "-h", "127.0.0.1", "-p", PGPORT, "-d", "postgres", "-v", "ON_ERROR_STOP=1",
         "-c", "CREATE USER checkout_svc WITH PASSWORD 'checkout_svc' IN ROLE checkout;"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    seeded = Path("/tmp/pg-fixtures/seeded")
    if not seeded.exists():
        for name in ("schema.sql", "seed.sql"):
            dest = _stage(FIXTURES / name)
            _check_postgres(
                [psql, "-h", "127.0.0.1", "-p", PGPORT, "-U", "checkout", "-d", "checkout",
                 "-v", "ON_ERROR_STOP=1", "-f", str(dest)],
            )
        seeded.write_text("1")
    return DSN


def apply_migration(path: str, timeout: float = None) -> float:
    """Apply the migration and return how long a complete run took, in seconds.

    timeout kills a run that overshoots, so one pathological migration cannot
    consume the whole verifier budget; the caller reports it as a budget
    failure rather than a hang.
    """
    dest = _stage(Path(path))
    bin_dir = _pg_bin()
    args = [str(bin_dir / "psql"), "-h", "127.0.0.1", "-p", PGPORT, "-U", "checkout",
            "-d", "checkout", "-v", "ON_ERROR_STOP=1", "-f", str(dest)]
    cmd = ["su", "-s", "/bin/bash", "postgres", "-c", " ".join(_shell_quote(a) for a in args)]
    started = time.monotonic()
    proc = subprocess.Popen(cmd, start_new_session=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.communicate()
        raise AssertionError(
            f"migration.sql was still running after {timeout:.0f}s and was killed"
        )
    elapsed = time.monotonic() - started
    if proc.returncode != 0:
        raise AssertionError(
            f"applying migration.sql failed (exit {proc.returncode}): {(out or '')[-2000:]}"
        )
    return elapsed


def aggregate_pass_seconds() -> float:
    """Time one full aggregate pass over orders and audit_events.

    This is the floor on what any complete backfill has to do -- read both
    tables once and group them by customer -- and it is measured on the same
    machine, moments before the migration runs, so the budget instruction.md
    states is a ratio rather than a wall-clock number tied to one runner.
    """
    import psycopg

    total = 0.0
    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            for sql in (
                "SELECT count(*) FROM (SELECT customer_id, count(*), sum(total_cents) "
                "FROM orders WHERE lower(status) <> 'canceled' GROUP BY customer_id) q",
                "SELECT count(*) FROM (SELECT payload->>'customer_id', count(*) "
                "FROM audit_events WHERE coalesce(payload->>'action', '') <> 'purged' "
                "GROUP BY 1) q",
            ):
                started = time.monotonic()
                cur.execute(sql)
                cur.fetchall()
                total += time.monotonic() - started
    return total


def apply_migration_interrupted(path: str, seconds: float) -> int:
    """Start the migration, interrupt it partway through, return psql's exit code.

    instruction.md says the file is a deploy step that may be interrupted and
    retried, and that a later complete run has to leave the database in the
    state an uninterrupted run would have produced. SIGINT to the process
    group is what a cancelled deploy or a dropped psql session looks like:
    psql cancels the running statement and exits under ON_ERROR_STOP=1.
    """
    dest = _stage(Path(path))
    bin_dir = _pg_bin()
    args = [str(bin_dir / "psql"), "-h", "127.0.0.1", "-p", PGPORT, "-U", "checkout",
            "-d", "checkout", "-v", "ON_ERROR_STOP=1", "-f", str(dest)]
    cmd = ["su", "-s", "/bin/bash", "postgres", "-c", " ".join(_shell_quote(a) for a in args)]
    proc = subprocess.Popen(cmd, start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(seconds)
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=60)
        except ProcessLookupError:
            pass
    return proc.returncode
