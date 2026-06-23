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
const TTL_MS     = 365 * 24 * 60 * 60 * 1000; // 1 year — stay signed in on the device

// ---- Canonical module catalogue (keys used by the frontend too) ----
const MODULE_KEYS = ['inward','ctl','shearing','salesqc','purchaseqc','dash_prod','dash_sales','dash_all'];

// Catalogue with labels/groups — sent to the Users admin UI to render switches.
const MODULE_META = [
  { key:'inward',     label:'Coil Inward',         group:'Inspections' },
  { key:'ctl',        label:'CTL Quality',         group:'Inspections' },
  { key:'shearing',   label:'Shearing Quality',    group:'Inspections' },
  { key:'salesqc',    label:'Sales QC',            group:'Complaints'  },
  { key:'purchaseqc', label:'Purchase QC',         group:'Complaints'  },
  { key:'dash_prod',  label:'Pending — Production', group:'Dashboards' },
  { key:'dash_sales', label:'Pending — Sales',      group:'Dashboards' },
  { key:'dash_all',   label:'Overall Dashboard',    group:'Dashboards' },
];

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
  return jwt.sign({ sub: u.id, kind, tv: u.token_version || 0 }, JWT_SECRET, { expiresIn: '365d' });
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
      const r = await q('SELECT * FROM customers WHERE lower(code)=lower($1) OR lower(email)=lower($1)', [String(identifier).trim()]);
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
    return res.json({ kind:'customer', code:u.code, name:u.contact_name || u.company, company:u.company, email:u.email, must_change_password:u.must_change_password });
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

// ---- Admin: user management (brick 2b) ----
router.get('/modules', requireAuth, (req, res) => res.json(MODULE_META));

router.get('/users', requireAuth, requireAdmin, async (req, res) => {
  try {
    const r = await q(`SELECT id, emp_no, name, role, is_admin, active, module_access, must_change_password, created_at
                       FROM employees ORDER BY created_at ASC`);
    res.json(r.rows);
  } catch (e) { console.error('[auth] list users:', e.message); res.status(500).json({ error:'server_error' }); }
});

router.post('/users', requireAuth, requireAdmin, async (req, res) => {
  try {
    let { emp_no, name, role, is_admin, active, password, module_access } = req.body || {};
    if (!emp_no || !name || !password) return res.status(400).json({ error:'missing_fields' });
    role = role === 'sales' ? 'sales' : 'pdqc';
    const mods = Object.assign({}, defaultModules(role), module_access || {});
    const h = await hash(password);
    const r = await q(
      `INSERT INTO employees (emp_no,name,role,is_admin,active,password_hash,must_change_password,module_access)
       VALUES ($1,$2,$3,$4,$5,$6,true,$7) RETURNING id`,
      [String(emp_no).trim(), String(name).trim(), role, !!is_admin, active !== false, h, JSON.stringify(mods)]
    );
    res.json({ ok:true, id:r.rows[0].id });
  } catch (e) {
    if (e.code === '23505') return res.status(409).json({ error:'emp_no_taken' });
    console.error('[auth] create user:', e.message); res.status(500).json({ error:'server_error' });
  }
});

router.patch('/users/:id', requireAuth, requireAdmin, async (req, res) => {
  try {
    const id = parseInt(req.params.id, 10);
    const { name, role, is_admin, active, module_access } = req.body || {};
    const cur = await q('SELECT * FROM employees WHERE id=$1', [id]);
    if (!cur.rows[0]) return res.status(404).json({ error:'not_found' });
    const u = cur.rows[0];
    // Prevent an admin from locking themselves out
    if (id === req.user.id && ((is_admin === false) || (active === false)))
      return res.status(400).json({ error:'self_lockout' });
    const newRole = role === undefined ? u.role : (role === 'sales' ? 'sales' : 'pdqc');
    const mods    = module_access !== undefined ? module_access : u.module_access;
    await q(
      `UPDATE employees SET name=$1, role=$2, is_admin=$3, active=$4, module_access=$5, token_version=token_version+1 WHERE id=$6`,
      [name == null ? u.name : name, newRole,
       is_admin === undefined ? u.is_admin : !!is_admin,
       active === undefined ? u.active : !!active,
       JSON.stringify(mods), id]
    );
    res.json({ ok:true });
  } catch (e) { console.error('[auth] update user:', e.message); res.status(500).json({ error:'server_error' }); }
});

