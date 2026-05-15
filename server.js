const express    = require('express');
const cors       = require('cors');
const fetch      = require('node-fetch');
const nodemailer = require('nodemailer');
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
const SMTP_PASS     = process.env.SMTP_PASS || '';

const transporter = nodemailer.createTransport({
  host: 'smtp.office365.com', port: 587, secure: false,
  auth: { user: SMTP_USER, pass: SMTP_PASS },
  tls: { ciphers: 'SSLv3' },
  connectionTimeout: 15000,
  greetingTimeout: 15000,
  socketTimeout: 30000
});

// =====================================================
// PDF GENERATION using pdfkit (pure JS, no Chrome needed)
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

      const W = doc.page.width - 80;  // usable width
      let y = 40;
      const ts = new Date().toLocaleString('en-IN');

      // HEADER
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
        const colW = W / 2;
        const labelW = 110;
        // Left cell
        doc.rect(40, y, labelW, 22).fillAndStroke(GRAY_BG, GRAY_BORDER);
        doc.rect(40 + labelW, y, colW - labelW, 22).strokeColor(GRAY_BORDER).stroke();
        // Right cell
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

      // =================== INWARD ===================
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
        if (data.wheels_india) {
          y += 4;
          rowFull('Wheels India Coil', 'YES');
        }
      }

      // =================== SHEARING ===================
      else if (folder === 'Shearing') {
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
        // Header row
        doc.rect(40, y, W, 18).fill('#1F2937');
        doc.fillColor('#fff').font('Helvetica-Bold').fontSize(8);
        let x = 40;
        headers.forEach((h, i) => { doc.text(h, x + 4, y + 5, { width: colWidths[i] - 8 }); x += colWidths[i]; });
        y += 18;
        // Data rows
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
      }

      // =================== CTL QUALITY ===================
      else {
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

      // Footer
      const footer = 'BHARAT STEEL (CHENNAI) PVT. LTD. · ' + (folder === 'Inward' ? 'Inward Inspection' : folder === 'Shearing' ? 'BSCQMS-PRD-008 REV 01' : 'BSCQMS-PRD-008 REV 03') + ' · ' + ref;
      doc.fillColor('#9CA3AF').font('Helvetica').fontSize(8);
      doc.text(footer, 40, doc.page.height - 30, { width: W, align: 'center' });

      doc.end();
    } catch(e) {
      reject(e);
    }
  });
}

async function sendInwardEmail(pdfBuffer, fileName, data) {
  const wheelsIndia = data.wheels_india;
  const batchNo  = data.batch_number || '-';
  const vehicleNo = data.vehicle_number || '-';
  const formDate = new Date(data.timestamp || Date.now()).toLocaleDateString('en-IN', { day:'2-digit', month:'2-digit', year:'numeric' });
  const subject = wheelsIndia
    ? 'WHEELS INDIA (Mother Coil Inspection Report) - ' + formDate + ' - ' + vehicleNo + ' - ' + batchNo
    : 'MOTHER COIL INSPECTION REPORT - ' + formDate + ' - ' + vehicleNo + ' - ' + batchNo;
  const recipients = wheelsIndia
    ? 'support@bharatsteels.in, kannan@bharatsteels.in'
    : 'support@bharatsteels.in';
  await transporter.sendMail({
    from: '"BSC Inspection" <' + SMTP_USER + '>',
    to: recipients, subject: subject,
    text: 'Please find attached the Mother Coil Inspection Report.\n\nBatch No: ' + batchNo + '\nVehicle No: ' + vehicleNo + '\nDate: ' + formDate,
    attachments: [{ filename: fileName + '.pdf', content: pdfBuffer, contentType: 'application/pdf' }]
  });
  console.log('Email sent | Subject:', subject, '| To:', recipients);
}

app.get('/', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'bharat-steel-inspection.html'));
});

app.get('/health', (req, res) => {
  res.json({ status: 'BSC Inspection Server is running' });
});

// STATS endpoint
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
        result[folder] = rows.map(r => ({
          fileName: r.values[0][0] || '',
          timestamp: r.values[0][1] || '',
          name: r.values[0][2] || '',
          batch: r.values[0][3] || ''
        }));
      } catch(e) { result[folder] = []; }
    }
    res.json(result);
  } catch(err) {
    res.status(500).json({ error: err.message });
  }
});

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
    
    // Generate PDF server-side (using pdfkit - pure JS, no Chrome)
    const ref = data.ref || ('BSC-' + Math.random().toString(36).substr(2, 6).toUpperCase());
    const pdfBuffer = await generatePDF(folder, data, ref);
    console.log('PDF generated, size:', pdfBuffer.length, 'bytes');
    
    // For CTL Quality forms, save into machine-specific subfolder (CTL-1 or CTL-2)
    let pdfFolder = 'BSC Inspections/' + folder + '/PDF';
    let photoBaseFolder = 'BSC Inspections/' + folder + '/Photos';
    if (folder === 'Quality' && data.machine_name) {
      const machine = String(data.machine_name).trim().toUpperCase().replace(/[^A-Z0-9\-]/g, '');
      if (machine === 'CTL-1' || machine === 'CTL1') {
        pdfFolder = 'BSC Inspections/' + folder + '/PDF/CTL-1';
        photoBaseFolder = 'BSC Inspections/' + folder + '/Photos/CTL-1';
      } else if (machine === 'CTL-2' || machine === 'CTL2') {
        pdfFolder = 'BSC Inspections/' + folder + '/PDF/CTL-2';
        photoBaseFolder = 'BSC Inspections/' + folder + '/Photos/CTL-2';
      }
    }
    
    // Upload PDF and photos IN PARALLEL for speed
    const uploadTasks = [
      uploadFile(token, pdfFolder + '/' + fileName + '.pdf', pdfBuffer, 'application/pdf')
    ];
    
    if (data.photos && data.photos.length > 0) {
      for (var i = 0; i < data.photos.length; i++) {
        var photo = data.photos[i];
        var photoName = 'photo_' + (i+1) + '_' + (photo.name || 'image.jpg').replace(/[^a-zA-Z0-9\.\-_]/g,'_');
        var photoBuffer = Buffer.from(photo.data.split(',')[1], 'base64');
        uploadTasks.push(uploadFile(token, photoBaseFolder + '/' + fileName + '/' + photoName, photoBuffer, photo.type || 'image/jpeg'));
      }
    }
    
    // Wait for all uploads in parallel
    await Promise.all(uploadTasks);
    
    await appendExcelRow(token, folder, data, fileName);
    
    // Send response FIRST, then send email in background (non-blocking)
    res.json({ status: 'success', ref: ref, filename: fileName });
    
    if (folder === 'Inward' && SMTP_PASS) {
      sendInwardEmail(pdfBuffer, fileName, data)
        .then(() => console.log('Email sent successfully'))
        .catch(err => console.error('Email error (non-fatal):', err.message));
    }
  } catch (err) {
    console.error('Submit error:', err.message);
    res.status(500).json({ status: 'error', message: err.message });
  }
});

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
