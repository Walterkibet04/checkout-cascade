#!/bin/bash
mkdir -p /app/telemetry /app/remediation
if [ ! -f /tmp/platform.started ]; then
  touch /tmp/platform.started
  /bin/bash /app/src/start.sh >/app/telemetry/boot.log 2>&1 &
fi
if [ "$#" -eq 0 ]; then
  exec sleep infinity
fi
exec "$@"