router.post('/users/:id/reset-password', requireAuth, requireAdmin, async (req, res) => {
  try {
    const id = parseInt(req.params.id, 10);
    const pw = (req.body && req.body.password) || 'Bsc@123';
    const h  = await hash(pw);
    const r = await q('UPDATE employees SET password_hash=$1, must_change_password=true, token_version=token_version+1 WHERE id=$2 RETURNING emp_no', [h, id]);
    if (!r.rows[0]) return res.status(404).json({ error:'not_found' });
    res.json({ ok:true, password:pw });
  } catch (e) { console.error('[auth] reset pw:', e.message); res.status(500).json({ error:'server_error' }); }
});

// ---- Admin: customer account management (brick 3) ----
router.get('/customers', requireAuth, requireAdmin, async (req, res) => {
  try {
    const r = await q(`SELECT id, code, company, contact_name, email, whatsapp, active, must_change_password, created_at
                       FROM customers ORDER BY created_at ASC`);
    res.json(r.rows);
  } catch (e) { console.error('[auth] list customers:', e.message); res.status(500).json({ error:'server_error' }); }
});

router.post('/customers', requireAuth, requireAdmin, async (req, res) => {
  try {
    const { code, company, contact_name, email, whatsapp, password, active } = req.body || {};
    if (!code || !company || !password) return res.status(400).json({ error:'missing_fields' });
    const h = await hash(password);
    const r = await q(
      `INSERT INTO customers (code, company, contact_name, email, whatsapp, active, password_hash, must_change_password)
       VALUES ($1,$2,$3,$4,$5,$6,$7,true) RETURNING id`,
      [String(code).trim(), String(company).trim(), (contact_name||'').trim(),
       email ? String(email).trim().toLowerCase() : null,
       whatsapp ? String(whatsapp).replace(/[^0-9]/g,'') : null, active !== false, h]
    );
    res.json({ ok:true, id:r.rows[0].id });
  } catch (e) {
    if (e.code === '23505') return res.status(409).json({ error:'code_taken' });
    console.error('[auth] create customer:', e.message); res.status(500).json({ error:'server_error' });
  }
});

router.patch('/customers/:id', requireAuth, requireAdmin, async (req, res) => {
  try {
    const id = parseInt(req.params.id, 10);
    const { company, contact_name, email, whatsapp, active } = req.body || {};
    const cur = await q('SELECT * FROM customers WHERE id=$1', [id]);
    if (!cur.rows[0]) return res.status(404).json({ error:'not_found' });
    const u = cur.rows[0];
    const newEmail = email === undefined ? u.email : (email ? String(email).trim().toLowerCase() : null);
    const newWa = whatsapp === undefined ? u.whatsapp : (whatsapp ? String(whatsapp).replace(/[^0-9]/g,'') : null);
    await q(`UPDATE customers SET company=$1, contact_name=$2, email=$3, whatsapp=$4, active=$5, token_version=token_version+1 WHERE id=$6`,
      [company == null ? u.company : company, contact_name == null ? u.contact_name : contact_name,
       newEmail, newWa, active === undefined ? u.active : !!active, id]);
    res.json({ ok:true });
  } catch (e) { console.error('[auth] update customer:', e.message); res.status(500).json({ error:'server_error' }); }
});

router.post('/customers/:id/reset-password', requireAuth, requireAdmin, async (req, res) => {
  try {
    const id = parseInt(req.params.id, 10);
    const pw = (req.body && req.body.password) || 'Welcome@123';
    const h  = await hash(pw);
    const r = await q('UPDATE customers SET password_hash=$1, must_change_password=true, token_version=token_version+1 WHERE id=$2 RETURNING email', [h, id]);
    if (!r.rows[0]) return res.status(404).json({ error:'not_found' });
    res.json({ ok:true, password:pw });
  } catch (e) { console.error('[auth] reset customer pw:', e.message); res.status(500).json({ error:'server_error' }); }
});

