const express = require('express');
const crypto = require('crypto');
const { q } = require('../lib/db');
const auth = require('../lib/auth');
const { downtimeMins, elapsedLabel } = require('../lib/util');
const excel = require('../lib/excel');
const wati = require('../lib/wati');
const { background } = require('../lib/bg');
const router = express.Router();

router.use(auth.requireAuth, auth.requireAdmin);

// ===================== EMPLOYEES =====================
// Distinct department names in use (plus any mapped in routing), for the Add/Edit dropdowns.
router.get('/departments', async (req, res) => {
  const rows = (await q(
    `SELECT d FROM (
       SELECT DISTINCT NULLIF(TRIM(department),'') AS d FROM employees
       UNION SELECT department FROM dept_approvers
     ) x WHERE d IS NOT NULL ORDER BY d`)).rows.map((r) => r.d);
  res.json(rows);
});

router.get('/employees', async (req, res) => {
  const { rows } = await q(
    `SELECT id,emp_no,name,email,phone,department,job_title,app_role,is_admin,active,must_reset,expense_category,reporting_manager_emp_id,conveyance_needs_manager,outpass_via_hr,can_self_raise,app_access,outpass_approver_id,outpass_backup_approver_id
     FROM employees ORDER BY emp_no`);
  res.json(rows);
});

router.post('/employees', async (req, res) => {
  const { emp_no, name, email, phone, department, job_title, is_admin,
          expense_category, reporting_manager_emp_id, conveyance_needs_manager,
          outpass_via_hr, can_self_raise, app_access,
          outpass_approver_id, outpass_backup_approver_id } = req.body || {};
  if (!emp_no || !name) return res.status(400).json({ error: 'Employee number and name required' });
  const hash = await auth.hashPw(process.env.DEFAULT_PASSWORD || 'Bsc@123');
  const cat = ['CAT1', 'CAT2'].includes(expense_category) ? expense_category : 'CAT2';
  const rm = reporting_manager_emp_id ? Number(reporting_manager_emp_id) : null;
  const opAppr = outpass_approver_id ? Number(outpass_approver_id) : null;
  const opBackup = outpass_backup_approver_id ? Number(outpass_backup_approver_id) : null;
  const convMgr = (conveyance_needs_manager === undefined || conveyance_needs_manager === null) ? true : !!conveyance_needs_manager;
  const opvHr = !!outpass_via_hr;
  const selfRaise = !!can_self_raise;
  const appAcc = (app_access && typeof app_access === 'object')
    ? JSON.stringify(require('../lib/apps').RESTRICTED.reduce((o, k) => { o[k] = app_access[k] === true; return o; }, {}))
    : '{}';
  try {
    const { rows } = await q(
      `INSERT INTO employees(emp_no,name,email,phone,department,job_title,is_admin,password_hash,must_reset,
         expense_category,reporting_manager_emp_id,conveyance_needs_manager,outpass_via_hr,can_self_raise,
         app_access,outpass_approver_id,outpass_backup_approver_id)
       VALUES($1,$2,$3,$4,$5,$6,$7,$8,TRUE,$9,$10,$11,$12,$13,$14::jsonb,$15,$16) RETURNING id`,
      [String(emp_no).trim(), name, email || null, normPhone(phone), department || null,
       job_title || null, !!is_admin, hash, cat, rm, convMgr, opvHr, selfRaise, appAcc, opAppr, opBackup]);
    // Fire a one-time WhatsApp welcome (install + module intro + first-time login).
    // Non-blocking: adding the employee never fails if WhatsApp/template isn't ready.
    const wphone = normPhone(phone);
    if (wphone) background(wati.notify.welcome(
      { name, emp_no: String(emp_no).trim(), phone: wphone }));
    res.json({ ok: true, id: rows[0].id });
  } catch (e) {
    if (e.code === '23505') return res.status(409).json({ error: 'Employee number already exists' });
    console.error('[add-employee] failed:', e.code, e.message);
    return res.status(400).json({ error: 'Could not add employee: ' + (e.detail || e.message) });
  }
});

// Resend the WhatsApp welcome/onboarding message to one employee (admin button).
router.post('/employees/:id/welcome', async (req, res) => {
  const row = (await q('SELECT emp_no,name,phone,must_reset FROM employees WHERE id=$1', [req.params.id])).rows[0];
  if (!row) return res.status(404).json({ error: 'Employee not found' });
  if (!row.phone) return res.status(400).json({ error: 'No WhatsApp number on file for this employee' });
  background(wati.notify.welcome(row));
  res.json({ ok: true });
});

router.put('/employees/:id', async (req, res) => {
  const { emp_no, name, email, phone, department, job_title, is_admin, active, expense_category, reporting_manager_emp_id, conveyance_needs_manager, outpass_via_hr, can_self_raise, app_access, outpass_approver_id, outpass_backup_approver_id } = req.body || {};
  const cat = ['CAT1', 'CAT2'].includes(expense_category) ? expense_category : null;
  const rm = reporting_manager_emp_id ? Number(reporting_manager_emp_id) : null;
  const opAppr = outpass_approver_id ? Number(outpass_approver_id) : null;
  const opBackup = outpass_backup_approver_id ? Number(outpass_backup_approver_id) : null;
  const convMgr = (conveyance_needs_manager === undefined || conveyance_needs_manager === null) ? null : !!conveyance_needs_manager;
  const opvHr = (outpass_via_hr === undefined || outpass_via_hr === null) ? null : !!outpass_via_hr;
  const selfRaise = (can_self_raise === undefined || can_self_raise === null) ? null : !!can_self_raise;
  // Only keep the known restricted keys; store as a clean JSONB map.
  const appAcc = (app_access && typeof app_access === 'object')
    ? JSON.stringify(require('../lib/apps').RESTRICTED.reduce((o, k) => { o[k] = app_access[k] === true; return o; }, {}))
    : null;
  try {
    await q(
      `UPDATE employees SET
         emp_no=COALESCE($2,emp_no), name=COALESCE($3,name), email=$4, phone=$5,
         department=$6, job_title=$7, is_admin=COALESCE($8,is_admin),
         active=COALESCE($9,active), expense_category=COALESCE($10,expense_category),
         reporting_manager_emp_id=$11, conveyance_needs_manager=COALESCE($12,conveyance_needs_manager),
         outpass_via_hr=COALESCE($13,outpass_via_hr), can_self_raise=COALESCE($14,can_self_raise),
         app_access=COALESCE($15::jsonb,app_access),
         outpass_approver_id=$16, outpass_backup_approver_id=$17
       WHERE id=$1`,
      [req.params.id, emp_no ? String(emp_no).trim() : null, name, email || null, normPhone(phone),
       department || null, job_title || null, is_admin, active, cat, rm, convMgr, opvHr, selfRaise, appAcc, opAppr, opBackup]);
    res.json({ ok: true });
  } catch (e) {
    if (e.code === '23505') return res.status(409).json({ error: 'That employee code is already in use' });
    console.error('[edit-employee] failed:', e.code, e.message);
    return res.status(400).json({ error: 'Could not save: ' + (e.detail || e.message) });
  }
});

// Deactivate (soft) — preserves ticket history.
router.post('/employees/:id/deactivate', async (req, res) => {
  const id = +req.params.id;
  await q('UPDATE employees SET active=FALSE WHERE id=$1', [id]);
  // Auto-reassign this person's OPEN tickets to the current resolved handler for each ticket's
  // category/trade/location, so nothing stays pointed at a deactivated handler.
  let reassigned = 0;
  try {
    const open = (await q(
      `SELECT id, category_id, trade_id, location_id, is_self, l1_emp_id, l2_emp_id, l3_emp_id
       FROM tickets WHERE status IN ('open','in_progress','reopened')
         AND (l1_emp_id=$1 OR l2_emp_id=$1 OR l3_emp_id=$1)`, [id])).rows;
    for (const t of open) {
      const cur = (await q(
        `SELECT COALESCE(tll.l1_emp_id, tr.l1_emp_id, c.l1_emp_id) AS l1, c.l2_emp_id AS l2, c.l3_emp_id AS l3
         FROM categories c
         LEFT JOIN trades tr ON tr.id=$2
         LEFT JOIN trade_location_l1 tll ON tll.trade_id=$2 AND tll.location_id=$3
         WHERE c.id=$1`, [t.category_id, t.trade_id, t.location_id])).rows[0] || {};
      const sets = [], vals = [t.id]; let i = 2; const moved = [];
      // L1: skip self-tickets (their L1 is the requester, not a category handler)
      if (t.l1_emp_id === id && !t.is_self && cur.l1 && cur.l1 !== id) { sets.push(`l1_emp_id=$${i}`); vals.push(cur.l1); i++; moved.push('L1'); }
      if (t.l2_emp_id === id && cur.l2 && cur.l2 !== id) { sets.push(`l2_emp_id=$${i}`); vals.push(cur.l2); i++; moved.push('L2'); }
      if (t.l3_emp_id === id && cur.l3 && cur.l3 !== id) { sets.push(`l3_emp_id=$${i}`); vals.push(cur.l3); i++; moved.push('L3'); }
      if (sets.length) {
        await q(`UPDATE tickets SET ${sets.join(',')} WHERE id=$1`, vals);
        await q(`INSERT INTO ticket_events(ticket_id,event,note) VALUES($1,'reassigned',$2)`,
          [t.id, `${moved.join('/')} reassigned to current handler after deactivation`]);
        reassigned++;
      }
    }
  } catch (e) { console.error('[deactivate reassign]', e.message); }
  res.json({ ok: true, reassigned });
});
router.post('/employees/:id/activate', async (req, res) => {
  await q('UPDATE employees SET active=TRUE WHERE id=$1', [req.params.id]);
  res.json({ ok: true });
});

