const express    = require('express');
const cors       = require('cors');
const fetch      = require('node-fetch');
const htmlPdf    = require('html-pdf-node');
const nodemailer = require('nodemailer');

const app = express();
app.use(cors());
app.use(express.json({ limit: '50mb' }));

// Serve static HTML files from public folder
const path = require('path');
app.use(express.static(path.join(__dirname, 'public')));

app.use((req, res, next) => {
  res.setTimeout(120000, () => {
    res.status(408).json({ status: 'error', message: 'Request timed out.' });
  });
  next();
});

const CLIENT_ID     = process.env.CLIENT_ID;
const CLIENT_SECRET = process.env.CLIENT_SECRET;
const TENANT_ID     = process.env.TENANT_ID;
const USER_ID       = process.env.USER_ID || 'pdqc@bharatsteels.in';
const SMTP_USER     = process.env.SMTP_USER || 'pdqc@bharatsteels.in';
const SMTP_PASS     = process.env.SMTP_PASS || '';

const transporter = nodemailer.createTransport({
  host: 'smtp.office365.com', port: 587, secure: false,
  auth: { user: SMTP_USER, pass: SMTP_PASS },
  tls: { ciphers: 'SSLv3' }
});

async function sendInwardEmail(pdfBuffer, fileName, data) {
  const wheelsIndia = data.wheels_india;
  const batchNo  = data.batch_number || '-';
  const vehicleNo = data.vehicle_number || '-';
  const formDate = data.timestamp
    ? new Date(data.timestamp).toLocaleDateString('en-IN', { day:'2-digit', month:'2-digit', year:'numeric' })
    : new Date().toLocaleDateString('en-IN', { day:'2-digit', month:'2-digit', year:'numeric' });
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

// STATS endpoint — reads Excel logs and returns daily counts
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
        // Each row: values[0][0]=fileName, values[0][1]=timestamp, values[0][2]=vehicle/customer, values[0][3]=batch
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
    console.error('Stats error:', err.message);
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
    const pdfBuffer = await generatePDF(data.pdf_content);
    await uploadFile(token, 'BSC Inspections/' + folder + '/PDF/' + fileName + '.pdf', pdfBuffer, 'application/pdf');
    if (data.photos && data.photos.length > 0) {
      for (var i = 0; i < data.photos.length; i++) {
        var photo = data.photos[i];
        var photoName = 'photo_' + (i+1) + '_' + (photo.name || 'image.jpg').replace(/[^a-zA-Z0-9\.\-_]/g,'_');
        var photoBuffer = Buffer.from(photo.data.split(',')[1], 'base64');
        await uploadFile(token, 'BSC Inspections/' + folder + '/Photos/' + fileName + '/' + photoName, photoBuffer, photo.type || 'image/jpeg');
      }
    }
    await appendExcelRow(token, folder, data, fileName);
    if (folder === 'Inward' && SMTP_PASS) {
      try { await sendInwardEmail(pdfBuffer, fileName, data); }
      catch (mailErr) { console.error('Email error (non-fatal):', mailErr.message); }
    }
    res.json({ status: 'success', ref: data.ref, filename: fileName });
  } catch (err) {
    console.error('Submit error:', err.message);
    res.status(500).json({ status: 'error', message: err.message });
  }
});

app.get('/list/:folder', async (req, res) => {
  try {
    const folder = req.params.folder;
    const token  = await getToken();
    const folderPath = 'BSC Inspections/' + folder + '/PDF';
    const listUrl = 'https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(folderPath) + ':/children?$select=name&$top=50';
    const listResp = await fetch(listUrl, { headers: { 'Authorization': 'Bearer ' + token } });
    if (!listResp.ok) return res.status(404).json({ error: 'Folder not found' });
    const files = ((await listResp.json()).value || []).map(f => f.name);
    res.json({ folder: folderPath, files });
  } catch(err) { res.status(500).json({ error: err.message }); }
});

app.post('/download', async (req, res) => {
  try {
    const { folder, batch, date_str } = req.body;
    if (!folder) return res.status(400).json({ status: 'error', message: 'Missing folder' });
    if (!batch && !date_str) return res.status(400).json({ status: 'error', message: 'Provide batch or date' });
    const token      = await getToken();
    const folderPath = 'BSC Inspections/' + folder + '/PDF';
    const listUrl    = 'https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(folderPath) + ':/children?$select=name,id&$top=200';
    const listResp   = await fetch(listUrl, { headers: { 'Authorization': 'Bearer ' + token } });
    if (!listResp.ok) return res.status(404).json({ status: 'error', message: 'PDF folder not found' });
    const files   = ((await listResp.json()).value || []).map(f => f.name);
    const matches = files.filter(name => {
      const matchBatch = batch    ? name.toLowerCase().includes(batch.toLowerCase()) : true;
      const matchDate  = date_str ? name.includes(date_str) : true;
      return matchBatch && matchDate && name.endsWith('.pdf');
    });
    if (matches.length === 0) return res.status(404).json({ status: 'error', message: 'No report found' });
    const fileName = matches.sort().pop();
    const fileUrl  = 'https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(folderPath + '/' + fileName) + ':/content';
    const fileResp = await fetch(fileUrl, { headers: { 'Authorization': 'Bearer ' + token } });
    if (!fileResp.ok) return res.status(404).json({ status: 'error', message: 'File not found' });
    const buffer = await fileResp.buffer();
    res.setHeader('Content-Type', 'application/pdf');
    res.setHeader('Content-Disposition', 'attachment; filename="' + fileName + '"');
    res.send(buffer);
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

app.post('/photos', async (req, res) => {
  try {
    const { folder, batch, date_str } = req.body;
    if (!folder) return res.status(400).json({ status:'error', message:'Missing folder' });
    const token      = await getToken();
    const photosRoot = 'BSC Inspections/' + folder + '/Photos';
    const listUrl  = 'https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/root:/' + encodeURIComponent(photosRoot) + ':/children?$select=name,id&$top=200';
    const listResp = await fetch(listUrl, { headers: { 'Authorization': 'Bearer ' + token } });
    if (!listResp.ok) return res.json({ photos: [] });
    const folders = ((await listResp.json()).value || []).filter(f => {
      const matchBatch = batch    ? f.name.toLowerCase().includes(batch.toLowerCase()) : true;
      const matchDate  = date_str ? f.name.includes(date_str) : true;
      return matchBatch && matchDate;
    });
    if (folders.length === 0) return res.json({ photos: [] });
    var allPhotos = [];
    for (var fi of folders) {
      const imgResp = await fetch('https://graph.microsoft.com/v1.0/users/' + USER_ID + '/drive/items/' + fi.id + '/children?$select=name,id,@microsoft.graph.downloadUrl', { headers: { 'Authorization': 'Bearer ' + token } });
      if (!imgResp.ok) continue;
      (await imgResp.json()).value.forEach(img => allPhotos.push({ name: img.name, url: img['@microsoft.graph.downloadUrl'] }));
    }
    res.json({ photos: allPhotos });
  } catch(err) { res.status(500).json({ status:'error', message: err.message }); }
});

async function generatePDF(htmlContent) {
  return await htmlPdf.generatePdf({ content: htmlContent }, { format: 'A4', printBackground: true, margin: { top:'15mm', bottom:'15mm', left:'15mm', right:'15mm' } });
}
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
