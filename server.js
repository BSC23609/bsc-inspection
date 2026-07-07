const express     = require('express');
const cors        = require('cors');
const fetch       = require('node-fetch');
const PDFDocument = require('pdfkit');

const app = express();
app.use(cors());
app.use(express.json({ limit: '50mb' }));

app.use((req, res, next) => {
  res.setTimeout(120000, () => {
    res.status(408).json({ status: 'error', message: 'Request timed out.' });
  });
  next();
});

const path = require('path');
app.use(express.static(path.join(__dirname, 'public')));

// ---- Auth & DB (Neon Postgres + JWT cookie sessions) ----
// Additive: this does NOT gate existing routes yet. Login lives under /auth/*.
const cookieParser = require('cookie-parser');
const { authRouter, requireAuth, requireEmployee, requireAdmin, requireCustomer, requireModule } = require('./auth');
const { q: pgq } = require('./db');
const reportsMod = require('./reports');
app.use(cookieParser());
app.get('/reports/dispatch/preview', async (req, res) => {
  if (!process.env.SETUP_KEY || req.query.key !== process.env.SETUP_KEY) return res.status(403).send('bad key');
  const code = String(req.query.code || '').trim();
  const ym   = String(req.query.month || '').trim();
  if (!/^\d+$/.test(code) || !/^\d{4}-\d{2}$/.test(ym)) return res.status(400).send('Use ?key=YOUR_SETUP_KEY&code=7206270&month=2026-06');
  try {
    const out = await reportsMod.buildMonthlyReport({ code, ym });
    res.setHeader('Content-Type', 'application/pdf');
    res.setHeader('Content-Disposition', 'inline; filename="dispatch-' + code + '-' + ym + '.pdf"');
    res.send(out.pdf);
  } catch (e) { console.error('[reports] preview', e.message); res.status(500).send('Report error: ' + e.message); }
});

// Admin-session preview (cookie-auth; used by the admin Reports panel)
app.get('/reports/admin/preview', requireAuth, requireAdmin, async (req, res) => {
  const code = String(req.query.code || '').trim();
  const ym   = String(req.query.month || '').trim();
  if (!/^\d+$/.test(code) || !/^\d{4}-\d{2}$/.test(ym)) return res.status(400).send('need code & month');
  try {
    const out = await reportsMod.buildMonthlyReport({ code, ym });
    res.setHeader('Content-Type', 'application/pdf');
    res.setHeader('Content-Disposition', 'inline; filename="report-' + code + '-' + ym + '.pdf"');
    res.send(out.pdf);
  } catch (e) { console.error('[reports] admin preview', e.message); res.status(500).send('Report error: ' + e.message); }
});

function prevMonthYM(){
  const d = new Date();
  d.setUTCDate(0); // last day of previous month
  return d.getUTCFullYear() + '-' + String(d.getUTCMonth() + 1).padStart(2, '0');
}

function splitRecipients(s){ return String(s || '').split(/[\s,;]+/).map(x => x.trim()).filter(Boolean); }
function normWa(s){ let n = String(s || '').replace(/\D/g, ''); if (n.length === 10) n = '91' + n; return n; }
async function sendReportEmail(cust, ym, pdf, trips, complaints){
  const [yy, mm] = ym.split('-').map(Number);
  const monthName = new Date(yy, mm - 1, 1).toLocaleString('en-IN', { month: 'long', year: 'numeric' });
  const fname = ('Dispatch-Report-' + (cust.company || cust.code) + '-' + ym).replace(/[^A-Za-z0-9_\-]+/g, '_') + '.pdf';
  const html = '<div style="font-family:Arial,Helvetica,sans-serif;max-width:560px;color:#101828">'
    + '<h2 style="color:#0F6CB6;margin:0 0 6px">Monthly Dispatch Report</h2>'
    + '<p style="font-size:13px;color:#475467;margin:0 0 10px">Dear ' + (cust.company || 'Customer') + ',</p>'
    + '<p style="font-size:13px;color:#475467;margin:0 0 12px">Please find attached your dispatch report for <b>' + monthName + '</b>.</p>'
    + '<table style="font-size:13px;border-collapse:collapse"><tr><td style="padding:3px 14px 3px 0;color:#667085">Total trips</td><td style="font-weight:700">' + trips + '</td></tr>'
    + '<tr><td style="padding:3px 14px 3px 0;color:#667085">Complaints logged</td><td style="font-weight:700">' + complaints + '</td></tr></table>'
    + '<p style="font-size:12px;color:#98A2B3;margin-top:18px">Bharat Steel (Chennai) Pvt. Ltd.</p></div>';
  const toList = splitRecipients(cust.email);
  return sendEmail({
    to: toList.length ? toList : [cust.email],
    cc: ['info@bharatsteels.in'],
    subject: 'Dispatch Report — ' + monthName + ' — ' + (cust.company || cust.code),
    html,
    text: 'Dear ' + (cust.company || 'Customer') + ', attached is your dispatch report for ' + monthName + '. Total trips: ' + trips + ', complaints: ' + complaints + '.',
    attachments: [{ filename: fname, content: pdf.toString('base64') }]
  });
}

const WATI_REPORT_TEMPLATE = process.env.WATI_REPORT_TEMPLATE || 'monthly_dispatch_report';
async function sendReportWhatsApp(cust, ym){
  if (!WATI_ENDPOINT || !WATI_TOKEN) return { skipped: 'wati_not_configured' };
  if (!process.env.REPORT_PUBLIC_TOKEN) return { skipped: 'no_public_token' };
  const nums = splitRecipients(cust.whatsapp).map(normWa).filter(Boolean);
  if (!nums.length) return { skipped: 'no_number' };
  const [yy, mm] = ym.split('-').map(Number);
  const monthName = new Date(yy, mm - 1, 1).toLocaleString('en-IN', { month: 'long', year: 'numeric' });
  const link = 'https://qms.bharatsteels.in/reports/file/' + encodeURIComponent(cust.code) + '/' + ym + '?t=' + encodeURIComponent(process.env.REPORT_PUBLIC_TOKEN);
  const parameters = [
    { name: 'company', value: String(cust.company || '') },
    { name: 'month', value: monthName },
    { name: 'link', value: link }
  ];
  const auth = WATI_TOKEN.startsWith('Bearer') ? WATI_TOKEN : ('Bearer ' + WATI_TOKEN);
  let okCount = 0; const errs = [];
  for (const num of nums){
    try {
      const url = WATI_ENDPOINT + '/api/v1/sendTemplateMessage?whatsappNumber=' + encodeURIComponent(num);
      const resp = await fetch(url, { method: 'POST', headers: { 'Authorization': auth, 'Content-Type': 'application/json' },
        body: JSON.stringify({ template_name: WATI_REPORT_TEMPLATE, broadcast_name: 'monthly_report_' + (cust.code || '') + '_' + ym, parameters }) });
      const txt = await resp.text();
      if (!resp.ok) throw new Error('WATI ' + resp.status + ': ' + txt.slice(0, 120));
      okCount++;
    } catch (e) { errs.push(num + ': ' + e.message); }
  }
  if (!okCount && errs.length) throw new Error(errs.join(' | '));
  return { sent: okCount, total: nums.length, errors: errs.length ? errs : undefined };
}

async function runMonthlyReports(ym){
  const cust = await pgq("SELECT * FROM customers WHERE active=true AND email IS NOT NULL AND email <> '' ORDER BY company");
  const results = [];
  for (const c of cust.rows){
    try {
      const out = await reportsMod.buildMonthlyReport({ code: c.code, ym });
      if (out.trips === 0 && out.complaints === 0){ results.push({ code: c.code, company: c.company, skipped: 'no data' }); continue; }
      await sendReportEmail(c, ym, out.pdf, out.trips, out.complaints);
      let wa = null;
      try { wa = await sendReportWhatsApp(c, ym); } catch (e) { wa = { error: e.message }; }
      results.push({ code: c.code, company: c.company, email: c.email, trips: out.trips, complaints: out.complaints, sent: true, whatsapp: wa });
    } catch (e) { results.push({ code: c.code, company: c.company, error: e.message }); }
  }
  return results;
}

// Cron-triggered run (key-auth). Defaults to previous month.
app.post('/reports/monthly/run', async (req, res) => {
  if (!process.env.SETUP_KEY || req.query.key !== process.env.SETUP_KEY) return res.status(403).json({ error: 'bad_key' });
  const ym = /^\d{4}-\d{2}$/.test(req.query.month || '') ? req.query.month : prevMonthYM();
  try { const results = await runMonthlyReports(ym); res.json({ ok: true, month: ym, sent: results.filter(r => r.sent).length, total: results.length, results }); }
  catch (e) { console.error('[reports] run', e.message); res.status(500).json({ error: 'run_failed', detail: e.message }); }
});

// Admin-session manual send (used by the Reports panel)
app.post('/reports/monthly/send', requireAuth, requireAdmin, async (req, res) => {
  const ym = /^\d{4}-\d{2}$/.test(req.query.month || '') ? req.query.month : prevMonthYM();
  try { const results = await runMonthlyReports(ym); res.json({ ok: true, month: ym, sent: results.filter(r => r.sent).length, total: results.length, results }); }
  catch (e) { console.error('[reports] send', e.message); res.status(500).json({ error: 'send_failed', detail: e.message }); }
});

// Public tokenized report download (the WhatsApp message links here)
app.get('/reports/file/:code/:ym', async (req, res) => {
  if (!process.env.REPORT_PUBLIC_TOKEN || req.query.t !== process.env.REPORT_PUBLIC_TOKEN) return res.status(403).send('forbidden');
  const code = String(req.params.code || '').trim();
  const ym = String(req.params.ym || '').replace(/\.pdf$/i, '').trim();
  if (!/^\d+$/.test(code) || !/^\d{4}-\d{2}$/.test(ym)) return res.status(400).send('bad params');
  try {
    const out = await reportsMod.buildMonthlyReport({ code, ym });
    res.setHeader('Content-Type', 'application/pdf');
    res.setHeader('Content-Disposition', 'inline; filename="Dispatch-Report-' + code + '-' + ym + '.pdf"');
    res.send(out.pdf);
  } catch (e) { console.error('[reports] file', e.message); res.status(500).send('error'); }
});
app.use('/auth', authRouter);

// ===================================================
// CUSTOMER PORTAL (brick 3) — Postgres-backed intake
// ===================================================
function ccPublic(r){
  return { id:r.id, ref:r.ref, company:r.company, filed_by:r.filed_by, contact_email:r.contact_email, mobile:r.mobile,
    grade:r.grade, dimensions:r.dimensions, batch_no:r.batch_no, tc_no:r.tc_no,
    invoice_no:r.invoice_no, invoice_date:r.invoice_date, quantity:r.quantity,
    description:r.description, status:r.status, decision_note:r.decision_note,
    resolution_note:r.resolution_note, customer_response:r.customer_response,
    handler_emp:r.handler_emp,
    photos:(Array.isArray(r.photos)?r.photos:[]).map(function(_,i){return '/portal/complaint-photo?id='+r.id+'&i='+i;}),
    created_at:r.created_at, updated_at:r.updated_at };
}

const COMPLAINT_NOTIFY_TO = (process.env.COMPLAINT_NOTIFY_TO || 'info@bharatsteels.in').split(/[\s,;]+/).map(s => s.trim()).filter(Boolean);
const COMPLAINT_NOTIFY_CC = (process.env.COMPLAINT_NOTIFY_CC || 'shivamshroff1997@gmail.com').split(/[\s,;]+/).map(s => s.trim()).filter(Boolean);
async function notifyNewComplaint(row){
  try {
    if (!RESEND_API_KEY) { console.log('[portal] RESEND_API_KEY not set; skip complaint email'); return; }
    const esc = s => String(s==null?'':s).replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
    const link   = 'https://qms.bharatsteels.in/?submission=' + encodeURIComponent(row.ref || row.id);
    const waText = encodeURIComponent('New BSC complaint ' + (row.ref||'') + ' from ' + (row.company||'') + '. Please review in the QMS.');
    const waLink = 'https://wa.me/919884384261?text=' + waText; // Shivam
    const rows = [
      ['Reference', row.ref],['Customer', row.company],['Filed by', row.filed_by],
      ['Mobile', row.mobile],['Email', row.contact_email],['Grade', row.grade],
      ['Dimensions', row.dimensions],['Batch No', row.batch_no],['TC Number', row.tc_no],
      ['Invoice No', row.invoice_no],['Invoice Date', row.invoice_date],['Quantity', row.quantity]
    ].filter(x => x[1]).map(x =>
      '<tr><td style="padding:5px 12px;color:#667085;border-bottom:1px solid #F2F4F7">'+esc(x[0])+
      '</td><td style="padding:5px 12px;font-weight:600;border-bottom:1px solid #F2F4F7">'+esc(x[1])+'</td></tr>').join('');
    const html = '<div style="font-family:Arial,Helvetica,sans-serif;max-width:580px;color:#101828">'
      + '<h2 style="color:#0F6CB6;margin:0 0 4px">New customer complaint</h2>'
      + '<p style="color:#475467;margin:0 0 14px;font-size:13px">A new quality complaint was logged in the QMS complaint portal.</p>'
      + '<table style="border-collapse:collapse;font-size:13px;border:1px solid #EAECF0;border-radius:8px;width:100%">'+rows+'</table>'
      + '<p style="margin:16px 0 6px;font-size:13px;color:#475467">Description</p>'
      + '<div style="font-size:13px;background:#F8FAFC;border:1px solid #EAECF0;border-radius:8px;padding:10px;white-space:pre-wrap">'+esc(row.description||'')+'</div>'
      + (row.photos && row.photos.length ? '<p style="font-size:12px;color:#667085;margin-top:8px">'+row.photos.length+' photo(s) attached — view them in the portal.</p>' : '')
      + '<div style="margin-top:18px">'
      + '<a href="'+link+'" style="background:#0F6CB6;color:#fff;text-decoration:none;padding:10px 16px;border-radius:8px;font-size:13px;font-weight:600">Open in QMS</a>&nbsp;&nbsp;'
      + '<a href="'+waLink+'" style="background:#25D366;color:#fff;text-decoration:none;padding:10px 16px;border-radius:8px;font-size:13px;font-weight:600">WhatsApp Shivam</a>'
      + '</div></div>';
    await sendEmail({
      to: COMPLAINT_NOTIFY_TO,
      cc: COMPLAINT_NOTIFY_CC,
      subject: 'New complaint ' + (row.ref||'') + ' — ' + (row.company||''),
      html,
      text: 'New complaint ' + (row.ref||'') + ' from ' + (row.company||'') + '. Filed by ' + (row.filed_by||'-') + '. Open: ' + link
    });
  } catch (e) { console.error('[portal] notifyNewComplaint', e.message); }
}

// ---- WATI WhatsApp template notification ----
const WATI_ENDPOINT = (process.env.WATI_API_ENDPOINT || '').replace(/\/+$/,'');
const WATI_TOKEN    = process.env.WATI_ACCESS_TOKEN || '';
const WATI_TEMPLATE = process.env.WATI_TEMPLATE_NAME || 'new_complaint_alert';
const WATI_NUMBERS  = (process.env.WATI_NOTIFY_NUMBERS || '919884384261').split(',').map(s => s.trim()).filter(Boolean);

async function sendWatiComplaint(row){
  if (!WATI_ENDPOINT || !WATI_TOKEN) { console.log('[wati] not configured; skip WhatsApp'); return; }
  const link = 'https://qms.bharatsteels.in/?submission=' + encodeURIComponent(row.ref || row.id);
  const parameters = [
    { name:'ref',      value: String(row.ref || '') },
    { name:'company',  value: String(row.company || '') },
    { name:'filed_by', value: String(row.filed_by || '-') },
    { name:'link',     value: link }
  ];
  const auth = WATI_TOKEN.startsWith('Bearer') ? WATI_TOKEN : ('Bearer ' + WATI_TOKEN);
  for (const num of WATI_NUMBERS) {
    try {
      const url = WATI_ENDPOINT + '/api/v1/sendTemplateMessage?whatsappNumber=' + encodeURIComponent(num);
      const resp = await fetch(url, {
        method:'POST',
        headers:{ 'Authorization': auth, 'Content-Type':'application/json' },
        body: JSON.stringify({ template_name: WATI_TEMPLATE, broadcast_name: 'qms_complaint_' + (row.ref || row.id), parameters })
      });
      const txt = await resp.text();
      if (!resp.ok) console.error('[wati] send failed', num, resp.status, txt);
      else console.log('[wati] sent to', num);
    } catch (e) { console.error('[wati] error', num, e.message); }
  }
}

// ---- WATI diagnostic (key-gated). Returns the raw WATI response so you can see the exact reason a send fails. ----
// Usage: /reports/wati-test?key=SETUP_KEY&number=9198XXXXXXXX&template=monthly_dispatch_report1
app.get('/reports/wati-test', async (req, res) => {
  if (!process.env.SETUP_KEY || req.query.key !== process.env.SETUP_KEY) return res.status(403).json({ error: 'bad_key' });
  const env = {
    WATI_API_ENDPOINT_set: !!WATI_ENDPOINT,
    WATI_API_ENDPOINT_value: WATI_ENDPOINT || null,
    WATI_ACCESS_TOKEN_set: !!WATI_TOKEN,
    WATI_REPORT_TEMPLATE: WATI_REPORT_TEMPLATE,
    WATI_TEMPLATE_NAME_complaint: WATI_TEMPLATE,
    WATI_NOTIFY_NUMBERS: WATI_NUMBERS,
    REPORT_PUBLIC_TOKEN_set: !!process.env.REPORT_PUBLIC_TOKEN
  };
  if (!WATI_ENDPOINT || !WATI_TOKEN) return res.json({ env, verdict: 'wati_not_configured (set WATI_API_ENDPOINT + WATI_ACCESS_TOKEN)' });
  const number = normWa(String(req.query.number || ''));
  if (!number) return res.json({ env, verdict: 'pass ?number=9198XXXXXXXX to attempt a real test send' });
  const template = String(req.query.template || WATI_REPORT_TEMPLATE);
  const parameters = [
    { name: 'company', value: 'TEST COMPANY' },
    { name: 'month', value: 'June 2026' },
    { name: 'link', value: 'https://qms.bharatsteels.in/' }
  ];
  const auth = WATI_TOKEN.startsWith('Bearer') ? WATI_TOKEN : ('Bearer ' + WATI_TOKEN);
  const url = WATI_ENDPOINT + '/api/v1/sendTemplateMessage?whatsappNumber=' + encodeURIComponent(number);
  try {
    const resp = await fetch(url, { method: 'POST', headers: { 'Authorization': auth, 'Content-Type': 'application/json' },
      body: JSON.stringify({ template_name: template, broadcast_name: 'wati_test_' + Date.now(), parameters }) });
    const raw = await resp.text();
    let body; try { body = JSON.parse(raw); } catch (e) { body = raw; }
    res.json({ env, request: { url, template, number, parameters }, response: { http_status: resp.status, ok: resp.ok, body } });
  } catch (e) {
    res.json({ env, request: { url, template, number }, fetch_error: e.message });
  }
});

