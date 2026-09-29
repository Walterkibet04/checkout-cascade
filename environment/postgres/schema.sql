CREATE TABLE customers (
    id            integer PRIMARY KEY,
    email         text NOT NULL,
    name          text NOT NULL,
    account_tier  text NOT NULL,
    region        text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE products (
    id          integer PRIMARY KEY,
    sku         text NOT NULL UNIQUE,
    name        text NOT NULL,
    category    text NOT NULL,
    price_cents integer NOT NULL
);

CREATE TABLE warehouses (
    id       integer PRIMARY KEY,
    code     text NOT NULL UNIQUE,
    region   text NOT NULL
);

CREATE TABLE inventory (
    product_id   integer NOT NULL REFERENCES products(id),
    warehouse_id integer NOT NULL REFERENCES warehouses(id),
    qty          integer NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (product_id, warehouse_id)
);

CREATE TABLE orders (
    id           integer PRIMARY KEY,
    customer_id  integer NOT NULL REFERENCES customers(id),
    status       text NOT NULL,
    total_cents  integer NOT NULL,
    warehouse_id integer NOT NULL REFERENCES warehouses(id),
    payment_ref  text,
    created_at   timestamptz NOT NULL
);

-- INC-2214 follow-up.
CREATE INDEX orders_customer_active_idx ON orders (customer_id) WHERE status <> 'canceled';

CREATE TABLE order_items (
    id               integer PRIMARY KEY,
    order_id         integer NOT NULL REFERENCES orders(id),
    product_id       integer NOT NULL REFERENCES products(id),
    qty              integer NOT NULL,
    unit_price_cents integer NOT NULL
);

CREATE TABLE payments (
    id           integer PRIMARY KEY,
    order_id     integer NOT NULL REFERENCES orders(id),
    amount_cents integer NOT NULL,
    status       text NOT NULL,
    provider     text NOT NULL,
    provider_ref text,
    created_at   timestamptz NOT NULL
);

CREATE TABLE audit_events (
    id          bigserial PRIMARY KEY,
    entity_type text NOT NULL,
    entity_id   integer NOT NULL,
    payload     jsonb NOT NULL,
    created_at  timestamptz NOT NULL
);

-- RISK-114 made the risk figures cover a trailing window. Its length is an
-- operational setting: Risk widens or narrows it without a deploy.
CREATE TABLE risk_settings (
    key   text PRIMARY KEY,
    value text NOT NULL
);
INSERT INTO risk_settings (key, value) VALUES ('risk_window', '3 months');

CREATE OR REPLACE FUNCTION audit_payload_belongs_to(j jsonb, p_customer_id integer)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
AS $$
BEGIN
    RETURN (j->>'customer_id') = p_customer_id::text
       AND coalesce(j->>'action', '') <> 'purged';
END;
$$;

-- RISK-151: checkout refuses to place a new order for a customer whose recent
-- payments keep being declined. The check lives in the database so that the
-- other services can call it too.
CREATE OR REPLACE FUNCTION checkout_recent_declines(p_customer_id integer)
RETURNS bigint
LANGUAGE sql
STABLE
AS $$
    SELECT count(*)
      FROM payments p
      JOIN orders o ON o.id = p.order_id
     WHERE o.customer_id = p_customer_id
       AND p.status = 'failed'
       AND p.created_at >= now() - interval '1 day';
$$;

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
) AS $$
    -- The lifetime_* column names predate RISK-114, which narrowed every one
    -- of these figures to the trailing window named in risk_settings. The names were kept so
    -- that callers did not have to change.
    SELECT
        c.id,
        c.email,
        c.account_tier,
        c.region,
        (SELECT COUNT(*) FROM orders o
          WHERE o.customer_id = c.id AND lower(o.status) <> 'canceled'
            AND o.created_at >= now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval),
        (SELECT COALESCE(SUM(o.total_cents), 0) FROM orders o
          WHERE o.customer_id = c.id AND lower(o.status) <> 'canceled'
            AND o.created_at >= now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval),
        (SELECT COUNT(*) FROM audit_events a
          WHERE audit_payload_belongs_to(a.payload, p_customer_id)
            AND a.created_at >= now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval),
        -- RISK-140: how many warehouses the customer has drawn on in the window.
        (SELECT COUNT(DISTINCT o.warehouse_id) FROM orders o
          WHERE o.customer_id = c.id AND lower(o.status) <> 'canceled'
            AND o.created_at >= now() - (SELECT value FROM risk_settings WHERE key = 'risk_window')::interval)
    FROM customers c
    WHERE c.id = p_customer_id;
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION reconcile_provider_payments(p_since timestamptz)
RETURNS TABLE (payment_id integer, provider_ref text, status text) AS $$
    SELECT p.id, p.provider_ref, p.status
    FROM payments p
    WHERE p.created_at >= p_since
      AND p.provider_ref IS NOT NULL
      AND p.status IN ('pending', 'unknown')
    ORDER BY p.created_at;
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION customer_affinity_preview(p_customer_id integer)
RETURNS TABLE (other_id integer, shared_skus bigint) AS $$
    SELECT oi2.product_id, COUNT(*)::bigint
    FROM orders o1
    JOIN order_items oi1 ON oi1.order_id = o1.id
    JOIN order_items oi2 ON oi2.product_id = oi1.product_id
    JOIN orders o2 ON o2.id = oi2.order_id
    WHERE o1.customer_id = p_customer_id
      AND o2.customer_id <> p_customer_id
    GROUP BY oi2.product_id
    ORDER BY 2 DESC
    LIMIT 5;
$$ LANGUAGE sql STABLE;

CREATE INDEX payments_created_at_idx ON payments (created_at);
CREATE INDEX products_sku_idx ON products (sku);
CREATE UNIQUE INDEX customers_email_idx ON customers (email);