// Admin password reset -> back to default + force change on next login.
router.post('/employees/:id/reset-password', async (req, res) => {
  const hash = await auth.hashPw(process.env.DEFAULT_PASSWORD || 'Bsc@123');
  await q('UPDATE employees SET password_hash=$1, must_reset=TRUE WHERE id=$2', [hash, req.params.id]);
  res.json({ ok: true, default_password: process.env.DEFAULT_PASSWORD || 'Bsc@123' });
});

function normPhone(p) {
  if (!p) return null;
  let d = String(p).replace(/\D/g, '');
  if (d.length === 10) d = '91' + d;
  return d || null;
}

// ===================== LOCATIONS =====================
router.get('/locations', async (req, res) =>
  res.json((await q('SELECT * FROM locations ORDER BY sort_order,name')).rows));
router.post('/locations', async (req, res) => {
  const { name } = req.body || {};
  if (!name) return res.status(400).json({ error: 'Name required' });
  const { rows } = await q('SELECT COALESCE(MAX(sort_order),0)+1 s FROM locations');
  await q('INSERT INTO locations(name,sort_order) VALUES($1,$2)', [name, rows[0].s]);
  res.json({ ok: true });
});
router.put('/locations/:id', async (req, res) => {
  const { name, active } = req.body || {};
  await q('UPDATE locations SET name=COALESCE($2,name), active=COALESCE($3,active) WHERE id=$1',
    [req.params.id, name, active]);
  res.json({ ok: true });
});

// ===================== CATEGORIES + TRADES + ROUTING =====================
router.get('/categories', async (req, res) => {
  const cats = (await q(`SELECT * FROM categories ORDER BY sort_order,name`)).rows;
  const trades = (await q(`SELECT * FROM trades ORDER BY sort_order,name`)).rows;
  const pool = (await q(`SELECT category_id, emp_id FROM category_l1_pool`)).rows;
  res.json(cats.map((c) => ({
    ...c,
    trades: trades.filter((t) => t.category_id === c.id),
    l1_pool: pool.filter((p) => p.category_id === c.id).map((p) => p.emp_id),
  })));
});

router.post('/categories', async (req, res) => {
  const { name, pattern, wait_unassigned_mins, wait_cycle_mins, wait_l3_mins } = req.body || {};
  if (!name) return res.status(400).json({ error: 'Name required' });
  const pat = pattern === 'direct' ? 'direct' : 'assign';
  const { rows } = await q('SELECT COALESCE(MAX(sort_order),0)+1 s FROM categories');
  const r = await q(
    `INSERT INTO categories(name,pattern,wait_unassigned_mins,wait_cycle_mins,wait_l3_mins,sort_order)
     VALUES($1,$2,$3,$4,$5,$6) RETURNING id`,
    [name, pat, wait_unassigned_mins || 60, wait_cycle_mins || 120, wait_l3_mins || 240, rows[0].s]);
  res.json({ ok: true, id: r.rows[0].id });
});

// Update routing (pattern, levels, waits, trades flag, activation) for a category.
router.put('/categories/:id', async (req, res) => {
  const { name, pattern, has_trades, l1_emp_id, l2_emp_id, l3_emp_id,
    wait_unassigned_mins, wait_cycle_mins, wait_l3_mins, active } = req.body || {};
  await q(
    `UPDATE categories SET name=COALESCE($2,name), pattern=COALESCE($3,pattern),
       has_trades=COALESCE($4,has_trades),
       l1_emp_id=$5, l2_emp_id=$6, l3_emp_id=$7,
       wait_unassigned_mins=COALESCE($8,wait_unassigned_mins),
       wait_cycle_mins=COALESCE($9,wait_cycle_mins),
       wait_l3_mins=COALESCE($10,wait_l3_mins),
       active=COALESCE($11,active) WHERE id=$1`,
    [req.params.id, name, pattern, (typeof has_trades === 'boolean' ? has_trades : null),
     l1_emp_id || null, l2_emp_id || null, l3_emp_id || null,
     wait_unassigned_mins, wait_cycle_mins, wait_l3_mins, active]);
  res.json({ ok: true });
});

// Delete a category (only if no tickets reference it; otherwise deactivate instead).
router.delete('/categories/:id', async (req, res) => {
  const used = (await q('SELECT COUNT(*)::int n FROM tickets WHERE category_id=$1', [req.params.id])).rows[0].n;
  if (used > 0) return res.status(409).json({ error: `Can't delete — ${used} ticket(s) use this category. Deactivate it instead.` });
  await q('DELETE FROM trades WHERE category_id=$1', [req.params.id]);
  await q('DELETE FROM category_l1_pool WHERE category_id=$1', [req.params.id]);
  await q('DELETE FROM categories WHERE id=$1', [req.params.id]);
  res.json({ ok: true });
});

// Replace the L1 pool (assignable handlers) for a category.
router.put('/categories/:id/pool', async (req, res) => {
  const ids = Array.isArray(req.body && req.body.emp_ids) ? req.body.emp_ids.filter(Boolean) : [];
  await q('DELETE FROM category_l1_pool WHERE category_id=$1', [req.params.id]);
  for (const eid of ids) {
    await q('INSERT INTO category_l1_pool(category_id,emp_id) VALUES($1,$2) ON CONFLICT DO NOTHING',
      [req.params.id, eid]);
  }
  res.json({ ok: true });
});

// ---- Holidays (working-calendar) ----
router.get('/holidays', async (req, res) => {
  const rows = (await q(`SELECT to_char(d,'YYYY-MM-DD') AS d, label FROM holidays ORDER BY d`)).rows;
  res.json(rows);
});
router.post('/holidays', async (req, res) => {
  const { d, label } = req.body || {};
  if (!d || !/^\d{4}-\d{2}-\d{2}$/.test(d)) return res.status(400).json({ error: 'Valid date (YYYY-MM-DD) required' });
  await q(`INSERT INTO holidays(d,label) VALUES($1,$2) ON CONFLICT (d) DO UPDATE SET label=EXCLUDED.label`, [d, (label || '').trim() || null]);
  res.json({ ok: true });
});
router.delete('/holidays/:d', async (req, res) => {
  await q('DELETE FROM holidays WHERE d=$1', [req.params.d]);
  res.json({ ok: true });
});

router.post('/trades', async (req, res) => {
  const { category_id, name, l1_emp_id } = req.body || {};
  if (!category_id || !name) return res.status(400).json({ error: 'Category and name required' });
  const { rows } = await q('SELECT COALESCE(MAX(sort_order),0)+1 s FROM trades WHERE category_id=$1', [category_id]);
  await q('INSERT INTO trades(category_id,name,l1_emp_id,sort_order) VALUES($1,$2,$3,$4)',
    [category_id, name, l1_emp_id || null, rows[0].s]);
  res.json({ ok: true });
});
router.put('/trades/:id', async (req, res) => {
  const { name, l1_emp_id, active, location_based } = req.body || {};
  const lb = (location_based === undefined || location_based === null) ? null : !!location_based;
  await q('UPDATE trades SET name=COALESCE($2,name), l1_emp_id=$3, active=COALESCE($4,active), location_based=COALESCE($5,location_based) WHERE id=$1',
    [req.params.id, name, l1_emp_id || null, active, lb]);
  res.json({ ok: true });
});

// Location → handler (L1) map for a location-based trade.
router.get('/trades/:id/locations', async (req, res) => {
  const tr = (await q('SELECT id,name,location_based FROM trades WHERE id=$1', [req.params.id])).rows[0];
  if (!tr) return res.status(404).json({ error: 'Trade not found' });
  const rows = (await q(
    `SELECT l.id AS location_id, l.name AS location_name, m.l1_emp_id
     FROM locations l
     LEFT JOIN trade_location_l1 m ON m.location_id=l.id AND m.trade_id=$1
     WHERE l.active=TRUE ORDER BY l.sort_order, l.name`, [req.params.id])).rows;
  res.json({ trade: tr, locations: rows });
});

