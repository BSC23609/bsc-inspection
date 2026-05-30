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

function generatePDF(folder, data, ref) {
  return new Promise((resolve, reject) => {
    try {
      const doc = new PDFDocument({ size: 'A4', margin: 40 });
      const buffers = [];
      doc.on('data', buffers.push.bind(buffers));
      doc.on('end', () => resolve(Buffer.concat(buffers)));
      doc.on('error', reject);
      const W = doc.page.width - 80;
      let y = 40;
      const ts = new Date().toLocaleString('en-IN');

      doc.fillColor(BLUE).font('Helvetica-Bold').fontSize(16);
      doc.text('BHARAT STEEL (CHENNAI) PVT. LTD.', 40, y);
      doc.fillColor('#666').font('Helvetica').fontSize(9);
      const subtitle = folder === 'Inward' ? 'Coil Inward Inspection Report'
                     : folder === 'Shearing' ? 'Shearing Quality Inspection Report · BSCQMS-PRD-008 REV 01'
                     : 'CTL Quality Inspection Report · BSCQMS-PRD-008 REV 03';
      doc.text(subtitle, 40, y + 22);
      doc.fontSize(9).fillColor('#666');
      doc.text('Ref: ' + ref, 40, y, { width: W, align: 'right' });
      doc.text('Date: ' + ts, 40, y + 14, { width: W, align: 'right' });
      y += 42;
      doc.moveTo(40, y).lineTo(40 + W, y).strokeColor(BLUE).lineWidth(2).stroke();
      y += 12;

      const sectionHeader = (title) => {
        if (y > doc.page.height - 80) { doc.addPage(); y = 40; }
        doc.rect(40, y, W, 22).fill(BLUE);
        doc.fillColor('#fff').font('Helvetica-Bold').fontSize(11);
        doc.text(title, 48, y + 6);
        y += 28;
      };
      const row2 = (l1, v1, l2, v2) => {
        if (y > doc.page.height - 60) { doc.addPage(); y = 40; }
        const colW = W / 2, labelW = 110;
        doc.rect(40, y, labelW, 22).fillAndStroke(GRAY_BG, GRAY_BORDER);
        doc.rect(40 + labelW, y, colW - labelW, 22).strokeColor(GRAY_BORDER).stroke();
        doc.rect(40 + colW, y, labelW, 22).fillAndStroke(GRAY_BG, GRAY_BORDER);
        doc.rect(40 + colW + labelW, y, colW - labelW, 22).strokeColor(GRAY_BORDER).stroke();
        doc.fillColor(TEXT_DARK).font('Helvetica-Bold').fontSize(9);
        doc.text(l1, 44, y + 7, { width: labelW - 8 });
        doc.text(l2, 44 + colW, y + 7, { width: labelW - 8 });
        doc.font('Helvetica').fillColor('#111');
        doc.text(String(v1 || '-'), 44 + labelW, y + 7, { width: colW - labelW - 8 });
        doc.text(String(v2 || '-'), 44 + colW + labelW, y + 7, { width: colW - labelW - 8 });
        y += 22;
      };
      const rowFull = (label, value) => {
        if (y > doc.page.height - 60) { doc.addPage(); y = 40; }
        const labelW = 110;
        doc.rect(40, y, labelW, 22).fillAndStroke(GRAY_BG, GRAY_BORDER);
        doc.rect(40 + labelW, y, W - labelW, 22).strokeColor(GRAY_BORDER).stroke();
        doc.fillColor(TEXT_DARK).font('Helvetica-Bold').fontSize(9);
        doc.text(label, 44, y + 7, { width: labelW - 8 });
        doc.font('Helvetica').fillColor('#111');
        doc.text(String(value || '-'), 44 + labelW, y + 7, { width: W - labelW - 8 });
        y += 22;
      };
      const rowBlock = (label, value) => {
        if (y > doc.page.height - 80) { doc.addPage(); y = 40; }
        const valStr = String(value || '-');
        const lines = Math.max(1, Math.ceil(valStr.length / 90));
        const h = Math.max(22, lines * 14 + 8);
        doc.rect(40, y, W, h).strokeColor(GRAY_BORDER).stroke();
        doc.fillColor(TEXT_DARK).font('Helvetica-Bold').fontSize(9);
        doc.text(label, 44, y + 7);
        doc.font('Helvetica').fillColor('#111').fontSize(9);
        doc.text(valStr, 44, y + 20, { width: W - 8 });
        y += h;
      };

      if (folder === 'Inward') {
        sectionHeader('Vehicle & Coil Identity');
        row2('Vehicle Number', data.vehicle_number, 'Batch Number', data.batch_number);
        row2('Make of Coil', data.make_of_coil, 'Grade', data.grade);
        y += 8;
        sectionHeader('Dimensions');
        row2('Width (mm)', data.width, 'Thickness (mm)', data.thickness);
        row2('Coil Weight (T)', data.coil_weight, 'Coil ID (mm)', data.coil_id);
        row2('Actual Thickness', data.actual_thickness, 'Actual Width', data.actual_width);
        y += 8;
        sectionHeader('Physical Condition');
        row2('ID Sticker', data.id_sticker, 'Edge Damage - Inner', data.edge_inner);
        row2('Edge Damage - Outer', data.edge_outer, 'Scratch Mark', data.scratch);
        row2('Strapping', data.strapping, 'Rust on Surface', data.rust);
        rowFull('Other Damages', data.other_damages);
        y += 8;
        sectionHeader('Inspector');
        row2('Inspected By', data.inspected_by, 'Remarks', data.remarks);
        if (data.wheels_india) { y += 4; rowFull('Wheels India Coil', 'YES'); }
      } else if (folder === 'Shearing') {
        sectionHeader('Header Information');
        rowFull('Customer Name', data.customer_name);
        row2('Batch Number', data.batch_number, 'Grade', data.grade);
        row2('Make', data.make, 'Type', data.type);
        row2('Process', data.process, 'Input Size', data.input_size);
        row2('Operator Name', data.operator, 'QC Name', data.qc_name);
        y += 8;
        sectionHeader('Sheet Measurements');
        const rows = (data.measurements || []).filter(r => r.sheet_no || r.width1 || r.width2);
        const colWidths = [W*0.10, W*0.15, W*0.15, W*0.15, W*0.15, W*0.30];
        const headers = ['Sheet No.', 'Width 1', 'Width 2', 'Diag 1', 'Diag 2', 'Remarks'];
        doc.rect(40, y, W, 18).fill('#1F2937');
        doc.fillColor('#fff').font('Helvetica-Bold').fontSize(8);
        let x = 40;
        headers.forEach((h, i) => { doc.text(h, x + 4, y + 5, { width: colWidths[i] - 8 }); x += colWidths[i]; });
        y += 18;
        doc.font('Helvetica').fontSize(8).fillColor('#111');
        if (rows.length === 0) {
          doc.rect(40, y, W, 18).strokeColor(GRAY_BORDER).stroke();
          doc.fillColor('#9CA3AF').text('No measurements entered', 40, y + 5, { width: W, align: 'center' });
          y += 18;
        } else {
          rows.forEach(r => {
            if (y > doc.page.height - 60) { doc.addPage(); y = 40; }
            doc.rect(40, y, W, 18).strokeColor(GRAY_BORDER).stroke();
            doc.fillColor('#111');
            x = 40;
            [r.sheet_no, r.width1, r.width2, r.diag1, r.diag2, r.remarks].forEach((v, i) => {
              doc.text(String(v || ''), x + 4, y + 5, { width: colWidths[i] - 8 });
              x += colWidths[i];
            });
            y += 18;
          });
        }
        y += 8;
        sectionHeader('Quality Checklist');
        row2('Burr -/< 10%', data.burr, 'Blade Clearance', data.blade_clearance);
        row2('Cutting Finish', data.cutting_finish, 'Surface Condition', data.surface_condition);
        row2('Bow / Bend', data.bow_bend, 'QC Signature Date', data.sig_date);
        y += 8;
        sectionHeader('Overall Observation');
        rowBlock('Observation', data.overall_observation);
      } else {
        sectionHeader('Customer & Coil Info');
        rowFull('Customer Name', data.customer_name);
        row2('Date', data.date, 'Time', data.time);
        row2('Coil Number', data.coil_number, 'Batch Number', data.batch_number);
        row2('Make', data.make, 'Coil Thickness', data.coil_thickness);
        row2('Grade', data.coil_grade, 'Width', data.coil_width);
        rowFull('Coil Weight (T)', data.coil_weight);
        y += 8;
        sectionHeader('Processing Info');
        row2('First Bit', data.first_bit, 'Last Bit', data.last_bit);
        row2('Defective Bit', data.defective, 'Balance Weight', data.balance_wt);
        row2('Coil Verified', data.coil_verified, 'Blade Clearance', data.blade_clearance);
        row2('Operator', data.operator, 'Machine Name', data.machine_name);
        row2('Inspector', data.inspector, 'Remarks', data.remarks);
        y += 8;
        sectionHeader('Quality Checklist');
        row2('Burr', data.bur, 'Cutting Finish', data.cutting_finish);
        row2('Scalling', data.scalling, 'Pit Marks', data.pit_marks);
        row2('Waviness', data.waviness, 'Center Bow', data.center_bow);
        row2('Cutting Bow', data.cutting_bow, 'Surface Defects', data.surface_defects);
        y += 8;
        sectionHeader('Processed Quantity');
        const pq = data.processed_qty || {};
        const sizeHeaders = ['Size #', 'Length', 'Nos', 'Weight (T)'];
        const sizeCols = [W*0.15, W*0.30, W*0.25, W*0.30];
        doc.rect(40, y, W, 18).fill('#1F2937');
        doc.fillColor('#fff').font('Helvetica-Bold').fontSize(8);
        let xs = 40;
        sizeHeaders.forEach((h, i) => { doc.text(h, xs + 4, y + 5, { width: sizeCols[i] - 8 }); xs += sizeCols[i]; });
        y += 18;
        doc.font('Helvetica').fontSize(8).fillColor('#111');
        for (let i = 1; i <= 10; i++) {
          const s = pq['size_' + i] || {};
          if (!s.length && !s.nos && !s.weight_t) continue;
          if (y > doc.page.height - 60) { doc.addPage(); y = 40; }
          doc.rect(40, y, W, 18).strokeColor(GRAY_BORDER).stroke();
          xs = 40;
          ['Size ' + i, s.length, s.nos, s.weight_t].forEach((v, j) => {
            doc.text(String(v || ''), xs + 4, y + 5, { width: sizeCols[j] - 8 });
            xs += sizeCols[j];
          });
          y += 18;
        }
      }

      const footer = 'BHARAT STEEL (CHENNAI) PVT. LTD. · ' + (folder === 'Inward' ? 'Inward Inspection' : folder === 'Shearing' ? 'BSCQMS-PRD-008 REV 01' : 'BSCQMS-PRD-008 REV 03') + ' · ' + ref;
      doc.fillColor('#9CA3AF').font('Helvetica').fontSize(8);
      doc.text(footer, 40, doc.page.height - 30, { width: W, align: 'center' });
      doc.end();
    } catch(e) { reject(e); }
  });
}

