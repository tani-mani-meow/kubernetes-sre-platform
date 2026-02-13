-- Demo data. Idempotent: the seed Job re-runs on every Argo sync.
--
-- Stock is deliberately huge. The load generator posts roughly 0.4 orders/second
-- continuously, so realistic stock levels would drain inside an afternoon and the
-- service would start returning 409s - which look like a broken service on a dashboard
-- and would pollute every incident timeline captured afterwards.

INSERT INTO products (id, name, category, price_cents, stock) VALUES
    (1,  'Mechanical Keyboard',   'peripherals', 12900, 1000000),
    (2,  '27-inch Monitor',       'displays',    31900, 1000000),
    (3,  'USB-C Dock',            'peripherals', 18900, 1000000),
    (4,  'Noise-cancelling Headset', 'audio',    24900, 1000000),
    (5,  'Ergonomic Mouse',       'peripherals',  7900, 1000000),
    (6,  'Laptop Stand',          'accessories',  5100, 1000000),
    (7,  '4K Webcam',             'video',       13900, 1000000),
    (8,  'Desk Microphone',       'audio',       10900, 1000000),
    (9,  'Cable Management Kit',  'accessories',  2900, 1000000),
    (10, 'Portable SSD 2TB',      'storage',     17900, 1000000),
    (11, 'KVM Switch',            'peripherals', 14900, 1000000),
    (12, 'Monitor Light Bar',     'accessories',  8900, 1000000)
ON CONFLICT (id) DO UPDATE
    SET name        = EXCLUDED.name,
        category    = EXCLUDED.category,
        price_cents = EXCLUDED.price_cents,
        -- Restock on re-seed so a long-running cluster can be topped up with
        -- `kubectl delete job` + resync rather than a database rebuild.
        stock       = EXCLUDED.stock;

-- SERIAL keeps its own counter, which the explicit ids above bypass. Without this the
-- first INSERT without an id collides on the primary key.
SELECT setval(
    pg_get_serial_sequence('products', 'id'),
    GREATEST((SELECT MAX(id) FROM products), 1)
);