app.get('/portal/complaints', requireAuth, requireCustomer, async (req, res) => {
  try {
    const r = await pgq('SELECT * FROM customer_complaints WHERE customer_id=$1 ORDER BY created_at DESC', [req.user.id]);
    res.json(r.rows.map(ccPublic));
  } catch(e){ console.error('[portal] list', e.message); res.status(500).json({error:'server_error'}); }
});

app.post('/portal/complaints', requireAuth, requireCustomer, async (req, res) => {
  try {
    const b = req.body || {};
    if (!b.description || String(b.description).trim().length < 5) return res.status(400).json({ error:'description_required' });
    let photos = Array.isArray(b.photos) ? b.photos.slice(0,10) : [];
    photos = photos.filter(p => typeof p === 'string' && p.startsWith('data:image') && p.length < 4000000);
    const yr = new Date().getFullYear();
    const c = await pgq('SELECT count(*)::int AS n FROM customer_complaints WHERE ref LIKE $1', ['CMP-'+yr+'-%']);
    const ref = 'CMP-'+yr+'-'+String(c.rows[0].n + 1).padStart(3,'0');
    const u = req.user;
    const S = v => (v==null || String(v).trim()==='') ? null : String(v).trim();
    const r = await pgq(
      `INSERT INTO customer_complaints
        (ref,customer_id,company,contact_name,email,filed_by,contact_email,mobile,
         grade,dimensions,batch_no,tc_no,invoice_no,invoice_date,quantity,
         description,photos,status)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,'submitted') RETURNING *`,
      [ref,u.id,u.company,u.contact_name,u.email,
       S(b.filed_by),S(b.email),S(b.mobile),
       S(b.grade),S(b.dimensions),S(b.batch_no),S(b.tc_no),S(b.invoice_no),S(b.invoice_date),S(b.quantity),
       String(b.description).trim(), JSON.stringify(photos)]
    );
    const row = r.rows[0];
    notifyNewComplaint(row); // email (fire-and-forget, won't block or fail the submission)
    sendWatiComplaint(row).catch(e => console.error('[wati]', e.message)); // WhatsApp
    processComplaintAssets(row.id).catch(e => console.error('[cc-assets]', e.message)); // photos+pdf -> OneDrive
    res.json({ ok:true, complaint: ccPublic(row) });
  } catch(e){ console.error('[portal] raise', e.message); res.status(500).json({error:'server_error'}); }
});

app.get('/portal/complaints/:id', requireAuth, requireCustomer, async (req, res) => {
  try {
    const r = await pgq('SELECT * FROM customer_complaints WHERE id=$1 AND customer_id=$2', [parseInt(req.params.id,10), req.user.id]);
    if (!r.rows[0]) return res.status(404).json({error:'not_found'});
    res.json(ccPublic(r.rows[0]));
  } catch(e){ console.error('[portal] get', e.message); res.status(500).json({error:'server_error'}); }
});

app.post('/portal/complaints/:id/respond', requireAuth, requireCustomer, async (req, res) => {
  try {
    const id = parseInt(req.params.id,10);
    const { action, note } = req.body || {};
    const r = await pgq('SELECT * FROM customer_complaints WHERE id=$1 AND customer_id=$2', [id, req.user.id]);
    const c = r.rows[0];
    if (!c) return res.status(404).json({error:'not_found'});
    if (c.status !== 'resolution_sent') return res.status(400).json({error:'no_resolution_pending'});
    if (action === 'accept') {
      await pgq(`UPDATE customer_complaints SET status='closed', customer_response=$1, updated_at=now() WHERE id=$2`, [note||'Accepted', id]);
    } else if (action === 'rereview') {
      await pgq(`UPDATE customer_complaints SET status='in_review', customer_response=$1, resolution_note=NULL, updated_at=now() WHERE id=$2`, [note||'Re-review requested', id]);
    } else return res.status(400).json({error:'bad_action'});
    reArchive(id);
    res.json({ ok:true });
  } catch(e){ console.error('[portal] respond', e.message); res.status(500).json({error:'server_error'}); }
});

app.get('/portal/admin/submissions', requireAuth, requireModule('salesqc'), async (req, res) => {
  try {
    const status = req.query.status;
    const r = status
      ? await pgq('SELECT * FROM customer_complaints WHERE status=$1 ORDER BY created_at DESC', [status])
      : await pgq('SELECT * FROM customer_complaints ORDER BY created_at DESC');
    res.json(r.rows.map(ccPublic));
  } catch(e){ console.error('[portal] submissions', e.message); res.status(500).json({error:'server_error'}); }
});

// One-time: push any complaints whose photos are still base64 in the DB up to OneDrive (key-gated).
app.get('/portal/admin/migrate-photos', async (req, res) => {
  if (!process.env.SETUP_KEY || req.query.key !== process.env.SETUP_KEY) return res.status(403).json({ error:'bad_key' });
  try {
    var r = await pgq('SELECT id,ref FROM customer_complaints ORDER BY id');
    var done = [];
    for (var i=0;i<r.rows.length;i++){ await processComplaintAssets(r.rows[i].id); done.push(r.rows[i].ref); }
    res.json({ ok:true, processed: done });
  } catch(e){ console.error('[cc-migrate]', e.message); res.status(500).json({ error:e.message }); }
});

// Stream a complaint photo (base64 in DB or file in OneDrive). Admin/Sales-QC or the owning customer only.
app.get('/portal/complaint-photo', requireAuth, async (req, res) => {
  try {
    var id = parseInt(req.query.id,10), i = parseInt(req.query.i,10);
    if (isNaN(id) || isNaN(i)) return res.status(400).send('bad request');
    var r = await pgq('SELECT id,customer_id,photos FROM customer_complaints WHERE id=$1',[id]);
    var row = r.rows[0];
    if (!row) return res.status(404).send('not found');
    if (req.userKind === 'customer') { if (row.customer_id !== req.user.id) return res.status(403).send('denied'); }
    else if (req.userKind === 'employee') { if (!(req.user.is_admin || (req.modules && req.modules.salesqc))) return res.status(403).send('denied'); }
    else return res.status(403).send('denied');
    var raw = Array.isArray(row.photos) ? row.photos : [];
    var p = raw[i];
    if (!p) return res.status(404).send('no photo');
    if (ccIsB64(p)) {
      var m = /^data:image\/(\w+);base64,(.+)$/.exec(p);
      if (!m) return res.status(404).send('bad');
      res.setHeader('Content-Type', 'image/'+m[1]);
      res.setHeader('Cache-Control', 'private, max-age=3600');
      return res.send(Buffer.from(m[2],'base64'));
    }
    var token = await getToken();
    var up = await fetch('https://graph.microsoft.com/v1.0/users/'+USER_ID+'/drive/root:/'+encodeURIComponent(p)+':/content', { headers:{ 'Authorization':'Bearer '+token } });
    if (!up.ok) return res.status(up.status).send('not found');
    res.setHeader('Content-Type', up.headers.get('content-type') || 'image/jpeg');
    res.setHeader('Cache-Control', 'private, max-age=3600');
    res.send(await up.buffer());
  } catch(e){ console.error('[cc-photo]', e.message); res.status(500).send('error'); }
});

app.post('/portal/admin/submissions/:id/decision', requireAuth, requireModule('salesqc'), async (req, res) => {
  try {
    const id = parseInt(req.params.id,10);
    const { action, note } = req.body || {};
    const r = await pgq('SELECT * FROM customer_complaints WHERE id=$1', [id]);
    const c = r.rows[0];
    if (!c) return res.status(404).json({error:'not_found'});
    const emp = (req.user.name||'') + ' ('+(req.user.emp_no||'')+')';
    if (action === 'accept') {
      await pgq(`UPDATE customer_complaints SET status='in_review', decision_note=$1, handler_emp=$2, updated_at=now() WHERE id=$3`, [note||null, emp, id]);
    } else if (action === 'decline') {
      if (!note) return res.status(400).json({error:'reason_required'});
      await pgq(`UPDATE customer_complaints SET status='declined', decision_note=$1, handler_emp=$2, updated_at=now() WHERE id=$3`, [note, emp, id]);
    } else if (action === 'resolve') {
      if (!note) return res.status(400).json({error:'resolution_required'});
      await pgq(`UPDATE customer_complaints SET status='resolution_sent', resolution_note=$1, handler_emp=$2, updated_at=now() WHERE id=$3`, [note, emp, id]);
    } else return res.status(400).json({error:'bad_action'});
    reArchive(id);
    res.json({ ok:true });
  } catch(e){ console.error('[portal] decision', e.message); res.status(500).json({error:'server_error'}); }
});


// ================= Customer complaint -> branded PDF (admin only) =================
function ccTimelineSteps(status){
  var idx = ({submitted:1,in_review:2,resolution_sent:3,closed:4,declined:1})[status] || 1;
  var closed = status === 'closed';
  function st(n){ return closed ? 'done' : (n < idx ? 'done' : (n === idx ? 'now' : 'todo')); }
  return [
    ['done',                         'Submitted',    'Complaint raised by the customer'],
    [closed?'done':st(1),            'Under review', 'Reviewed by the sales team'],
    [closed?'done':st(2),            'In progress',  'Quality team working on it'],
    [closed?'done':st(3),            'Resolution',   'Shared with the customer for sign-off'],
    [closed?'done':(status==='declined'?'todo':'todo'), 'Closed', 'Complaint closed out']
  ];
}
function ccIsB64(p){ return typeof p==='string' && p.indexOf('data:')===0; }
function ccExt(p){ var m=/^data:image\/(\w+)/.exec(p); var e=m?m[1]:'jpg'; return e==='jpeg'?'jpg':e; }
function ccMime(ext){ return 'image/'+(ext==='jpg'?'jpeg':ext); }
async function loadPhotoBuffers(rawPhotos){
  var out=[]; var token=null;
  var arr=Array.isArray(rawPhotos)?rawPhotos:[];
  for (var i=0;i<arr.length;i++){
    var p=arr[i];
    try{
      if (ccIsB64(p)){ var m=/^data:image\/\w+;base64,(.+)$/.exec(p); if(m) out.push(Buffer.from(m[1],'base64')); }
      else { if(!token) token=await getToken();
        var up=await fetch('https://graph.microsoft.com/v1.0/users/'+USER_ID+'/drive/root:/'+encodeURIComponent(p)+':/content',{headers:{'Authorization':'Bearer '+token}});
        if(up.ok) out.push(await up.buffer()); }
    }catch(e){ console.error('[cc-photo] load', e.message); }
  }
  return out;
}
async function archiveComplaintPDF(row){
  try{
    if(!(CLIENT_ID&&CLIENT_SECRET&&TENANT_ID)) return;
    var buffers=await loadPhotoBuffers(row.photos||[]);
    var pdf=await buildCustomerComplaintPDF(row, buffers);
    var token=await getToken();
    await uploadFile(token,'BSC Inspections/Customer Complaints/'+row.ref+'/'+row.ref+'.pdf', pdf, 'application/pdf');
  }catch(e){ console.error('[cc-pdf-archive]', e.message); }
}
async function processComplaintAssets(id){
  try{
    var r=await pgq('SELECT * FROM customer_complaints WHERE id=$1',[id]); var row=r.rows[0]; if(!row) return;
    var raw=Array.isArray(row.photos)?row.photos:[];
    if(raw.some(ccIsB64) && CLIENT_ID && CLIENT_SECRET && TENANT_ID){
      var token=await getToken();
      var folder='BSC Inspections/Customer Complaints/'+row.ref;
      var paths=[];
      for(var i=0;i<raw.length;i++){
        var p=raw[i];
        if(ccIsB64(p)){
          var m=/^data:image\/\w+;base64,(.+)$/.exec(p); if(!m) continue;
          var ext=ccExt(p); var fp=folder+'/photo-'+(i+1)+'.'+ext;
          await uploadFile(token, fp, Buffer.from(m[1],'base64'), ccMime(ext));
          paths.push(fp);
        } else paths.push(p);
      }
      await pgq('UPDATE customer_complaints SET photos=$1 WHERE id=$2',[JSON.stringify(paths), id]);
      row.photos=paths;
    }
    await archiveComplaintPDF(row);
  }catch(e){ console.error('[cc-assets]', e.message); }
}
function reArchive(id){ processComplaintAssets(id).catch(function(){}); }
function buildCustomerComplaintPDF(c, photoBuffers){
  return new Promise(function(resolve, reject){
    try{
      var doc = new PDFDocument({ size:'A4', margin:0, bufferPages:true });
      var bufs=[]; doc.on('data', function(b){bufs.push(b);}); doc.on('end', function(){resolve(Buffer.concat(bufs));}); doc.on('error', reject);
      var pageW = doc.page.width;
      var L = 40, R = pageW - 40, W = R - L;
      var STLABEL = ({submitted:'Submitted',in_review:'In review',resolution_sent:'Awaiting customer',declined:'Declined',closed:'Closed'})[c.status] || c.status;

      var y = drawBrandedHeader(doc, { title:'CUSTOMER COMPLAINT', subtitle:'Quality Complaint Report', refLabel: c.ref ? ('Ref: '+c.ref) : ('#'+c.id) });

      // Title row: company + status chip
      doc.fillColor(TEXT).font('Helvetica-Bold').fontSize(15).text(c.company || 'Customer complaint', L, y, { width: W-140 });
      doc.roundedRect(R-120, y-2, 120, 22, 11).fill(BRAND_BLUE);
      doc.fillColor('#fff').font('Helvetica-Bold').fontSize(9).text(STLABEL.toUpperCase(), R-120, y+4, { width:120, align:'center' });
      y += 22;
      doc.fillColor(MUTED).font('Helvetica').fontSize(9)
         .text('Raised: ' + (c.created_at ? new Date(c.created_at).toLocaleString('en-IN') : '-')
             + (c.filed_by ? ('     Filed by: ' + c.filed_by) : '')
             + (c.handler_emp ? ('     Handler: ' + c.handler_emp) : ''), L, y, { width: W });
      y += 20;
      doc.moveTo(L,y).lineTo(R,y).lineWidth(1).strokeColor(BORDER).stroke(); y += 12;

      // Details grid (label/value, two columns)
      var fields = [
        ['Grade', c.grade], ['Dimensions', c.dimensions], ['Batch No', c.batch_no], ['TC No', c.tc_no],
        ['Invoice No', c.invoice_no], ['Invoice Date', c.invoice_date], ['Quantity', c.quantity],
        ['Mobile', c.mobile], ['Email', c.contact_email || c.email]
      ].filter(function(p){ return p[1]; });
      doc.fillColor(BRAND_DARK).font('Helvetica-Bold').fontSize(10).text('COMPLAINT DETAILS', L, y); y += 16;
      var colW = W/2, rowH = 18, startY = y;
      fields.forEach(function(p, i){
        var col = i % 2, row = Math.floor(i/2);
        var x = L + col*colW, yy = startY + row*rowH;
        doc.fillColor(MUTED).font('Helvetica').fontSize(8.5).text(p[0].toUpperCase(), x, yy, { width: 90 });
        doc.fillColor(TEXT).font('Helvetica-Bold').fontSize(9.5).text(String(p[1]), x+92, yy-1, { width: colW-100 });
      });
      y = startY + Math.ceil(fields.length/2)*rowH + 8;

      // Description
      if (c.description){
        doc.fillColor(BRAND_DARK).font('Helvetica-Bold').fontSize(10).text('DESCRIPTION', L, y); y += 15;
        doc.fillColor(TEXT).font('Helvetica').fontSize(9.5).text(String(c.description), L, y, { width: W, lineGap: 2 });
        y = doc.y + 10;
      }

      // Resolution / decision
      if (c.resolution_note || c.decision_note){
        var isDecline = c.status === 'declined';
        var boxColor = isDecline ? '#FEF3F2' : '#ECFDF3', brdr = isDecline ? '#FECDCA' : '#A6F4C5', txtColor = isDecline ? '#B42318' : '#067647';
        var label = isDecline ? 'DECISION (DECLINED)' : (c.status==='closed' ? 'RESOLUTION (CLOSED)' : 'RESOLUTION SHARED');
        var note = c.resolution_note || c.decision_note;
        var noteH = doc.heightOfString(String(note), { width: W-24, lineGap:2 }) + 30;
        if (y + noteH > 760){ doc.addPage(); y = 40; }
        doc.roundedRect(L, y, W, noteH, 8).fill(boxColor);
        doc.fillColor(txtColor).font('Helvetica-Bold').fontSize(9).text(label, L+12, y+10);
        doc.fillColor('#344054').font('Helvetica').fontSize(9.5).text(String(note), L+12, y+24, { width: W-24, lineGap:2 });
        y += noteH + 12;
      }

      // Status timeline
      if (y + 130 > 780){ doc.addPage(); y = 40; }
      doc.fillColor(BRAND_DARK).font('Helvetica-Bold').fontSize(10).text('STATUS TIMELINE', L, y); y += 16;
      ccTimelineSteps(c.status).forEach(function(s, i, arr){
        var done = s[0]==='done', now = s[0]==='now';
        var cx = L+9, cy = y+7;
        if (i < arr.length-1){ doc.moveTo(cx, cy+9).lineTo(cx, y+28).lineWidth(2).strokeColor(done ? '#12B76A' : BORDER).stroke(); }
        if (done){ doc.circle(cx, cy, 8).fill('#12B76A'); doc.fillColor('#fff').font('Helvetica-Bold').fontSize(8).text('\u2713', cx-3, cy-4); }
        else if (now){ doc.circle(cx, cy, 8).lineWidth(2).strokeColor(BRAND_BLUE).stroke(); doc.circle(cx, cy, 3.2).fill(BRAND_BLUE); }
        else { doc.circle(cx, cy, 8).lineWidth(1.5).strokeColor(BORDER).stroke(); }
        doc.fillColor(now||done ? TEXT : '#98A2B3').font('Helvetica-Bold').fontSize(10).text(s[1], L+26, y);
        doc.fillColor(MUTED).font('Helvetica').fontSize(8.5).text(s[2], L+26, y+13);
        y += 30;
      });
      y += 6;

      // Photos (pre-loaded buffers: base64 or fetched from OneDrive)
      var photos = Array.isArray(photoBuffers) ? photoBuffers.filter(Boolean) : [];
      if (photos.length){
        if (y + 40 > 780){ doc.addPage(); y = 40; }
        doc.fillColor(BRAND_DARK).font('Helvetica-Bold').fontSize(10).text('ATTACHED PHOTOS ('+photos.length+')', L, y); y += 16;
        var pw = (W - 3*10) / 4, ph = pw; // 4 per row, square
        var col = 0;
        photos.forEach(function(buf){
          if (col === 4){ col = 0; y += ph + 10; }
          if (y + ph > 800){ doc.addPage(); y = 40; col = 0; }
          var x = L + col*(pw+10);
          try { doc.image(buf, x, y, { fit:[pw, ph], align:'center', valign:'center' }); doc.roundedRect(x, y, pw, ph, 4).lineWidth(1).strokeColor(BORDER).stroke(); } catch(e){}
          col++;
        });
        y += ph + 10;
      }

      // Footer on every page
      var range = doc.bufferedPageRange();
      for (var i=0;i<range.count;i++){
        doc.switchToPage(range.start+i);
        doc.fillColor(MUTED).font('Helvetica').fontSize(7.5)
           .text('Generated ' + new Date().toLocaleString('en-IN') + '   \u00b7   Bharat Steel (Chennai) Pvt. Ltd.', L, 812, { width: W, lineBreak:false })
           .text('Page ' + (i+1) + ' of ' + range.count, L, 812, { width: W, align:'right', lineBreak:false });
      }
      doc.end();
    } catch(e){ reject(e); }
  });
}
app.get('/portal/admin/complaints/:id/pdf', requireAuth, requireModule('salesqc'), async (req, res) => {
  try{
    var id = parseInt(req.params.id, 10);
    var r = await pgq('SELECT * FROM customer_complaints WHERE id=$1', [id]);
    var c = r.rows[0];
    if (!c) return res.status(404).json({ error:'not_found' });
    var buffers = await loadPhotoBuffers(c.photos || []);
    var pdf = await buildCustomerComplaintPDF(c, buffers);
    res.setHeader('Content-Type', 'application/pdf');
    res.setHeader('Content-Disposition', 'inline; filename="' + (c.ref || ('complaint-'+id)) + '.pdf"');
    res.send(pdf);
  } catch(e){ console.error('[portal] complaint pdf', e.message); res.status(500).json({ error:'server_error' }); }
});

