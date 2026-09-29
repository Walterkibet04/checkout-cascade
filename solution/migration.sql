-- INC-2291 remediation.
--
-- Two independent sources of per-request database work on the checkout path:
--
--   1. checkout_risk_context recomputed the customer's lifetime order
--      aggregates and audit-event count on every call. Its orders predicate
--      filters on lower(status), which orders_customer_active_idx (partial on
--      status <> 'canceled') cannot serve, and its audit predicate sits inside
--      an opaque plpgsql function the planner cannot see through, so both fell
--      back to full scans of orders and audit_events.
--
--   2. payment-service counts pending payments synchronously inside every
--      charge, scanning the whole payments table each time.
--
-- Indexing alone removes the scans but leaves the risk lookup proportional to
-- the customer's own history, which is what hurts the largest accounts. The
-- figures are over a trailing three-month window, so a single per-customer total
-- maintained by triggers would be wrong: a row leaves the window when it ages
-- past the window's edge, with no write for any trigger to see. Instead each customer's
-- figures are kept per day in customer_risk_daily. The lookup sums the days
-- strictly inside the window from there, and reads the base rows only for the
-- one day the window's edge falls in -- bounded by one day of history, not all
-- of it.
--
-- Re-runnable: every step is IF NOT EXISTS / OR REPLACE, and the backfill
-- recomputes absolute values rather than applying deltas, so a second run is a
-- no-op rather than a double count.
--
-- Online: orders, payments, and audit_events take live checkout writes
-- throughout, so indexes are built concurrently (top level, outside any
-- transaction block) and no step holds a lock on those tables while bulk work
-- happens. The backfill commits in small batches.

-- Two deploy runners can start this file at the same moment. CREATE TABLE IF
-- NOT EXISTS and CREATE INDEX CONCURRENTLY are not atomic against a second
-- session doing the same thing -- both see the object missing and one then
-- fails on the duplicate name -- so the whole file runs under a session-level
-- advisory lock. The second runner waits and then re-runs everything, which is
-- idempotent. The lock is session level, not transactional, because CREATE
-- INDEX CONCURRENTLY cannot run inside a transaction block; psql releases it
-- when the session ends, including when a run is interrupted.
SELECT pg_advisory_lock(8675309001);


-- One row per customer. Its values are no longer read; the row is the lock that
-- serialises every change to that customer's figures (see customer_risk_lock).
CREATE TABLE IF NOT EXISTS customer_risk_summary (
    customer_id          integer PRIMARY KEY,
    lifetime_orders      bigint NOT NULL DEFAULT 0,
    lifetime_value_cents bigint NOT NULL DEFAULT 0,
    audit_event_count    bigint NOT NULL DEFAULT 0
);

-- Which warehouses a customer used, per day. A distinct count cannot be summed
-- across days the way the other figures can -- a warehouse used on Monday and
-- again on Tuesday is still one warehouse -- so the days are kept separately
-- and the lookup unions them.
CREATE TABLE IF NOT EXISTS customer_risk_warehouse_daily (
    customer_id  integer NOT NULL,
    warehouse_id integer NOT NULL,
    day_no       integer NOT NULL,
    order_count  bigint  NOT NULL DEFAULT 0,
    PRIMARY KEY (customer_id, warehouse_id, day_no)
);

-- A customer's figures per UTC day, day_no = floor(epoch / 86400).
CREATE TABLE IF NOT EXISTS customer_risk_daily (
    customer_id  integer NOT NULL,
    day_no       integer NOT NULL,
    order_count  bigint  NOT NULL DEFAULT 0,
    order_cents  bigint  NOT NULL DEFAULT 0,
    audit_count  bigint  NOT NULL DEFAULT 0,
    PRIMARY KEY (customer_id, day_no)
);

-- A run that was interrupted partway through leaves an invalid index behind
-- (the concurrent build never reached its final phase). CREATE INDEX
-- CONCURRENTLY IF NOT EXISTS would then find a same-named index and skip it,
-- so the index stays unusable and the table keeps being scanned. Clear out
-- only what a previous run left broken, before rebuilding below. \gexec runs
-- each generated statement at top level, which is where DROP INDEX
-- CONCURRENTLY has to run.
SELECT format('DROP INDEX CONCURRENTLY %I;', c.relname)
  FROM pg_index i
  JOIN pg_class c ON c.oid = i.indexrelid
  JOIN pg_class t ON t.oid = i.indrelid
  JOIN pg_namespace n ON n.oid = t.relnamespace
 WHERE n.nspname = 'public'
   AND c.relname IN ('orders_customer_window_idx',
                     'audit_events_customer_canonical_idx',
                     'payments_pending_idx',
                     'orders_warehouse_recent_idx',
                     'payments_failed_recent_idx')
   AND NOT (i.indisvalid AND i.indisready)
