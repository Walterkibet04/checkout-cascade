#!/bin/bash
set -euo pipefail
mkdir -p /app/remediation
cp /solution/migration.sql /app/remediation/migration.sql


export PGPASSWORD=checkout
if command -v psql >/dev/null 2>&1; then
  for i in $(seq 1 90); do
    if psql -h 127.0.0.1 -U checkout -d checkout -c 'SELECT 1' >/dev/null 2>&1; then
      psql -h 127.0.0.1 -U checkout -d checkout -v ON_ERROR_STOP=1 -f /app/remediation/migration.sql
      break
    fi
    sleep 2
  done
fi