const CLIENT_ID     = process.env.CLIENT_ID;
const CLIENT_SECRET = process.env.CLIENT_SECRET;
const TENANT_ID     = process.env.TENANT_ID;
const USER_ID       = process.env.USER_ID || 'pdqc@bharatsteels.in';
const SMTP_USER     = process.env.SMTP_USER || 'pdqc@bharatsteels.in';
const RESEND_API_KEY = process.env.RESEND_API_KEY || '';
const MAIL_FROM     = 'BSC QMS <qms@bharatsteels.in>';

// =====================================================
// PDF GENERATION
// =====================================================
const BLUE = '#1A6DAF';
const GRAY_BG = '#F3F4F6';
const GRAY_BORDER = '#E5E7EB';
const TEXT_DARK = '#1F2937';

// =====================================================
// BRANDED PDF HELPERS - Bharat Steel style
// =====================================================
const fs = require('fs');
const logoPath = path.join(__dirname, 'public', 'bsc-logo.png');
let LOGO_BUFFER = null;
try { LOGO_BUFFER = fs.readFileSync(logoPath); } catch(e) { console.log('Logo not loaded:', e.message); }

const BRAND_BLUE = '#1A6DAF';
const BRAND_DARK = '#0F4C75';
const BRAND_LIGHT = '#E8F2FB';
const ROW_ALT = '#F9FAFB';
const BORDER = '#D1D5DB';
const TEXT = '#1F2937';
const MUTED = '#6B7280';

// Draw the standard branded header on current page (top ~95px)
function drawBrandedHeader(doc, opts) {
  const pageW = doc.page.width;
  const opts2 = opts || {};
  const headerH = 90;
  
  // White header band
  doc.rect(0, 0, pageW, headerH).fill('#FFFFFF');
  
  // Logo on the left (white bg, no overlap)
  if (LOGO_BUFFER) {
    try { doc.image(LOGO_BUFFER, 30, 18, { fit: [200, 56] }); } catch(e) {}
  } else {
    doc.fillColor(BRAND_DARK).font('Helvetica-Bold').fontSize(16);
    doc.text('BHARAT STEEL', 30, 28);
    doc.fontSize(10).text('(CHENNAI) PVT. LTD.', 30, 52);
  }
  
  // Right side - report title (dark text on white bg)
  const boxW = 240;
  const boxX = pageW - boxW - 30;
  doc.fillColor(BRAND_DARK).font('Helvetica-Bold').fontSize(15);
  doc.text(opts2.title || 'INSPECTION REPORT', boxX, 22, { width: boxW, align: 'right' });
  
  if (opts2.subtitle) {
    doc.fillColor(MUTED).font('Helvetica').fontSize(9);
    doc.text(opts2.subtitle, boxX, 48, { width: boxW, align: 'right' });
  }
  if (opts2.refLabel) {
    doc.fillColor(BRAND_BLUE).font('Helvetica-Bold').fontSize(9);
    doc.text(opts2.refLabel, boxX, 64, { width: boxW, align: 'right' });
  }
  
  // Thick brand-blue bottom border
  doc.rect(0, headerH - 4, pageW, 4).fill(BRAND_BLUE);
  
  // Reset
  doc.fillColor(TEXT).font('Helvetica');
  return headerH + 10;
}

// Section title bar
function drawSectionTitle(doc, y, text) {
  const pageW = doc.page.width;
  doc.rect(40, y, pageW - 80, 22).fill(BRAND_BLUE);
  doc.fillColor('#fff').font('Helvetica-Bold').fontSize(10);
  doc.text(text, 50, y + 6);
  doc.fillColor(TEXT).font('Helvetica');
  return y + 28;
}

// Draw a 2-column key/value table (4 cells per row)
function drawDataTable(doc, y, rows) {
  const pageW = doc.page.width;
  const startX = 40;
  const tblW = pageW - 80;
  const cellH = 22;
  const lblW = 110;
  const valW = (tblW - lblW * 2) / 2;
  
  for (let i = 0; i < rows.length; i += 2) {
    const r1 = rows[i];
    const r2 = rows[i + 1];
    // Background alternating
    if ((i / 2) % 2 === 1) {
      doc.rect(startX, y, tblW, cellH).fill(ROW_ALT);
    }
    // Borders
    doc.lineWidth(0.5).strokeColor(BORDER);
    doc.rect(startX, y, tblW, cellH).stroke();
    if (r2) doc.moveTo(startX + lblW + valW, y).lineTo(startX + lblW + valW, y + cellH).stroke();
    doc.moveTo(startX + lblW, y).lineTo(startX + lblW, y + cellH).stroke();
    if (r2) doc.moveTo(startX + lblW + valW + lblW, y).lineTo(startX + lblW + valW + lblW, y + cellH).stroke();
    
    // Label backgrounds (light blue)
    doc.rect(startX, y, lblW, cellH).fill(BRAND_LIGHT);
    if (r2) doc.rect(startX + lblW + valW, y, lblW, cellH).fill(BRAND_LIGHT);
    // Re-stroke borders after fill
    doc.lineWidth(0.5).strokeColor(BORDER);
    doc.rect(startX, y, tblW, cellH).stroke();
    doc.moveTo(startX + lblW, y).lineTo(startX + lblW, y + cellH).stroke();
    if (r2) {
      doc.moveTo(startX + lblW + valW, y).lineTo(startX + lblW + valW, y + cellH).stroke();
      doc.moveTo(startX + lblW + valW + lblW, y).lineTo(startX + lblW + valW + lblW, y + cellH).stroke();
    }
    
    // Text
    doc.fillColor(BRAND_DARK).font('Helvetica-Bold').fontSize(8);
    doc.text(r1[0] || '', startX + 5, y + 7, { width: lblW - 10 });
    doc.fillColor(TEXT).font('Helvetica').fontSize(9);
    doc.text(String(r1[1] || '-'), startX + lblW + 5, y + 6, { width: valW - 10 });
    if (r2) {
      doc.fillColor(BRAND_DARK).font('Helvetica-Bold').fontSize(8);
      doc.text(r2[0] || '', startX + lblW + valW + 5, y + 7, { width: lblW - 10 });
      doc.fillColor(TEXT).font('Helvetica').fontSize(9);
      doc.text(String(r2[1] || '-'), startX + lblW + valW + lblW + 5, y + 6, { width: valW - 10 });
    }
    y += cellH;
  }
  return y + 8;
}

// Draw footer on every page
function drawFooter(doc, pageNum, totalPages) {
  const pageW = doc.page.width;
  const pageH = doc.page.height;
  doc.rect(0, pageH - 30, pageW, 30).fill(BRAND_LIGHT);
  doc.fillColor(MUTED).font('Helvetica').fontSize(8);
  doc.text('Bharat Steel (Chennai) Pvt. Ltd.', 40, pageH - 22);
  doc.text('Generated: ' + new Date().toLocaleString('en-IN'), 0, pageH - 22, { width: pageW - 40, align: 'right' });
  doc.fillColor(TEXT);
}


// Embed photos in an adaptive grid - keeps to 1 extra page max
function drawPhotosGrid(doc, y, photos, hdr, sizeMode) {
  const valid = (photos || []).filter(p => p && (p.data || typeof p === 'string'));
  if (valid.length === 0) return y;
  
  const tblW = doc.page.width - 80;
  y = ensureSpace(doc, y, 80, hdr);
  y = drawSectionTitle(doc, y, 'PHOTOS (' + valid.length + ')');
  
  // sizeMode: 'compact' for inspections (Inward/CTL/Shearing - small tiles, fits 1 page), 'complaint' for adaptive grid
  let cols, photoH;
  const n = valid.length;
  if (sizeMode === 'compact') {
    // Inspection PDFs - 3 cols x small height, fits ~9 in 1 page extension
    cols = 3;
    photoH = 110;
  } else {
    if (n === 1) {           cols = 1; photoH = 240; }
    else if (n === 2) {      cols = 2; photoH = 200; }
    else if (n <= 4) {       cols = 2; photoH = 160; }
    else if (n <= 6) {       cols = 3; photoH = 130; }
    else if (n <= 9) {       cols = 3; photoH = 110; }
    else {                   cols = 4; photoH =  95; }
  }
  
  const gap = 8;
  const photoW = (tblW - gap * (cols - 1)) / cols;
  let col = 0;
  let rowY = y;
  const pageBottomLimit = doc.page.height - 50;
  
  for (let i = 0; i < valid.length; i++) {
    const p = valid[i];
    if (col === 0 && i > 0) {
      if (rowY + photoH > pageBottomLimit) {
        doc.addPage();
        rowY = drawBrandedHeader(doc, hdr) + 10;
        rowY = drawSectionTitle(doc, rowY, 'PHOTOS (continued)');
      }
    }
    try {
      let imgBuf;
      if (typeof p === 'string') {
        imgBuf = Buffer.from(p.split(',')[1] || p, 'base64');
      } else if (p.data) {
        imgBuf = Buffer.from(p.data.split(',')[1] || p.data, 'base64');
      }
      if (imgBuf) {
        const x = 40 + col * (photoW + gap);
        doc.rect(x, rowY, photoW, photoH).lineWidth(0.5).strokeColor(BORDER).stroke();
        doc.image(imgBuf, x + 2, rowY + 2, { fit: [photoW - 4, photoH - 4], align: 'center', valign: 'center' });
        doc.rect(x + 2, rowY + photoH - 14, 18, 12).fill(BRAND_BLUE);
        doc.fillColor('#fff').font('Helvetica-Bold').fontSize(7);
        doc.text(String(i + 1), x + 2, rowY + photoH - 12, { width: 18, align: 'center' });
        doc.fillColor(TEXT).font('Helvetica');
      }
    } catch(e) { console.log('Image embed error:', e.message); }
    col++;
    if (col >= cols) {
      col = 0;
      rowY += photoH + gap;
    }
  }
  if (col > 0) rowY += photoH + gap;
  return rowY + 4;
}

// Ensure space on page; create new page with header if not enough
function ensureSpace(doc, y, need, headerOpts) {
  const limit = doc.page.height - 50;
  if (y + need > limit) {
    doc.addPage();
    return drawBrandedHeader(doc, headerOpts);
  }
  return y;
}