router.put('/trades/:id/locations', async (req, res) => {
  const maps = Array.isArray(req.body && req.body.mappings) ? req.body.mappings : [];
  try {
    for (const m of maps) {
      const loc = Number(m.location_id); const emp = m.l1_emp_id ? Number(m.l1_emp_id) : null;
      if (!loc) continue;
      if (emp) {
        await q(
          `INSERT INTO trade_location_l1(trade_id,location_id,l1_emp_id) VALUES($1,$2,$3)
           ON CONFLICT(trade_id,location_id) DO UPDATE SET l1_emp_id=EXCLUDED.l1_emp_id`,
          [req.params.id, loc, emp]);
      } else {
        await q('DELETE FROM trade_location_l1 WHERE trade_id=$1 AND location_id=$2', [req.params.id, loc]);
      }
    }
    res.json({ ok: true });
  } catch (e) { console.error('save loc handlers', e); res.status(500).json({ error: 'Could not save location handlers' }); }
});
router.delete('/trades/:id', async (req, res) => {
  const used = (await q('SELECT COUNT(*)::int n FROM tickets WHERE trade_id=$1', [req.params.id])).rows[0].n;
  if (used > 0) return res.status(409).json({ error: `Can't delete — ${used} ticket(s) use this trade. Deactivate it instead.` });
  await q('DELETE FROM trades WHERE id=$1', [req.params.id]);
  res.json({ ok: true });
});

// ===================== CLEANUP (delete individual test entries) =====================
// Delete a single ticket (its photos + event history cascade away).
// Mute / unmute WhatsApp reminders (L1/L2/L3 escalation + resolved nudges) for ONE ticket.
router.post('/tickets/:id/reminders', async (req, res) => {
  const id = req.params.id;
  if (!/^\d+$/.test(id)) return res.status(400).json({ error: 'Bad id' });
  const off = !!(req.body && req.body.off);
  const r = await q('UPDATE tickets SET reminders_off=$2 WHERE id=$1 RETURNING ref_no, reminders_off', [id, off]);
  if (!r.rows.length) return res.status(404).json({ error: 'Ticket not found' });
  res.json({ ok: true, ref_no: r.rows[0].ref_no, reminders_off: r.rows[0].reminders_off });
});

// Manual "nudge now" from the dashboard ticket list: WhatsApp the assigned handler of every OPEN
// ticket (skips unassigned/gated/muted). Sends run in the background so a slow WATI run can't
// time out the request; the response reports how many will be reminded and the handler names.
router.post('/tickets/remind-open', async (req, res) => {
  const rows = (await q(
    `SELECT t.id, t.ref_no, t.subject, t.raised_at, c.name AS category_name,
            r.name AS requester_name, e.name AS handler_name, e.phone AS handler_phone
     FROM tickets t
     JOIN categories c ON c.id = t.category_id
     JOIN employees r ON r.id = t.requester_id
     JOIN employees e ON e.id = t.l1_emp_id AND e.active = TRUE
     WHERE t.status IN ('open','in_progress','reopened') AND t.closed_at IS NULL
       AND NOT COALESCE(t.reminders_off, false) AND t.maint_gate IS DISTINCT FROM 'pending'
     ORDER BY t.raised_at`)).rows;
  const toSend = rows.filter(t => t.handler_phone);
  const handlers = [...new Set(toSend.map(t => t.handler_name))];
  res.json({ ok: true, tickets: rows.length, reminded: toSend.length, handlers });
  background((async () => {
    for (const t of toSend) {
      const mins = Math.max(1, Math.round((Date.now() - new Date(t.raised_at).getTime()) / 60000));
      try {
        await wati.notify.reminder({ name: t.handler_name, phone: t.handler_phone },
          { id: t.id, ref_no: t.ref_no, requester_name: t.requester_name, category_name: t.category_name, subject: t.subject },
          elapsedLabel(mins));
        await q(`UPDATE tickets SET last_reminder_at = now() WHERE id = $1`, [t.id]);
        await q(`INSERT INTO ticket_events(ticket_id,event,note) VALUES($1,'reminder',$2)`,
          [t.id, `Manual reminder to ${t.handler_name} (admin)`]);
      } catch (e) { console.error('[remind-open]', t.id, e.message); }
    }
  })());
});

// Admin force-resolve: mark a ticket resolved directly from the cleanup list. Mirrors the normal
// resolve flow (sets resolved_at + confirm token, nudges requester/L2 to confirm closure).
router.post('/tickets/:id/resolve', async (req, res) => {
  const id = req.params.id;
  if (!/^\d+$/.test(id)) return res.status(400).json({ error: 'Bad id' });
  const t = (await q(
    `SELECT t.*, c.name AS category_name, r.name AS requester_name, r.phone AS requester_phone
     FROM tickets t JOIN categories c ON c.id=t.category_id JOIN employees r ON r.id=t.requester_id
     WHERE t.id=$1`, [id])).rows[0];
  if (!t) return res.status(404).json({ error: 'Ticket not found' });
  if (!['open', 'in_progress', 'reopened'].includes(t.status))
    return res.status(400).json({ error: `Ticket is already ${t.status}` });
  const note = ((req.body && req.body.note) || 'Resolved by admin').trim();
  const confirmToken = crypto.randomBytes(20).toString('hex');
  await q(`UPDATE tickets SET status='resolved', resolved_at=now(), resolution_note=$2, confirm_token=$3 WHERE id=$1`,
    [id, note, confirmToken]);
  await q(`INSERT INTO ticket_events(ticket_id,event,by_emp_id,note) VALUES($1,'resolved',$2,$3)`,
    [id, req.user.id, note]);
  res.json({ ok: true });
  background((async () => {
    t.confirm_token = confirmToken;
    const seen = new Set(); const recipients = [];
    const targets = [{ id: t.requester_id, name: t.requester_name, phone: t.requester_phone }];
    if (t.l2_emp_id) { const l2 = (await q('SELECT id,name,phone FROM employees WHERE id=$1', [t.l2_emp_id])).rows[0]; if (l2) targets.push(l2); }
    for (const p of targets) {
      if (!p || !p.phone || seen.has(p.id)) continue; seen.add(p.id);
      try { await wati.notify.resolved(p, t, 'Admin', note); recipients.push(p); } catch (e) { console.error('[admin-resolve] notify', e.message); }
    }
    try { await excel.syncLogToOneDrive(); } catch (e) {}
  })());
});

router.delete('/tickets/:id', async (req, res) => {
  const id = req.params.id;
  if (!/^\d+$/.test(id)) return res.status(400).json({ error: 'Bad id' });
  try {
    // Delete child rows first, so the ticket is removed even if the live DB's foreign keys were
    // created without ON DELETE CASCADE (in which case a plain DELETE FROM tickets throws, the
    // ticket survives, and the escalation cron keeps nudging L1/L2/L3).
    await q('DELETE FROM ticket_photos WHERE ticket_id=$1', [id]);
    await q('DELETE FROM ticket_events WHERE ticket_id=$1', [id]);
    const r = await q('DELETE FROM tickets WHERE id=$1 RETURNING ref_no', [id]);
    if (!r.rows.length) return res.status(404).json({ error: 'Ticket not found' });
    res.json({ ok: true, ref_no: r.rows[0].ref_no });
  } catch (e) {
    console.error('[ticket delete] failed:', e.message);
    res.status(500).json({ error: 'Delete failed: ' + e.message });
  }
});

// Recent outpass/gatepass entries (for review + cleanup).
// Recent tickets (review + cleanup of test entries), filterable by status/search.
router.get('/ticket-list', async (req, res) => {
  const from = req.query.from || '2000-01-01';
  const to = req.query.to || '2999-12-31';
  const params = [from, to];
  let where = 't.raised_at::date BETWEEN $1 AND $2';
  if (req.query.status === 'external') {
    where += " AND t.external_hold=TRUE AND t.status IN ('open','in_progress','reopened')";
  } else if (req.query.status) {
    params.push(req.query.status); where += ` AND t.status=$${params.length}`;
  }
  if (req.query.q) {
    params.push('%' + String(req.query.q).toLowerCase() + '%');
    where += ` AND (lower(t.ref_no) LIKE $${params.length} OR lower(t.subject) LIKE $${params.length})`;
  }
  const rows = (await q(
    `SELECT t.id,t.ref_no,t.subject,t.status,t.priority,t.raised_at,t.resolved_at,t.closed_at,t.external_hold,
            COALESCE(t.reminders_off,false) AS reminders_off,
            c.name AS category_name, r.name AS requester_name,
            l1.name AS assigned_to,
            EXISTS(SELECT 1 FROM ticket_events e WHERE e.ticket_id=t.id AND e.event='forwarded') AS was_forwarded
     FROM tickets t JOIN categories c ON c.id=t.category_id JOIN employees r ON r.id=t.requester_id
     LEFT JOIN employees l1 ON l1.id=t.l1_emp_id
     WHERE ${where}
     ORDER BY t.raised_at DESC LIMIT 300`, params)).rows;
  res.json(rows.map((t) => ({
    ...t,
    downtime_mins: downtimeMins(t),
    // Time from raised to when it was marked resolved (handler's resolve time). Falls back to closed.
    resolve_mins: t.resolved_at ? Math.round((new Date(t.resolved_at) - new Date(t.raised_at)) / 60000) : downtimeMins(t),
  })));
});

