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
    
    // Excel row append - non-blocking so submission succeeds even if Excel times out
    appendExcelRow(token, folder, data, fileName)
      .then(() => console.log('[xlsx] row appended for', fileName))
      .catch(e => console.error('[xlsx] row append FAILED for', fileName, '-', e.message));

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
      result.excel_headers = ['Case ID','Timestamp','Source','Filed By','Customer Name','Batch Number','Grade','Dimensions','TC Number','Invoice No','Invoice Date','Quantity','Mill','Defect Type','Remarks','Status','Reviewed By','Root Cause','Decision','Vendor Name','Vendor Email','Resolution','Resolved Date','Customer Email','Production Comments','Sales Reviewer','Customer Message','Customer Outcome By','Customer Outcome Notes'];
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
app.get('/complaint/files', async (req, res) => {
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
app.get('/complaint/file', async (req, res) => {
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
app.get('/settings', async (req, res) => {
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
app.post('/admin/settings', async (req, res) => {
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
app.post('/admin/verify', (req, res) => {
  if (req.body && req.body.password === ADMIN_PASSWORD) {
    return res.json({ status: 'success' });
  }
  res.status(403).json({ error: 'Invalid password' });
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log('BSC Server running on port ' + PORT));