function generatePDF(folder, data, ref) {
  return new Promise((resolve, reject) => {
    try {
      const doc = new PDFDocument({ size: 'A4', margin: 0, bufferPages: true });
      const buffers = [];
      doc.on('data', buffers.push.bind(buffers));
      doc.on('end', () => resolve(Buffer.concat(buffers)));
      doc.on('error', reject);

      const titles = {
        'Inward': { title: 'COIL INWARD INSPECTION', subtitle: 'Mother Coil Inspection Report' },
        'Quality': { title: 'CTL QUALITY INSPECTION', subtitle: 'Cut-to-Length Inspection · BSCQMS-PRD-008 REV 03' },
        'Shearing': { title: 'SHEARING QUALITY INSPECTION', subtitle: 'Shearing Inspection · BSCQMS-PRD-008 REV 01' }
      };
      const hdr = titles[folder] || { title: 'INSPECTION REPORT', subtitle: '' };
      hdr.refLabel = 'Ref: ' + (ref || '-');
      
      let y = drawBrandedHeader(doc, hdr);
      y += 10;
      
      // Submission summary strip
      const submitDate = new Date(data.timestamp || Date.now()).toLocaleString('en-IN', { 
        day:'2-digit', month:'short', year:'numeric', hour:'2-digit', minute:'2-digit' 
      });
      doc.fillColor(MUTED).font('Helvetica').fontSize(9);
      doc.text('Submitted: ' + submitDate, 40, y);
      doc.fillColor(TEXT);
      y += 18;

      if (folder === 'Inward') {
        y = drawSectionTitle(doc, y, 'VEHICLE & COIL IDENTITY');
        y = drawDataTable(doc, y, [
          ['Vehicle No.', data.vehicle_number],
          ['Batch No.', data.batch_number],
          ['Make of Coil', data.make_of_coil],
          ['Grade', data.grade],
          ['Coil ID', data.coil_id || '-'],
          ['Coil Weight (T)', data.coil_weight]
        ]);
        
        y = drawSectionTitle(doc, y, 'DIMENSIONS');
        y = drawDataTable(doc, y, [
          ['Width (mm)', data.width],
          ['Thickness (mm)', data.thickness],
          ['Actual Thickness', data.actual_thickness],
          ['Actual Width', data.actual_width]
        ]);
        
        y = drawSectionTitle(doc, y, 'PHYSICAL CONDITION');
        y = drawDataTable(doc, y, [
          ['ID Sticker', data.id_sticker],
          ['Edge Damage Inner', data.edge_inner],
          ['Edge Damage Outer', data.edge_outer],
          ['Scratch Mark', data.scratch],
          ['Strapping', data.strapping],
          ['Rust on Surface', data.rust],
          ['Other Damages', data.other_damages || '-'],
          ['Wheels India', data.wheels_india ? 'Yes' : 'No']
        ]);
        
        y = drawSectionTitle(doc, y, 'INSPECTION');
        y = drawDataTable(doc, y, [
          ['Inspected By', data.inspected_by],
          ['Inspection Date', submitDate]
        ]);
        
        if (data.remarks) {
          y = ensureSpace(doc, y, 60, hdr);
          y = drawSectionTitle(doc, y, 'REMARKS');
          const tblW = doc.page.width - 80;
          doc.rect(40, y, tblW, 50).stroke(BORDER);
          doc.fillColor(TEXT).font('Helvetica').fontSize(9);
          doc.text(data.remarks, 48, y + 6, { width: tblW - 16 });
          y += 58;
        }
      } else if (folder === 'Quality') {
        y = drawSectionTitle(doc, y, 'HEADER');
        y = drawDataTable(doc, y, [
          ['Customer', data.customer_name],
          ['Date', data.date],
          ['Time', data.time],
          ['Machine', data.machine_name],
          ['Coil Number', data.coil_number],
          ['Batch Number', data.batch_number]
        ]);
        
        y = drawSectionTitle(doc, y, 'COIL DETAILS');
        y = drawDataTable(doc, y, [
          ['Make', data.make],
          ['Grade', data.coil_grade],
          ['Thickness', data.coil_thickness],
          ['Width', data.coil_width],
          ['Weight (T)', data.coil_weight],
          ['', '']
        ]);
        
        y = drawSectionTitle(doc, y, 'FINAL MEASUREMENTS');
        y = drawDataTable(doc, y, [
          ['First Bit Length', data.first_bit],
          ['Last Bit Length', data.last_bit],
          ['Defective Length', data.defective],
          ['Balance Coil Wt', data.balance_wt],
          ['Coil Verified', data.coil_verified],
          ['Blade Clearance', data.blade_clearance]
        ]);
        
        y = drawSectionTitle(doc, y, 'QUALITY CHECKLIST');
        y = drawDataTable(doc, y, [
          ['Bur', data.bur],
          ['Cutting Finish', data.cutting_finish],
          ['Scalling', data.scalling],
          ['Pit Marks', data.pit_marks],
          ['Waviness', data.waviness],
          ['Center Bow', data.center_bow],
          ['Cutting Bow', data.cutting_bow],
          ['Surface Defects', data.surface_defects]
        ]);
        
        // Sheet Measurements table (if any sheets recorded)
        const ctlSheets = (data.sheet_measurements || []).filter(r => r && (r.sheet_no || r.thickness || r.width || r.length || r.d1 || r.d2));
        if (ctlSheets.length > 0) {
          y = ensureSpace(doc, y, 60, hdr);
          y = drawSectionTitle(doc, y, 'SHEET MEASUREMENTS');
          const tblWQ = doc.page.width - 80;
          const colWQ = tblWQ / 6;
          doc.rect(40, y, tblWQ, 18).fill(BRAND_LIGHT);
          doc.lineWidth(0.5).strokeColor(BORDER);
          doc.rect(40, y, tblWQ, 18).stroke();
          doc.fillColor(BRAND_DARK).font('Helvetica-Bold').fontSize(8);
          const hdrs = ['Sheet No', 'Thickness', 'Width', 'Length', 'Diag 1', 'Diag 2'];
          hdrs.forEach((h, idx) => {
            doc.text(h, 40 + colWQ * idx + 4, y + 5, { width: colWQ - 8 });
            if (idx > 0) doc.moveTo(40 + colWQ * idx, y).lineTo(40 + colWQ * idx, y + 18).stroke();
          });
          y += 18;
          doc.font('Helvetica').fontSize(8).fillColor(TEXT);
          ctlSheets.forEach((r, i) => {
            y = ensureSpace(doc, y, 16, hdr);
            if (i % 2 === 1) doc.rect(40, y, tblWQ, 16).fill(ROW_ALT);
            doc.rect(40, y, tblWQ, 16).stroke();
            const cells = [r.sheet_no, r.thickness, r.width, r.length, r.d1, r.d2];
            cells.forEach((c, ci) => {
              doc.fillColor(TEXT).text(String(c || '-'), 40 + colWQ * ci + 4, y + 4, { width: colWQ - 8 });
              if (ci > 0) doc.moveTo(40 + colWQ * ci, y).lineTo(40 + colWQ * ci, y + 16).stroke();
            });
            y += 16;
          });
          y += 6;
        }
        
        // Processed Quantity table for CTL
        const ctlPq = data.processed_qty || {};
        const ctlPqRows = [];
        for (let i = 1; i <= 20; i++) {
          const s = ctlPq['size_' + i] || {};
          if (s.length || s.nos || s.weight_t) ctlPqRows.push({ size: 'Size ' + i, len: s.length, nos: s.nos, wt: s.weight_t });
        }
        if (ctlPqRows.length > 0) {
          y = ensureSpace(doc, y, 60, hdr);
          y = drawSectionTitle(doc, y, 'PROCESSED QUANTITY');
          const tblWP = doc.page.width - 80;
          const cwP = [70, (tblWP - 70) / 3, (tblWP - 70) / 3, (tblWP - 70) / 3];
          doc.rect(40, y, tblWP, 18).fill(BRAND_LIGHT);
          doc.lineWidth(0.5).strokeColor(BORDER);
          doc.rect(40, y, tblWP, 18).stroke();
          doc.fillColor(BRAND_DARK).font('Helvetica-Bold').fontSize(8);
          let xcP = 40;
          ['Size', 'Length (mm)', 'No. of Sheets', 'Weight (T)'].forEach((h, idx) => {
            doc.text(h, xcP + 4, y + 5, { width: cwP[idx] - 8 });
            if (idx > 0) doc.moveTo(xcP, y).lineTo(xcP, y + 18).stroke();
            xcP += cwP[idx];
          });
          y += 18;
          doc.font('Helvetica').fontSize(9).fillColor(TEXT);
          ctlPqRows.forEach((r, i) => {
            y = ensureSpace(doc, y, 16, hdr);
            if (i % 2 === 1) doc.rect(40, y, tblWP, 16).fill(ROW_ALT);
            doc.rect(40, y, tblWP, 16).stroke();
            const vals = [r.size, r.len || '-', r.nos || '-', r.wt || '-'];
            let xv = 40;
            vals.forEach((v, idx) => {
              if (idx > 0) doc.moveTo(xv, y).lineTo(xv, y + 16).stroke();
              doc.fillColor(TEXT).text(String(v), xv + 4, y + 4, { width: cwP[idx] - 8 });
              xv += cwP[idx];
            });
            y += 16;
          });
          y += 6;
        }
        
        y = ensureSpace(doc, y, 80, hdr);
        y = drawSectionTitle(doc, y, 'SIGN-OFF');
        y = drawDataTable(doc, y, [
          ['Operator', data.operator],
          ['Inspector', data.inspector]
        ]);
      } else if (folder === 'Shearing') {
        y = drawSectionTitle(doc, y, 'HEADER');
        y = drawDataTable(doc, y, [
          ['Customer', data.customer_name],
          ['Date', data.date],
          ['Batch Number', data.batch_number],
          ['Grade', data.grade],
          ['Make', data.make],
          ['Type', data.type]
        ]);
        
        y = drawSectionTitle(doc, y, 'INPUT');
        y = drawDataTable(doc, y, [
          ['Process', data.process],
          ['Input Size', data.input_size],
          ['Operator', data.operator],
          ['QC', data.qc_name]
        ]);
        
        // Output Sizes table
        const outputSizes = (data.output_sizes || []).filter(s => s && String(s).trim() !== '');
        if (outputSizes.length > 0) {
          y = ensureSpace(doc, y, 60, hdr);
          y = drawSectionTitle(doc, y, 'OUTPUT SIZES');
          const tblW = doc.page.width - 80;
          doc.rect(40, y, tblW, 18).fill(BRAND_LIGHT);
          doc.lineWidth(0.5).strokeColor(BORDER);
          doc.rect(40, y, tblW, 18).stroke();
          doc.fillColor(BRAND_DARK).font('Helvetica-Bold').fontSize(8);
          doc.text('#', 44, y + 5, { width: 30 });
          doc.text('Output Size', 80, y + 5);
          doc.moveTo(74, y).lineTo(74, y + 18).stroke();
          y += 18;
          doc.font('Helvetica').fontSize(9).fillColor(TEXT);
          outputSizes.forEach((s, i) => {
            y = ensureSpace(doc, y, 16, hdr);
            if (i % 2 === 1) doc.rect(40, y, tblW, 16).fill(ROW_ALT);
            doc.rect(40, y, tblW, 16).stroke();
            doc.moveTo(74, y).lineTo(74, y + 16).stroke();
            doc.fillColor(TEXT).text(String(i + 1), 44, y + 4, { width: 30 });
            doc.text(String(s), 80, y + 4);
            y += 16;
          });
          y += 6;
        }
        
        // Processed Quantity table
        const pq = data.processed_qty || {};
        const pqSizes = [];
        for (let i = 1; i <= 20; i++) {
          const s = pq['size_' + i] || {};
          if (s.length || s.nos || s.weight_t) pqSizes.push({ size: 'Size ' + i, len: s.length, nos: s.nos, wt: s.weight_t });
        }
        if (pqSizes.length > 0) {
          y = ensureSpace(doc, y, 60, hdr);
          y = drawSectionTitle(doc, y, 'PROCESSED QUANTITY');
          const tblW = doc.page.width - 80;
          const cw = [70, (tblW - 70) / 3, (tblW - 70) / 3, (tblW - 70) / 3];
          doc.rect(40, y, tblW, 18).fill(BRAND_LIGHT);
          doc.lineWidth(0.5).strokeColor(BORDER);
          doc.rect(40, y, tblW, 18).stroke();
          doc.fillColor(BRAND_DARK).font('Helvetica-Bold').fontSize(8);
          let xc = 40;
          ['Size', 'Dimension (mm)', 'No. of Sheets', 'Weight (T)'].forEach((h, idx) => {
            doc.text(h, xc + 4, y + 5, { width: cw[idx] - 8 });
            if (idx > 0) doc.moveTo(xc, y).lineTo(xc, y + 18).stroke();
            xc += cw[idx];
          });
          y += 18;
          doc.font('Helvetica').fontSize(9).fillColor(TEXT);
          pqSizes.forEach((r, i) => {
            y = ensureSpace(doc, y, 16, hdr);
            if (i % 2 === 1) doc.rect(40, y, tblW, 16).fill(ROW_ALT);
            doc.rect(40, y, tblW, 16).stroke();
            const vals = [r.size, r.len || '-', r.nos || '-', r.wt || '-'];
            let xv = 40;
            vals.forEach((v, idx) => {
              if (idx > 0) doc.moveTo(xv, y).lineTo(xv, y + 16).stroke();
              doc.fillColor(TEXT).text(String(v), xv + 4, y + 4, { width: cw[idx] - 8 });
              xv += cw[idx];
            });
            y += 16;
          });
          y += 6;
        }
        
        // Sheet measurements table
        if (data.measurements && data.measurements.length > 0) {
          y = ensureSpace(doc, y, 60, hdr);
          y = drawSectionTitle(doc, y, 'SHEET MEASUREMENTS');
          const tblW = doc.page.width - 80;
          const colW = tblW / 6;
          doc.rect(40, y, tblW, 18).fill(BRAND_LIGHT);
          doc.lineWidth(0.5).strokeColor(BORDER);
          doc.rect(40, y, tblW, 18).stroke();
          doc.fillColor(BRAND_DARK).font('Helvetica-Bold').fontSize(8);
          const headers = ['Sheet No', 'Width 1', 'Width 2', 'Diag 1', 'Diag 2', 'Remarks'];
          headers.forEach((h, idx) => {
            doc.text(h, 40 + colW * idx + 4, y + 5, { width: colW - 8 });
            if (idx > 0) doc.moveTo(40 + colW * idx, y).lineTo(40 + colW * idx, y + 18).stroke();
          });
          y += 18;
          doc.font('Helvetica').fontSize(8).fillColor(TEXT);
          data.measurements.forEach((row, idx) => {
            y = ensureSpace(doc, y, 16, hdr);
            if (idx % 2 === 1) doc.rect(40, y, tblW, 16).fill(ROW_ALT);
            doc.rect(40, y, tblW, 16).stroke();
            const cells = [row.sheet_no, row.width1, row.width2, row.diag1, row.diag2, row.remarks];
            cells.forEach((c, i) => {
              doc.fillColor(TEXT).text(String(c || '-'), 40 + colW * i + 4, y + 4, { width: colW - 8 });
              if (i > 0) doc.moveTo(40 + colW * i, y).lineTo(40 + colW * i, y + 16).stroke();
            });
            y += 16;
          });
          y += 6;
        }
        
        y = ensureSpace(doc, y, 130, hdr);
        y = drawSectionTitle(doc, y, 'QUALITY CHECKLIST');
        y = drawDataTable(doc, y, [
          ['Burr (<10%)', data.burr],
          ['Blade Clearance', data.blade_clearance],
          ['Cutting Finish', data.cutting_finish],
          ['Surface Condition', data.surface_condition],
          ['Bow / Bend', data.bow_bend],
          ['Taper Cutting', data.taper_cutting]
        ]);
        
        // Rejection table
        const rejList = (data.rejections || []).filter(r => r && (r.size || r.qty));
        if (data.rejection_flag === 'Yes' && rejList.length > 0) {
          y = ensureSpace(doc, y, 60, hdr);
          y = drawSectionTitle(doc, y, 'REJECTION QUANTITY');
          const tblW = doc.page.width - 80;
          const cw2 = [tblW / 2, tblW / 2];
          doc.rect(40, y, tblW, 18).fill(BRAND_LIGHT);
          doc.lineWidth(0.5).strokeColor(BORDER);
          doc.rect(40, y, tblW, 18).stroke();
          doc.fillColor(BRAND_DARK).font('Helvetica-Bold').fontSize(8);
          doc.text('Size', 44, y + 5);
          doc.text('Quantity', 40 + cw2[0] + 4, y + 5);
          doc.moveTo(40 + cw2[0], y).lineTo(40 + cw2[0], y + 18).stroke();
          y += 18;
          doc.font('Helvetica').fontSize(9).fillColor(TEXT);
          rejList.forEach((r, i) => {
            y = ensureSpace(doc, y, 16, hdr);
            if (i % 2 === 1) doc.rect(40, y, tblW, 16).fill(ROW_ALT);
            doc.rect(40, y, tblW, 16).stroke();
            doc.moveTo(40 + cw2[0], y).lineTo(40 + cw2[0], y + 16).stroke();
            doc.fillColor(TEXT).text(String(r.size || '-'), 44, y + 4);
            doc.text(String(r.qty || '-'), 40 + cw2[0] + 4, y + 4);
            y += 16;
          });
          y += 6;
        }
        
        if (data.remarks) {
          y = ensureSpace(doc, y, 60, hdr);
          y = drawSectionTitle(doc, y, 'REMARKS');
          const tblW = doc.page.width - 80;
          doc.rect(40, y, tblW, 50).stroke(BORDER);
          doc.fillColor(TEXT).font('Helvetica').fontSize(9);
          doc.text(data.remarks, 48, y + 6, { width: tblW - 16 });
          y += 58;
        }
        
        if (data.overall_observation) {
          y = ensureSpace(doc, y, 60, hdr);
          y = drawSectionTitle(doc, y, 'OVERALL OBSERVATION');
          const tblW = doc.page.width - 80;
          doc.rect(40, y, tblW, 50).stroke(BORDER);
          doc.fillColor(TEXT).font('Helvetica').fontSize(9);
          doc.text(data.overall_observation, 48, y + 6, { width: tblW - 16 });
          y += 58;
        }
      }
      
      // Inspection photos (Inward / CTL / Shearing) - small compact grid
      if (data.photos && data.photos.length > 0) {
        y = drawPhotosGrid(doc, y, data.photos, hdr, 'compact');
      }

      // Footer on all pages
      const range = doc.bufferedPageRange();
      for (let i = 0; i < range.count; i++) {
        doc.switchToPage(i);
        drawFooter(doc, i + 1, range.count);
      }
      
      doc.end();
    } catch(err) { reject(err); }
  });
}

async function generateComplaintPDF(data, photos) {
  return new Promise((resolve, reject) => {
    try {
      const doc = new PDFDocument({ size: 'A4', margin: 0, bufferPages: true });
      const buffers = [];
      doc.on('data', buffers.push.bind(buffers));
      doc.on('end', () => resolve(Buffer.concat(buffers)));
      doc.on('error', reject);
      
      const hdr = {
        title: 'QUALITY / DEFECT REPORT',
        subtitle: data.source === 'internal' ? 'Internal Finding' : 'Customer Complaint',
        refLabel: data.case_id ? 'Case: ' + data.case_id : ''
      };
      
      let y = drawBrandedHeader(doc, hdr);
      y += 10;
      
      // QC Date | Internal No strip
      const pageW = doc.page.width;
      const tblW = pageW - 80;
      doc.fillColor(MUTED).font('Helvetica').fontSize(9);
      doc.text('QC Date: ' + (data.qc_date || new Date().toLocaleDateString('en-IN')), 40, y);
      doc.text('Internal No.: ' + (data.case_id || '-'), 0, y, { width: pageW - 40, align: 'right' });
      doc.fillColor(TEXT);
      y += 20;
      
      if (data.customer_name) {
        y = drawSectionTitle(doc, y, 'CUSTOMER');
        y = drawDataTable(doc, y, [
          ['Customer Name', data.customer_name],
          ['Filed By', data.filed_by]
        ]);
      } else {
        y = drawSectionTitle(doc, y, 'FILED BY');
        y = drawDataTable(doc, y, [
          ['Filed By', data.filed_by],
          ['Source', 'Internal Finding']
        ]);
      }
      
      y = drawSectionTitle(doc, y, 'MATERIAL DETAILS');
      y = drawDataTable(doc, y, [
        ['Grade', data.grade],
        ['Dimensions', data.dimensions],
        ['Batch Number', data.batch_number],
        ['TC Number', data.tc_number],
        ['Invoice No', data.invoice_no],
        ['Invoice Date', data.invoice_date],
        ['Quantity (T)', data.quantity],
        ['Mill', data.mill]
      ]);
      
      y = drawSectionTitle(doc, y, 'REMARKS / DEFECT DESCRIPTION');
      doc.rect(40, y, tblW, 60).stroke(BORDER);
      doc.fillColor(TEXT).font('Helvetica').fontSize(10);
      doc.text(data.remarks || '-', 48, y + 8, { width: tblW - 16 });
      y += 68;
      
      // Defect photos - adaptive grid keeping report within 2 pages
      const validPhotos = (photos || []).filter(p => p);
      if (validPhotos.length > 0) {
        y = drawSectionTitle(doc, y, 'DEFECT PHOTOS (' + validPhotos.length + ')');
        
        // Adaptive grid sizing based on photo count - keeps everything compact
        let cols, photoH;
        const n = validPhotos.length;
        if (n === 1) {           cols = 1; photoH = 240; }
        else if (n === 2) {      cols = 2; photoH = 200; }
        else if (n <= 4) {       cols = 2; photoH = 160; }
        else if (n <= 6) {       cols = 3; photoH = 130; }
        else if (n <= 9) {       cols = 3; photoH = 110; }
        else {                   cols = 4; photoH =  95; } // 10 photos
        
        const gap = 8;
        const photoW = (tblW - gap * (cols - 1)) / cols;
        let col = 0;
        let rowY = y;
        const pageBottomLimit = doc.page.height - 50;
        
        for (let i = 0; i < validPhotos.length; i++) {
          const p = validPhotos[i];
          if (col === 0 && i > 0) {
            // Check if next row fits on current page; if not, new page
            if (rowY + photoH > pageBottomLimit) {
              doc.addPage();
              rowY = drawBrandedHeader(doc, hdr) + 10;
              rowY = drawSectionTitle(doc, rowY, 'DEFECT PHOTOS (continued)');
            }
          }
          try {
            let imgBuf;
            if (typeof p === 'string') {
              imgBuf = Buffer.from(p.split(',')[1] || p, 'base64');
            } else if (p.data) {
              imgBuf = Buffer.from(p.data.split(',')[1] || p.data, 'base64');
            }
            if (imgBuf) {
              const x = 40 + col * (photoW + gap);
              doc.rect(x, rowY, photoW, photoH).lineWidth(0.5).strokeColor(BORDER).stroke();
              doc.image(imgBuf, x + 2, rowY + 2, { fit: [photoW - 4, photoH - 4], align: 'center', valign: 'center' });
              // photo number badge
              doc.rect(x + 2, rowY + photoH - 14, 18, 12).fill(BRAND_BLUE);
              doc.fillColor('#fff').font('Helvetica-Bold').fontSize(7);
              doc.text(String(i + 1), x + 2, rowY + photoH - 12, { width: 18, align: 'center' });
              doc.fillColor(TEXT).font('Helvetica');
            }
          } catch(e) { console.log('Image embed error:', e.message); }
          col++;
          if (col >= cols) {
            col = 0;
            rowY += photoH + gap;
          }
        }
        if (col > 0) rowY += photoH + gap;
        y = rowY + 4;
      }
      
      // Production analysis section (if filled)
      if (data.reviewed_by || data.root_cause) {
        y = ensureSpace(doc, y, 100, hdr);
        y = drawSectionTitle(doc, y, 'PRODUCTION ANALYSIS');
        y = drawDataTable(doc, y, [
          ['Reviewed By', data.reviewed_by],
          ['Decision', data.decision]
        ]);
        if (data.root_cause) {
          doc.fillColor(BRAND_DARK).font('Helvetica-Bold').fontSize(8);
          doc.text('ROOT CAUSE', 40, y);
          y += 12;
          doc.rect(40, y, tblW, 50).stroke(BORDER);
          doc.fillColor(TEXT).font('Helvetica').fontSize(9);
          doc.text(data.root_cause, 48, y + 6, { width: tblW - 16 });
          y += 58;
        }
      }
      
      // Resolution
      if (data.resolution) {
        y = ensureSpace(doc, y, 80, hdr);
        y = drawSectionTitle(doc, y, 'RESOLUTION');
        doc.rect(40, y, tblW, 50).stroke(BORDER);
        doc.fillColor(TEXT).font('Helvetica').fontSize(9);
        doc.text(data.resolution, 48, y + 6, { width: tblW - 16 });
        y += 58;
      }
      
      // Footer on all pages
      const range = doc.bufferedPageRange();
      for (let i = 0; i < range.count; i++) {
        doc.switchToPage(i);
        drawFooter(doc, i + 1, range.count);
      }
      
      doc.end();
    } catch(err) { reject(err); }
  });
}

