// =====================================================
// auth.js — login, JWT cookie sessions, access middleware
// Env: JWT_SECRET (required in prod), SETUP_KEY (for /auth/migrate)
// =====================================================
const express = require('express');
const bcrypt  = require('bcryptjs');
const jwt     = require('jsonwebtoken');
const { q, migrate } = require('./db');

const JWT_SECRET = process.env.JWT_SECRET || 'dev-insecure-change-me';
const SETUP_KEY  = process.env.SETUP_KEY  || '';
const COOKIE     = 'bsc_qms_token';
const TTL_MS     = 8 * 60 * 60 * 1000; // 8h

// ---- Canonical module catalogue (keys used by the frontend too) ----
const MODULE_KEYS = ['inward','ctl','shearing','salesqc','purchaseqc','dash_prod','dash_sales','dash_all'];

function defaultModules(role) {
  if (role === 'sales') {
    return { inward:false, ctl:false, shearing:false, salesqc:true, purchaseqc:false, dash_prod:false, dash_sales:true,  dash_all:false };
  }
  // pdqc (default)
  return   { inward:true,  ctl:true,  shearing:true,  salesqc:true, purchaseqc:true,  dash_prod:true,  dash_sales:false, dash_all:false };
}

// Effective access = role defaults, overlaid by per-user overrides; admins get everything.
function effectiveModules(user) {
  if (user.is_admin) {
    const all = {};
    MODULE_KEYS.forEach(k => all[k] = true);
    return all;
  }
  return Object.assign({}, defaultModules(user.role), user.module_access || {});
}

async function hash(pw)        { return bcrypt.hash(pw, 10); }
async function verify(pw, h)   { return bcrypt.compare(pw, h); }

function signToken(u, kind) {
  return jwt.sign({ sub: u.id, kind, tv: u.token_version || 0 }, JWT_SECRET, { expiresIn: '8h' });
}
function setCookie(res, token) {
  res.cookie(COOKIE, token, { httpOnly:true, secure:true, sameSite:'lax', maxAge:TTL_MS, path:'/' });
}
function clearCookie(res) { res.clearCookie(COOKIE, { path:'/' }); }

async function loadUser(kind, id) {
  const table = kind === 'employee' ? 'employees' : 'customers';
  const r = await q(`SELECT * FROM ${table} WHERE id=$1`, [id]);
  return r.rows[0];
}

// ---- Middleware ----
async function requireAuth(req, res, next) {
  try {
    const t = req.cookies && req.cookies[COOKIE];
    if (!t) return res.status(401).json({ error:'auth_required' });
    const p = jwt.verify(t, JWT_SECRET);
    const u = await loadUser(p.kind, p.sub);
    if (!u || !u.active)               return res.status(401).json({ error:'inactive' });
    if ((u.token_version||0) !== (p.tv||0)) return res.status(401).json({ error:'session_expired' });
    req.user = u;
    req.userKind = p.kind;
    req.modules = p.kind === 'employee' ? effectiveModules(u) : null;
    next();
  } catch (e) {
    return res.status(401).json({ error:'invalid_token' });
  }
}
function requireEmployee(req, res, next) {
  if (req.userKind !== 'employee') return res.status(403).json({ error:'employees_only' });
  next();
}
function requireCustomer(req, res, next) {
  if (req.userKind !== 'customer') return res.status(403).json({ error:'customers_only' });
  next();
}
function requireAdmin(req, res, next) {
  if (req.userKind !== 'employee' || !req.user.is_admin) return res.status(403).json({ error:'admin_only' });
  next();
}
function requireModule(key) {
  return (req, res, next) => {
    if (req.userKind !== 'employee') return res.status(403).json({ error:'forbidden' });
    if (req.user.is_admin || (req.modules && req.modules[key])) return next();
    return res.status(403).json({ error:'module_denied', module:key });
  };
}

// ---- Routes ----
const router = express.Router();