\gexec
-- A payload customer_id belongs to customer N only if it is exactly N's
-- canonical text form, which is what the original predicate compared against:
-- '007' is not customer 7.
CREATE OR REPLACE FUNCTION customer_risk_payload_customer(j jsonb)
RETURNS integer
LANGUAGE sql
IMMUTABLE
AS $fn$
    SELECT CASE
             WHEN j->>'customer_id' ~ '^[0-9]{1,9}$'
              AND j->>'customer_id' = ((j->>'customer_id')::integer)::text
             THEN (j->>'customer_id')::integer
           END;
$fn$;


-- Predicates match checkout_risk_context's originals exactly, so a recompute
-- reads only one customer's rows.
CREATE INDEX CONCURRENTLY IF NOT EXISTS orders_customer_window_idx
    ON orders (customer_id, created_at) INCLUDE (total_cents)
    WHERE lower(status) <> 'canceled';

-- Keyed by the customer the payload actually belongs to, not by the payload's
-- text. customer_risk_payload_customer is immutable and returns the customer
-- whose id::text is exactly that string, so this index serves both the lookup
-- and -- because it is ordered by customer id -- a backfill that aggregates a
-- whole range of customers in one scan instead of probing each one.
CREATE INDEX CONCURRENTLY IF NOT EXISTS audit_events_customer_canonical_idx
    ON audit_events (customer_risk_payload_customer(payload), created_at)
    WHERE coalesce(payload->>'action', '') <> 'purged';

-- payment-service's per-charge pending count.
CREATE INDEX CONCURRENTLY IF NOT EXISTS payments_pending_idx
    ON payments (id) WHERE status = 'pending';

-- inventory-service checks how much a warehouse has taken on in the last hour
-- before every reservation. It is in that service's source and nowhere in the
-- schema, and it scans orders on every checkout.
CREATE INDEX CONCURRENTLY IF NOT EXISTS orders_warehouse_recent_idx
    ON orders (warehouse_id, created_at);

-- checkout_recent_declines is called on every checkout and lives in the
-- database, not in any service's source: it joins payments to orders to count
-- a customer's recent declines, and without this it reads all of payments.
CREATE INDEX CONCURRENTLY IF NOT EXISTS payments_failed_recent_idx
    ON payments (created_at) WHERE status = 'failed';

-- Recompute one customer's aggregates from the live tables.
--
-- The row lock is taken first and the recompute runs as a separate statement
-- afterwards. That ordering is what makes concurrent writers and the backfill
-- safe: a session that has to wait for the lock recomputes only once it holds
-- it, from a fresh snapshot that already contains the other transaction's
-- committed rows. Absolute values, so nothing is ever counted twice.
CREATE OR REPLACE FUNCTION customer_risk_lock(p_customer_id integer)
RETURNS void
LANGUAGE plpgsql
AS $fn$
BEGIN
    IF p_customer_id IS NULL THEN
        RETURN;
    END IF;
    INSERT INTO customer_risk_summary AS s (customer_id)
    VALUES (p_customer_id)
    ON CONFLICT (customer_id)
    DO UPDATE SET lifetime_orders = s.lifetime_orders;
END;
$fn$;

-- Add one row's signed contribution to its customer's figures for its day.
-- Constant work per write, whatever the customer's history.
CREATE OR REPLACE FUNCTION customer_risk_apply(p_customer_id integer,
                                               p_at timestamptz,
                                               p_orders bigint,
                                               p_cents bigint,
                                               p_audit bigint)
