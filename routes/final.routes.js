// Management Final Approval for Payments — one inbox aggregating everything awaiting a
// management/final decision: expense claims (pending_final), staff OT monthly batches, and
// labour OT+shearing (grouped under a single "Labours" payee). Approvals reuse the existing
// per-module endpoints so all their side-effects (PDFs, accounts emails) stay intact.
const express = require('express');
const { q } = require('../lib/db');
const auth = require('../lib/auth');
const chain = require('../lib/chain');
const graph = require('../lib/graph');
const { stampApprovals } = require('../lib/approval_stamp');
const router = express.Router();
router.use(auth.requireAuth);

const FORM_LABEL = { conveyance: 'Local Conveyance', outstation: 'Outstation', misc: 'Miscellaneous' };
const LABOUR_CO = { BSC: 'Bharat Steel (Chennai)', G2: 'G2 Steel Services' };
const monthLabel = (p) => { if (!p) return ''; const [y, m] = String(p).split('-'); return new Date(y, m - 1, 1).toLocaleString('en-IN', { month: 'long', year: 'numeric' }); };
const money = (n) => '\u20b9' + Number(n || 0).toLocaleString('en-IN');

async function isMgmt(u) {
  const mgmt = ((await q(`SELECT value FROM app_settings WHERE key='ot_mgmt_emp_ids'`)).rows[0]?.value || '').split(',').map(Number).filter(Boolean);
  return mgmt.includes(u.id);
}
// HR (e.g. Rajasekar) may also use the Send-to-Accounts screen — not just management.
// "HR" here = the labour/OT HR (ot_hr_emp_id, matches me.ot_hr on the frontend) OR an expense-chain HR.
async function isMgmtOrHr(u) {
  if (u && u.is_admin) return true;
  if (await isMgmt(u)) return true;
  const otHr = (await q(`SELECT 1 FROM app_settings WHERE key='ot_hr_emp_id' AND value=$1`, [String(u.id)])).rows.length > 0;
  if (otHr) return true;
  try { const c = await chain.getChain(); return (c.hr_approver_ids || []).includes(u.id); } catch (e) { return false; }
}

// ---- Approval badge data (approver name + time) for the per-page stamp on the reports ----
const _ist = (col) => `to_char(${col} AT TIME ZONE 'Asia/Kolkata','DD Mon YYYY, HH12:MI AM')`;
async function otApprovals(companyKey, month) {
  const C = COMPANIES[companyKey]; if (!C) return [];
  const out = [];
  const b = (await q(`SELECT mgmt_name, ${_ist('reviewed_at')} AS at FROM ot_batches
                      WHERE period=$1 AND status IN ('approved','sent_accounts') AND mgmt_name IS NOT NULL
                      ORDER BY reviewed_at DESC NULLS LAST LIMIT 1`, [month])).rows[0];
  if (b && b.mgmt_name) out.push({ label: 'Staff OT approved', by: b.mgmt_name, at: b.at });
  const lp = (await q(`SELECT mgmt_by_name, ${_ist('mgmt_at')} AS at FROM labour_period
                       WHERE company=$1 AND period=$2 AND ot_status='approved' AND mgmt_by_name IS NOT NULL`, [C.labourCo, month])).rows[0];
  if (lp && lp.mgmt_by_name) out.push({ label: 'Labour OT approved', by: lp.mgmt_by_name, at: lp.at });
  return out;
}
async function shearingApprovals(companyKey, month) {
  const C = COMPANIES[companyKey]; if (!C) return [];
  const lp = (await q(`SELECT mgmt_by_name, ${_ist('mgmt_at')} AS at FROM labour_period
                       WHERE company=$1 AND period=$2 AND shearing_status='approved' AND mgmt_by_name IS NOT NULL`, [C.labourCo, month])).rows[0];
  return (lp && lp.mgmt_by_name) ? [{ label: 'Shearing approved', by: lp.mgmt_by_name, at: lp.at }] : [];
}

router.get('/queue', async (req, res) => {
  if (!(await isMgmt(req.user))) return res.status(403).json({ error: 'Management / admin only.' });
  const items = [];

  // 1) Expense claims awaiting final approval — grouped under the real employee.
  const exp = (await q(
    `SELECT s.id, s.ref_no, s.form_type, s.period, s.total_amount, s.pdf_token, e.name AS emp_name, e.emp_no
     FROM expense_submissions s JOIN employees e ON e.id=s.employee_id
     WHERE s.status='pending_final' ORDER BY s.submitted_at`)).rows;
  exp.forEach(r => items.push({
    kind: 'expense', type_key: r.form_type, type_label: FORM_LABEL[r.form_type] || r.form_type,
    payee: r.emp_name, payee_key: 'emp:' + (r.emp_no || r.emp_name), emp_no: r.emp_no || '',
    sub: (r.period ? monthLabel(r.period) + ' \u00b7 ' : '') + r.ref_no,
    amount: Number(r.total_amount || 0), pdf_token: r.pdf_token || null,
    approve: { url: '/expense/' + r.id + '/final-approve' },
  }));

  // 2) Staff OT — monthly batches awaiting management (approved as a batch).
  const ot = (await q(`SELECT id, period, emp_count, entry_count, total_amount FROM ot_batches WHERE status='mgmt_pending' ORDER BY period`)).rows;
  ot.forEach(b => items.push({
    kind: 'ot', type_key: 'ot', type_label: 'Overtime', payee: 'Overtime', payee_key: 'overtime',
    sub: monthLabel(b.period) + ' \u00b7 ' + b.emp_count + ' staff \u00b7 ' + b.entry_count + ' days',
    amount: Number(b.total_amount || 0), report_url: '/api/final/ot-report/' + b.id,
    approve: { url: '/ot/mgmt-batch/' + b.id + '/approve' },
  }));

  // 3) Labour OT + shearing — grouped under "Labours", submitted/approved per part.
  const lab = (await q(`SELECT company, period, ot_total, shearing_total, ot_status, shearing_status FROM labour_period WHERE ot_status='pending_mgmt' OR shearing_status='pending_mgmt' ORDER BY period`)).rows;
  lab.forEach(p => {
    if (p.ot_status === 'pending_mgmt') items.push({
      kind: 'labour', type_key: 'labour', type_label: 'Labour (OT + Shearing)', payee: 'Labours', payee_key: 'labours',
      sub: (LABOUR_CO[p.company] || p.company) + ' \u00b7 ' + monthLabel(p.period) + ' \u00b7 Overtime',
      amount: Number(p.ot_total || 0), company: p.company, period: p.period,
      report_url: `/api/labour/report-pdf/${p.company}/${p.period}/ot`,
      approve: { url: '/labour/approve', body: { company: p.company, month: p.period, part: 'ot' } },
    });
    if (p.shearing_status === 'pending_mgmt') items.push({
      kind: 'labour', type_key: 'labour', type_label: 'Labour (OT + Shearing)', payee: 'Labours', payee_key: 'labours',
      sub: (LABOUR_CO[p.company] || p.company) + ' \u00b7 ' + monthLabel(p.period) + ' \u00b7 Shearing',
      amount: Number(p.shearing_total || 0), company: p.company, period: p.period,
      report_url: `/api/labour/report-pdf/${p.company}/${p.period}/shearing`,
      approve: { url: '/labour/approve', body: { company: p.company, month: p.period, part: 'shearing' } },
    });
  });

  const total = items.reduce((s, i) => s + i.amount, 0);
  res.json({ items, total, count: items.length });
});

