Checkout at Meridian is broken. POST /api/checkout through the gateway on port 8080 is seeing over 20% errors and multi-second p95 latency. Catalog reads are fine.

You are already on a live slice of the path taking traffic:

gateway :8080
order-service :8081
inventory :8082
payments :8083
postgres :5432 (user, password, and database are all checkout)

Service source is in `/app/src`. Telemetry is in `/app/telemetry`; `/app/telemetry/incident_window` contains a copy of the first minute of the incident. `/app/runbooks` contains last quarter's on-call notes.

Write `/app/remediation/migration.sql`. Do not modify the application source or any other file.

For grading, the file is applied with `psql -v ON_ERROR_STOP=1 -f` as the `checkout` role, four times in a row against a freshly seeded copy of the database -- twice interrupted partway through, then twice to completion -- while checkout writes run against `orders` and `audit_events` throughout all four runs, and checkout traffic is then run through the gateway against that copy. In production it is applied while checkout traffic continues normally. More than one instance of each service may be running at the same time, and two of them can write rows belonging to the same customer simultaneously. All of those instances share the same PostgreSQL database.

The database you have is a slice, not the database this file will be applied to. That one carries customers and rows yours does not, including statuses and payloads that do not look like anything in your slice. What those rows mean is whatever the code path in front of you does with them today, so derive your remediation from that code rather than from the values you can see.

The existing checkout behavior must remain unchanged from a customer's point of view. In particular, the customer-risk information used by checkout must keep exactly the meaning the code that produces it today gives it, including every filter that code applies.

Those values must be correct for existing data, and must stay correct as orders, payments, and audit events are created or changed, and as time passes. The window's length is an operational setting that Risk changes without a deploy, and the figures must follow the current setting immediately.

Do not assume that historical rows are immutable. An order may change state after it is created, including becoming canceled or becoming non-canceled again. Relevant monetary values may also be corrected. Audit events may be inserted, removed, or have their customer association changed. More generally, any column the risk figures depend on may be corrected after the row is written, and corrections arrive in bulk as well as one row at a time -- a single statement may change thousands of rows. New customers are created continuously too; one created after the migration must have correct figures from its first order onwards.

The migration must establish a correct starting state, and every write that lands while it runs must end up reflected exactly once: not lost, and not counted twice.

When you are done, none of the database queries a checkout request issues, including those inside functions it calls, may need a sequential scan of `orders`, `payments`, or `audit_events`. Investigate the actual checkout path and determine which database operations are responsible for the incident.

Measured directly against the database, the risk lookup must return in under 5 ms median for every customer in this database, not just a typical one. Writes must not get slower for customers with more history either: a single-row write to `orders` or `audit_events` for any customer in this database must take no more than twice as long, median, as the same write for a customer with a typical history, or 0.6 ms, whichever is longer. Writes are timed one statement at a time, excluding commit.

This database is not small, and the migration has a cost budget. A complete run of the file must finish within 40 times the time a single aggregate pass over `orders` and `audit_events` takes -- one pass that reads both tables and groups them by customer. That baseline is measured on the same machine immediately before your file is applied, so the budget is a ratio rather than a stopwatch reading.

Do not change the application queries to accomplish this; the application must keep using its existing database interface.

The database remains writable throughout, and live writes must not be held up for long while the file runs: 99% of them must finish within 1.5 seconds. Do not take the system offline, stop traffic, disable retries, or rely on another service instance coordinating the migration. Any index you add to `orders`, `payments`, or `audit_events` must be built with `CREATE INDEX CONCURRENTLY`. Outside SQL comments, the file must not contain a `CREATE INDEX` on those tables without `CONCURRENTLY` anywhere, including DO blocks, function bodies, fallback paths, and SQL assembled from string literals.

The migration is a deploy step and may be interrupted and retried. It must therefore be safe to execute more than once. A second complete execution must succeed without requiring manual cleanup and must leave the database in the same logical state as the first successful execution. An interrupted execution can leave half-finished objects behind; after a later complete execution the database must be in the same correct state an uninterrupted run would have produced, with nothing left behind in an unusable condition. Six deploy runners may also start the file at the same moment: all six must finish without error, and the result must be the state a single complete run produces.

Do not TRUNCATE or DELETE FROM `customers`, `orders`, `order_items`, `payments`, or `audit_events`, and do not add rows to them. Do not DROP those tables or the `public` schema.

Do not solve the incident by removing customer-risk information, weakening its semantics, stopping checkout traffic, or bypassing the affected database work.

The fix is judged at the runbook's offered load: 8 checkouts per second from 6 concurrent clients, each for a random customer, for 60 seconds through the gateway. Under that load, checkout must stay at or below 5% errors with p95 under 350 ms, and order-service must report pool-wait timeouts for no more than 2% of requests. Measured on its own, the risk-context lookup checkout makes on every request must answer in under 40 ms median and 80 ms p95.

You have 9000 seconds to complete this task. Do not cheat by using online solutions or hints specific to this task.