router.get('/outpass-list', async (req, res) => {
  const from = req.query.from || '2000-01-01';
  const to = req.query.to || '2999-12-31';
  const params = [from, to];
  let where = 'o.req_date BETWEEN $1 AND $2';
  if (req.query.type)   { params.push(req.query.type);   where += ` AND o.type=$${params.length}`; }
  if (req.query.status) { params.push(req.query.status); where += ` AND o.status=$${params.length}`; }
  const rows = (await q(
    `SELECT o.id, o.ref_no, o.type, o.req_date, o.status, o.purpose,
            o.out_time, o.in_time, o.returned_at,
            r.name AS requester_name
     FROM outpass_requests o LEFT JOIN employees r ON r.id=o.requester_id
     WHERE ${where}
     ORDER BY o.id DESC LIMIT 300`, params)).rows;
  res.json(rows);
});

// Delete a single outpass/gatepass entry.
router.delete('/outpass/:id', async (req, res) => {
  const r = await q('DELETE FROM outpass_requests WHERE id=$1 RETURNING ref_no', [req.params.id]);
  if (!r.rows.length) return res.status(404).json({ error: 'Entry not found' });
  res.json({ ok: true, ref_no: r.rows[0].ref_no });
});

// Manually fire the overdue WhatsApp for one pass, to HR + the approver, right now.
// Uses the real notify path (same as the cron) so it also verifies delivery end-to-end.
router.post('/outpass/:id/send-overdue', async (req, res) => {
  const op = require('../routes/outpass.routes')._internal;
  const wati = require('../lib/wati');
  const o = (await q(
    `SELECT o.*, r.name AS req_name, r.phone AS req_phone, ap.name AS approver_name, ap.phone AS approver_phone
     FROM outpass_requests o JOIN employees r ON r.id=o.requester_id
     LEFT JOIN employees ap ON ap.id=o.approver_id WHERE o.id=$1`, [req.params.id])).rows[0];
  if (!o) return res.status(404).json({ error: 'Pass not found' });
  const cfg = await op.gateConfig();
  let hr = null;
  if (cfg.hrEmpId) hr = (await q(`SELECT name, phone FROM employees WHERE id=$1 AND active=TRUE`, [cfg.hrEmpId])).rows[0] || null;
  const mins = o.expected_back_at ? Math.max(0, Math.floor((Date.now() - +new Date(o.expected_back_at)) / 60000)) : 0;
  const payload = { employee: o.req_name, ref: o.ref_no, out_time: o.out_time, expected: o.in_time,
    overdue_min: mins, purpose: o.purpose, duty: o.on_duty ? 'On duty (official)' : 'Personal' };
  const results = {};
  if (o.approver_phone) results.approver = await wati.notify.outpass.overdue({ name: o.approver_name, phone: o.approver_phone }, payload);
  else results.approver_note = 'approver has no phone';
  if (hr && hr.phone) { results.hr = await wati.notify.outpass.overdue({ name: hr.name, phone: hr.phone }, payload); if (results.hr) await op.markHrAlerted(o.id); }
  else results.hr_note = cfg.hrEmpId ? 'HR has no phone' : 'HR not configured';
  if (o.req_phone) {
    const token = o.return_token || await op.ensureReturnToken(o.id);
    results.requester = await wati.notify.outpass.returnReminder({ name: o.req_name, phone: o.req_phone }, { ...payload, token });
    if (results.requester) await op.markRequesterReminded(o.id);
  } else results.requester_note = 'requester has no phone';
  res.json({ ok: true, ref_no: o.ref_no, hr_name: (hr && hr.name) || null, results });
});

// Recent OT entries (for review + cleanup of test entries).
router.get('/ot-list', async (req, res) => {
  const from = req.query.from || '2000-01-01';
  const to = req.query.to || '2999-12-31';
  const params = [from, to];
  let where = 'e.ot_date BETWEEN $1 AND $2';
  if (req.query.status) { params.push(req.query.status); where += ` AND e.status=$${params.length}`; }
  const rows = (await q(
    `SELECT e.id, e.ot_date, e.end_time, e.hours, e.amount, e.status, e.department,
            emp.name AS employee_name, emp.emp_no
     FROM ot_entries e LEFT JOIN employees emp ON emp.id=e.employee_id
     WHERE ${where}
     ORDER BY e.id DESC LIMIT 300`, params)).rows;
  res.json(rows);
});
router.delete('/ot/:id', async (req, res) => {
  const r = await q('DELETE FROM ot_entries WHERE id=$1 RETURNING id', [req.params.id]);
  if (!r.rows.length) return res.status(404).json({ error: 'Entry not found' });
  res.json({ ok: true });
});

// Recent expense submissions (for review + cleanup of test entries).
router.get('/expense-list', async (req, res) => {
  const from = req.query.from || '2000-01-01';
  const to = req.query.to || '2999-12-31';
  const params = [from, to];
  let where = `s.created_at::date BETWEEN $1 AND $2 AND NOT (s.status='draft' AND COALESCE(s.total_amount,0)=0)`;
  if (req.query.form_type) { params.push(req.query.form_type); where += ` AND s.form_type=$${params.length}`; }
  if (req.query.status)    { params.push(req.query.status);    where += ` AND s.status=$${params.length}`; }
  const rows = (await q(
    `SELECT s.id, s.ref_no, s.form_type, s.period, s.status, s.total_amount,
            e.name AS employee_name
     FROM expense_submissions s LEFT JOIN employees e ON e.id=s.employee_id
     WHERE ${where}
     ORDER BY s.id DESC LIMIT 300`, params)).rows;
  res.json(rows);
});

// Delete a single expense submission. For conveyance, also clear that month's trips
// so the period resets cleanly (trips are keyed by employee+period, not the submission).
router.delete('/expense/:id', async (req, res) => {
  const row = (await q('SELECT id, ref_no, form_type, employee_id, period FROM expense_submissions WHERE id=$1', [req.params.id])).rows[0];
  if (!row) return res.status(404).json({ error: 'Entry not found' });
  if (row.form_type === 'conveyance' && row.period) {
    await q('DELETE FROM conveyance_trips WHERE employee_id=$1 AND period=$2', [row.employee_id, row.period]);
  }
  await q('DELETE FROM expense_submissions WHERE id=$1', [row.id]);
  res.json({ ok: true, ref_no: row.ref_no });
});

// ===================== REQUESTER GROUPS (self-ticket "Requested by") =====================
router.get('/requester-groups', async (req, res) => {
  const r = (await q(`SELECT value FROM app_settings WHERE key='requester_groups'`)).rows[0];
  res.json({ value: (r && r.value) || '' });
});
router.put('/requester-groups', async (req, res) => {
  const value = String((req.body && req.body.value) || '')
    .split(/[\n,]/).map((s) => s.trim()).filter(Boolean).join('\n');
  await q(`INSERT INTO app_settings(key,value) VALUES('requester_groups',$1)
           ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value`, [value]);
  res.json({ ok: true });
});

// Global kill switch for ticket reminders — the L1/L2/L3 escalation nudges AND the
// resolved-ticket confirm-close nudges. Default ON. Turning it off pauses all of them
// without touching any per-ticket data.
router.get('/ticket-reminders', async (req, res) => {
  const r = (await q(`SELECT value FROM app_settings WHERE key='ticket_reminders_enabled'`)).rows[0];
  res.json({ enabled: !r || r.value !== 'false' });
});
router.post('/ticket-reminders', async (req, res) => {
  const enabled = !!(req.body && req.body.enabled);
  await q(`INSERT INTO app_settings(key,value) VALUES('ticket_reminders_enabled',$1)
           ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value`, [enabled ? 'true' : 'false']);
  res.json({ ok: true, enabled });
});