// Consolidated Overtime report (Employees + Labour) for a pending OT batch — shown as the PDF
// preview on the Overtime item. Includes labour OT for the same month so management sees all OT.
router.get('/ot-report/:batchId', async (req, res) => {
  if (!(await isMgmt(req.user))) return res.status(403).send('Not allowed');
  const b = (await q(`SELECT period FROM ot_batches WHERE id=$1`, [req.params.batchId])).rows[0];
  if (!b) return res.status(404).send('Not found');
  const period = b.period;
  const emp = (await q(`SELECT e.name, COALESCE(SUM(o.hours),0) AS hours, COALESCE(SUM(o.amount),0) AS amount
                        FROM ot_entries o JOIN employees e ON e.id=o.employee_id WHERE o.batch_id=$1 GROUP BY e.name ORDER BY e.name`, [req.params.batchId])).rows;
  const lab = (await q(`SELECT labour_name AS name, COALESCE(SUM(hours),0) AS hours, COALESCE(SUM(amount),0) AS amount
                        FROM labour_ot WHERE period=$1 GROUP BY labour_name ORDER BY labour_name`, [period])).rows;
  const empT = emp.reduce((s, r) => s + Number(r.amount), 0), labT = lab.reduce((s, r) => s + Number(r.amount), 0);
  const { buildPaymentReportPDF } = require('../lib/payment_pdf');
  const col = [{ label: 'Name', width: 0.6 }, { label: 'Total hours', width: 0.2, align: 'right' }, { label: 'Amount', width: 0.2, align: 'right' }];
  const pdf = await buildPaymentReportPDF({
    title: 'Overtime — consolidated', subtitle: `${monthLabel(period)} · Employees + Labour`, approved: false,
    sections: [
      { name: 'Employees', total: empT, cols: col, rows: emp.map(r => [r.name, (+r.hours).toFixed(2), money(r.amount)]) },
      { name: 'Labour (daily wage)', total: labT, cols: col, rows: lab.map(r => [r.name, (+r.hours).toFixed(2), money(r.amount)]) },
    ],
    grandTotal: empT + labT,
  });
  const stamped = await stampApprovals(pdf, [], { preview: true });
  res.setHeader('Content-Type', 'application/pdf');
  res.setHeader('Content-Disposition', `inline; filename="OT_consolidated_${period}.pdf"`);
  res.end(stamped);
});

module.exports = router;

// ===================== MONTHLY CONSOLIDATED PAYMENTS TO ACCOUNTS =====================
const prevMonth = () => { const d = new Date(); d.setDate(1); d.setMonth(d.getMonth() - 1); return d.toISOString().slice(0, 7); };
const OT_DONE = "('mgmt_approved','paid')";  // final-approved stage (paid keeps mgmt_approved history via 'paid')

// Which run-month an item belongs to — keyed off the ACTUAL date, not the entry date.
//   Conveyance/Outstation: their period (already the 26th→25th cycle from trip dates).
//   Misc: its "Set month" period if forced, else its submission date (misc has no trip cycle).
//   OT: the OT date's calendar month (1st→last).
function expInMonth(s, p) {
  return `(
    (${s}.form_type <> 'misc' AND ${s}.period = ${p})
    OR (${s}.form_type = 'misc' AND COALESCE(${s}.period, to_char(${s}.submitted_at,'YYYY-MM'), to_char(${s}.created_at,'YYYY-MM'), to_char(${s}.final_at,'YYYY-MM')) = ${p})
  )`;
}
function otInMonth(o, p) {
  return `${o}.ot_date >= (${p}||'-01')::date AND ${o}.ot_date < ((${p}||'-01')::date + interval '1 month') AND ${o}.status = 'mgmt_approved'`;  // unpaid only
}

// Build one employee's consolidated PDF: breakdown cover + each approved claim + OT summary.
// ---- Restructured reports for accounts (per company) ----
const COMPANIES = {
  BSC: { label: 'Bharat Steel (Chennai)', staffPrefix: 'BSC', labourCo: 'BSC', logo: 'BSC' },
  G2: { label: 'G2 Steel Services', staffPrefix: 'G2S', labourCo: 'G2', logo: 'G2' },
};
const otStatusClause = (inc) => inc ? "IN ('mgmt_approved','paid')" : "= 'mgmt_approved'";

