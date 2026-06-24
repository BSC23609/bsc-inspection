// Monthly dispatch + complaints report engine
// Recipient = the Ship-To client. Bill-to "Customer" (U_VSPCN) is filtered to Ashok Leyland for now.
const PDFDocument = require('pdfkit');
const fetch = require('node-fetch');
const fs = require('fs');
const path = require('path');
const { q } = require('./db');

let LOGO = null;
try { LOGO = fs.readFileSync(path.join(__dirname, 'public', 'bsc-logo.png')); } catch (e) { console.log('[reports] logo not loaded'); }

const CSV_URL  = process.env.WEIGHBRIDGE_CSV_URL || 'https://raw.githubusercontent.com/BSC23609/weighbridge-data/main/data.csv';
const GH_TOKEN = process.env.WEIGHBRIDGE_TOKEN || '';
const BILLTO   = (process.env.REPORT_BILLTO_CODE === undefined) ? '7206270' : process.env.REPORT_BILLTO_CODE; // '' = all bill-tos

const BLUE = '#0F6CB6', DARK = '#101828', MUTED = '#667085', LINE = '#E5E9F0', ZEBRA = '#F6F8FB';

function parseCSV(text){
  text = text.replace(/^\uFEFF/, '');
  const rows = []; let field = '', row = [], inQ = false;
  for (let i = 0; i < text.length; i++){
    const c = text[i];
    if (inQ){ if (c === '"'){ if (text[i+1] === '"'){ field += '"'; i++; } else inQ = false; } else field += c; }
    else { if (c === '"') inQ = true; else if (c === ','){ row.push(field); field = ''; } else if (c === '\n'){ row.push(field); rows.push(row); row = []; field = ''; } else if (c === '\r'){} else field += c; }
  }
  if (field.length || row.length){ row.push(field); rows.push(row); }
  return rows;
}
function rowsToObjects(rows){
  if (!rows.length) return [];
  const head = rows[0].map(h => h.trim());
  return rows.slice(1).filter(r => r.length > 1 && r.some(x => x && x.trim()))
    .map(r => { const o = {}; head.forEach((h, i) => o[h] = (r[i] || '').trim()); return o; });
}
function codeFrom(s){ const m = String(s || '').match(/\((\d+)\)\s*$/); return m ? m[1] : null; }
function nameOnly(s){ return String(s || '').replace(/\s*\(\d+\)\s*$/, '').trim(); }
function norm(s){
  return String(s || '').toUpperCase().replace(/\(.*?\)/g, '')
    .replace(/\b(PRIVATE|PVT|LIMITED|LTD|AUTOMOTIVE|INDUSTRIES|INDUSTRY|ENGINEERING|ENGINEERS|COMPONENTS|ANCILLARIES|PRODUCTS|INDIA|P)\b/g, '')
    .replace(/[&.,\-]/g, ' ').replace(/\s+/g, ' ').trim();
}
function inMonth(dateStr, ym){
  const s = String(dateStr || '').trim(); let m;
  if ((m = s.match(/^(\d{4})-(\d{2})-(\d{2})/))) return (m[1] + '-' + m[2]) === ym;
  if ((m = s.match(/^(\d{2})[-\/](\d{2})[-\/](\d{2,4})/))) { let y = m[3].length === 2 ? '20' + m[3] : m[3]; return (y + '-' + m[2]) === ym; }
  if ((m = s.match(/^(\d{1,2})[-\/]([A-Za-z]{3})[-\/](\d{2,4})/))) {
    const M = {jan:'01',feb:'02',mar:'03',apr:'04',may:'05',jun:'06',jul:'07',aug:'08',sep:'09',oct:'10',nov:'11',dec:'12'}[m[2].toLowerCase()];
    let y = m[3].length === 2 ? '20' + m[3] : m[3]; return M && (y + '-' + M) === ym;
  }
  return false;
}
function dispDate(s){ const t = String(s || '').trim(); let m; if ((m = t.match(/^(\d{4})-(\d{2})-(\d{2})/))) return m[3] + '-' + m[2] + '-' + m[1]; return t; }
function num(v){ const n = parseFloat(String(v || '').replace(/,/g, '')); return isFinite(n) ? n : 0; }
function fmtHHMM(v){ let s = String(v || '').trim(); if (!s || s === '0') return '\u2014'; if (s.includes(':')) return s; if (!/^\d{1,4}$/.test(s)) return s; s = s.padStart(4, '0'); return s.slice(0, 2) + ':' + s.slice(2); }
function mins(v){ const s = String(v || '').replace(/\s*min\.?/i, '').trim(); return s || '\u2014'; }
function toMin(v){ let s = String(v || '').trim(); if (!s) return null; s = s.replace(':', ''); if (!/^\d{1,4}$/.test(s)) return null; s = s.padStart(4, '0'); const hh = +s.slice(0, 2), mm = +s.slice(2); if (hh > 23 || mm > 59) return null; return hh * 60 + mm; }

