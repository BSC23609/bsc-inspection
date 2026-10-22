// Expense Reimbursement — module routes.
// Phase 1: Local Travel Conveyance (monthly draft → month-gated submit → 1-page
// PDF → OneDrive + HR email → portal approval). Outstation & Miscellaneous next.
const express = require('express');
const crypto = require('crypto');
const { q } = require('../lib/db');
const auth = require('../lib/auth');
const graph = require('../lib/graph');
const { background } = require('../lib/bg');
const { getPolicy, catOf } = require('../lib/expense_policy');
const { buildConveyancePDF } = require('../lib/expense_pdf');
const wati = require('../lib/wati');
const chain = require('../lib/chain');
const report = require('../lib/expense_report');

const FORM_LABEL = { conveyance: 'Local Conveyance', outstation: 'Outstation Travel', misc: 'Miscellaneous' };
const STAGE_LABEL = { hr: 'HR review', final: 'final approval' };

async function empById(id) { if (!id) return null; return (await q('SELECT id,name,phone,emp_no,email FROM employees WHERE id=$1', [id])).rows[0] || null; }
// TEMP test CC for CMD WhatsApp — set env CMD_TEST_PHONE (e.g. 7395956648); unset to disable.
const CMD_TEST_PHONE = (() => { const d = String(process.env.CMD_TEST_PHONE || '').replace(/\D/g, ''); return d ? (d.length === 10 ? '91' + d : d) : null; })();
async function isHrApprover(u) { if (u.is_admin) return true; const c = await chain.getChain(); return c.hr_approver_ids.includes(u.id); }
async function isAccounts(u) { if (u.is_admin) return true; const c = await chain.getChain(); return !!(c.accounts_notify_id && u.id === c.accounts_notify_id); }
function chainSummary(full) { return { id: full.id, ref_no: full.ref_no, emp_name: full.emp_name, form_label: FORM_LABEL[full.form_type] || full.form_type, period_label: full.period ? monthLabel(full.period) : '\u2014', total_label: fmtMoney(full.total_amount), pdf_token: full.pdf_token }; }
function chainPdfName(full) { const lbl = FORM_LABEL[full.form_type] || full.form_type; const per = full.period ? (' ' + monthLabel(full.period)) : ''; return `${full.emp_name} - ${lbl}${per} (${full.ref_no.replace(/\//g, '-')}).pdf`; }
async function pdfFor(full, status, approver, at) {
  if (full.form_type === 'conveyance') return conveyancePdf(full, status, approver, at);
  if (full.form_type === 'outstation') return outstationPdf(full, status, approver, at);
  return miscPdf(full, status, approver, at);
}
async function uploadChainPdf(full, status, approver, at) {
  const pdf = await pdfFor(full, status, approver, at);
  const url = await graph.uploadExpensePdf(chainPdfName(full), pdf);
  if (url && url !== true) await q(`UPDATE expense_submissions SET pdf_url=$2 WHERE id=$1`, [full.id, url]);
  return pdf;
}
// Move a submission to the HR stage and notify the configured HR approvers.
async function enterChain(rowId) {
  await q(`UPDATE expense_submissions SET status='pending_hr', submitted_at=now(),
    pdf_token=COALESCE(pdf_token,$2),
    hr_by_id=NULL, hr_by_name=NULL, hr_at=NULL, final_approver_id=NULL, final_by_name=NULL, final_at=NULL,
    return_reason=NULL, return_stage=NULL WHERE id=$1`, [rowId, crypto.randomBytes(12).toString('hex')]);
  background((async () => {
    const c = await chain.getChain();
    const full = await loadRow(rowId);
    await uploadChainPdf(full, 'pending');
    for (const hid of c.hr_approver_ids) { const hr = await empById(hid); if (hr && hr.phone) await wati.notify.expense.submitted(hr, chainSummary(full)); }
  })());
}
async function returnToEmployee(row, stage, reason, byName) {
  await q(`UPDATE expense_submissions SET status='returned', return_reason=$2, return_stage=$3, reviewed_by_name=$4, reviewed_at=now() WHERE id=$1`,
    [row.id, reason, stage, byName]);
  await q(`UPDATE conveyance_trips SET claim_ref=NULL WHERE claim_ref=$1`, [row.ref_no]); // free the trips to be edited/resubmitted
  background((async () => {
    const full = await loadRow(row.id);
    const emp = await empById(full.employee_id);
    if (emp && emp.phone) await wati.notify.expense.returned(emp, { ...chainSummary(full), stage_label: STAGE_LABEL[stage] || stage, reason });
  })());
}

const router = express.Router();
router.use(auth.requireAuth);
function requireHR(req, res, next) { if (!req.user.is_admin) return res.status(403).json({ error: 'HR only' }); next(); }

const HR_EMAIL = process.env.EXPENSE_HR_EMAIL || 'hr@bharatsteels.in';
const catLabel = (c) => (c === 'CAT1' ? 'Category 1' : 'Category 2');
// ===== 25th–25th claim cycle. Period key 'YYYY-MM' = the cycle CLOSING in that month.
// e.g. '2026-06' (June cycle) covers 26 May 2026 → 25 Jun 2026 inclusive. Submit opens the 26th.
const CUTOVER_PERIOD = '2026-07';                 // new cycle system goes live 1 Jul 2026
const CUTOVER_START = new Date(2026, 6, 1);       // 1 Jul 2026 (June-30 and earlier stay old-system)
function cycleRange(period) { const [y, m] = period.split('-').map(Number);
  if (period < CUTOVER_PERIOD) return { start: new Date(y, m - 1, 1), end: new Date(y, m, 0) }; // pre-cutover = calendar month
  const start = period === CUTOVER_PERIOD ? new Date(CUTOVER_START) : new Date(y, m - 2, 26);
  return { start, end: new Date(y, m - 1, 25) }; }