// Overtime consolidated report for a company: summary table (staff + labour) then per-person per-day.
async function buildOtCombinedPdf(companyKey, month, opts = {}) {
  const C = COMPANIES[companyKey]; if (!C) return null;
  const { buildCombinedReportPDF, money } = require('../lib/reports');
  const inc = !!opts.includePaid;
  const staff = (await q(
    `SELECT e.emp_no AS code, e.name, to_char(o.ot_date,'DD Mon') AS d, o.end_time, o.hours, o.amount
     FROM ot_entries o JOIN employees e ON e.id=o.employee_id
     WHERE e.emp_no LIKE $1 AND o.status ${otStatusClause(inc)}
       AND o.ot_date >= ($2||'-01')::date AND o.ot_date < (($2||'-01')::date + interval '1 month')
     ORDER BY e.name, o.ot_date`, [C.staffPrefix + '/%', month])).rows;
  const labour = (await q(
    `SELECT lo.labour_code AS code, lo.labour_name AS name, to_char(lo.ot_date,'DD Mon') AS d, lo.hours, lo.amount
     FROM labour_ot lo JOIN labour_period lp ON lp.company=lo.company AND lp.period=lo.period
     WHERE lo.company=$1 AND lo.period=$2 AND lp.ot_status='approved' AND lo.paid_at IS NULL
     ORDER BY lo.labour_name, lo.ot_date`, [C.labourCo, month])).rows;

  const people = new Map();
  const add = (key, code, name, kind) => { if (!people.has(key)) people.set(key, { code: code || '\u2014', name, kind, rows: [], total: 0, hours: 0 }); return people.get(key); };
  staff.forEach(r => { const p = add('s:' + r.name + (r.code || ''), r.code, r.name, 'staff'); p.rows.push([r.d, r.end_time, (+r.hours).toFixed(2), money(r.amount)]); p.total += Number(r.amount); p.hours += Number(r.hours); });
  labour.forEach(r => { const p = add('l:' + r.name + (r.code || ''), r.code, r.name, 'labour'); p.rows.push([r.d, '\u2014', (+r.hours).toFixed(2), money(r.amount)]); p.total += Number(r.amount); p.hours += Number(r.hours); });
  const list = [...people.values()];
  if (!list.length) return null;
  const grand = list.reduce((s, p) => s + p.total, 0);

  const __otpdf = await buildCombinedReportPDF({
    companyKey, title: `Overtime — ${C.label}`, subtitle: `${monthLabel(month)} · consolidated`,
    summary: {
      cols: [{ label: '#', width: 0.08 }, { label: 'Emp code', width: 0.2 }, { label: 'Name', width: 0.42 }, { label: 'Total OT hrs', width: 0.15, align: 'right' }, { label: 'Amount', width: 0.15, align: 'right' }],
      rows: list.map((p, i) => [String(i + 1), p.code, p.name, p.hours.toFixed(2), money(p.total)]),
      totalRow: ['', '', 'TOTAL', '', money(grand)],
    },
    people: list.map(p => ({
      heading: `Overtime — ${p.name}`, sub: `${p.code} · ${monthLabel(month)}`,
      cols: [{ label: 'Date', width: 0.34 }, { label: 'End time', width: 0.22 }, { label: 'Hours', width: 0.22, align: 'right' }, { label: 'Amount', width: 0.22, align: 'right' }],
      rows: p.rows, totalRow: ['Total', '', p.hours.toFixed(2), money(p.total)],
    })),
  });
  return stampApprovals(__otpdf, await otApprovals(companyKey, month));
}

// Shearing consolidated report (labour only) for a company.
async function buildShearingCombinedPdf(companyKey, month) {
  const C = COMPANIES[companyKey]; if (!C) return null;
  const { buildCombinedReportPDF, money } = require('../lib/reports');
  const sh = (await q(
    `SELECT ls.labour_code AS code, ls.labour_name AS name, to_char(ls.sh_date,'DD Mon') AS d, ls.days, ls.amount
     FROM labour_shearing ls JOIN labour_period lp ON lp.company=ls.company AND lp.period=ls.period
     WHERE ls.company=$1 AND ls.period=$2 AND lp.shearing_status='approved' AND ls.paid_at IS NULL
     ORDER BY ls.labour_name, ls.sh_date`, [C.labourCo, month])).rows;
  const people = new Map();
  sh.forEach(r => { const k = r.name + (r.code || ''); if (!people.has(k)) people.set(k, { code: r.code || '\u2014', name: r.name, rows: [], total: 0, days: 0 }); const p = people.get(k); p.rows.push([r.d, (+r.days).toFixed(1), money(r.amount)]); p.total += Number(r.amount); p.days += Number(r.days); });
  const list = [...people.values()];
  if (!list.length) return null;
  const grand = list.reduce((s, p) => s + p.total, 0);
  const __shpdf = await buildCombinedReportPDF({
    companyKey, title: `Shed-B Shearing — ${C.label}`, subtitle: `${monthLabel(month)} · consolidated`,
    summary: {
      cols: [{ label: '#', width: 0.08 }, { label: 'Emp code', width: 0.2 }, { label: 'Name', width: 0.42 }, { label: 'Total days', width: 0.15, align: 'right' }, { label: 'Amount', width: 0.15, align: 'right' }],
      rows: list.map((p, i) => [String(i + 1), p.code, p.name, p.days.toFixed(1), money(p.total)]),
      totalRow: ['', '', 'TOTAL', '', money(grand)],
    },
    people: list.map(p => ({
      heading: `Shearing — ${p.name}`, sub: `${p.code} · ${monthLabel(month)}`,
      cols: [{ label: 'Date', width: 0.5 }, { label: 'Days', width: 0.25, align: 'right' }, { label: 'Amount', width: 0.25, align: 'right' }],
      rows: p.rows, totalRow: ['Total', p.days.toFixed(1), money(p.total)],
    })),
  });
  return stampApprovals(__shpdf, await shearingApprovals(companyKey, month));
}

router.get('/report/ot/:company/:month', async (req, res) => {
  if (!(await isMgmtOrHr(req.user))) return res.status(403).send('Not allowed');
  if (!/^\d{4}-\d{2}$/.test(req.params.month) || !COMPANIES[req.params.company]) return res.status(400).send('Bad request');
  try {
    const pdf = await buildOtCombinedPdf(req.params.company, req.params.month, { includePaid: req.query.all === '1' });
    if (!pdf) return res.status(404).send('No approved OT for this company/month.');
    res.setHeader('Content-Type', 'application/pdf'); res.setHeader('Content-Disposition', `inline; filename="OT_${req.params.company}_${req.params.month}.pdf"`); res.end(pdf);
  } catch (e) { console.error('[ot report]', e); res.status(500).send(e.message); }
});
router.get('/report/shearing/:company/:month', async (req, res) => {
  if (!(await isMgmtOrHr(req.user))) return res.status(403).send('Not allowed');
  if (!/^\d{4}-\d{2}$/.test(req.params.month) || !COMPANIES[req.params.company]) return res.status(400).send('Bad request');
  try {
    const pdf = await buildShearingCombinedPdf(req.params.company, req.params.month);
    if (!pdf) return res.status(404).send('No approved shearing for this company/month.');
    res.setHeader('Content-Type', 'application/pdf'); res.setHeader('Content-Disposition', `inline; filename="Shearing_${req.params.company}_${req.params.month}.pdf"`); res.end(pdf);
  } catch (e) { console.error('[shearing report]', e); res.status(500).send(e.message); }
});

