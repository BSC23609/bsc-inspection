// Vercel serverless entry. All routes are handled by this single Express app
// (vercel.json rewrites /api/* and /api/cron/* here). Static pages live in /public.
const express = require('express');
const { q } = require('../lib/db');
const wati = require('../lib/wati');

const app = express();
app.use(express.json({ limit: '1mb' })); // photos go straight to OneDrive, not through here
app.use(express.urlencoded({ extended: false })); // reject-reason form post from the WhatsApp link

// Keep the schema current on deploy (adds any new columns) — but do it in the BACKGROUND on cold
// start, never in the request path, so it can't add latency or hang a request. Idempotent.
require('../lib/autoMigrate').autoMigrate().catch(() => {});

app.get('/api/health', (req, res) =>
  res.json({ ok: true, wati: wati.configured(), graph: require('../lib/graph').configured() }));

// Keep-alive: runs a trivial query so Neon's compute does NOT auto-suspend during working hours.
// A plain HTTP ping is not enough — Neon suspends on DB inactivity, so we must actually touch the DB.
// No secret needed (it only does SELECT 1). Answers GET and HEAD so UptimeRobot-style monitors work too.
app.all('/api/keepalive', async (req, res) => {
  const t0 = Date.now();
  try {
    await q('SELECT 1');
    res.json({ ok: true, db: true, waking_ms: Date.now() - t0 });
  } catch (e) {
    // Return 200 regardless so a monitor doesn't page anyone — the query itself is what wakes Neon.
    res.json({ ok: true, db: false, error: e.message });
  }
});

// Deep link target for WhatsApp buttons: /t/<id> -> login (or app) carrying the ticket.
app.get('/t/:id', (req, res) =>
  res.redirect('/?t=' + encodeURIComponent(req.params.id)));

// Outpass approver deep link: /og/<id> -> login -> approval page for that request.
app.get('/og/:id', (req, res) =>
  res.redirect('/?og=' + encodeURIComponent(req.params.id)));

// Expense approver deep link: /e/<id> -> login -> that claim opens in the portal.
app.get('/e/:id', (req, res) =>
  res.redirect('/?claim=' + encodeURIComponent(req.params.id)));

// Gate self-return: the QR poster points here. Static page handles auth + GPS.
app.get('/gate', (req, res) => res.redirect('/gate.html'));

// One-tap approve/reject straight from the WhatsApp buttons (token = the request's
// action_token). No login: the unguessable token is the authorisation. Single-use
// (a second tap shows the current status) and dead once the pass date has passed.
const outpassActions = require('../routes/outpass.routes')._internal;
function actionPage(emoji, title, msg) {
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${title}</title></head>
  <body style="margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f1f5f9">
  <div style="max-width:440px;margin:12vh auto;background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:32px 24px;text-align:center">
    <div style="font-size:56px;line-height:1">${emoji}</div>
    <h2 style="color:#112532;margin:.5em 0 .2em">${title}</h2>
    <p style="color:#64748b;font-size:15px;margin:0">${msg}</p>
  </div></body></html>`;
}
// The "I'm Back" page: asks for the phone's location and only logs the return if at the gate.
function gprPage(token, ref) {
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Log your return</title></head>
  <body style="margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f1f5f9">
  <div style="max-width:440px;margin:10vh auto;background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:28px 22px;text-align:center">
    <div style="font-size:52px;line-height:1">📍</div>
    <h2 style="color:#112532;margin:.4em 0 .2em">Log your return</h2>
    <p id="msg" style="color:#64748b;font-size:15px;margin:0 0 18px">Gatepass <b>${ref}</b>. Tap below and allow location — we'll confirm you're at the gate, then record your return.</p>
    <button id="btn" style="width:100%;padding:14px;border:0;border-radius:12px;background:#0A4566;color:#fff;font-size:16px;font-weight:600;cursor:pointer">I'm at the gate — log my return</button>
    <p id="hint" style="color:#94a3b8;font-size:12px;margin:14px 0 0">Your location is used only to verify you're at the gate.</p>
  </div>
  <script>
    var token=${JSON.stringify(token)}, btn=document.getElementById('btn'), msg=document.getElementById('msg'), hint=document.getElementById('hint');
    function ok(t,h){ document.querySelector('div').innerHTML='<div style="font-size:52px">✅</div><h2 style="color:#112532;margin:.4em 0 .2em">'+t+'</h2><p style="color:#64748b;font-size:15px">'+h+'</p>'; }
    function fail(t,h,retry){ msg.innerHTML='<span style="color:#b91c1c">'+t+'</span><br>'+h; btn.disabled=false; btn.textContent = retry||"Try again"; }
    function getPos(){ return new Promise(function(res,rej){ if(!navigator.geolocation) return rej({m:'Location is not supported on this device.'});
      navigator.geolocation.getCurrentPosition(function(p){res(p.coords);}, function(e){ rej({m: e.code===1?'Location permission denied. Please allow location for this page and try again.':'Could not get your location. Step into open sky at the gate and retry.'}); }, {enableHighAccuracy:true,timeout:15000,maximumAge:0}); }); }
    btn.onclick=async function(){
      btn.disabled=true; btn.textContent='Getting your location…';
      try{
        var c=await getPos();
        btn.textContent='Recording return…';
        var r=await fetch('/gpr/'+encodeURIComponent(token)+'/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({lat:c.latitude,lng:c.longitude,accuracy:c.accuracy})});
        var d=await r.json();
        if(d.status==='ok') return ok('Return logged','Thanks! Your return'+(d.ref?' for '+d.ref:'')+' is recorded'+(d.dist!=null?' · '+d.dist+' m from the gate':'')+'. You can close this page.');
        if(d.status==='far') return fail('You appear to be '+d.dist+' m from the gate.','Please come to the gate and tap again to log your return.','Try again at the gate');
        if(d.status==='already') return ok('Already logged','Your return was already recorded. Thank you!');
        if(d.status==='nogate') return fail('Gate location isn\\'t set up yet.','Please ask the admin to set the gate location, or use the QR at the gate.');
        if(d.status==='noloc') return fail('Location not received.','Please allow location access and try again.');
        return fail('This link is no longer valid.','Please use the QR at the gate or ask the guard to mark your return.');
      }catch(e){ fail('Location needed',(e&&e.m)||'Please allow location and try again.'); }
    };
  </script>
  </body></html>`;
}
async function loadByToken(token) {
  return (await q(
    `SELECT o.*, ap.name AS approver_name, r.name AS req_name
     FROM outpass_requests o LEFT JOIN employees ap ON ap.id = o.approver_id
     JOIN employees r ON r.id = o.requester_id WHERE o.action_token=$1`, [token])).rows[0];
}
function passExpired(reqDate) {
  const istToday = new Date(Date.now() + 5.5 * 3600 * 1000).toISOString().slice(0, 10);
  return new Date(reqDate).toISOString().slice(0, 10) < istToday;
}
const typeName = (t) => (t === 'gatepass' ? 'gate pass' : 'outpass');

app.get('/oga/:token', async (req, res) => {
  try {
    const o = await loadByToken(req.params.token);
    if (!o) return res.status(404).send(actionPage('⛔', 'Link not valid', 'This approval link is not recognised.'));
    if (o.status !== 'pending') return res.send(actionPage(o.status === 'approved' ? '✅' : '⛔', `Already ${o.status}`, `This request was already ${o.status}${o.actioned_by_name ? ' by ' + o.actioned_by_name : ''}.`));
    if (passExpired(o.req_date)) return res.send(actionPage('🕒', 'Link expired', 'The date on this pass has already passed.'));
    await outpassActions.applyApprove(o, o.approver_id, o.approver_name || o.approver_label || 'Approver');
    res.send(actionPage('✅', 'Approved', `${o.req_name}'s ${typeName(o.type)} is approved. The pass has been sent to them on WhatsApp.`));
  } catch (e) { console.error('oga', e); res.status(500).send(actionPage('⚠️', 'Something went wrong', 'Please try again, or use the app.')); }
});