const transporter = null; // not used - using Resend HTTP API

async function sendInwardEmail(pdfBuffer, fileName, data) {
  if (!RESEND_API_KEY) { console.log('RESEND_API_KEY not set'); return; }
  const wheelsIndia = data.wheels_india;
  const batchNo  = data.batch_number || '-';
  const vehicleNo = data.vehicle_number || '-';
  const formDate = new Date(data.timestamp || Date.now()).toLocaleDateString('en-IN', { day:'2-digit', month:'2-digit', year:'numeric' });
  const subject = wheelsIndia
    ? 'WHEELS INDIA (Mother Coil Inspection Report) - ' + formDate + ' - ' + vehicleNo + ' - ' + batchNo
    : 'MOTHER COIL INSPECTION REPORT - ' + formDate + ' - ' + vehicleNo + ' - ' + batchNo;
  // Try to use settings from OneDrive
  let recipients;
  try {
    const tokenForSettings = await getToken();
    const settings = await loadSettings(tokenForSettings);
    if (wheelsIndia) {
      recipients = (settings.inward_emails && settings.inward_emails.wheels_india_to) || ['support@bharatsteels.in', 'kannan@bharatsteels.in'];
    } else {
      recipients = (settings.inward_emails && settings.inward_emails.default_to) || ['support@bharatsteels.in'];
    }
  } catch(e) {
    recipients = wheelsIndia
      ? ['support@bharatsteels.in', 'kannan@bharatsteels.in']
      : ['support@bharatsteels.in'];
  }
  await sendEmail({
    to: recipients,
    subject: subject,
    text: 'Please find attached the Mother Coil Inspection Report.\n\nBatch No: ' + batchNo + '\nVehicle No: ' + vehicleNo + '\nDate: ' + formDate,
    attachments: [{ filename: fileName + '.pdf', content: pdfBuffer.toString('base64') }]
  });
}

async function sendEmail({ to, cc, subject, text, html, attachments }) {
  if (!RESEND_API_KEY) throw new Error('RESEND_API_KEY not set');
  const body = {
    from: MAIL_FROM,
    to: Array.isArray(to) ? to : [to],
    subject: subject
  };
  if (cc) body.cc = Array.isArray(cc) ? cc : [cc];
  if (text) body.text = text;
  if (html) body.html = html;
  if (attachments) body.attachments = attachments;
  const resp = await fetch('https://api.resend.com/emails', {
    method: 'POST',
    headers: { 'Authorization': 'Bearer ' + RESEND_API_KEY, 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  if (!resp.ok) {
    const err = await resp.text();
    throw new Error('Resend API error (' + resp.status + '): ' + err);
  }
  const result = await resp.json();
  console.log('Email sent | ID:', result.id, '| To:', to);
  return result;
}

// =====================================================
// COMPLAINT EMAIL TEMPLATES
// =====================================================
function buildComplaintEmailBody(data, stage, baseUrl) {
  baseUrl = baseUrl || 'https://qms.bharatsteels.in';
  const caseLink = baseUrl + '/?case=' + encodeURIComponent(data.case_id || '');
  const tableRows = [
    ['Grade', data.grade],
    ['Material', data.dimensions],
    ['T.C Number', data.tc_number],
    ['Batch Number', data.batch_number],
    ['Invoice no', data.invoice_no],
    ['Invoice Date', data.invoice_date],
    ['Qty', data.quantity],
    ['Mill', data.mill],
    ['Remarks', data.remarks || data.description]
  ];

  let intro = '';
  if (stage === 'customer_to_production') {
    intro = '<p>Dear Sir,</p>'
      + '<p>I hope this message finds you well.</p>'
      + '<p>We have received a quality complaint from <b>' + escHtml(data.customer_name || 'customer') + '</b> regarding the recent supply.</p>'
      + '<p>Please find the case details below and the attached documents (TC, Invoice, Photos) for your review.</p>';
  } else if (stage === 'to_vendor') {
    intro = '<p>Dear Sir,</p>'
      + '<p>I hope this message finds you well.</p>'
      + '<p>We would like to bring to your attention a quality concern regarding the recent batch of <b>' + escHtml(data.dimensions || 'HR sheets') + '</b>, Invoice No: <b>' + escHtml(data.invoice_no || '-') + '</b>, dated <b>' + escHtml(data.invoice_date || '-') + '</b> received from your end.</p>'
      + '<p>Upon inspection we have observed <b>' + escHtml(data.remarks || data.description || 'a defect') + '</b> in several sheets. This deviation is causing issues in further processes including difficulties in forming and achieving dimensional accuracy in final components.</p>'
      + '<p>We kindly request your investigation and corrective action regarding this issue.</p>';
  } else if (stage === 'resolved_to_sales') {
    intro = '<p>Dear Team,</p>'
      + '<p>The quality complaint case <b>' + escHtml(data.case_id) + '</b> has been resolved. Resolution notes are below.</p>';
  } else {
    intro = '<p>Please find the quality case details below.</p>';
  }

  const tableHtml = '<table style="border-collapse:collapse;border:1px solid #555;margin:14px 0">'
    + tableRows.map(r => '<tr><td style="border:1px solid #555;padding:6px 12px;background:#f4f4f4;font-weight:bold;width:140px">' + escHtml(r[0]) + '</td><td style="border:1px solid #555;padding:6px 12px">' + escHtml(r[1] || '-') + '</td></tr>').join('')
    + '</table>';

  // Only include "View Case" button for internal stages, NOT for vendor-bound emails
  const includeCaseBtn = stage !== 'to_vendor';
  const caseBtn = includeCaseBtn
    ? '<div style="margin:20px 0">'
      + '<a href="' + caseLink + '" style="display:inline-block;background:#1A6DAF;color:#fff;text-decoration:none;padding:12px 22px;border-radius:6px;font-weight:600;font-size:14px">View Case in App →</a>'
      + '<div style="font-size:11px;color:#888;margin-top:6px">Case ID: ' + escHtml(data.case_id || '-') + '</div>'
      + '</div>'
    : '';

  const html = '<div style="font-family:Arial,sans-serif;font-size:14px;color:#222">'
    + intro
    + tableHtml
    + caseBtn
    + '<p>Regards,<br><b>Bharat Steel (Chennai) Pvt. Ltd.</b></p>'
    + '</div>';

  return html;
}

function escHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
}

// =====================================================
// ROUTES
// =====================================================
app.get('/', (req, res) => { res.sendFile(path.join(__dirname, 'public', 'bharat-steel-inspection.html')); });
app.get('/health', (req, res) => { res.json({ status: 'BSC Inspection Server is running' }); });

app.get('/stats', requireAuth, requireEmployee, async (req, res) => {
  try {
    const token = await getToken();
    const folders = ['Inward', 'Quality', 'Shearing'];
    const result = {};
    for (const folder of folders) {
      const filePath = 'BSC Inspections/' + folder + '/' + folder + '_Log.xlsx';
      const tableName = folder === 'Inward' ? 'InwardLog' : folder === 'Shearing' ? 'ShearingLog' : 'QualityLog';
      try {
        const fileResp = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(filePath), { headers: { 'Authorization': 'Bearer ' + token } });
        if (!fileResp.ok) { result[folder] = []; continue; }
        const fileId = (await fileResp.json()).id;
        const rowsResp = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/items/' + fileId + '/workbook/tables/' + tableName + '/rows?$select=values&$top=500', { headers: { 'Authorization': 'Bearer ' + token } });
        if (!rowsResp.ok) { result[folder] = []; continue; }
        const rows = (await rowsResp.json()).value || [];
        result[folder] = rows.map(r => {
          const v = r.values[0];
          if (folder === 'Inward') {
            return { fileName: v[0]||'', timestamp: v[1]||'', vehicle: v[2]||'', batch: v[3]||'', make: v[4]||'', grade: v[5]||'', inspector: v[19]||'', remarks: v[20]||'' };
          } else if (folder === 'Shearing') {
            return { fileName: v[0]||'', timestamp: v[1]||'', customer: v[2]||'', date: v[3]||'', batch: v[4]||'', grade: v[5]||'', make: v[6]||'', qc: v[11]||'' };
          } else {
            return { fileName: v[0]||'', timestamp: v[1]||'', customer: v[2]||'', date: v[3]||'', coil: v[5]||'', batch: v[6]||'', make: v[7]||'', grade: v[9]||'', machine: v[19]||'', inspector: v[20]||'' };
          }
        });
      } catch(e) { result[folder] = []; }
    }
    // Also load complaints
    try {
      const cFilePath = 'BSC Inspections/Complaints/Complaints_Log.xlsx';
      const cFileResp = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(cFilePath), { headers: { 'Authorization': 'Bearer ' + token } });
      if (cFileResp.ok) {
        const cFileId = (await cFileResp.json()).id;
        const cRowsResp = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/items/' + cFileId + '/workbook/tables/ComplaintsLog/rows?$select=values&$top=500', { headers: { 'Authorization': 'Bearer ' + token } });
        if (cRowsResp.ok) {
          const cRows = (await cRowsResp.json()).value || [];
          result.Complaints = cRows.map(r => {
            const v = r.values[0];
            return {
              case_id: v[0]||'', timestamp: v[1]||'', source: v[2]||'', filed_by: v[3]||'',
              customer_name: v[4]||'', batch_number: v[5]||'', grade: v[6]||'', dimensions: v[7]||'',
              tc_number: v[8]||'', invoice_no: v[9]||'', invoice_date: v[10]||'', quantity: v[11]||'',
              mill: v[12]||'', defect_type: v[13]||'', remarks: v[14]||'',
              status: v[15]||'Open',
              reviewed_by: v[16]||'', root_cause: v[17]||'', decision: v[18]||'',
              vendor_name: v[19]||'', vendor_email: v[20]||'',
              resolution: v[21]||'', resolved_date: v[22]||'',
              customer_email: v[23]||'', production_comments: v[24]||'', sales_reviewer: v[25]||'',
              customer_message: v[26]||'', customer_outcome_by: v[27]||'', customer_outcome_notes: v[28]||''
            };
          });
        } else result.Complaints = [];
      } else result.Complaints = [];
    } catch(e) { result.Complaints = []; }
    res.json(result);
  } catch(err) { res.status(500).json({ error: err.message }); }
});

