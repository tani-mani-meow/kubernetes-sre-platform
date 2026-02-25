-- Catalog schema. Additive only: new columns and tables, never a rename or drop, so an
-- old catalog pod and a new one can share the database during a rollout.
-- Applied by the seed Job (gitops/services/postgres/base/seed-job.yaml) on every sync;
-- IF NOT EXISTS keeps that idempotent.

CREATE TABLE IF NOT EXISTS products (
    id          SERIAL PRIMARY KEY,
    name        TEXT        NOT NULL,
    category    TEXT        NOT NULL,
    price_cents INTEGER     NOT NULL CHECK (price_cents >= 0),
    stock       INTEGER     NOT NULL CHECK (stock >= 0)
);

CREATE TABLE IF NOT EXISTS orders (
    id          SERIAL PRIMARY KEY,
    product_id  INTEGER     NOT NULL REFERENCES products (id),
    quantity    INTEGER     NOT NULL CHECK (quantity > 0),
    total_cents INTEGER     NOT NULL CHECK (total_cents >= 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The load generator posts orders continuously; without this index the orders table
-- scan grows linearly through a demo and quietly inflates catalog's latency SLI.
CREATE INDEX IF NOT EXISTS orders_product_id_idx ON orders (product_id);
CREATE INDEX IF NOT EXISTS orders_created_at_idx ON orders (created_at DESC);