// =====================================================
// COMPLAINT / DEFECT REPORT PDF (matches old template)
// =====================================================
async function generateComplaintPDF(data, photos) {
  return new Promise(async (resolve, reject) => {
    try {
      const doc = new PDFDocument({ size: 'A4', margin: 40 });
      const buffers = [];
      doc.on('data', buffers.push.bind(buffers));
      doc.on('end', () => resolve(Buffer.concat(buffers)));
      doc.on('error', reject);

      const W = doc.page.width - 80;
      let y = 40;

      // Centered title
      doc.fillColor('#000').font('Helvetica-Bold').fontSize(13);
      doc.text('BHARAT STEEL (CHENNAI) PRIVATE LIMITED', 40, y, { width: W, align: 'center' });
      y += 16;
      doc.fontSize(11);
      doc.text('QUALITY / DEFECT REPORT', 40, y, { width: W, align: 'center' });
      y += 22;

      // Date and internal no row
      doc.font('Helvetica').fontSize(9);
      doc.text('QC Date: ' + (data.qc_date || new Date().toLocaleDateString('en-IN')), 40, y);
      doc.text('Internal No. : ' + (data.case_id || '-'), 40, y, { width: W, align: 'right' });
      y += 16;

      // Info table (2 col x 4 rows)
      const drawBoxRow = (l1, v1, l2, v2) => {
        const colW = W / 2, labelW = 90;
        doc.rect(40, y, labelW, 20).fillAndStroke('#fff', '#000');
        doc.rect(40 + labelW, y, colW - labelW, 20).fillAndStroke('#fff', '#000');
        doc.rect(40 + colW, y, labelW, 20).fillAndStroke('#fff', '#000');
        doc.rect(40 + colW + labelW, y, colW - labelW, 20).fillAndStroke('#fff', '#000');
        doc.fillColor('#000').font('Helvetica-Bold').fontSize(9);
        doc.text(l1, 44, y + 6, { width: labelW - 8 });
        doc.text(l2, 44 + colW, y + 6, { width: labelW - 8 });
        doc.font('Helvetica');
        doc.text(String(v1 || '-'), 44 + labelW, y + 6, { width: colW - labelW - 8 });
        doc.text(String(v2 || '-'), 44 + colW + labelW, y + 6, { width: colW - labelW - 8 });
        y += 20;
      };

      drawBoxRow('Grade', data.grade, 'Invoice No', data.invoice_no);
      drawBoxRow('Dimensions', data.dimensions, 'Invoice date', data.invoice_date);
      drawBoxRow('Batch no', data.batch_number, 'Qty', data.quantity);
      drawBoxRow('TC Number', data.tc_number, 'Mill', data.mill);
      y += 16;

      // DEFECT PHOTOS heading
      doc.font('Helvetica-Bold').fontSize(10);
      doc.text('DEFECT PHOTOS', 40, y);
      y += 14;

      // Embed photos in 2-column grid
      if (photos && photos.length > 0) {
        const photoW = (W - 10) / 2;
        const photoH = 180;
        for (let i = 0; i < photos.length; i++) {
          if (y + photoH > doc.page.height - 60) { doc.addPage(); y = 40; }
          const col = i % 2;
          const xPos = 40 + col * (photoW + 10);
          try {
            const photoBuf = Buffer.from(photos[i].data.split(',')[1], 'base64');
            doc.image(photoBuf, xPos, y, { fit: [photoW, photoH], align: 'center', valign: 'center' });
            doc.rect(xPos, y, photoW, photoH).strokeColor('#000').stroke();
          } catch(e) {
            doc.rect(xPos, y, photoW, photoH).strokeColor('#ccc').stroke();
          }
          if (col === 1 || i === photos.length - 1) y += photoH + 10;
        }
      } else {
        doc.rect(40, y, W, 60).strokeColor('#ccc').stroke();
        doc.fillColor('#999').font('Helvetica').fontSize(9);
        doc.text('No photos attached', 40, y + 25, { width: W, align: 'center' });
        y += 70;
      }

      // REMARKS
      if (y > doc.page.height - 80) { doc.addPage(); y = 40; }
      y += 6;
      doc.fillColor('#000').font('Helvetica-Bold').fontSize(10);
      doc.text('REMARKS:', 40, y, { continued: true });
      doc.font('Helvetica').fontSize(10);
      doc.text(' ' + (data.remarks || data.description || '-'), { width: W - 70 });

      doc.end();
    } catch(e) { reject(e); }
  });
}