// SUBMIT inspection form (existing)
app.post('/submit', requireAuth, requireEmployee, async (req, res) => {
  try {
    const data    = req.body;
    const folder  = data.form_type;
    const batchNo = (data.batch_number || 'NOBATCH').replace(/[^a-zA-Z0-9\-_]/g, '_');
    const dateStr = new Date().toLocaleDateString('en-IN', { day:'2-digit', month:'2-digit', year:'numeric' }).replace(/\//g, '-');
    const suffix  = folder === 'Inward' ? 'Inward' : folder === 'Shearing' ? 'Shearing' : 'CTL_Inspection';
    const fileName = batchNo + '_(' + dateStr + ')_' + suffix;
    if (!folder) return res.status(400).json({ status: 'error', message: 'Missing form_type' });

    const token = await getToken();
    const ref = data.ref || ('BSC-' + Math.random().toString(36).substr(2, 6).toUpperCase());
    const pdfBuffer = await generatePDF(folder, data, ref);

    let pdfFolder = 'BSC Inspections/' + folder + '/PDF';
    let photoBaseFolder = 'BSC Inspections/' + folder + '/Photos';
    if (folder === 'Quality' && data.machine_name) {
      const machineNorm = String(data.machine_name).trim().toUpperCase().replace(/\s+/g, '');
      if (machineNorm === 'CTL-1' || machineNorm === 'CTL1') {
        pdfFolder = 'BSC Inspections/' + folder + '/PDF/CTL-1';
        photoBaseFolder = 'BSC Inspections/' + folder + '/Photos/CTL-1';
      } else if (machineNorm === 'CTL-2' || machineNorm === 'CTL2') {
        pdfFolder = 'BSC Inspections/' + folder + '/PDF/CTL-2';
        photoBaseFolder = 'BSC Inspections/' + folder + '/Photos/CTL-2';
      }
    }

    const uploadTasks = [uploadFile(token, pdfFolder + '/' + fileName + '.pdf', pdfBuffer, 'application/pdf')];
    if (data.photos && data.photos.length > 0) {
      for (var i = 0; i < data.photos.length; i++) {
        var photo = data.photos[i];
        var photoName = 'photo_' + (i+1) + '_' + (photo.name || 'image.jpg').replace(/[^a-zA-Z0-9\.\-_]/g,'_');
        var photoBuffer = Buffer.from(photo.data.split(',')[1], 'base64');
        uploadTasks.push(uploadFile(token, photoBaseFolder + '/' + fileName + '/' + photoName, photoBuffer, photo.type || 'image/jpeg'));
      }
    }
    await Promise.all(uploadTasks);
    
    // Excel row append - non-blocking so submission succeeds even if Excel times out
    appendExcelRow(token, folder, data, fileName)
      .then(() => console.log('[xlsx] row appended for', fileName))
      .catch(e => console.error('[xlsx] row append FAILED for', fileName, '-', e.message));

    const pdf_path = pdfFolder + '/' + fileName + '.pdf';
    res.json({ status: 'success', ref: ref, filename: fileName, pdf_path: pdf_path });

    if (folder === 'Inward' && RESEND_API_KEY) {
      sendInwardEmail(pdfBuffer, fileName, data)
        .then(() => console.log('Inward email sent'))
        .catch(err => console.error('Email error:', err.message));
    }
  } catch (err) {
    console.error('Submit error:', err.message);
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// =====================================================
// COMPLAINTS ENDPOINTS
// =====================================================

// Get next sequential complaint number
async function getNextCaseId(token) {
  try {
    const filePath = 'BSC Inspections/Complaints/Complaints_Log.xlsx';
    const fileResp = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(filePath), { headers: { 'Authorization': 'Bearer ' + token } });
    if (!fileResp.ok) {
      // First time
      return 'BSC-QC-001-' + new Date().getFullYear();
    }
    const fileId = (await fileResp.json()).id;
    const rowsResp = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/items/' + fileId + '/workbook/tables/ComplaintsLog/rows?$select=values&$top=2000', { headers: { 'Authorization': 'Bearer ' + token } });
    if (!rowsResp.ok) return 'BSC-QC-001-' + new Date().getFullYear();
    const rows = (await rowsResp.json()).value || [];
    const thisYear = new Date().getFullYear();
    let maxN = 0;
    rows.forEach(r => {
      const cid = String(r.values[0][0] || '');
      const m = cid.match(/BSC-QC-(\d+)-(\d+)/);
      if (m && parseInt(m[2]) === thisYear) {
        const n = parseInt(m[1]);
        if (n > maxN) maxN = n;
      }
    });
    return 'BSC-QC-' + String(maxN + 1).padStart(3, '0') + '-' + thisYear;
  } catch(e) {
    return 'BSC-QC-' + Date.now().toString().slice(-6) + '-' + new Date().getFullYear();
  }
}

// Stage 1: Submit new complaint

// =====================================================
// ONE-TIME: Initialize Complaints folder + Excel template
// Hit once: GET /init-complaints?key=BSC_MIGRATE_2026
// =====================================================
app.get('/init-complaints', requireAuth, requireAdmin, async (req, res) => {
  if (req.query.key !== 'BSC_MIGRATE_2026') {
    return res.status(403).json({ error: 'Invalid key' });
  }
  try {
    const token = await getToken();
    const result = { created: [], existed: [] };
    
    // Helper to ensure folder exists
    async function ensureFolder(parent, name) {
      const checkPath = parent + '/' + name;
      const checkResp = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(checkPath), { headers: { 'Authorization': 'Bearer ' + token } });
      if (checkResp.ok) { result.existed.push(checkPath); return; }
      const parentResp = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(parent), { headers: { 'Authorization': 'Bearer ' + token } });
      const parentId = (await parentResp.json()).id;
      const createResp = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/items/' + parentId + '/children', {
        method: 'POST', headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name, folder: {}, '@microsoft.graph.conflictBehavior': 'fail' })
      });
      if (createResp.ok) result.created.push(checkPath);
      else throw new Error('Folder create failed: ' + (await createResp.text()));
    }
    
    await ensureFolder('BSC Inspections', 'Complaints');
    await ensureFolder('BSC Inspections/Complaints', 'PDF');
    await ensureFolder('BSC Inspections/Complaints', 'Attachments');
    
    // Check if Excel file exists
    const xlsxPath = 'BSC Inspections/Complaints/Complaints_Log.xlsx';
    const xlsxCheck = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(xlsxPath), { headers: { 'Authorization': 'Bearer ' + token } });
    
    if (xlsxCheck.ok) {
      result.existed.push(xlsxPath);
    } else {
      // Create empty xlsx by uploading a minimal valid xlsx file
      // Easiest: create blank xlsx using PDFKit isn't possible; use SheetJS
      // For now, return instructions to user
      result.todo = 'Excel file does not exist. Please create it manually in OneDrive at: ' + xlsxPath;
      result.excel_headers = ['Case ID','Timestamp','Source','Filed By','Customer Name','Batch Number','Grade','Dimensions','TC Number','Invoice No','Invoice Date','Quantity','Mill','Defect Type','Remarks','Status','Reviewed By','Root Cause','Decision','Vendor Name','Vendor Email','Resolution','Resolved Date','Customer Email','Production Comments','Sales Reviewer','Customer Message','Customer Outcome By','Customer Outcome Notes'];
      result.instructions = '1) Open OneDrive 2) Go to BSC Inspections/Complaints/ 3) Create new Excel file named Complaints_Log.xlsx 4) Add the 23 column headers shown in excel_headers in row 1 5) Select all data (Ctrl+A in row 1) 6) Insert > Table (check "My table has headers") 7) Click the table > Table Design tab > Table Name: "ComplaintsLog" 8) Save and close.';
    }
    
    res.json(result);
  } catch(err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/complaint/submit', requireAuth, requireEmployee, async (req, res) => {
  try {
    const data = req.body;
    if (!data.batch_number || !data.grade || !data.dimensions) {
      return res.status(400).json({ status: 'error', message: 'Missing required fields' });
    }
    const token = await getToken();
    const caseId = await getNextCaseId(token);
    const dateStr = new Date().toLocaleDateString('en-IN', { day:'2-digit', month:'2-digit', year:'numeric' }).replace(/\//g, '-');
    const fileName = caseId + '_Defect_Report';

    data.case_id = caseId;
    data.qc_date = new Date().toLocaleDateString('en-IN');
    data.timestamp = new Date().toISOString();

    // Generate PDF
    const pdfBuffer = await generateComplaintPDF(data, data.photos);

    // Upload PDF
    const pdfPath = 'BSC Inspections/Complaints/PDF/' + fileName + '.pdf';
    const uploadTasks = [uploadFile(token, pdfPath, pdfBuffer, 'application/pdf')];

    // Upload TC, Invoice, photos, other attachments
    const attachmentsFolder = 'BSC Inspections/Complaints/Attachments/' + caseId;
    if (data.tc_file && data.tc_file.data) {
      const buf = Buffer.from(data.tc_file.data.split(',')[1], 'base64');
      uploadTasks.push(uploadFile(token, attachmentsFolder + '/TC_' + (data.tc_file.name || 'TC.pdf'), buf, data.tc_file.type || 'application/pdf'));
    }
    if (data.invoice_file && data.invoice_file.data) {
      const buf = Buffer.from(data.invoice_file.data.split(',')[1], 'base64');
      uploadTasks.push(uploadFile(token, attachmentsFolder + '/Invoice_' + (data.invoice_file.name || 'Invoice.pdf'), buf, data.invoice_file.type || 'application/pdf'));
    }
    if (data.po_file && data.po_file.data) {
      const buf = Buffer.from(data.po_file.data.split(',')[1], 'base64');
      uploadTasks.push(uploadFile(token, attachmentsFolder + '/PO_' + (data.po_file.name || 'PO.pdf'), buf, data.po_file.type || 'application/pdf'));
    }
    if (data.so_file && data.so_file.data) {
      const buf = Buffer.from(data.so_file.data.split(',')[1], 'base64');
      uploadTasks.push(uploadFile(token, attachmentsFolder + '/SO_' + (data.so_file.name || 'SO.pdf'), buf, data.so_file.type || 'application/pdf'));
    }
    if (data.photos && data.photos.length > 0) {
      for (let i = 0; i < data.photos.length; i++) {
        const p = data.photos[i];
        const pname = 'photo_' + (i+1) + '_' + (p.name || 'image.jpg').replace(/[^a-zA-Z0-9\.\-_]/g, '_');
        const buf = Buffer.from(p.data.split(',')[1], 'base64');
        uploadTasks.push(uploadFile(token, attachmentsFolder + '/' + pname, buf, p.type || 'image/jpeg'));
      }
    }
    await Promise.all(uploadTasks);

    // Set initial status based on source
    let status, recipients, cc;
    const isInternal = data.source === 'internal';
    if (isInternal && data.vendor_email) {
      // Direct to vendor
      status = 'Vendor Notified';
      recipients = [data.vendor_email];
      cc = ['pdqc@bharatsteels.in', 'kannan@bharatsteels.in', 'gourav@bharatsteels.in'];
    } else if (isInternal) {
      // Internal but no vendor specified yet
      status = 'Under Review';
      recipients = ['pdqc@bharatsteels.in', 'kannan@bharatsteels.in'];
      cc = null;
    } else {
      // Customer complaint - use settings
      status = 'Open';
      let settings;
      try { settings = await loadSettings(token); } catch(e) {}
      recipients = (settings && settings.complaint_emails && settings.complaint_emails.sales_raise_to)
        || ['pdqc@bharatsteels.in', 'kannan@bharatsteels.in', 'gourav@bharatsteels.in'];
      cc = null;
    }
    data.status = status;

    // Append to Excel
    await appendComplaintRow(token, data);

    res.json({ status: 'success', case_id: caseId, filename: fileName, pdf_path: pdfPath });

    // Send email
    if (RESEND_API_KEY) {
      const attachments = [{ filename: caseId + '_Defect_Report.pdf', content: pdfBuffer.toString('base64') }];
      if (data.tc_file && data.tc_file.data) {
        attachments.push({ filename: 'TC_' + (data.tc_file.name || 'TC.pdf'), content: data.tc_file.data.split(',')[1] });
      }
      if (data.invoice_file && data.invoice_file.data) {
        attachments.push({ filename: 'Invoice_' + (data.invoice_file.name || 'Invoice.pdf'), content: data.invoice_file.data.split(',')[1] });
      }
      if (data.po_file && data.po_file.data) {
        attachments.push({ filename: 'PO_' + (data.po_file.name || 'PO.pdf'), content: data.po_file.data.split(',')[1] });
      }
      if (data.so_file && data.so_file.data) {
        attachments.push({ filename: 'SO_' + (data.so_file.name || 'SO.pdf'), content: data.so_file.data.split(',')[1] });
      }

      const subject = isInternal && data.vendor_email
        ? 'Quality Complaint - Action Required - ' + caseId
        : isInternal
          ? 'Internal Quality Issue - ' + caseId + ' - ' + (data.batch_number || '')
          : 'New Customer Quality Complaint - ' + caseId + ' - ' + (data.customer_name || '');

      const html = buildComplaintEmailBody(data, isInternal && data.vendor_email ? 'to_vendor' : 'customer_to_production');

      sendEmail({ to: recipients, cc: cc, subject: subject, html: html, attachments: attachments })
        .then(() => console.log('Complaint email sent for', caseId))
        .catch(err => console.error('Complaint email error:', err.message));
    }
  } catch (err) {
    console.error('Complaint submit error:', err.message);
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// Stage 2/3: Update complaint with production analysis or vendor escalation or resolution
app.post('/complaint/update', requireAuth, requireEmployee, async (req, res) => {
  try {
    const data = req.body;
    if (!data.case_id) return res.status(400).json({ status: 'error', message: 'Missing case_id' });
    const token = await getToken();

    // Find the row in Excel
    const filePath = 'BSC Inspections/Complaints/Complaints_Log.xlsx';
    const fileResp = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(filePath), { headers: { 'Authorization': 'Bearer ' + token } });
    if (!fileResp.ok) throw new Error('Complaints log not found');
    const fileId = (await fileResp.json()).id;

    const rowsResp = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/items/' + fileId + '/workbook/tables/ComplaintsLog/rows?$select=values&$top=2000', { headers: { 'Authorization': 'Bearer ' + token } });
    const rows = (await rowsResp.json()).value || [];
    let rowIndex = -1;
    let existing = null;
    rows.forEach((r, idx) => {
      if (r.values[0][0] === data.case_id) { rowIndex = idx; existing = r.values[0]; }
    });
    if (rowIndex === -1) throw new Error('Case not found: ' + data.case_id);

    // Merge new fields into existing row
    const updated = [...existing];
    // Make sure we have 29 columns
    while (updated.length < 29) updated.push('');
    if (data.status)        updated[15] = data.status;
    if (data.reviewed_by)   updated[16] = data.reviewed_by;
    if (data.root_cause)    updated[17] = data.root_cause;
    if (data.decision)      updated[18] = data.decision;
    if (data.vendor_name)   updated[19] = data.vendor_name;
    if (data.vendor_email)  updated[20] = data.vendor_email;
    if (data.resolution)    updated[21] = data.resolution;
    if (data.resolved_date) updated[22] = data.resolved_date;
    if (data.customer_email)        updated[23] = data.customer_email;
    if (data.production_comments)   updated[24] = data.production_comments;
    if (data.sales_reviewer)        updated[25] = data.sales_reviewer;
    if (data.customer_message)      updated[26] = data.customer_message;
    if (data.customer_outcome_by)   updated[27] = data.customer_outcome_by;
    if (data.customer_outcome_notes) updated[28] = data.customer_outcome_notes;

    // Update via Graph API patch on the table row
    const patchUrl = 'https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/items/' + fileId + '/workbook/tables/ComplaintsLog/rows/itemAt(index=' + rowIndex + ')';
    const patchResp = await fetch(patchUrl, {
      method: 'PATCH',
      headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' },
      body: JSON.stringify({ values: [updated] })
    });
    if (!patchResp.ok) throw new Error('Excel update failed: ' + await patchResp.text());

    // Upload 8D report if provided
    if (data.file_8d && data.file_8d.data) { data.eightd_file = data.file_8d; }
    if (data.eightd_file && data.eightd_file.data) {
      const buf = Buffer.from(data.eightd_file.data.split(',')[1], 'base64');
      const fname = '8D_Report_' + (data.eightd_file.name || 'report.pdf');
      try {
        await uploadFile(token, 'BSC Inspections/Complaints/Attachments/' + data.case_id + '/' + fname, buf, data.eightd_file.type || 'application/pdf');
        console.log('[upload] 8D Report uploaded:', fname);
      } catch(e) { console.error('[upload] 8D failed:', e.message); }
    }
    // Upload RM Invoice if provided (for vendor escalation)
    if (data.rm_invoice_file && data.rm_invoice_file.data) {
      const buf = Buffer.from(data.rm_invoice_file.data.split(',')[1], 'base64');
      const fname = 'Invoice_RM_' + (data.rm_invoice_file.name || 'invoice.pdf');
      try {
        await uploadFile(token, 'BSC Inspections/Complaints/Attachments/' + data.case_id + '/' + fname, buf, data.rm_invoice_file.type || 'application/pdf');
        console.log('[upload] RM Invoice uploaded:', fname);
      } catch(e) { console.error('[upload] RM Invoice failed:', e.message); }
    }

    res.json({ status: 'success' });

    // Send appropriate email
    if (RESEND_API_KEY) {
      const dataForEmail = {
        case_id: data.case_id,
        customer_name: existing[4],
        batch_number: existing[5],
        grade: existing[6],
        dimensions: existing[7],
        tc_number: existing[8],
        invoice_no: existing[9],
        invoice_date: existing[10],
        quantity: existing[11],
        mill: existing[12],
        remarks: existing[14],
        resolution: data.resolution || ''
      };

      // Re-fetch PDF + PO + SO + TC + Invoice + 8D for re-attaching to email
      let attachments = [];
      try {
        const attachFolder = 'BSC Inspections/Complaints/Attachments/' + data.case_id;
        const pdfPath = 'BSC Inspections/Complaints/PDF/' + data.case_id + '_Defect_Report.pdf';
        console.log('[attach] fetching PDF:', pdfPath);
        const pdfFetch = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(pdfPath) + ':/content', { headers: { 'Authorization': 'Bearer ' + token } });
        if (pdfFetch.ok) {
          const buf = await pdfFetch.buffer();
          attachments.push({ filename: data.case_id + '_Defect_Report.pdf', content: buf.toString('base64') });
          console.log('[attach] PDF added,', buf.length, 'bytes');
        } else {
          console.log('[attach] PDF NOT FOUND:', pdfFetch.status);
        }
        // List attachments folder
        console.log('[attach] listing folder:', attachFolder);
        const listResp = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(attachFolder) + ':/children?$top=20', { headers: { 'Authorization': 'Bearer ' + token } });
        if (listResp.ok) {
          const items = (await listResp.json()).value || [];
          console.log('[attach] folder has', items.length, 'items');
          for (const item of items.slice(0, 8)) {
            try {
              console.log('[attach] downloading', item.name, '(' + item.size + ' bytes)');
              const dl = await fetch(item['@microsoft.graph.downloadUrl']);
              if (dl.ok) {
                const buf = await dl.buffer();
                attachments.push({ filename: item.name, content: buf.toString('base64') });
                console.log('[attach] added', item.name);
              } else {
                console.log('[attach] download failed:', item.name, dl.status);
              }
            } catch(e) {
              console.log('[attach] item error:', item.name, e.message);
            }
          }
        } else {
          console.log('[attach] folder list FAILED:', listResp.status, await listResp.text());
        }
        console.log('[attach] TOTAL attachments to send:', attachments.length);
      } catch(e) { console.error('[attach] fatal error:', e.message); }

      const isEscalateToVendor = data.decision === 'Escalate to Vendor' && data.vendor_email;
      const isInternalClose = data.decision === 'Close Internally' || data.status === 'Completed (Internal)';
      const isVendorResolved = data.status === 'Completed (Vendor)';
      // Sales QC flow
      const isSalesProdAnalysis = data.stage_action === 'production_analysis_to_sales' || (data.decision === 'Sent to Sales' && data.status === 'Pending Sales Review');
      const isSendToCustomer = data.stage_action === 'send_to_customer';
      const isCustomerAccepted = data.stage_action === 'customer_accepted';
      const isCustomerEscalated = data.stage_action === 'customer_escalated';
      
      // Add resolved info to email payload
      dataForEmail.vendor_message = data.vendor_message || '';
      dataForEmail.resolved_by = data.resolved_by || '';
      // Sales QC extra fields for email body
      dataForEmail.production_comments = data.production_comments || existing[24] || '';
      dataForEmail.customer_email = data.customer_email || existing[23] || '';
      dataForEmail.customer_outcome_notes = data.customer_outcome_notes || '';
      
      // ----- SALES QC EMAIL BRANCHES -----
      if (isSalesProdAnalysis) {
        // Stage 2: Production submits analysis -> sales
        const subject = 'Production Analysis Ready for Review - ' + data.case_id + ' - ' + (existing[4] || '');
        const html = '<div style="font-family:Arial,sans-serif;font-size:14px;color:#222">'
          + '<p>Dear Sales Team,</p>'
          + '<p>Production team has completed analysis for case <b>' + escHtml(data.case_id) + '</b>.</p>'
          + '<p>Please review the analysis and prepare the response to be sent to the customer.</p>'
          + '<p><b>Customer:</b> ' + escHtml(existing[4] || '-') + '</p>'
          + '<p><b>Batch:</b> ' + escHtml(existing[5] || '-') + ' | <b>Invoice:</b> ' + escHtml(existing[9] || '-') + '</p>'
          + '<p><b>Root Cause:</b><br>' + escHtml(data.root_cause || '-') + '</p>'
          + '<p><b>Production Comments / Response Draft:</b><br>' + escHtml(data.production_comments || '-').replace(/\n/g,'<br>') + '</p>'
          + '<div style="margin:20px 0">'
          + '<a href="https://qms.bharatsteels.in/?case=' + encodeURIComponent(data.case_id) + '" style="display:inline-block;background:#1A6DAF;color:#fff;text-decoration:none;padding:12px 22px;border-radius:6px;font-weight:600;font-size:14px">Review & Send to Customer →</a>'
          + '</div>'
          + '<p>Regards,<br><b>Bharat Steel (Chennai) Pvt. Ltd.</b></p>'
          + '</div>';
        sendEmail({
          to: ['info@bharatsteels.in'],
          cc: ['kannan@bharatsteels.in', 'pdqc@bharatsteels.in'],
          subject: subject, html: html, attachments: attachments
        }).then(() => console.log('Sales-prod-analysis email sent for', data.case_id)).catch(err => console.error('Sales-prod-analysis email error:', err.message));
      } else if (isSendToCustomer) {
        // Stage 3: Sales sends response to customer
        const custEmail = data.customer_email;
        if (!custEmail) {
          console.log('No customer email available for', data.case_id);
        } else {
          const subject = 'Quality Complaint Response - ' + (existing[4] || '') + ' - ' + (existing[9] || data.case_id);
          // Filter attachments: include PDF + 8D only, NOT TC/Invoice/PO/SO
          const customerAttachments = attachments.filter(a => {
            const n = (a.filename || '').toLowerCase();
            return n.indexOf('defect_report') >= 0 || n.indexOf('8d') >= 0;
          });
          const html = '<div style="font-family:Arial,sans-serif;font-size:14px;color:#222;white-space:pre-wrap">'
            + escHtml(data.customer_message || '').replace(/\n/g, '<br>')
            + '</div>';
          sendEmail({
            to: [custEmail],
            cc: ['info@bharatsteels.in'],
            subject: subject, html: html, attachments: customerAttachments
          }).then(() => console.log('Customer email sent for', data.case_id, 'to', custEmail)).catch(err => console.error('Customer email error:', err.message));
        }
      } else if (isCustomerAccepted) {
        // Stage 4a: Customer accepted -> closed
        const subject = 'Sales Quality Complaint Closed - ' + (existing[4] || '') + ' - ' + (existing[9] || '') + ' - ' + data.case_id;
        const html = '<div style="font-family:Arial,sans-serif;font-size:14px;color:#222">'
          + '<p>Dear Team,</p>'
          + '<p>The sales quality complaint <b>' + escHtml(data.case_id) + '</b> has been closed.</p>'
          + '<p><b>Customer:</b> ' + escHtml(existing[4] || '-') + '</p>'
          + '<p><b>Invoice No:</b> ' + escHtml(existing[9] || '-') + '</p>'
          + '<p><b>Customer Response:</b><br>' + escHtml(data.customer_outcome_notes || '-').replace(/\n/g,'<br>') + '</p>'
          + '<p><b>Closed By:</b> ' + escHtml(data.customer_outcome_by || '-') + '</p>'
          + '<p>Regards,<br><b>Bharat Steel (Chennai) Pvt. Ltd.</b></p>'
          + '</div>';
        sendEmail({
          to: ['info@bharatsteels.in'],
          cc: ['pdqc@bharatsteels.in', 'kannan@bharatsteels.in'],
          subject: subject, html: html
        }).then(() => console.log('Customer-accepted email sent for', data.case_id)).catch(err => console.error('Customer-accepted email error:', err.message));
      } else if (isCustomerEscalated) {
        // Stage 4b: Customer wants re-review -> back to production
        const subject = 'Complaint Escalated for Re-review - ' + (existing[4] || '') + ' - ' + data.case_id;
        const html = '<div style="font-family:Arial,sans-serif;font-size:14px;color:#222">'
          + '<p>Dear PDQC Team,</p>'
          + '<p>Customer has requested further review for case <b>' + escHtml(data.case_id) + '</b>.</p>'
          + '<p><b>Customer:</b> ' + escHtml(existing[4] || '-') + '</p>'
          + '<p><b>Batch:</b> ' + escHtml(existing[5] || '-') + ' | <b>Invoice:</b> ' + escHtml(existing[9] || '-') + '</p>'
          + '<p><b>Customer Response:</b><br>' + escHtml(data.customer_outcome_notes || '-').replace(/\n/g,'<br>') + '</p>'
          + '<p><b>Marked By (Sales):</b> ' + escHtml(data.customer_outcome_by || '-') + '</p>'
          + '<div style="margin:20px 0">'
          + '<a href="https://qms.bharatsteels.in/?case=' + encodeURIComponent(data.case_id) + '" style="display:inline-block;background:#1A6DAF;color:#fff;text-decoration:none;padding:12px 22px;border-radius:6px;font-weight:600;font-size:14px">Review Case →</a>'
          + '</div>'
          + '<p>Regards,<br><b>Bharat Steel (Chennai) Pvt. Ltd.</b></p>'
          + '</div>';
        sendEmail({
          to: ['pdqc@bharatsteels.in'],
          cc: ['info@bharatsteels.in', 'kannan@bharatsteels.in'],
          subject: subject, html: html
        }).then(() => console.log('Customer-escalated email sent for', data.case_id)).catch(err => console.error('Customer-escalated email error:', err.message));
      } else if (isEscalateToVendor) {
        // Stage: Escalate to vendor
        const subject = 'Quality Complaint - Action Required - ' + data.case_id;
        let html = buildComplaintEmailBody(dataForEmail, 'to_vendor');
        // Add vendor message if provided
        if (data.vendor_message) {
          html = html.replace('</p><table', '</p><p><b>Additional notes:</b> ' + escHtml(data.vendor_message) + '</p><table');
        }
        let st2b_cc;
        try {
          const s = await loadSettings(token);
          st2b_cc = (s.complaint_emails && s.complaint_emails.vendor_escalation_cc) || ['pdqc@bharatsteels.in', 'kannan@bharatsteels.in', 'gourav@bharatsteels.in'];
        } catch(e) { st2b_cc = ['pdqc@bharatsteels.in', 'kannan@bharatsteels.in', 'gourav@bharatsteels.in']; }
        sendEmail({
          to: [data.vendor_email],
          cc: st2b_cc,
          subject: subject, html: html, attachments: attachments
        }).then(() => console.log('Vendor email sent for', data.case_id, 'with', attachments.length, 'attachments')).catch(err => console.error('Vendor email error:', err.message));
      } else if (isInternalClose) {
        // Stage: Closed internally - notify sales + CC gourav
        const subject = 'Quality Complaint Resolved (Internal) - ' + data.case_id;
        let html = buildComplaintEmailBody(dataForEmail, 'resolved_to_sales');
        // Add root cause and resolution
        const extra = '<p><b>Root Cause:</b> ' + escHtml(data.root_cause || '-') + '</p>'
          + '<p><b>Resolution:</b> ' + escHtml(data.resolution || '-') + '</p>'
          + '<p><b>Reviewed By:</b> ' + escHtml(data.reviewed_by || '-') + '</p>';
        html = html.replace('<table', extra + '<table');
        let st2a_to;
        try {
          const s = await loadSettings(token);
          st2a_to = (s.complaint_emails && s.complaint_emails.internal_close_to) || ['info@bharatsteels.in', 'gourav@bharatsteels.in', 'kannan@bharatsteels.in'];
        } catch(e) { st2a_to = ['info@bharatsteels.in', 'gourav@bharatsteels.in', 'kannan@bharatsteels.in']; }
        sendEmail({
          to: st2a_to,
          cc: null,
          subject: subject, html: html
        }).then(() => console.log('Internal-close email sent for', data.case_id)).catch(err => console.error('Internal-close email error:', err.message));
      } else if (isVendorResolved) {
        // Stage: Vendor case resolved
        const subject = 'Quality Complaint Closed - ' + data.case_id;
        const html = '<div style="font-family:Arial,sans-serif;font-size:14px;color:#222">'
          + '<p>Dear Team,</p>'
          + '<p>The quality complaint case <b>' + escHtml(data.case_id) + '</b> has now been closed.</p>'
          + '<p><b>Resolution:</b> ' + escHtml(data.resolution || '-') + '</p>'
          + (data.resolved_by ? '<p><b>Resolved By:</b> ' + escHtml(data.resolved_by) + '</p>' : '')
          + '<p>Regards,<br><b>Bharat Steel (Chennai) Pvt. Ltd.</b></p>'
          + '</div>';
        let st3_to;
        try {
          const s = await loadSettings(token);
          st3_to = (s.complaint_emails && s.complaint_emails.resolved_to) || ['info@bharatsteels.in', 'pdqc@bharatsteels.in', 'kannan@bharatsteels.in', 'gourav@bharatsteels.in'];
        } catch(e) { st3_to = ['info@bharatsteels.in', 'pdqc@bharatsteels.in', 'kannan@bharatsteels.in', 'gourav@bharatsteels.in']; }
        sendEmail({
          to: st3_to,
          cc: null,
          subject: subject, html: html
        }).then(() => console.log('Resolution email sent for', data.case_id)).catch(err => console.error('Resolution email error:', err.message));
      } else {
        console.log('No email triggered for status:', data.status, 'decision:', data.decision);
      }
    }
  } catch (err) {
    console.error('Complaint update error:', err.message);
    res.status(500).json({ status: 'error', message: err.message });
  }
});