app.get('/ogr/:token', async (req, res) => {
  try {
    const o = await loadByToken(req.params.token);
    if (!o) return res.status(404).send(actionPage('⛔', 'Link not valid', 'This link is not recognised.'));
    if (o.status !== 'pending') return res.send(actionPage(o.status === 'approved' ? '✅' : '⛔', `Already ${o.status}`, `This request was already ${o.status}${o.actioned_by_name ? ' by ' + o.actioned_by_name : ''}.`));
    if (passExpired(o.req_date)) return res.send(actionPage('🕒', 'Link expired', 'The date on this pass has already passed.'));
    await outpassActions.applyReject(o, o.approver_id, o.approver_name || o.approver_label || 'Approver', null);
    res.send(actionPage('⛔', 'Rejected', `${o.req_name}'s ${typeName(o.type)} has been rejected. They have been notified.`));
  } catch (e) { console.error('ogr', e); res.status(500).send(actionPage('⚠️', 'Something went wrong', 'Please try again, or use the app.')); }
});

// ---- Local Conveyance: reporting-manager one-tap approve/reject (per trip) ----
const fmtTripDate = (s) => { if (!s) return ''; const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d).toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' }); };
const tripRouteLabel = (t) => [t.from_loc, t.to_loc].filter(Boolean).join(' \u2192 ') || (t.purpose || 'Trip');
const inrLabel = (n) => '₹' + Number(n || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
function rejectPage(token, t) {
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Reject trip</title></head>
  <body style="margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f1f5f9">
  <div style="max-width:440px;margin:8vh auto;background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:28px 24px">
    <div style="font-size:44px;line-height:1;text-align:center">📝</div>
    <h2 style="color:#112532;margin:.4em 0 .2em;text-align:center">Reject this trip?</h2>
    <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:12px 14px;margin:14px 0;color:#334155;font-size:14px">
      <div><b>${t.emp_name}</b></div>
      <div style="color:#64748b">${fmtTripDate(t.trip_date_s)} · ${tripRouteLabel(t)}</div>
      <div style="color:#64748b">${t.purpose ? t.purpose + ' · ' : ''}${t.km} km · ${inrLabel(t.amount)}</div>
    </div>
    <form method="POST" action="/cvr/${token}">
      <label style="font-size:13px;color:#64748b">Reason (optional, shown to the employee)</label>
      <textarea name="reason" rows="3" placeholder="e.g. Wrong distance / personal trip" style="width:100%;box-sizing:border-box;margin-top:6px;padding:10px;border:1px solid #cbd5e1;border-radius:10px;font-size:14px"></textarea>
      <button type="submit" style="width:100%;margin-top:14px;background:#dc2626;color:#fff;border:0;border-radius:10px;padding:13px;font-size:15px;font-weight:600">Confirm rejection</button>
    </form>
  </div></body></html>`;
}

// --- Maintenance-gate one-tap (WhatsApp button -> /mta/:token) ---
function maintApprovePage(token, t) {
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Maintenance request</title></head>
  <body style="margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f1f5f9">
  <div style="max-width:460px;margin:6vh auto;background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:26px 22px">
    <div style="font-size:42px;line-height:1;text-align:center">\ud83d\udd27</div>
    <h2 style="color:#112532;margin:.4em 0 .2em;text-align:center">Maintenance request</h2>
    <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:12px 14px;margin:14px 0;color:#334155;font-size:14px">
      <div style="font-weight:700">${t.ref_no}${t.category_name ? ' \u00b7 ' + t.category_name : ''}</div>
      <div style="margin-top:4px">${t.subject || ''}</div>
      <div style="color:#64748b;margin-top:4px">Raised by ${t.requester_name || '\u2014'}</div>
    </div>
    <form method="POST" action="/mta/${token}/approve">
      <button type="submit" style="width:100%;background:#059669;color:#fff;border:0;border-radius:10px;padding:14px;font-size:16px;font-weight:700">\u2713 Approve \u2014 send to maintenance</button>
    </form>
    <a href="/mta/${token}/reject" style="display:block;text-align:center;margin-top:12px;color:#dc2626;font-weight:600;font-size:14px;text-decoration:none">Reject</a>
  </div></body></html>`;
}
function maintDone(emoji, title, msg) { return actionPage(emoji, title, msg); }
app.get('/mta/:token', async (req, res) => {
  try {
    const tk = require('../routes/tickets.routes')._internal;
    const t = await tk.loadTicketByMaintToken(req.params.token);
    if (!t) return res.status(404).send(actionPage('\u26d4', 'Link not valid', 'This link is not recognised, or the request was already handled.'));
    if (t.maint_gate !== 'pending') return res.send(actionPage(t.maint_gate === 'approved' ? '\u2705' : '\u26d4', `Already ${t.maint_gate}`, `This request was already ${t.maint_gate}.`));
    res.send(maintApprovePage(req.params.token, t));
  } catch (e) { console.error('mta-get', e); res.status(500).send(actionPage('\u26a0\ufe0f', 'Something went wrong', 'Please try again.')); }
});
app.post('/mta/:token/approve', async (req, res) => {
  try {
    const tk = require('../routes/tickets.routes')._internal;
    const t = await tk.loadTicketByMaintToken(req.params.token);
    if (!t) return res.status(404).send(actionPage('\u26d4', 'Link not valid', 'This link is not recognised, or the request was already handled.'));
    if (t.maint_gate !== 'pending') return res.send(actionPage(t.maint_gate === 'approved' ? '\u2705' : '\u26d4', `Already ${t.maint_gate}`, `This request was already ${t.maint_gate}.`));
    await tk.applyMaintApprove(t);
    res.send(actionPage('\u2705', 'Approved', `${t.ref_no} has been approved and sent to the maintenance team.`));
  } catch (e) { console.error('mta-approve', e); res.status(500).send(actionPage('\u26a0\ufe0f', 'Something went wrong', 'Please try again.')); }
});
app.get('/mta/:token/reject', async (req, res) => {
  try {
    const tk = require('../routes/tickets.routes')._internal;
    const t = await tk.loadTicketByMaintToken(req.params.token);
    if (!t) return res.status(404).send(actionPage('\u26d4', 'Link not valid', 'This link is not recognised, or the request was already handled.'));
    if (t.maint_gate !== 'pending') return res.send(actionPage(t.maint_gate === 'approved' ? '\u2705' : '\u26d4', `Already ${t.maint_gate}`, `This request was already ${t.maint_gate}.`));
    res.send(`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Reject request</title></head>
    <body style="margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f1f5f9">
    <div style="max-width:460px;margin:8vh auto;background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:26px 22px">
      <div style="font-size:42px;line-height:1;text-align:center">\ud83d\udeab</div>
      <h2 style="color:#112532;margin:.4em 0 .2em;text-align:center">Reject this request?</h2>
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:12px 14px;margin:14px 0;color:#334155;font-size:14px">
        <div style="font-weight:700">${t.ref_no}</div><div>${t.subject || ''}</div>
      </div>
      <p style="font-size:13px;color:#64748b">The requester will be asked to raise it through the Opmaint app.</p>
      <form method="POST" action="/mta/${req.params.token}/reject">
        <button type="submit" style="width:100%;margin-top:8px;background:#dc2626;color:#fff;border:0;border-radius:10px;padding:13px;font-size:15px;font-weight:600">Confirm rejection</button>
      </form>
    </div></body></html>`);
  } catch (e) { console.error('mta-reject-get', e); res.status(500).send(actionPage('\u26a0\ufe0f', 'Something went wrong', 'Please try again.')); }
});
app.post('/mta/:token/reject', async (req, res) => {
  try {
    const tk = require('../routes/tickets.routes')._internal;
    const t = await tk.loadTicketByMaintToken(req.params.token);
    if (!t) return res.status(404).send(actionPage('\u26d4', 'Link not valid', 'This link is not recognised, or the request was already handled.'));
    if (t.maint_gate !== 'pending') return res.send(actionPage(t.maint_gate === 'approved' ? '\u2705' : '\u26d4', `Already ${t.maint_gate}`, `This request was already ${t.maint_gate}.`));
    await tk.applyMaintReject(t);
    res.send(actionPage('\u26d4', 'Rejected', `${t.ref_no} was rejected. The requester has been asked to use the Opmaint app.`));
  } catch (e) { console.error('mta-reject-post', e); res.status(500).send(actionPage('\u26a0\ufe0f', 'Something went wrong', 'Please try again.')); }
});

// Two-button style: token at the END of the URL (Meta requires the dynamic var last).
// A tapped WhatsApp button is a GET, so these act directly. Single-use token guards double-taps.
app.get('/mta/approve/:token', async (req, res) => {
  try {
    const tk = require('../routes/tickets.routes')._internal;
    const t = await tk.loadTicketByMaintToken(req.params.token);
    if (!t) return res.status(404).send(actionPage('\u26d4', 'Link not valid', 'This link is not recognised, or the request was already handled.'));
    if (t.maint_gate !== 'pending') return res.send(actionPage(t.maint_gate === 'approved' ? '\u2705' : '\u26d4', `Already ${t.maint_gate}`, `This request was already ${t.maint_gate}.`));
    await tk.applyMaintApprove(t);
    res.send(actionPage('\u2705', 'Approved', `${t.ref_no} has been approved and sent to the maintenance team.`));
  } catch (e) { console.error('mta-approve-get', e); res.status(500).send(actionPage('\u26a0\ufe0f', 'Something went wrong', 'Please try again.')); }
});
app.get('/mta/reject/:token', async (req, res) => {
  try {
    const tk = require('../routes/tickets.routes')._internal;
    const t = await tk.loadTicketByMaintToken(req.params.token);
    if (!t) return res.status(404).send(actionPage('\u26d4', 'Link not valid', 'This link is not recognised, or the request was already handled.'));
    if (t.maint_gate !== 'pending') return res.send(actionPage(t.maint_gate === 'approved' ? '\u2705' : '\u26d4', `Already ${t.maint_gate}`, `This request was already ${t.maint_gate}.`));
    await tk.applyMaintReject(t);
    res.send(actionPage('\u26d4', 'Rejected', `${t.ref_no} was rejected. The requester has been asked to use the Opmaint app.`));
  } catch (e) { console.error('mta-reject-get', e); res.status(500).send(actionPage('\u26a0\ufe0f', 'Something went wrong', 'Please try again.')); }
});

// --- requester one-tap "I'm Back" from the WhatsApp return reminder (no app / no scan) ---
app.get('/gpr/:token', async (req, res) => {
  try {
    if (req.params.token === 'test-token')
      return res.send(gprPage('test-token', 'GP-TEST-01'));
    const op = require('../routes/outpass.routes')._internal;
    const r = await op.peekReturnToken(req.params.token);
    if (r.status === 'notfound') return res.status(404).send(actionPage('\u26d4', 'Link not valid', 'This return link is not recognised or has expired.'));
    if (r.status === 'already') return res.send(actionPage('\u2705', 'Already logged', `Your return for ${r.ref} was already recorded. Thank you!`));
    if (r.status === 'invalid') return res.send(actionPage('\u26d4', 'Not applicable', 'This pass can no longer be marked returned.'));
    res.send(gprPage(req.params.token, r.ref));   // ask for location, then verify
  } catch (e) { console.error('gpr', e); res.status(500).send(actionPage('\u26a0\ufe0f', 'Something went wrong', 'Please try again in a moment.')); }
});
// The page posts the phone's GPS here; we only log the return if inside the gate radius.
app.post('/gpr/:token/confirm', async (req, res) => {
  try {
    if (req.params.token === 'test-token') return res.json({ status: 'ok', ref: 'GP-TEST-01', dist: 0, test: true });
    const { lat, lng, accuracy } = req.body || {};
    const op = require('../routes/outpass.routes')._internal;
    const r = await op.markReturnByTokenGps(req.params.token, Number(lat), Number(lng), accuracy);
    const code = { ok: 200, far: 422, noloc: 400, nogate: 503, already: 409, invalid: 409, notfound: 404 }[r.status] || 400;
    res.status(code).json(r);
  } catch (e) { console.error('gpr confirm', e); res.status(500).json({ status: 'error' }); }
});

// --- OT one-tap approval (WhatsApp button -> /ota/:token) ---
function otApprovePage(token, o) {
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Approve overtime</title></head>
  <body style="margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f1f5f9">
  <div style="max-width:440px;margin:7vh auto;background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:28px 24px">
    <div style="font-size:44px;line-height:1;text-align:center">\u23f0</div>
    <h2 style="color:#112532;margin:.4em 0 .2em;text-align:center">Approve this overtime?</h2>
    <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:12px 14px;margin:14px 0;color:#334155;font-size:14px">
      <div><b>${o.emp_name}</b>${o.department ? ' \u00b7 ' + o.department : ''}</div>
      <div style="color:#64748b">${fmtTripDate(o.ot_date_s)} \u00b7 ends ${o.end_time} \u00b7 ${Number(o.hours).toFixed(2)} h${o.is_late ? ' \u00b7 <span style="color:#b45309;font-weight:600">late</span>' : ''}</div>
      <div style="color:#0f766e;font-weight:700;font-size:16px;margin-top:4px">${inrLabel(o.amount)}</div>
    </div>
    <form method="POST" action="/ota/${token}/approve">
      <button type="submit" style="width:100%;background:#059669;color:#fff;border:0;border-radius:10px;padding:14px;font-size:16px;font-weight:700">\u2713 Approve</button>
    </form>
    <a href="/ota/${token}/reject" style="display:block;text-align:center;margin-top:12px;color:#dc2626;font-weight:600;font-size:14px;text-decoration:none">Reject instead</a>
  </div></body></html>`;
}
app.get('/ota/:token', async (req, res) => {
  try {
    const ot = require('../routes/ot.routes')._internal;
    const o = await ot.loadOtByToken(req.params.token);
    if (!o) return res.status(404).send(actionPage('\u26d4', 'Link not valid', 'This approval link is not recognised, or the OT was already handled.'));
    if (o.status !== 'pending') return res.send(actionPage(o.status === 'approved' ? '\u2705' : '\u26d4', `Already ${o.status}`, `This OT entry was already ${o.status}.`));
    res.send(otApprovePage(req.params.token, o));
  } catch (e) { console.error('ota-get', e); res.status(500).send(actionPage('\u26a0\ufe0f', 'Something went wrong', 'Please try again.')); }
});
app.post('/ota/:token/approve', async (req, res) => {
  try {
    const ot = require('../routes/ot.routes')._internal;
    const o = await ot.loadOtByToken(req.params.token);
    if (!o) return res.status(404).send(actionPage('\u26d4', 'Link not valid', 'This link is not recognised, or the OT was already handled.'));
    if (o.status !== 'pending') return res.send(actionPage(o.status === 'approved' ? '\u2705' : '\u26d4', `Already ${o.status}`, `This OT entry was already ${o.status}.`));
    await ot.applyOtApprove(o);
    res.send(actionPage('\u2705', 'Approved', `${o.emp_name}'s overtime on ${fmtTripDate(o.ot_date_s)} (${inrLabel(o.amount)}) is approved and sent to HR.`));
  } catch (e) { console.error('ota-approve', e); res.status(500).send(actionPage('\u26a0\ufe0f', 'Something went wrong', 'Please try again.')); }
});
app.get('/ota/:token/reject', async (req, res) => {
  try {
    const ot = require('../routes/ot.routes')._internal;
    const o = await ot.loadOtByToken(req.params.token);
    if (!o) return res.status(404).send(actionPage('\u26d4', 'Link not valid', 'This link is not recognised, or the OT was already handled.'));
    if (o.status !== 'pending') return res.send(actionPage(o.status === 'approved' ? '\u2705' : '\u26d4', `Already ${o.status}`, `This OT entry was already ${o.status}.`));
    res.send(`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Reject overtime</title></head>
    <body style="margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f1f5f9">
    <div style="max-width:440px;margin:8vh auto;background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:28px 24px">
      <div style="font-size:44px;line-height:1;text-align:center">\ud83d\udcdd</div>
      <h2 style="color:#112532;margin:.4em 0 .2em;text-align:center">Reject this overtime?</h2>
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:12px 14px;margin:14px 0;color:#334155;font-size:14px">
        <div><b>${o.emp_name}</b></div>
        <div style="color:#64748b">${fmtTripDate(o.ot_date_s)} \u00b7 ends ${o.end_time} \u00b7 ${inrLabel(o.amount)}</div>
      </div>
      <form method="POST" action="/ota/${req.params.token}/reject">
        <label style="font-size:13px;color:#64748b">Reason (optional, shown to the employee)</label>
        <textarea name="reason" rows="3" placeholder="e.g. Wrong end time" style="width:100%;box-sizing:border-box;margin-top:6px;padding:10px;border:1px solid #cbd5e1;border-radius:10px;font-size:14px"></textarea>
        <button type="submit" style="width:100%;margin-top:14px;background:#dc2626;color:#fff;border:0;border-radius:10px;padding:13px;font-size:15px;font-weight:600">Confirm rejection</button>
      </form>
    </div></body></html>`);
  } catch (e) { console.error('ota-reject-get', e); res.status(500).send(actionPage('\u26a0\ufe0f', 'Something went wrong', 'Please try again.')); }
});
app.post('/ota/:token/reject', async (req, res) => {
  try {
    const ot = require('../routes/ot.routes')._internal;
    const o = await ot.loadOtByToken(req.params.token);
    if (!o) return res.status(404).send(actionPage('\u26d4', 'Link not valid', 'This link is not recognised, or the OT was already handled.'));
    if (o.status !== 'pending') return res.send(actionPage(o.status === 'approved' ? '\u2705' : '\u26d4', `Already ${o.status}`, `This OT entry was already ${o.status}.`));
    const reason = ((req.body && req.body.reason) || '').toString().trim();
    await ot.applyOtReject(o, reason);
    res.send(actionPage('\u26d4', 'Rejected', `${o.emp_name}'s overtime on ${fmtTripDate(o.ot_date_s)} has been rejected. They have been notified.`));
  } catch (e) { console.error('ota-reject-post', e); res.status(500).send(actionPage('\u26a0\ufe0f', 'Something went wrong', 'Please try again.')); }
});
app.get('/cva/:token', async (req, res) => {
  try {
    const conv = require('../routes/expense.routes')._internal;
    const t = await conv.loadTripByToken(req.params.token);
    if (!t) return res.status(404).send(actionPage('⛔', 'Link not valid', 'This approval link is not recognised, or the trip was already handled.'));
    if (t.status !== 'pending') return res.send(actionPage(t.status === 'approved' ? '✅' : '⛔', `Already ${t.status}`, `This trip was already ${t.status}.`));
    await conv.applyTripApprove(t);
    res.send(actionPage('✅', 'Approved', `${t.emp_name}'s trip on ${fmtTripDate(t.trip_date_s)} (${tripRouteLabel(t)}) is approved. They have been notified.`));
  } catch (e) { console.error('cva', e); res.status(500).send(actionPage('⚠️', 'Something went wrong', 'Please try again.')); }
});
app.get('/cvr/:token', async (req, res) => {
  try {
    const conv = require('../routes/expense.routes')._internal;
    const t = await conv.loadTripByToken(req.params.token);
    if (!t) return res.status(404).send(actionPage('⛔', 'Link not valid', 'This link is not recognised, or the trip was already handled.'));
    if (t.status !== 'pending') return res.send(actionPage(t.status === 'approved' ? '✅' : '⛔', `Already ${t.status}`, `This trip was already ${t.status}.`));
    res.send(rejectPage(req.params.token, t));
  } catch (e) { console.error('cvr-get', e); res.status(500).send(actionPage('⚠️', 'Something went wrong', 'Please try again.')); }
});
app.post('/cvr/:token', async (req, res) => {
  try {
    const conv = require('../routes/expense.routes')._internal;
    const t = await conv.loadTripByToken(req.params.token);
    if (!t) return res.status(404).send(actionPage('⛔', 'Link not valid', 'This link is not recognised, or the trip was already handled.'));
    if (t.status !== 'pending') return res.send(actionPage(t.status === 'approved' ? '✅' : '⛔', `Already ${t.status}`, `This trip was already ${t.status}.`));
    const reason = ((req.body && req.body.reason) || '').toString().trim();
    await conv.applyTripReject(t, reason);
    res.send(actionPage('⛔', 'Rejected', `${t.emp_name}'s trip on ${fmtTripDate(t.trip_date_s)} has been rejected. They have been notified.`));
  } catch (e) { console.error('cvr-post', e); res.status(500).send(actionPage('⚠️', 'Something went wrong', 'Please try again.')); }
});