// ---- Send to Accounts: what's ready per company + the send action ----
// Cleanup: list the people in an OT/Shearing report, and remove one person's entries (for
// duplicate-entry mistakes). Management/admin only. The report regenerates live afterwards.
async function refreshLabourTotals(company, period) {
  await q(`UPDATE labour_period SET
     ot_total = COALESCE((SELECT SUM(amount) FROM labour_ot WHERE company=$1 AND period=$2),0),
     shearing_total = COALESCE((SELECT SUM(amount) FROM labour_shearing WHERE company=$1 AND period=$2),0),
     updated_at=now() WHERE company=$1 AND period=$2`, [company, period]);
}
router.get('/report-people/:kind/:company/:month', async (req, res) => {
  if (!(await isMgmtOrHr(req.user))) return res.status(403).json({ error: 'Management, HR or admin only.' });
  const kind = req.params.kind, C = COMPANIES[req.params.company], month = req.params.month;
  if (!C || !/^\d{4}-\d{2}$/.test(month) || !['ot', 'shearing'].includes(kind)) return res.status(400).json({ error: 'Bad request' });
  const people = [];
  if (kind === 'ot') {
    const staff = (await q(`SELECT e.id, e.emp_no AS code, e.name, COALESCE(SUM(o.hours),0) AS hours, COALESCE(SUM(o.amount),0) AS total
      FROM ot_entries o JOIN employees e ON e.id=o.employee_id
      WHERE e.emp_no LIKE $1 AND o.status IN ('mgmt_approved','paid') AND o.ot_date >= ($2||'-01')::date AND o.ot_date < (($2||'-01')::date + interval '1 month')
      GROUP BY e.id, e.emp_no, e.name ORDER BY e.name`, [C.staffPrefix + '/%', month])).rows;
    const lab = (await q(`SELECT lo.labour_name AS name, MAX(lo.labour_code) AS code, COALESCE(SUM(lo.hours),0) AS hours, COALESCE(SUM(lo.amount),0) AS total
      FROM labour_ot lo WHERE lo.company=$1 AND lo.period=$2 GROUP BY lo.labour_name ORDER BY lo.labour_name`, [C.labourCo, month])).rows;
    staff.forEach(r => people.push({ key: 'emp:' + r.id, name: r.name, code: r.code, kind: 'staff', qty: Number(r.hours), total: Number(r.total) }));
    lab.forEach(r => people.push({ key: 'lab:' + r.name, name: r.name, code: r.code || '\u2014', kind: 'labour', qty: Number(r.hours), total: Number(r.total) }));
  } else {
    const lab = (await q(`SELECT ls.labour_name AS name, MAX(ls.labour_code) AS code, COALESCE(SUM(ls.days),0) AS days, COALESCE(SUM(ls.amount),0) AS total
      FROM labour_shearing ls WHERE ls.company=$1 AND ls.period=$2 GROUP BY ls.labour_name ORDER BY ls.labour_name`, [C.labourCo, month])).rows;
    lab.forEach(r => people.push({ key: 'lab:' + r.name, name: r.name, code: r.code || '\u2014', kind: 'labour', qty: Number(r.days), total: Number(r.total) }));
  }
  res.json({ people, unit: kind === 'ot' ? 'hrs' : 'days' });
});
router.post('/report-remove', async (req, res) => {
  if (!(await isMgmtOrHr(req.user))) return res.status(403).json({ error: 'Management, HR or admin only.' });
  const kind = req.body.kind, C = COMPANIES[req.body.company], month = /^\d{4}-\d{2}$/.test(String(req.body.month || '')) ? req.body.month : null;
  const key = String(req.body.key || '');
  if (!C || !month || !key || !['ot', 'shearing'].includes(kind)) return res.status(400).json({ error: 'Bad request' });
  let changed = 0;
  if (kind === 'ot' && key.startsWith('emp:')) {
    const r = await q(`DELETE FROM ot_entries WHERE employee_id=$1 AND status IN ('mgmt_approved','paid') AND ot_date >= ($2||'-01')::date AND ot_date < (($2||'-01')::date + interval '1 month') RETURNING id`, [+key.slice(4), month]);
    changed = r.rows.length;
  } else if (kind === 'ot' && key.startsWith('lab:')) {
    const r = await q(`DELETE FROM labour_ot WHERE company=$1 AND period=$2 AND labour_name=$3 RETURNING id`, [C.labourCo, month, key.slice(4)]);
    changed = r.rows.length; await refreshLabourTotals(C.labourCo, month);
  } else if (kind === 'shearing') {
    const r = await q(`DELETE FROM labour_shearing WHERE company=$1 AND period=$2 AND labour_name=$3 RETURNING id`, [C.labourCo, month, key.replace(/^lab:/, '')]);
    changed = r.rows.length; await refreshLabourTotals(C.labourCo, month);
  } else return res.status(400).json({ error: 'Bad key' });
  res.json({ ok: true, changed });
});

async function companyExpenseEmployees(companyKey, month) {
  const C = COMPANIES[companyKey];
  return (await q(
    `SELECT e.id, e.name, e.emp_no,
       (SELECT COALESCE(SUM(total_amount),0) FROM expense_submissions s
        WHERE s.employee_id=e.id AND s.status='approved' AND s.paid_at IS NULL AND ${expInMonth('s', '$2')}) AS total
     FROM employees e
     WHERE e.emp_no LIKE $1
       AND EXISTS (SELECT 1 FROM expense_submissions s WHERE s.employee_id=e.id AND s.status='approved' AND s.paid_at IS NULL AND ${expInMonth('s', '$2')})
     ORDER BY e.emp_no`, [C.staffPrefix + '/%', month])).rows;
}
async function companyOtTotal(companyKey, month) {
  const C = COMPANIES[companyKey];
  const staff = (await q(`SELECT COALESCE(SUM(amount),0) AS t, count(DISTINCT employee_id) AS c FROM ot_entries o WHERE o.status='mgmt_approved' AND (SELECT emp_no FROM employees WHERE id=o.employee_id) LIKE $1 AND o.ot_date >= ($2||'-01')::date AND o.ot_date < (($2||'-01')::date + interval '1 month')`, [C.staffPrefix + '/%', month])).rows[0];
  const lab = (await q(`SELECT COALESCE(SUM(lo.amount),0) AS t, count(DISTINCT lo.labour_name) AS c FROM labour_ot lo JOIN labour_period lp ON lp.company=lo.company AND lp.period=lo.period WHERE lo.company=$1 AND lo.period=$2 AND lp.ot_status='approved' AND lo.paid_at IS NULL`, [C.labourCo, month])).rows[0];
  return { total: Number(staff.t) + Number(lab.t), count: Number(staff.c) + Number(lab.c) };
}
async function companyShearingTotal(companyKey, month) {
  const C = COMPANIES[companyKey];
  const r = (await q(`SELECT COALESCE(SUM(ls.amount),0) AS t, count(DISTINCT ls.labour_name) AS c FROM labour_shearing ls JOIN labour_period lp ON lp.company=ls.company AND lp.period=ls.period WHERE ls.company=$1 AND ls.period=$2 AND lp.shearing_status='approved' AND ls.paid_at IS NULL`, [C.labourCo, month])).rows[0];
  return { total: Number(r.t), count: Number(r.c) };
}