// Diagnostic: fire the real conveyance_request template at a chosen phone (or a manager's own
// number) and return WATI's raw reply, so we can see exactly why managers aren't getting notified.
router.post('/wati-test', async (req, res) => {
  const wati = require('../lib/wati');
  const which = (req.body && req.body.which) || 'conveyance';
  const phone = (req.body && req.body.phone || '').replace(/[^\d+]/g, '');
  if (!phone) return res.status(400).json({ error: 'Enter a phone number with country code.' });
  let template, params;
  if (which === 'overdue') {
    template = wati.OUTPASS_TPL.overdue;
    params = { name: 'HR', employee: 'TEST Employee', duty: 'Personal', ref: 'GP-TEST-01',
      out_time: '02:15 PM', expected: '03:15 PM', overdue_min: '25', purpose: 'Bank work' };
  } else if (which === 'return_reminder') {
    template = wati.OUTPASS_TPL.returnReminder;
    params = { name: 'TEST Employee', ref: 'GP-TEST-01',
      out_time: '02:15 PM', expected: '03:15 PM', overdue_min: '25', '1': 'test-token', token: 'test-token' };
  } else if (which === 'ot_approval') {
    template = wati.TEMPLATES.ot_approval;
    params = { name: 'Kannan', employee: 'TEST Employee', date: '05 Aug 2026', hours: '1.50', amount: '150', pending: '3' };
  } else if (which === 'ot_hr') {
    template = wati.TEMPLATES.ot_hr_verify;
    params = { name: 'Rajasekar', count: '12', total: '3600' };
  } else if (which === 'ot_mgmt') {
    template = wati.TEMPLATES.ot_mgmt;
    params = { name: 'Goverdhan', period: 'July 2026', employees: '8', total: '9200' };
  } else if (which === 'ot_accounts') {
    template = wati.TEMPLATES.ot_accounts;
    params = { name: 'Lakshmi', period: 'July 2026', employees: '8', total: '9200' };
  } else if (which === 'ot_rejected') {
    template = wati.TEMPLATES.ot_rejected;
    params = { name: 'TEST Employee', date: '05 Aug 2026', amount: '150', stage: 'approver', reason: 'Wrong end time' };
  } else if (which === 'maint_gate') {
    template = wati.TEMPLATES.maint_gate;
    params = { name: 'Mathan', ref: 'TKT/BSC/260813/001', requester: 'TEST Requester', subject: 'AC not cooling', category: 'Maintenance', token: 'testtoken123' };
  } else if (which === 'maint_rejected') {
    template = wati.TEMPLATES.maint_rejected;
    params = { name: 'TEST Requester', ref: 'TKT/BSC/260813/001', subject: 'AC not cooling' };
  } else {
    template = wati.EXPENSE_TPL.cv_request;
    params = { name: 'Manager', requester: 'TEST Employee', date: '28 Jul 2026',
      route: 'Plant \u2192 Bank', amount: '\u20b9240', token: 'TESTTOKEN123' };
  }
  const out = await wati.sendTemplateDebug(phone, template, params);
  res.json({ tested_phone: phone, template, result: out });
});

// ===================== DAILY REPORT =====================
const parsePhones = (v) => String(v || '').split(/[\n,]/).map((s) => s.replace(/[^\d+]/g, '')).filter(Boolean);

router.get('/daily-report', async (req, res) => {
  const r = (await q(`SELECT value FROM app_settings WHERE key='daily_report_phone'`)).rows[0];
  res.json({ value: (r && r.value) || '' });
});
router.put('/daily-report', async (req, res) => {
  const value = parsePhones(req.body && req.body.value).join('\n');
  await q(`INSERT INTO app_settings(key,value) VALUES('daily_report_phone',$1)
           ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value`, [value]);
  res.json({ ok: true, value });
});

// Admin previews the PDF straight from the panel (cookie-authed). Optional ?emp= for a person's scoped view.
router.get('/report-daily.pdf', async (req, res) => {
  const date = /^\d{4}-\d{2}-\d{2}$/.test(req.query.date || '')
    ? req.query.date : new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });
  let scope = null, subtitle = null;
  if (req.query.emp) {
    const s = (await q(`SELECT s.category_ids, s.trade_ids, e.name FROM report_subscriptions s JOIN employees e ON e.id=s.employee_id WHERE s.employee_id=$1`, [+req.query.emp])).rows[0];
    if (s) { scope = { categoryIds: s.category_ids, tradeIds: s.trade_ids }; subtitle = s.name; }
  }
  try {
    const { pdf } = await require('../lib/report').dailyReportPdf(date, scope, subtitle, { remark: !!scope });
    res.setHeader('Content-Type', 'application/pdf');
    res.setHeader('Content-Disposition', `inline; filename="daily-report-${date}.pdf"`);
    res.send(pdf);
  } catch (e) { console.error('admin report pdf', e); res.status(500).json({ error: 'report error' }); }
});

// "Send now" — runs the full dispatch (overall recipients + per-person subscribers).
router.post('/daily-report/send', async (req, res) => {
  const date = new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });
  try {
    const r = await require('../lib/report').dispatchDailyReports(date);
    if (!r.overall_sent.length && !r.scoped_sent.length)
      return res.status(400).json({ error: 'Nobody to send to — add an overall recipient or a per-person subscriber.' });
    res.json({ ok: true, ...r });
  } catch (e) { console.error('send report', e); res.status(500).json({ error: 'Could not send report' }); }
});

// ----- Per-employee report subscriptions -----
router.get('/report-subscriptions', async (req, res) => {
  const rows = (await q(
    `SELECT s.employee_id, s.enabled, s.category_ids, s.trade_ids, e.name, e.emp_no, e.phone
     FROM report_subscriptions s JOIN employees e ON e.id=s.employee_id
     ORDER BY e.name`)).rows;
  res.json(rows);
});
router.put('/report-subscriptions/:empId', async (req, res) => {
  const empId = +req.params.empId;
  const b = req.body || {};
  const enabled = b.enabled === undefined ? true : !!b.enabled;
  const cats = Array.isArray(b.category_ids) ? b.category_ids.map(Number).filter(Boolean) : [];
  const trades = Array.isArray(b.trade_ids) ? b.trade_ids.map(Number).filter(Boolean) : [];
  await q(
    `INSERT INTO report_subscriptions(employee_id,enabled,category_ids,trade_ids)
     VALUES($1,$2,$3,$4)
     ON CONFLICT(employee_id) DO UPDATE SET enabled=EXCLUDED.enabled, category_ids=EXCLUDED.category_ids, trade_ids=EXCLUDED.trade_ids`,
    [empId, enabled, cats, trades]);
  res.json({ ok: true });
});
router.delete('/report-subscriptions/:empId', async (req, res) => {
  await q(`DELETE FROM report_subscriptions WHERE employee_id=$1`, [+req.params.empId]);
  res.json({ ok: true });
});

// ===================== DASHBOARD =====================
router.get('/dashboard', async (req, res) => {
  const from = req.query.from || '2000-01-01';
  const to = req.query.to || '2999-01-01';
  const range = [from, to + ' 23:59:59'];

  const totals = (await q(
    `SELECT
       COUNT(*)::int AS total,
       COUNT(*) FILTER (WHERE status IN ('open','in_progress','reopened'))::int AS open,
       COUNT(*) FILTER (WHERE status='resolved')::int AS resolved,
       COUNT(*) FILTER (WHERE status='closed')::int AS closed,
       COUNT(*) FILTER (WHERE escalation_level>0)::int AS escalated
     FROM tickets WHERE raised_at BETWEEN $1 AND $2`, range)).rows[0];

  const byCategory = (await q(
    `SELECT c.name AS category, COUNT(*)::int AS count,
            ROUND(AVG(EXTRACT(EPOCH FROM (t.closed_at - t.raised_at))/60)
                  FILTER (WHERE t.closed_at IS NOT NULL))::int AS avg_downtime_mins
     FROM tickets t JOIN categories c ON c.id=t.category_id
     WHERE t.raised_at BETWEEN $1 AND $2
     GROUP BY c.name ORDER BY count DESC`, range)).rows;

  const byPriority = (await q(
    `SELECT priority, COUNT(*)::int AS count FROM tickets
     WHERE raised_at BETWEEN $1 AND $2 GROUP BY priority`, range)).rows;

  const avgAll = (await q(
    `SELECT ROUND(AVG(EXTRACT(EPOCH FROM (closed_at - raised_at))/60))::int AS m
     FROM tickets WHERE closed_at IS NOT NULL AND raised_at BETWEEN $1 AND $2`, range)).rows[0].m;

  const recent = (await q(
    `SELECT t.id,t.ref_no,t.subject,t.status,t.priority,t.escalation_level,t.raised_at,t.closed_at,t.external_hold,
            c.name AS category_name, r.name AS requester_name, l1.name AS l1_name
     FROM tickets t JOIN categories c ON c.id=t.category_id JOIN employees r ON r.id=t.requester_id
     LEFT JOIN employees l1 ON l1.id=t.l1_emp_id
     WHERE t.raised_at BETWEEN $1 AND $2 ORDER BY t.raised_at DESC LIMIT 200`, range)).rows;

  // Activity counts in the period (from the event log) + self-ticket count.
  const act = (await q(
    `SELECT COUNT(*) FILTER (WHERE event='forwarded')::int    AS forwarded,
            COUNT(*) FILTER (WHERE event='reopened')::int     AS reopened,
            COUNT(*) FILTER (WHERE event='escalated_l3')::int AS escalated_l3
     FROM ticket_events WHERE at BETWEEN $1 AND $2`, range)).rows[0];
  const selfCount = (await q(
    `SELECT COUNT(*)::int n FROM tickets WHERE is_self=TRUE AND raised_at BETWEEN $1 AND $2`, range)).rows[0].n;
  const activity = { ...act, self: selfCount };

  // Per-handler rollup: resolved/forwarded in the period + how many are open on their plate now.
  const resByHandler = (await q(
    `SELECT emp.id, emp.name,
            COUNT(*) FILTER (WHERE e.event='resolved')::int  AS resolved,
            COUNT(*) FILTER (WHERE e.event='forwarded')::int AS forwarded
     FROM ticket_events e JOIN employees emp ON emp.id=e.by_emp_id
     WHERE e.at BETWEEN $1 AND $2 AND e.event IN ('resolved','forwarded')
     GROUP BY emp.id, emp.name`, range)).rows;
  const openByHandler = (await q(
    `SELECT emp.id, emp.name, COUNT(*)::int AS open_now
     FROM tickets t JOIN employees emp ON emp.id=t.l1_emp_id
     WHERE t.status IN ('open','in_progress','reopened')
     GROUP BY emp.id, emp.name`)).rows;
  const hmap = {};
  for (const r of resByHandler) hmap[r.id] = { handler: r.name, resolved: r.resolved, forwarded: r.forwarded, open_now: 0 };
  for (const o of openByHandler) { (hmap[o.id] = hmap[o.id] || { handler: o.name, resolved: 0, forwarded: 0, open_now: 0 }).open_now = o.open_now; }
  const byHandler = Object.values(hmap).sort((a, b) => (b.resolved - a.resolved) || (b.open_now - a.open_now));

  res.json({
    totals, byCategory, byPriority, avg_downtime_mins: avgAll, activity, byHandler,
    recent: recent.map((t) => ({ ...t, downtime_mins: downtimeMins(t) })),
  });
});