// ---- One-shot bulk import of the BSC employee roster ----
// Department -> role: Production=pdqc, Sales=sales, Management=sales(+admin).
// "App Role" containing Admin -> is_admin. Dispatch staff are intentionally excluded.
const STAFF_ROSTER = [
  // [emp_no, name, role, is_admin]
  ['CMD',     'GOVERDHAN AGARWAL',     'sales', true],
  ['CEO',     'GOURAV SARAF',          'sales', true],
  ['BSC/017', 'SHIVAM Shroff',         'sales', true],
  ['BSC/119', 'Jeevabharathy S',       'sales', true],
  ['BSC/098', 'Kannan K',              'pdqc',  true],
  ['BSC/005', 'PARAMAGURU S',          'sales', false],
  ['BSC/012', 'VENKATESH PRASAD DOBA', 'sales', false],
  ['BSC/102', 'VijayaLakshmi k',       'sales', false],
  ['BSC/110', 'Aarthi S',              'sales', false],
  ['BSC/111', 'Archana A',             'sales', false],
  ['BSC/120', 'Varsha K',              'sales', false],
  ['BSC/130', 'Uma BalaSubramani',     'sales', false],
  ['BSC/039', 'Ragupathi C',           'pdqc',  false],
  ['BSC/083', 'G YUVARAJ',             'pdqc',  false],
  ['BSC/084', 'Velu C',                'pdqc',  false],
  ['BSC/090', 'Surajit Sasanka Maity', 'pdqc',  false],
  ['BSC/093', 'Pappu Kumar',           'pdqc',  false],
  ['BSC/103', 'Primith P',             'pdqc',  false],
  ['BSC/109', 'Vignesh A',             'pdqc',  false],
  ['BSC/118', 'Mathan M',              'pdqc',  false],
  ['BSC/134', 'Kumar Balakrishnan',    'pdqc',  false],
  ['BSC/141', 'Amulraj D',             'pdqc',  false],
  ['BSC/146', 'ISAAC G',               'pdqc',  false],
  ['BSC/147', 'Mohan K',               'pdqc',  false],
  ['BSC/151', 'DHINAKRAN K',           'pdqc',  false],
  ['BSC/154', 'Charumathi N',          'pdqc',  false],
  ['BSC/158', 'R V STALIN',            'pdqc',  false],
  // Dispatch — accounts only, no QMS modules by default (grant per-person in Users)
  ['BSC/008', 'SATHYA D',              'pdqc',  false, 'none'],
  ['BSC/013', 'MAHENDRAN RAMDAS',      'pdqc',  false, 'none'],
  ['BSC/019', 'KUMAR N',               'pdqc',  false, 'none'],
  ['BSC/028', 'JEGAN',                 'pdqc',  false, 'none'],
  ['BSC/029', 'SOORIYARAJ L',          'pdqc',  false, 'none'],
  ['BSC/156', 'Sam Kumar',             'pdqc',  false, 'none'],
  ['BSC/157', 'M Dhanush',             'pdqc',  false, 'none'],
];

router.get('/seed-employees', async (req, res) => {
  if (!SETUP_KEY || req.query.key !== SETUP_KEY) return res.status(403).json({ error:'bad_key' });
  try {
    await migrate();
    const pw = String(req.query.pw || 'Bsc@2026');
    const h  = await hash(pw);
    let created = 0, updated = 0;
    for (const [emp_no, name, role, is_admin, mods] of STAFF_ROSTER) {
      const modAccess = mods === 'none'
        ? MODULE_KEYS.reduce((o,k)=>{ o[k]=false; return o; },{})
        : defaultModules(role);
      const r = await q(
        `INSERT INTO employees (emp_no,name,role,is_admin,active,password_hash,must_change_password,module_access)
         VALUES ($1,$2,$3,$4,true,$5,true,$6)
         ON CONFLICT (emp_no) DO UPDATE SET name=EXCLUDED.name, role=EXCLUDED.role, is_admin=EXCLUDED.is_admin
         RETURNING (xmax = 0) AS inserted`,
        [emp_no, name, role, is_admin, h, JSON.stringify(modAccess)]
      );
      if (r.rows[0] && r.rows[0].inserted) created++; else updated++;
    }
    res.json({
      ok: true, total: STAFF_ROSTER.length, created, updated,
      temp_password: created ? pw : undefined,
      note: created
        ? 'New accounts use this temp password; each is forced to set their own on first login. Existing accounts kept their password — only name/role/admin were refreshed.'
        : 'All accounts already existed; mappings refreshed, passwords untouched.'
    });
  } catch (e) {
    console.error('[auth] seed error:', e.message);
    res.status(500).json({ error:'seed_failed', detail:e.message });
  }
});