// Per-company accounts email + WhatsApp contact (configured in Admin → OT approvers).
async function companyEmail(companyKey) {
  const key = companyKey === 'G2' ? 'accounts_email_g2' : 'accounts_email_bsc';
  const v = (await q(`SELECT value FROM app_settings WHERE key=$1`, [key])).rows[0]?.value;
  return (v && v.trim()) || (companyKey === 'G2' ? 'g2@bharatsteels.in' : 'accounts@bharatsteels.in');
}
async function companyAccountsContact(companyKey) {
  const key = companyKey === 'G2' ? 'accounts_emp_g2' : 'accounts_emp_bsc';
  const id = +((await q(`SELECT value FROM app_settings WHERE key=$1`, [key])).rows[0]?.value || 0);
  if (!id) return null;
  return (await q(`SELECT name, phone FROM employees WHERE id=$1 AND active=TRUE`, [id])).rows[0] || null;
}

router.get('/accounts-queue', async (req, res) => {
  if (!(await isMgmtOrHr(req.user))) return res.status(403).json({ error: 'Management, HR or admin only.' });
  const month = /^\d{4}-\d{2}$/.test(String(req.query.month || '')) ? req.query.month : prevMonth();
  const sentRows = (await q(`SELECT company, kind, to_char(sent_at,'DD Mon HH24:MI') AS at FROM accounts_send WHERE period=$1`, [month])).rows;
  const sentMap = {}; sentRows.forEach(r => { sentMap[r.company + ':' + r.kind] = r.at; });
  const companies = [];
  for (const key of ['BSC', 'G2']) {
    const C = COMPANIES[key];
    const email = await companyEmail(key);
    const emps = await companyExpenseEmployees(key, month);
    const ot = await companyOtTotal(key, month);
    const sh = await companyShearingTotal(key, month);
    const expTotal = emps.reduce((s, e) => s + Number(e.total), 0);
    companies.push({
      key, label: C.label, email,
      expense: { employees: emps.map(e => ({ id: e.id, name: e.name, emp_no: e.emp_no, total: Number(e.total) })), total: expTotal, sent_at: sentMap[key + ':expense'] || null },
      ot: { total: ot.total, count: ot.count, sent_at: sentMap[key + ':ot'] || null },
      shearing: { total: sh.total, count: sh.count, sent_at: sentMap[key + ':shearing'] || null },
    });
  }
  res.json({ month, companies });
});

router.post('/send-to-accounts', async (req, res) => {
  if (!(await isMgmtOrHr(req.user))) return res.status(403).json({ error: 'Management, HR or admin only.' });
  const company = COMPANIES[req.body.company] ? req.body.company : null;
  const month = /^\d{4}-\d{2}$/.test(String(req.body.month || '')) ? req.body.month : null;
  if (!company || !month) return res.status(400).json({ error: 'Bad request' });
  const C = COMPANIES[company];
  const graph = require('../lib/graph');
  const email = await companyEmail(company);
  if (!email) return res.status(400).json({ error: 'No accounts email configured for ' + company });

  const attachments = []; const done = [];
  // 1) per-employee expense
  const emps = await companyExpenseEmployees(company, month);
  for (const e of emps) { const r = await buildEmployeeConsolidatedPdf(e.id, month); if (r) { attachments.push({ name: `Expense - ${e.name.replace(/[^\w .-]/g, '')} - ${month}.pdf`, contentType: 'application/pdf', contentBytes: r.pdf.toString('base64') }); } }
  if (emps.length) done.push({ kind: 'expense', total: emps.reduce((s, e) => s + Number(e.total), 0) });
  // 2) OT combined
  const otPdf = await buildOtCombinedPdf(company, month);
  if (otPdf) { attachments.push({ name: `Overtime - ${company} - ${month}.pdf`, contentType: 'application/pdf', contentBytes: otPdf.toString('base64') }); const ot = await companyOtTotal(company, month); done.push({ kind: 'ot', total: ot.total }); }
  // 3) Shearing combined
  const shPdf = await buildShearingCombinedPdf(company, month);
  if (shPdf) { attachments.push({ name: `Shearing - ${company} - ${month}.pdf`, contentType: 'application/pdf', contentBytes: shPdf.toString('base64') }); const sh = await companyShearingTotal(company, month); done.push({ kind: 'shearing', total: sh.total }); }

  if (!attachments.length) return res.status(400).json({ error: 'Nothing approved & unpaid to send for ' + company });
  await graph.sendMail({
    to: email,
    subject: `Payments — ${C.label} — ${monthLabel(month)} — ${attachments.length} report(s)`,
    html: `<p>Please find attached the approved payment reports for <b>${C.label} — ${monthLabel(month)}</b>:</p>
           <ul>${emps.length ? `<li>${emps.length} employee expense report(s)</li>` : ''}${otPdf ? '<li>Overtime (consolidated)</li>' : ''}${shPdf ? '<li>Shed-B Shearing (consolidated)</li>' : ''}</ul>
           <p>Kindly process the payments.</p>`,
    attachments,
  });
  for (const d of done) {
    await q(`INSERT INTO accounts_send(period,company,kind,total,email,sent_by) VALUES($1,$2,$3,$4,$5,$6)
             ON CONFLICT (period,company,kind) DO UPDATE SET total=EXCLUDED.total, email=EXCLUDED.email, sent_at=now(), sent_by=EXCLUDED.sent_by`,
      [month, company, d.kind, Math.round(d.total), email, req.user.name]);
  }
  // Notify the company's accounts contact on WhatsApp (if configured).
  try {
    const acc = await companyAccountsContact(company);
    if (acc && acc.phone) {
      const wati = require('../lib/wati');
      const grand = done.reduce((s, d) => s + d.total, 0);
      await wati.notify.ot.accounts(acc, { period: `${C.label} · ${monthLabel(month)}`, employees: attachments.length, total: String(Math.round(grand)) });
    }
  } catch (e) { console.error('[accounts contact wa]', e.message); }
  res.json({ ok: true, company, email, attachments: attachments.length, kinds: done.map(d => d.kind) });
});