// =====================================================
// EMAIL FUNCTIONS
// =====================================================
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
  const recipients = wheelsIndia
    ? ['support@bharatsteels.in', 'kannan@bharatsteels.in']
    : ['support@bharatsteels.in'];
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

  const caseBtn = '<div style="margin:20px 0">'
    + '<a href="' + caseLink + '" style="display:inline-block;background:#1A6DAF;color:#fff;text-decoration:none;padding:12px 22px;border-radius:6px;font-weight:600;font-size:14px">View Case in App →</a>'
    + '<div style="font-size:11px;color:#888;margin-top:6px">Case ID: ' + escHtml(data.case_id || '-') + '</div>'
    + '</div>';

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

app.get('/stats', async (req, res) => {
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
              resolution: v[21]||'', resolved_date: v[22]||''
            };
          });
        } else result.Complaints = [];
      } else result.Complaints = [];
    } catch(e) { result.Complaints = []; }
    res.json(result);
  } catch(err) { res.status(500).json({ error: err.message }); }
});

// SUBMIT inspection form (existing)
app.post('/submit', async (req, res) => {
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
    await appendExcelRow(token, folder, data, fileName);

    res.json({ status: 'success', ref: ref, filename: fileName });

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
app.get('/init-complaints', async (req, res) => {
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
      result.excel_headers = ['Case ID','Timestamp','Source','Filed By','Customer Name','Batch Number','Grade','Dimensions','TC Number','Invoice No','Invoice Date','Quantity','Mill','Defect Type','Remarks','Status','Reviewed By','Root Cause','Decision','Vendor Name','Vendor Email','Resolution','Resolved Date'];
      result.instructions = '1) Open OneDrive 2) Go to BSC Inspections/Complaints/ 3) Create new Excel file named Complaints_Log.xlsx 4) Add the 23 column headers shown in excel_headers in row 1 5) Select all data (Ctrl+A in row 1) 6) Insert > Table (check "My table has headers") 7) Click the table > Table Design tab > Table Name: "ComplaintsLog" 8) Save and close.';
    }
    
    res.json(result);
  } catch(err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/complaint/submit', async (req, res) => {
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
      // Customer complaint - to pdqc, kannan, gourav
      status = 'Open';
      recipients = ['pdqc@bharatsteels.in', 'kannan@bharatsteels.in', 'gourav@bharatsteels.in'];
      cc = null;
    }
    data.status = status;

    // Append to Excel
    await appendComplaintRow(token, data);

    res.json({ status: 'success', case_id: caseId, filename: fileName });

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
app.post('/complaint/update', async (req, res) => {
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
    if (data.status)        updated[15] = data.status;
    if (data.reviewed_by)   updated[16] = data.reviewed_by;
    if (data.root_cause)    updated[17] = data.root_cause;
    if (data.decision)      updated[18] = data.decision;
    if (data.vendor_name)   updated[19] = data.vendor_name;
    if (data.vendor_email)  updated[20] = data.vendor_email;
    if (data.resolution)    updated[21] = data.resolution;
    if (data.resolved_date) updated[22] = data.resolved_date;

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
      await uploadFile(token, 'BSC Inspections/Complaints/Attachments/' + data.case_id + '/' + fname, buf, data.eightd_file.type || 'application/pdf');
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

      // Re-fetch PDF + TC + Invoice for re-attaching
      let attachments = [];
      try {
        const attachFolder = 'BSC Inspections/Complaints/Attachments/' + data.case_id;
        const pdfPath = 'BSC Inspections/Complaints/PDF/' + data.case_id + '_Defect_Report.pdf';
        const pdfFetch = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(pdfPath) + ':/content', { headers: { 'Authorization': 'Bearer ' + token } });
        if (pdfFetch.ok) {
          const buf = await pdfFetch.buffer();
          attachments.push({ filename: data.case_id + '_Defect_Report.pdf', content: buf.toString('base64') });
        }
        // List attachments folder
        const listResp = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(attachFolder) + ':/children?$select=name,@microsoft.graph.downloadUrl&$top=20', { headers: { 'Authorization': 'Bearer ' + token } });
        if (listResp.ok) {
          const items = (await listResp.json()).value || [];
          for (const item of items.slice(0, 8)) { // max 8 attachments
            try {
              const dl = await fetch(item['@microsoft.graph.downloadUrl']);
              if (dl.ok) {
                const buf = await dl.buffer();
                attachments.push({ filename: item.name, content: buf.toString('base64') });
              }
            } catch(e) {}
          }
        }
      } catch(e) { console.error('Attachment fetch error:', e.message); }

      const isEscalateToVendor = data.decision === 'Escalate to Vendor' && data.vendor_email;
      const isInternalClose = data.decision === 'Close Internally' || data.status === 'Completed (Internal)';
      const isVendorResolved = data.status === 'Completed (Vendor)';
      
      // Add resolved info to email payload
      dataForEmail.vendor_message = data.vendor_message || '';
      dataForEmail.resolved_by = data.resolved_by || '';
      
      if (isEscalateToVendor) {
        // Stage: Escalate to vendor
        const subject = 'Quality Complaint - Action Required - ' + data.case_id;
        let html = buildComplaintEmailBody(dataForEmail, 'to_vendor');
        // Add vendor message if provided
        if (data.vendor_message) {
          html = html.replace('</p><table', '</p><p><b>Additional notes:</b> ' + escHtml(data.vendor_message) + '</p><table');
        }
        sendEmail({
          to: [data.vendor_email],
          cc: ['pdqc@bharatsteels.in', 'kannan@bharatsteels.in', 'gourav@bharatsteels.in'],
          subject: subject, html: html, attachments: attachments
        }).then(() => console.log('Vendor email sent for', data.case_id)).catch(err => console.error('Vendor email error:', err.message));
      } else if (isInternalClose) {
        // Stage: Closed internally - notify sales + CC gourav
        const subject = 'Quality Complaint Resolved (Internal) - ' + data.case_id;
        let html = buildComplaintEmailBody(dataForEmail, 'resolved_to_sales');
        // Add root cause and resolution
        const extra = '<p><b>Root Cause:</b> ' + escHtml(data.root_cause || '-') + '</p>'
          + '<p><b>Resolution:</b> ' + escHtml(data.resolution || '-') + '</p>'
          + '<p><b>Reviewed By:</b> ' + escHtml(data.reviewed_by || '-') + '</p>';
        html = html.replace('<table', extra + '<table');
        sendEmail({
          to: ['info@bharatsteels.in', 'gourav@bharatsteels.in', 'kannan@bharatsteels.in'],
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
        sendEmail({
          to: ['info@bharatsteels.in', 'pdqc@bharatsteels.in', 'kannan@bharatsteels.in', 'gourav@bharatsteels.in'],
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
    data.resolution||'', data.resolved_date||''
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
  ]] : folder === 'Shearing' ? [[
    fileName, data.timestamp||'', data.customer_name||'', data.date||'',
    data.batch_number||'', data.grade||'', data.make||'', data.type||'',
    data.process||'', data.operator||'', data.input_size||'', data.qc_name||'',
    data.burr||'', data.blade_clearance||'', data.cutting_finish||'', data.surface_condition||'', data.bow_bend||'',
    data.overall_observation||'', data.sig_date||'',
    ...[...Array(30)].flatMap((_,i) => [sr(i).sheet_no||'', sr(i).width1||'', sr(i).width2||'', sr(i).diag1||'', sr(i).diag2||'', sr(i).remarks||''])
  ]] : [[
    fileName, data.timestamp||'', data.customer_name||'', data.date||'', data.time||'',
    data.coil_number||'', data.batch_number||'', data.make||'', data.coil_thickness||'',
    data.coil_grade||'', data.coil_width||'', data.coil_weight||'',
    data.first_bit||'', data.last_bit||'', data.defective||'', data.balance_wt||'',
    data.coil_verified||'', data.blade_clearance||'',
    data.operator||'', data.machine_name||'', data.inspector||'', data.remarks||'',
    data.bur||'', data.cutting_finish||'', data.scalling||'', data.pit_marks||'',
    data.waviness||'', data.center_bow||'', data.cutting_bow||'', data.surface_defects||'',
    s(1).length||'', s(1).nos||'', s(1).weight_t||'', s(2).length||'', s(2).nos||'', s(2).weight_t||'',
    s(3).length||'', s(3).nos||'', s(3).weight_t||'', s(4).length||'', s(4).nos||'', s(4).weight_t||'',
    s(5).length||'', s(5).nos||'', s(5).weight_t||'', s(6).length||'', s(6).nos||'', s(6).weight_t||'',
    s(7).length||'', s(7).nos||'', s(7).weight_t||'', s(8).length||'', s(8).nos||'', s(8).weight_t||'',
    s(9).length||'', s(9).nos||'', s(9).weight_t||'', s(10).length||'', s(10).nos||'', s(10).weight_t||''
  ]];
  const rowResp = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/items/' + fileId + '/workbook/tables/' + tableName + '/rows/add', {
    method:'POST', headers:{ 'Authorization':'Bearer ' + token, 'Content-Type':'application/json' }, body:JSON.stringify({ values })
  });
  if (!rowResp.ok) throw new Error('Excel row failed: ' + JSON.stringify(await rowResp.json()));
}

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log('BSC Server running on port ' + PORT));