RETURNS void
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_day integer;
BEGIN
    IF p_customer_id IS NULL OR p_at IS NULL THEN
        RETURN;
    END IF;
    IF p_orders = 0 AND p_cents = 0 AND p_audit = 0 THEN
        RETURN;
    END IF;
    -- Every day is kept. The window's length is a setting that can be widened,
    -- which brings older days back into range, so nothing may be discarded on
    -- the grounds that it currently sits behind the edge.
    v_day := floor(extract(epoch FROM p_at) / 86400)::integer;
    INSERT INTO customer_risk_daily AS d
           (customer_id, day_no, order_count, order_cents, audit_count)
    VALUES (p_customer_id, v_day, p_orders, p_cents, p_audit)
    ON CONFLICT (customer_id, day_no) DO UPDATE
       SET order_count = d.order_count + EXCLUDED.order_count,
           order_cents = d.order_cents + EXCLUDED.order_cents,
           audit_count = d.audit_count + EXCLUDED.audit_count;
END;
$fn$;

CREATE OR REPLACE FUNCTION customer_risk_apply_warehouse(p_customer_id integer,
                                                         p_warehouse_id integer,
                                                         p_at timestamptz,
                                                         p_orders bigint)
RETURNS void
LANGUAGE plpgsql
AS $fn$
BEGIN
    IF p_customer_id IS NULL OR p_warehouse_id IS NULL OR p_at IS NULL OR p_orders = 0 THEN
        RETURN;
    END IF;
    INSERT INTO customer_risk_warehouse_daily AS d
           (customer_id, warehouse_id, day_no, order_count)
    VALUES (p_customer_id, p_warehouse_id,
            floor(extract(epoch FROM p_at) / 86400)::integer, p_orders)
    ON CONFLICT (customer_id, warehouse_id, day_no) DO UPDATE
       SET order_count = d.order_count + EXCLUDED.order_count;
END;
$fn$;

CREATE OR REPLACE FUNCTION customer_risk_orders_trg()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    o_live boolean := false;
    n_live boolean := false;
BEGIN
    IF TG_OP = 'UPDATE'
       AND NEW.customer_id IS NOT DISTINCT FROM OLD.customer_id
       AND NEW.status IS NOT DISTINCT FROM OLD.status
       AND NEW.total_cents IS NOT DISTINCT FROM OLD.total_cents
       AND NEW.warehouse_id IS NOT DISTINCT FROM OLD.warehouse_id
       AND NEW.created_at IS NOT DISTINCT FROM OLD.created_at THEN
        RETURN NULL;
    END IF;

    -- Exactly the contract's predicate, untidied.
    IF TG_OP <> 'INSERT' THEN
        o_live := coalesce(lower(OLD.status) <> 'canceled', false);
    END IF;
    IF TG_OP <> 'DELETE' THEN
        n_live := coalesce(lower(NEW.status) <> 'canceled', false);
    END IF;

    -- Lock every customer this write touches, lower id first, before any
    -- figure changes. That is what keeps a write that lands during the
    -- backfill counted exactly once (see customer_risk_backfill).
    IF TG_OP = 'INSERT' THEN
        PERFORM customer_risk_lock(NEW.customer_id);
    ELSIF TG_OP = 'DELETE' THEN
        PERFORM customer_risk_lock(OLD.customer_id);
    ELSE
        PERFORM customer_risk_lock(least(OLD.customer_id, NEW.customer_id));
        IF NEW.customer_id IS DISTINCT FROM OLD.customer_id THEN
            PERFORM customer_risk_lock(greatest(OLD.customer_id, NEW.customer_id));
        END IF;
    END IF;

    IF o_live THEN
        PERFORM customer_risk_apply(OLD.customer_id, OLD.created_at,
                                    -1, -(OLD.total_cents::bigint), 0);
        PERFORM customer_risk_apply_warehouse(OLD.customer_id, OLD.warehouse_id,
                                              OLD.created_at, -1);
    END IF;
    IF n_live THEN
        PERFORM customer_risk_apply(NEW.customer_id, NEW.created_at,
                                    1, NEW.total_cents::bigint, 0);
        PERFORM customer_risk_apply_warehouse(NEW.customer_id, NEW.warehouse_id,
                                              NEW.created_at, 1);
    END IF;
    RETURN NULL;
END;
$fn$;