// Public, no-login PDF download for an approved pass (token from the WhatsApp button).
// The slip is regenerated from the DB on demand, so the link always works.
app.get('/dl/:token', async (req, res) => {
  try {
    const o = (await q(
      `SELECT o.*, r.emp_no AS req_code, r.name AS req_name, r.job_title AS designation
       FROM outpass_requests o JOIN employees r ON r.id = o.requester_id
       WHERE o.pdf_token=$1 AND o.status='approved'`, [req.params.token])).rows[0];
    if (!o) return res.status(404).send('Pass not found.');
    const { buildOutpassPDF } = require('../lib/outpass_pdf');
    const fmtDate = (d) => new Date(d).toLocaleDateString('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', year: 'numeric' });
    const fmtDateTime = (d) => new Date(d).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
    const pdf = await buildOutpassPDF({
      type: o.type, on_duty: o.on_duty, date: fmtDate(o.req_date), emp_code: o.req_code,
      name: o.req_name, designation: o.designation || '', purpose: o.purpose, out_time: o.out_time,
      in_time: o.in_time, ref_no: o.ref_no, approver: o.actioned_by_name, approved_at: fmtDateTime(o.actioned_at) });
    res.setHeader('Content-Type', 'application/pdf');
    res.setHeader('Content-Disposition', `inline; filename="${o.req_name} ${o.ref_no.replace(/\//g, '-')}.pdf"`);
    res.send(pdf);
  } catch (e) { console.error('dl', e); res.status(500).send('Could not generate the pass.'); }
});