async function buildEmployeeConsolidatedPdf(empId, month, opts = {}) {
  const includePaid = !!opts.includePaid;
  const emp = (await q(`SELECT id,name,emp_no FROM employees WHERE id=$1`, [empId])).rows[0];
  if (!emp) return null;
  const claims = (await q(
    `SELECT id, form_type, total_amount, final_by_name, to_char(final_at AT TIME ZONE 'Asia/Kolkata','DD Mon YYYY, HH12:MI AM') AS final_at_fmt FROM expense_submissions s
     WHERE employee_id=$1 AND status='approved' ${includePaid ? '' : 'AND paid_at IS NULL'} AND ${expInMonth('s','$2')}
     ORDER BY array_position(ARRAY['conveyance','outstation','misc']::text[], form_type), final_at`, [empId, month])).rows;
  const FORM_LABEL = { conveyance: 'Local Conveyance', outstation: 'Outstation', misc: 'Miscellaneous' };
  const breakdown = claims.map(c => ({ label: FORM_LABEL[c.form_type] || c.form_type, amount: Number(c.total_amount || 0), by: c.final_by_name || '\u2014', at: c.final_at_fmt || '' }));
  const total = breakdown.reduce((s, b) => s + b.amount, 0);
  if (total <= 0) return null;

  const { buildCoverPdf, mergePdfs } = require('../lib/consolidated');
  const cover = await buildCoverPdf({ empName: emp.name, empNo: emp.emp_no, monthLabel: monthLabel(month), breakdown, total });
  const expense = require('./expense.routes');
  // Regenerate every claim PDF in parallel (was sequential — slow / timed out for heavy employees).
  const claimPdfs = await Promise.all(claims.map(c => expense._internal.claimPdfById(c.id).catch(e => { console.error('[consolidated claimPdf]', c.id, e.message); return null; })));
  const parts = [cover, ...claimPdfs.filter(Boolean)];
  const merged = await mergePdfs(parts);
  // Approval badge(s) on every page (page 1 cover included). One badge per distinct final approver.
  const approvals = []; const _seen = new Set();
  for (const b of breakdown) {
    if (!b.by || b.by === '—') continue;
    const k = b.by + '|' + (b.at || ''); if (_seen.has(k)) continue; _seen.add(k);
    approvals.push({ label: 'Final approved', by: b.by, at: b.at });
  }
  const stamped = await stampApprovals(merged, approvals, { preview: approvals.length === 0 });
  return { pdf: stamped, total, emp, breakdown };
}

// Run the monthly consolidation for a given month: one email per company (by emp_no prefix)
// with one consolidated PDF attachment per employee.
async function recordSent(period, empId, total, acct, by) {
  await q(`INSERT INTO payment_run_sent(period,employee_id,total,accounts_email,sent_by)
           VALUES($1,$2,$3,$4,$5)
           ON CONFLICT (period,employee_id) DO UPDATE SET total=EXCLUDED.total, accounts_email=EXCLUDED.accounts_email, sent_at=now(), sent_by=EXCLUDED.sent_by`,
    [period, empId, Math.round(total), acct, by || null]);
}

// One email per company (by emp_no prefix) with one consolidated PDF per employee.
// Employees already emailed for this month are skipped (no double-send).
async function runMonthlyAccounts(month, byName) {
  const chain = require('../lib/chain');
  const graph = require('../lib/graph');
  const cfg = await chain.getChain();
  const emps = (await q(
    `SELECT DISTINCT e.id, e.name, e.emp_no FROM employees e
     WHERE (EXISTS (SELECT 1 FROM expense_submissions s WHERE s.employee_id=e.id AND s.status='approved' AND s.paid_at IS NULL AND ${expInMonth('s','$1')})
        OR EXISTS (SELECT 1 FROM ot_entries o WHERE o.employee_id=e.id AND ${otInMonth('o','$1')}))
       AND NOT EXISTS (SELECT 1 FROM payment_run_sent r WHERE r.period=$1 AND r.employee_id=e.id)
     ORDER BY e.emp_no`, [month])).rows;
  const byPrefix = {};
  for (const e of emps) { const pfx = String(e.emp_no || '').split('/')[0].toUpperCase() || 'BSC'; (byPrefix[pfx] = byPrefix[pfx] || []).push(e); }
  const sent = [];
  for (const pfx of Object.keys(byPrefix)) {
    const acct = chain.accountsEmailFor(cfg, pfx + '/x');
    if (!acct) continue;
    const attachments = []; const done = [];
    for (const e of byPrefix[pfx]) {
      const r = await buildEmployeeConsolidatedPdf(e.id, month);
      if (r) { attachments.push({ name: `${e.name.replace(/[^\w .-]/g, '')} - ${month}.pdf`, contentType: 'application/pdf', contentBytes: r.pdf.toString('base64') }); done.push({ id: e.id, total: r.total }); }
    }
    if (attachments.length) {
      await graph.sendMail({
        to: acct,
        subject: `Monthly payments — ${pfx} — ${monthLabel(month)} — ${attachments.length} employees`,
        html: `<p>Please find attached the consolidated monthly payment reports (one per employee) for <b>${pfx} — ${monthLabel(month)}</b>.</p><p>Kindly process the payments.</p>`,
        attachments,
      });
      for (const d of done) await recordSent(month, d.id, d.total, acct, byName || 'Monthly run');
      sent.push({ company: pfx, accounts: acct, employees: attachments.length });
    }
  }
  return { ok: true, month, sent };
}

router.post('/monthly-run', async (req, res) => {
  if (!(await isMgmt(req.user))) return res.status(403).json({ error: 'Management / admin only.' });
  const month = /^\d{4}-\d{2}$/.test(String(req.body.month || '')) ? req.body.month : prevMonth();
  try { res.json(await runMonthlyAccounts(month, req.user.name)); }
  catch (e) { console.error('[monthly-run]', e); res.status(500).json({ error: e.message }); }
});

// Late catch-up: send one employee's consolidated report on demand (after the 5th).
router.post('/send-employee', async (req, res) => {
  if (!(await isMgmt(req.user))) return res.status(403).json({ error: 'Management / admin only.' });
  const month = /^\d{4}-\d{2}$/.test(String(req.body.month || '')) ? req.body.month : prevMonth();
  const empId = +req.body.emp_id;
  const r = await buildEmployeeConsolidatedPdf(empId, month);
  if (!r) return res.status(400).json({ error: 'No approved payments for this employee in that month.' });
  const chain = require('../lib/chain');
const graph = require('../lib/graph'); const cfg = await chain.getChain();
  const acct = chain.accountsEmailFor(cfg, r.emp.emp_no);
  await graph.sendMail({
    to: acct,
    subject: `Payment report — ${r.emp.name} — ${monthLabel(month)} — ${money(r.total)}`,
    html: `<p>Consolidated payment report for <b>${r.emp.name} (${r.emp.emp_no})</b> — ${monthLabel(month)}. Kindly process.</p>`,
    attachments: [{ name: `${r.emp.name.replace(/[^\w .-]/g, '')} - ${month}.pdf`, contentType: 'application/pdf', contentBytes: r.pdf.toString('base64') }],
  });
  await recordSent(month, r.emp.id, r.total, acct, req.user.name);
  res.json({ ok: true, total: r.total, accounts: acct, emp: r.emp.name });
});

