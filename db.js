// =====================================================
// db.js  — Neon Postgres pool + schema migration
// Used by auth.js. Requires env: DATABASE_URL (Neon).
// =====================================================
const { Pool } = require('pg');

const connectionString = process.env.DATABASE_URL || '';
const isLocal = connectionString.includes('localhost') || connectionString.includes('127.0.0.1');

const pool = new Pool({
  connectionString,
  // Neon (and most hosted Postgres) require SSL; local does not.
  ssl: connectionString && !isLocal ? { rejectUnauthorized: false } : false,
  max: 5,
  idleTimeoutMillis: 30000,
});

pool.on('error', (err) => console.error('[db] idle client error:', err.message));

async function q(text, params) {
  return pool.query(text, params);
}

// Create tables if they don't exist. Safe to run repeatedly.
async function migrate() {
  await q(`
    CREATE TABLE IF NOT EXISTS employees (
      id                   SERIAL PRIMARY KEY,
      emp_no               TEXT UNIQUE NOT NULL,
      name                 TEXT NOT NULL,
      role                 TEXT NOT NULL DEFAULT 'pdqc',   -- 'pdqc' | 'sales'
      is_admin             BOOLEAN NOT NULL DEFAULT false,
      active               BOOLEAN NOT NULL DEFAULT true,
      password_hash        TEXT NOT NULL,
      must_change_password BOOLEAN NOT NULL DEFAULT true,
      module_access        JSONB NOT NULL DEFAULT '{}'::jsonb,
      token_version        INTEGER NOT NULL DEFAULT 0,
      created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
    );
  `);
  await q(`
    CREATE TABLE IF NOT EXISTS customers (
      id                   SERIAL PRIMARY KEY,
      company              TEXT NOT NULL,
      contact_name         TEXT,
      code                 TEXT UNIQUE,
      email                TEXT,
      active               BOOLEAN NOT NULL DEFAULT true,
      password_hash        TEXT NOT NULL,
      must_change_password BOOLEAN NOT NULL DEFAULT true,
      token_version        INTEGER NOT NULL DEFAULT 0,
      created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
    );
  `);
  await q(`CREATE INDEX IF NOT EXISTS idx_emp_empno ON employees (lower(emp_no));`);
  await q(`CREATE INDEX IF NOT EXISTS idx_cust_email ON customers (lower(email));`);
  // ensure code column + relaxed email on already-created DBs
  await q(`ALTER TABLE customers ADD COLUMN IF NOT EXISTS code TEXT;`);
  await q(`CREATE UNIQUE INDEX IF NOT EXISTS customers_code_key ON customers (code);`);
  await q(`ALTER TABLE customers ALTER COLUMN email DROP NOT NULL;`);
  await q(`ALTER TABLE customers ADD COLUMN IF NOT EXISTS whatsapp TEXT;`);

  // Customer-portal complaint intake / tracking (brick 3)
  await q(`
    CREATE TABLE IF NOT EXISTS customer_complaints (
      id              SERIAL PRIMARY KEY,
      ref             TEXT UNIQUE,
      customer_id     INTEGER REFERENCES customers(id),
      company         TEXT,
      contact_name    TEXT,
      email           TEXT,
      invoice_no      TEXT, po_no TEXT, so_no TEXT, tc_no TEXT,
      product         TEXT, grade TEXT, qty_affected TEXT,
      description     TEXT NOT NULL,
      photos          JSONB NOT NULL DEFAULT '[]'::jsonb,
      status          TEXT NOT NULL DEFAULT 'submitted',  -- submitted|in_review|declined|resolution_sent|closed
      decision_note   TEXT,
      resolution_note TEXT,
      customer_response TEXT,
      handler_emp     TEXT,
      created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    );
  `);
  await q(`CREATE INDEX IF NOT EXISTS idx_cc_email  ON customer_complaints (lower(email));`);
  await q(`CREATE INDEX IF NOT EXISTS idx_cc_status ON customer_complaints (status);`);
  // extra intake fields (added later)
  await q(`ALTER TABLE customer_complaints ADD COLUMN IF NOT EXISTS filed_by     TEXT;`);
  await q(`ALTER TABLE customer_complaints ADD COLUMN IF NOT EXISTS contact_email TEXT;`);
  await q(`ALTER TABLE customer_complaints ADD COLUMN IF NOT EXISTS mobile       TEXT;`);
  await q(`ALTER TABLE customer_complaints ADD COLUMN IF NOT EXISTS dimensions   TEXT;`);
  await q(`ALTER TABLE customer_complaints ADD COLUMN IF NOT EXISTS batch_no     TEXT;`);
  await q(`ALTER TABLE customer_complaints ADD COLUMN IF NOT EXISTS invoice_date TEXT;`);
  await q(`ALTER TABLE customer_complaints ADD COLUMN IF NOT EXISTS quantity     TEXT;`);

  // ===== HRC Stock module =====
  await q(`
    CREATE TABLE IF NOT EXISTS stock_items (
      id           SERIAL PRIMARY KEY,
      item_code    TEXT UNIQUE NOT NULL,
      item_name    TEXT,
      thickness    NUMERIC,
      width        NUMERIC,
      length       NUMERIC,
      grade        TEXT,
      session_wgt  NUMERIC NOT NULL DEFAULT 0,
      opening_pcs  NUMERIC NOT NULL DEFAULT 0,
      opening_date DATE,
      active       BOOLEAN NOT NULL DEFAULT true,
      sort_order   INTEGER NOT NULL DEFAULT 0,
      created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    );
  `);
  await q(`
    CREATE TABLE IF NOT EXISTS stock_txns (
      id         SERIAL PRIMARY KEY,
      item_id    INTEGER NOT NULL REFERENCES stock_items(id) ON DELETE CASCADE,
      txn_date   DATE NOT NULL,
      txn_type   TEXT NOT NULL,   -- purchase | prod_in | prod_out | sales
      qty        NUMERIC NOT NULL DEFAULT 0,
      doc_no     TEXT,
      entered_by TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
  `);
  await q(`CREATE INDEX IF NOT EXISTS idx_stk_txn_date ON stock_txns (txn_date);`);
  await q(`CREATE INDEX IF NOT EXISTS idx_stk_txn_item ON stock_txns (item_id, txn_date);`);
  await q(`ALTER TABLE stock_txns ADD COLUMN IF NOT EXISTS qty_tons NUMERIC NOT NULL DEFAULT 0;`);
}

module.exports = { pool, q, migrate };
