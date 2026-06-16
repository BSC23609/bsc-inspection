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
      email                TEXT UNIQUE NOT NULL,
      active               BOOLEAN NOT NULL DEFAULT true,
      password_hash        TEXT NOT NULL,
      must_change_password BOOLEAN NOT NULL DEFAULT true,
      token_version        INTEGER NOT NULL DEFAULT 0,
      created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
    );
  `);
  await q(`CREATE INDEX IF NOT EXISTS idx_emp_empno ON employees (lower(emp_no));`);
  await q(`CREATE INDEX IF NOT EXISTS idx_cust_email ON customers (lower(email));`);
}

module.exports = { pool, q, migrate };