// Employees who have approved payments in a month (for the catch-up picker).
// Per-employee OT detail report (per-day rows) — used by the Final Approvals "PDF" on an OT row.
// Includes PENDING entries (mgmt_pending) so management can preview exactly what they are about to
// approve, as well as mgmt_approved / paid. Works before final approval.
async function buildOtEmployeePdf(empId, month) {
  const { buildPaymentReportPDF } = require('../lib/payment_pdf');
  const emp = (await q(`SELECT id,name,emp_no FROM employees WHERE id=$1`, [empId])).rows[0];
  if (!emp) return null;
  const rows = (await q(
    `SELECT to_char(ot_date,'DD Mon YYYY') AS d, end_time, hours, amount, status
     FROM ot_entries
     WHERE employee_id=$1 AND status IN ('mgmt_pending','mgmt_approved','paid')
       AND ot_date >= ($2||'-01')::date AND ot_date < (($2||'-01')::date + interval '1 month')
     ORDER BY ot_date`, [empId, month])).rows;
  if (!rows.length) return null;
  const total = rows.reduce((s, r) => s + Number(r.amount || 0), 0);
  const allApproved = rows.every(r => r.status === 'mgmt_approved' || r.status === 'paid');
  const stLabel = { mgmt_pending: 'Pending', mgmt_approved: 'Approved', paid: 'Paid' };
  const pdf = await buildPaymentReportPDF({
    title: `Overtime — ${emp.name}`,
    subtitle: `${emp.emp_no} · ${monthLabel(month)}${allApproved ? '' : ' · PREVIEW (awaiting approval)'}`,
    approved: allApproved,
    sections: [{ name: 'Overtime — day-wise', total,
      cols: [{ label: 'Date', width: 0.3 }, { label: 'End time', width: 0.18 }, { label: 'Hours', width: 0.17, align: 'right' }, { label: 'Amount', width: 0.2, align: 'right' }, { label: 'Status', width: 0.15 }],
      rows: rows.map(r => [r.d, r.end_time || '\u2014', (+r.hours).toFixed(2), money(r.amount), stLabel[r.status] || r.status]) }],
    grandTotal: total,
  });
  return { pdf, total, emp };
}
router.get('/ot-employee/:empId/:month', async (req, res) => {
  try {
    if (!(await isMgmt(req.user))) return res.status(403).send('Not allowed');
    if (!/^\d{4}-\d{2}$/.test(req.params.month)) return res.status(400).send('Bad month');
    let empId = +req.params.empId;
    if (!empId) {
      const no = String(req.params.empId).replace(/_/g, '/');
      empId = (await q(`SELECT id FROM employees WHERE emp_no=$1 OR emp_no=$2 LIMIT 1`, [no, req.params.empId])).rows[0]?.id || 0;
    }
    if (!empId) return res.status(404).send('Employee not found.');
    const r = await buildOtEmployeePdf(empId, req.params.month);
    if (!r) return res.status(404).type('text/plain').send('No overtime entries for this employee in ' + req.params.month + '.');
    res.setHeader('Content-Type', 'application/pdf');
    res.setHeader('Content-Disposition', `inline; filename="OT - ${r.emp.name.replace(/[^\w .-]/g, '')} - ${req.params.month}.pdf"`);
    res.end(r.pdf);
  } catch (e) { console.error('[ot-employee report]', e); res.status(500).send('Could not build the report: ' + e.message); }
});

// Preview an employee's consolidated report (breakdown + each claim/OT) before it's emailed.
router.get('/employee-report/:empId/:month', async (req, res) => {
  try {
    if (!(await isMgmt(req.user))) return res.status(403).send('Not allowed');
    if (!/^\d{4}-\d{2}$/.test(req.params.month)) return res.status(400).send('Bad month');
    // Accept a numeric employee id OR an emp code (e.g. BSC/119 or BSC_119).
    let empId = +req.params.empId;
    if (!empId) {
      const no = String(req.params.empId).replace(/_/g, '/');
      empId = (await q(`SELECT id FROM employees WHERE emp_no=$1 OR emp_no=$2 LIMIT 1`, [no, req.params.empId])).rows[0]?.id || 0;
    }
    if (!empId) return res.status(404).send('Employee not found.');
    const includePaid = req.query.all === '1' || req.query.all === 'true';
    const r = await buildEmployeeConsolidatedPdf(empId, req.params.month, { includePaid });
    if (!r) {
      const m = req.params.month;
      const exp = (await q(`SELECT form_type, status, (paid_at IS NOT NULL) AS paid, total_amount FROM expense_submissions s WHERE employee_id=$1 AND ${expInMonth('s', '$2')} ORDER BY form_type`, [empId, m])).rows;
      const ot = (await q(`SELECT status, count(*) c, sum(amount) amt FROM ot_entries WHERE employee_id=$1 AND ot_date >= ($2||'-01')::date AND ot_date < (($2||'-01')::date + interval '1 month') GROUP BY status`, [empId, m])).rows;
      let msg = `No UNPAID, final-approved payments for this employee in ${m}.\n\nThe consolidated report includes ONLY items that are Final Approved AND Unpaid.\n\nWhat this employee has this month:\n`;
      if (exp.length) exp.forEach(e => { msg += `  • ${e.form_type}: \u20b9${e.total_amount} — status=${e.status}${e.paid ? '  [PAID → excluded]' : ''}\n`; });
      else msg += '  • (no expense claims fall in this month)\n';
      if (ot.length) ot.forEach(o => { msg += `  • OT: \u20b9${o.amt} — status=${o.status} (${o.c} entries)\n`; });
      msg += `\nFix: approve any 'pending_final' items, and un-mark any that are Paid, in Final Approvals.\nTo preview the report design with PAID items included, add ?all=1 to the URL.`;
      return res.status(404).type('text/plain').send(msg);
    }
    res.setHeader('Content-Type', 'application/pdf');
    res.setHeader('Content-Disposition', `inline; filename="${r.emp.name.replace(/[^\w .-]/g, '')} - ${req.params.month}.pdf"`);
    res.end(r.pdf);
  } catch (e) { console.error('[employee-report]', e); res.status(500).send('Could not build the report: ' + e.message); }
});