async function fetchRows(){
  const headers = { 'User-Agent': 'bsc-qms' };
  if (GH_TOKEN) headers['Authorization'] = 'token ' + GH_TOKEN;
  const resp = await fetch(CSV_URL + (CSV_URL.includes('?') ? '&' : '?') + 't=' + Date.now(), { headers });
  if (!resp.ok) throw new Error('CSV fetch ' + resp.status + ' from ' + CSV_URL);
  return rowsToObjects(parseCSV(await resp.text()));
}
function shipToMatches(r, code, company){
  const sc = codeFrom(r['Ship To Name']);
  if (sc) return sc === String(code);
  if (!company) return false;
  const a = norm(company), b = norm(r['Ship To Name']);
  return a && b && (b.indexOf(a) >= 0 || a.indexOf(b) >= 0);
}
async function dispatchForShipTo(code, company, ym){
  const all = await fetchRows();
  return all.filter(r => inMonth(r['CreateDate'], ym) && (!BILLTO || codeFrom(r['U_VSPCN']) === BILLTO) && shipToMatches(r, code, company));
}

function monthLabel(ym){ const [y, m] = ym.split('-').map(Number); return new Date(y, m - 1, 1).toLocaleString('en-IN', { month: 'long', year: 'numeric' }); }
function clip(doc, s, w){ s = String(s == null ? '' : s); while (s.length && doc.widthOfString(s) > w) s = s.slice(0, -1); return s; }

