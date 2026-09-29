-- Deterministic seed. IDs and arithmetic are part of the functional contract.
--
-- Order and audit timestamps are relative to the moment the seed runs, so the
-- risk function's trailing three-month window (89 to 92 days, depending on
-- the calendar) always cuts through the data. Every
-- ordinary customer's first order and first audit event are between 1 hour and
-- 85 days old; their second ones are between 95 and 145 days old. Nothing is
-- seeded within five days of the window's edge, so no seeded row crosses it
-- during a session of any realistic length.
--
-- The large account (400001) did most of its business in one ten-minute
-- burst, 12:00-12:10 UTC on the day 30 days back: 90% of its orders and
-- audit events sit in that span, which never straddles midnight,
-- 5% are spread over the rest of the window and 5% are older than it.
INSERT INTO warehouses (id, code, region) VALUES
    (1, 'IAD1', 'us-east'),
    (2, 'SFO1', 'us-west'),
    (3, 'DUB1', 'eu-west'),
    (4, 'BOM1', 'ap-south'),
    (5, 'FRA1', 'eu-west');

INSERT INTO products (id, sku, name, category, price_cents)
SELECT
    i,
    'SKU-' || lpad(i::text, 5, '0'),
    'Product ' || i,
    (ARRAY['apparel', 'home', 'electronics', 'grocery'])[1 + (i % 4)],
    499 + (i * 17) % 25000
FROM generate_series(1, 400) AS i;

INSERT INTO inventory (product_id, warehouse_id, qty, updated_at)
SELECT p.id, w.id, 50 + ((p.id * 13 + w.id * 7) % 400), timestamptz '2026-06-01 +00'
FROM products p CROSS JOIN warehouses w;

INSERT INTO customers (id, email, name, account_tier, region, created_at)
SELECT
    i,
    'user' || i || '@shop.example',
    'User ' || i,
    (ARRAY['free', 'pro', 'enterprise'])[1 + (i % 3)],
    (ARRAY['us-east', 'us-west', 'eu-west', 'ap-south'])[1 + (i % 4)],
    timestamptz '2024-01-01 +00' + (i || ' minutes')::interval
FROM generate_series(1, 400000) AS i;

-- Status is a function of order-sequence-within-customer and customer_id.
INSERT INTO orders (id, customer_id, status, total_cents, warehouse_id, payment_ref, created_at)
SELECT
    i,
    1 + ((i - 1) % 400000),
    CASE
        WHEN (i::bigint * 48271) % 2147483647 = 1170185582 THEN 'Canceled'
        ELSE (ARRAY['placed', 'shipped', 'delivered', 'canceled'])
             [1 + ((((i - 1) / 400000) + (1 + ((i - 1) % 400000))) % 4)]
    END,
    800 + ((i * 31) % 40000),
    1 + ((i - 1) % 5),
    'pay_' || i,
    now() - ((CASE WHEN i <= 400000 THEN 3600 + ((i::bigint * 2654435761) % 7344000) ELSE 8208000 + ((i::bigint * 2654435761) % 4320000) END) || ' seconds')::interval
FROM generate_series(1, 800000) AS i;

INSERT INTO order_items (id, order_id, product_id, qty, unit_price_cents)
SELECT
    i,
    1 + ((i - 1) % 120000),
    1 + ((i - 1) % 400),
    1 + ((i - 1) % 3),
    499 + ((i * 17) % 25000)
FROM generate_series(1, 180000) AS i;

-- Five payment attempts recorded per order on average (provider timeouts,
-- card declines, and retries before a captured or still-pending result),
-- not one -- order_id cycles back over the first 120000 orders rather than
-- growing 1:1 with the orders table. amount_cents/created_at are generated directly
-- rather than joined back to orders -- their exact values aren't part of
-- checkout_risk_context's contract or asserted anywhere, and a plain
-- arithmetic INSERT is cheap to build at this size regardless.
INSERT INTO payments (id, order_id, amount_cents, status, provider, provider_ref, created_at)
SELECT
    i,
    1 + ((i - 1) % 120000),
    800 + ((i * 31) % 40000),
    CASE WHEN (i % 17) = 0 THEN 'pending' ELSE 'captured' END,
    CASE WHEN (i % 5) = 0 THEN 'adyen' ELSE 'stripe' END,
    'prov_' || i,
    timestamptz '2024-06-01 +00' + (i || ' seconds')::interval
FROM generate_series(1, 600000) AS i;

-- Four audit rows per customer.
INSERT INTO audit_events (entity_type, entity_id, payload, created_at)
SELECT
    'customer',
    1 + ((i - 1) % 400000),
    jsonb_build_object(
        'customer_id', CASE WHEN (i::bigint * 48271) % 2147483647 = 204765582 THEN '9999' ELSE (1 + ((i - 1) % 400000))::text END,
        'action', (ARRAY['view', 'cart_add', 'checkout_attempt', 'login'])[1 + ((i - 1) % 4)],
        'ip', '10.2.' || ((i / 255) % 255) || '.' || (i % 255)
    ),
    now() - ((CASE WHEN i <= 400000 THEN 3600 + ((i::bigint * 2246822519) % 7344000) ELSE 8208000 + ((i::bigint * 2246822519) % 4320000) END) || ' seconds')::interval
