// Post-processing approval stamp: draws the approval badge(s) on EVERY page of a finished PDF.
// Used for all payment reports (expense consolidated, OT, Shearing, Labour) so the approval
// status — with approver name + time — is visible on page 1 and on every following page,
// including the merged consolidated reports whose inner pages come from several builders.
//
//   approvals: [{ label, by, at }]   e.g. { label:'Final approved', by:'Rajan', at:'13 Oct 2026, 03:40 PM' }
//   opts.preview: true  -> amber "AWAITING APPROVAL" strip instead of green
//
// Never throws: on any failure the original buffer is returned unchanged so delivery is never blocked.
const { PDFDocument, StandardFonts, rgb } = require('pdf-lib');

const GREEN = rgb(0.023, 0.588, 0.412);   // #059669
const AMBER = rgb(0.72, 0.33, 0.02);      // #B8560A
const INK   = rgb(0.067, 0.145, 0.196);   // #112532
const BACK  = rgb(0.965, 0.980, 1.0);     // light pill backing
const BORDER = rgb(0.84, 0.88, 0.92);

// Keep only characters the standard (WinAnsi) fonts can encode; map common “smart” punctuation.
function clean(s) {
  return String(s == null ? '' : s)
    .replace(/[‒-―]/g, '-')
    .replace(/[‘’′]/g, "'")
    .replace(/[“”]/g, '"')
    .replace(/…/g, '...')
    .replace(/₹/g, 'Rs ')
    .replace(/[^\x20-\x7E\xA0-\xFF]/g, '')
    .trim();
}

async function stampApprovals(pdfBuffer, approvals, opts = {}) {
  try {
    const list = Array.isArray(approvals) ? approvals.filter(a => a && (a.by || a.label)) : [];
    const preview = !!opts.preview || list.length === 0;

    // De-duplicate identical approver+time pairs (multiple claims often share one approver).
    const seen = new Set();
    const uniq = [];
    for (const a of list) {
      const k = (a.label || '') + '|' + (a.by || '') + '|' + (a.at || '');
      if (!seen.has(k)) { seen.add(k); uniq.push(a); }
    }

    const doc = await PDFDocument.load(pdfBuffer);
    const bold = await doc.embedFont(StandardFonts.HelveticaBold);
    const reg = await doc.embedFont(StandardFonts.Helvetica);
    const pages = doc.getPages();

    const M = 40, yb = 7, H = 22;
    const statusTxt = preview ? 'AWAITING APPROVAL' : 'APPROVED';
    const statusCol = preview ? AMBER : GREEN;
    const sSize = 9;

    // Build the approval pill texts (name + time). Cap at 2 pills; collapse the rest into "+N more".
    const pills = [];
    if (!preview) {
      const fmt = (a) => {
        const lab = a.label ? clean(a.label) + ': ' : '';
        const by = a.by ? clean(a.by) : '';
        const at = a.at ? '  ·  ' + clean(a.at) : '';
        return (lab + by + at).trim();
      };
      if (uniq.length <= 2) { uniq.forEach(a => pills.push(fmt(a))); }
      else { pills.push(fmt(uniq[0])); pills.push('+' + (uniq.length - 1) + ' more approvals'); }
    } else {
      pills.push('Preview · not yet approved');
    }

    for (const page of pages) {
      const W = page.getWidth();
      // status pill
      const sW = bold.widthOfTextAtSize(statusTxt, sSize) + 16;
      page.drawRectangle({ x: M, y: yb, width: sW, height: H, color: statusCol });
      page.drawText(statusTxt, { x: M + 8, y: yb + (H - sSize) / 2 + 1, size: sSize, font: bold, color: rgb(1, 1, 1) });

      // approval pills (light backing + ink text)
      let x = M + sW + 8;
      const tSize = 8;
      for (const t of pills) {
        const tw = reg.widthOfTextAtSize(t, tSize);
        let pw = tw + 14;
        if (x + pw > W - M) {                       // truncate the last pill to fit the page
          const avail = W - M - x - 14;
          if (avail < 30) break;
          let tt = t;
          while (tt.length > 1 && reg.widthOfTextAtSize(tt, tSize) > avail) tt = tt.slice(0, -1);
          const ttw = reg.widthOfTextAtSize(tt, tSize); pw = ttw + 14;
          page.drawRectangle({ x, y: yb, width: pw, height: H, color: BACK, borderColor: BORDER, borderWidth: 0.75 });
          page.drawText(tt, { x: x + 7, y: yb + (H - tSize) / 2 + 1, size: tSize, font: reg, color: INK });
          break;
        }
        page.drawRectangle({ x, y: yb, width: pw, height: H, color: BACK, borderColor: BORDER, borderWidth: 0.75 });
        page.drawText(t, { x: x + 7, y: yb + (H - tSize) / 2 + 1, size: tSize, font: reg, color: INK });
        x += pw + 6;
      }
    }
    const out = await doc.save();
    return Buffer.from(out);
  } catch (e) {
    console.error('[approval_stamp] skipped:', e.message);
    return pdfBuffer;
  }
}

module.exports = { stampApprovals };