function buildPDF({ billToName, shipToName, code, ym, rows, complaints }){
  return new Promise((resolve, reject) => {
    try {
      const doc = new PDFDocument({ size: 'A4', layout: 'landscape', margin: 36, bufferPages: true });
      const buffers = []; doc.on('data', b => buffers.push(b)); doc.on('end', () => resolve(Buffer.concat(buffers))); doc.on('error', reject);
      const L = 36, R = 806, W = R - L;

      if (LOGO) { try { doc.image(LOGO, L, 30, { width: 100 }); } catch (e) {} }
      doc.fillColor(BLUE).font('Helvetica-Bold').fontSize(15).text('MONTHLY DISPATCH REPORT', L, 36, { width: W, align: 'right' });
      doc.fillColor(MUTED).font('Helvetica').fontSize(9).text('Bharat Steel (Chennai) Pvt. Ltd.   \u00b7   ' + monthLabel(ym), L, 56, { width: W, align: 'right' });
      doc.moveTo(L, 74).lineTo(R, 74).lineWidth(2).strokeColor(BLUE).stroke();

      let y = 84;
      doc.fillColor(DARK).font('Helvetica-Bold').fontSize(13).text(shipToName, L, y, { width: W }); y += 17;
      doc.fillColor(MUTED).font('Helvetica').fontSize(9.5).text('Ship-To code: ' + code + '       Customer (bill-to): ' + billToName, L, y); y += 20;

      const trips = rows.length;
      const completed = rows.filter(r => /complet/i.test(r['U_VSPLWBST'] || '')).length;
      const netWt = rows.reduce((s, r) => s + num(r['Net Weight']), 0);
      const tats = [];
      rows.forEach(r => {
        if (!/complet/i.test(r['U_VSPLWBST'] || '')) return;
        const a = toMin(r['In Time']), b = toMin(r['Invoice Time']);
        if (a == null || b == null) return;
        let d = b - a; if (d < 0) d += 1440; // invoice billed past midnight
        if (d >= 0 && d < 1440) tats.push(d);
      });
      const avgTAT = tats.length ? tats.reduce((s, v) => s + v, 0) / tats.length : null;
      const stats = [
        ['Total trips', String(trips), false], ['Completed', String(completed), false],
        ['Pending', String(trips - completed), false], ['Net weight (T)', netWt.toFixed(2), false],
        ['Avg TAT (min)', avgTAT == null ? '\u2014' : String(Math.round(avgTAT)), true]
      ];
      const bw = W / stats.length;
      stats.forEach((s, i) => {
        const x = L + i * bw;
        if (s[2]) {
          doc.roundedRect(x + 3, y, bw - 6, 44, 6).fill(BLUE);
          doc.fillColor('#CDE3F7').font('Helvetica-Bold').fontSize(7.5).text(s[0].toUpperCase(), x + 10, y + 9, { width: bw - 20 });
          doc.fillColor('#fff').font('Helvetica-Bold').fontSize(15).text(s[1], x + 10, y + 21, { width: bw - 20 });
        } else {
          doc.roundedRect(x + 3, y, bw - 6, 44, 6).lineWidth(1).strokeColor(LINE).stroke();
          doc.fillColor(MUTED).font('Helvetica').fontSize(7.5).text(s[0].toUpperCase(), x + 10, y + 9, { width: bw - 20 });
          doc.fillColor(BLUE).font('Helvetica-Bold').fontSize(15).text(s[1], x + 10, y + 21, { width: bw - 20 });
        }
      });
      y += 60;

      doc.fillColor(DARK).font('Helvetica-Bold').fontSize(10).text('DISPATCH DETAILS', L, y); y += 15;
      const cols = [
        ['Date', 84], ['Status', 80], ['Vehicle', 110], ['In', 60], ['Out', 60],
        ['Inv Time', 72], ['Invoice #', 94], ['Net Wt', 70], ['W-Time', 70], ['B-Time', 70]
      ];
      function header(){ doc.rect(L, y, W, 17).fill(BLUE); doc.fillColor('#fff').font('Helvetica-Bold').fontSize(7.5); let x = L; cols.forEach(c => { doc.text(c[0], x + 4, y + 5, { width: c[1] - 8, align: 'center' }); x += c[1]; }); y += 17; }
      header();
      rows.sort((a, b) => String(a['CreateDate']).localeCompare(String(b['CreateDate'])));
      rows.forEach((r, idx) => {
        if (y > 540){ doc.addPage(); y = 36; header(); }
        if (idx % 2) doc.rect(L, y, W, 14).fill(ZEBRA);
        const vals = [dispDate(r['CreateDate']), r['U_VSPLWBST'], r['U_VSPV'], fmtHHMM(r['In Time']), fmtHHMM(r['Out Time']),
          r['Invoice Time'] || '\u2014', r['Invoice No'] || '\u2014',
          num(r['Net Weight']) ? num(r['Net Weight']).toFixed(2) : '\u2014', r['Time Taken'] || '\u2014', mins(r['OutTime_To_InvoiceTime'])];
        let x = L; doc.font('Helvetica').fontSize(7.5);
        cols.forEach((c, i) => {
          if (i === 1){ doc.fillColor(/complet/i.test(vals[1] || '') ? '#067647' : '#B54708'); } else doc.fillColor(DARK);
          doc.text(clip(doc, vals[i], c[1] - 8), x + 4, y + 4, { width: c[1] - 8, align: 'center' });
          x += c[1];
        });
        doc.fillColor(DARK); y += 14;
      });
      if (!rows.length){ doc.fillColor(MUTED).font('Helvetica-Oblique').fontSize(9).text('No dispatches recorded for this period.', L, y + 6); y += 22; }

      y += 6;
      doc.fillColor(MUTED).font('Helvetica-Oblique').fontSize(7).text(
        'W-Time = weighbridge turnaround (In \u2192 Out)   \u00b7   B-Time = billing time (Out \u2192 Invoice)   \u00b7   TAT = total turnaround (In \u2192 Invoice).  All durations in minutes.',
        L, y, { width: W });
      y += 12;

      y += 16; if (y > 500){ doc.addPage(); y = 36; }
      doc.fillColor(DARK).font('Helvetica-Bold').fontSize(10).text('COMPLAINTS THIS PERIOD', L, y); y += 15;
      if (complaints && complaints.length){
        const cc = [['Ref', 90], ['Raised', 80], ['Grade', 80], ['Status', 110], ['Resolution', W - 360]];
        doc.rect(L, y, W, 17).fill(BLUE); doc.fillColor('#fff').font('Helvetica-Bold').fontSize(7.5);
        let x = L; cc.forEach(c => { doc.text(c[0], x + 4, y + 5, { width: c[1] - 8 }); x += c[1]; }); y += 17;
        complaints.forEach((c, idx) => {
          if (y > 540){ doc.addPage(); y = 36; }
          if (idx % 2) doc.rect(L, y, W, 14).fill(ZEBRA);
          const v = [c.ref || '', new Date(c.created_at).toLocaleDateString('en-IN'), c.grade || '-',
            ({ submitted:'Under review', in_review:'In progress', resolution_sent:'Resolution sent', declined:'Declined', closed:'Closed' }[c.status] || c.status),
            c.resolution_note || c.decision_note || '-'];
          let x2 = L; doc.fillColor(DARK).font('Helvetica').fontSize(7.5);
          cc.forEach((col, i) => { doc.text(clip(doc, v[i], col[1] - 8), x2 + 4, y + 4, { width: col[1] - 8 }); x2 += col[1]; });
          y += 14;
        });
      } else { doc.fillColor(MUTED).font('Helvetica-Oblique').fontSize(9).text('No complaints were logged in this period.', L, y); }

      const range = doc.bufferedPageRange();
      for (let i = 0; i < range.count; i++){
        doc.switchToPage(range.start + i);
        doc.fillColor(MUTED).font('Helvetica').fontSize(7.5)
           .text('Generated ' + new Date().toLocaleString('en-IN') + '   \u00b7   Bharat Steel (Chennai)', L, 547, { width: W, lineBreak: false })
           .text('Page ' + (i + 1) + ' of ' + range.count, L, 547, { width: W, align: 'right', lineBreak: false });
      }
      doc.end();
    } catch (e) { reject(e); }
  });
}

async function buildMonthlyReport({ code, ym }){
  let acct = null;
  try { const r = await q('SELECT * FROM customers WHERE code=$1', [String(code)]); acct = r.rows[0] || null; } catch (e) {}
  const company = acct && acct.company;
  const rows = await dispatchForShipTo(code, company, ym);
  let complaints = [];
  if (acct){ try { const cr = await q("SELECT * FROM customer_complaints WHERE customer_id=$1 AND to_char(created_at,'YYYY-MM')=$2 ORDER BY created_at", [acct.id, ym]); complaints = cr.rows; } catch (e) {} }
  const shipToName = company || (rows[0] && nameOnly(rows[0]['Ship To Name'])) || ('Customer ' + code);
  const billToName = (rows[0] && nameOnly(rows[0]['U_VSPCN'])) || 'ASHOK LEYLAND LIMITED';
  const pdf = await buildPDF({ billToName, shipToName, code, ym, rows, complaints });
  return { pdf, shipToName, email: acct && acct.email, trips: rows.length, complaints: complaints.length };
}

module.exports = { buildMonthlyReport, dispatchForShipTo, fetchRows, codeFrom, nameOnly, inMonth };
