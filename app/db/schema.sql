-- Esquema SQLite del chatbot de ventas. Idempotente: puede ejecutarse en cada arranque.
-- Las fechas/horas se guardan como texto ISO 8601 en UTC; requested_date como YYYY-MM-DD.

BEGIN;

CREATE TABLE IF NOT EXISTS customers (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL CHECK (length(trim(name)) > 0),
    phone       TEXT    NOT NULL UNIQUE
                        CHECK (phone NOT GLOB '*[^0-9]*' AND length(phone) BETWEEN 11 AND 15),
    active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS products (
    id             INTEGER PRIMARY KEY,
    sku            TEXT    NOT NULL UNIQUE CHECK (length(trim(sku)) > 0),
    name           TEXT    NOT NULL CHECK (length(trim(name)) > 0),
    -- Arreglos JSON de texto. Los valores de allowed_units se validan en Python (kg | bultos).
    aliases        TEXT    NOT NULL DEFAULT '[]'
                           CHECK (json_valid(aliases) AND json_type(aliases) = 'array'),
    allowed_units  TEXT    NOT NULL
                           CHECK (json_valid(allowed_units) AND json_type(allowed_units) = 'array'
                                  AND json_array_length(allowed_units) > 0),
    kg_per_bag     REAL    CHECK (kg_per_bag IS NULL OR kg_per_bag > 0),
    active         INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY,
    folio           TEXT    UNIQUE,  -- lo asigna el backend al confirmar
    customer_id     INTEGER NOT NULL REFERENCES customers(id),
    -- IS (no =): date() devuelve NULL con texto inválido y un CHECK en NULL se considera válido.
    requested_date  TEXT    CHECK (requested_date IS NULL OR date(requested_date) IS requested_date),
    urgency         TEXT    CHECK (urgency IS NULL OR urgency IN ('normal', 'high', 'critical')),
    urgency_note    TEXT,
    notes           TEXT,
    status          TEXT    NOT NULL DEFAULT 'draft'
                            CHECK (status IN ('draft', 'awaiting_confirmation', 'pending_sales',
                                              'cancelled', 'closed')),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    confirmed_at    TEXT,
    -- Un pedido registrado debe tener folio, confirmación, fecha solicitada y urgencia.
    CHECK (status NOT IN ('pending_sales', 'closed')
           OR (folio IS NOT NULL AND confirmed_at IS NOT NULL
               AND requested_date IS NOT NULL AND urgency IS NOT NULL))
);

-- Un solo borrador activo por cliente.
CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_one_active_per_customer
    ON orders(customer_id) WHERE status IN ('draft', 'awaiting_confirmation');
CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status, created_at);

CREATE TABLE IF NOT EXISTS order_items (
    id          INTEGER PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id  INTEGER NOT NULL REFERENCES products(id),
    quantity    REAL    NOT NULL CHECK (quantity > 0),
    unit        TEXT    NOT NULL CHECK (unit IN ('kg', 'bultos')),
    UNIQUE (order_id, product_id, unit)
);

CREATE TABLE IF NOT EXISTS messages (
    id                   INTEGER PRIMARY KEY,
    whatsapp_message_id  TEXT    UNIQUE,  -- NULL permitido (varios); único cuando existe
    direction            TEXT    NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    phone                TEXT    NOT NULL,  -- wa_id tal como lo usa Meta
    text                 TEXT,
    message_type         TEXT    NOT NULL DEFAULT 'text'
                                 CHECK (message_type IN ('text', 'template', 'system', 'unsupported')),
    status               TEXT    NOT NULL DEFAULT 'pending'
                                 CHECK (status IN ('pending', 'processing', 'processed', 'sent', 'failed')),
    attempts             INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at      TEXT,
    last_error           TEXT,
    order_id             INTEGER REFERENCES orders(id),
    created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS ix_messages_queue ON messages(direction, status, next_attempt_at, id);
CREATE INDEX IF NOT EXISTS ix_messages_phone ON messages(phone, id);

PRAGMA user_version = 1;

COMMIT;
