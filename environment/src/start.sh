#!/bin/bash
set -euo pipefail

export PLATFORM_LOG_DIR=/app/telemetry
export DATABASE_URL="${DATABASE_URL:-postgresql://checkout:checkout@127.0.0.1:5432/checkout}"
mkdir -p /app/telemetry /var/lib/postgresql /var/run/postgresql /app/remediation
chown -R postgres:postgres /var/lib/postgresql /var/run/postgresql /etc/postgresql 2>/dev/null || true
# logging_collector below writes postgres.log into /app/telemetry as the
# postgres OS user, but this directory is created root-owned at image
# build time -- without this, postgres can't open its own log file,
# pg_ctl fails, and this script (set -euo pipefail) aborts before ever
# creating a role, loading the schema, or starting a single service,
# silently contradicting "you are on a live slice already taking
# traffic". Group-writable, not chowned outright, so the agent's own
# root-run investigation tooling can still read/write here freely.
chown root:postgres /app/telemetry
chmod 775 /app/telemetry

PGDATA=/var/lib/postgresql/data
if [ ! -f "$PGDATA/PG_VERSION" ]; then
  if [ -d /usr/lib/postgresql/16 ]; then
    PG_VER=16
  else
    PG_VER=$(ls /usr/lib/postgresql | sort -n | tail -1)
  fi
  export PG_VER
  mkdir -p "$PGDATA"
  chown postgres:postgres "$PGDATA"
  su -s /bin/bash postgres -c "/usr/lib/postgresql/${PG_VER}/bin/initdb -D $PGDATA --encoding=UTF8 --locale=C --auth-local=trust --auth-host=password"
  echo "listen_addresses = '127.0.0.1'" >> "$PGDATA/postgresql.conf"
  echo "port = 5432" >> "$PGDATA/postgresql.conf"
  echo "log_min_duration_statement = 8000" >> "$PGDATA/postgresql.conf"
  echo "log_statement = 'none'" >> "$PGDATA/postgresql.conf"
  echo "logging_collector = on" >> "$PGDATA/postgresql.conf"
  echo "log_directory = '/app/telemetry'" >> "$PGDATA/postgresql.conf"
  echo "log_filename = 'postgres.log'" >> "$PGDATA/postgresql.conf"
  echo "max_connections = 80" >> "$PGDATA/postgresql.conf"
  echo "shared_buffers = 128MB" >> "$PGDATA/postgresql.conf"
  echo "work_mem = 4MB" >> "$PGDATA/postgresql.conf"
  echo "127.0.0.1:5432:checkout:checkout:checkout" > /var/lib/postgresql/.pgpass
  chown postgres:postgres /var/lib/postgresql/.pgpass
  chmod 600 /var/lib/postgresql/.pgpass
  su -s /bin/bash postgres -c "/usr/lib/postgresql/${PG_VER}/bin/pg_ctl -D $PGDATA -w start"
  su -s /bin/bash postgres -c "psql -v ON_ERROR_STOP=1 -c \"CREATE USER checkout WITH PASSWORD 'checkout';\" postgres"
  su -s /bin/bash postgres -c "psql -v ON_ERROR_STOP=1 -c \"CREATE DATABASE checkout OWNER checkout;\" postgres"
  su -s /bin/bash postgres -c "psql -v ON_ERROR_STOP=1 -U checkout -d checkout -f /app/postgres/schema.sql"
  su -s /bin/bash postgres -c "psql -v ON_ERROR_STOP=1 -U checkout -d checkout -f /app/postgres/seed.sql"
  # The order/payments/inventory services connect as their own role,
  # separate from the checkout role used for ad-hoc investigation and
  # for applying migration.sql. This isn't an artificial nerf: it's the
  # same least-privilege posture most real production DBs already use
  # -- an app service account isn't normally granted visibility into
  # every other session's live query text (pg_stat_activity.query is
  # only visible across roles to a superuser or pg_read_all_stats,
  # neither of which checkout_svc or checkout have here), and a
  # database owner isn't automatically the same identity the
  # connection-pooled services run as. checkout_svc only needs data
  # access, not schema ownership, so it's granted table privileges via
  # membership rather than made an owner of anything.
  su -s /bin/bash postgres -c "psql -v ON_ERROR_STOP=1 -c \"CREATE USER checkout_svc WITH PASSWORD 'checkout_svc' IN ROLE checkout;\" postgres"
  echo "127.0.0.1:5432:checkout:checkout_svc:checkout_svc" >> /var/lib/postgresql/.pgpass
else
  PG_VER=$(ls /usr/lib/postgresql | sort -n | tail -1)
  su -s /bin/bash postgres -c "/usr/lib/postgresql/${PG_VER}/bin/pg_ctl -D $PGDATA -w start" || true
fi

export PGDATA PG_VER
echo "$PG_VER" > /tmp/pg_ver
echo "$PGDATA" > /tmp/pg_data

cd /app/src
SVC_DATABASE_URL="postgresql://checkout_svc:checkout_svc@127.0.0.1:5432/checkout"
DATABASE_URL="$SVC_DATABASE_URL" python3 inventory.py >/app/telemetry/inventory.stdout 2>&1 &
echo $! > /tmp/inventory.pid
DATABASE_URL="$SVC_DATABASE_URL" python3 payments.py >/app/telemetry/payments.stdout 2>&1 &
echo $! > /tmp/payments.pid
DATABASE_URL="$SVC_DATABASE_URL" python3 orders.py >/app/telemetry/orders.stdout 2>&1 &
echo $! > /tmp/orders.pid
python3 gateway.py >/app/telemetry/gateway.stdout 2>&1 &
echo $! > /tmp/gateway.pid

for i in $(seq 1 60); do
  if curl -sf http://127.0.0.1:8080/health >/dev/null && curl -sf http://127.0.0.1:8081/health >/dev/null; then
    break
  fi
  sleep 1
done

python3 loadgen.py >/app/telemetry/loadgen.stdout 2>&1 &
echo $! > /tmp/loadgen.pid

echo "platform up" >&2
wait -n || true
sleep infinity