router.post('/login', async (req, res) => {
  try {
    const { kind, identifier, password } = req.body || {};
    if (!identifier || !password) return res.status(400).json({ error:'missing_credentials' });

    if (kind === 'customer') {
      const r = await q('SELECT * FROM customers WHERE lower(email)=lower($1)', [String(identifier).trim()]);
      const u = r.rows[0];
      if (!u || !u.active || !(await verify(password, u.password_hash)))
        return res.status(401).json({ error:'invalid_login' });
      setCookie(res, signToken(u, 'customer'));
      return res.json({ ok:true, kind:'customer', name:u.contact_name || u.company, company:u.company, must_change_password:u.must_change_password });
    }

    const r = await q('SELECT * FROM employees WHERE lower(emp_no)=lower($1)', [String(identifier).trim()]);
    const u = r.rows[0];
    if (!u || !u.active || !(await verify(password, u.password_hash)))
      return res.status(401).json({ error:'invalid_login' });
    setCookie(res, signToken(u, 'employee'));
    return res.json({ ok:true, kind:'employee', name:u.name, role:u.role, is_admin:u.is_admin, modules:effectiveModules(u), must_change_password:u.must_change_password });
  } catch (e) {
    console.error('[auth] login error:', e.message);
    return res.status(500).json({ error:'server_error' });
  }
});

router.post('/logout', (req, res) => { clearCookie(res); res.json({ ok:true }); });

router.get('/me', requireAuth, (req, res) => {
  const u = req.user;
  if (req.userKind === 'customer') {
    return res.json({ kind:'customer', name:u.contact_name || u.company, company:u.company, email:u.email, must_change_password:u.must_change_password });
  }
  res.json({ kind:'employee', name:u.name, emp_no:u.emp_no, role:u.role, is_admin:u.is_admin, modules:effectiveModules(u), must_change_password:u.must_change_password });
});

router.post('/change-password', requireAuth, async (req, res) => {
  try {
    const { current_password, new_password } = req.body || {};
    if (!new_password || String(new_password).length < 6) return res.status(400).json({ error:'weak_password' });
    const u = req.user;
    if (!u.must_change_password) {
      if (!current_password || !(await verify(current_password, u.password_hash)))
        return res.status(401).json({ error:'wrong_current' });
    }
    const h = await hash(new_password);
    const table = req.userKind === 'employee' ? 'employees' : 'customers';
    await q(`UPDATE ${table} SET password_hash=$1, must_change_password=false, token_version=token_version+1 WHERE id=$2`, [h, u.id]);
    const fresh = await loadUser(req.userKind, u.id);
    setCookie(res, signToken(fresh, req.userKind));
    res.json({ ok:true });
  } catch (e) {
    console.error('[auth] change-password error:', e.message);
    res.status(500).json({ error:'server_error' });
  }
});

// One-time setup: create tables + seed first admin. Guarded by SETUP_KEY.
// Visit: /auth/migrate?key=YOUR_SETUP_KEY  (optionally &admin=BSC/ADMIN&pw=Bsc@123)
router.get('/migrate', async (req, res) => {
  if (!SETUP_KEY || req.query.key !== SETUP_KEY) return res.status(403).json({ error:'bad_key' });
  try {
    await migrate();
    const c = await q('SELECT count(*)::int AS n FROM employees');
    let seeded = false;
    if (c.rows[0].n === 0) {
      const empNo = String(req.query.admin || 'BSC/ADMIN').trim();
      const pw    = String(req.query.pw || 'Bsc@123');
      const h     = await hash(pw);
      await q(
        `INSERT INTO employees (emp_no,name,role,is_admin,active,password_hash,must_change_password,module_access)
         VALUES ($1,'Administrator','pdqc',true,true,$2,true,$3)`,
        [empNo, h, JSON.stringify(defaultModules('pdqc'))]
      );
      seeded = { emp_no: empNo, password: pw, note:'change this password on first login' };
    }
    res.json({ ok:true, migrated:true, seeded });
  } catch (e) {
    console.error('[auth] migrate error:', e.message);
    res.status(500).json({ error:'migrate_failed', detail:e.message });
  }
});

module.exports = {
  authRouter: router,
  requireAuth, requireEmployee, requireCustomer, requireAdmin, requireModule,
  effectiveModules, defaultModules, MODULE_KEYS, hash, verify,
};