// ===================== EXCEL EXPORT (on demand) =====================
router.get('/export.xlsx', async (req, res) => {
  try {
    const buf = await excel.buildWorkbookBuffer();
    res.setHeader('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
    res.setHeader('Content-Disposition', 'attachment; filename="Ticket Log.xlsx"');
    res.send(buf);
  } catch (e) { console.error('export', e); res.status(500).json({ error: 'Export failed' }); }
});

// ===================== OUTPASS APPROVERS =====================
// Department -> head mapping + fallback approver for Outpass/Gatepass routing.
router.get('/approvers', async (req, res) => {
  const employees = (await q(
    `SELECT id, emp_no, name, department FROM employees WHERE active = TRUE ORDER BY name`)).rows;
  // Employees flagged as heads (own outpass routes to the Heads' approver).
  const heads = (await q(
    `SELECT id, name, department FROM employees WHERE active = TRUE AND outpass_via_hr = TRUE ORDER BY name`)).rows;
  // every department that exists on an employee, plus any already-mapped one
  const depts = (await q(
    `SELECT d FROM (
       SELECT DISTINCT NULLIF(TRIM(department),'') AS d FROM employees WHERE active = TRUE
       UNION SELECT department FROM dept_approvers
     ) x WHERE d IS NOT NULL ORDER BY d`)).rows.map(r => r.d);
  const maps = (await q(
    `SELECT d.department, d.head_emp_id, d.active, d.leave_cover_emp_id,
            e.name AS head_name, lc.name AS leave_cover_name
     FROM dept_approvers d
     LEFT JOIN employees e  ON e.id  = d.head_emp_id
     LEFT JOIN employees lc ON lc.id = d.leave_cover_emp_id`)).rows;
  const mapBy = Object.fromEntries(maps.map(m => [m.department.toLowerCase(), m]));
  const departments = depts.map(d => {
    const m = mapBy[d.toLowerCase()] || {};
    return { department: d, head_emp_id: m.head_emp_id || null, head_name: m.head_name || null,
             leave_cover_emp_id: m.leave_cover_emp_id || null, leave_cover_name: m.leave_cover_name || null,
             active: m.active !== false };
  });
  const fb = (await q(`SELECT value FROM app_settings WHERE key = 'outpass_fallback_emp_id'`)).rows[0];
  const lc = (await q(`SELECT value FROM app_settings WHERE key = 'outpass_leavecover_emp_id'`)).rows[0];
  const hd = (await q(`SELECT value FROM app_settings WHERE key = 'outpass_heads_emp_id'`)).rows[0];
  res.json({ departments, employees,
    fallback_emp_id: fb && fb.value ? Number(fb.value) : null,
    leavecover_emp_id: lc && lc.value ? Number(lc.value) : null,
    heads_emp_id: hd && hd.value ? Number(hd.value) : null, heads });
});

router.put('/approvers/dept', async (req, res) => {
  const { department, head_emp_id, active, leave_cover_emp_id } = req.body || {};
  if (!department || !String(department).trim()) return res.status(400).json({ error: 'Department is required' });
  await q(
    `INSERT INTO dept_approvers(department, head_emp_id, active, leave_cover_emp_id, updated_at)
     VALUES($1,$2,COALESCE($3,TRUE),$4,now())
     ON CONFLICT (department) DO UPDATE SET head_emp_id = EXCLUDED.head_emp_id,
       active = COALESCE($3, dept_approvers.active),
       leave_cover_emp_id = EXCLUDED.leave_cover_emp_id, updated_at = now()`,
    [String(department).trim(), head_emp_id || null, active, leave_cover_emp_id || null]);
  res.json({ ok: true });
});

router.put('/approvers/fallback', async (req, res) => {
  const { emp_id } = req.body || {};
  await q(
    `INSERT INTO app_settings(key, value) VALUES('outpass_fallback_emp_id', $1)
     ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value`,
    [emp_id ? String(emp_id) : null]);
  res.json({ ok: true });
});
router.put('/approvers/heads', async (req, res) => {
  const { emp_id } = req.body || {};
  await q(`INSERT INTO app_settings(key, value) VALUES('outpass_heads_emp_id', $1)
     ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value`, [emp_id ? String(emp_id) : null]);
  res.json({ ok: true });
});
router.put('/approvers/leavecover', async (req, res) => {
  const { emp_id } = req.body || {};
  await q(
    `INSERT INTO app_settings(key, value) VALUES('outpass_leavecover_emp_id', $1)
     ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value`,
    [emp_id ? String(emp_id) : null]);
  res.json({ ok: true });
});

// ---- Gate geofence + overdue settings (for outpass return tracking) ----
// OT approvers: Production → Kannan, Dispatch → Kumar N (picked from employee list).
router.get('/ot-approvers', async (req, res) => {
  const rows = (await q(`SELECT key,value FROM app_settings WHERE key IN ('ot_approver_production','ot_approver_dispatch','ot_hr_emp_id','ot_mgmt_emp_ids','ot_accounts_emp_id','ot_accounts_email','labour_accounts_emp_id','simple_home_emp_ids','accounts_email_bsc','accounts_email_g2','accounts_emp_bsc','accounts_emp_g2')`)).rows;
  const m = Object.fromEntries(rows.map(r => [r.key, r.value]));
  const people = (await q(`SELECT id,name,emp_no,department FROM employees WHERE active=TRUE ORDER BY name`)).rows;
  res.json({
    production: +m.ot_approver_production || null, dispatch: +m.ot_approver_dispatch || null,
    hr: +m.ot_hr_emp_id || null,
    mgmt: (m.ot_mgmt_emp_ids || '').split(',').map(Number).filter(Boolean),
    accounts: +m.ot_accounts_emp_id || null, accounts_email: m.ot_accounts_email || '',
    accounts_email_bsc: m.accounts_email_bsc || 'accounts@bharatsteels.in', accounts_email_g2: m.accounts_email_g2 || 'g2@bharatsteels.in',
    accounts_emp_bsc: +m.accounts_emp_bsc || null, accounts_emp_g2: +m.accounts_emp_g2 || null,
    labour_accounts: +m.labour_accounts_emp_id || null,
    simple_home: (m.simple_home_emp_ids || '').split(',').map(Number).filter(Boolean),
    people,
  });
});
router.post('/ot-approvers', async (req, res) => {
  const b = req.body || {};
  const setStr = async (key, val) => {
    if (val) await q(`INSERT INTO app_settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value`, [key, String(val)]);
    else await q(`DELETE FROM app_settings WHERE key=$1`, [key]);
  };
  await setStr('ot_approver_production', +b.production || null);
  await setStr('ot_approver_dispatch', +b.dispatch || null);
  await setStr('ot_hr_emp_id', +b.hr || null);
  await setStr('ot_mgmt_emp_ids', Array.isArray(b.mgmt) ? b.mgmt.map(Number).filter(Boolean).join(',') : null);
  await setStr('ot_accounts_emp_id', +b.accounts || null);
  await setStr('ot_accounts_email', (b.accounts_email || '').trim() || null);
  await setStr('labour_accounts_emp_id', +b.labour_accounts || null);
  await setStr('simple_home_emp_ids', Array.isArray(b.simple_home) ? b.simple_home.map(Number).filter(Boolean).join(',') : null);
  await setStr('accounts_email_bsc', (b.accounts_email_bsc || '').trim() || null);
  await setStr('accounts_email_g2', (b.accounts_email_g2 || '').trim() || null);
  await setStr('accounts_emp_bsc', +b.accounts_emp_bsc || null);
  await setStr('accounts_emp_g2', +b.accounts_emp_g2 || null);
  res.json({ ok: true });
});

// System health: one place that shows what's configured and what's silently broken.
router.get('/health', async (req, res) => {
  const wati = require('../lib/wati');
  const graph = require('../lib/graph');
  const setget = async (keys) => Object.fromEntries((await q(
    `SELECT key,value FROM app_settings WHERE key = ANY($1)`, [keys])).rows.map(r => [r.key, r.value]));

  const wa = Object.fromEntries((await q(
    `SELECT result, count(*)::int n FROM wa_log WHERE created_at > now() - interval '24 hours' GROUP BY result`)).rows.map(r => [r.result, r.n]));

  const active = (await q(`SELECT count(*)::int n FROM employees WHERE active=TRUE`)).rows[0].n;
  const badPhone = (await q(
    `SELECT count(*)::int n FROM employees WHERE active=TRUE AND (phone IS NULL OR length(regexp_replace(phone,'\\D','','g')) < 10)`)).rows[0].n;

  const ot = await setget(['ot_approver_production', 'ot_approver_dispatch', 'ot_hr_emp_id', 'ot_mgmt_emp_ids', 'ot_accounts_emp_id', 'ot_accounts_email']);
  const gate = await setget(['gate_lat', 'gate_lng', 'outpass_hr_emp_id']);
  const crons = Object.fromEntries((await q(
    `SELECT key, value FROM app_settings WHERE key LIKE 'cron_last_%'`)).rows.map(r => [r.key.replace('cron_last_', ''), r.value]));

  let expense = {};
  try { const c = await chain.getChain();
    expense = { hr_approvers: (c.hr_approver_ids || []).length, final_approvers: (c.final_approver_ids || []).length,
      accounts_email: !!c.accounts_email, entity_prefixes: Object.keys(c.accounts_email_by_prefix || {}) };
  } catch (e) { expense = { error: e.message }; }

  const opsEmail = (await q(`SELECT value FROM app_settings WHERE key='ops_alert_email'`)).rows[0]?.value || '';

  res.json({
    integrations: { wati: wati.configured(), graph: graph.configured() },
    whatsapp_24h: wa,
    phones: { active, missing_or_bad: badPhone },
    ot: {
      production: !!ot.ot_approver_production, dispatch: !!ot.ot_approver_dispatch, hr: !!ot.ot_hr_emp_id,
      management: !!(ot.ot_mgmt_emp_ids && ot.ot_mgmt_emp_ids.length), accounts_contact: !!ot.ot_accounts_emp_id, accounts_email: !!ot.ot_accounts_email,
    },
    gate: { location: !!(gate.gate_lat && gate.gate_lng), overdue_hr: !!gate.outpass_hr_emp_id },
    expense,
    crons,
    ops_alert_email: opsEmail,
    now: new Date().toISOString(),
  });
});

// Management dashboards: OT spend, expense trends, gatepass/overstay — last 6 months.
router.get('/dashboards', async (req, res) => {
  const OT_SPEND = ['approved', 'hr_verified', 'mgmt_pending', 'mgmt_approved', 'paid'];
  const EXP_DONE = ['approved', 'settled_offline'];
  const d6 = new Date(); d6.setMonth(d6.getMonth() - 5); const sixAgo = d6.toISOString().slice(0, 7);

  const otMonthly = (await q(
    `SELECT period AS ym, SUM(amount)::float AS total, COUNT(*)::int AS n
     FROM ot_entries WHERE status = ANY($1) AND period >= $2 GROUP BY period ORDER BY period`, [OT_SPEND, sixAgo])).rows;
  const otByDept = (await q(
    `SELECT COALESCE(NULLIF(department,''),'—') AS dept, SUM(amount)::float AS total
     FROM ot_entries WHERE status = ANY($1) AND period >= $2 GROUP BY department ORDER BY total DESC`, [OT_SPEND, sixAgo])).rows;

  const expMonthly = (await q(
    `SELECT to_char(COALESCE(final_at, settled_offline_at, submitted_at),'YYYY-MM') AS ym,
            SUM(total_amount)::float AS total, COUNT(*)::int AS n
     FROM expense_submissions WHERE status = ANY($1)
       AND COALESCE(final_at, settled_offline_at, submitted_at) > now() - interval '6 months'
     GROUP BY ym ORDER BY ym`, [EXP_DONE])).rows;
  const expByType = (await q(
    `SELECT form_type, SUM(total_amount)::float AS total, COUNT(*)::int AS n
     FROM expense_submissions WHERE status = ANY($1)
       AND COALESCE(final_at, settled_offline_at, submitted_at) > now() - interval '6 months'
     GROUP BY form_type ORDER BY total DESC`, [EXP_DONE])).rows;

  const gpMonthly = (await q(
    `SELECT to_char(created_at,'YYYY-MM') AS ym, COUNT(*)::int AS total,
            COUNT(*) FILTER (WHERE overdue_alert_at IS NOT NULL)::int AS overstays
     FROM outpass_requests WHERE type='gatepass' AND created_at > now() - interval '6 months'
     GROUP BY ym ORDER BY ym`)).rows;

  res.json({ ot: { monthly: otMonthly, by_dept: otByDept }, expense: { monthly: expMonthly, by_type: expByType }, gatepass: { monthly: gpMonthly } });
});

// Maintenance approval gate: which category is gated + who approves it.
router.get('/maint-gate', async (req, res) => {
  const s = Object.fromEntries((await q(
    `SELECT key,value FROM app_settings WHERE key IN ('maint_gate_category_id','maint_gate_approver_id')`)).rows.map(r => [r.key, r.value]));
  res.json({ category_id: s.maint_gate_category_id ? Number(s.maint_gate_category_id) : null,
    approver_id: s.maint_gate_approver_id ? Number(s.maint_gate_approver_id) : null });
});
router.post('/maint-gate', async (req, res) => {
  const cat = req.body && req.body.category_id ? Number(req.body.category_id) : null;
  const appr = req.body && req.body.approver_id ? Number(req.body.approver_id) : null;
  for (const [k, v] of [['maint_gate_category_id', cat], ['maint_gate_approver_id', appr]]) {
    if (v) await q(`INSERT INTO app_settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value`, [k, String(v)]);
    else await q(`DELETE FROM app_settings WHERE key=$1`, [k]);
  }
  res.json({ ok: true });
});

// Phone audit: active employees whose number is missing or not a valid WhatsApp number.
router.get('/phone-audit', async (req, res) => {
  const rows = (await q(
    `SELECT id, emp_no, name, department, phone
     FROM employees
     WHERE active=TRUE AND (phone IS NULL OR length(regexp_replace(phone,'\\D','','g')) < 10)
     ORDER BY department NULLS LAST, name`)).rows;
  res.json({ rows });
});

// Update just the phone (used by the audit tool) — doesn't touch any other field.
router.post('/employees/:id/phone', async (req, res) => {
  const phone = normPhone((req.body && req.body.phone) || '');
  if (!phone) return res.status(400).json({ error: 'Enter a valid number' });
  await q(`UPDATE employees SET phone=$2 WHERE id=$1`, [req.params.id, phone]);
  res.json({ ok: true, phone });
});

// Save the email that receives "a cron looks stopped" alerts (empty to disable).
router.post('/ops-alert-email', async (req, res) => {
  const email = String((req.body && req.body.email) || '').trim();
  if (email) await q(`INSERT INTO app_settings(key,value) VALUES('ops_alert_email',$1) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value`, [email]);
  else await q(`DELETE FROM app_settings WHERE key='ops_alert_email'`);
  res.json({ ok: true, email });
});

// WhatsApp delivery log — see every send + outcome (sent / declined / no_phone / error).
router.get('/wa-log', async (req, res) => {
  const limit = Math.min(300, +req.query.limit || 100);
  const where = []; const params = [];
  if (req.query.result) { params.push(req.query.result); where.push(`result=$${params.length}`); }
  if (req.query.template) { params.push('%' + req.query.template + '%'); where.push(`template ILIKE $${params.length}`); }
  if (req.query.phone) { params.push('%' + String(req.query.phone).replace(/\D/g, '') + '%'); where.push(`phone ILIKE $${params.length}`); }
  const rows = (await q(
    `SELECT id, phone, template, result, detail, to_char(created_at AT TIME ZONE 'Asia/Kolkata','DD Mon HH24:MI') AS at
     FROM wa_log ${where.length ? 'WHERE ' + where.join(' AND ') : ''} ORDER BY created_at DESC LIMIT ${limit}`, params)).rows;
  const summary = Object.fromEntries((await q(
    `SELECT result, count(*)::int AS n FROM wa_log WHERE created_at > now() - interval '24 hours' GROUP BY result`)).rows.map(r => [r.result, r.n]));
  res.json({ rows, summary });
});

router.get('/gate-settings', async (req, res) => {
  res.json(await readGateSettings(true));
});
router.put('/gate-settings', async (req, res) => {
  const b = req.body || {};
  const keys = ['lat', 'lng', 'radius_m', 'overdue_min', 'hr_emp_id', 'multiday_hours'];
  const present = keys.filter((k) => k in b);
  if (!present.length) return res.status(400).json({ error: 'No settings received — the form sent an empty request. Hard-refresh (Ctrl+Shift+R) and try again.' });
  const set = async (k, v) => q(
    `INSERT INTO app_settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value`,
    [k, v == null || v === '' ? null : String(v)]);
  const map = { lat: 'gate_lat', lng: 'gate_lng', radius_m: 'gate_radius_m', overdue_min: 'outpass_overdue_min', hr_emp_id: 'outpass_hr_emp_id', multiday_hours: 'outpass_multiday_hours' };
  try {
    for (const k of present) await set(map[k], b[k]);
  } catch (e) {
    console.error('[gate-settings save] failed:', e.code, e.message);
    return res.status(500).json({ error: 'Database refused the save: ' + (e.detail || e.message) });
  }
  // Read straight back from the DB in the SAME request, so the response reflects what is
  // actually stored — not just "the write was sent". This removes any ambiguity about persistence.
  const saved = await readGateSettings(false);
  const ok = ('lat' in b) ? (saved.lat != null && saved.lng != null) : true;
  // Also report WHICH database this write actually landed in, so it can be compared against the
  // database read elsewhere. If two requests report different hosts, the app is talking to >1 DB.
  let dbHost = null, dbName = null;
  try { const u = new URL(process.env.DATABASE_URL || ''); dbHost = u.host; } catch {}
  try { dbName = (await q(`SELECT current_database() AS d`)).rows[0].d; } catch {}
  res.json({ ok, saved, db: { host: dbHost, name: dbName } });
});

// Shared reader. withEmployees=true also returns the HR dropdown list.
async function readGateSettings(withEmployees) {
  const rows = (await q(`SELECT key, value FROM app_settings WHERE key IN
    ('gate_lat','gate_lng','gate_radius_m','outpass_overdue_min','outpass_hr_emp_id','outpass_multiday_hours')`)).rows;
  const m = Object.fromEntries(rows.map((r) => [r.key, r.value]));
  const out = {
    lat: m.gate_lat != null ? Number(m.gate_lat) : null,
    lng: m.gate_lng != null ? Number(m.gate_lng) : null,
    radius_m: m.gate_radius_m != null ? Number(m.gate_radius_m) : 150,
    overdue_min: m.outpass_overdue_min != null ? Number(m.outpass_overdue_min) : 5,
    hr_emp_id: m.outpass_hr_emp_id ? Number(m.outpass_hr_emp_id) : null,
    multiday_hours: m.outpass_multiday_hours != null ? Number(m.outpass_multiday_hours) : 16,
  };
  if (withEmployees) out.employees = (await q(`SELECT id, name, emp_no FROM employees WHERE active=TRUE ORDER BY name`)).rows;
  return out;
}

// ---- Diagnostics: which database is the app ACTUALLY connected to right now ----
// Read-only. Lets us compare the app's live DB against the one you're querying in the console —
// if they differ, writes "succeed" into a DB you're not looking at.
router.get('/db-info', async (req, res) => {
  const raw = process.env.DATABASE_URL || '';
  let host = null, dbname = null, user = null;
  try { const u = new URL(raw); host = u.host; dbname = u.pathname.replace(/^\//, ''); user = u.username; } catch {}
  const out = { build: 'FIXED221', env_host: host, env_dbname: dbname, env_user: user };
  try {
    const r = (await q(`SELECT current_database() AS db, current_user AS usr,
      inet_server_addr()::text AS server_ip, now() AS now`)).rows[0];
    Object.assign(out, r);
    out.app_settings_count = (await q(`SELECT COUNT(*)::int AS n FROM app_settings`)).rows[0].n;
    out.gate_keys = (await q(`SELECT key, value FROM app_settings WHERE key LIKE 'gate_%' OR key LIKE 'outpass_%' ORDER BY key`)).rows;
    // CROSS-REQUEST PERSISTENCE TEST (the correct one):
    // 1) Read whatever the PREVIOUS db-info call wrote. If it's here, writes survive across requests.
    // 2) Then write a NEW marker for the next call to find.
    const prev = (await q(`SELECT value FROM app_settings WHERE key='__diag_probe'`)).rows[0];
    out.previous_probe = prev ? prev.value : null;
    out.previous_probe_found = !!prev;
    const probe = 'probe-' + new Date().toISOString() + '-' + Math.random().toString(36).slice(2, 8);
    await q(`INSERT INTO app_settings(key,value) VALUES('__diag_probe',$1)
             ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value`, [probe]);
    out.new_probe = probe;
  } catch (e) { out.error = e.message; }
  res.json(out);
});


router.get('/outpass-export.xlsx', async (req, res) => {
  try {
    const buf = await require('../lib/outpass_excel').buildOutpassWorkbook();
    res.setHeader('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
    res.setHeader('Content-Disposition', 'attachment; filename="Outpass Log.xlsx"');
    res.send(buf);
  } catch (e) { console.error('outpass export', e); res.status(500).json({ error: 'Export failed' }); }
});

// ===================== EXPENSE POLICY & CATEGORY =====================
router.get('/expense-policy', async (req, res) => {
  const { rows } = await q('SELECT key, value FROM expense_policy');
  const m = {}; for (const r of rows) m[r.key] = Number(r.value);
  res.json(m);
});
router.put('/expense-policy', async (req, res) => {
  const allowed = ['rate_bike', 'rate_car', 'cat1_food', 'cat1_accom', 'cat2_food', 'cat2_accom', 'conveyance_log_hours'];
  for (const k of allowed) {
    if (req.body[k] != null && !isNaN(parseFloat(req.body[k]))) {
      await q(`INSERT INTO expense_policy(key,value) VALUES($1,$2)
               ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value`, [k, parseFloat(req.body[k])]);
    }
  }
  require('../lib/expense_policy').invalidate();
  res.json({ ok: true });
});

// ----- Expense approval chain (HR approvers, final approvers, accounts) -----
const chain = require('../lib/chain');
router.get('/expense-chain', async (req, res) => {
  const c = await chain.getChain();
  const ids = [...c.hr_approver_ids, ...c.final_approver_ids, ...(c.accounts_notify_id ? [c.accounts_notify_id] : []), ...(c.cmd_notify_id ? [c.cmd_notify_id] : [])];
  const employees_ref = ids.length ? (await q('SELECT id,name,emp_no FROM employees WHERE id=ANY($1)', [ids])).rows : [];
  res.json({ ...c, employees_ref });
});
router.put('/expense-chain', async (req, res) => {
  const b = req.body || {};
  await chain.setChain({
    hr_approver_ids: Array.isArray(b.hr_approver_ids) ? b.hr_approver_ids.map(Number) : [],
    final_approver_ids: Array.isArray(b.final_approver_ids) ? b.final_approver_ids.map(Number) : [],
    accounts_email: (b.accounts_email || '').trim() || 'accounts@bharatsteels.in',
    accounts_notify_id: b.accounts_notify_id ? Number(b.accounts_notify_id) : null,
    cmd_notify_id: b.cmd_notify_id ? Number(b.cmd_notify_id) : null,
    accounts_email_by_prefix: (() => { const o = b.accounts_email_by_prefix; const out = {};
      if (o && typeof o === 'object') for (const k of Object.keys(o)) { const key = String(k).trim().toUpperCase(); const val = String(o[k] || '').trim(); if (key && val) out[key] = val; }
      return out; })(),
  });
  res.json({ ok: true });
});
// Set an employee's expense category (CAT1 / CAT2).
router.put('/employees/:id/expense-category', async (req, res) => {
  const cat = req.body.category === 'CAT1' ? 'CAT1' : 'CAT2';
  await q('UPDATE employees SET expense_category=$2 WHERE id=$1', [req.params.id, cat]);
  res.json({ ok: true, category: cat });
});

// Month-end submit lock toggle (per form). anytime=true lifts the lock.
router.get('/expense-gate', async (req, res) => {
  let g = { conveyance_anytime: false, outstation_anytime: false, min_cycle: '2026-07' };
  try { const r = await q(`SELECT value FROM app_settings WHERE key='expense_gate'`); if (r.rows[0]) g = { ...g, ...JSON.parse(r.rows[0].value) }; } catch {}
  res.json(g);
});
router.put('/expense-gate', async (req, res) => {
  const g = { conveyance_anytime: !!(req.body && req.body.conveyance_anytime), outstation_anytime: !!(req.body && req.body.outstation_anytime), min_cycle: (req.body && /^\d{4}-\d{2}$/.test(req.body.min_cycle)) ? req.body.min_cycle : '2026-07' };
  await q(`INSERT INTO app_settings(key,value) VALUES('expense_gate',$1)
    ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value`, [JSON.stringify(g)]);
  res.json({ ok: true, ...g });
});

module.exports = router;