async function appendComplaintRow(token, data) {
  const filePath  = 'BSC Inspections/Complaints/Complaints_Log.xlsx';
  const fileResp  = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(filePath), { headers:{ 'Authorization':'Bearer ' + token } });
  if (!fileResp.ok) throw new Error('Complaints log not found at: ' + filePath);
  const fileId = (await fileResp.json()).id;
  const values = [[
    data.case_id||'', data.timestamp||'', data.source||'', data.filed_by||'',
    data.customer_name||'', data.batch_number||'', data.grade||'', data.dimensions||'',
    data.tc_number||'', data.invoice_no||'', data.invoice_date||'', data.quantity||'',
    data.mill||'', (Array.isArray(data.defect_type) ? data.defect_type.join(', ') : (data.defect_type||'')),
    data.remarks||'', data.status||'Open',
    data.reviewed_by||'', data.root_cause||'', data.decision||'',
    data.vendor_name||'', data.vendor_email||'',
    data.resolution||'', data.resolved_date||'',
    data.customer_email||'',          // col 23
    data.production_comments||'',     // col 24
    data.sales_reviewer||'',          // col 25
    data.customer_message||'',        // col 26
    data.customer_outcome_by||'',     // col 27
    data.customer_outcome_notes||''   // col 28
  ]];
  const rowResp = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/items/' + fileId + '/workbook/tables/ComplaintsLog/rows/add', {
    method:'POST', headers:{ 'Authorization':'Bearer ' + token, 'Content-Type':'application/json' }, body:JSON.stringify({ values })
  });
  if (!rowResp.ok) throw new Error('Complaint row failed: ' + JSON.stringify(await rowResp.json()));
}

// =====================================================
// HELPERS
// =====================================================
async function getToken() {
  const body = new URLSearchParams({ grant_type:'client_credentials', client_id:CLIENT_ID, client_secret:CLIENT_SECRET, scope:'https://graph.microsoft.com/.default' });
  const resp = await fetch('https://login.microsoftonline.com/' + TENANT_ID + '/oauth2/v2.0/token', { method:'POST', body });
  const json = await resp.json();
  if (!json.access_token) throw new Error('Token error: ' + JSON.stringify(json));
  return json.access_token;
}
async function uploadFile(token, filePath, content, contentType) {
  const resp = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(filePath) + ':/content', {
    method:'PUT', headers:{ 'Authorization':'Bearer ' + token, 'Content-Type':contentType }, body:content
  });
  if (!resp.ok) throw new Error('Upload failed (' + resp.status + '): ' + await resp.text());
}
async function appendExcelRow(token, folder, data, fileName) {
  const filePath  = 'BSC Inspections/' + folder + '/' + folder + '_Log.xlsx';
  const tableName = folder === 'Inward' ? 'InwardLog' : folder === 'Shearing' ? 'ShearingLog' : 'QualityLog';
  const fileResp  = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(filePath), { headers:{ 'Authorization':'Bearer ' + token } });
  if (!fileResp.ok) throw new Error('Excel file not found: ' + filePath);
  const fileId = (await fileResp.json()).id;
  const pq = data.processed_qty || {};
  const s  = n => (pq['size_' + n] || {});
  const sh = data.measurements || [];
  const sr = n => (sh[n] || {});
  const values = folder === 'Inward' ? [[
    fileName, data.timestamp||'', data.vehicle_number||'', data.batch_number||'', data.make_of_coil||'', data.grade||'',
    data.width||'', data.thickness||'', data.coil_weight||'', data.coil_id||'', data.actual_thickness||'', data.actual_width||'',
    data.id_sticker||'', data.edge_inner||'', data.edge_outer||'', data.scratch||'', data.strapping||'', data.rust||'',
    data.other_damages||'', data.inspected_by||'', data.remarks||''
  ]] : folder === 'Shearing' ? (() => {
    const os = data.output_sizes || [];
    const spq = data.processed_qty || {};
    const spqGet = n => (spq['size_' + n] || {});
    const rj = data.rejections || [];
    const rjGet = n => (rj[n] || {});
    return [[
      fileName, data.timestamp||'', data.customer_name||'', data.date||'',
      data.batch_number||'', data.grade||'', data.make||'', data.type||'',
      data.process||'', data.operator||'', data.input_size||'', data.qc_name||'',
      data.burr||'', data.blade_clearance||'', data.cutting_finish||'', data.surface_condition||'', data.bow_bend||'',
      data.taper_cutting||'',
      data.rejection_flag||'No',
      data.remarks||'',
      data.overall_observation||'',
      ...[...Array(30)].flatMap((_,i) => [sr(i).sheet_no||'', sr(i).width1||'', sr(i).width2||'', sr(i).diag1||'', sr(i).diag2||'', sr(i).remarks||'']),
      ...[...Array(20)].map((_,i) => os[i] || ''),
      ...[...Array(20)].flatMap((_,i) => [spqGet(i+1).length||'', spqGet(i+1).nos||'', spqGet(i+1).weight_t||'']),
      ...[...Array(10)].flatMap((_,i) => [rjGet(i).size||'', rjGet(i).qty||''])
    ]];
  })() : [[
    fileName, data.timestamp||'', data.customer_name||'', data.date||'', data.time||'',
    data.coil_number||'', data.batch_number||'', data.make||'', data.coil_thickness||'',
    data.coil_grade||'', data.coil_width||'', data.coil_weight||'',
    data.first_bit||'', data.last_bit||'', data.defective||'', data.balance_wt||'',
    data.coil_verified||'', data.blade_clearance||'',
    data.operator||'', data.machine_name||'', data.inspector||'', data.remarks||'',
    data.bur||'', data.cutting_finish||'', data.scalling||'', data.pit_marks||'',
    data.waviness||'', data.center_bow||'', data.cutting_bow||'', data.surface_defects||'',
    ...[...Array(20)].flatMap((_,i) => [s(i+1).length||'', s(i+1).nos||'', s(i+1).weight_t||''])
  ]];
  // Use workbook session for faster writes on large tables. Retry on timeout.
  const addUrl = 'https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/items/' + fileId + '/workbook/tables/' + tableName + '/rows/add';
  let lastErr = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      // Open a non-persistent session for this write
      let sessionId = null;
      try {
        const sessResp = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/items/' + fileId + '/workbook/createSession', {
          method:'POST',
          headers:{ 'Authorization':'Bearer ' + token, 'Content-Type':'application/json' },
          body: JSON.stringify({ persistChanges: true })
        });
        if (sessResp.ok) {
          const sData = await sessResp.json();
          sessionId = sData.id;
        }
      } catch(e) { console.log('[xlsx] session create failed:', e.message); }
      
      const headers = { 'Authorization':'Bearer ' + token, 'Content-Type':'application/json' };
      if (sessionId) headers['workbook-session-id'] = sessionId;
      
      const rowResp = await fetch(addUrl, { method:'POST', headers, body:JSON.stringify({ values }) });
      
      // Close session
      if (sessionId) {
        fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/items/' + fileId + '/workbook/closeSession', {
          method:'POST', headers: { 'Authorization':'Bearer ' + token, 'workbook-session-id': sessionId }
        }).catch(()=>{});
      }
      
      if (rowResp.ok) { lastErr = null; break; }
      const errBody = await rowResp.json();
      lastErr = new Error('Excel row failed: ' + JSON.stringify(errBody));
      // Only retry on timeout/transient errors
      const code = errBody && errBody.error && errBody.error.code;
      if (code !== 'MaxRequestDurationExceeded' && code !== 'gatewayTimeoutUncategorized' && code !== 'InternalServerError') break;
      console.log('[xlsx] attempt', attempt+1, 'failed with', code, '- retrying');
      await new Promise(r => setTimeout(r, 1000 * (attempt+1)));
    } catch(e) {
      lastErr = e;
      console.log('[xlsx] attempt', attempt+1, 'exception:', e.message);
      await new Promise(r => setTimeout(r, 1000));
    }
  }
  if (lastErr) throw lastErr;
}



// =====================================================
// COMPLAINT FILES - list & stream for in-app PDF viewer
// =====================================================

// GET /complaint/files?caseId=BSC-QC-021-2026 → list of files for that case
app.get('/complaint/files', requireAuth, requireEmployee, async (req, res) => {
  const caseId = req.query.caseId;
  if (!caseId) return res.status(400).json({ error: 'caseId required' });
  try {
    const token = await getToken();
    const files = [];
    
    // 1. The Defect Report PDF
    const pdfPath = 'BSC Inspections/Complaints/PDF/' + caseId + '_Defect_Report.pdf';
    try {
      const pdfMeta = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(pdfPath), { headers: { 'Authorization': 'Bearer ' + token } });
      if (pdfMeta.ok) {
        const m = await pdfMeta.json();
        files.push({
          name: caseId + '_Defect_Report.pdf',
          path: pdfPath,
          size: m.size || 0,
          type: 'application/pdf',
          category: 'report'
        });
      }
    } catch(e) {}
    
    // 2. All files in the Attachments folder
    const attachFolder = 'BSC Inspections/Complaints/Attachments/' + caseId;
    try {
      const listResp = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(attachFolder) + ':/children?$top=50', { headers: { 'Authorization': 'Bearer ' + token } });
      if (listResp.ok) {
        const items = (await listResp.json()).value || [];
        items.forEach(it => {
          if (it.folder) return; // skip subfolders
          const name = it.name || '';
          let category = 'other';
          if (/^PO_/i.test(name)) category = 'po';
          else if (/^SO_/i.test(name)) category = 'so';
          else if (/^TC_/i.test(name)) category = 'tc';
          else if (/^Invoice_RM_/i.test(name)) category = 'invoice_rm';
          else if (/^Invoice_/i.test(name)) category = 'invoice';
          else if (/^8D_Report/i.test(name)) category = '8d';
          else if (/\.(jpe?g|png|gif|webp)$/i.test(name)) category = 'photo';
          files.push({
            name: name,
            path: attachFolder + '/' + name,
            size: it.size || 0,
            type: (it.file && it.file.mimeType) || 'application/octet-stream',
            category: category
          });
        });
      }
    } catch(e) {}
    
    res.json({ caseId: caseId, files: files });
  } catch(err) {
    res.status(500).json({ error: err.message });
  }
});

// GET /complaint/file?path=<path>&download=1 → stream file from OneDrive
// Generic file streaming endpoint - broader path allowed (security: must be under BSC Inspections/)
app.get('/file', requireAuth, requireEmployee, async (req, res) => {
  const filePath = req.query.path;
  const download = req.query.download === '1';
  if (!filePath) return res.status(400).send('path required');
  // Security: only allow paths under BSC Inspections/
  if (!filePath.startsWith('BSC Inspections/')) {
    return res.status(403).send('Access denied');
  }
  // Block path traversal
  if (filePath.indexOf('..') >= 0) {
    return res.status(403).send('Invalid path');
  }
  try {
    const token = await getToken();
    const upstream = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(filePath) + ':/content', { headers: { 'Authorization': 'Bearer ' + token } });
    if (!upstream.ok) {
      return res.status(upstream.status).send('File not found');
    }
    const ct = upstream.headers.get('content-type') || 'application/octet-stream';
    res.setHeader('Content-Type', ct);
    const fname = filePath.split('/').pop();
    res.setHeader('Content-Disposition', (download ? 'attachment' : 'inline') + '; filename="' + fname + '"');
    res.setHeader('X-Frame-Options', 'SAMEORIGIN');
    const buf = await upstream.buffer();
    res.send(buf);
  } catch(err) {
    res.status(500).send('Error: ' + err.message);
  }
});