function cycleOf(d) { d = new Date(d); let y = d.getFullYear(), m = d.getMonth(); if (d.getDate() >= 26) { m++; if (m > 11) { m = 0; y++; } } return `${y}-${String(m + 1).padStart(2, '0')}`; }
function inCycle(period, dateStr) { if (!dateStr) return false; const { start, end } = cycleRange(period); const d = new Date(String(dateStr).slice(0,10) + 'T00:00:00'); const s = new Date(start.getFullYear(), start.getMonth(), start.getDate()); const e = new Date(end.getFullYear(), end.getMonth(), end.getDate()); return d >= s && d <= e; }
const monthLabel = (period) => { const { start, end } = cycleRange(period); const f = d => d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }); return `${f(start)} – ${f(end)} ${end.getFullYear()}`; };
const fmtMoney = (n) => '₹' + Number(n || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtDateTime = (d) => new Date(d).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
// Submit unlocks on the 1st of the month AFTER the claim's period.
function canSubmit(period) { if (!period) return false; const [y, m] = period.split('-').map(Number); return new Date() >= new Date(y, m - 1, 26); } // opens the 26th (day after the 25th close)
// Admins can lift the month-end submit lock per form (handy for testing).
async function submitAllowed(formType, period) {
  let g = {};
  try { const r = await q(`SELECT value FROM app_settings WHERE key='expense_gate'`); if (r.rows[0]) g = JSON.parse(r.rows[0].value); } catch {}
  if (formType === 'conveyance' && g.conveyance_anytime) return true;
  if (formType === 'outstation' && g.outstation_anytime) return true;
  return canSubmit(period);
}

async function refNo(prefix) {
  const now = new Date();
  const dp = `${String(now.getFullYear()).slice(2)}${String(now.getMonth() + 1).padStart(2, '0')}${String(now.getDate()).padStart(2, '0')}`;
  const base = `EXP/BSC/${prefix}/${dp}/`;
  const { rows } = await q(
    `SELECT COALESCE(MAX(NULLIF(split_part(ref_no, '/', 5), '')::int), 0) AS mx
     FROM expense_submissions WHERE ref_no LIKE $1`, [`${base}%`]);
  return `${base}${String(rows[0].mx + 1).padStart(3, '0')}`;
}

// Unified final-approval authority: the management team (Admin → OT approvers → Management
// approvers). No admin bypass — only these people can final-approve expense / OT / labour.
async function managementIds() {
  return ((await q(`SELECT value FROM app_settings WHERE key='ot_mgmt_emp_ids'`)).rows[0]?.value || '').split(',').map(Number).filter(Boolean);
}
async function isManagement(u) { return (await managementIds()).includes(u.id); }

async function loadRow(id) {
  return (await q(`SELECT s.*, e.name AS emp_name, e.emp_no AS emp_code, e.job_title AS designation,
    e.expense_category, e.email AS emp_email FROM expense_submissions s
    JOIN employees e ON e.id = s.employee_id WHERE s.id = $1`, [id])).rows[0];
}
// An entry is "late" if it was first logged more than `hours` after its trip date (IST).
function entryIsLate(dateStr, loggedAt, hours) {
  if (!dateStr || !loggedAt || !hours) return false;
  const trip = new Date(dateStr + 'T00:00:00+05:30').getTime();
  return (new Date(loggedAt).getTime() - trip) > hours * 3600000;
}
async function conveyancePdf(row, status, approver, approvedAt) {
  const pol = await getPolicy();
  const cat = row.expense_category === 'CAT1' ? 'CAT1' : 'CAT2';
  const hours = pol.log_hours;
  const entries = ((row.payload && row.payload.entries) || []).map(e => ({ ...e, late: entryIsLate(e.date, e.logged_at, hours) }));
  return buildConveyancePDF({
    ref_no: row.ref_no, emp_name: row.emp_name, emp_code: row.emp_code, designation: row.designation || '',
    category: catLabel(cat), period_label: monthLabel(row.period), vehicle_rates: pol.rates,
    entries, total: Number(row.total_amount || 0), log_hours: hours,
    status, approver, approved_at: approvedAt ? fmtDateTime(approvedAt) : '',
  });
}
function emailHtml(title, row, count) {
  return `<div style="font-family:Arial,sans-serif;color:#112532">
    <h2 style="margin:0 0 4px">${title}</h2>
    <p style="color:#6b7c8c;margin:0 0 16px">A new claim has been submitted for your approval on the BSC portal.</p>
    <table style="border-collapse:collapse;font-size:14px">
      <tr><td style="padding:4px 16px 4px 0;color:#6b7c8c">Employee</td><td><b>${row.emp_name}</b> (${row.emp_code})</td></tr>
      <tr><td style="padding:4px 16px 4px 0;color:#6b7c8c">Reference</td><td>${row.ref_no}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;color:#6b7c8c">Period</td><td>${row.period ? monthLabel(row.period) : '\u2014'}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;color:#6b7c8c">Entries</td><td>${count}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;color:#6b7c8c">Total</td><td><b>${fmtMoney(row.total_amount)}</b></td></tr>
    </table>
    <p style="margin:16px 0 0;font-size:13px;color:#6b7c8c">The full claim PDF is attached. Approve or reject it on the portal.</p>
  </div>`;
}

// ---------------- meta ----------------
router.get('/meta', async (req, res) => {
  const pol = await getPolicy();
  const cat = catOf(req.user);
  const mgr = await resolveManager(req.user.id);
  const c = await chain.getChain();
  let min_cycle = '2026-07';
  try { const r = await q(`SELECT value FROM app_settings WHERE key='expense_gate'`); if (r.rows[0]) { const g = JSON.parse(r.rows[0].value); if (g.min_cycle) min_cycle = g.min_cycle; } } catch {}
  res.json({ rates: pol.rates, limits: pol.limits, category: cat, category_label: catLabel(cat), min_cycle,
    reporting_manager: mgr ? mgr.name : null, conveyance_log_hours: pol.log_hours,
    is_hr_approver: req.user.is_admin || c.hr_approver_ids.includes(req.user.id),
    is_final_approver: await isManagement(req.user),
    is_accounts: req.user.is_admin || !!(c.accounts_notify_id && req.user.id === c.accounts_notify_id),
    is_trip_manager: await isTripManager(req.user),
    me: { emp_no: req.user.emp_no, name: req.user.name, designation: req.user.job_title || '' }, hr_email: HR_EMAIL });
});

// Resolve an employee's active reporting manager (or null).
async function resolveManager(empId) {
  return (await q(`SELECT rm.id, rm.name, rm.phone, rm.emp_no FROM employees e
    JOIN employees rm ON rm.id = e.reporting_manager_emp_id
    WHERE e.id = $1 AND rm.active = TRUE`, [empId])).rows[0] || null;
}
const tripDateLabel = (s) => { if (!s) return ''; const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d).toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' }); };
const tripRoute = (t) => [t.from_loc, t.to_loc].filter(Boolean).join(' \u2192 ') || (t.purpose || 'Trip');
function sendTripToManager(trip, mgr, requester) {
  background((async () => {
    if (!mgr || !mgr.phone) return;
    await wati.notify.conveyance.request(mgr, {
      requester, date_label: tripDateLabel(trip.trip_date), route: tripRoute(trip),
      amount_label: fmtMoney(trip.amount), action_token: trip.action_token });
  })());
}
const TRIP_COLS = `id, employee_id, to_char(trip_date,'YYYY-MM-DD') AS trip_date, from_loc, to_loc, purpose,
  vehicle, km, rate, amount, status, approver_name, reject_reason, reviewed_at, action_token, logged_at, claim_ref`;

// Get-or-create the month container + the month's trips.
router.get('/conveyance/:period', async (req, res) => {
  const period = req.params.period;
  if (!/^\d{4}-\d{2}$/.test(period)) return res.status(400).json({ error: 'Bad period' });
  // Self-heal: if this employee no longer needs manager approval, clear any leftover
  // "awaiting manager" trips (e.g. logged before the setting was turned off).
  if (req.user.conveyance_needs_manager === false) {
    await q(`UPDATE conveyance_trips SET status='approved', reviewed_at=now(),
             approver_emp_id=NULL, approver_name=NULL, action_token=NULL
             WHERE employee_id=$1 AND period=$2 AND status='pending' AND claim_ref IS NULL`,
      [req.user.id, period]);
  }
  // Working draft (created lazily at submit-time). May not exist yet — that's fine.
  const row = (await q(`SELECT * FROM expense_submissions WHERE employee_id=$1 AND form_type='conveyance' AND period=$2 AND status IN ('draft','returned') ORDER BY id DESC LIMIT 1`, [req.user.id, period])).rows[0] || null;
  // Prior claims for this month (submitted / paid). The month stays open alongside these.
  const claims = (await q(`SELECT id, ref_no, status, total_amount, pdf_token, submitted_at FROM expense_submissions
    WHERE employee_id=$1 AND form_type='conveyance' AND period=$2 AND status IN ('pending_hr','pending_final','approved') ORDER BY id`, [req.user.id, period])).rows;
  const claimStatus = {}; for (const c of claims) claimStatus[c.ref_no] = c.status;
  const pol = await getPolicy();
  const trips = (await q(`SELECT ${TRIP_COLS} FROM conveyance_trips WHERE employee_id=$1 AND period=$2 ORDER BY trip_date, id`, [req.user.id, period])).rows
    .map(t => ({ ...t, late: entryIsLate(t.trip_date, t.logged_at, pol.log_hours),
      claimed: !!t.claim_ref, claim_status: t.claim_ref ? (claimStatus[t.claim_ref] || 'submitted') : null }));
  const mgr = await resolveManager(req.user.id);
  const allow = await submitAllowed('conveyance', period);
  const openTrips = trips.filter(t => !t.claimed);           // "new" trips = not yet in any claim
  res.json({
    period, period_label: monthLabel(period),
    can_submit: allow && openTrips.some(t => t.status === 'approved'),
    submission: row ? { id: row.id, ref_no: row.ref_no, status: row.status, total_amount: row.total_amount,
      pdf_token: row.pdf_token, return_reason: row.return_reason, return_stage: row.return_stage } : null,
    claims,
    manager: mgr ? { name: mgr.name } : null,
    needs_manager: req.user.conveyance_needs_manager !== false,
    log_hours: pol.log_hours,
    pending_count: openTrips.filter(t => t.status === 'pending').length,
    trips,
  });
});

// Add one trip → send to the reporting manager (or auto-approve if that's off for this employee).
router.post('/conveyance/:period/trip', async (req, res) => {
  const period = req.params.period;
  if (!/^\d{4}-\d{2}$/.test(period)) return res.status(400).json({ error: 'Bad period' });
  const b = req.body || {};
  const km = parseFloat(b.km); const veh = b.vehicle === 'car' ? 'car' : 'bike';
  if (!b.date) return res.status(400).json({ error: 'Pick the trip date.' });
  if (!inCycle(period, b.date)) { const { start, end } = cycleRange(period); return res.status(400).json({ error: `Trip date must be within this cycle (${start.toLocaleDateString('en-IN')} – ${end.toLocaleDateString('en-IN')}).` }); }
  if (!(km > 0)) return res.status(400).json({ error: 'Enter the distance in km.' });
  const pol = await getPolicy();
  const rate = pol.rates[veh]; const amount = +(km * rate).toFixed(2);
  const needsMgr = req.user.conveyance_needs_manager !== false;

  if (!needsMgr) {
    // No manager approval required for this employee — log the trip as approved straight away.
    const trip = (await q(`INSERT INTO conveyance_trips
      (employee_id,period,trip_date,from_loc,to_loc,purpose,vehicle,km,rate,amount,status,reviewed_at)
      VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'approved',now())
      RETURNING ${TRIP_COLS}`,
      [req.user.id, period, b.date, (b.from || '').trim(), (b.to || '').trim(), (b.purpose || '').trim(),
       veh, km, rate, amount])).rows[0];
    return res.json({ ok: true, trip, auto: true });
  }

  const mgr = await resolveManager(req.user.id);
  if (!mgr) return res.status(400).json({ error: 'No reporting manager assigned. Ask an admin to set yours, or turn off manager approval for you in the employee directory.' });
  const token = crypto.randomBytes(20).toString('hex');
  const trip = (await q(`INSERT INTO conveyance_trips
    (employee_id,period,trip_date,from_loc,to_loc,purpose,vehicle,km,rate,amount,status,approver_emp_id,approver_name,action_token)
    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'pending',$11,$12,$13)
    RETURNING ${TRIP_COLS}`,
    [req.user.id, period, b.date, (b.from || '').trim(), (b.to || '').trim(), (b.purpose || '').trim(),
     veh, km, rate, amount, mgr.id, mgr.name, token])).rows[0];
  sendTripToManager(trip, mgr, req.user.name);
  res.json({ ok: true, trip, approver: mgr.name, notified: Boolean(mgr.phone) });
});

// Edit a rejected trip and re-send it (pending/approved trips are locked).
router.put('/conveyance/trip/:id', async (req, res) => {
  const t = (await q('SELECT * FROM conveyance_trips WHERE id=$1', [req.params.id])).rows[0];
  if (!t || t.employee_id !== req.user.id) return res.status(403).json({ error: 'Not allowed' });
  if (t.claim_ref) return res.status(409).json({ error: 'This trip is already part of a submitted claim and is locked.' });
  if (!['rejected', 'approved'].includes(t.status)) return res.status(409).json({ error: 'Only rejected or approved trips can be edited.' });
  const b = req.body || {};
  const km = parseFloat(b.km); const veh = b.vehicle === 'car' ? 'car' : 'bike';
  if (!b.date || !(km > 0)) return res.status(400).json({ error: 'Date and km are required.' });
  if (!inCycle(t.period, b.date)) return res.status(400).json({ error: 'Trip date is outside this cycle.' });
  const pol = await getPolicy();
  const rate = pol.rates[veh]; const amount = +(km * rate).toFixed(2);
  const needsMgr = req.user.conveyance_needs_manager !== false;
  if (!needsMgr) {
    // Employee doesn't need manager approval — keep the edited trip approved (don't send back to a manager).
    const trip = (await q(`UPDATE conveyance_trips SET trip_date=$2,from_loc=$3,to_loc=$4,purpose=$5,vehicle=$6,
      km=$7,rate=$8,amount=$9,status='approved',approver_emp_id=NULL,approver_name=NULL,reviewed_at=now(),
      reject_reason=NULL,action_token=NULL,updated_at=now() WHERE id=$1 RETURNING ${TRIP_COLS}`,
      [t.id, b.date, (b.from || '').trim(), (b.to || '').trim(), (b.purpose || '').trim(), veh, km, rate, amount])).rows[0];
    return res.json({ ok: true, trip, auto: true });
  }
  const mgr = await resolveManager(req.user.id);
  if (!mgr) return res.status(400).json({ error: 'No reporting manager assigned.' });
  const token = crypto.randomBytes(20).toString('hex');
  const trip = (await q(`UPDATE conveyance_trips SET trip_date=$2,from_loc=$3,to_loc=$4,purpose=$5,vehicle=$6,
    km=$7,rate=$8,amount=$9,status='pending',approver_emp_id=$10,approver_name=$11,reviewed_at=NULL,
    reject_reason=NULL,action_token=$12,updated_at=now() WHERE id=$1 RETURNING ${TRIP_COLS}`,
    [t.id, b.date, (b.from || '').trim(), (b.to || '').trim(), (b.purpose || '').trim(), veh, km, rate, amount,
     mgr.id, mgr.name, token])).rows[0];
  sendTripToManager(trip, mgr, req.user.name);
  res.json({ ok: true, trip, notified: Boolean(mgr.phone) });
});

// Withdraw a trip. Allowed until it's part of a submitted claim (claim_ref set) — even if manager-approved.
router.delete('/conveyance/trip/:id', async (req, res) => {
  const t = (await q('SELECT * FROM conveyance_trips WHERE id=$1', [req.params.id])).rows[0];
  if (!t || t.employee_id !== req.user.id) return res.status(403).json({ error: 'Not allowed' });
  if (t.claim_ref) return res.status(409).json({ error: 'This trip is already part of a submitted claim and is locked.' });
  await q('DELETE FROM conveyance_trips WHERE id=$1', [t.id]);
  res.json({ ok: true });
});

// Submit the month into the HR payment chain (snapshots ALL the month's trips).
router.post('/conveyance/:period/submit', async (req, res) => {
  const period = req.params.period;
  if (!/^\d{4}-\d{2}$/.test(period)) return res.status(400).json({ error: 'Bad period' });
  if (!(await submitAllowed('conveyance', period))) return res.status(400).json({ error: 'This month can be submitted from the 1st of next month.' });
  const trips = (await q(`SELECT ${TRIP_COLS} FROM conveyance_trips WHERE employee_id=$1 AND period=$2 ORDER BY trip_date, id`, [req.user.id, period])).rows;
  const open = trips.filter(t => !t.claim_ref); // not yet in any claim
  if (!open.length) return res.status(400).json({ error: 'No new trips to submit — everything here is already in a claim.' });
  const stillPending = open.filter(t => t.status === 'pending').length;
  if (stillPending) return res.status(400).json({ error: `${stillPending} new trip(s) are still awaiting manager approval. All must be approved before submitting.` });
  const claimTrips = open.filter(t => t.status === 'approved');
  if (!claimTrips.length) return res.status(400).json({ error: 'None of the new trips were approved, so there is nothing to submit.' });
  // Reuse a working draft, or create a fresh claim row for this batch.
  let row = (await q(`SELECT * FROM expense_submissions WHERE employee_id=$1 AND form_type='conveyance' AND period=$2 AND status IN ('draft','returned') ORDER BY id DESC LIMIT 1`, [req.user.id, period])).rows[0];
  if (!row) {
    const ref = await refNo('CNV');
    row = (await q(`INSERT INTO expense_submissions(ref_no,employee_id,form_type,period,payload,status)
      VALUES($1,$2,'conveyance',$3,$4,'draft') RETURNING *`, [ref, req.user.id, period, JSON.stringify({ entries: [] })])).rows[0];
  }
  let total = 0;
  const entries = claimTrips.map(t => {
    total += Number(t.amount || 0);
    return { date: t.trip_date, from: t.from_loc || '', to: t.to_loc || '', purpose: t.purpose || '',
      vehicle: t.vehicle, vehicle_label: t.vehicle === 'car' ? 'Car' : 'Bike', km: Number(t.km), rate: Number(t.rate),
      amount: Number(t.amount), logged_at: t.logged_at, mgr_status: t.status, mgr_reason: t.reject_reason || '',
      approver: t.approver_name || '' };
  });
  await q(`UPDATE expense_submissions SET payload=$2, total_amount=$3, updated_at=now() WHERE id=$1`,
    [row.id, JSON.stringify({ entries }), +total.toFixed(2)]);
  // Lock these trips to this claim so they can't be edited or re-claimed.
  await q(`UPDATE conveyance_trips SET claim_ref=$3, updated_at=now() WHERE employee_id=$1 AND id = ANY($2)`,
    [req.user.id, claimTrips.map(t => t.id), row.ref_no]);
  await enterChain(row.id);
  res.json({ ok: true, claimed: claimTrips.length });
});

// Employee recalls their OWN month while it's still awaiting HR (before HR acts on it).
// Reverts to draft so they can add/edit trips and submit again. Blocked once HR has moved it on.
router.post('/conveyance/:period/recall', async (req, res) => {
  const period = req.params.period;
  if (!/^\d{4}-\d{2}$/.test(period)) return res.status(400).json({ error: 'Bad period' });
  const row = (await q(`SELECT * FROM expense_submissions WHERE employee_id=$1 AND form_type='conveyance' AND period=$2 AND status='pending_hr' ORDER BY id DESC LIMIT 1`, [req.user.id, period])).rows[0];
  if (!row) return res.status(404).json({ error: 'No claim is awaiting HR to recall.' });
  await q(`UPDATE expense_submissions SET status='draft', submitted_at=NULL, updated_at=now() WHERE id=$1`, [row.id]);
  await q(`UPDATE conveyance_trips SET claim_ref=NULL, updated_at=now() WHERE employee_id=$1 AND claim_ref=$2`, [req.user.id, row.ref_no]);
  res.json({ ok: true });
});

// One-tap helpers (used by the public /cva /cvr endpoints) — operate on a single trip.
async function loadTripByToken(token) {
  return (await q(`SELECT t.*, to_char(t.trip_date,'YYYY-MM-DD') AS trip_date_s,
    e.name AS emp_name, e.phone AS emp_phone
    FROM conveyance_trips t JOIN employees e ON e.id = t.employee_id
    WHERE t.action_token = $1`, [token])).rows[0] || null;
}
async function applyTripApprove(t) {
  await q(`UPDATE conveyance_trips SET status='approved', reviewed_at=now(), action_token=NULL WHERE id=$1`, [t.id]);
  background((async () => {
    if (t.emp_phone) await wati.notify.conveyance.approved({ name: t.emp_name, phone: t.emp_phone },
      { date_label: tripDateLabel(t.trip_date_s), route: tripRoute(t), approver_name: t.approver_name || 'your manager' });
  })());
}
async function applyTripReject(t, reason) {
  await q(`UPDATE conveyance_trips SET status='rejected', reject_reason=$2, reviewed_at=now(), action_token=NULL WHERE id=$1`, [t.id, reason || null]);
  background((async () => {
    if (t.emp_phone) await wati.notify.conveyance.rejected({ name: t.emp_name, phone: t.emp_phone },
      { date_label: tripDateLabel(t.trip_date_s), route: tripRoute(t), approver_name: t.approver_name || 'your manager', reason: reason || '' });
  })());
}

// ================= Manager trip queue (in-app approval) =================
// Managers approve/reject their team's conveyance trips inside the portal, so approvals no
// longer depend on the one-tap WhatsApp link being received and tapped. Admins can act on
// any trip (override), and every action records who really did it.
function requireAdminUser(req, res, next) { if (!req.user.is_admin) return res.status(403).json({ error: 'Admin only' }); next(); }

const PENDING_TRIP_COLS = `t.id, t.period, to_char(t.trip_date,'YYYY-MM-DD') AS trip_date,
  t.from_loc, t.to_loc, t.purpose, t.vehicle, t.km, t.rate, t.amount, t.status,
  t.logged_at, t.approver_emp_id, t.approver_name, (t.action_token IS NOT NULL) AS has_link,
  e.id AS emp_id, e.name AS emp_name, e.emp_no AS emp_no`;

const decorateTrip = (t) => ({ ...t, route: tripRoute(t), date_label: tripDateLabel(t.trip_date),
  period_label: t.period ? monthLabel(t.period) : '', amount_label: fmtMoney(t.amount) });

// Show the queue to anyone who is an active person's reporting manager, or who already has
// trips parked on them (covers managers unset after trips were logged).
async function isTripManager(u) {
  if (u.is_admin) return true;
  const r = await q(`SELECT 1 FROM employees WHERE reporting_manager_emp_id=$1 AND active=TRUE LIMIT 1`, [u.id]);
  if (r.rows.length) return true;
  const p = await q(`SELECT 1 FROM conveyance_trips WHERE approver_emp_id=$1 AND status='pending' LIMIT 1`, [u.id]);
  return p.rows.length > 0;
}

async function loadTripForAction(id) {
  return (await q(`SELECT t.*, to_char(t.trip_date,'YYYY-MM-DD') AS trip_date_s,
    e.name AS emp_name, e.phone AS emp_phone
    FROM conveyance_trips t JOIN employees e ON e.id = t.employee_id WHERE t.id=$1`, [id])).rows[0] || null;
}
const canActOnTrip = (u, t) => u.is_admin || t.approver_emp_id === u.id;
// Admin acting for someone else is stamped as such, so the PDF/history shows the truth.
const actorLabel = (u, t) => (t.approver_emp_id === u.id)
  ? (t.approver_name || u.name)
  : `${u.name} (admin for ${t.approver_name || 'manager'})`;

async function actOnTrip(user, id, decision, reason) {
  const t = await loadTripForAction(id);
  if (!t) return { code: 404, error: 'Trip not found.' };
  if (!canActOnTrip(user, t)) return { code: 403, error: 'This trip is not yours to approve.' };
  if (t.claim_ref) return { code: 409, error: 'This trip is already in a submitted claim.' };
  if (t.status !== 'pending') return { code: 409, error: `This trip is already ${t.status}.` };
  const label = actorLabel(user, t);
  await q(`UPDATE conveyance_trips SET approver_name=$2 WHERE id=$1`, [t.id, label]);
  const withActor = { ...t, approver_name: label };
  if (decision === 'approve') await applyTripApprove(withActor);
  else await applyTripReject(withActor, reason);
  return { ok: true };
}

// Trips waiting on ME (the logged-in manager).
router.get('/trip-approvals', async (req, res) => {
  const { rows } = await q(`SELECT ${PENDING_TRIP_COLS} FROM conveyance_trips t
    JOIN employees e ON e.id = t.employee_id
    WHERE t.status='pending' AND t.claim_ref IS NULL AND t.approver_emp_id=$1
    ORDER BY e.name, t.trip_date`, [req.user.id]);
  res.json(rows.map(decorateTrip));
});

// Admin: every pending trip in the org, with the manager it's stuck on.
router.get('/admin/pending-trips', requireAdminUser, async (req, res) => {
  const { rows } = await q(`SELECT ${PENDING_TRIP_COLS}, m.name AS mgr_name, m.phone AS mgr_phone
    FROM conveyance_trips t
    JOIN employees e ON e.id = t.employee_id
    LEFT JOIN employees m ON m.id = t.approver_emp_id
    WHERE t.status='pending' AND t.claim_ref IS NULL
    ORDER BY e.name, t.trip_date`);
  res.json(rows.map(decorateTrip));
});

router.post('/trip/:id/approve', async (req, res) => {
  const r = await actOnTrip(req.user, req.params.id, 'approve');
  if (r.error) return res.status(r.code).json({ error: r.error });
  res.json({ ok: true });
});

router.post('/trip/:id/reject', async (req, res) => {
  const reason = String((req.body && req.body.reason) || '').trim();
  if (!reason) return res.status(400).json({ error: 'Give a reason so the employee can fix it.' });
  const r = await actOnTrip(req.user, req.params.id, 'reject', reason);
  if (r.error) return res.status(r.code).json({ error: r.error });
  res.json({ ok: true });
});

// Approve many at once (the whole month in one click). Skips anything not actionable.
router.post('/trips/bulk-approve', async (req, res) => {
  const ids = Array.isArray(req.body && req.body.ids) ? req.body.ids.map(Number).filter(Boolean) : [];
  if (!ids.length) return res.status(400).json({ error: 'Select at least one trip.' });
  let approved = 0, skipped = 0;
  for (const id of ids) {
    const r = await actOnTrip(req.user, id, 'approve');
    if (r.ok) approved++; else skipped++;
  }
  res.json({ ok: true, approved, skipped });
});

// Re-send the WhatsApp approve links / digest on demand. Managers can nudge themselves;
// admins can nudge everyone (or one manager) — the same engine the daily cron uses.
router.post('/trips/nudge', async (req, res) => {
  const target = (req.body && req.body.manager_id) ? Number(req.body.manager_id) : null;
  if (!req.user.is_admin && target && target !== req.user.id) return res.status(403).json({ error: 'Admin only' });
  const managerId = req.user.is_admin ? target : req.user.id;
  const out = await nudgeManagers({ managerId });
  res.json({ ok: true, ...out });
});

// Daily WhatsApp nudge to every manager sitting on pending trips.
//   mode 'resend'  (default) — re-sends the existing, Meta-approved conveyance_request template
//                              for each pending trip, so the manager gets live one-tap links again.
//   mode 'digest'            — one summary message per manager (needs the conveyance_pending
//                              template approved in WATI first). Set CV_NUDGE_MODE=digest to switch.
async function nudgeManagers({ managerId = null, mode = null } = {}) {
  const M = String(mode || process.env.CV_NUDGE_MODE || 'resend').toLowerCase();
  const CAP = Number(process.env.CV_NUDGE_MAX_TRIPS || 6);   // don't blast 30 messages at one manager
  // Same time budget as the escalation cron: never let a slow WATI run overrun the function.
  const DEADLINE = Date.now() + Number(process.env.CRON_BUDGET_MS || 20000);
  const outOfTime = () => Date.now() > DEADLINE;
  const { rows } = await q(`SELECT t.id, to_char(t.trip_date,'YYYY-MM-DD') AS trip_date,
      t.from_loc, t.to_loc, t.purpose, t.amount, t.action_token, t.approver_emp_id,
      m.name AS mgr_name, m.phone AS mgr_phone, e.name AS emp_name
    FROM conveyance_trips t
    JOIN employees e ON e.id = t.employee_id
    JOIN employees m ON m.id = t.approver_emp_id
    WHERE t.status='pending' AND t.claim_ref IS NULL
      AND m.active = TRUE AND m.phone IS NOT NULL AND m.phone <> ''
      AND ($1::int IS NULL OR t.approver_emp_id = $1)
    ORDER BY t.approver_emp_id, t.trip_date`, [managerId]);

  const byMgr = new Map();
  for (const r of rows) {
    if (!byMgr.has(r.approver_emp_id)) byMgr.set(r.approver_emp_id, []);
    byMgr.get(r.approver_emp_id).push(r);
  }

  let sent = 0, failed = 0, deferred = 0;
  for (const [mid, trips] of byMgr) {
    if (outOfTime()) { deferred++; continue; }
    const mgr = { id: mid, name: trips[0].mgr_name, phone: trips[0].mgr_phone };
    const total = trips.reduce((s, t) => s + Number(t.amount || 0), 0);
    if (M === 'digest') {
      // One summary message. Per-manager, so a single failure can't abort the run.
      try {
        await wati.notify.conveyance.pending(mgr, {
          count: String(trips.length), total_label: fmtMoney(total),
          oldest: tripDateLabel(trips[0].trip_date),
          people: [...new Set(trips.map(t => t.emp_name))].join(', '),
        });
        sent++;
      } catch (e) { failed++; console.error('[trip-nudge] digest failed for', mgr.name, e.message); }
    } else {
      for (const t of trips.slice(0, CAP)) {
        if (outOfTime()) { deferred++; break; }
        try {
          await wati.notify.conveyance.request(mgr, {
            requester: t.emp_name, date_label: tripDateLabel(t.trip_date), route: tripRoute(t),
            amount_label: fmtMoney(t.amount), action_token: t.action_token });
          sent++;
        } catch (e) { failed++; console.error('[trip-nudge] trip', t.id, 'failed:', e.message); }
      }
    }
  }
  return { managers: byMgr.size, trips: rows.length, sent, failed, deferred, mode: M };
}


// ---------------- my submissions (non-draft) ----------------
router.get('/', async (req, res) => {
  const rows = (await q(`SELECT id,ref_no,form_type,period,total_amount,status,submitted_at,created_at,pdf_token
    FROM expense_submissions WHERE employee_id=$1 AND status<>'draft'
    ORDER BY COALESCE(submitted_at,created_at) DESC LIMIT 100`, [req.user.id])).rows;
  res.json(rows.map(r => ({ ...r, period_label: r.period ? monthLabel(r.period) : '' })));
});

// ---------------- HR queue (stage 1) ----------------
router.get('/approvals', async (req, res) => {
  if (!(await isHrApprover(req.user))) return res.status(403).json({ error: 'HR only' });
  const rows = (await q(`SELECT s.id,s.ref_no,s.form_type,s.period,s.total_amount,s.status,s.submitted_at,
    e.name AS emp_name, e.emp_no AS emp_code FROM expense_submissions s
    JOIN employees e ON e.id=s.employee_id WHERE s.status='pending_hr' ORDER BY s.submitted_at DESC LIMIT 200`)).rows;
  res.json(rows.map(r => ({ ...r, period_label: r.period ? monthLabel(r.period) : '' })));
});
// ---------------- Final-approver queue (stage 2) ----------------
router.get('/final-approvals', async (req, res) => {
  const rows = (await q(`SELECT s.id,s.ref_no,s.form_type,s.period,s.total_amount,s.status,s.submitted_at,s.hr_by_name,
    e.name AS emp_name, e.emp_no AS emp_code FROM expense_submissions s
    JOIN employees e ON e.id=s.employee_id
    WHERE s.status='pending_final' AND ($1 OR s.final_approver_id=$2) ORDER BY s.submitted_at DESC LIMIT 200`,
    [req.user.is_admin, req.user.id])).rows;
  res.json(rows.map(r => ({ ...r, period_label: r.period ? monthLabel(r.period) : '' })));
});
// Final approvers HR can route to (for the approve dropdown).
router.get('/chain-approvers', async (req, res) => {
  if (!(await isHrApprover(req.user))) return res.status(403).json({ error: 'HR only' });
  const mids = await managementIds();
  if (!mids.length) return res.json([]);
  res.json((await q(`SELECT id,name,emp_no FROM employees WHERE id = ANY($1) AND active=TRUE ORDER BY name`, [mids])).rows);
});

// ---------------- detail (owner / HR / final approver) ----------------
router.get('/:id', async (req, res) => {
  const row = await loadRow(req.params.id);
  if (!row) return res.status(404).json({ error: 'Not found' });
  const owner = row.employee_id === req.user.id;
  const hr = await isHrApprover(req.user);
  if (!owner && !hr && row.final_approver_id !== req.user.id) return res.status(403).json({ error: 'Not allowed' });
  const _c = await chain.getChain();
  res.json({ ...row, period_label: row.period ? monthLabel(row.period) : '',
    category_label: catLabel(row.expense_category === 'CAT1' ? 'CAT1' : 'CAT2'),
    perms: {
      is_owner: owner,
      can_hr: row.status === 'pending_hr' && hr,
      can_final: row.status === 'pending_final' && (await isManagement(req.user)),
      can_send_cmd: (req.user.is_admin || hr || row.final_approver_id === req.user.id) && ['pending_hr', 'pending_final', 'approved'].includes(row.status),
      can_settle_offline: (owner || req.user.is_admin || (_c.accounts_notify_id && _c.accounts_notify_id === req.user.id))
        && ['draft', 'pending', 'pending_hr', 'pending_final', 'returned'].includes(row.status),
      is_returned: row.status === 'returned' && owner,
    } });
});

// ---------------- chain actions ----------------
router.post('/:id/hr-approve', async (req, res) => {
  if (!(await isHrApprover(req.user))) return res.status(403).json({ error: 'HR only' });
  const finalId = Number((req.body && req.body.final_approver_id) || 0);
  const row = await loadRow(req.params.id);
  if (!row) return res.status(404).json({ error: 'Not found' });
  if (row.status !== 'pending_hr') return res.status(409).json({ error: 'Not awaiting HR' });
  if (!(await managementIds()).includes(finalId)) return res.status(400).json({ error: 'Pick a valid final approver (management).' });
  await q(`UPDATE expense_submissions SET status='pending_final', hr_by_id=$2, hr_by_name=$3, hr_at=now(), final_approver_id=$4 WHERE id=$1`,
    [row.id, req.user.id, req.user.name, finalId]);
  res.json({ ok: true });
  // WhatsApp ping to management for final approval is intentionally disabled (no final-approval notifications).
});
router.post('/:id/hr-return', async (req, res) => {
  if (!(await isHrApprover(req.user))) return res.status(403).json({ error: 'HR only' });
  const reason = ((req.body && req.body.reason) || '').trim();
  const row = await loadRow(req.params.id);
  if (!row) return res.status(404).json({ error: 'Not found' });
  if (row.status !== 'pending_hr') return res.status(409).json({ error: 'Not awaiting HR' });
  if (!reason) return res.status(400).json({ error: 'Add a reason' });
  await returnToEmployee(row, 'hr', reason, req.user.name);
  res.json({ ok: true });
});
// Settle offline: big payments the filer gets physically signed by management and hands to accounts.
// Skips the whole in-app HR + final chain; just logs it as settled offline. Filer, Accounts or admin.
const OFFLINE_FROM = ['draft', 'pending', 'pending_hr', 'pending_final', 'returned'];
router.post('/:id/settle-offline', async (req, res) => {
  const row = await loadRow(req.params.id);
  if (!row) return res.status(404).json({ error: 'Not found' });
  const c = await chain.getChain();
  const owner = row.employee_id === req.user.id;
  const isAccounts = c.accounts_notify_id && c.accounts_notify_id === req.user.id;
  if (!(owner || req.user.is_admin || isAccounts))
    return res.status(403).json({ error: 'Only the person who filed it, Accounts, or an admin can settle it offline.' });
  if (!OFFLINE_FROM.includes(row.status))
    return res.status(409).json({ error: `Can't settle offline once it is ${row.status}.` });
  const token = row.pdf_token || crypto.randomBytes(12).toString('hex');
  await q(`UPDATE expense_submissions SET status='settled_offline', settled_offline_at=now(), settled_offline_by_name=$2,
       pdf_token=COALESCE(pdf_token,$3), submitted_at=COALESCE(submitted_at,now()), updated_at=now() WHERE id=$1`,
    [row.id, req.user.name, token]);
  res.json({ ok: true, pdf_token: row.pdf_token || token });
});

async function applyFinalApprove(row, byName) {
  await q(`UPDATE expense_submissions SET status='approved', final_by_name=$2, final_at=now() WHERE id=$1`, [row.id, byName]);
  background((async () => {
    const c = await chain.getChain();
    const full = await loadRow(row.id);
    // Store the approved-stamped PDF (download link / audit). We no longer email accounts per claim:
    // approved expenses reach accounts ONLY via the monthly CONSOLIDATED run
    // (one email per company, one consolidated PDF per employee). See final.routes.js runMonthlyAccounts.
    await uploadChainPdf(full, 'approved', full.final_by_name || byName, fmtDateTime(new Date()));
    if (c.accounts_notify_id) { const acc = await empById(c.accounts_notify_id); if (acc && acc.phone) await wati.notify.expense.paid(acc, chainSummary(full)); }
    if (c.cmd_notify_id) { const cmd = await empById(c.cmd_notify_id); if (cmd && cmd.phone) await wati.notify.expense.cmd(cmd, chainSummary(full), 'Approved for payment'); }
    if (CMD_TEST_PHONE) await wati.notify.expense.cmd({ name: 'Test', phone: CMD_TEST_PHONE }, chainSummary(full), 'Approved for payment');
  })());
}

router.post('/:id/final-approve', async (req, res) => {
  const row = await loadRow(req.params.id);
  if (!row) return res.status(404).json({ error: 'Not found' });
  if (row.status !== 'pending_final') return res.status(409).json({ error: 'Not awaiting final approval' });
  if (!(await isManagement(req.user))) return res.status(403).json({ error: 'Only the named management approvers can final-approve.' });
  await applyFinalApprove(row, req.user.name);
  res.json({ ok: true });
});

// One-shot: approve for payment every claim awaiting THIS approver's final approval.
router.post('/final-approve-all', async (req, res) => {
  if (!(await isManagement(req.user))) return res.status(403).json({ error: 'Only management can final-approve.' });
  const rows = (await q(`SELECT * FROM expense_submissions WHERE status='pending_final'`)).rows;
  for (const row of rows) { try { await applyFinalApprove(row, req.user.name); } catch (e) { console.error('[final-approve-all]', row.id, e.message); } }
  res.json({ ok: true, approved: rows.length });
});

// Manually send a submitted claim to the CMD for verification (with a download-PDF WhatsApp button).
router.post('/:id/send-cmd', async (req, res) => {
  const row = await loadRow(req.params.id);
  if (!row) return res.status(404).json({ error: 'Not found' });
  const c = await chain.getChain();
  const canReview = req.user.is_admin || c.hr_approver_ids.includes(req.user.id) || c.final_approver_ids.includes(req.user.id) || row.final_approver_id === req.user.id;
  if (!canReview) return res.status(403).json({ error: 'Not allowed' });
  if (!['pending_hr', 'pending_final', 'approved'].includes(row.status)) return res.status(400).json({ error: 'Only a submitted claim can be sent for verification.' });
  const cmd = c.cmd_notify_id ? await empById(c.cmd_notify_id) : null;
  if ((!cmd || !cmd.phone) && !CMD_TEST_PHONE) return res.status(400).json({ error: 'Set the CMD WhatsApp contact first (Admin \u2192 Approval chain).' });
  if (!row.pdf_token) { const tok = crypto.randomBytes(12).toString('hex'); await q(`UPDATE expense_submissions SET pdf_token=$2 WHERE id=$1`, [row.id, tok]); row.pdf_token = tok; }
  const sum = chainSummary(row);
  if (cmd && cmd.phone) background(wati.notify.expense.cmd(cmd, sum, 'For your verification'));
  if (CMD_TEST_PHONE) background(wati.notify.expense.cmd({ name: 'Test', phone: CMD_TEST_PHONE }, sum, 'For your verification'));
  res.json({ ok: true, to: cmd ? cmd.name : 'test number' });
});
router.post('/:id/final-return', async (req, res) => {
  const reason = ((req.body && req.body.reason) || '').trim();
  const row = await loadRow(req.params.id);
  if (!row) return res.status(404).json({ error: 'Not found' });
  if (row.status !== 'pending_final') return res.status(409).json({ error: 'Not awaiting final approval' });
  if (!(req.user.is_admin || row.final_approver_id === req.user.id)) return res.status(403).json({ error: 'Not the assigned approver' });
  if (!reason) return res.status(400).json({ error: 'Add a reason' });
  await returnToEmployee(row, 'final', reason, req.user.name);
  res.json({ ok: true });
});

// ===================== OUTSTATION & MISC shared =====================
const { buildOutstationSummary, buildMiscSummary, mergeBills } = require('../lib/expense_pdf');
const CAT_LABEL = { accommodation: 'Accommodation', food: 'Food', conveyance: 'Conveyance', others: 'Others' };
const guessMime = (name) => { const e = (name || '').toLowerCase().split('.').pop();
  return e === 'png' ? 'image/png' : e === 'pdf' ? 'application/pdf' : (e === 'jpg' || e === 'jpeg') ? 'image/jpeg' : 'application/octet-stream'; };
function itemBills(it) { return (it && it.bills && it.bills.length) ? it.bills : (it && it.bill ? [it.bill] : []); }
const inr = (n) => '₹' + Number(n || 0).toLocaleString('en-IN');
function flagsHtml(flags) {
  if (!flags || !flags.length) return '';
  return `<div style="margin-top:14px"><b style="color:#b45309">Flagged (over daily limit):</b><ul style="margin:6px 0;color:#92400e;font-size:13px">${flags.map(f => `<li>${f}</li>`).join('')}</ul></div>`;
}

// Compute totals + per-day food/accom over-limit flags for an outstation claim.
function computeOutstation(body, limits) {
  let total = 0; const flags = [];
  const trips = (body.trips || []).map(trip => {
    const items = (trip.items || []).map(it => {
      const amount = Math.max(0, parseFloat(it.amount) || 0); total += amount;
      const category = ['accommodation', 'food', 'conveyance', 'others', 'long_distance'].includes(it.category) ? it.category : 'others';
      return { category, date: it.date || '', desc: (it.desc || '').trim(), amount,
        bills: itemBills(it), flag: false };
    });
    for (const key of ['food', 'accommodation']) {
      const lim = key === 'food' ? limits.food : limits.accom;
      const byDate = {};
      items.forEach(it => { if (it.category === key) (byDate[it.date] = byDate[it.date] || []).push(it); });
      for (const [date, arr] of Object.entries(byDate)) {
        const sum = arr.reduce((s, x) => s + x.amount, 0);
        if (sum > lim) { arr.forEach(x => x.flag = true);
          flags.push(`${key === 'food' ? 'Food' : 'Accommodation'} ${date || '(no date)'} ${inr(sum)} exceeds ${key === 'food' ? 'food' : 'stay'} limit ${inr(lim)}${trip.place ? ' · ' + trip.place : ''}`); }
      }
    }
    return { place: (trip.place || '').trim(), from_date: trip.from_date || '', to_date: trip.to_date || '',
      reason: (trip.reason || '').trim(), items };
  });
  return { trips, total: +total.toFixed(2), flags };
}

async function outstationPdf(row, status, approver, approvedAt) {
  const pol = await getPolicy();
  const cat = row.expense_category === 'CAT1' ? 'CAT1' : 'CAT2';
  const trips = (row.payload && row.payload.trips) || [];
  const summary = await buildOutstationSummary({
    ref_no: row.ref_no, emp_name: row.emp_name, emp_code: row.emp_code, designation: row.designation || '',
    category: catLabel(cat), period_label: monthLabel(row.period), limits: pol.limits[cat],
    trips, total: Number(row.total_amount || 0), flags: row.flags || [], status, approver, approved_at: approvedAt ? fmtDateTime(approvedAt) : '' });
  const bills = [];
  for (const t of trips) for (const it of (t.items || [])) {
    for (const b of itemBills(it)) {
      if (b && b.drive_item_id) {
        const bytes = await graph.fetchDriveItemContent(b.drive_item_id);
        if (bytes) bills.push({ bytes, mime: b.mime || guessMime(b.name), caption: `Bill: ${it.desc || ''} — ${CAT_LABEL[it.category] || it.category}` });
      }
    }
  }
  return bills.length ? await mergeBills(summary, bills) : summary;
}
async function miscPdf(row, status, approver, approvedAt) {
  const items = (row.payload && row.payload.items) || [];
  const summary = await buildMiscSummary({
    ref_no: row.ref_no, emp_name: row.emp_name, emp_code: row.emp_code, designation: row.designation || '',
    category: catLabel(row.expense_category === 'CAT1' ? 'CAT1' : 'CAT2'),
    items, total: Number(row.total_amount || 0), generated_at: fmtDateTime(new Date()),
    status, approver, approved_at: approvedAt ? fmtDateTime(approvedAt) : '' });
  const bills = [];
  for (const it of items) for (const b of itemBills(it)) if (b && b.drive_item_id) {
    const bytes = await graph.fetchDriveItemContent(b.drive_item_id);
    if (bytes) bills.push({ bytes, mime: b.mime || guessMime(b.name), caption: `Bill: ${it.desc || ''}` });
  }
  return bills.length ? await mergeBills(summary, bills) : summary;
}

// Mint an upload session for a bill (browser uploads it straight to OneDrive).
router.post('/bill-session', async (req, res) => {
  const { ref, name } = req.body || {};
  if (!ref || !name) return res.status(400).json({ error: 'ref and name required' });
  const own = (await q('SELECT id FROM expense_submissions WHERE ref_no=$1 AND employee_id=$2', [ref, req.user.id])).rows[0];
  if (!own) return res.status(403).json({ error: 'Not your claim' });
  const safe = String(name).replace(/[^\w.\- ]+/g, '_').slice(0, 120);
  const sess = await graph.createExpenseBillSession(ref, safe);
  if (!sess) return res.status(503).json({ error: 'Bill storage is not set up yet.' });
  res.json({ uploadUrl: sess.uploadUrl, file_name: safe });
});

// Stream a bill back THROUGH the portal, so it opens with only a portal login —
// no OneDrive/SharePoint sign-in. Authorized like the submission it belongs to
// (owner, an HR approver, or the assigned final approver).
router.get('/bill/:itemId', async (req, res) => {
  const itemId = String(req.params.itemId || '');
  if (!itemId) return res.status(400).send('Bad request');
  const sub = (await q(
    `SELECT employee_id, final_approver_id, payload FROM expense_submissions
     WHERE payload::text LIKE '%' || $1 || '%' ORDER BY id DESC LIMIT 1`, [itemId])).rows[0];
  if (!sub) return res.status(404).send('Bill not found');
  const owner = sub.employee_id === req.user.id;
  const hr = await isHrApprover(req.user);
  if (!owner && !hr && sub.final_approver_id !== req.user.id) return res.status(403).send('Not allowed');
  // Best-effort: read the stored name/mime so the browser shows it inline correctly.
  let mime = 'application/octet-stream', name = 'bill';
  try {
    const items = [].concat(...((sub.payload && sub.payload.trips) || []).map(t => t.items || []), (sub.payload && sub.payload.items) || []);
    let hit = null;
    for (const it of items) { for (const b of itemBills(it)) { if (b && b.drive_item_id === itemId) { hit = b; break; } } if (hit) break; }
    if (hit) { mime = hit.mime || guessMime(hit.name); name = hit.name || name; }
  } catch (_) { /* fall back to generic */ }
  const bytes = await graph.fetchDriveItemContent(itemId);
  if (!bytes) return res.status(502).send('Could not fetch the bill from storage');
  res.setHeader('Content-Type', mime);
  res.setHeader('Content-Disposition', `inline; filename="${name.replace(/"/g, '')}"`);
  res.setHeader('Cache-Control', 'private, max-age=300');
  res.send(bytes);
});

// ---------------- OUTSTATION ----------------
router.get('/outstation/:period', async (req, res) => {
  const period = req.params.period;
  if (!/^\d{4}-\d{2}$/.test(period)) return res.status(400).json({ error: 'Bad period' });
  // Working draft (draft/returned). If none — e.g. a prior claim is already approved — start a fresh one so the month stays open.
  let row = (await q(`SELECT * FROM expense_submissions WHERE employee_id=$1 AND form_type='outstation' AND period=$2 AND status IN ('draft','returned') ORDER BY id DESC LIMIT 1`, [req.user.id, period])).rows[0];
  if (!row) {
    const ref = await refNo('OUT');
    row = (await q(`INSERT INTO expense_submissions(ref_no,employee_id,form_type,period,payload,status)
      VALUES($1,$2,'outstation',$3,$4,'draft') RETURNING *`, [ref, req.user.id, period, JSON.stringify({ trips: [] })])).rows[0];
  }
  const claims = (await q(`SELECT id, ref_no, status, total_amount, pdf_token, submitted_at FROM expense_submissions
    WHERE employee_id=$1 AND form_type='outstation' AND period=$2 AND status IN ('pending_hr','pending_final','approved') ORDER BY id`, [req.user.id, period])).rows;
  const allow = await submitAllowed('outstation', period);
  res.json({ ...row, can_submit: ['draft', 'returned'].includes(row.status) && allow, period_label: monthLabel(period), claims });
});
router.put('/outstation/:id', async (req, res) => {
  const row = (await q('SELECT * FROM expense_submissions WHERE id=$1', [req.params.id])).rows[0];
  if (!row || row.employee_id !== req.user.id) return res.status(403).json({ error: 'Not allowed' });
  if (!['draft', 'returned'].includes(row.status)) return res.status(409).json({ error: 'Already submitted' });
  const pol = await getPolicy();
  const cat = req.user.expense_category === 'CAT1' ? 'CAT1' : 'CAT2';
  const { trips, total, flags } = computeOutstation(req.body || {}, pol.limits[cat]);
  await q(`UPDATE expense_submissions SET payload=$2, total_amount=$3, flags=$4, updated_at=now() WHERE id=$1`,
    [row.id, JSON.stringify({ trips }), total, JSON.stringify(flags)]);
  res.json({ ok: true, total, flags });
});
router.post('/outstation/:id/submit', async (req, res) => {
  const row = (await q('SELECT * FROM expense_submissions WHERE id=$1', [req.params.id])).rows[0];
  if (!row || row.employee_id !== req.user.id) return res.status(403).json({ error: 'Not allowed' });
  if (!['draft', 'returned'].includes(row.status)) return res.status(409).json({ error: 'Already submitted' });
  if (!(await submitAllowed('outstation', row.period))) return res.status(400).json({ error: 'This month can be submitted from the 1st of next month.' });
  const trips = (row.payload && row.payload.trips) || [];
  const itemCount = trips.reduce((s, t) => s + ((t.items || []).length), 0);
  if (!itemCount) return res.status(400).json({ error: 'Add at least one expense line before submitting.' });
  await enterChain(row.id);
  res.json({ ok: true });
});

// ---------------- MISCELLANEOUS (self-service, no HR) ----------------
router.get('/misc/new', async (req, res) => {
  const ref = await refNo('MSC');
  const row = (await q(`INSERT INTO expense_submissions(ref_no,employee_id,form_type,payload,status)
    VALUES($1,$2,'misc',$3,'draft') RETURNING *`, [ref, req.user.id, JSON.stringify({ items: [] })])).rows[0];
  res.json(row);
});
router.put('/misc/:id', async (req, res) => {
  const row = (await q('SELECT * FROM expense_submissions WHERE id=$1', [req.params.id])).rows[0];
  if (!row || row.employee_id !== req.user.id) return res.status(403).json({ error: 'Not allowed' });
  // Editable until HR approves (online) or while still settled-offline (offline path has no HR gate).
  // Locks once it moves to pending_final / approved / paid.
  if (!['draft', 'generated', 'returned', 'pending_hr', 'settled_offline'].includes(row.status)) return res.status(409).json({ error: 'This claim can no longer be edited — it has already been approved.' });
  let total = 0;
  const items = (req.body.items || []).map(it => {
    const amount = Math.max(0, parseFloat(it.amount) || 0); total += amount;
    return { date: it.date || '', desc: (it.desc || '').trim(), amount, bills: itemBills(it) };
  });
  await q(`UPDATE expense_submissions SET payload=$2, total_amount=$3, updated_at=now() WHERE id=$1`,
    [row.id, JSON.stringify({ items }), +total.toFixed(2)]);
  res.json({ ok: true, total: +total.toFixed(2) });
});
router.post('/misc/:id/submit', async (req, res) => {
  const row = (await q('SELECT * FROM expense_submissions WHERE id=$1', [req.params.id])).rows[0];
  if (!row || row.employee_id !== req.user.id) return res.status(403).json({ error: 'Not allowed' });
  if (!['draft', 'generated', 'returned'].includes(row.status)) return res.status(409).json({ error: 'Locked' });
  const items = (row.payload && row.payload.items) || [];
  if (!items.length) return res.status(400).json({ error: 'Add at least one item.' });
  if (!row.pdf_token) await q(`UPDATE expense_submissions SET pdf_token=$2 WHERE id=$1`, [row.id, crypto.randomBytes(12).toString('hex')]);
  await enterChain(row.id);
  res.json({ ok: true });
});

// ===================== CONSOLIDATED CMD REPORT (approved-only) =====================
// Accounts: list approved claims + their payment state.
router.get('/payments/list', async (req, res) => {
  if (!(await isAccounts(req.user))) return res.status(403).json({ error: 'Accounts/Admin only' });
  const filter = req.query.filter === 'paid' ? 'paid' : req.query.filter === 'all' ? 'all' : 'unpaid';
  const cond = filter === 'paid' ? 'AND s.paid_at IS NOT NULL' : filter === 'unpaid' ? 'AND s.paid_at IS NULL' : '';
  const rows = (await q(`SELECT s.id, s.ref_no, s.form_type, s.total_amount, s.final_at, s.paid_at, s.paid_by_name, s.pdf_token,
      e.name AS emp_name, e.emp_no
    FROM expense_submissions s JOIN employees e ON e.id=s.employee_id
    WHERE s.status='approved' ${cond} ORDER BY (s.paid_at IS NULL) DESC, s.final_at DESC LIMIT 300`)).rows;
  res.json(rows.map(r => ({ ...r, form_label: FORM_LABEL[r.form_type] || r.form_type })));
});
router.post('/:id/mark-paid', async (req, res) => {
  if (!(await isAccounts(req.user))) return res.status(403).json({ error: 'Accounts/Admin only' });
  const row = (await q('SELECT id, status FROM expense_submissions WHERE id=$1', [req.params.id])).rows[0];
  if (!row) return res.status(404).json({ error: 'Not found' });
  if (row.status !== 'approved') return res.status(400).json({ error: 'Only approved claims can be marked paid.' });
  await q(`UPDATE expense_submissions SET paid_at=COALESCE(paid_at, now()), paid_by_name=$2 WHERE id=$1`, [row.id, req.user.name]);
  res.json({ ok: true });
});
router.post('/:id/unmark-paid', async (req, res) => {
  if (!(await isAccounts(req.user))) return res.status(403).json({ error: 'Accounts/Admin only' });
  await q(`UPDATE expense_submissions SET paid_at=NULL, paid_by_name=NULL WHERE id=$1`, [req.params.id]);
  res.json({ ok: true });
});

// Admin/HR: force a claim into a specific CMD-report month (blank = auto; misc auto = submission date).
router.post('/:id/report-month', async (req, res) => {
  if (!(await isHrApprover(req.user))) return res.status(403).json({ error: 'HR/Admin only' });
  const row = (await q('SELECT id FROM expense_submissions WHERE id=$1', [req.params.id])).rows[0];
  if (!row) return res.status(404).json({ error: 'Not found' });
  const period = (req.body && req.body.period) || null;
  if (period && !/^\d{4}-\d{2}$/.test(period)) return res.status(400).json({ error: 'Bad month (YYYY-MM)' });
  await q('UPDATE expense_submissions SET report_period_override=$2 WHERE id=$1', [row.id, period]);
  res.json({ ok: true });
});
router.get('/report/:period/summary', async (req, res) => {
  if (!(await isHrApprover(req.user))) return res.status(403).json({ error: 'HR/Admin only' });
  const period = req.params.period; if (!/^\d{4}-\d{2}$/.test(period)) return res.status(400).json({ error: 'Bad period' });
  const d = await report.reportData(period);
  const c = await chain.getChain();
  const cmd = c.cmd_notify_id ? await empById(c.cmd_notify_id) : null;
  res.json({ period, cycle: report.reportLabel(period), count: d.count, total: d.total,
    cmd_name: cmd ? cmd.name : null, cmd_email: cmd ? (cmd.email || null) : null, cmd_phone: cmd ? Boolean(cmd.phone) : false });
});

router.get('/report/:period/xlsx', async (req, res) => {
  if (!(await isHrApprover(req.user))) return res.status(403).json({ error: 'HR/Admin only' });
  const period = req.params.period; if (!/^\d{4}-\d{2}$/.test(period)) return res.status(400).json({ error: 'Bad period' });
  const buf = await report.buildReportBuffer(period);
  res.setHeader('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
  res.setHeader('Content-Disposition', `attachment; filename="${report.reportFileName(period)}"`);
  res.send(buf);
});

router.post('/report/:period/send', async (req, res) => {
  if (!(await isHrApprover(req.user))) return res.status(403).json({ error: 'HR/Admin only' });
  const period = req.params.period; if (!/^\d{4}-\d{2}$/.test(period)) return res.status(400).json({ error: 'Bad period' });
  const d = await report.reportData(period);
  if (!d.count) return res.status(400).json({ error: 'No approved reimbursements in this cycle yet.' });
  const c = await chain.getChain();
  const cmd = c.cmd_notify_id ? await empById(c.cmd_notify_id) : null;
  const buf = await report.buildReportBuffer(period);
  const fname = report.reportFileName(period);
  const cycle = report.reportLabel(period);
  const out = { count: d.count, total: d.total, emailed: false, whatsapped: false, to: cmd ? cmd.name : null };
  // Email with the .xlsx attached (CMD; cc accounts).
  if (cmd && cmd.email) {
    out.emailed = await graph.sendMail({
      to: cmd.email, cc: c.accounts_email || undefined,
      subject: `Reimbursements pending payment — ${cycle} — ${fmtMoney(d.total)}`,
      html: `<p>Approved reimbursements <b>pending payment</b> for <b>${cycle}</b>.</p>`
          + `<p>${d.count} claim(s), total <b>${fmtMoney(d.total)}</b>. Full breakdown attached (Excel). Items drop off once accounts marks them paid.</p>`,
      attachments: [{ name: fname, contentType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', contentBytes: buf.toString('base64') }],
    });
  }
  // WhatsApp a download link (CMD + optional test number).
  const link = report.signLink(period);
  const payload = { cycle, count: d.count, total_label: fmtMoney(d.total), token: link };
  if (cmd && cmd.phone) { background(wati.notify.expense.report(cmd, payload)); out.whatsapped = true; }
  if (CMD_TEST_PHONE) { background(wati.notify.expense.report({ name: 'Test', phone: CMD_TEST_PHONE }, payload)); out.whatsapped = true; }
  out.no_cmd = !cmd; out.no_email = cmd && !cmd.email;
  res.json({ ok: true, ...out });
});

// One-time cleanup: reopen approved-but-unpaid claims that were final-approved by someone who is
// NOT in the management team — sends them back to pending_final for proper management approval.
router.post('/reopen-nonmgmt', async (req, res) => {
  if (!(await isManagement(req.user))) return res.status(403).json({ error: 'Only management can do this.' });
  const mids = await managementIds();
  if (!mids.length) return res.status(400).json({ error: 'Configure the management approvers first.' });
  const names = (await q(`SELECT name FROM employees WHERE id = ANY($1)`, [mids])).rows.map(r => r.name);
  const r = await q(
    `UPDATE expense_submissions SET status='pending_final', final_at=NULL, final_by_name=NULL, updated_at=now()
     WHERE status='approved' AND paid_at IS NULL AND (final_by_name IS NULL OR final_by_name <> ALL($1::text[])) RETURNING id`,
    [names]);
  res.json({ ok: true, reopened: r.rows.length });
});

// Reopen ONE approved-but-unpaid claim back to pending_final (per submission).
router.post('/:id/reopen-final', async (req, res) => {
  if (!(await isManagement(req.user))) return res.status(403).json({ error: 'Only management can reopen.' });
  const row = await loadRow(req.params.id);
  if (!row) return res.status(404).json({ error: 'Not found' });
  if (row.status !== 'approved') return res.status(409).json({ error: 'Only an approved claim can be reopened.' });
  if (row.paid_at) return res.status(409).json({ error: 'This claim is marked paid — unmark paid first.' });
  await q(`UPDATE expense_submissions SET status='pending_final', final_at=NULL, final_by_name=NULL, updated_at=now() WHERE id=$1`, [row.id]);
  res.json({ ok: true });
});

async function claimPdfById(id) {
  const full = await loadRow(id);
  if (!full) return null;
  return pdfFor(full, 'approved', full.final_by_name || 'Management', full.final_at);
}
module.exports = router;
module.exports.nudgeManagers = nudgeManagers;
module.exports._internal = { conveyancePdf, outstationPdf, miscPdf, loadRow, claimPdfById, loadTripByToken, applyTripApprove, applyTripReject, nudgeManagers };