app.use('/api', require('../routes/auth.routes'));
app.use('/api/tickets', require('../routes/tickets.routes'));
app.use('/api/outpass', require('../routes/outpass.routes'));
app.use('/api/expense', require('../routes/expense.routes'));
app.use('/api/ot', require('../routes/ot.routes'));
app.use('/api/labour', require('../routes/labour.routes'));
app.use('/api/final', require('../routes/final.routes'));
app.use('/api/accounts', require('../routes/accounts.routes'));
app.use('/api/inbox', require('../routes/inbox.routes'));
app.use('/api/admin', require('../routes/admin.routes'));
app.use('/api/canteen', require('../routes/canteen.routes'));

// Public, no-login download of an expense PDF (token from the portal / email).
app.get('/rx/:token', async (req, res) => {
  try {
    const report = require('../lib/expense_report');
    const period = report.verifyLink(req.params.token);
    if (!period) return res.status(404).send('This report link has expired or is invalid.');
    const buf = await report.buildReportBuffer(period);
    res.setHeader('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
    res.setHeader('Content-Disposition', `attachment; filename="${report.reportFileName(period)}"`);
    res.send(buf);
  } catch (e) { console.error('rx', e); res.status(500).send('Could not generate the report.'); }
});
app.get('/dlx/:token', async (req, res) => {
  try {
    const exp = require('../routes/expense.routes')._internal;
    const row = (await q(`SELECT s.*, e.name AS emp_name, e.emp_no AS emp_code, e.job_title AS designation,
      e.expense_category FROM expense_submissions s JOIN employees e ON e.id=s.employee_id
      WHERE s.pdf_token=$1`, [req.params.token])).rows[0];
    if (!row) return res.status(404).send('Not found.');
    let pdf;
    const approved = row.status === 'approved';
    const approver = approved ? (row.final_by_name || row.reviewed_by_name) : undefined;
    const approvedAt = approved && (row.final_at || row.reviewed_at) ? new Date(row.final_at || row.reviewed_at).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : undefined;
    if (row.form_type === 'conveyance') pdf = await exp.conveyancePdf(row, row.status, approver, approvedAt);
    else if (row.form_type === 'outstation') pdf = await exp.outstationPdf(row, row.status, approver, approvedAt);
    else if (row.form_type === 'misc') pdf = await exp.miscPdf(row);
    else return res.status(404).send('Not available.');
    res.setHeader('Content-Type', 'application/pdf');
    res.setHeader('Content-Disposition', `inline; filename="${row.emp_name} ${row.ref_no.replace(/\//g, '-')}.pdf"`);
    res.send(pdf);
  } catch (e) { console.error('dlx', e); res.status(500).send('Could not generate the PDF.'); }
});

// ---- Cron: 48-hr auto-confirm-close of resolved tickets ----
// Triggered daily by Vercel Cron (see vercel.json). Protected by CRON_SECRET.
// Heartbeat: record when each cron last ran, so the admin Health page can flag a stale/stopped cron.
async function recordCron(name) {
  try { await q(`INSERT INTO app_settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value`,
    ['cron_last_' + name, new Date().toISOString()]); } catch (e) {}
}

// Watchdog: if a cron hasn't run within its expected window, email an ops address — but at most once
// per cooldown window, so GitHub Actions' flaky/late scheduling doesn't cause an email storm.
// Thresholds match each job's REAL cadence, not a flat number:
//   escalate */15 and outpass-overdue */5 (GitHub, often delayed) → 90 / 60 min
//   auto-close & daily-report run once a day (Vercel cron) → ~26 h
//   trip-nudge runs Mon-Sat → allow the ~48 h Sunday gap
const CRON_MAX_STALE_MIN = { 'escalate': 180, 'outpass-overdue': 120, 'auto-close': 1560, 'daily-report': 1560, 'trip-nudge': 3000 };
const CRON_ALERT_COOLDOWN_MIN = Number(process.env.CRON_ALERT_COOLDOWN_MIN || 720); // 12h
async function checkStaleCrons() {
  try {
    const email = (await q(`SELECT value FROM app_settings WHERE key='ops_alert_email'`)).rows[0]?.value;
    if (!email) return;                                   // no recipient set → nothing to do
    const last = Object.fromEntries((await q(`SELECT key,value FROM app_settings WHERE key LIKE 'cron_last_%'`)).rows
      .map(r => [r.key.replace('cron_last_', ''), r.value]));
    const now = Date.now(); const stale = [];
    for (const [name, max] of Object.entries(CRON_MAX_STALE_MIN)) {
      const iso = last[name]; if (!iso) continue;         // never run yet (fresh deploy) → don't cry wolf
      const mins = (now - new Date(iso)) / 60000;
      if (mins > max) stale.push(`${name}: last ran ${Math.round(mins)} min ago (limit ${max} min)`);
    }
    if (!stale.length) return;
    // Cooldown: only one alert per window, no matter how often a cron flaps around its threshold.
    const lastAlert = (await q(`SELECT value FROM app_settings WHERE key='cron_stale_alerted'`)).rows[0]?.value;
    if (lastAlert && (now - new Date(lastAlert)) < CRON_ALERT_COOLDOWN_MIN * 60000) return;
    const ok = await require('../lib/graph').sendMail({
      to: email, subject: '⚠ BSC Tickets — a background job looks stopped',
      html: `<p>These scheduled jobs haven't run recently:</p><ul>${stale.map(s => `<li>${s}</li>`).join('')}</ul>`
          + `<p>Note: GitHub Actions often delays scheduled runs, so brief lateness can be normal. If it persists, check the Actions runs / Vercel cron. The admin <b>Health</b> tab shows live status.</p>`
          + `<p style="color:#888">You won't get another of these for at least ${Math.round(CRON_ALERT_COOLDOWN_MIN / 60)} h.</p>`,
    });
    if (ok) await q(`INSERT INTO app_settings(key,value) VALUES('cron_stale_alerted',$1) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value`, [new Date().toISOString()]);
  } catch (e) { console.error('[checkStaleCrons]', e.message); }
}

app.all('/api/cron/auto-close', async (req, res) => {
  const secret = process.env.CRON_SECRET;
  const provided = (req.headers.authorization || '').replace('Bearer ', '') || req.query.key;
  if (secret && provided !== secret) return res.status(401).json({ error: 'unauthorized' });

  recordCron('auto-close');
  // Auto-close scrapped: tickets are NEVER force-closed. Resolved-ticket confirm/reopen nudges now
  // run on the 15-min escalation cron (so they can fire ~hourly). This daily job just keeps the
  // OneDrive Excel log in sync as a safety net.
  try { await require('../lib/excel').syncLogToOneDrive(); } catch (e) { console.error('cron log sync', e); }
  try { await require('../lib/canteen_excel').syncCanteenLog(); } catch (e) { console.error('cron canteen log sync', e); }
  res.json({ ok: true });
});

// ---- Ticket resolve: one-tap Confirm-close / Reopen from the requester's WhatsApp ----
// Token = the ticket's confirm_token (rotated on every resolve). No login needed.
async function loadTicketByToken(token) {
  return (await q(
    `SELECT t.*, c.name AS category_name, r.name AS requester_name
     FROM tickets t JOIN categories c ON c.id=t.category_id JOIN employees r ON r.id=t.requester_id
     WHERE t.confirm_token=$1`, [token])).rows[0];
}

app.get('/rc/:token', async (req, res) => {
  try {
    const t = await loadTicketByToken(req.params.token);
    if (!t) return res.status(404).send(actionPage('⛔', 'Link not valid', 'This link is not recognised, or a newer update has replaced it.'));
    if (t.status === 'closed') return res.send(actionPage('✅', 'Already closed', `Ticket ${t.ref_no} is already closed. Thank you!`));
    if (t.status !== 'resolved') return res.send(actionPage('ℹ️', 'Ticket is active', `Ticket ${t.ref_no} is currently "${t.status}", so there's nothing to confirm right now.`));
    await q(`UPDATE tickets SET status='closed', closed_at=now() WHERE id=$1`, [t.id]);
    await q(`INSERT INTO ticket_events(ticket_id,event,by_emp_id,note) VALUES($1,'confirmed_closed',$2,'Confirmed from WhatsApp')`, [t.id, t.requester_id]);
    require('../lib/bg').background(require('../lib/excel').syncLogToOneDrive());
    res.send(actionPage('✅', 'Confirmed & closed', `Thanks! Ticket ${t.ref_no} — "${t.subject}" is now closed.`));
  } catch (e) { console.error('rc', e); res.status(500).send(actionPage('⚠️', 'Something went wrong', 'Please try again, or open the app.')); }
});

app.get('/rr/:token', async (req, res) => {
  try {
    const t = await loadTicketByToken(req.params.token);
    if (!t) return res.status(404).send(actionPage('⛔', 'Link not valid', 'This link is not recognised, or a newer update has replaced it.'));
    if (['open', 'in_progress', 'reopened'].includes(t.status))
      return res.send(actionPage('↩️', 'Already reopened', `Ticket ${t.ref_no} is open again — the team is on it.`));
    if (!['resolved', 'closed'].includes(t.status))
      return res.send(actionPage('ℹ️', 'No action needed', `Ticket ${t.ref_no} is "${t.status}".`));
    await q(`UPDATE tickets SET status='reopened', resolved_at=NULL, closed_at=NULL WHERE id=$1`, [t.id]);
    await q(`INSERT INTO ticket_events(ticket_id,event,by_emp_id,note) VALUES($1,'reopened',$2,'Reopened from WhatsApp')`, [t.id, t.requester_id]);
    require('../lib/bg').background((async () => {
      const l1 = (await q('SELECT id,name,phone FROM employees WHERE id=$1', [t.l1_emp_id])).rows[0];
      if (l1 && l1.phone) await wati.notify.reopened(l1, { ...t, requester_name: t.requester_name });
      await require('../lib/excel').syncLogToOneDrive();
    })());
    res.send(actionPage('↩️', 'Reopened', `Ticket ${t.ref_no} — "${t.subject}" has been reopened and the handler notified.`));
  } catch (e) { console.error('rr', e); res.status(500).send(actionPage('⚠️', 'Something went wrong', 'Please try again, or open the app.')); }
});

// ---- Resolution attachments: tokenized download (no login), linked from the WhatsApp button ----
const _docMime = (name) => {
  const ext = (name || '').toLowerCase().split('.').pop();
  return ({ pdf: 'application/pdf', jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png', gif: 'image/gif',
    doc: 'application/msword', docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    xls: 'application/vnd.ms-excel', xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    txt: 'text/plain', csv: 'text/csv' })[ext] || 'application/octet-stream';
};
async function streamDoc(res, doc) {
  const buf = await require('../lib/graph').fetchDriveItemContent(doc.drive_item_id);
  if (!buf) return res.status(502).send('Could not fetch the file from storage.');
  res.setHeader('Content-Type', _docMime(doc.file_name));
  res.setHeader('Content-Disposition', `inline; filename="${(doc.file_name || 'document').replace(/"/g, '')}"`);
  res.send(buf);
}
app.get('/rd/:token', async (req, res) => {
  try {
    const t = (await q(`SELECT id, ref_no, subject FROM tickets WHERE confirm_token=$1`, [req.params.token])).rows[0];
    if (!t) return res.status(404).send(actionPage('⛔', 'Link not valid', 'This link is not recognised, or a newer update has replaced it.'));
    const docs = (await q(`SELECT id, file_name, drive_item_id FROM ticket_photos WHERE ticket_id=$1 AND kind='resolution' ORDER BY id`, [t.id])).rows;
    if (!docs.length) return res.send(actionPage('📄', 'No documents', `No documents are attached to ticket ${t.ref_no}.`));
    if (docs.length === 1) return streamDoc(res, docs[0]);
    const tok = encodeURIComponent(req.params.token);
    const items = docs.map((d) => `<a href="/rd/${tok}/file/${d.id}" style="display:block;margin:8px 0;padding:12px 16px;background:#1B7BC0;color:#fff;border-radius:10px;text-decoration:none;font-weight:600">⬇ ${String(d.file_name).replace(/</g, '&lt;')}</a>`).join('');
    return res.send(`<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><body style="font-family:system-ui,sans-serif;max-width:520px;margin:40px auto;padding:0 16px;color:#112532"><h2 style="margin:0 0 4px">Resolution documents</h2><div style="color:#8A97A3;font-size:14px;margin-bottom:16px">${t.ref_no} — ${String(t.subject).replace(/</g, '&lt;')}</div>${items}</body>`);
  } catch (e) { console.error('rd', e); res.status(500).send('error'); }
});
app.get('/rd/:token/file/:docId', async (req, res) => {
  try {
    const t = (await q(`SELECT id FROM tickets WHERE confirm_token=$1`, [req.params.token])).rows[0];
    if (!t) return res.status(404).send('Link not valid');
    const d = (await q(`SELECT file_name, drive_item_id FROM ticket_photos WHERE id=$1 AND ticket_id=$2 AND kind='resolution'`, [+req.params.docId, t.id])).rows[0];
    if (!d) return res.status(404).send('Not found');
    return streamDoc(res, d);
  } catch (e) { console.error('rd file', e); res.status(500).send('error'); }
});

// ---- SSO: hand a sibling app (e.g. QMS) a short-lived signed emp_no token ----
// The portal is the login authority; the other app trusts this token and starts its own session.
app.get('/sso/go', require('../lib/auth').requireAuth, (req, res) => {
  const targets = { qms: process.env.QMS_SSO_URL || 'https://qms.bharatsteels.in/auth/sso' };
  const base = targets[req.query.to];
  if (!base) return res.status(404).send('Unknown app');
  // Restricted apps require an explicit grant (admins always allowed).
  if (!require('../lib/apps').appAccessFor(req.user)[req.query.to])
    return res.status(403).send('You do not have access to this app. Contact IT if you need it.');
  const secret = process.env.SSO_SECRET;
  if (!secret) return res.status(500).send('SSO is not configured (set SSO_SECRET).');
  const token = require('jsonwebtoken').sign(
    { emp_no: req.user.emp_no, name: req.user.name }, secret,
    { expiresIn: '90s', issuer: 'bsc-portal' });
  res.redirect(`${base}?token=${encodeURIComponent(token)}`);
});

// ---- Cron: daily conveyance-trip nudge to reporting managers ----
// Runs once each weekday morning (GitHub Actions). Protected by CRON_SECRET. Any manager
// still sitting on pending trips gets a WhatsApp with the live one-tap approve links again
// (or a single digest if CV_NUDGE_MODE=digest). Responds immediately; sends in background.
app.all('/api/cron/trip-nudge', async (req, res) => {
  const secret = process.env.CRON_SECRET;
  const provided = (req.headers.authorization || '').replace('Bearer ', '') || req.query.key;
  if (secret && provided !== secret) return res.status(401).json({ error: 'unauthorized' });

  recordCron('trip-nudge');
  const { rows } = await q(`SELECT COUNT(*)::int AS n FROM conveyance_trips t
    JOIN employees m ON m.id = t.approver_emp_id
    WHERE t.status='pending' AND t.claim_ref IS NULL AND m.active = TRUE`);
  const pending = rows[0] ? rows[0].n : 0;
  res.json({ ok: true, pending, queued: pending > 0 });

  if (!pending) return;
  require('../lib/bg').background((async () => {
    try {
      const out = await require('../routes/expense.routes').nudgeManagers();
      console.log('[trip-nudge]', JSON.stringify(out));
    } catch (e) { console.error('[trip-nudge] failed:', e.message); }
  })());
});

// ---- Cron: working-hours reminder / escalation engine ----
// Runs every ~15 min (GitHub Actions). Protected by CRON_SECRET. Only sends
// during Mon–Sat 09:30–18:00 IST (minus holidays); timers count working minutes.
app.all('/api/cron/escalate', async (req, res) => {
  const secret = process.env.CRON_SECRET;
  const provided = (req.headers.authorization || '').replace('Bearer ', '') || req.query.key;
  if (secret && provided !== secret) return res.status(401).json({ error: 'unauthorized' });

  recordCron('escalate');
  require('../lib/bg').background(checkStaleCrons());
  const wati = require('../lib/wati');
  const { businessMinutesBetween, isWorkingNow, isWorkingClock, elapsedLabel } = require('../lib/util');

  // Clock check FIRST — no DB. Most runs (nights, Sundays) exit here in milliseconds. Hitting
  // Neon before this woke a suspended compute just to say "nothing to do", and the cold start
  // could exceed maxDuration and return 504 to the GitHub job.
  if (!isWorkingClock()) return res.json({ ok: true, skipped: 'outside working hours', sent: 0 });

  const holidaySet = new Set((await q(`SELECT to_char(d,'YYYY-MM-DD') AS d FROM holidays`)).rows.map(r => r.d));
  if (!isWorkingNow(holidaySet)) return res.json({ ok: true, skipped: 'holiday', sent: 0 });

  // Global admin kill switch — pause every ticket reminder (L1/L2/L3 + resolved nudges) at once.
  const remSwitch = (await q(`SELECT value FROM app_settings WHERE key='ticket_reminders_enabled'`)).rows[0];
  if (remSwitch && remSwitch.value === 'false') return res.json({ ok: true, skipped: 'reminders disabled by admin', sent: 0 });

  // Time budget: waitUntil() work still counts against the function's maxDuration, so we stop
  // sending before we hit it and let the next 15-min run pick up the rest (all sends are
  // idempotent — last_reminder_at guards them).
  const DEADLINE = Date.now() + Number(process.env.CRON_BUDGET_MS || 20000);
  const outOfTime = () => Date.now() > DEADLINE;

  // Resolve the CURRENT handler for each ticket (category/trade/location now), not the stale
  // snapshot taken at creation, and only ACTIVE employees — so replaced or deactivated handlers
  // stop getting nudged and the person who holds the role today is notified instead.
  // Also exclude anything already closed (closed_at) as a belt-and-suspenders on top of status.
  const { rows: tickets } = await q(
    `SELECT t.id, t.ref_no, t.subject, t.priority, t.status, t.escalation_level,
            t.is_self, t.raised_at, t.last_reminder_at,
            t.external_hold, t.external_set_at, t.external_hours,
            c.name AS category_name, c.wait_cycle_mins, c.wait_l3_mins, r.name AS requester_name,
            cl1.name AS l1_name, cl1.phone AS l1_phone,
            cl2.name AS l2_name, cl2.phone AS l2_phone,
            cl3.name AS l3_name, cl3.phone AS l3_phone,
            c.l3_emp_id AS cur_l3_id
     FROM tickets t
     JOIN categories c ON c.id = t.category_id
     JOIN employees r ON r.id = t.requester_id
     LEFT JOIN trades tr ON tr.id = t.trade_id
     LEFT JOIN trade_location_l1 tll ON tll.trade_id = t.trade_id AND tll.location_id = t.location_id
     LEFT JOIN employees cl1 ON cl1.id = (CASE WHEN t.is_self THEN t.l1_emp_id ELSE COALESCE(tll.l1_emp_id, tr.l1_emp_id, c.l1_emp_id) END) AND cl1.active = TRUE
     LEFT JOIN employees cl2 ON cl2.id = c.l2_emp_id AND cl2.active = TRUE
     LEFT JOIN employees cl3 ON cl3.id = c.l3_emp_id AND cl3.active = TRUE
     WHERE t.status IN ('open','in_progress','reopened') AND t.closed_at IS NULL AND NOT COALESCE(t.reminders_off, false)
       AND t.maint_gate IS DISTINCT FROM 'pending'`);

  // Uniform escalation for every category/trade:
  //   >= 2h unresolved -> remind L1 + L2      (level 2)
  //   >= 4h unresolved -> remind L1 + L2 + L3  (level 3), then re-nudge every cycle.
  // Per-category timing, falling back to global app_settings, then defaults.
  const S = (await q(`SELECT key,value FROM app_settings WHERE key IN ('remind_l2_mins','remind_l3_mins','remind_repeat_mins')`)).rows
    .reduce((m, r) => (m[r.key] = Number(r.value) || null, m), {});
  const REPEAT = S.remind_repeat_mins || 120;

  // Also nudge requesters to confirm-close/reopen resolved tickets — every ~1h (throttled), working hours only.
  const { rows: nudgeTix } = await q(
    `SELECT t.*, r.name AS requester_name, r.phone AS requester_phone,
       (SELECT e.name FROM ticket_events ev JOIN employees e ON e.id=ev.by_emp_id
        WHERE ev.ticket_id=t.id AND ev.event='resolved' ORDER BY ev.id DESC LIMIT 1) AS resolver_name
     FROM tickets t JOIN employees r ON r.id=t.requester_id
     WHERE t.status='resolved' AND t.resolved_at < now() - interval '1 hour'
       AND NOT COALESCE(t.reminders_off, false)
       AND (t.last_reminder_at IS NULL OR t.last_reminder_at < now() - interval '1 hour')`);
  // Respond immediately; do the WhatsApp-heavy work in the background so a slow or failing
  // send can never time out or fail the cron job (which is what surfaced as GitHub errors).
  res.json({ ok: true, scanned: tickets.length, to_nudge: nudgeTix.length, queued: true });
  require('../lib/bg').background((async () => {
  const now = Date.now();
  let fired = 0;
  for (const t of tickets) {
    if (outOfTime()) { console.warn('[escalate] budget spent — remaining tickets deferred to next run'); break; }
    try {
    // On external/vendor hold: stay quiet until the working-hours ETA lapses, then resume reminders.
    if (t.external_hold && t.external_set_at) {
      const held = businessMinutesBetween(t.external_set_at, now, holidaySet);
      if (held < (Number(t.external_hours) || 0) * 60) continue;
    }
    const L2_AFTER = t.wait_cycle_mins || S.remind_l2_mins || 120;
    const L3_AFTER = t.wait_l3_mins || S.remind_l3_mins || 240;
    const sinceRaised = businessMinutesBetween(t.raised_at, now, holidaySet);
    let newLevel = 0;
    if (sinceRaised >= L3_AFTER && t.cur_l3_id) newLevel = 3;
    else if (sinceRaised >= L2_AFTER) newLevel = 2;
    if (newLevel === 0) continue;

    const recipients = newLevel === 3
      ? [{ name: t.l1_name, phone: t.l1_phone }, { name: t.l2_name, phone: t.l2_phone }, { name: t.l3_name, phone: t.l3_phone }]
      : [{ name: t.l1_name, phone: t.l1_phone }, { name: t.l2_name, phone: t.l2_phone }];

    // Send if we just crossed into a higher level, or the repeat interval has elapsed.
    const leveledUp = newLevel > (t.escalation_level || 0);
    const dueByCadence = !t.last_reminder_at || businessMinutesBetween(t.last_reminder_at, now, holidaySet) >= REPEAT;
    if (!leveledUp && !dueByCadence) continue;

    fired++;
    const label = elapsedLabel(sinceRaised);
    const tObj = { id: t.id, ref_no: t.ref_no, requester_name: t.requester_name,
      category_name: t.category_name, subject: t.subject };
    const named = [];
    for (const h of recipients) { if (h.phone) { try { await wati.notify.reminder(h, tObj, label); } catch (e) { console.error('reminder send', t.id, e); } } if (h.name) named.push(h.name); }

    await q(`UPDATE tickets SET last_reminder_at=now(), escalation_level=$2,
             escalated_l2_at=CASE WHEN escalated_l2_at IS NULL THEN now() ELSE escalated_l2_at END,
             escalated_l3_at=CASE WHEN $2=3 AND escalated_l3_at IS NULL THEN now() ELSE escalated_l3_at END
             WHERE id=$1`, [t.id, newLevel]);
    await q(`INSERT INTO ticket_events(ticket_id,event,note) VALUES($1,'reminder',$2)`,
      [t.id, `${label} · notified ${named.join(', ')}`]);
    } catch (e) { console.error('escalate ticket', t.id, e); }
  }
  // Resolved-ticket confirm/reopen nudges (re-sent ~hourly until the requester acts).
  for (const t of nudgeTix) {
    if (outOfTime()) { console.warn('[escalate] budget spent — remaining nudges deferred to next run'); break; }
    try {
      if (t.requester_phone && t.confirm_token) {
        await wati.notify.resolved({ id: t.requester_id, name: t.requester_name, phone: t.requester_phone },
          t, t.resolver_name || 'the team', t.resolution_note);
      }
      await q(`UPDATE tickets SET last_reminder_at=now() WHERE id=$1`, [t.id]);
    } catch (e) { console.error('resolve-nudge', t.id, e); }
  }
  })());
});

// ---- Daily report PDF (token-guarded so the WhatsApp link opens without login) ----
app.get('/api/report/daily.pdf', async (req, res) => {
  const token = process.env.REPORT_TOKEN;
  if (!token || req.query.key !== token) return res.status(401).send('unauthorized');
  const today = new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });
  let date = today, scope = null, subtitle = null;
  try {
    if (req.query.q) {
      // scoped per-employee link: q = "<YYYY-MM-DD>_<employeeId>"
      const [qd, empId] = String(req.query.q).split('_');
      if (/^\d{4}-\d{2}-\d{2}$/.test(qd)) date = qd;
      if (empId) {
        const s = (await q(`SELECT s.category_ids, s.trade_ids, e.name FROM report_subscriptions s JOIN employees e ON e.id=s.employee_id WHERE s.employee_id=$1`, [+empId])).rows[0];
        if (s) { scope = { categoryIds: s.category_ids, tradeIds: s.trade_ids }; subtitle = s.name; }
      }
    } else if (/^\d{4}-\d{2}-\d{2}$/.test(req.query.date || '')) {
      date = req.query.date;
    }
    const { pdf } = await require('../lib/report').dailyReportPdf(date, scope, subtitle, { remark: !!scope });
    res.setHeader('Content-Type', 'application/pdf');
    res.setHeader('Content-Disposition', `inline; filename="daily-report-${date}.pdf"`);
    res.send(pdf);
  } catch (e) { console.error('report pdf', e); res.status(500).send('report error'); }
});