// ---- Admin: remove employee / customer ----
router.delete('/users/:id', requireAuth, requireAdmin, async (req, res) => {
  try {
    const id = parseInt(req.params.id, 10);
    if (id === req.user.id) return res.status(400).json({ error:'self_delete' });
    const r = await q('DELETE FROM employees WHERE id=$1 RETURNING emp_no', [id]);
    if (!r.rows[0]) return res.status(404).json({ error:'not_found' });
    res.json({ ok:true });
  } catch (e) { console.error('[auth] delete user:', e.message); res.status(500).json({ error:'server_error' }); }
});
router.delete('/customers/:id', requireAuth, requireAdmin, async (req, res) => {
  try {
    const id = parseInt(req.params.id, 10);
    const r = await q('DELETE FROM customers WHERE id=$1 RETURNING email', [id]);
    if (!r.rows[0]) return res.status(404).json({ error:'not_found' });
    res.json({ ok:true });
  } catch (e) {
    if (e.code === '23503') return res.status(409).json({ error:'has_records' });
    console.error('[auth] delete customer:', e.message); res.status(500).json({ error:'server_error' });
  }
});

// ---- One-shot bulk import of the complaint-portal accounts (Vendor IDs) ----
const CUSTOMER_ROSTER = [
  ['7204547','ARC FABS'],['7200465','BALAJI FAB'],['7206172','BELRISE'],['7200232','CHIRAKKAL'],
  ['7203923','DALI & SAMIR'],['7200489','DIAMOND'],['7200490','DIG VIJAY'],['7200394','ELKAYAM'],
  ['7200030','ESWARI AUOT'],['7200401','HEGDE'],['7201061','KAILASH VAHN PRIVATE LTD.'],['7200186','KLN ENGG'],
  ['7201003','KUMAR INDUS'],['7200115','KUMAR INDUS'],['7200346','METAL FORMS'],['7200558','NEEL INDUS'],
  ['7204981','PRABHA 3'],['7205022','PRABHA Chennai'],['7204931','PRAVEEN ENGG'],['7203916','QUALITECH'],
  ['7205097','RV INDUS'],['7200733','SASCO ENGG'],['7205001','SREE DEVI ENGG'],['7200520','STS MFG'],
  ['7205460','SURIN'],['7206247','VJS AUTO'],['7201121','ALBONAIR'],['7205457','JAI SUSPENSION'],
  ['7204101','JAMNA'],['7204803','ALF Eng'],['7441130','Space AGE'],['7201296','Schwing setter'],
];
router.get('/seed-customers', async (req, res) => {
  if (!SETUP_KEY || req.query.key !== SETUP_KEY) return res.status(403).json({ error:'bad_key' });
  try {
    await migrate();
    const pw = String(req.query.pw || 'Bsc@2026');
    const h  = await hash(pw);
    let created = 0, updated = 0;
    for (const [code, company] of CUSTOMER_ROSTER) {
      const r = await q(
        `INSERT INTO customers (code, company, contact_name, email, active, password_hash, must_change_password)
         VALUES ($1,$2,'',NULL,true,$3,true)
         ON CONFLICT (code) DO UPDATE SET company=EXCLUDED.company
         RETURNING (xmax = 0) AS inserted`,
        [code, company, h]
      );
      if (r.rows[0] && r.rows[0].inserted) created++; else updated++;
    }
    res.json({ ok:true, total:CUSTOMER_ROSTER.length, created, updated,
      default_password: created ? pw : undefined,
      note: created ? 'New accounts use this default password; each is asked to set their own on first login.' : 'All accounts already existed; names refreshed, passwords untouched.' });
  } catch (e) { console.error('[auth] seed customers:', e.message); res.status(500).json({ error:'seed_failed', detail:e.message }); }
});

module.exports = {
  authRouter: router,
  requireAuth, requireEmployee, requireCustomer, requireAdmin, requireModule,
  effectiveModules, defaultModules, MODULE_KEYS, MODULE_META, hash, verify,
};