FROM generate_series(1, 800000) AS i;

-- One enterprise account with a very large history. Ordinary customers carry
-- two orders and two audit events; this one carries tens of thousands, so a
-- risk lookup whose cost tracks the customer's own history is visible on it
-- even when it looks fine for everyone else. Its ids sit above the range the
-- load generator uses.
INSERT INTO customers (id, email, name, account_tier, region, created_at)
VALUES (400001, 'ops@globex.example', 'Globex Retail Group', 'enterprise', 'us-east',
        timestamptz '2024-01-01 +00');

INSERT INTO orders (id, customer_id, status, total_cents, warehouse_id, payment_ref, created_at)
SELECT
    800000 + i,
    400001,
    (ARRAY['placed', 'shipped', 'delivered', 'canceled'])[1 + (i % 4)],
    800 + ((i * 31) % 40000),
    1 + (i % 5),
    'pay_e' || i,
    CASE
        WHEN i % 20 = 0 THEN now() - ((8208000 + ((i::bigint * 2654435761) % 4320000)) || ' seconds')::interval
        WHEN i % 10 <> 0 THEN to_timestamp((floor(extract(epoch FROM now()) / 86400) - 30) * 86400
                                             + 43200 + ((i::bigint * 2654435761) % 600))
        ELSE now() - ((3600 + ((i::bigint * 2654435761) % 7344000)) || ' seconds')::interval
    END
FROM generate_series(1, 40000) AS i;

INSERT INTO audit_events (entity_type, entity_id, payload, created_at)
SELECT
    'customer',
    400001,
    jsonb_build_object(
        'customer_id', '400001',
        'action', (ARRAY['view', 'cart_add', 'checkout_attempt', 'login', 'purged'])[1 + (i % 5)],
        'ip', '10.3.' || ((i / 255) % 255) || '.' || (i % 255)
    ),
    CASE
        WHEN i % 20 = 0 THEN now() - ((8208000 + ((i::bigint * 2246822519) % 4320000)) || ' seconds')::interval
        WHEN i % 10 <> 0 THEN to_timestamp((floor(extract(epoch FROM now()) / 86400) - 30) * 86400
                                             + 43200 + ((i::bigint * 2246822519) % 600))
        ELSE now() - ((3600 + ((i::bigint * 2246822519) % 7344000)) || ' seconds')::interval
    END
FROM generate_series(1, 60000) AS i;

-- Production shapes the agent's slice does not carry. instruction.md states
-- that the database this file is applied to holds rows the slice does not,
-- and that the meaning of the graded values is whatever the current code path
-- does with them. Every row below is decided by checkout_risk_context's own
-- predicates -- lower(status) <> 'canceled', (payload->>'customer_id') =
-- id::text, coalesce(payload->>'action','') <> 'purged' -- so a remediation
-- that reproduces those predicates is right here by construction, and one
-- that tidies them up (trim, a spelling list, casting the payload id to an
-- integer, lowercasing the action, jsonb containment) diverges on exactly
-- these customers and nowhere else.
INSERT INTO orders (id, customer_id, status, total_cents, warehouse_id, payment_ref, created_at) VALUES
    -- lower('Cancelled') is not 'canceled': a British spelling is an active order
    (900001, 390001, 'Cancelled', 12900, 1, 'pay_p1', now() - interval '11 days'),
    -- lower(' canceled') is not 'canceled' either: the predicate does not trim
    (900002, 390002, ' canceled', 8400, 2, 'pay_p2', now() - interval '12 days'),
    -- lower('CANCELED') is 'canceled': this one is excluded
    (900003, 390003, 'CANCELED', 7700, 3, 'pay_p3', now() - interval '13 days');

INSERT INTO audit_events (entity_type, entity_id, payload, created_at) VALUES
    -- customer_id as a JSON number: ->> still yields '390004', so it counts
    ('customer', 390004, jsonb_build_object('customer_id', 390004, 'action', 'view'),
     now() - interval '14 days'),
    -- 'Purged' is not 'purged': the exclusion is case-sensitive, so it counts
    ('customer', 390005, jsonb_build_object('customer_id', '390005', 'action', 'Purged'),
     now() - interval '15 days'),
    -- '0390006' is not '390006': the comparison is on text, not on an integer
    ('customer', 390006, jsonb_build_object('customer_id', '0390006', 'action', 'view'),
     now() - interval '16 days'),
    -- no action key at all: coalesce makes it '', which is not 'purged'
    ('customer', 390007, jsonb_build_object('customer_id', '390007'),
     now() - interval '17 days'),
    -- a customer_id belonging to no customer row at all
    ('customer', 390008, jsonb_build_object('customer_id', '999999999', 'action', 'view'),
     now() - interval '18 days');

-- An account with no orders and no audit events: the function returns zeros
-- for it, not no row at all.
INSERT INTO customers (id, email, name, account_tier, region, created_at)
VALUES (400002, 'quiet@shop.example', 'Quiet Account', 'free', 'eu-west',
        now() - interval '19 days');

VACUUM (ANALYZE);