CREATE OR REPLACE FUNCTION customer_risk_audit_trg()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    old_cid integer;
    new_cid integer;
    o_live  boolean := false;
    n_live  boolean := false;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.payload IS NOT DISTINCT FROM OLD.payload
       AND NEW.created_at IS NOT DISTINCT FROM OLD.created_at THEN
        RETURN NULL;
    END IF;
    IF TG_OP <> 'INSERT' THEN
        old_cid := customer_risk_payload_customer(OLD.payload);
        o_live  := old_cid IS NOT NULL
                   AND coalesce(OLD.payload->>'action', '') <> 'purged';
    END IF;
    IF TG_OP <> 'DELETE' THEN
        new_cid := customer_risk_payload_customer(NEW.payload);
        n_live  := new_cid IS NOT NULL
                   AND coalesce(NEW.payload->>'action', '') <> 'purged';
    END IF;

    PERFORM customer_risk_lock(least(old_cid, new_cid));
    IF old_cid IS DISTINCT FROM new_cid THEN
        PERFORM customer_risk_lock(greatest(old_cid, new_cid));
    END IF;

    IF o_live THEN
        PERFORM customer_risk_apply(old_cid, OLD.created_at, 0, 0, -1);
    END IF;
    IF n_live THEN
        PERFORM customer_risk_apply(new_cid, NEW.created_at, 0, 0, 1);
    END IF;
    RETURN NULL;
END;
$fn$;

-- ShareRowExclusiveLock, held only for the catalog write. Re-running replaces
-- the trigger in place; a DROP would need AccessExclusive and would stall live
-- writes behind it.
CREATE OR REPLACE TRIGGER customer_risk_orders_aiud
    AFTER INSERT OR UPDATE OR DELETE ON orders
    FOR EACH ROW EXECUTE FUNCTION customer_risk_orders_trg();

CREATE OR REPLACE TRIGGER customer_risk_audit_aiud
    AFTER INSERT OR UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION customer_risk_audit_trg();

-- Backfill. The triggers are already live and committed, so every write from
-- here on maintains its own customer's row; this pass only brings existing
-- customers up to date.
--
-- It runs a range of customers per round rather than one customer per round.
-- A statement per customer would be correct but would cost one planner and
-- executor cycle per customer, which is hundreds of times a single pass over
-- the data at this size. Within a round the same two-step order as the
-- trigger is kept: one statement creates and locks every summary row in the
-- range, and a second statement -- a new statement, so a new snapshot --
-- recomputes them. Anything that committed while the locks were being taken
-- is therefore visible to the recompute, and anything that arrives after is
-- blocked until the round commits and then maintained by the trigger.
CREATE OR REPLACE PROCEDURE customer_risk_backfill()
LANGUAGE plpgsql
AS $fn$
DECLARE
    lo integer := 0;
    hi integer;
BEGIN
    LOOP
        SELECT max(id) INTO hi
          FROM (SELECT id FROM customers WHERE id > lo ORDER BY id LIMIT 2000) q;
        EXIT WHEN hi IS NULL;

        INSERT INTO customer_risk_summary AS s (customer_id)
        SELECT c.id FROM customers c WHERE c.id > lo AND c.id <= hi
        ON CONFLICT (customer_id) DO UPDATE SET lifetime_orders = s.lifetime_orders;

        DELETE FROM customer_risk_daily d
         WHERE d.customer_id > lo AND d.customer_id <= hi;

        -- One range scan per table per batch. Probing each customer in turn is
        -- correct and far too slow at this many customers.
        INSERT INTO customer_risk_daily (customer_id, day_no, order_count, order_cents, audit_count)
        SELECT x.customer_id, x.day_no, sum(x.n_orders), sum(x.v), sum(x.n_audit)
          FROM (
                SELECT o.customer_id,
                       floor(extract(epoch FROM o.created_at) / 86400)::integer AS day_no,
                       1::bigint AS n_orders,
                       o.total_cents::bigint AS v,
                       0::bigint AS n_audit
                  FROM orders o
                 WHERE o.customer_id > lo AND o.customer_id <= hi
                   AND lower(o.status) <> 'canceled'
                UNION ALL
                SELECT customer_risk_payload_customer(a.payload),
                       floor(extract(epoch FROM a.created_at) / 86400)::integer,
                       0::bigint,
                       0::bigint,
                       1::bigint
                  FROM audit_events a
                 WHERE customer_risk_payload_customer(a.payload) > lo
                   AND customer_risk_payload_customer(a.payload) <= hi
                   AND coalesce(a.payload->>'action', '') <> 'purged'
               ) x
         GROUP BY x.customer_id, x.day_no;

        DELETE FROM customer_risk_warehouse_daily d
         WHERE d.customer_id > lo AND d.customer_id <= hi;

        INSERT INTO customer_risk_warehouse_daily (customer_id, warehouse_id, day_no, order_count)
        SELECT o.customer_id, o.warehouse_id,
               floor(extract(epoch FROM o.created_at) / 86400)::integer,
               count(*)
          FROM orders o
         WHERE o.customer_id > lo AND o.customer_id <= hi
           AND lower(o.status) <> 'canceled'
         GROUP BY 1, 2, 3;

        lo := hi;
        COMMIT;
    END LOOP;
