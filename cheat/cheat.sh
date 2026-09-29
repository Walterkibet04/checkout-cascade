#!/bin/bash
# Deliberate cheating attempt. Informational only -- the pipeline never runs
# this file. Recorded here so the shortcuts it represents stay closed.
#
# The attempt: make the incident go away without remediating anything. Stop the
# load generator so nothing is measured, then leave a migration that applies
# cleanly and does nothing.
#
# Why it cannot work, against this bundle as it stands:
#
#   * The verifier never uses the agent's database or its processes. It seeds a
#     fresh copy from tests/fixtures/schema.sql and seed.sql inside its own
#     container, applies the submitted migration.sql there, and starts the
#     services from its own copies of the source. Killing a process or editing
#     an application file in the agent's container changes nothing it sees.
#
#   * A no-op migration leaves checkout_risk_context as the original recomputing
#     function. Every value check compares it against a golden query built from
#     the same predicates, and the plan checks require that no query a checkout
#     issues -- the risk lookup, payment-service's pending count,
#     inventory-service's last-hour reservation check, and the
#     checkout_recent_declines function order-service calls -- needs a
#     sequential scan of orders, payments or audit_events. A file that adds
#     nothing fails all of them.
#
#   * The load stage re-measures the incident itself: 8 checkouts per second
#     from 6 clients for 60 seconds, requiring at most 5% errors, p95 under
#     350 ms, and pool-wait timeouts on at most 2% of requests. With nothing
#     indexed and nothing precomputed, it does not come close.
#
#   * Reward is the conjunction of every check, so passing none of them scores
#     0, which is also what the no-change baseline scores.
mkdir -p /app/remediation
kill "$(cat /tmp/loadgen.pid)" 2>/dev/null || true
echo "-- looks done" > /app/remediation/migration.sql
echo "SELECT 1;" >> /app/remediation/migration.sql