// Everything for a month that's relevant to final approval, grouped by category, each with its
// status (approved / pending). Drives the month-centric Final Approval screen.
router.get('/month-items', async (req, res) => {
  if (!(await isMgmt(req.user))) return res.status(403).json({ error: 'Management / admin only.' });
  const month = /^\d{4}-\d{2}$/.test(String(req.query.month || '')) ? req.query.month : prevMonth();
  try {
  const cats = {
    conveyance: { key: 'conveyance', label: 'Local Conveyance', items: [] },
    outstation: { key: 'outstation', label: 'Outstation', items: [] },
    misc: { key: 'misc', label: 'Miscellaneous', items: [] },
    ot: { key: 'ot', label: 'Overtime (staff)', items: [] },
    labour: { key: 'labour', label: 'Labour (OT + Shearing)', items: [] },
  };
  const exp = (await q(
    `SELECT s.id, s.form_type, s.total_amount, s.status, s.paid_at, s.final_by_name, s.pdf_token, e.name AS emp_name, e.emp_no
     FROM expense_submissions s JOIN employees e ON e.id=s.employee_id
     WHERE s.status IN ('pending_final','approved','settled_offline') AND ${expInMonth('s', '$1')}
     ORDER BY e.name`, [month])).rows;
  const mids = ((await q(`SELECT value FROM app_settings WHERE key='ot_mgmt_emp_ids'`)).rows[0]?.value || '').split(',').map(Number).filter(Boolean);
  const mgmtNames = mids.length ? (await q(`SELECT name FROM employees WHERE id = ANY($1)`, [mids])).rows.map(r => r.name) : [];
  exp.forEach(r => {
    const state = r.status === 'settled_offline' ? 'offline' : (r.status === 'approved' ? 'approved' : 'pending');
    const paid = !!r.paid_at;
    (cats[r.form_type] || cats.misc).items.push({
      payee: r.emp_name, emp_no: r.emp_no, amount: Number(r.total_amount || 0), state, paid,
      approved_by: state === 'approved' ? (r.final_by_name || '') : null,
      nonmgmt: state === 'approved' && r.final_by_name && !mgmtNames.includes(r.final_by_name),
      pdf_token: r.pdf_token || null,
      approve: state === 'pending' ? { url: '/expense/' + r.id + '/final-approve' } : null,
      paidToggle: state === 'approved' ? { url: '/expense/' + r.id + '/' + (paid ? 'unmark-paid' : 'mark-paid') } : null,
      reopen: (state === 'approved' && !paid) ? { url: '/expense/' + r.id + '/reopen-final' } : null,
    });
  });
  // Staff OT — one row per employee (approved individually), by OT date's calendar month.
  const ot = (await q(
    `SELECT e.id AS emp_id, e.name, e.emp_no, COALESCE(SUM(o.hours),0) AS hours, COALESCE(SUM(o.amount),0) AS amount,
            bool_or(o.status='mgmt_pending') AS has_pending, bool_or(o.status='mgmt_approved') AS has_unpaid
     FROM ot_entries o JOIN employees e ON e.id=o.employee_id
     WHERE o.status IN ('mgmt_pending','mgmt_approved','paid')
       AND o.ot_date >= ($1||'-01')::date AND o.ot_date < (($1||'-01')::date + interval '1 month')
     GROUP BY e.id, e.name, e.emp_no ORDER BY e.name`, [month])).rows;
  ot.forEach(r => {
    const state = r.has_pending ? 'pending' : 'approved';
    const paid = !r.has_pending && !r.has_unpaid; // all final entries are 'paid'
    cats.ot.items.push({
      payee: r.name, emp_no: r.emp_no, amount: Number(r.amount || 0), state, paid,
      report_url: `/api/final/ot-employee/${r.emp_id}/${month}`,
      approve: state === 'pending' ? { url: '/ot/mgmt-employee-approve', body: { emp_id: r.emp_id, month } } : null,
      paidToggle: state === 'approved' ? { url: '/ot/mgmt-employee-paid', body: { emp_id: r.emp_id, month, paid: !paid } } : null,
      reopen: (state === 'approved' && !paid) ? { url: '/ot/mgmt-employee-unapprove', body: { emp_id: r.emp_id, month } } : null,
    });
  });
  const lab = (await q(`SELECT company, period, ot_total, shearing_total, ot_status, shearing_status FROM labour_period WHERE period=$1`, [month])).rows;
  lab.forEach(p => {
    for (const part of ['ot', 'shearing']) {
      const st = part === 'ot' ? p.ot_status : p.shearing_status;
      if (st !== 'pending_mgmt' && st !== 'approved') continue;
      const state = st === 'approved' ? 'approved' : 'pending';
      cats.labour.items.push({
        payee: (LABOUR_CO[p.company] || p.company) + ' · ' + (part === 'ot' ? 'Overtime' : 'Shearing'),
        amount: Number((part === 'ot' ? p.ot_total : p.shearing_total) || 0), state,
        report_url: `/api/labour/report-pdf/${p.company}/${p.period}/${part}`,
        approve: state === 'pending' ? { url: '/labour/approve', body: { company: p.company, month: p.period, part } } : null,
      });
    }
  });
  let pending = 0, total = 0, count = 0;
  Object.values(cats).forEach(c => c.items.forEach(i => { count++; total += i.amount; if (i.state === 'pending') pending++; }));
  res.json({ month, categories: Object.values(cats).filter(c => c.items.length), pending, count, total, all_approved: count > 0 && pending === 0 });
  } catch (e) { console.error('[month-items]', e); res.status(500).json({ error: e.message }); }
});

router.get('/monthly-list', async (req, res) => {
  if (!(await isMgmt(req.user))) return res.status(403).json({ error: 'Management / admin only.' });
  const month = /^\d{4}-\d{2}$/.test(String(req.query.month || '')) ? req.query.month : prevMonth();
  const rows = (await q(
    `SELECT e.id, e.name, e.emp_no,
       COALESCE((SELECT SUM(total_amount) FROM expense_submissions s WHERE s.employee_id=e.id AND s.status='approved' AND s.paid_at IS NULL AND ${expInMonth('s','$1')}),0)
       + COALESCE((SELECT SUM(amount) FROM ot_entries o WHERE o.employee_id=e.id AND ${otInMonth('o','$1')}),0) AS total,
       (SELECT to_char(sent_at,'DD Mon HH24:MI') FROM payment_run_sent r WHERE r.period=$1 AND r.employee_id=e.id) AS sent_at
     FROM employees e
     WHERE EXISTS (SELECT 1 FROM expense_submissions s WHERE s.employee_id=e.id AND s.status='approved' AND s.paid_at IS NULL AND ${expInMonth('s','$1')})
        OR EXISTS (SELECT 1 FROM ot_entries o WHERE o.employee_id=e.id AND ${otInMonth('o','$1')})
     ORDER BY (SELECT 1 FROM payment_run_sent r WHERE r.period=$1 AND r.employee_id=e.id) NULLS FIRST, e.emp_no`, [month])).rows;
  res.json({ month, employees: rows });
});

module.exports._internal = { runMonthlyAccounts, buildEmployeeConsolidatedPdf, prevMonth, expInMonth, COMPANIES };