END;
$fn$;

CALL customer_risk_backfill();

-- Same signature, same column names, same values. Days strictly after the one
-- the window's edge falls in come from customer_risk_daily; that one edge day
-- is read from the base rows, from the edge onward. Days before it are outside
-- the window entirely. The work is bounded by the window's length in days plus
-- one day of rows, never by the customer's whole history.
CREATE OR REPLACE FUNCTION checkout_risk_context(p_customer_id integer)
RETURNS TABLE (
    customer_id        integer,
    email              text,
    account_tier       text,
    region             text,
    lifetime_orders    bigint,
    lifetime_value_cents bigint,
    audit_event_count  bigint,
    distinct_warehouses bigint
) AS $fn$
    SELECT
        c.id,
        c.email,
        c.account_tier,
        c.region,
        (coalesce((SELECT sum(d.order_count) FROM customer_risk_daily d
                     WHERE d.customer_id = c.id
                       AND d.day_no > floor(extract(epoch FROM now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval) / 86400)::integer), 0)
         + (SELECT count(*) FROM orders o
             WHERE o.customer_id = c.id
               AND lower(o.status) <> 'canceled'
               AND o.created_at >= now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval
               AND o.created_at < to_timestamp(
                     (floor(extract(epoch FROM now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval) / 86400) + 1) * 86400))
        )::bigint,
        (coalesce((SELECT sum(d.order_cents) FROM customer_risk_daily d
                     WHERE d.customer_id = c.id
                       AND d.day_no > floor(extract(epoch FROM now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval) / 86400)::integer), 0)
         + (SELECT coalesce(sum(o.total_cents), 0) FROM orders o
             WHERE o.customer_id = c.id
               AND lower(o.status) <> 'canceled'
               AND o.created_at >= now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval
               AND o.created_at < to_timestamp(
                     (floor(extract(epoch FROM now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval) / 86400) + 1) * 86400))
        )::bigint,
        (coalesce((SELECT sum(d.audit_count) FROM customer_risk_daily d
                     WHERE d.customer_id = c.id
                       AND d.day_no > floor(extract(epoch FROM now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval) / 86400)::integer), 0)
         + (SELECT count(*) FROM audit_events a
             WHERE customer_risk_payload_customer(a.payload) = c.id
               AND coalesce(a.payload->>'action', '') <> 'purged'
               AND a.created_at >= now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval
               AND a.created_at < to_timestamp(
                     (floor(extract(epoch FROM now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval) / 86400) + 1) * 86400))
        )::bigint,
        -- Warehouses are a fixed, small set, so ask of each one whether this
        -- customer used it inside the window: on a whole day from the per-day
        -- rows, or on the edge day from the base rows. Correlated EXISTS
        -- rather than a union in a FROM subquery, which could not reference
        -- c.id from two levels up without LATERAL.
        (SELECT count(*) FROM warehouses wh
          WHERE EXISTS (SELECT 1 FROM customer_risk_warehouse_daily w
                         WHERE w.customer_id = c.id
                           AND w.warehouse_id = wh.id
                           AND w.order_count > 0
                           AND w.day_no > floor(extract(epoch FROM now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval) / 86400)::integer)
             OR EXISTS (SELECT 1 FROM orders o
                         WHERE o.customer_id = c.id
                           AND o.warehouse_id = wh.id
                           AND lower(o.status) <> 'canceled'
                           AND o.created_at >= now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval
                           AND o.created_at < to_timestamp(
                                 (floor(extract(epoch FROM now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval) / 86400)::integer + 1) * 86400)))::bigint
    FROM customers c
    WHERE c.id = p_customer_id;
$fn$ LANGUAGE sql STABLE;

ANALYZE customer_risk_summary;
ANALYZE customer_risk_daily;
ANALYZE customer_risk_warehouse_daily;

SELECT pg_advisory_unlock(8675309001);