app.get('/complaint/file', requireAuth, requireEmployee, async (req, res) => {
  const filePath = req.query.path;
  const download = req.query.download === '1';
  if (!filePath) return res.status(400).send('path required');
  // Security: only allow paths under BSC Inspections/Complaints/
  if (!filePath.startsWith('BSC Inspections/Complaints/')) {
    return res.status(403).send('Access denied');
  }
  try {
    const token = await getToken();
    const upstream = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(filePath) + ':/content', { headers: { 'Authorization': 'Bearer ' + token } });
    if (!upstream.ok) {
      return res.status(upstream.status).send('File not found');
    }
    // Forward content type
    const ct = upstream.headers.get('content-type') || 'application/octet-stream';
    res.setHeader('Content-Type', ct);
    // Inline vs attachment
    const fname = filePath.split('/').pop();
    res.setHeader('Content-Disposition', (download ? 'attachment' : 'inline') + '; filename="' + fname + '"');
    // Allow embedding in iframe from same origin
    res.setHeader('X-Frame-Options', 'SAMEORIGIN');
    // Stream the response
    const buf = await upstream.buffer();
    res.send(buf);
  } catch(err) {
    res.status(500).send('Error: ' + err.message);
  }
});


// =====================================================
// PDF REGENERATION - rebuild past PDFs using new template
// Hit repeatedly: GET /regenerate-pdfs?type=quality&batch=0&key=BSC_MIGRATE_2026
// =====================================================
const BATCH_SIZE = 15;

async function fetchPhotosFromFolder(token, folderPath) {
  const photos = [];
  try {
    const listResp = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(folderPath) + ':/children?$top=50', { headers: { 'Authorization': 'Bearer ' + token } });
    if (!listResp.ok) return [];
    const items = (await listResp.json()).value || [];
    for (const it of items) {
      if (it.folder) continue;
      const name = (it.name || '').toLowerCase();
      if (!/\.(jpe?g|png|gif|webp)$/i.test(name)) continue;
      try {
        const dl = await fetch(it['@microsoft.graph.downloadUrl']);
        if (dl.ok) {
          const buf = await dl.buffer();
          const mime = (it.file && it.file.mimeType) || 'image/jpeg';
          photos.push({ name: it.name, type: mime, data: 'data:' + mime + ';base64,' + buf.toString('base64') });
        }
      } catch(e) { console.log('[regen] photo fetch err:', e.message); }
    }
  } catch(e) { console.log('[regen] folder list err:', e.message); }
  return photos;
}

// Reconstruct data object from a row of the Inspection Excel
function rowToInwardData(v) {
  return {
    timestamp: v[1]||'', vehicle_number: v[2]||'', batch_number: v[3]||'',
    make_of_coil: v[4]||'', grade: v[5]||'', width: v[6]||'',
    thickness: v[7]||'', coil_weight: v[8]||'', coil_id: v[9]||'',
    actual_thickness: v[10]||'', actual_width: v[11]||'',
    id_sticker: v[12]||'', edge_inner: v[13]||'', edge_outer: v[14]||'',
    scratch: v[15]||'', strapping: v[16]||'', rust: v[17]||'',
    other_damages: v[18]||'', wheels_india: (v[19] === true || v[19] === 'TRUE' || v[19] === 'true' || v[19] === 'Yes'),
    inspected_by: v[20]||'', remarks: v[21]||''
  };
}

function rowToQualityData(v) {
  const data = {
    timestamp: v[1]||'', customer_name: v[2]||'', date: v[3]||'',
    time: v[4]||'', coil_number: v[5]||'', batch_number: v[6]||'',
    make: v[7]||'', coil_grade: v[8]||'', coil_thickness: v[9]||'',
    coil_width: v[10]||'', coil_weight: v[11]||'',
    first_bit: v[12]||'', last_bit: v[13]||'', defective: v[14]||'',
    balance_wt: v[15]||'', coil_verified: v[16]||'', blade_clearance: v[17]||'',
    bur: v[18]||'', cutting_finish: v[19]||'', scalling: v[20]||'',
    pit_marks: v[21]||'', waviness: v[22]||'', center_bow: v[23]||'',
    cutting_bow: v[24]||'', surface_defects: v[25]||'',
    operator: v[26]||'', machine_name: v[27]||'', inspector: v[28]||'',
    remarks: v[29]||''
  };
  // Sheet measurements - columns 30-179 (30 rows x 5 fields each from old layout, or 6 fields new)
  // We try the new 6-field layout first; if file has old 5-field, the offsets won't match but won't crash
  data.sheet_measurements = [];
  for (let i = 0; i < 30; i++) {
    const base = 30 + i * 6;
    const row = {
      sheet_no: v[base]||'', thickness: v[base+1]||'', width: v[base+2]||'',
      length: v[base+3]||'', d1: v[base+4]||'', d2: v[base+5]||''
    };
    if (row.sheet_no || row.thickness || row.width || row.length) data.sheet_measurements.push(row);
  }
  // Processed quantity - columns 210+ (20 sizes x 3 fields)
  data.processed_qty = {};
  for (let i = 1; i <= 20; i++) {
    const base = 210 + (i-1) * 3;
    data.processed_qty['size_' + i] = {
      length: v[base]||'', nos: v[base+1]||'', weight_t: v[base+2]||''
    };
  }
  return data;
}

function rowToShearingData(v) {
  const data = {
    timestamp: v[1]||'', customer_name: v[2]||'', date: v[3]||'',
    batch_number: v[4]||'', grade: v[5]||'', make: v[6]||'', type: v[7]||'',
    process: v[8]||'', operator: v[9]||'', input_size: v[10]||'', qc_name: v[11]||'',
    burr: v[12]||'', blade_clearance: v[13]||'', cutting_finish: v[14]||'',
    surface_condition: v[15]||'', bow_bend: v[16]||'', taper_cutting: v[17]||'',
    rejection_flag: v[18]||'No', remarks: v[19]||'', overall_observation: v[20]||''
  };
  // Sheet measurements - 30 rows x 6 fields starting at col 21
  data.measurements = [];
  for (let i = 0; i < 30; i++) {
    const base = 21 + i * 6;
    const row = {
      sheet_no: v[base]||'', width1: v[base+1]||'', width2: v[base+2]||'',
      diag1: v[base+3]||'', diag2: v[base+4]||'', remarks: v[base+5]||''
    };
    if (row.sheet_no || row.width1 || row.width2) data.measurements.push(row);
  }
  // Output sizes - 20 cols starting at 201
  data.output_sizes = [];
  for (let i = 0; i < 20; i++) {
    if (v[201 + i]) data.output_sizes.push(v[201 + i]);
  }
  // Processed quantity - 20 sizes x 3 fields starting at 221
  data.processed_qty = {};
  for (let i = 1; i <= 20; i++) {
    const base = 221 + (i-1) * 3;
    data.processed_qty['size_' + i] = {
      length: v[base]||'', nos: v[base+1]||'', weight_t: v[base+2]||''
    };
  }
  // Rejections - 10 rows x 2 fields starting at 281
  data.rejections = [];
  for (let i = 0; i < 10; i++) {
    const base = 281 + i * 2;
    if (v[base] || v[base+1]) data.rejections.push({ size: v[base]||'', qty: v[base+1]||'' });
  }
  return data;
}

function rowToComplaintData(v) {
  return {
    case_id: v[0]||'', timestamp: v[1]||'', source: v[2]||'', filed_by: v[3]||'',
    customer_name: v[4]||'', batch_number: v[5]||'', grade: v[6]||'', dimensions: v[7]||'',
    tc_number: v[8]||'', invoice_no: v[9]||'', invoice_date: v[10]||'', quantity: v[11]||'',
    mill: v[12]||'', defect_type: v[13]||'', remarks: v[14]||'', status: v[15]||'',
    reviewed_by: v[16]||'', root_cause: v[17]||'', decision: v[18]||'',
    vendor_name: v[19]||'', vendor_email: v[20]||'', resolution: v[21]||'', resolved_date: v[22]||'',
    customer_email: v[23]||'', production_comments: v[24]||'', sales_reviewer: v[25]||'',
    customer_message: v[26]||'', customer_outcome_by: v[27]||'', customer_outcome_notes: v[28]||''
  };
}

app.get('/regenerate-pdfs', requireAuth, requireAdmin, async (req, res) => {
  if (req.query.key !== 'BSC_MIGRATE_2026') return res.status(403).json({ error: 'Invalid key' });
  const type = String(req.query.type || '').toLowerCase();
  const batch = parseInt(req.query.batch || '0', 10);
  if (!['inward','quality','shearing','complaints'].includes(type)) {
    return res.status(400).json({ error: 'type must be inward|quality|shearing|complaints' });
  }
  
  try {
    const token = await getToken();
    
    // Map type to Excel file + table
    const configs = {
      inward:    { file: 'BSC Inspections/Inward/Inward_Log.xlsx',     table: 'InwardLog',    pdfFolder: 'BSC Inspections/Inward/PDF',     photoBase: 'BSC Inspections/Inward/Photos' },
      quality:   { file: 'BSC Inspections/Quality/Quality_Log.xlsx',   table: 'QualityLog',   pdfFolder: 'BSC Inspections/Quality/PDF',    photoBase: 'BSC Inspections/Quality/Photos' },
      shearing:  { file: 'BSC Inspections/Shearing/Shearing_Log.xlsx', table: 'ShearingLog',  pdfFolder: 'BSC Inspections/Shearing/PDF',   photoBase: 'BSC Inspections/Shearing/Photos' },
      complaints:{ file: 'BSC Inspections/Complaints/Complaints_Log.xlsx', table: 'ComplaintsLog', pdfFolder: 'BSC Inspections/Complaints/PDF', photoBase: 'BSC Inspections/Complaints/Attachments' }
    };
    const cfg = configs[type];
    
    // Get fileId
    const meta = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(cfg.file), { headers: { 'Authorization': 'Bearer ' + token } });
    if (!meta.ok) return res.status(404).json({ error: 'Log file not found: ' + cfg.file });
    const fileId = (await meta.json()).id;
    
    // Read all rows
    const rowsResp = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/items/' + fileId + '/workbook/tables/' + cfg.table + '/rows', { headers: { 'Authorization': 'Bearer ' + token } });
    if (!rowsResp.ok) return res.status(500).json({ error: 'Cannot read table: ' + cfg.table });
    const allRows = (await rowsResp.json()).value || [];
    const total = allRows.length;
    
    // Slice this batch
    const start = batch * BATCH_SIZE;
    const end = Math.min(start + BATCH_SIZE, total);
    const slice = allRows.slice(start, end);
    
    const errors = [];
    const processed = [];
    
    for (const row of slice) {
      const v = row.values[0];
      try {
        if (type === 'complaints') {
          const cdata = rowToComplaintData(v);
          if (!cdata.case_id) { errors.push({ idx: row.index, err: 'no case_id' }); continue; }
          // Fetch photos from Attachments/<caseId> folder (only images)
          const photos = await fetchPhotosFromFolder(token, cfg.photoBase + '/' + cdata.case_id);
          const pdfBuf = await generateComplaintPDF(cdata, photos);
          const pdfName = cdata.case_id + '_Defect_Report.pdf';
          await uploadFile(token, cfg.pdfFolder + '/' + pdfName, pdfBuf, 'application/pdf');
          processed.push(cdata.case_id);
        } else {
          // Inspection types: Inward, Quality, Shearing
          const fileNameInExcel = v[0]; // first col is file name
          if (!fileNameInExcel) { errors.push({ idx: row.index, err: 'no filename' }); continue; }
          
          let data, folder, pdfPath, photoPath;
          if (type === 'inward') {
            data = rowToInwardData(v);
            folder = 'Inward';
            pdfPath = cfg.pdfFolder + '/' + fileNameInExcel + '.pdf';
            photoPath = cfg.photoBase + '/' + fileNameInExcel;
          } else if (type === 'quality') {
            data = rowToQualityData(v);
            folder = 'Quality';
            // CTL has machine-specific subfolders
            const machineNorm = String(data.machine_name || '').trim().toUpperCase().replace(/\s+/g, '');
            let sub = '';
            if (machineNorm === 'CTL-1' || machineNorm === 'CTL1') sub = '/CTL-1';
            else if (machineNorm === 'CTL-2' || machineNorm === 'CTL2') sub = '/CTL-2';
            pdfPath = cfg.pdfFolder + sub + '/' + fileNameInExcel + '.pdf';
            photoPath = cfg.photoBase + sub + '/' + fileNameInExcel;
          } else {
            data = rowToShearingData(v);
            folder = 'Shearing';
            pdfPath = cfg.pdfFolder + '/' + fileNameInExcel + '.pdf';
            photoPath = cfg.photoBase + '/' + fileNameInExcel;
          }
          
          // Fetch photos from OneDrive
          const photos = await fetchPhotosFromFolder(token, photoPath);
          data.photos = photos;
          
          // Reference number from filename for PDF header
          const refNo = fileNameInExcel;
          const pdfBuf = await generatePDF(folder, data, refNo);
          await uploadFile(token, pdfPath, pdfBuf, 'application/pdf');
          processed.push(fileNameInExcel);
        }
      } catch(e) {
        errors.push({ idx: row.index, err: e.message });
        console.log('[regen]', type, 'row', row.index, 'failed:', e.message);
      }
    }
    
    res.json({
      type: type,
      batch: batch,
      batch_size: BATCH_SIZE,
      total: total,
      processed_in_batch: processed.length,
      processed_total_so_far: end,
      done: end >= total,
      next_batch: end >= total ? null : (batch + 1),
      errors: errors,
      processed_files: processed
    });
  } catch(err) {
    res.status(500).json({ error: err.message, stack: err.stack });
  }
});

// =====================================================
// ADMIN: Settings storage on OneDrive
// =====================================================
const ADMIN_PASSWORD = 'Bsc@123';
const SETTINGS_PATH = 'BSC Inspections/Config/settings.json';

const DEFAULT_SETTINGS = {
  inspectors_inward: ['Sathya', 'Kumar', 'Mahendran', 'Dhanush'],
  inspectors_quality: ['Sathya', 'Kumar', 'Mahendran', 'Dhanush', 'Vignesh'],
  qc_shearing_default: 'Vignesh',
  mills: ['SAIL', 'SAIL RSP', 'SAIL BSP', 'JSW', 'TATA', 'AMNS', 'NMDC', 'JSPL'],
  vendors: { 'SAIL': '', 'NMDC': '', 'RINL': '', 'JSW': '' },
  grades: ['E250', 'E350 / ST52', 'HSFQ450 / 450 / EQV', 'HSFQ550 / 550 / EQV'],
  complaint_emails: {
    sales_raise_to: ['pdqc@bharatsteels.in', 'kannan@bharatsteels.in', 'gourav@bharatsteels.in'],
    internal_close_to: ['info@bharatsteels.in', 'gourav@bharatsteels.in', 'kannan@bharatsteels.in'],
    vendor_escalation_cc: ['pdqc@bharatsteels.in', 'kannan@bharatsteels.in', 'gourav@bharatsteels.in'],
    resolved_to: ['info@bharatsteels.in', 'pdqc@bharatsteels.in', 'kannan@bharatsteels.in', 'gourav@bharatsteels.in']
  },
  inward_emails: {
    default_to: ['support@bharatsteels.in'],
    wheels_india_to: ['support@bharatsteels.in', 'kannan@bharatsteels.in']
  }
};

async function loadSettings(token) {
  try {
    const resp = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(SETTINGS_PATH) + ':/content', { headers: { 'Authorization': 'Bearer ' + token } });
    if (resp.ok) {
      const text = await resp.text();
      return Object.assign({}, DEFAULT_SETTINGS, JSON.parse(text));
    }
  } catch(e) { console.log('Settings load error:', e.message); }
  return DEFAULT_SETTINGS;
}

async function saveSettings(token, settings) {
  // Ensure Config folder exists
  try {
    const checkResp = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent('BSC Inspections/Config'), { headers: { 'Authorization': 'Bearer ' + token } });
    if (!checkResp.ok) {
      const parentResp = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent('BSC Inspections'), { headers: { 'Authorization': 'Bearer ' + token } });
      const parentId = (await parentResp.json()).id;
      await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/items/' + parentId + '/children', {
        method: 'POST', headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: 'Config', folder: {}, '@microsoft.graph.conflictBehavior': 'rename' })
      });
    }
  } catch(e) { console.log('Config folder ensure error:', e.message); }
  
  // Upload settings.json
  const buf = Buffer.from(JSON.stringify(settings, null, 2));
  return uploadFile(token, SETTINGS_PATH, buf, 'application/json');
}

// GET /settings - public read of allowed config
app.get('/settings', requireAuth, requireEmployee, async (req, res) => {
  try {
    const token = await getToken();
    const s = await loadSettings(token);
    // Public can read everything (no secrets here)
    res.json(s);
  } catch(e) {
    res.status(500).json({ error: e.message });
  }
});

// POST /admin/settings - update settings (password required)
app.post('/admin/settings', requireAuth, requireAdmin, async (req, res) => {
  const { password, settings } = req.body;
  if (password !== ADMIN_PASSWORD) return res.status(403).json({ error: 'Invalid password' });
  if (!settings || typeof settings !== 'object') return res.status(400).json({ error: 'Invalid settings' });
  try {
    const token = await getToken();
    const current = await loadSettings(token);
    const merged = Object.assign({}, current, settings);
    await saveSettings(token, merged);
    res.json({ status: 'success', settings: merged });
  } catch(e) {
    res.status(500).json({ error: e.message });
  }
});

// POST /admin/verify - just check password
app.post('/admin/verify', requireAuth, requireAdmin, (req, res) => {
  if (req.body && req.body.password === ADMIN_PASSWORD) {
    return res.json({ status: 'success' });
  }
  res.status(403).json({ error: 'Invalid password' });
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log('BSC Server running on port ' + PORT));