// ---- Cron: overdue-gatepass watcher ----
// Runs every ~5 min. Clock-gated (broad watch window, no DB) so night/Sunday runs skip before
// waking Neon. For each gatepass past its expected return + threshold with no return yet, it
// WhatsApps the approver + HR once (overdue_alert_at dedupes). Idempotent — safe to re-run.
app.all('/api/cron/outpass-overdue', async (req, res) => {
  const secret = process.env.CRON_SECRET;
  const provided = (req.headers.authorization || '').replace('Bearer ', '') || req.query.key;
  if (secret && provided !== secret) return res.status(401).json({ error: 'unauthorized' });

  recordCron('outpass-overdue');
  require('../lib/bg').background(checkStaleCrons());
  const op = require('../routes/outpass.routes')._internal;
  // Heal legacy gatepasses that have no expected-return time, so the watcher can see them.
  const backfill = await op.backfillExpectedBack();
  const { isOutpassWatchClock } = require('../lib/util');
  const startMin = Number(process.env.OUTPASS_WATCH_START_MIN || 8 * 60);
  const endMin = Number(process.env.OUTPASS_WATCH_END_MIN || 20 * 60);
  if (!isOutpassWatchClock(startMin, endMin)) return res.json({ ok: true, skipped: 'outside watch window', backfill });

  const cfg = await op.gateConfig();
  const overdue = await op.findOverdue(cfg.overdueMin, cfg.multidayHours);
  // Resolve HR up-front so we can report it (this is the usual reason HR doesn't get the alert).
  let hr = null;
  if (cfg.hrEmpId) hr = (await q(`SELECT name, phone FROM employees WHERE id=$1 AND active=TRUE`, [cfg.hrEmpId])).rows[0] || null;
  // Diagnostic breakdown — tells you exactly why overdue may be 0 (the usual real reasons).
  const diag = (await q(
    `SELECT
       COUNT(*) FILTER (WHERE type='gatepass' AND status='approved' AND returned_at IS NULL) AS open_gatepasses,
       COUNT(*) FILTER (WHERE type='gatepass' AND status='approved' AND returned_at IS NULL AND expected_back_at IS NULL) AS gp_no_expected_time,
       COUNT(*) FILTER (WHERE type='gatepass' AND status='approved' AND returned_at IS NULL AND expected_back_at > now()) AS gp_not_due_yet,
       COUNT(*) FILTER (WHERE type='gatepass' AND status='approved' AND returned_at IS NULL AND expected_back_at < (now() - ($1||' hours')::interval)) AS gp_likely_multiday,
       COUNT(*) FILTER (WHERE type='gatepass' AND status='approved' AND returned_at IS NULL AND expected_back_at < now() AND hr_alert_at IS NOT NULL) AS gp_hr_already_alerted,
       COUNT(*) FILTER (WHERE type='outpass'  AND status='approved' AND returned_at IS NULL) AS open_outpasses_not_watched
     FROM outpass_requests`, [String(cfg.multidayHours)])).rows[0];
  res.json({
    ok: true, overdue: overdue.length,
    hr_emp_id: cfg.hrEmpId || null,          // null = "HR (also receives overdue alerts)" not set in Gate settings
    hr_resolved: !!hr,                        // false = that person is missing/inactive
    hr_has_phone: !!(hr && hr.phone),         // false = HR has no phone on their record
    watch_window: '08:00-20:00 IST (Mon-Sat)',
    backfill,                                 // legacy gatepasses healed this run
    diagnostics: diag,                        // where the open passes actually sit
  });
  if (!overdue.length) return;

  require('../lib/bg').background((async () => {
    const wati = require('../lib/wati');
    const { fmtDateTime } = { fmtDateTime: (d) => new Date(d).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit' }) };
    const DEADLINE = Date.now() + Number(process.env.CRON_BUDGET_MS || 20000);
    for (const o of overdue) {
      if (Date.now() > DEADLINE) { console.warn('[outpass-overdue] budget spent, rest deferred'); break; }
      const mins = Math.floor((Date.now() - +new Date(o.expected_back_at)) / 60000);
      const token = o.return_token || await op.ensureReturnToken(o.id);
      const payload = { employee: o.req_name, ref: o.ref_no, out_time: o.out_time,
        expected: o.in_time, overdue_min: Math.max(0, mins), purpose: o.purpose,
        duty: o.on_duty ? 'On duty (official)' : 'Personal', token };
      try {
        // Requester: nudge right at their expected return time (once) — so someone already
        // back but who forgot to log can tap "I'm Back" straight away.
        if (!o.requester_reminder_at) {
          if (o.req_phone) {
            const ok = await wati.notify.outpass.returnReminder({ name: o.req_name, phone: o.req_phone }, payload);
            if (ok) await op.markRequesterReminded(o.id);
          } else {
            await op.markRequesterReminded(o.id);   // no phone on record — nothing to retry
          }
        }
        // HR + approver: escalate only once genuinely overdue past the threshold.
        if (mins >= cfg.overdueMin) {
          // Approver: alert once. Mark done only on real delivery (or if there's no phone to try).
          if (!o.overdue_alert_at) {
            const ok = o.approver_phone
              ? await wati.notify.outpass.overdue({ name: o.approver_name, phone: o.approver_phone }, payload)
              : false;
            if (ok || !o.approver_phone) await op.markApproverAlerted(o.id);
          }
          // HR: retry every run until WATI actually delivers.
          if (!o.hr_alert_at) {
            if (hr && hr.phone) {
              const ok = await wati.notify.outpass.overdue({ name: hr.name, phone: hr.phone }, payload);
              if (ok) await op.markHrAlerted(o.id);
            } else {
              await op.markHrAlerted(o.id);   // no HR configured — nothing to retry
            }
          }
        }
      } catch (e) { console.error('[outpass-overdue] alert failed for', o.ref_no, e.message); }
    }
  })());
});

// ---- Cron: 6:30pm IST daily report — overall recipients + per-person subscribers ----
app.all('/api/cron/monthly-accounts', async (req, res) => {
  const secret = process.env.CRON_SECRET;
  const provided = (req.headers.authorization || '').replace('Bearer ', '') || req.query.key;
  if (secret && provided !== secret) return res.status(401).json({ error: 'unauthorized' });
  recordCron('monthly-accounts');
  // Only act on the 5th (IST) unless a month is passed explicitly (so a daily pinger is safe).
  const todayIST = new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });
  const dom = Number(todayIST.slice(8, 10));
  const fin = require('../routes/final.routes')._internal;
  const month = /^\d{4}-\d{2}$/.test(req.query.month || '') ? req.query.month : fin.prevMonth();
  if (!req.query.month && dom !== 5) return res.json({ ok: true, skipped: `not the 5th (today ${todayIST})` });
  try { res.json(await fin.runMonthlyAccounts(month)); }
  catch (e) { console.error('[cron monthly-accounts]', e); res.status(500).json({ error: e.message }); }
});

app.all('/api/cron/daily-report', async (req, res) => {
  const secret = process.env.CRON_SECRET;
  const provided = (req.headers.authorization || '').replace('Bearer ', '') || req.query.key;
  if (secret && provided !== secret) return res.status(401).json({ error: 'unauthorized' });

  recordCron('daily-report');  const date = /^\d{4}-\d{2}-\d{2}$/.test(req.query.date || '')
    ? req.query.date : new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });
  try {
    const r = await require('../lib/report').dispatchDailyReports(date);
    res.json({ ok: true, ...r });
  } catch (e) { console.error('cron daily-report', e); res.status(500).json({ error: 'dispatch failed' }); }
});

app.use((err, req, res, next) => {
  console.error('API error:', err);
  res.status(500).json({ error: 'Server error' });
});

module.exports = app;
