// NMDC backend for MIS portal — sync (Excel->Neon) + dispatch (read). Transporter/WATI/arrive disabled (routes removed; WATI env vars unset).
// original single-file build (dispatch, sync, tform, arrive) — build 2026-09-21g (consignment lines, Ref key, SO master)

// lib/db.js
import { neon } from "@neondatabase/serverless";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
var sql = neon(process.env.DATABASE_URL);
var STATUSES = ["Under Loading", "Loaded", "In-Transit", "Arrived"];
function json(res, status, body) {
  res.setHeader("Cache-Control", "no-store");
  res.status(status).json(body);
}
var d = (v) => v ? String(v).slice(0, 10) : null;
function cors(req, res) {
  const norm2 = (u) => String(u || "").trim().toLowerCase().replace(/\/+$/, "");
  const allowed = (process.env.ALLOWED_ORIGINS || "").split(",").map(norm2).filter(Boolean);
  const origin = req.headers.origin;
  const ok = origin && (allowed.includes("*") || allowed.some((a) => a === norm2(origin) || a === norm2(origin).replace(/^https?:\/\//, "")));
  if (ok) {
    res.setHeader("Access-Control-Allow-Origin", origin);
    res.setHeader("Vary", "Origin");
    res.setHeader("Access-Control-Allow-Headers", "Content-Type, x-dash-pin");
    res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    res.setHeader("Access-Control-Max-Age", "86400");
  }
  if (req.method === "OPTIONS") {
    res.status(204).end();
    return true;
  }
  return false;
}

// api/dispatch.js
var lastSyncAt = 0;
async function handler(req, res) {
  if (cors(req, res)) return;
  // Optional live pull: ?sync=1 runs the Excel->Neon import first (rate-limited to
  // once per 20s so a refresh-spam can't hammer Graph). Uses the server-side secret;
  // the browser never sees it.
  const wantSync = /[?&]sync=1/.test(req.url || "");
  if (wantSync && process.env.CRON_SECRET && (Date.now() - lastSyncAt > 20000)) {
    lastSyncAt = Date.now();
    try {
      const base = (process.env.PUBLIC_URL || `https://${req.headers.host}`).replace(/\/$/, "");
      await fetch(base + "/api/sync", { headers: { authorization: "Bearer " + process.env.CRON_SECRET } });
    } catch (e) { /* if sync fails, still return whatever Neon has */ }
  }
  try {
    const c = await sql`select * from coils where coalesce(remarks,'') not ilike '%cancelled%' order by excel_row nulls last, created_at`;
    const rows = c.map((r) => ({
      "Entry Date": r.entry_date,
      "NMDC SO No": r.so_no,
      "NMDC Invoice / DO No": r.do_no,
      "Ref": r.coil_no,
      "No of Coils": r.n_coils ?? "",
      "Form": r.form,
      "Grade": r.grade,
      "Thk (mm)": r.thk == null ? "" : Number(r.thk),
      "Width (mm)": r.width ?? "",
      "Length (mm)": r.length ?? "",
      "Weight (MT)": r.weight == null ? "" : Number(r.weight),
      "Status": r.status,
      "Vehicle No": r.vehicle_no || "",
      "Transporter Name": r.transporter_name || "",
      "Transporter Mobile": r.transporter_mobile || "",
      "LR No": r.lr_no || "",
      "Dispatch Date (ex NMDC)": r.dispatch_date,
      "Expected Arrival": r.expected_arrival,
      "Actual Arrival": r.actual_arrival,
      "Receiving Unit": r.destination || "",
      "Remarks": r.remarks || "",
      "Entered By": r.entered_by || "BSC"
    }));
    const sos = (await sql`select so_no, so_date, description, ordered_mt, ordered_coils, remarks from sos order by so_no`).map((x) => ({ so_no: x.so_no, so_date: x.so_date, description: x.description || "", ordered_mt: x.ordered_mt == null ? null : Number(x.ordered_mt), ordered_coils: x.ordered_coils, remarks: x.remarks || "" }));
    json(res, 200, { rows, sos, fetchedAt: (/* @__PURE__ */ new Date()).toISOString() });
  } catch (e) {
    json(res, 502, { error: e.message });
  }
}

// api/sync.js
import crypto from "node:crypto";

// lib/graph.js
var G = "https://graph.microsoft.com/v1.0";
var tok = { v: null, exp: 0 };
var itemRef = null;
var layout = null;
async function token() {
  if (tok.v && Date.now() < tok.exp - 6e4) return tok.v;
  const body = new URLSearchParams({
    client_id: process.env.CLIENT_ID,
    client_secret: process.env.CLIENT_SECRET,
    scope: "https://graph.microsoft.com/.default",
    grant_type: "client_credentials"
  });
  const r = await fetch(
    `https://login.microsoftonline.com/${process.env.TENANT_ID}/oauth2/v2.0/token`,
    { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body }
  );
  const j = await r.json();
  if (!r.ok) throw new Error("Token error: " + (j.error_description || r.status));
  tok = { v: j.access_token, exp: Date.now() + j.expires_in * 1e3 };
  return j.access_token;
}
async function graph(path, t, opts = {}) {
  const r = await fetch(G + path, {
    method: opts.method || "GET",
    body: opts.body,
    headers: { Authorization: "Bearer " + t, "Cache-Control": "no-cache", "Content-Type": "application/json", ...opts.headers || {} }
  });
  if (!r.ok) throw new Error(`Graph ${r.status} ${path.slice(0, 80)}: ${(await r.text()).slice(0, 300)}`);
  return r.status === 204 ? null : r.json();
}
var shareId = (u) => "u!" + Buffer.from(u).toString("base64").replace(/=+$/, "").replace(/\//g, "_").replace(/\+/g, "-");
var TABLE = () => encodeURIComponent(process.env.TABLE_NAME || "tbl_Dispatch");
async function workbook(t) {
  if (!itemRef) {
    let it;
    if (process.env.DRIVE_USER && process.env.FILE_PATH) {
      const p = process.env.FILE_PATH.replace(/^\/+/, "").split("/").map(encodeURIComponent).join("/");
      it = await graph(`/users/${encodeURIComponent(process.env.DRIVE_USER)}/drive/root:/${p}:?$select=id,parentReference`, t);
    } else {
      it = await graph(`/shares/${shareId(process.env.SHARE_LINK)}/driveItem?$select=id,parentReference`, t);
    }
    itemRef = { driveId: it.parentReference.driveId, itemId: it.id };
  }
  return `/drives/${itemRef.driveId}/items/${itemRef.itemId}/workbook`;
}
async function withSession(persist, fn) {
  const t = await token();
  const wb = await workbook(t);
  const s2 = await graph(wb + "/createSession", t, { method: "POST", body: JSON.stringify({ persistChanges: persist }) });
  const h = { "workbook-session-id": s2.id };
  try {
    return await fn({ t, wb, h, get: (p) => graph(wb + p, t, { headers: h }), patch: (p, body) => graph(wb + p, t, { method: "PATCH", headers: h, body: JSON.stringify(body) }) });
  } finally {
    graph(wb + "/closeSession", t, { method: "POST", headers: h }).catch(() => {
    });
  }
}
async function tableLayout(s2) {
  if (layout) return layout;
  const r = await s2.get(`/tables/${TABLE()}/range?$select=address`);
  const m = /^(.*)!\$?([A-Z]+)\$?(\d+):/.exec(r.address);
  layout = { sheet: m[1].replace(/^'|'$/g, ""), col: m[2], headerRow: parseInt(m[3], 10) };
  return layout;
}
async function readTable(s2) {
  const [hdr, body] = await Promise.all([s2.get(`/tables/${TABLE()}/headerRowRange?$select=values`), s2.get(`/tables/${TABLE()}/rows?$select=values`)]);
  const headers = hdr.values[0].map((h) => String(h).trim());
  return { headers, rows: body.value.map((x, i) => ({ i, raw: x.values[0], obj: Object.fromEntries(headers.map((h, k) => [h, x.values[0][k]])) })) };
}
var colLetter = (n2) => {
  let s2 = "";
  n2 += 1;
  while (n2 > 0) {
    const m = (n2 - 1) % 26;
    s2 = String.fromCharCode(65 + m) + s2;
    n2 = Math.floor((n2 - 1) / 26);
  }
  return s2;
};
var serial = (iso) => iso ? Math.round(Date.parse(String(iso).slice(0, 10) + "T00:00:00Z") / 864e5 + 25569) : "";
var fromSerial = (v) => typeof v === "number" && v > 2e4 ? new Date(Math.round((v - 25569) * 864e5)).toISOString().slice(0, 10) : typeof v === "string" && /^\d{4}-\d{2}-\d{2}/.test(v) ? v.slice(0, 10) : null;

// lib/wati.js
function watiBase(){ return process.env.WATI_API_URL || process.env.WATI_ENDPOINT || process.env.WATI_BASE_URL || ""; }
function watiKey(){ return (process.env.WATI_API_KEY || process.env.WATI_ACCESS_TOKEN || process.env.WATI_TOKEN || "").replace(/^Bearer\s+/i, ""); }
async function send(mobile, template, parameters) {
  const base = watiBase(), key = watiKey();
  if (!base || !key) return { skipped: "WATI not configured" };
  const num = String(mobile).replace(/\D/g, "").replace(/^0+/, "");
  const wa = num.length === 10 ? "91" + num : num;
  const r = await fetch(`${base.replace(/\/$/, "")}/api/v1/sendTemplateMessage?whatsappNumber=${wa}`, {
    method: "POST",
    headers: { Authorization: "Bearer " + key, "Content-Type": "application/json" },
    body: JSON.stringify({ template_name: template, broadcast_name: template, parameters })
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || j.result === false) throw new Error("WATI: " + (j.info || j.message || r.status));
  return { ok: true };
}
var sendDispatchLink = ({ mobile, name, coils, doNo, link }) => send(mobile, process.env.WATI_TEMPLATE || "nmdc_dispatch_link", [{ name: "name", value: name || "Partner" }, { name: "coils", value: String(coils) }, { name: "do_no", value: doNo || "-" }, { name: "link", value: link }]);
var sendTransporterLink = ({ mobile, name, link }) => send(mobile, process.env.WATI_TEMPLATE_TRANSPORTER || "nmdc_transporter_link", [{ name: "name", value: name || "Partner" }, { name: "link", value: link }]);

// api/sync.js
var OWN = ["Entry Date", "NMDC SO No", "NMDC Invoice / DO No", "No of Coils", "Form", "Grade", "Thk (mm)", "Width (mm)", "Length (mm)", "Weight (MT)", "Receiving Unit", "Transporter Name", "Transporter Mobile", "Remarks"];
var MIRROR = ["Status", "Vehicle No", "LR No", "Dispatch Date (ex NMDC)", "Expected Arrival", "Actual Arrival"];
var PRESENT = ["Entry Date", "NMDC SO No", "NMDC Invoice / DO No", "No of Coils", "Thk (mm)", "Weight (MT)", "Status", "Vehicle No"];
var s = (v) => v == null ? "" : String(v).trim();
var n = (v) => typeof v === "number" ? v : v === "" || v == null ? null : isNaN(Number(v)) ? null : Number(v);
var norm = (o) => {
  if (!("Receiving Unit" in o) && "Destination" in o) o["Receiving Unit"] = o["Destination"];
  return o;
};
var sha = (o, keys) => crypto.createHash("sha1").update(keys.map((k) => s(o[k])).join("|")).digest("hex");
var formOf = (v) => /^ctl/i.test(s(v)) ? "CTL" : s(v) ? "Coil" : "";
var statusOf = (v, o) => STATUSES.includes(s(v)) ? s(v) : s(o["Actual Arrival"]) ? "Arrived" : s(o["Vehicle No"]) && s(o["Dispatch Date (ex NMDC)"]) ? "In-Transit" : "Under Loading";
async function handler2(req, res) {
  const auth = req.headers.authorization || "";
  if (!process.env.CRON_SECRET) return json(res, 503, { error: "CRON_SECRET not set" });
  const key = decodeURIComponent((/[?&]key=([^&]*)/.exec(req.url || "") || [])[1] || req.query && req.query.key || "");
  if (auth !== "Bearer " + process.env.CRON_SECRET && key !== process.env.CRON_SECRET) return json(res, 401, { error: "unauthorized" });
  const out = { sos: 0, transporters: 0, imported: 0, updated: 0, adopted: 0, dispatches: 0, notified: 0, mirrored: 0, appended: 0, errors: [] };
  const base = (process.env.PUBLIC_URL || `https://${req.headers.host}`).replace(/\/$/, "");
  try {
    await withSession(true, async (sess) => {
      try {
        const tr = await sess.get(`/tables/tbl_Transporters/rows?$select=values`);
        for (const [i, row] of tr.value.entries()) {
          const [name, mobile, link, sent, active] = row.values[0].map((v) => s(v));
          const mob = mobile.replace(/\D/g, "");
          if (!name) continue;
          let t = (await sql`select * from transporters where lower(name)=lower(${name}) or (mobile<>'' and mobile=${mob}) order by id limit 1`)[0];
          const isActive = active.toLowerCase() !== "no";
          if (!t) {
            const token2 = crypto.randomBytes(9).toString("base64url");
            t = (await sql`insert into transporters (name, mobile, token, active, excel_row) values (${name}, ${mob || "x-" + token2}, ${token2}, ${isActive}, ${i}) returning *`)[0];
            out.transporters++;
          } else if (t.name !== name || t.active !== isActive || t.excel_row !== i || mob && t.mobile !== mob) {
            await sql`update transporters set name=${name}, active=${isActive}, excel_row=${i}, mobile=${mob || t.mobile} where id=${t.id}`;
            t = { ...t, name, active: isActive, mobile: mob || t.mobile };
          }
          const wantLink = `${base}/t/${t.token}`;
          let sentVal = sent;
          if (!t.link_sent_at && isActive && mob.length >= 10) {
            try {
              const r = await sendTransporterLink({ mobile: mob, name, link: wantLink });
              if (!r.skipped) {
                await sql`update transporters set link_sent_at=now(), notify_error=null where id=${t.id}`;
                sentVal = (/* @__PURE__ */ new Date()).toISOString().slice(0, 10);
                out.notified++;
              } else await sql`update transporters set notify_error=${r.skipped} where id=${t.id}`;
            } catch (e) {
              out.errors.push(e.message);
              await sql`update transporters set notify_error=${e.message} where id=${t.id}`;
            }
          }
          if (link !== wantLink || sentVal !== sent) await sess.patch(`/tables/tbl_Transporters/rows/itemAt(index=${i})`, { values: [[name, mobile, wantLink, sentVal, active || "Yes"]] });
        }
      } catch (e) {
        if (!/404|ItemNotFound/i.test(e.message)) throw e;
      }
      try {
        const so = await sess.get(`/tables/tbl_SO/rows?$select=values`);
        for (const [i, row] of so.value.entries()) {
          const [soNo, soDate, desc, mt, coils, rem] = row.values[0];
          if (!s(soNo)) continue;
          const r = await sql`insert into sos (so_no, so_date, description, ordered_mt, ordered_coils, remarks, excel_row) values (${s(soNo)}, ${fromSerial(soDate)}, ${s(desc)}, ${n(mt)}, ${n(coils)}, ${s(rem)}, ${i})
            on conflict (so_no) do update set so_date=excluded.so_date, description=excluded.description, ordered_mt=excluded.ordered_mt, ordered_coils=excluded.ordered_coils, remarks=excluded.remarks, excel_row=excluded.excel_row, updated_at=now()
            where (sos.so_date, sos.description, sos.ordered_mt, sos.ordered_coils, sos.remarks) is distinct from (excluded.so_date, excluded.description, excluded.ordered_mt, excluded.ordered_coils, excluded.remarks) returning so_no`;
          if (r.length) out.sos++;
        }
      } catch (e) {
        if (!/404|ItemNotFound/i.test(e.message)) throw e;
      }
      const tlist = await sql`select id, name, mobile from transporters`;
      const tByMobile = Object.fromEntries(tlist.filter((t) => /^\d{10,}$/.test(t.mobile)).map((t) => [t.mobile, t]));
      const tByName = Object.fromEntries(tlist.map((t) => [t.name.toLowerCase(), t]));
      const { headers, rows } = await readTable(sess);
      rows.forEach((r) => norm(r.obj));
      const hidx = Object.fromEntries(headers.map((h, i) => [h, i]));
      if (!("Receiving Unit" in hidx) && "Destination" in hidx) hidx["Receiving Unit"] = hidx["Destination"];
      for (const k of [...OWN, ...MIRROR, "Ref", "Entered By"]) if (!(k in hidx)) throw new Error(`Column "${k}" not found in ${TABLE()}`);
      const lay = await tableLayout(sess);
      const refCol = colLetter(hidx["Ref"]);
      const live = rows.filter((r) => PRESENT.some((k) => s(r.obj[k]) !== ""));
      const existing = Object.fromEntries((await sql`select coil_no, excel_hash, mirror_hash, excel_row, dispatch_id, status, source, updated_at, mirrored_at from coils`).map((c) => [c.coil_no, c]));
      let nextNo = Math.max(0, ...Object.keys(existing).map((k) => parseInt((/^D-(\d+)$/.exec(k) || [])[1] || "0", 10)), ...live.map((r) => parseInt((/^D-(\d+)$/.exec(s(r.obj["Ref"])) || [])[1] || "0", 10))) + 1;
      const fresh = [];
      for (const r of live) {
        const o = r.obj, sheetRow = lay.headerRow + 1 + r.i;
        let ref = s(o["Ref"]);
        if (!ref || !/^D-\d+$/.test(ref)) {
          ref = "D-" + String(nextNo++).padStart(4, "0");
          await sess.patch(`/worksheets('${encodeURIComponent(lay.sheet)}')/range(address='${refCol}${sheetRow}')`, { values: [[ref]], numberFormat: [["@"]] });
          o["Ref"] = ref;
        }
        const ex = existing[ref];
        if (ex && ex.source === "transporter") {
          if (ex.excel_row !== r.i) await sql`update coils set excel_row=${r.i} where coil_no=${ref}`;
          continue;
        }
        const h = sha(o, OWN), mh = sha(o, MIRROR);
        const own = {
          entry_date: fromSerial(o["Entry Date"]),
          so_no: s(o["NMDC SO No"]),
          do_no: s(o["NMDC Invoice / DO No"]),
          n_coils: n(o["No of Coils"]),
          form: formOf(o["Form"]),
          grade: s(o["Grade"]),
          thk: n(o["Thk (mm)"]),
          width: n(o["Width (mm)"]),
          length: n(o["Length (mm)"]),
          weight: n(o["Weight (MT)"]),
          destination: s(o["Receiving Unit"]),
          transporter_name: s(o["Transporter Name"]),
          transporter_mobile: s(o["Transporter Mobile"]).replace(/\D/g, ""),
          remarks: s(o["Remarks"])
        };
        if (!own.transporter_mobile && own.transporter_name && tByName[own.transporter_name.toLowerCase()]) {
          const m = tByName[own.transporter_name.toLowerCase()].mobile;
          if (/^\d{10,}$/.test(m)) own.transporter_mobile = m;
        }
        const tid = tByMobile[own.transporter_mobile]?.id ?? tByName[own.transporter_name.toLowerCase()]?.id ?? null;
        const mir = { status: statusOf(o["Status"], o), vehicle_no: s(o["Vehicle No"]).toUpperCase(), lr_no: s(o["LR No"]), dispatch_date: fromSerial(o["Dispatch Date (ex NMDC)"]), expected_arrival: fromSerial(o["Expected Arrival"]), actual_arrival: fromSerial(o["Actual Arrival"]) };
        if (!ex) {
          await sql`insert into coils (coil_no, entry_date, so_no, do_no, n_coils, form, grade, thk, width, length, weight, destination, transporter_name, transporter_mobile, remarks,
              status, vehicle_no, lr_no, dispatch_date, expected_arrival, actual_arrival, excel_row, excel_hash, mirror_hash, mirrored_at, source, transporter_id, entered_by)
            values (${ref}, ${own.entry_date}, ${own.so_no}, ${own.do_no}, ${own.n_coils}, ${own.form}, ${own.grade}, ${own.thk}, ${own.width}, ${own.length}, ${own.weight}, ${own.destination},
              ${own.transporter_name}, ${own.transporter_mobile}, ${own.remarks}, ${mir.status}, ${mir.vehicle_no}, ${mir.lr_no}, ${mir.dispatch_date}, ${mir.expected_arrival}, ${mir.actual_arrival},
              ${r.i}, ${h}, ${mh}, now(), 'bsc', ${tid}, ${s(o["Entered By"]) || "BSC"})`;
          await sql`insert into events (coil_no, action, actor, detail) values (${ref}, 'imported', 'sync', ${JSON.stringify({ row: r.i })})`;
          out.imported++;
          if (own.transporter_mobile) fresh.push({ ref, ...own });
          continue;
        }
        if (ex.excel_hash !== h || ex.excel_row !== r.i) {
          await sql`update coils set entry_date=${own.entry_date}, so_no=${own.so_no}, do_no=${own.do_no}, n_coils=${own.n_coils}, form=${own.form}, grade=${own.grade}, thk=${own.thk}, width=${own.width}, length=${own.length},
              weight=${own.weight}, destination=${own.destination}, transporter_name=${own.transporter_name}, transporter_mobile=${own.transporter_mobile}, remarks=${own.remarks},
              excel_row=${r.i}, excel_hash=${h}, transporter_id=${tid} where coil_no=${ref}`;
          if (ex.excel_hash !== h) out.updated++;
          if (!ex.dispatch_id && own.transporter_mobile) fresh.push({ ref, ...own });
        }
        const neonNewer = ex.mirrored_at && ex.updated_at && new Date(ex.updated_at) > new Date(ex.mirrored_at);
        if (ex.mirror_hash !== mh && !neonNewer) {
          await sql`update coils set status=${mir.status}, vehicle_no=${mir.vehicle_no}, lr_no=${mir.lr_no}, dispatch_date=${mir.dispatch_date}, expected_arrival=${mir.expected_arrival}, actual_arrival=${mir.actual_arrival}, mirror_hash=${mh}, mirrored_at=now(), updated_at=now() where coil_no=${ref}`;
          await sql`insert into events (coil_no, action, actor, detail) values (${ref}, 'excel-edit', 'bsc', ${JSON.stringify(mir)})`;
          out.adopted++;
        }
      }
      const groups = {};
      for (const f of fresh) {
        const k = (f.do_no || f.so_no) + "|" + f.transporter_mobile;
        (groups[k] ||= []).push(f);
      }
      for (const k of Object.keys(groups)) {
        const g = groups[k], f = g[0], dn = f.do_no || f.so_no;
        let disp = (await sql`select * from dispatches where do_no=${dn} and transporter_mobile=${f.transporter_mobile} order by id desc limit 1`)[0];
        if (!disp) {
          disp = (await sql`insert into dispatches (token, do_no, transporter_name, transporter_mobile, transporter_id) values (${crypto.randomBytes(9).toString("base64url")}, ${dn}, ${f.transporter_name}, ${f.transporter_mobile}, ${tByMobile[f.transporter_mobile]?.id ?? null}) returning *`)[0];
          out.dispatches++;
        }
        await sql`update coils set dispatch_id=${disp.id} where coil_no = any(${g.map((x) => x.ref)})`;
        try {
          const count = (await sql`select coalesce(sum(coalesce(n_coils,1)),0)::int as c from coils where dispatch_id=${disp.id}`)[0].c;
          const standing = tByMobile[disp.transporter_mobile];
          const r = await sendDispatchLink({ mobile: disp.transporter_mobile, name: disp.transporter_name, coils: count, doNo: disp.do_no, link: `${base}/t/${standing ? standing.token : disp.token}` });
          await sql`update dispatches set notified_at=now(), notify_error=${r.skipped || null} where id=${disp.id}`;
          if (!r.skipped) out.notified++;
        } catch (e) {
          out.errors.push(e.message);
          await sql`update dispatches set notify_error=${e.message} where id=${disp.id}`;
        }
      }
      const fullRow = (c) => headers.map((h) => ({
        "Sl No": "",
        "Entry Date": serial(c.entry_date),
        "NMDC SO No": c.so_no || "",
        "NMDC Invoice / DO No": c.do_no || "",
        "No of Coils": c.n_coils ?? "",
        "Form": c.form || "",
        "Grade": c.grade || "",
        "Thk (mm)": c.thk ?? "",
        "Width (mm)": c.width ?? "",
        "Length (mm)": c.length ?? "",
        "Weight (MT)": c.weight ?? "",
        "Status": c.status,
        "Vehicle No": c.vehicle_no || "",
        "Transporter Name": c.transporter_name || "",
        "Transporter Mobile": c.transporter_mobile || "",
        "LR No": c.lr_no || "",
        "Dispatch Date (ex NMDC)": serial(c.dispatch_date),
        "Expected Arrival": serial(c.expected_arrival),
        "Actual Arrival": serial(c.actual_arrival),
        "Receiving Unit": c.destination || "",
        "Destination": c.destination || "",
        "Remarks": c.remarks || "",
        "Entered By": c.entered_by || c.transporter_name || "",
        "Ref": c.coil_no,
        "Last Updated": ""
      })[h] ?? "");
      const slFormula = (sheetRow) => `=IF(COUNTA(${colLetter(hidx["Entry Date"])}${sheetRow}:${colLetter(hidx["Entered By"])}${sheetRow})=0,"",ROW()-${lay.headerRow})`;
      const luFormula = (sheetRow) => `=IF(COUNTA(${colLetter(hidx["Entry Date"])}${sheetRow}:${colLetter(hidx["Entered By"])}${sheetRow})=0,"",NOW())`;
      const rowFmt = headers.map((h) => /Date|Arrival|Updated/.test(h) ? "dd-mmm-yy" : /Weight/.test(h) ? "#,##0.000" : /Mobile|SO No|Ref|Vehicle|LR|Invoice/.test(h) ? "@" : "General");
      const news = await sql`select * from coils where source='transporter' and excel_row is null order by created_at`;
      let nextRow = live.length;
      for (const c of news) {
        const rowIdx = nextRow++, sheetRow = lay.headerRow + 1 + rowIdx;
        if (rowIdx >= rows.length) {
          out.errors.push("tbl_Dispatch is full \u2014 extend the table");
          break;
        }
        const vals = fullRow(c);
        vals[hidx["Sl No"]] = slFormula(sheetRow);
        if ("Last Updated" in hidx) vals[hidx["Last Updated"]] = luFormula(sheetRow);
        await sess.patch(`/worksheets('${encodeURIComponent(lay.sheet)}')/range(address='${colLetter(0)}${sheetRow}:${colLetter(headers.length - 1)}${sheetRow}')`, { values: [vals], numberFormat: [rowFmt] });
        await sql`update coils set excel_row=${rowIdx}, mirrored_at=now() where coil_no=${c.coil_no}`;
        live.push({ i: rowIdx, obj: { Ref: c.coil_no } });
        out.appended++;
      }
      const dirty = await sql`select * from coils where excel_row is not null and (mirrored_at is null or updated_at > mirrored_at)`;
      const byRow = Object.fromEntries(live.map((r) => [r.i, r]));
      const firstCol = hidx["Status"], lastCol = hidx["Actual Arrival"];
      for (const c of dirty) {
        const r = byRow[c.excel_row];
        if (!r || s(r.obj["Ref"]) !== c.coil_no) {
          out.errors.push(`row moved for ${c.coil_no}`);
          continue;
        }
        const sheetRow = lay.headerRow + 1 + c.excel_row;
        if (c.source === "transporter") {
          const vals = fullRow(c);
          vals[hidx["Sl No"]] = slFormula(sheetRow);
          if ("Last Updated" in hidx) vals[hidx["Last Updated"]] = luFormula(sheetRow);
          await sess.patch(`/worksheets('${encodeURIComponent(lay.sheet)}')/range(address='${colLetter(0)}${sheetRow}:${colLetter(headers.length - 1)}${sheetRow}')`, { values: [vals] });
        } else {
          const map = {
            "Status": c.status,
            "Vehicle No": c.vehicle_no || "",
            "Transporter Name": c.transporter_name || "",
            "Transporter Mobile": c.transporter_mobile || "",
            "LR No": c.lr_no || "",
            "Dispatch Date (ex NMDC)": serial(c.dispatch_date),
            "Expected Arrival": serial(c.expected_arrival),
            "Actual Arrival": serial(c.actual_arrival)
          };
          const vals = headers.slice(firstCol, lastCol + 1).map((h) => h in map ? map[h] : r.obj[h] ?? "");
          const fmt = headers.slice(firstCol, lastCol + 1).map((h) => /Date|Arrival/.test(h) ? "dd-mmm-yy" : /Mobile|Vehicle|LR/.test(h) ? "@" : "General");
          await sess.patch(`/worksheets('${encodeURIComponent(lay.sheet)}')/range(address='${colLetter(firstCol)}${sheetRow}:${colLetter(lastCol)}${sheetRow}')`, { values: [vals], numberFormat: [fmt] });
        }
        const o2 = { ...r.obj, "Status": c.status, "Vehicle No": c.vehicle_no || "", "LR No": c.lr_no || "", "Dispatch Date (ex NMDC)": serial(c.dispatch_date), "Expected Arrival": serial(c.expected_arrival), "Actual Arrival": serial(c.actual_arrival) };
        await sql`update coils set mirrored_at=now(), mirror_hash=${sha(o2, MIRROR)} where coil_no=${c.coil_no}`;
        out.mirrored++;
      }
    });
    json(res, 200, { ok: true, ...out });
  } catch (e) {
    json(res, 500, { ok: false, ...out, error: e.message });
  }
}

// api/tform.js
import crypto2 from "node:crypto";
var VEH = /^[A-Z]{2}[ -]?\d{1,2}[ -]?[A-Z]{0,3}[ -]?\d{4}$/i;
var destCache = { at: 0, list: [] };
async function destinations() {
  if (Date.now() - destCache.at < 10 * 6e4 && destCache.list.length) return destCache.list;
  try {
    const list = await withSession(false, async (s2) => (await s2.get(`/worksheets('Lists')/range(address='C2:C31')?$select=values`)).values.flat().map((v) => String(v).trim()).filter(Boolean));
    destCache = { at: Date.now(), list };
  } catch {
  }
  return destCache.list;
}
var group = (lines) => {
  const g = {};
  for (const c of lines) (g[c.do_no || (c.so_no ? "SO " + c.so_no : "(no invoice yet)")] ||= []).push(c);
  return Object.entries(g).map(([do_no, coils]) => ({ do_no, coils }));
};
async function handler3(req, res) {
  const qtoken = decodeURIComponent((/[?&]token=([^&]*)/.exec(req.url || "") || [])[1] || req.query && req.query.token || "");
  const token2 = String((req.method === "GET" ? qtoken : req.body?.token) || "");
  if (!token2) return json(res, 400, { error: "token missing" });
  let tp = (await sql`select * from transporters where token=${token2} and active`)[0];
  let disp = null;
  if (!tp) {
    disp = (await sql`select * from dispatches where token=${token2}`)[0];
    if (!disp) return json(res, 404, { error: "This link is not valid. Please contact Bharat Steel." });
    if (disp.transporter_id) tp = (await sql`select * from transporters where id=${disp.transporter_id} and active`)[0] || null;
  }
  const name = tp?.name || disp?.transporter_name || "";
  const mobile = tp?.mobile || disp?.transporter_mobile || "";
  const actor = "transporter:" + mobile;
  const mine = () => tp ? sql`select coil_no as ref, do_no, so_no, n_coils, form, grade, thk, width, length, weight, destination, status, vehicle_no, lr_no, dispatch_date, expected_arrival, actual_arrival, source, entered_by
          from coils where (transporter_id=${tp.id} or (transporter_mobile<>'' and transporter_mobile=${tp.mobile})) and coalesce(remarks,'') not ilike '%cancelled%' order by created_at desc, coil_no` : sql`select coil_no as ref, do_no, so_no, n_coils, form, grade, thk, width, length, weight, destination, status, vehicle_no, lr_no, dispatch_date, expected_arrival, actual_arrival, source, entered_by
          from coils where dispatch_id=${disp.id} order by coil_no`;
  if (req.method === "GET") return json(res, 200, { mode: tp ? "transporter" : "dispatch", name, do_no: disp?.do_no || null, groups: group(await mine()), destinations: await destinations() });
  if (req.method !== "POST") return json(res, 405, { error: "method" });
  const b = req.body || {}, action = String(b.action || ""), today = (/* @__PURE__ */ new Date()).toISOString().slice(0, 10);
  if (action === "create") {
    if (!tp) return json(res, 403, { error: "This link cannot create new dispatches" });
    const doNo = String(b.do_no || "").trim(), soNo = String(b.so_no || "").trim(), dest = String(b.destination || "").trim();
    const veh = String(b.vehicle_no || "").toUpperCase().replace(/\s+/g, " ").trim(), lr = String(b.lr_no || "").trim();
    const dd = d(b.dispatch_date) || today, ea = d(b.expected_arrival);
    const lines = (Array.isArray(b.lines) ? b.lines : []).map((c) => ({
      n_coils: c.n_coils === "" || c.n_coils == null ? null : Number(c.n_coils),
      form: /^ctl/i.test(String(c.form || "")) ? "CTL" : "Coil",
      grade: String(c.grade || "").trim(),
      thk: Number(c.thk) || null,
      width: Number(c.width) || null,
      length: Number(c.length) || null,
      weight: Number(c.weight) || null
    }));
    if (!doNo && !soNo) return json(res, 400, { error: "Enter the NMDC invoice / DO number (or the SO number if the invoice is not issued yet)" });
    if (!dest) return json(res, 400, { error: "Select where the material is going" });
    if (!lines.length) return json(res, 400, { error: "Add at least one line" });
    if (lines.some((c) => !(c.thk > 0) || !(c.width > 0))) return json(res, 400, { error: "Enter thickness and width for every line" });
    if (lines.some((c) => c.form === "Coil" && !(c.n_coils > 0))) return json(res, 400, { error: "Enter the number of coils for each coil line" });
    if (veh && !VEH.test(veh)) return json(res, 400, { error: "Enter a valid vehicle number, e.g. TN 20 BX 7719" });
    if (veh && !ea) return json(res, 400, { error: "Enter the expected arrival date" });
    const status = veh ? "Loaded" : "Under Loading";
    const dn = doNo || soNo;
    let dsp = (await sql`select * from dispatches where do_no=${dn} and transporter_mobile=${tp.mobile} order by id desc limit 1`)[0];
    if (!dsp) dsp = (await sql`insert into dispatches (token, do_no, transporter_name, transporter_mobile, transporter_id, notified_at) values (${crypto2.randomBytes(9).toString("base64url")}, ${dn}, ${tp.name}, ${tp.mobile}, ${tp.id}, now()) returning *`)[0];
    const last = (await sql`select max(cast(substring(coil_no from 3) as integer)) as m from coils where coil_no ~ '^D-[0-9]+$'`)[0].m || 0;
    let no = last + 1;
    for (const c of lines) {
      const ref = "D-" + String(no++).padStart(4, "0");
      await sql`insert into coils (coil_no, entry_date, so_no, do_no, n_coils, form, grade, thk, width, length, weight, destination, transporter_name, transporter_mobile, transporter_id, status, vehicle_no, lr_no, dispatch_date, expected_arrival, dispatch_id, source, entered_by)
                values (${ref}, ${today}, ${soNo}, ${doNo}, ${c.n_coils}, ${c.form}, ${c.grade}, ${c.thk}, ${c.width}, ${c.length}, ${c.weight}, ${dest}, ${tp.name}, ${/^\d{10,}$/.test(tp.mobile) ? tp.mobile : ""}, ${tp.id}, ${status}, ${veh}, ${lr}, ${veh ? dd : null}, ${ea}, ${dsp.id}, 'transporter', ${tp.name})`;
      await sql`insert into events (coil_no, action, actor, detail) values (${ref}, 'created', ${actor}, ${JSON.stringify({ doNo, soNo, dest, veh, lr })})`;
    }
    return json(res, 200, { ok: true, groups: group(await mine()) });
  }
  const sel = Array.isArray(b.coils) ? b.coils.map(String) : [];
  if (!sel.length) return json(res, 400, { error: "Tick at least one line" });
  const own = tp ? await sql`select coil_no, status, source from coils where coil_no = any(${sel}) and (transporter_id=${tp.id} or (transporter_mobile<>'' and transporter_mobile=${tp.mobile}))` : await sql`select coil_no, status, source from coils where coil_no = any(${sel}) and dispatch_id=${disp.id}`;
  if (own.length !== sel.length) return json(res, 403, { error: "One of the lines is not on this link" });
  if (action === "load") {
    const veh = String(b.vehicle_no || "").toUpperCase().replace(/\s+/g, " ").trim(), lr = String(b.lr_no || "").trim();
    const dd = d(b.dispatch_date) || today, ea = d(b.expected_arrival);
    if (!VEH.test(veh)) return json(res, 400, { error: "Enter a valid vehicle number, e.g. TN 20 BX 7719" });
    if (!ea) return json(res, 400, { error: "Enter the expected arrival date" });
    await sql`update coils set vehicle_no=${veh}, lr_no=${lr}, dispatch_date=${dd}, expected_arrival=${ea}, status = case when status='Under Loading' then 'Loaded' else status end, updated_at=now() where coil_no = any(${sel}) and status <> 'Arrived'`;
    await sql`insert into events (coil_no, action, actor, detail) select unnest(${sel}::text[]), 'load', ${actor}, ${JSON.stringify({ veh, lr, dd, ea })}`;
  } else if (action === "gateout") {
    const blocked = own.filter((c) => c.status === "Under Loading");
    if (blocked.length) return json(res, 400, { error: "Enter truck details first for " + blocked.map((c) => c.coil_no).join(", ") });
    await sql`update coils set status='In-Transit', dispatch_date=coalesce(dispatch_date, ${today}), updated_at=now() where coil_no = any(${sel}) and status='Loaded'`;
    await sql`insert into events (coil_no, action, actor) select unnest(${sel}::text[]), 'gateout', ${actor}`;
  } else if (action === "delivered") {
    await sql`update coils set status='Arrived', actual_arrival=coalesce(actual_arrival, ${d(b.actual_arrival) || today}), updated_at=now() where coil_no = any(${sel}) and status in ('Loaded','In-Transit')`;
    await sql`insert into events (coil_no, action, actor) select unnest(${sel}::text[]), 'delivered', ${actor}`;
  } else if (action === "edit") {
    const editable = own.filter((c) => c.source === "transporter" && c.status !== "Arrived");
    if (editable.length !== own.length) return json(res, 403, { error: "Only your own undelivered entries can be edited" });
    const w = b.weight == null ? null : Number(b.weight), dest = b.destination == null ? null : String(b.destination).trim(), inv = b.do_no == null ? null : String(b.do_no).trim();
    await sql`update coils set weight=coalesce(${w}, weight), destination=coalesce(${dest}, destination), do_no=coalesce(${inv}, do_no), updated_at=now() where coil_no = any(${sel})`;
    await sql`insert into events (coil_no, action, actor, detail) select unnest(${sel}::text[]), 'edit', ${actor}, ${JSON.stringify({ w, dest })}`;
  } else return json(res, 400, { error: "unknown action" });
  json(res, 200, { ok: true, groups: group(await mine()) });
}

// api/arrive.js
async function handler4(req, res) {
  if (cors(req, res)) return;
  if (req.method !== "POST") return json(res, 405, { error: "method" });
  const b = req.body || {};
  if (!process.env.YARD_PIN) return json(res, 503, { error: "YARD_PIN not set" });
  if (String(b.pin || "") !== process.env.YARD_PIN) return json(res, 401, { error: "Wrong PIN" });
  const today = (/* @__PURE__ */ new Date()).toISOString().slice(0, 10);
  let rows;
  if (b.vehicle_no) rows = await sql`update coils set status='Arrived', actual_arrival=coalesce(actual_arrival, ${today}), updated_at=now()
                                       where upper(regexp_replace(vehicle_no,'\s','','g')) = upper(regexp_replace(${String(b.vehicle_no)},'\s','','g')) and status in ('Loaded','In-Transit') returning coil_no`;
  else if (Array.isArray(b.coils)) rows = await sql`update coils set status='Arrived', actual_arrival=coalesce(actual_arrival, ${today}), updated_at=now() where coil_no = any(${b.coils.map(String)}) and status in ('Loaded','In-Transit') returning coil_no`;
  else return json(res, 400, { error: "vehicle_no or coils required" });
  for (const r of rows) await sql`insert into events (coil_no, action, actor) values (${r.coil_no}, 'delivered', 'yard')`;
  json(res, 200, { ok: true, updated: rows.map((r) => r.coil_no) });
}


// wheels.js — settings + WIP exclusions for the Wheels India module (Neon-backed).
async function ensureWheelsTable(){
  await sql`create table if not exists wheels_settings (
    id int primary key default 1,
    mail_to text[] default '{}', mail_cc text[] default '{}',
    send_time text default '10:00', enabled boolean default true,
    wip text[] default '{}', mail_subject text default '', mail_body text default '', updated_at timestamptz default now()
  )`;
  await sql`insert into wheels_settings (id) values (1) on conflict (id) do nothing`;
  await sql`alter table wheels_settings add column if not exists mail_subject text default ''`;
  await sql`alter table wheels_settings add column if not exists mail_body text default ''`;
  await sql`alter table wheels_settings add column if not exists wa_to text[] default '{}'`;
  await sql`alter table wheels_settings add column if not exists wa_enabled boolean default true`;
  await sql`alter table wheels_settings add column if not exists prod_mail_to text[] default '{}'`;
  await sql`alter table wheels_settings add column if not exists prod_wa_to text[] default '{}'`;
  await sql`alter table wheels_settings add column if not exists prod_enabled boolean default true`;
  await sql`alter table wheels_settings add column if not exists prod_format text default 'image'`;
  await sql`alter table wheels_settings add column if not exists holidays text[] default '{}'`;
  // Management group (gets Wheels stock + production report + company-wide Daily Stock report)
  await sql`alter table wheels_settings add column if not exists mgmt_mail_to text[] default '{}'`;
  await sql`alter table wheels_settings add column if not exists mgmt_wa_to text[] default '{}'`;
  await sql`alter table wheels_settings add column if not exists mgmt_enabled boolean default true`;
  await sql`create table if not exists wheels_send_log (
    id bigserial primary key,
    ts timestamptz default now(),
    run_date date default (now() at time zone 'Asia/Kolkata')::date,
    report text,
    channel text,
    status text,
    total int default 0,
    ok_count int default 0,
    fail_count int default 0,
    detail text default ''
  )`;
}
// Append one delivery-log row. Best-effort: never throws, so a logging failure can't break a send.
async function logSend(o){
  try{
    await sql`insert into wheels_send_log (report, channel, status, total, ok_count, fail_count, detail)
      values (${String(o.report||'').slice(0,40)}, ${String(o.channel||'').slice(0,20)}, ${String(o.status||'').slice(0,20)},
              ${Number(o.total||0)}, ${Number(o.ok||0)}, ${Number(o.fail||0)}, ${String(o.detail||'').slice(0,500)})`;
  }catch(e){ /* best-effort; logging must never break a send */ }
}
// Summarise a WATI per-number result array into a log row ({status, ok, fail, detail}).
function waSummary(sent){
  const okc = sent.filter(x=>!x.error).length, failc = sent.length - okc;
  const status = failc===0 ? 'ok' : (okc ? 'partial' : 'fail');
  const detail = failc ? sent.filter(x=>x.error).map(x=>x.num+': '+x.error).join('; ')
                       : sent.length+' recipient(s): '+sent.map(x=>x.num).join(', ');
  return { status, ok:okc, fail:failc, total:sent.length, detail };
}
async function handlerWheels(req, res){
  if (cors(req, res)) return;
  try{
    await ensureWheelsTable();
    // GET /api/wheels?fmt=pdf  ->  the statement as a PDF (same data/structure as the email).
    // Public, no-auth: this is the URL WATI fetches for the WhatsApp document template.
    const fmt = (req.query && req.query.fmt) || ((/[?&]fmt=(pdf|xlsx|stockpng|prodpdf|prodpng|dailypng|wadiag|sendlog)\b/.exec(req.url || "")) || [])[1] || "";
    if (req.method === "GET" && fmt === "pdf"){
      const pdf = await buildWheelsPdf();
      res.setHeader("Content-Type", "application/pdf");
      res.setHeader("Content-Disposition", 'inline; filename="WheelsIndia_Stock_' + new Date().toISOString().slice(0,10).replace(/-/g,"") + '.pdf"');
      res.setHeader("Cache-Control", "no-store");
      return res.status(200).end(pdf);
    }
    if (req.method === "GET" && fmt === "xlsx"){
      const b64 = await buildWheelsXlsx(await wheelsRows());
      res.setHeader("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet");
      res.setHeader("Content-Disposition", 'attachment; filename="WheelsIndia_Stock_' + new Date().toISOString().slice(0,10).replace(/-/g,"") + '.xlsx"');
      res.setHeader("Cache-Control", "no-store");
      return res.status(200).end(Buffer.from(b64, "base64"));
    }
    if (req.method === "GET" && fmt === "stockpng"){
      const png = await buildWheelsStockPng();
      res.setHeader("Content-Type", "image/png");
      res.setHeader("Content-Disposition", 'inline; filename="WheelsIndia_Stock_' + new Date().toISOString().slice(0,10).replace(/-/g,"") + '.png"');
      res.setHeader("Cache-Control", "no-store");
      return res.status(200).end(png);
    }
    if (req.method === "GET" && fmt === "prodpdf"){
      const pdf = await buildWheelsProdPdf();
      res.setHeader("Content-Type", "application/pdf");
      res.setHeader("Content-Disposition", 'inline; filename="WheelsIndia_Production_' + new Date().toISOString().slice(0,10).replace(/-/g,"") + '.pdf"');
      res.setHeader("Cache-Control", "no-store");
      return res.status(200).end(pdf);
    }
    if (req.method === "GET" && fmt === "prodpng"){
      const png = await buildWheelsProdPng();
      res.setHeader("Content-Type", "image/png");
      res.setHeader("Content-Disposition", 'inline; filename="WheelsIndia_Production_' + new Date().toISOString().slice(0,10).replace(/-/g,"") + '.png"');
      res.setHeader("Cache-Control", "no-store");
      return res.status(200).end(png);
    }
    if (req.method === "GET" && fmt === "dailypng"){
      const png = await buildDailyStockPng();
      res.setHeader("Content-Type", "image/png");
      res.setHeader("Content-Disposition", 'inline; filename="BSC_DailyStock_' + new Date().toISOString().slice(0,10).replace(/-/g,"") + '.png"');
      res.setHeader("Cache-Control", "no-store");
      return res.status(200).end(png);
    }
    // Delivery log: recent WhatsApp/email send results for the daily reminders (read by wheels/log.html).
    if (req.method === "GET" && fmt === "sendlog"){
      let rows = [];
      try { rows = await sql`select ts, run_date, report, channel, status, total, ok_count, fail_count, detail
                             from wheels_send_log order by id desc limit 300`; }
      catch(e){ return json(res, 200, { rows: [], error: String(e.message||e) }); }
      return json(res, 200, { rows });
    }
    // Diagnostic: which WhatsApp env vars does this deployment actually see? (names/booleans only, no secrets)
    if (req.method === "GET" && fmt === "wadiag"){
      let st = {};
      try { const dr = await sql`select wa_to, wa_enabled, prod_wa_to, prod_enabled, prod_format, holidays from wheels_settings where id=1`; st = dr[0] || {}; } catch(e){ st = { _err:String(e.message||e) }; }
      return json(res, 200, {
        // --- env (names/booleans only, no secrets) ---
        WHEELS_WA_TEMPLATE: process.env.WHEELS_WA_TEMPLATE || null,
        WHEELS_WA_PROD_TEMPLATE: process.env.WHEELS_WA_PROD_TEMPLATE || null,
        WHEELS_WA_DAILY_TEMPLATE: process.env.WHEELS_WA_DAILY_TEMPLATE || null,
        WATI_API_URL: !!watiBase(),
        WATI_key_present: !!watiKey(),
        CRON_SECRET_present: !!process.env.CRON_SECRET,
        wati_url_names: { WATI_API_URL: !!process.env.WATI_API_URL, WATI_ENDPOINT: !!process.env.WATI_ENDPOINT, WATI_BASE_URL: !!process.env.WATI_BASE_URL },
        wati_key_names: { WATI_API_KEY: !!process.env.WATI_API_KEY, WATI_ACCESS_TOKEN: !!process.env.WATI_ACCESS_TOKEN, WATI_TOKEN: !!process.env.WATI_TOKEN },
        // --- live settings the daily trigger reads (this is what decides whether it actually sends) ---
        stock_wa_enabled: st.wa_enabled !== false,
        stock_wa_numbers: (st.wa_to || []).length,
        prod_enabled: st.prod_enabled !== false,
        prod_wa_numbers: (st.prod_wa_to || []).length,
        prod_format: st.prod_format || "image",
        holidays_count: (st.holidays || []).length,
        settings_error: st._err || null
      });
    }
    if (req.method === "POST"){
      let body = req.body;
      if (typeof body === "string") { try { body = JSON.parse(body); } catch { body = {}; } }
      body = body || {};
      // Delivery-log ingest — the daily PowerShell scripts POST here after each EMAIL send so the
      // Send Log page shows email status too (WhatsApp is logged by the portal itself below). CRON_SECRET-guarded.
      if (body.log_add) {
        const auth = req.headers.authorization || "";
        if (!process.env.CRON_SECRET) return json(res, 503, { error: "CRON_SECRET not set" });
        if (auth !== "Bearer " + process.env.CRON_SECRET) return json(res, 401, { error: "unauthorized" });
        const okc = Number(body.ok||0), failc = Number(body.fail||0), tot = Number(body.total || (okc+failc));
        const status = String(body.status || (failc ? (okc ? 'partial' : 'fail') : 'ok'));
        await logSend({ report: body.report, channel: body.channel || 'email', status, total: tot, ok: okc, fail: failc, detail: body.detail });
        return json(res, 200, { ok: true });
      }
      // WhatsApp send trigger (fired by the daily script after the email). Guarded by CRON_SECRET.
      // Creds + template come from Vercel env (WATI_API_URL, WATI_API_KEY, WHEELS_WA_TEMPLATE);
      // recipient numbers come from the in-app settings (wa_to). PDF is served at /api/wheels?fmt=pdf.
      if (body.whatsapp) {
        const auth = req.headers.authorization || "";
        if (!process.env.CRON_SECRET) return json(res, 503, { error: "CRON_SECRET not set" });
        if (auth !== "Bearer " + process.env.CRON_SECRET) return json(res, 401, { error: "unauthorized" });
        const tmpl = process.env.WHEELS_WA_TEMPLATE;
        if (!tmpl) return json(res, 400, { error: "WHEELS_WA_TEMPLATE not set" });
        const r3 = await sql`select wa_to, wa_enabled, mgmt_wa_to, mgmt_enabled, prod_wa_to, prod_enabled from wheels_settings where id=1`;
        const s3 = r3[0] || {};
        // Wheels India Stock on WhatsApp goes to Client + Management + Production, each gated by its own switch.
        const clean = a => (a || []).map(String).map(x => x.trim()).filter(Boolean);
        const stockSet = [
          ...(s3.wa_enabled === false ? [] : clean(s3.wa_to)),
          ...(s3.mgmt_enabled === false ? [] : clean(s3.mgmt_wa_to)),
          ...(s3.prod_enabled === false ? [] : clean(s3.prod_wa_to))
        ];
        const nums = [...new Set(stockSet)];
        if (!nums.length) { await logSend({report:'wheels_stock', channel:'whatsapp', status:'skipped', detail:'no recipients (client/management/production all empty or disabled)'}); return json(res, 200, { ok: true, skipped: "no whatsapp recipients (client/management/production all empty or disabled)" }); }
        const base = (process.env.PUBLIC_URL || `https://${req.headers.host}`).replace(/\/$/, "");
        const sent = [];
        const img = base + "/wheels.png?d=" + imgStamp();
        for (const num of nums) {
          try { const r = await send(num, tmpl, [{ name: "report", value: img }]); sent.push({ num, ...(r || { ok: true }) }); }   // image header "report" + static URL buttons
          catch (e) { sent.push({ num, error: String(e.message || e) }); }
        }
        await logSend({ report:'wheels_stock', channel:'whatsapp', ...waSummary(sent) });
        return json(res, 200, { ok: true, image: img, pdf: base + "/wheels.pdf", xlsx: base + "/wheels.xlsx", template: tmpl, sent });
      }
      // WhatsApp test send (triggered from the settings page, like the email test — no CRON_SECRET).
      // Sends to wa_test_to if given, else the first saved number. Uses the same env creds/template.
      if (body.whatsapp_test) {
        const tmpl = process.env.WHEELS_WA_TEMPLATE;
        if (!tmpl) return json(res, 200, { ok: true, skipped: "WHEELS_WA_TEMPLATE not set on server" });
        if (!watiBase() || !watiKey()) return json(res, 200, { ok: true, skipped: "WATI not configured on server (need WATI_API_URL + token)" });
        let num = String(body.wa_test_to || "").trim();
        if (!num) {
          const r4 = await sql`select wa_to from wheels_settings where id=1`;
          num = ((r4[0]?.wa_to) || []).map(String)[0] || "";
        }
        if (!num) return json(res, 200, { ok: true, skipped: "no test number (enter one or save a number first)" });
        const base = (process.env.PUBLIC_URL || `https://${req.headers.host}`).replace(/\/$/, "");
        const img = base + "/wheels.png?d=" + imgStamp();
        const sent = [];
        try { const r = await send(num, tmpl, [{ name: "report", value: img }]); sent.push({ num, ...(r || { ok: true }) }); }   // image header "report" + static URL buttons
        catch (e) { sent.push({ num, error: String(e.message || e) }); }
        return json(res, 200, { ok: true, image: img, pdf: base + "/wheels.pdf", xlsx: base + "/wheels.xlsx", template: tmpl, sent });
      }
      // Production WhatsApp — daily trigger (CRON_SECRET). Template WHEELS_WA_PROD_TEMPLATE, numbers from prod_wa_to,
      // media = /wheels-production.(png|pdf) per prod_format. Header variable must be named "report".
      if (body.prod_whatsapp) {
        const auth = req.headers.authorization || "";
        if (!process.env.CRON_SECRET) return json(res, 503, { error: "CRON_SECRET not set" });
        if (auth !== "Bearer " + process.env.CRON_SECRET) return json(res, 401, { error: "unauthorized" });
        const tmpl = process.env.WHEELS_WA_PROD_TEMPLATE;
        if (!tmpl) return json(res, 400, { error: "WHEELS_WA_PROD_TEMPLATE not set" });
        const r5 = await sql`select prod_wa_to, prod_enabled, prod_format, mgmt_wa_to, mgmt_enabled from wheels_settings where id=1`;
        const s5 = r5[0] || {};
        // Production report on WhatsApp goes to Management + Production, each gated by its own switch.
        const cleanP = a => (a || []).map(String).map(x => x.trim()).filter(Boolean);
        const prodSet = [
          ...(s5.mgmt_enabled === false ? [] : cleanP(s5.mgmt_wa_to)),
          ...(s5.prod_enabled === false ? [] : cleanP(s5.prod_wa_to))
        ];
        const nums = [...new Set(prodSet)];
        if (!nums.length) { await logSend({report:'production', channel:'whatsapp', status:'skipped', detail:'no recipients (management/production empty or disabled)'}); return json(res, 200, { ok: true, skipped: "no production whatsapp recipients (management/production empty or disabled)" }); }
        const base = (process.env.PUBLIC_URL || `https://${req.headers.host}`).replace(/\/$/, "");
        const url = base + "/wheels-production.png?d=" + imgStamp(); // WhatsApp always image (dated to force fresh fetch); email respects the pdf/image toggle
        const sent = [];
        for (const num of nums) { try { const r = await send(num, tmpl, [{ name: "report", value: url }]); sent.push({ num, ...(r || { ok: true }) }); } catch (e) { sent.push({ num, error: String(e.message || e) }); } }
        await logSend({ report:'production', channel:'whatsapp', ...waSummary(sent) });
        return json(res, 200, { ok: true, url, template: tmpl, sent });
      }
      // Production email — test send (and reused by nothing else). Renders the chosen format and emails it.
      if (body.prod_mail_test) {
        const r7 = await sql`select prod_mail_to, prod_format from wheels_settings where id=1`;
        const s7 = r7[0] || {};
        const to = body.test_to ? [String(body.test_to)] : (s7.prod_mail_to || []);
        if (!to.length) return json(res, 400, { error: "no recipient" });
        const fmtv = (s7.prod_format) || "image";
        const stamp = new Date().toISOString().slice(0,10).replace(/-/g,"");
        let attach, html;
        if (fmtv === "pdf") {
          attach = { name:"WheelsIndia_Production_"+stamp+".pdf", b64:(await buildWheelsProdPdf()).toString("base64"), contentType:"application/pdf" };
          html = "<p>Dear Team,</p><p>Please find attached the Wheels India Production Report (PDF).</p><p>Bharat Steel (Chennai) Pvt. Ltd.</p>";
        } else {
          attach = { name:"WheelsIndia_Production_"+stamp+".png", b64:(await buildWheelsProdPng()).toString("base64"), contentType:"image/png" };
          html = `<p>Dear Team,</p><p>Wheels India Production Report:</p><img src="cid:prodimg" style="max-width:100%;border:1px solid #d0d7dd"/><p>Bharat Steel (Chennai) Pvt. Ltd.</p>`;
          attach.cid = "prodimg";
        }
        await sendWheelsMail(to, [], html, (body.test_to?"[TEST] ":"")+"Wheels India — Production Report — "+new Date().toLocaleDateString("en-IN"), attach);
        return json(res, 200, { ok:true, sent_to:to, format:fmtv });
      }
      // Production WhatsApp — test send from the settings page (no CRON_SECRET)
      if (body.prod_whatsapp_test) {
        const tmpl = process.env.WHEELS_WA_PROD_TEMPLATE;
        if (!tmpl) return json(res, 200, { ok: true, skipped: "WHEELS_WA_PROD_TEMPLATE not set on server" });
        if (!watiBase() || !watiKey()) return json(res, 200, { ok: true, skipped: "WATI not configured on server (need WATI_API_URL + token)" });
        const r6 = await sql`select prod_wa_to, prod_format from wheels_settings where id=1`;
        const s6 = r6[0] || {};
        let num = String(body.prod_wa_test_to || "").trim();
        if (!num) num = (s6.prod_wa_to || []).map(String)[0] || "";
        if (!num) return json(res, 200, { ok: true, skipped: "no test number (enter one or save a number first)" });
        const base = (process.env.PUBLIC_URL || `https://${req.headers.host}`).replace(/\/$/, "");
        const url = base + "/wheels-production.png?d=" + imgStamp(); // WhatsApp always image
        const sent = [];
        try { const r = await send(num, tmpl, [{ name: "report", value: url }]); sent.push({ num, ...(r || { ok: true }) }); } catch (e) { sent.push({ num, error: String(e.message || e) }); }
        return json(res, 200, { ok: true, url, template: tmpl, sent });
      }
      // Daily Stock (company-wide) WhatsApp — daily trigger (CRON_SECRET). Goes to Management only.
      // Template WHEELS_WA_DAILY_TEMPLATE (image header, variable "report" = /daily-stock.png).
      if (body.daily_whatsapp) {
        const auth = req.headers.authorization || "";
        if (!process.env.CRON_SECRET) return json(res, 503, { error: "CRON_SECRET not set" });
        if (auth !== "Bearer " + process.env.CRON_SECRET) return json(res, 401, { error: "unauthorized" });
        const tmpl = process.env.WHEELS_WA_DAILY_TEMPLATE || process.env.WHEELS_WA_PROD_TEMPLATE;
        if (!tmpl) return json(res, 400, { error: "WHEELS_WA_DAILY_TEMPLATE not set" });
        const rd = await sql`select mgmt_wa_to, mgmt_enabled from wheels_settings where id=1`;
        const sd = rd[0] || {};
        if (sd.mgmt_enabled === false) { await logSend({report:'daily_stock', channel:'whatsapp', status:'skipped', detail:'management disabled in settings'}); return json(res, 200, { ok: true, skipped: "management disabled in settings" }); }
        const nums = [...new Set((sd.mgmt_wa_to || []).map(String).map(x => x.trim()).filter(Boolean))];
        if (!nums.length) { await logSend({report:'daily_stock', channel:'whatsapp', status:'skipped', detail:'no management whatsapp recipients'}); return json(res, 200, { ok: true, skipped: "no management whatsapp recipients" }); }
        const base = (process.env.PUBLIC_URL || `https://${req.headers.host}`).replace(/\/$/, "");
        const url = base + "/daily-stock.png?d=" + imgStamp();
        const sent = [];
        for (const num of nums) { try { const r = await send(num, tmpl, [{ name: "report", value: url }]); sent.push({ num, ...(r || { ok: true }) }); } catch (e) { sent.push({ num, error: String(e.message || e) }); } }
        await logSend({ report:'daily_stock', channel:'whatsapp', ...waSummary(sent) });
        return json(res, 200, { ok: true, url, template: tmpl, sent });
      }
      // Daily Stock WhatsApp — test send from the settings page (no CRON_SECRET)
      if (body.daily_whatsapp_test) {
        const tmpl = process.env.WHEELS_WA_DAILY_TEMPLATE || process.env.WHEELS_WA_PROD_TEMPLATE;
        if (!tmpl) return json(res, 200, { ok: true, skipped: "WHEELS_WA_DAILY_TEMPLATE not set on server" });
        if (!watiBase() || !watiKey()) return json(res, 200, { ok: true, skipped: "WATI not configured on server (need WATI_API_URL + token)" });
        const rd = await sql`select mgmt_wa_to from wheels_settings where id=1`;
        const sd = rd[0] || {};
        let num = String(body.daily_wa_test_to || "").trim();
        if (!num) num = (sd.mgmt_wa_to || []).map(String)[0] || "";
        if (!num) return json(res, 200, { ok: true, skipped: "no test number (enter one or save a management number first)" });
        const base = (process.env.PUBLIC_URL || `https://${req.headers.host}`).replace(/\/$/, "");
        const url = base + "/daily-stock.png?d=" + imgStamp();
        const sent = [];
        try { const r = await send(num, tmpl, [{ name: "report", value: url }]); sent.push({ num, ...(r || { ok: true }) }); } catch (e) { sent.push({ num, error: String(e.message || e) }); }
        return json(res, 200, { ok: true, url, template: tmpl, sent });
      }
      // Daily Stock email — test send from the settings page
      if (body.daily_mail_test) {
        const rd = await sql`select mgmt_mail_to from wheels_settings where id=1`;
        const sd = rd[0] || {};
        const to = body.test_to ? [String(body.test_to)] : (sd.mgmt_mail_to || []);
        if (!to.length) return json(res, 400, { error: "no recipient (enter one or save a management email first)" });
        const stamp = new Date().toISOString().slice(0,10).replace(/-/g,"");
        const png = (await buildDailyStockPng()).toString("base64");
        const html = `<p>Dear Team,</p><p>Company-wide Daily Stock report:</p><img src="cid:dailyimg" style="max-width:100%;border:1px solid #d0d7dd"/><p>Bharat Steel (Chennai) Pvt. Ltd.</p>`;
        const attach = { name:"BSC_DailyStock_"+stamp+".png", b64:png, contentType:"image/png", cid:"dailyimg" };
        await sendWheelsMail(to, [], html, (body.test_to?"[TEST] ":"")+"BSC — Daily Stock Report — "+new Date().toLocaleDateString("en-IN"), attach);
        return json(res, 200, { ok:true, sent_to:to });
      }
      // update only the keys provided (settings save and WIP save are separate posts)
      if (Array.isArray(body.mail_to)) await sql`update wheels_settings set mail_to=${body.mail_to} where id=1`;
      if (Array.isArray(body.mail_cc)) await sql`update wheels_settings set mail_cc=${body.mail_cc} where id=1`;
      if (typeof body.send_time === "string") await sql`update wheels_settings set send_time=${body.send_time} where id=1`;
      if (typeof body.enabled === "boolean") await sql`update wheels_settings set enabled=${body.enabled} where id=1`;
      if (typeof body.mail_subject === "string") await sql`update wheels_settings set mail_subject=${body.mail_subject} where id=1`;
      if (typeof body.mail_body === "string") await sql`update wheels_settings set mail_body=${body.mail_body} where id=1`;
      if (Array.isArray(body.wip)) await sql`update wheels_settings set wip=${body.wip.map(String)} where id=1`;
      if (Array.isArray(body.wa_to)) await sql`update wheels_settings set wa_to=${body.wa_to.map(String)} where id=1`;
      if (typeof body.wa_enabled === "boolean") await sql`update wheels_settings set wa_enabled=${body.wa_enabled} where id=1`;
      if (Array.isArray(body.prod_mail_to)) await sql`update wheels_settings set prod_mail_to=${body.prod_mail_to.map(String)} where id=1`;
      if (Array.isArray(body.prod_wa_to)) await sql`update wheels_settings set prod_wa_to=${body.prod_wa_to.map(String)} where id=1`;
      if (typeof body.prod_enabled === "boolean") await sql`update wheels_settings set prod_enabled=${body.prod_enabled} where id=1`;
      if (body.prod_format==="pdf"||body.prod_format==="image") await sql`update wheels_settings set prod_format=${body.prod_format} where id=1`;
      if (Array.isArray(body.holidays)) await sql`update wheels_settings set holidays=${body.holidays.map(s=>String(s).slice(0,10)).filter(s=>/^\d{4}-\d{2}-\d{2}$/.test(s))} where id=1`;
      if (Array.isArray(body.mgmt_mail_to)) await sql`update wheels_settings set mgmt_mail_to=${body.mgmt_mail_to.map(String)} where id=1`;
      if (Array.isArray(body.mgmt_wa_to)) await sql`update wheels_settings set mgmt_wa_to=${body.mgmt_wa_to.map(String)} where id=1`;
      if (typeof body.mgmt_enabled === "boolean") await sql`update wheels_settings set mgmt_enabled=${body.mgmt_enabled} where id=1`;
      // test send: render from the last CSV and email to the given address (or configured To)
      if (body.send) {
        const rows = await sql`select mail_to, mail_cc from wheels_settings where id=1`;
        const s2 = rows[0] || {};
        const wipSet = new Set();   // WIP exclusion removed — all stock is shown
        const base = (process.env.PUBLIC_URL || `https://${req.headers.host}`).replace(/\/$/, "");
        const note = typeof body.mail_body === "string" ? body.mail_body : (s2.mail_body||"");
        const { html, gb, gq, rows: wrows } = await renderWheelsHtml(base, wipSet, note);
        const to = body.test_to ? [String(body.test_to)] : (s2.mail_to||[]);
        const cc = body.test_to ? [] : (s2.mail_cc||[]);
        if (!to.length) return json(res, 400, { error: "no recipient" });
        const baseSubj = (body.mail_subject || s2.mail_subject || "BSC — Wheels India Job Work Stock Statement — " + new Date().toLocaleDateString("en-IN"));
        const subj = (body.test_to ? "[TEST] " : "") + baseSubj;
        let attach = null, attachErr = null;
        try {
          const xlsxB64 = await buildWheelsXlsx(wrows);
          attach = { name: "WheelsIndia_Stock_" + new Date().toISOString().slice(0,10).replace(/-/g,"") + ".xlsx", b64: xlsxB64 };
        } catch(e){ attachErr = String(e.message||e); }
        await sendWheelsMail(to, cc, html, subj, attach);
        await sql`update wheels_settings set updated_at=now() where id=1`;
        return json(res, 200, { ok: true, sent_to: to, items: gb, mt: Number(gq.toFixed(3)), attached: !!attach, attach_error: attachErr });
      }
      await sql`update wheels_settings set updated_at=now() where id=1`;
      return json(res, 200, { ok: true });
    }
    const rows = await sql`select mail_to, mail_cc, send_time, enabled, wip, mail_subject, mail_body, wa_to, wa_enabled, prod_mail_to, prod_wa_to, prod_enabled, prod_format, holidays, mgmt_mail_to, mgmt_wa_to, mgmt_enabled from wheels_settings where id=1`;
    const s = rows[0] || {};
    return json(res, 200, {
      mail_to: s.mail_to || [], mail_cc: s.mail_cc || [],
      send_time: s.send_time || "10:00", enabled: s.enabled !== false,
      wip: s.wip || [], mail_subject: s.mail_subject || "", mail_body: s.mail_body || "",
      wa_to: s.wa_to || [], wa_enabled: s.wa_enabled !== false,
      prod_mail_to: s.prod_mail_to || [], prod_wa_to: s.prod_wa_to || [],
      prod_enabled: s.prod_enabled !== false, prod_format: s.prod_format || "image",
      holidays: s.holidays || [],
      mgmt_mail_to: s.mgmt_mail_to || [], mgmt_wa_to: s.mgmt_wa_to || [], mgmt_enabled: s.mgmt_enabled !== false
    });
  }catch(e){
    return json(res, 500, { error: String(e.message || e) });
  }
}


// wheels-mail.js — render the branded statement from the last-pushed CSV and send via Graph.
function wheelsFormOf(n){ n=(n||"").toUpperCase();
  if(n.includes("SCRAP"))return "SCRAP";
  if(n.includes("BLANK")||n.includes("FLAT")||n.includes("STRIP"))return "STRIP";
  if(n.includes("PLATE"))return "PLATE";
  if(n.includes("COIL"))return "COIL"; return "OTHER"; }
function parseCsvSrv(t){ if(!t)return[]; if(t.charCodeAt(0)===0xFEFF)t=t.slice(1);
  const rows=[];let row=[],cur="",q=false;
  for(let i=0;i<t.length;i++){const c=t[i];
    if(q){if(c==='"'){if(t[i+1]==='"'){cur+='"';i++;}else q=false;}else cur+=c;}
    else{if(c==='"')q=true;else if(c===",")row.push(cur),cur="";else if(c==="\n")row.push(cur),rows.push(row),row=[],cur="";else if(c==="\r"){}else cur+=c;}}
  if(cur!==""||row.length){row.push(cur);rows.push(row);}
  const h=rows.shift().map(x=>x.trim());
  return rows.filter(r=>r.length>1).map(r=>{const o={};h.forEach((k,i)=>o[k]=(r[i]??"").trim());return o;}); }
function dimS(v){ const n=parseFloat(v); return isNaN(n)?"":(""+ +n.toFixed(2)); }
function lenS(v){ const n=parseFloat(v); return (n&&n>0)?(""+ +n.toFixed(0)):"—"; }
function esc2(s){ return String(s??"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }

async function renderWheelsHtml(baseUrl, wipSet, note){
  const DATA_BASE = process.env.DATA_BASE || "https://raw.githubusercontent.com/BSC23609/bsc-stock-data/main";
  const csvRes = await fetch(DATA_BASE + "/wheels_stock.csv?t=" + Date.now());
  const rows = parseCsvSrv(csvRes.ok ? await csvRes.text() : "").map(r=>({...r, Quantity:parseFloat(r.Quantity)||0, form:wheelsFormOf(r.ItemName)}))
    .filter(r=>!wipSet.has(String(r.BatchNum)))
    .filter(r=>!((r.form==="PLATE"&&r.Quantity<0.1)||(r.form==="STRIP"&&r.Quantity<0.011))); // plate <0.1 MT, strip <0.011 MT not listed
  const SEC=[["PLATE","Plate Stock (WIP / FG)","#0e7a4b"],["STRIP","Strip / Blank Stock","#b5651d"],["COIL","Coil Stock","#1367a6"]]; // WIP/FG (plate+strip) first, coil last
  let body="", gb=0, gq=0;
  for(const [k,t,c] of SEC){
    const rs=rows.filter(r=>r.form===k).sort((a,b)=>b.Quantity-a.Quantity); if(!rs.length)continue;
    const tq=rs.reduce((a,r)=>a+r.Quantity,0); gb+=rs.length; gq+=tq;
    body+=`<tr><td colspan="9" style="background:${c};color:#fff;font-weight:700;padding:7px 10px;font-size:13px">${t}<span style="float:right">${rs.length} items &middot; ${tq.toFixed(3)} MT</span></td></tr>`+
      `<tr style="background:#eef3f7"><th>#</th><th>Mill</th><th>Grade</th><th>Thk</th><th>Width</th><th>Length</th><th>Coil / Batch No</th><th>Qty (MT)</th><th>Aging (d)</th></tr>`;
    rs.forEach((x,i)=>{ const mill=(x.ItemName||"").split(" ")[0];
      const bc=(+x.BatchCount>1)?` <span style="color:#999">(${x.BatchCount} batches)</span>`:"";
      body+=`<tr><td style="text-align:center;color:#888">${i+1}</td><td style="text-align:center">${esc2(mill)}</td><td style="text-align:center">${esc2(x.Grade)}</td><td style="text-align:center">${dimS(x.Thick)}</td><td style="text-align:center">${dimS(x.Width)}</td><td style="text-align:center">${lenS(x.Length)}</td><td style="text-align:center;font-family:monospace;font-size:11px">${esc2(x.CoilNo||x.BatchNum)}${bc}</td><td style="text-align:center;font-weight:600">${x.Quantity.toFixed(3)}</td><td style="text-align:center;color:#888">${esc2(x.AgeDays)}</td></tr>`; });
    body+=`<tr><td colspan="8" style="text-align:right;font-weight:700;padding:6px 10px;border-top:2px solid #333">${t} total</td><td style="text-align:right;font-weight:700;border-top:2px solid #333">${tq.toFixed(3)}</td><td style="border-top:2px solid #333"></td></tr>`;
  }
  const asof=new Date().toLocaleString("en-IN",{timeZone:"Asia/Kolkata",day:"2-digit",month:"short",year:"numeric",hour:"2-digit",minute:"2-digit"});
  const noteHtml = note && note.trim() ? `<div style="padding:14px 22px 0;font-size:13px;color:#222;line-height:1.6">${esc2(note).replace(/\n/g,"<br>")}</div>` : "";
  const html=`<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>@media only screen and (max-width:600px){.wk-tbl{font-size:10px !important}.wk-tbl th,.wk-tbl td{padding:2px 3px !important}.wk-pad{padding:12px 8px 4px !important}.wk-hd{padding:12px 12px !important}}</style></head><body style="margin:0;padding:0;background:#fff;-webkit-text-size-adjust:100%">`+
    `<div style="font-family:Arial,Helvetica,sans-serif;color:#1a1a1a;max-width:920px;width:100%;margin:auto;border:1px solid #d0d7dd;box-sizing:border-box">
    <div style="background:#0e2a47;color:#fff;padding:14px 22px"><div style="font-size:18px;font-weight:700">Job Work Stock Statement &mdash; Wheels India Limited</div><div style="font-size:12px;opacity:.85;margin-top:3px">Bharat Steel (Chennai) Pvt. Ltd. &middot; Material held at BSC on customer's account</div></div>
    ${noteHtml}
    <div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;padding:11px 22px;background:#f7f9fb;border-bottom:1px solid #e3e8ec;font-size:13px"><div>Customer: <b>Wheels India Limited</b></div><div>As on: <b>${asof}</b></div><div>Warehouse: <b>Job Work (WH 45)</b></div></div>
    <div class="wk-pad" style="padding:14px 22px 4px;overflow-x:auto;-webkit-overflow-scrolling:touch"><table class="wk-tbl" style="width:100%;border-collapse:collapse;font-size:12px">${body}</table></div>
    <div style="margin:14px 22px;padding:12px 16px;background:#0e2a47;color:#fff;border-radius:5px;display:flex;justify-content:space-between;font-size:15px;font-weight:700"><span>TOTAL MATERIAL HELD</span><span>${gb} items &middot; ${gq.toFixed(3)} MT</span></div>
    <div style="margin:0 22px 14px;padding:9px 13px;background:#fff8e1;border:1px solid #f0dc9a;color:#6b5200;font-size:11px;border-radius:5px">Plates currently under process (awaiting shearing) are excluded from this statement.</div>
    <div style="padding:6px 22px;font-size:9px;color:#aaa;font-style:italic">Computer generated Statement - may contain mistakes</div><div style="padding:16px 22px;font-size:11px;color:#667;border-top:1px solid #e3e8ec;line-height:1.6"><b>Bharat Steel (Chennai) Pvt. Ltd.</b> &middot; No.147, Survey No.133, Thirunilai Village, Ponneri Taluk, Chennai 600103 &middot; 044 6791 7800 &middot; info@bharatsteels.in<br>System-generated statement of your material held at BSC as on the date/time shown.</div></div></body></html>`;
  return { html, gb, gq, rows };
}

async function buildWheelsXlsx(rows){
  let XLSX;
  try { XLSX = await import("xlsx"); } catch(e){ throw new Error("xlsx module not available on server: "+e.message); }
  const SEC=[["PLATE","Plate Stock (WIP / FG)"],["STRIP","Strip / Blank Stock"],["COIL","Coil Stock"]]; // WIP/FG (plate+strip) first, coil last
  const aoa=[["BHARAT STEEL (CHENNAI) PRIVATE LIMITED"],
             ["Job Work Stock Statement — Wheels India Limited"],
             ["As on: "+new Date().toLocaleString("en-IN",{timeZone:"Asia/Kolkata"})+"   |   Customer: Wheels India Limited   |   Warehouse: Job Work (WH 45)"],
             []];
  const hdr=["#","Mill","Grade","Thk","Width","Length","Coil / Batch No","Mother Coil","Qty (MT)","Aging (d)"];
  let gGrand=0, secRanges=[];
  for(const [k,t] of SEC){
    const rs=rows.filter(r=>r.form===k).sort((a,b)=>b.Quantity-a.Quantity); if(!rs.length)continue;
    const tq=rs.reduce((a,r)=>a+r.Quantity,0); gGrand+=tq;
    aoa.push([`${t}   (${rs.length} items, ${tq.toFixed(3)} MT)`]);
    aoa.push(hdr);
    rs.forEach((x,i)=>{ const mill=(x.ItemName||"").split(" ")[0];
      const T=parseFloat(x.Thick), W=parseFloat(x.Width), L=parseFloat(x.Length);
      aoa.push([i+1, mill, x.Grade, isNaN(T)?"":T, isNaN(W)?"":W, (L>0?L:""), (x.CoilNo||x.BatchNum), (x.Mothercoil||"-"), Number(x.Quantity.toFixed(3)), x.AgeDays?parseInt(x.AgeDays):""]); });
    aoa.push(["","","","","","","","",Number(tq.toFixed(3)),""]);
    aoa.push([]);
  }
  aoa.push(["TOTAL MATERIAL HELD","","","","","","","",Number(gGrand.toFixed(3)),""]);
  aoa.push([]);
  aoa.push(["System generated statement - might have mistakes"]);
  const ws=XLSX.utils.aoa_to_sheet(aoa);
  ws["!cols"]=[{wch:4},{wch:10},{wch:16},{wch:7},{wch:8},{wch:9},{wch:22},{wch:22},{wch:11},{wch:7}];
  const wb=XLSX.utils.book_new(); XLSX.utils.book_append_sheet(wb,ws,"Stock Statement");
  return XLSX.write(wb,{type:"base64",bookType:"xlsx"});
}
// Fetch + filter the current Wheels India stock rows from the data repo (shared by PDF/XLSX).
async function wheelsRows(){
  const DATA_BASE = process.env.DATA_BASE || "https://raw.githubusercontent.com/BSC23609/bsc-stock-data/main";
  const csvRes = await fetch(DATA_BASE + "/wheels_stock.csv?t=" + Date.now());
  return parseCsvSrv(csvRes.ok ? await csvRes.text() : "").map(r=>({...r, Quantity:parseFloat(r.Quantity)||0, form:wheelsFormOf(r.ItemName)}))
    .filter(r=>!((r.form==="PLATE"&&r.Quantity<0.1)||(r.form==="STRIP"&&r.Quantity<0.011))); // plate <0.1 MT, strip <0.011 MT not listed
}
// Build the statement as a PDF (same sections/columns as the email). Pure pdfkit,
// no headless browser — safe on Vercel Hobby. Served at /api/wheels?fmt=pdf (and /wheels.pdf).
async function buildWheelsPdf(){
  let PDFDocument;
  try { PDFDocument = (await import("pdfkit")).default; } catch(e){ throw new Error("pdfkit module not available on server: "+e.message); }
  const rows = await wheelsRows();
  const SECDEF = {
    COIL:  ["Coil Stock","#1367a6"],
    PLATE: ["Plate Stock (WIP / FG)","#0e7a4b"],
    STRIP: ["Strip / Blank Stock","#b5651d"]
  };
  // Page 1 = WIP / FG (plate + strip) ; Page 2 = Coil Stock
  const PAGES = [
    { title:"WIP / FG Stock", forms:["PLATE","STRIP"] },
    { title:"Coil Stock",     forms:["COIL"] }
  ];

  const doc = new PDFDocument({ size:"A4", margin:28 });
  const chunks=[]; doc.on("data",c=>chunks.push(c));
  const done = new Promise(r=>doc.on("end",r));

  const L=28, R=567, W=R-L;                        // A4 = 595pt wide, 28pt margins
  const cols=[
    {k:"n",   t:"#",              w:22, a:"center"},
    {k:"mill",t:"Mill",           w:50, a:"center"},
    {k:"grade",t:"Grade",         w:92, a:"left"},
    {k:"thk", t:"Thk",            w:38, a:"center"},
    {k:"wid", t:"Width",          w:44, a:"center"},
    {k:"len", t:"Length",         w:48, a:"center"},
    {k:"coil",t:"Coil / Batch No",w:118,a:"left"},
    {k:"qty", t:"Qty (MT)",       w:56, a:"right"},
    {k:"age", t:"Aging",          w:34, a:"center"}
  ];
  const sumW=cols.reduce((a,c)=>a+c.w,0), scale=W/sumW; let cx=L;
  cols.forEach(c=>{ c.w=c.w*scale; c.x=cx; cx+=c.w; });
  const qtyCol=cols.find(c=>c.k==="qty");

  const asof=new Date().toLocaleString("en-IN",{timeZone:"Asia/Kolkata",day:"2-digit",month:"short",year:"numeric",hour:"2-digit",minute:"2-digit"});
  const rowH=13.5, headH=15;
  let y=28;

  function drawHeadRow(){
    doc.rect(L,y,W,headH).fill("#eef3f7");
    doc.fillColor("#1a2a3a").font("Helvetica-Bold").fontSize(7.5);
    cols.forEach(c=>doc.text(c.t, c.x+2, y+4, {width:c.w-4, align:c.a, lineBreak:false}));
    y+=headH;
  }
  function ensure(h){ if(y+h>812){ doc.addPage(); y=28; return true; } return false; }

  function drawBrandHeader(pageTitle){
    y=28;
    doc.rect(L,y,W,42).fill("#0e2a47");
    doc.fillColor("#fff").font("Helvetica-Bold").fontSize(14).text("Job Work Stock Statement — Wheels India Limited", L+10, y+9, {width:W-20});
    doc.font("Helvetica").fontSize(8).fillColor("#cfe0ef").text("Bharat Steel (Chennai) Pvt. Ltd.  ·  Material held at BSC on customer's account", L+10, y+28, {width:W-20});
    y+=50;
    doc.fillColor("#333").font("Helvetica").fontSize(8.5)
       .text("Customer: Wheels India Limited        As on: "+asof+"        Warehouse: Job Work (WH 45)", L, y, {width:W});
    y+=16;
    doc.rect(L,y,W,20).fill("#0e2a47");
    doc.fillColor("#fff").font("Helvetica-Bold").fontSize(11).text(pageTitle, L+8, y+5, {lineBreak:false});
    y+=26;
  }

  function drawSection(form){
    const [t,hex]=SECDEF[form];
    const rs=rows.filter(r=>r.form===form).sort((a,b)=>b.Quantity-a.Quantity);
    if(!rs.length) return { n:0, q:0 };
    const tq=rs.reduce((a,r)=>a+r.Quantity,0);
    ensure(16+headH+rowH*2);
    doc.rect(L,y,W,16).fill(hex);
    doc.fillColor("#fff").font("Helvetica-Bold").fontSize(9).text(t, L+8, y+4, {lineBreak:false});
    doc.font("Helvetica").fontSize(8).text(rs.length+" items · "+tq.toFixed(3)+" MT", L, y+4, {width:W-8, align:"right"});
    y+=16;
    drawHeadRow();
    rs.forEach((x,i)=>{
      if(ensure(rowH)) drawHeadRow();
      if(i%2){ doc.rect(L,y,W,rowH).fill("#f7fafc"); }
      doc.fillColor("#222").font("Helvetica").fontSize(7.5);
      const mill=(x.ItemName||"").split(" ")[0];
      const vals={ n:String(i+1), mill, grade:x.Grade||"", thk:dimS(x.Thick), wid:dimS(x.Width), len:lenS(x.Length), coil:(x.CoilNo||x.BatchNum||""), qty:x.Quantity.toFixed(3), age:(x.AgeDays||"") };
      cols.forEach(c=>doc.text(String(vals[c.k]), c.x+2, y+3.3, {width:c.w-4, align:c.a, lineBreak:false, ellipsis:true}));
      y+=rowH;
    });
    ensure(rowH+4);
    doc.moveTo(L,y).lineWidth(1).lineTo(R,y).strokeColor("#333").stroke();
    doc.fillColor("#000").font("Helvetica-Bold").fontSize(8);
    doc.text(t+" total", L, y+3, {width:qtyCol.x-L-4, align:"right", lineBreak:false});
    doc.text(tq.toFixed(3), qtyCol.x+2, y+3, {width:qtyCol.w-4, align:"right", lineBreak:false});
    y+=rowH+8;
    return { n:rs.length, q:tq };
  }

  function pageTotal(label, n, q){
    ensure(30);
    doc.rect(L,y,W,22).fill("#0e2a47");
    doc.fillColor("#fff").font("Helvetica-Bold").fontSize(10).text(label, L+10, y+6, {lineBreak:false});
    doc.text(n+" items · "+q.toFixed(3)+" MT", L, y+6, {width:W-10, align:"right"});
    y+=30;
  }

  function footerNote(){
    ensure(26);
    doc.fillColor("#6b5200").font("Helvetica").fontSize(7).text("Plates currently under process (awaiting shearing) are excluded from this statement.", L, y, {width:W});
    y+=11;
    doc.fillColor("#999").font("Helvetica-Oblique").fontSize(6.5).text("System generated statement - might have mistakes.  Bharat Steel (Chennai) Pvt. Ltd. · No.147, Survey No.133, Thirunilai Village, Ponneri Taluk, Chennai 600103 · 044 6791 7800 · info@bharatsteels.in", L, y, {width:W});
  }

  PAGES.forEach((pg, idx)=>{
    if(idx>0) doc.addPage();
    drawBrandHeader(pg.title);
    let pn=0, pq=0;
    pg.forms.forEach(f=>{ const r=drawSection(f); pn+=r.n; pq+=r.q; });
    pageTotal(pg.title+" — Total", pn, pq);
    footerNote();
  });

  doc.end();
  await done;
  return Buffer.concat(chunks);
}
// Stock statement as a PNG (for the WhatsApp image header). Same sections/columns as the PDF/email.
function wheelsStockSvg(rows, asof){
  const L=16;
  const SEC=[["PLATE","Plate Stock (WIP / FG)","#0e7a4b"],["STRIP","Strip / Blank Stock","#b5651d"],["COIL","Coil Stock","#1367a6"]];
  // content-sized columns (no stretch-to-fill) so the image stays compact on a phone
  const cols=[{k:"n",t:"#",w:22,a:"c"},{k:"mill",t:"Mill",w:52,a:"c"},{k:"grade",t:"Grade",w:54,a:"l"},{k:"thk",t:"Thk",w:40,a:"c"},{k:"wid",t:"Width",w:44,a:"c"},{k:"len",t:"Length",w:46,a:"c"},{k:"coil",t:"Coil / Batch No",w:142,a:"l"},{k:"qty",t:"Qty (MT)",w:64,a:"r"},{k:"age",t:"Aging",w:38,a:"c"}];
  let cx=L; cols.forEach(c=>{ c.x=cx; cx+=c.w; });
  const W=cx+L, R=W-L;
  const qtyCol=cols.find(c=>c.k==="qty");
  const els=[];
  const rect=(x,y,w,h,f)=>els.push(`<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${f}"/>`);
  const txt=(x,y,s,o={})=>els.push(`<text x="${x}" y="${y}" font-family="DejaVu Sans" font-size="${o.size||9}"${o.bold?' font-weight="bold"':''} fill="${o.fill||'#222'}" text-anchor="${o.anchor||'start'}">${esc2(s)}</text>`);
  const put=(c,y,s,o)=>{ if(c.a==="r") txt(c.x+c.w-5,y,s,{...o,anchor:"end"}); else if(c.a==="c") txt(c.x+c.w/2,y,s,{...o,anchor:"middle"}); else txt(c.x+5,y,s,{...o,anchor:"start"}); };
  let y=0;
  rect(0,0,W,58,"#0e2a47");
  txt(L,24,"Job Work Stock Statement - Wheels India Limited",{size:16,bold:true,fill:"#fff"});
  txt(L,44,"Bharat Steel (Chennai) Pvt. Ltd.  ·  Material held at BSC on customer's account",{size:10,fill:"#cfe0ef"});
  y=58; txt(L,y+16,"Customer: Wheels India Limited      As on: "+asof+"      Warehouse: Job Work (WH 45)",{size:10,fill:"#445"}); y+=26;
  const GC="#c4d0da"; const colXs=cols.map(c=>c.x).concat(R);
  function grid(rowYs){ const lft=colXs[0],rgt=colXs[colXs.length-1],top=rowYs[0],bot=rowYs[rowYs.length-1];
    rowYs.forEach(yy=>rect(lft,yy,rgt-lft,1,GC)); colXs.forEach(xx=>rect(xx,top,1,bot-top,GC)); }
  let gb=0, gq=0;
  for(const [form,title,hex] of SEC){
    const rs=rows.filter(r=>r.form===form).sort((a,b)=>b.Quantity-a.Quantity);
    if(!rs.length) continue;
    const tq=rs.reduce((a,r)=>a+r.Quantity,0); gb+=rs.length; gq+=tq;
    rect(L,y,R-L,20,hex); txt(L+8,y+14,title,{size:11,bold:true,fill:"#fff"}); txt(R-6,y+14,rs.length+" items · "+tq.toFixed(3)+" MT",{size:9,fill:"#fff",anchor:"end"}); y+=20;
    const rowYs=[y];
    rect(L,y,R-L,18,"#eef3f7"); cols.forEach(c=>put(c,y+12,c.t,{size:8,bold:true,fill:"#1a2a3a"})); y+=18; rowYs.push(y);
    const rH=16;
    rs.forEach((xr,i)=>{ if(i%2) rect(L,y,R-L,rH,"#f7fafc");
      const mill=(xr.ItemName||"").split(" ")[0];
      const v={n:String(i+1),mill,grade:xr.Grade||"",thk:dimS(xr.Thick),wid:dimS(xr.Width),len:lenS(xr.Length),coil:(xr.CoilNo||xr.BatchNum||""),qty:xr.Quantity.toFixed(3),age:(xr.AgeDays||"")};
      cols.forEach(c=>put(c,y+11,String(v[c.k]),{size:8,fill:"#222"})); y+=rH; rowYs.push(y); });
    rect(L,y,R-L,18,"#e7eef4"); txt(qtyCol.x-6,y+12,title+" total",{size:8.5,bold:true,fill:"#1a2233",anchor:"end"}); put(qtyCol,y+12,tq.toFixed(3),{size:8.5,bold:true,fill:"#1a2233"}); y+=18; rowYs.push(y);
    grid(rowYs);
    y+=4;
  }
  rect(L,y,R-L,24,"#0e2a47"); txt(L+10,y+16,"TOTAL MATERIAL HELD",{size:11,bold:true,fill:"#fff"}); txt(R-8,y+16,gb+" items · "+gq.toFixed(3)+" MT",{size:11,bold:true,fill:"#fff",anchor:"end"}); y+=30;
  txt(L,y,"System generated statement - might have mistakes.  Bharat Steel (Chennai) Pvt. Ltd. · info@bharatsteels.in",{size:8,fill:"#999"}); y+=10;
  const H=y+6;
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}"><rect width="${W}" height="${H}" fill="#ffffff"/>${els.join("")}</svg>`;
}
async function buildWheelsStockPng(){
  let Resvg; try{ Resvg=(await import("@resvg/resvg-js")).Resvg; }catch(e){ throw new Error("resvg-js not available on server: "+e.message); }
  const ff=prodFontFiles();
  const rows=await wheelsRows();
  const asof=new Date().toLocaleString("en-IN",{timeZone:"Asia/Kolkata",day:"2-digit",month:"short",year:"numeric",hour:"2-digit",minute:"2-digit"});
  const svg=wheelsStockSvg(rows, asof);
  const r=new Resvg(svg, { font:{ fontFiles:ff, loadSystemFonts: ff.length===0, defaultFontFamily:"DejaVu Sans" }, background:"#ffffff" });
  return Buffer.from(r.render().asPng());
}
// ================= Wheels India PRODUCTION report (machine-wise, 3 sections) =================
// Machines (MDS) in display order.
const PROD_MACHS=[
  {code:"MACHINE02 (MDS)", name:"CTL 01-06 MM (MDS)"},
  {code:"MACHINE01 (MDS)", name:"CTL 6-25 MM (MDS)"},
  {code:"MACHINE15 (MDS)", name:"Shearing 06-25 MM (MDS)"}
];
const PROD_MON=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
async function wheelsProdData(){
  const DATA_BASE = process.env.DATA_BASE || "https://raw.githubusercontent.com/BSC23609/bsc-stock-data/main";
  const txt = await fetch(DATA_BASE+"/wheels_proddaily.csv?t="+Date.now()).then(r=>r.ok?r.text():"").catch(()=>"");
  return parseCsvSrv(txt); // rows: Date(YYYY-MM-DD), Machine, Machine Name, Total Qty, Wheels Qty
}
async function prodHolidays(){
  try{ const r=await sql`select holidays from wheels_settings where id=1`; return (r[0] && r[0].holidays) || []; }
  catch(e){ return []; }
}
function istYMD(){ const o={}; new Intl.DateTimeFormat("en-CA",{timeZone:"Asia/Kolkata",year:"numeric",month:"2-digit",day:"2-digit"}).formatToParts(new Date()).forEach(x=>o[x.type]=x.value); return {y:+o.year,m:+o.month,d:+o.day}; }
// daily cache-buster for WhatsApp image URLs (YYYYMMDD, IST) so Meta/WATI re-fetches a fresh image each day
function imgStamp(){ const {y,m,d}=istYMD(); return ""+y+String(m).padStart(2,"0")+String(d).padStart(2,"0"); }
function isoUTC(dt){ return dt.getUTCFullYear()+"-"+String(dt.getUTCMonth()+1).padStart(2,"0")+"-"+String(dt.getUTCDate()).padStart(2,"0"); }
function ddmmUTC(dt){ return String(dt.getUTCDate()).padStart(2,"0")+"-"+String(dt.getUTCMonth()+1).padStart(2,"0")+"-"+dt.getUTCFullYear(); }
// last working day strictly before `today`, skipping Sundays and holidays (array of 'YYYY-MM-DD').
function lastWorkingDay(y,m,d,holidays){
  const hol=new Set((holidays||[]).map(s=>String(s).slice(0,10)));
  let b=new Date(Date.UTC(y,m-1,d,12));
  do{ b.setUTCDate(b.getUTCDate()-1); } while(b.getUTCDay()===0 || hol.has(isoUTC(b)));
  return b;
}
async function prodModel(holidays){
  const rows = await wheelsProdData();
  if(holidays===undefined) holidays = await prodHolidays();
  const {y,m,d}=istYMD();
  const base=lastWorkingDay(y,m,d,holidays);
  const baseISO=isoUTC(base);
  const bY=base.getUTCFullYear(), bM=base.getUTCMonth(), bD=base.getUTCDate();
  const monStart=isoUTC(new Date(Date.UTC(bY,bM,1,12)));
  const yrStart =isoUTC(new Date(Date.UTC(bY,0,1,12)));
  let ly=bY, lmo=bM-1; if(lmo<0){ lmo=11; ly--; }
  const dim=new Date(Date.UTC(ly,lmo+1,0,12)).getUTCDate();
  const lmDay=Math.min(bD,dim);
  const lmStart=isoUTC(new Date(Date.UTC(ly,lmo,1,12)));
  const lmEndDt=new Date(Date.UTC(ly,lmo,lmDay,12));
  const lmEnd=isoUTC(lmEndDt);
  // parse rows once
  const R=rows.map(r=>({ ds:String(r["Date"]||"").slice(0,10), mc:String(r["Machine"]||"").trim(),
                         tot:parseFloat(r["Total Qty"])||0, wh:parseFloat(r["Wheels Qty"])||0 }))
              .filter(r=>/^\d{4}-\d{2}-\d{2}$/.test(r.ds));
  const sum=(fld,mc,from,to)=>R.reduce((a,r)=>{ if(mc&&r.mc!==mc)return a; if(from&&r.ds<from)return a; if(to&&r.ds>to)return a; return a+r[fld]; },0);
  const fmt=n=>String(Math.round(n||0)); // Production report: whole numbers (MT), rounded
  // Section A: Total production (all grades) by machine
  const total={rows:[],grand:{on:0,mon:0,lm:0}};
  PROD_MACHS.forEach(mo=>{
    const on=sum("tot",mo.code,baseISO,baseISO), mon=sum("tot",mo.code,monStart,baseISO), lm=sum("tot",mo.code,lmStart,lmEnd);
    total.rows.push({name:mo.name,on,mon,lm}); total.grand.on+=on; total.grand.mon+=mon; total.grand.lm+=lm;
  });
  // Section B: Wheels India production (WIR grades) by machine
  const wheels={rows:[],grand:{on:0,mon:0,lm:0,yr:0}};
  PROD_MACHS.forEach(mo=>{
    const on=sum("wh",mo.code,baseISO,baseISO), mon=sum("wh",mo.code,monStart,baseISO),
          lm=sum("wh",mo.code,lmStart,lmEnd), yr=sum("wh",mo.code,yrStart,baseISO);
    wheels.rows.push({name:mo.name,on,mon,lm,yr}); wheels.grand.on+=on; wheels.grand.mon+=mon; wheels.grand.lm+=lm; wheels.grand.yr+=yr;
  });
  // Section C: Wheels India monthly (WIR) — month x machine
  const monthsWithData=R.filter(r=>r.wh!==0 && r.ds>=yrStart).map(r=>+r.ds.slice(5,7));
  let firstM=monthsWithData.length?Math.min(...monthsWithData):(bM+1);
  const monthly={rows:[],grand:{c:[0,0,0],tot:0}};
  for(let mm=firstM; mm<=bM+1; mm++){
    const from=isoUTC(new Date(Date.UTC(bY,mm-1,1,12))), to=isoUTC(new Date(Date.UTC(bY,mm,0,12)));
    const c=PROD_MACHS.map(mo=>sum("wh",mo.code,from,to));
    const tot=c.reduce((a,b)=>a+b,0);
    monthly.rows.push({month:PROD_MON[mm-1]+" "+bY, c, tot});
    c.forEach((v,i)=>monthly.grand.c[i]+=v); monthly.grand.tot+=tot;
  }
  const asof=new Date().toLocaleString("en-IN",{timeZone:"Asia/Kolkata",day:"2-digit",month:"short",year:"numeric",hour:"2-digit",minute:"2-digit"});
  const labels={
    on:"Production on "+ddmmUTC(base),
    mon:PROD_MON[bM]+" "+bY+" upto "+ddmmUTC(base),
    lm:"Last month upto "+ddmmUTC(lmEndDt),
    yr:"This Year upto "+ddmmUTC(base)
  };
  const machNames=PROD_MACHS.map(mo=>mo.name);
  return { asof, labels, machNames, total, wheels, monthly, fmt };
}
function prodFontFiles(){
  const dirs=[];
  try{ dirs.push(path.join(process.cwd(),"api","fonts")); }catch(e){}
  try{ dirs.push(path.join(path.dirname(fileURLToPath(import.meta.url)),"fonts")); }catch(e){}
  for(const dir of dirs){ try{ if(fs.existsSync(path.join(dir,"DejaVuSans.ttf"))) return [path.join(dir,"DejaVuSans.ttf"), path.join(dir,"DejaVuSans-Bold.ttf")]; }catch(e){} }
  return [];
}
function prodWrap(s,maxChars){ const w=String(s).split(/\s+/); const lines=[]; let cur="";
  for(const t of w){ if(((cur?cur+" ":"")+t).length<=maxChars){ cur=(cur?cur+" ":"")+t; } else { if(cur)lines.push(cur); cur=t; } }
  if(cur)lines.push(cur); return lines.length?lines:[""]; }
function prodSvg(M){
  const L=16, f=M.fmt, COLW=70, FW_MACH=140, FW_MONTH=92;
  // columns are a fixed, content-sized width (not stretched to the page) so the image stays compact on mobile
  const widest = Math.max(FW_MACH+4*COLW, FW_MONTH+4*COLW);
  const W = L + widest + L, R = W - L;
  const els=[]; let y=0;
  const rect=(x,yy,w,h,fill)=>els.push(`<rect x="${x}" y="${yy}" width="${w}" height="${h}" fill="${fill}"/>`);
  const txt=(x,yy,s,o={})=>els.push(`<text x="${x}" y="${yy}" font-family="DejaVu Sans" font-size="${o.size||11}"${o.bold?' font-weight="bold"':''} fill="${o.fill||'#222'}" text-anchor="${o.anchor||'start'}">${esc2(s)}</text>`);
  rect(0,0,W,58,"#0e2a47");
  txt(L,26,"Wheels India Production Report",{size:17,bold:true,fill:"#fff"});
  txt(L,46,"Bharat Steel (Chennai) Pvt. Ltd.  ·  Machine-wise  ·  Qty in MT",{size:10,fill:"#cfe0ef"});
  y=58; txt(L,y+18,"As on: "+M.asof,{size:10,fill:"#445"}); y+=30;
  const GC="#c4d0da"; // gridline colour
  function grid(colXs, rowYs){ const lft=colXs[0], rgt=colXs[colXs.length-1], top=rowYs[0], bot=rowYs[rowYs.length-1];
    rowYs.forEach(yy=>els.push(`<rect x="${lft}" y="${yy}" width="${rgt-lft}" height="1" fill="${GC}"/>`));
    colXs.forEach(xx=>els.push(`<rect x="${xx}" y="${top}" width="1" height="${bot-top}" fill="${GC}"/>`)); }
  function section(title, headers, firstW, dataRows, grand){
    const n=headers.length-1, colW=COLW, tw=firstW+n*colW;
    const colXs=[L, L+firstW]; for(let i=1;i<=n;i++) colXs.push(L+firstW+i*colW);
    rect(L,y,tw,22,"#0e2a47"); txt(L+8,y+15,title,{size:12,bold:true,fill:"#fff"}); y+=22;
    const rowYs=[y];
    const hl=headers.map((h,i)=> i===0?[h]:prodWrap(h, Math.max(6,Math.floor(colW/6.6))));
    const maxLines=Math.max(...hl.map(a=>a.length));
    const hH=12+maxLines*11+4;
    rect(L,y,tw,hH,"#eef3f7");
    txt(L+firstW/2,y+hH/2+4,headers[0],{size:10,bold:true,fill:"#1a2a3a",anchor:"middle"});
    for(let i=1;i<headers.length;i++){ const cx=L+firstW+(i-1)*colW+colW/2; const lines=hl[i]; const sY=y+16+(maxLines-lines.length)*5.5;
      lines.forEach((ln,li)=>txt(cx,sY+li*11,ln,{size:9,bold:true,fill:"#1a2a3a",anchor:"middle"})); }
    y+=hH; rowYs.push(y);
    const rH=22;
    dataRows.forEach((r,ri)=>{ rect(L,y,tw,rH, ri%2?"#f7fafc":"#ffffff");
      txt(L+firstW/2,y+15,r.label,{size:10,fill:"#222",anchor:"middle"});
      r.vals.forEach((v,i)=>{ const cx=L+firstW+i*colW+colW/2; txt(cx,y+15,v,{size:10,fill:"#222",anchor:"middle"}); }); y+=rH; rowYs.push(y); });
    rect(L,y,tw,rH,"#0e2a47"); txt(L+firstW/2,y+15,grand.label,{size:10,bold:true,fill:"#fff",anchor:"middle"});
    grand.vals.forEach((v,i)=>{ const cx=L+firstW+i*colW+colW/2; txt(cx,y+15,v,{size:10,bold:true,fill:"#fff",anchor:"middle"}); }); y+=rH; rowYs.push(y);
    grid(colXs, rowYs);
    y+=18;
  }
  section("TOTAL PRODUCTION REPORT",
    ["Machine", M.labels.on, M.labels.mon, M.labels.lm], FW_MACH,
    M.total.rows.map(r=>({label:r.name, vals:[f(r.on),f(r.mon),f(r.lm)]})),
    {label:"GRAND TOTAL", vals:[f(M.total.grand.on),f(M.total.grand.mon),f(M.total.grand.lm)]});
  section("WHEELS INDIA PRODUCTION REPORT",
    ["Machine", M.labels.on, M.labels.mon, M.labels.lm, M.labels.yr], FW_MACH,
    M.wheels.rows.map(r=>({label:r.name, vals:[f(r.on),f(r.mon),f(r.lm),f(r.yr)]})),
    {label:"GRAND TOTAL", vals:[f(M.wheels.grand.on),f(M.wheels.grand.mon),f(M.wheels.grand.lm),f(M.wheels.grand.yr)]});
  section("WHEELS INDIA MONTHLY PRODUCTION REPORT",
    ["Month", M.machNames[0], M.machNames[1], M.machNames[2], "Total"], FW_MONTH,
    M.monthly.rows.map(r=>({label:r.month, vals:[f(r.c[0]),f(r.c[1]),f(r.c[2]),f(r.tot)]})),
    {label:"TOTAL", vals:[f(M.monthly.grand.c[0]),f(M.monthly.grand.c[1]),f(M.monthly.grand.c[2]),f(M.monthly.grand.tot)]});
  txt(L,y,"Computer-generated report - may contain mistakes.  Bharat Steel (Chennai) Pvt. Ltd. · info@bharatsteels.in",{size:8,fill:"#999"}); y+=12;
  const H=y+6;
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}"><rect width="${W}" height="${H}" fill="#ffffff"/>${els.join("")}</svg>`;
}
async function buildWheelsProdPng(holidays){
  let Resvg; try{ Resvg=(await import("@resvg/resvg-js")).Resvg; }catch(e){ throw new Error("resvg-js not available on server: "+e.message); }
  const ff=prodFontFiles();
  const svg=prodSvg(await prodModel(holidays));
  const r=new Resvg(svg, { font:{ fontFiles:ff, loadSystemFonts: ff.length===0, defaultFontFamily:"DejaVu Sans" }, background:"#ffffff" });
  return Buffer.from(r.render().asPng());
}
async function buildWheelsProdPdf(holidays){
  let PDFDocument; try{ PDFDocument=(await import("pdfkit")).default; }catch(e){ throw new Error("pdfkit not available on server: "+e.message); }
  const M=await prodModel(holidays), f=M.fmt;
  const doc=new PDFDocument({size:"A4",margin:28}); const chunks=[]; doc.on("data",c=>chunks.push(c)); const done=new Promise(r=>doc.on("end",r));
  const L=28,R=567,W=R-L; let y=28;
  doc.rect(L,y,W,44).fill("#0e2a47");
  doc.fillColor("#fff").font("Helvetica-Bold").fontSize(15).text("Wheels India Production Report",L+10,y+9);
  doc.font("Helvetica").fontSize(8).fillColor("#cfe0ef").text("Bharat Steel (Chennai) Pvt. Ltd.   Machine-wise   Qty in MT",L+10,y+28,{width:W-20});
  y+=52; doc.fillColor("#445").font("Helvetica").fontSize(9).text("As on: "+M.asof,L,y); y+=16;
  function section(title, headers, firstW, dataRows, grand){
    if(y>720){ doc.addPage(); y=28; }
    const n=headers.length-1, colW=(W-firstW)/n;
    doc.rect(L,y,W,18).fill("#0e2a47"); doc.fillColor("#fff").font("Helvetica-Bold").fontSize(10).text(title,L+8,y+5,{lineBreak:false}); y+=18;
    const hH=34; doc.rect(L,y,W,hH).fill("#eef3f7");
    doc.fillColor("#1a2a3a").font("Helvetica-Bold").fontSize(8).text(headers[0],L,y+hH/2-4,{width:firstW,align:"center"});
    for(let i=1;i<headers.length;i++){ const cx=L+firstW+(i-1)*colW; doc.font("Helvetica-Bold").fontSize(7).fillColor("#1a2a3a").text(headers[i],cx+2,y+5,{width:colW-4,align:"center"}); }
    y+=hH;
    const rH=18;
    dataRows.forEach((r,ri)=>{ if(ri%2){doc.rect(L,y,W,rH).fill("#f7fafc");}
      doc.font("Helvetica").fontSize(9).fillColor("#222").text(r.label,L,y+5,{width:firstW,align:"center",lineBreak:false});
      r.vals.forEach((v,i)=>{ const cx=L+firstW+i*colW; doc.font("Helvetica").fontSize(9).fillColor("#222").text(v,cx,y+5,{width:colW,align:"center",lineBreak:false}); }); y+=rH; });
    doc.rect(L,y,W,rH).fill("#0e2a47"); doc.font("Helvetica-Bold").fontSize(9).fillColor("#fff").text(grand.label,L,y+5,{width:firstW,align:"center",lineBreak:false});
    grand.vals.forEach((v,i)=>{ const cx=L+firstW+i*colW; doc.font("Helvetica-Bold").fontSize(9).fillColor("#fff").text(v,cx,y+5,{width:colW,align:"center",lineBreak:false}); }); y+=rH+14;
  }
  section("TOTAL PRODUCTION REPORT",
    ["Machine", M.labels.on, M.labels.mon, M.labels.lm], 150,
    M.total.rows.map(r=>({label:r.name, vals:[f(r.on),f(r.mon),f(r.lm)]})),
    {label:"GRAND TOTAL", vals:[f(M.total.grand.on),f(M.total.grand.mon),f(M.total.grand.lm)]});
  section("WHEELS INDIA PRODUCTION REPORT",
    ["Machine", M.labels.on, M.labels.mon, M.labels.lm, M.labels.yr], 150,
    M.wheels.rows.map(r=>({label:r.name, vals:[f(r.on),f(r.mon),f(r.lm),f(r.yr)]})),
    {label:"GRAND TOTAL", vals:[f(M.wheels.grand.on),f(M.wheels.grand.mon),f(M.wheels.grand.lm),f(M.wheels.grand.yr)]});
  section("WHEELS INDIA MONTHLY PRODUCTION REPORT",
    ["Month", M.machNames[0], M.machNames[1], M.machNames[2], "Total"], 90,
    M.monthly.rows.map(r=>({label:r.month, vals:[f(r.c[0]),f(r.c[1]),f(r.c[2]),f(r.tot)]})),
    {label:"TOTAL", vals:[f(M.monthly.grand.c[0]),f(M.monthly.grand.c[1]),f(M.monthly.grand.c[2]),f(M.monthly.grand.tot)]});
  doc.fillColor("#999").font("Helvetica-Oblique").fontSize(7).text("Computer-generated report - may contain mistakes. Bharat Steel (Chennai) Pvt. Ltd.",L,y,{width:W});
  doc.end(); await done; return Buffer.concat(chunks);
}

// ================= Company-wide DAILY STOCK report (PNG) — mirrors stock/daily.html =================
const DS_SHOWN_WH=["11","38","22","07","39","50","51","52"];
const DS_FYMON=["Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec","Jan","Feb","Mar"];
const dsFyOf=(y,m)=>m>=4?y:y-1;
const dsIdx=m=>(m+8)%12;
function f0IN(n){ return Number(n||0).toLocaleString("en-IN",{maximumFractionDigits:0}); }
function f3IN(n){ return Number(n||0).toLocaleString("en-IN",{minimumFractionDigits:3,maximumFractionDigits:3}); }
async function dailyStockData(){
  const DATA_BASE = process.env.DATA_BASE || "https://raw.githubusercontent.com/BSC23609/bsc-stock-data/main";
  const g=u=>fetch(DATA_BASE+"/"+u+"?t="+Date.now()).then(r=>r.ok?r.text():"").catch(()=>"");
  const [stock,sales,pur,jw,wstk,prod] = await Promise.all([
    g("mis_stock.csv"), g("mis_sales.csv"), g("mis_purchase.csv"), g("mis_jobwork_flow.csv"), g("wheels_stock.csv"), g("production.csv")
  ]);
  const meta = await fetch(DATA_BASE+"/mis_meta.json?t="+Date.now()).then(r=>r.ok?r.json():{}).catch(()=>({}));
  return { stock:parseCsvSrv(stock), sales:parseCsvSrv(sales), pur:parseCsvSrv(pur), jw:parseCsvSrv(jw), wstk:parseCsvSrv(wstk), prod:parseCsvSrv(prod), meta };
}
// month x category pivot (current FY). onlyJw: true=only job-work rows, false=exclude job-work, undefined=all rows.
function dsPivot(rows, fy, onlyJw){
  const data={}, tot={}; let latest=-1;
  rows.forEach(r=>{ const isJw=r.JW==="Y"; if(onlyJw!==undefined && (onlyJw?!isJw:isJw)) return;
    const y=+r.Yr, m=+r.Mth; if(!m || dsFyOf(y,m)!==fy) return; const i=dsIdx(m), q=parseFloat(r.Qty)||0;
    const c=r.Category||"(uncategorised)"; (data[c]??={})[i]=(data[c][i]||0)+q; tot[i]=(tot[i]||0)+q; if(i>latest)latest=i; });
  const cats=Object.keys(data).sort();
  const months=DS_FYMON.map((_,i)=>i).filter(i=>i<=latest);
  return { cats, months, data, tot };
}
async function dailyStockModel(){
  const D=await dailyStockData();
  const {y:iy,m:im}=istYMD(); const fy = im>=4 ? iy : iy-1;
  const num=v=>parseFloat(v)||0;
  const STOCK=D.stock.filter(r=>DS_SHOWN_WH.includes(r.WhsCode));
  // HR coil stock (WH 38, item-wise)
  const w38={}; STOCK.filter(r=>r.WhsCode==="38").forEach(r=>{ w38[r.ItemName]=(w38[r.ItemName]||0)+num(r.OnHand); });
  const coilArr=Object.entries(w38).sort((a,b)=>b[1]-a[1]); const t38=coilArr.reduce((s,[,q])=>s+q,0);
  // sales / job-work / purchase pivots
  const salesP=dsPivot(D.sales, fy, false);
  const jwP=dsPivot(D.sales, fy, true);
  const purP=dsPivot(D.pur, fy, undefined);
  const inFy=r=>dsFyOf(+r.Yr,+r.Mth)===fy;
  const ownMt=D.sales.filter(r=>r.JW!=="Y"&&inFy(r)).reduce((a,r)=>a+num(r.Qty),0);
  const jwMt =D.sales.filter(r=>r.JW==="Y"&&inFy(r)).reduce((a,r)=>a+num(r.Qty),0);
  // Wheels India throughput
  const isW=s=>/WHEELS INDIA/i.test(s||"");
  let recv=0,disp=0;
  D.jw.forEach(r=>{ if(dsFyOf(+r.Yr,+r.Mth)!==fy)return;
    if(isW(r.CardName)&&/VENDOR/i.test(r.CardName)&&/^Received/.test(r.Kind)) recv+=num(r.InQty);
    if(isW(r.CardName)&&/CUSTOMER/i.test(r.CardName)&&/^Dispatched/.test(r.Kind)) disp+=num(r.OutQty); });
  const held=D.wstk.reduce((a,r)=>a+num(r.Quantity),0);
  const wInv=D.sales.filter(r=>r.JW==="Y"&&isW(r.CardName)&&inFy(r)).reduce((a,r)=>a+num(r.Qty),0);
  // warehouse-wise (category x warehouse)
  const wd={}, ws=new Set(), wt={};
  STOCK.forEach(r=>{ const q=num(r.OnHand), c=r.Category||"8. OTHERS"; (wd[c]??={})[r.WhsCode]=(wd[c][r.WhsCode]||0)+q; ws.add(r.WhsCode); wt[r.WhsCode]=(wt[r.WhsCode]||0)+q; });
  const whCats=Object.keys(wd).sort(), whs=[...ws].sort();
  const whGrand=Object.values(wt).reduce((a,b)=>a+b,0);
  // production today / month-to-date
  const PC=["Today Prime Nos","Today Defective Nos","Today Scrap Nos","Today Prime Qty","Today Defective Qty","Today Scrap Qty","Month Prime Nos","Month Defective Nos","Month Scrap Nos","Month Prime Qty","Month Defective Qty","Month Scrap Qty"];
  const prodRows=D.prod.map(r=>({ name:(r["Machine Name"]||r["Machine Code"]||""), vals:PC.map(c=>num(r[c])) }));
  const prodTot=PC.map((_,i)=>prodRows.reduce((a,r)=>a+r.vals[i],0));
  const gen = D.meta.generated ? new Date(D.meta.generated) : new Date();
  const asof = gen.toLocaleString("en-IN",{day:"2-digit",month:"short",year:"numeric",hour:"2-digit",minute:"2-digit",timeZone:"Asia/Kolkata"});
  const dateDMY = gen.toLocaleDateString("en-GB",{day:"2-digit",month:"2-digit",year:"numeric",timeZone:"Asia/Kolkata"}); // DD/MM/YYYY
  return { fy, asof, dateDMY, coilArr, t38, salesP, jwP, purP, ownMt, jwMt, recv, disp, held, wInv, wd, whCats, whs, wt, whGrand, prodRows, prodTot };
}
function dailyStockSvg(M){
  // Two-column layout (mirrors the BSC Excel pivot sheet):
  //   LEFT  = HR Coil Stock (compact: Item | 38 | Grand Total)
  //   RIGHT = date box, then Sales / Purchase / Warehousewise tables.
  const salesN=(M.salesP.months||[]).length, purN=(M.purP.months||[]).length, whN=(M.whs||[]).length;
  const MW=52, CATW=118, GRW=66;                    // fixed right-column widths (no stretch-to-fill)
  const COILW=170+64+72;                            // left HR-coil table width
  const rightNeeded=Math.max(CATW+Math.max(1,salesN,purN)*MW+GRW, CATW+Math.max(1,whN)*MW+GRW, 200);
  const L=16, W=L+COILW+24+rightNeeded+L, R=W-L;
  const els=[];
  const rect=(x,yy,w,h,f)=>els.push(`<rect x="${x}" y="${yy}" width="${w}" height="${h}" fill="${f}"/>`);
  const txt=(x,yy,s,o={})=>els.push(`<text x="${x}" y="${yy}" font-family="DejaVu Sans" font-size="${o.size||9}"${o.bold?' font-weight="bold"':''} fill="${o.fill||'#222'}" text-anchor="${o.anchor||'start'}">${esc2(s)}</text>`);
  const put=(c,yy,s,o)=>{ if(c.align==="r") txt(c.x+c.w-5,yy,s,{...o,anchor:"end"}); else if(c.align==="c") txt(c.x+c.w/2,yy,s,{...o,anchor:"middle"}); else txt(c.x+5,yy,s,{...o,anchor:"start"}); };
  // draw a table with its own origin x0/y0; returns the y after the table.
  const GC="#c4d0da";
  function drawTable(ox, oy, title, cols, rows, headH){
    let x=ox; cols.forEach(c=>{ c.x=x; x+=c.w; }); const tw=x-ox; let y=oy;
    rect(ox,y,tw,18,"#ffe08a"); txt(ox+6,y+13,title,{size:10,bold:true,fill:"#5a4500"}); y+=18;
    const gridTop=y; const rowYs=[y];
    const hH=headH||18; rect(ox,y,tw,hH,"#dbeaf5");
    cols.forEach(c=>{ if(c.sub){ put(c,y+9,c.t,{size:7.5,bold:true,fill:"#1a2a3a"}); put(c,y+hH-3,c.sub,{size:7.5,bold:true,fill:"#1a2a3a"}); } else put(c,y+hH/2+3.5,c.t,{size:8.5,bold:true,fill:"#1a2a3a"}); });
    y+=hH; rowYs.push(y);
    const rH=16;
    rows.forEach((r,ri)=>{ if(r.gt) rect(ox,y,tw,rH,"#dbeaf5"); else if(ri%2) rect(ox,y,tw,rH,"#f7fafc");
      cols.forEach((c,ci)=>put(c,y+11,r.v[ci]==null?"":String(r.v[ci]),{size:8.5,bold:!!r.gt,fill:"#1a2233"})); y+=rH; rowYs.push(y); });
    // gridlines: a line under every row, and between every column
    rowYs.forEach(yy=>rect(ox,yy,tw,1,GC));
    cols.forEach(c=>rect(c.x,gridTop,1,y-gridTop,GC)); rect(ox+tw,gridTop,1,y-gridTop,GC);
    els.push(`<rect x="${ox}" y="${oy}" width="${tw}" height="${y-oy}" fill="none" stroke="#9fb3c4" stroke-width="1"/>`);
    return y;
  }
  // title bar
  rect(0,0,W,30,"#2f9fe0"); txt(W/2,20,"BHARAT STEEL (CHENNAI) PRIVATE LIMITED",{size:15,bold:true,fill:"#ffffff",anchor:"middle"});
  const startY=44;
  // ---- LEFT: HR coil stock ----
  const coilCols=[{t:"Row Labels",w:170,align:"l"},{t:"38",w:64,align:"r"},{t:"Grand Total",w:72,align:"r"}];
  const coilRows=M.coilArr.map(([n,q])=>({v:[n,f3IN(q),f3IN(q)]})).concat([{v:["Grand Total",f3IN(M.t38),f3IN(M.t38)],gt:true}]);
  const LW=COILW;
  const leftY=drawTable(L, startY, "BHARAT HR COIL STOCK (Warehouse 38 · MT)", coilCols, coilRows);
  // ---- RIGHT column ----
  const xR = L + LW + 24, rightW = R - xR;
  let ry = startY;
  rect(xR, ry, 200, 22, "#ffe08a"); txt(xR+100, ry+15, M.dateDMY, {size:12,bold:true,fill:"#5a4500",anchor:"middle"}); ry += 34;
  function pivotCols(months){
    return [{t:"Row Labels",w:CATW,align:"l"}].concat(months.map(i=>({t:DS_FYMON[i],w:MW,align:"r"}))).concat([{t:"Grand Total",w:GRW,align:"r"}]);
  }
  function pivotRows(P){
    const rows=P.cats.map(c=>{ const rt=P.months.reduce((a,i)=>a+(P.data[c][i]||0),0);
      return {v:[c].concat(P.months.map(i=>P.data[c][i]?f0IN(P.data[c][i]):"")).concat([f0IN(rt)])}; });
    rows.push({v:["Grand Total"].concat(P.months.map(i=>f0IN(P.tot[i]||0))).concat([f0IN(Object.values(P.tot).reduce((a,b)=>a+b,0))]), gt:true});
    return rows;
  }
  // Sales
  if(M.salesP.cats.length) ry=drawTable(xR, ry, "BHARAT SALES REPORT (own material, excl. job work · MT)", pivotCols(M.salesP.months), pivotRows(M.salesP));
  else ry=drawTable(xR, ry, "BHARAT SALES REPORT (MT)", [{t:"Row Labels",w:rightW,align:"l"}], [{v:["No data."]}]);
  ry+=16;
  // Purchase
  if(M.purP.cats.length) ry=drawTable(xR, ry, "BHARAT PURCHASE REPORT (MT)", pivotCols(M.purP.months), pivotRows(M.purP));
  else ry=drawTable(xR, ry, "BHARAT PURCHASE REPORT (MT)", [{t:"Row Labels",w:rightW,align:"l"}], [{v:["No data."]}]);
  ry+=16;
  // Warehousewise
  {
    const cols=[{t:"Row Labels",w:CATW,align:"l"}].concat(M.whs.map(w=>({t:w,w:MW,align:"r"}))).concat([{t:"Grand Total",w:GRW,align:"r"}]);
    const rows=M.whCats.map(c=>{ const rt=M.whs.reduce((a,w)=>a+(M.wd[c][w]||0),0);
      return {v:[c].concat(M.whs.map(w=>M.wd[c][w]?f0IN(M.wd[c][w]):"")).concat([f0IN(rt)])}; });
    rows.push({v:["Grand Total"].concat(M.whs.map(w=>f0IN(M.wt[w]||0))).concat([f0IN(M.whGrand)]), gt:true});
    ry=drawTable(xR, ry, "WAREHOUSEWISE STOCK (MT)", cols, rows);
  }
  const H=Math.max(leftY, ry)+28;
  txt(L, H-10, "Computer-generated report - may contain mistakes.  Bharat Steel (Chennai) Pvt. Ltd. · info@bharatsteels.in  ·  As on "+M.asof, {size:8,fill:"#999"});
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}"><rect width="${W}" height="${H}" fill="#ffffff"/>${els.join("")}</svg>`;
}
async function buildDailyStockPng(){
  let Resvg; try{ Resvg=(await import("@resvg/resvg-js")).Resvg; }catch(e){ throw new Error("resvg-js not available on server: "+e.message); }
  const ff=prodFontFiles();
  const svg=dailyStockSvg(await dailyStockModel());
  const r=new Resvg(svg, { font:{ fontFiles:ff, loadSystemFonts: ff.length===0, defaultFontFamily:"DejaVu Sans" }, background:"#ffffff" });
  return Buffer.from(r.render().asPng());
}
async function sendWheelsMail(to, cc, html, subject, attach){
  const tenant=process.env.TENANT_ID, cid=process.env.CLIENT_ID, sec=process.env.CLIENT_SECRET;
  const sender=process.env.WHEELS_SENDER || "pdqc@bharatsteels.in";
  const tok = await fetch(`https://login.microsoftonline.com/${tenant}/oauth2/v2.0/token`, {
    method:"POST", headers:{"Content-Type":"application/x-www-form-urlencoded"},
    body:new URLSearchParams({client_id:cid,client_secret:sec,scope:"https://graph.microsoft.com/.default",grant_type:"client_credentials"})
  }).then(r=>r.json());
  if(!tok.access_token) throw new Error("token: "+(tok.error_description||tok.error||"failed"));
  const message={ subject, body:{contentType:"HTML",content:html},
    toRecipients:(to||[]).map(a=>({emailAddress:{address:a}})),
    ccRecipients:(cc||[]).map(a=>({emailAddress:{address:a}})) };
  if(attach && attach.b64){
    const a={ "@odata.type":"#microsoft.graph.fileAttachment", name:attach.name,
      contentType:(attach.contentType||"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"), contentBytes:attach.b64 };
    if(attach.cid){ a.contentId=attach.cid; a.isInline=true; }
    message.attachments=[a];
  }
  const msg={ message, saveToSentItems:true };
  const r = await fetch(`https://graph.microsoft.com/v1.0/users/${sender}/sendMail`, {
    method:"POST", headers:{Authorization:"Bearer "+tok.access_token,"Content-Type":"application/json"}, body:JSON.stringify(msg) });
  if(r.status!==202) throw new Error("sendMail HTTP "+r.status+" "+(await r.text()).slice(0,200));
}

// ================= Portal authentication (login + admin user management) =================
const PORTAL_MODULES = ["stock","wheels","dispatch","nmdc"];
const AUTH_COOKIE = "bsc_session";
const AUTH_DAYS = 7;
function authSecret(){ return process.env.AUTH_SECRET || process.env.CRON_SECRET || ""; }
const _b64u = buf => Buffer.from(buf).toString("base64").replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,"");
const _b64uToBuf = s => Buffer.from(String(s).replace(/-/g,"+").replace(/_/g,"/"), "base64");
async function _authHmac(bodyStr, secret){
  const key = await globalThis.crypto.subtle.importKey("raw", new TextEncoder().encode(secret), {name:"HMAC",hash:"SHA-256"}, false, ["sign"]);
  const sig = await globalThis.crypto.subtle.sign("HMAC", key, new TextEncoder().encode(bodyStr));
  return _b64u(new Uint8Array(sig));
}
async function authSign(payload){ const s=authSecret(); if(!s) return ""; const body=_b64u(new TextEncoder().encode(JSON.stringify(payload))); return body+"."+await _authHmac(body,s); }
async function authVerify(tok){ const s=authSecret(); if(!s||!tok) return null; const [body,sig]=String(tok).split("."); if(!body||!sig) return null;
  let exp; try{ exp=await _authHmac(body,s); }catch(e){ return null; } if(sig!==exp) return null;
  try{ const o=JSON.parse(_b64uToBuf(body).toString("utf8")); if(o.exp && Date.now()>o.exp) return null; return o; }catch(e){ return null; } }
function hashPw(pw){ const salt=crypto.randomBytes(16); const dk=crypto.scryptSync(String(pw),salt,32); return "scrypt$"+salt.toString("hex")+"$"+dk.toString("hex"); }
function checkPw(pw, stored){ try{ const [,saltHex,hashHex]=String(stored).split("$"); const dk=crypto.scryptSync(String(pw),Buffer.from(saltHex,"hex"),32); const hb=Buffer.from(hashHex,"hex"); return dk.length===hb.length && crypto.timingSafeEqual(dk,hb); }catch(e){ return false; } }
function getCookie(req, name){ const c=req.headers.cookie||""; const m=new RegExp("(?:^|; )"+name+"=([^;]+)").exec(c); return m?decodeURIComponent(m[1]):""; }
function authCookie(token, maxAge){ return AUTH_COOKIE+"="+token+"; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age="+maxAge; }
async function authUser(req){ return await authVerify(getCookie(req, AUTH_COOKIE)); }
async function ensurePortalUsers(){
  await sql`create table if not exists portal_users (
    id serial primary key, email text unique not null, pass_hash text not null,
    is_admin boolean default false, modules text[] default '{}',
    reset_token text, reset_expires timestamptz,
    created_at timestamptz default now(), updated_at timestamptz default now() )`;
  const c = await sql`select count(*)::int n from portal_users`;
  if ((c[0]?.n||0) === 0 && process.env.ADMIN_EMAIL && process.env.ADMIN_PASSWORD){
    try { await sql`insert into portal_users (email, pass_hash, is_admin, modules) values (${String(process.env.ADMIN_EMAIL).toLowerCase()}, ${hashPw(process.env.ADMIN_PASSWORD)}, true, ${PORTAL_MODULES}) on conflict (email) do nothing`; } catch(e){}
  }
}
async function handlerAuth(req, res){
  if (cors(req, res)) return;
  try{
    await ensurePortalUsers();
    let body = req.body; if (typeof body === "string"){ try{ body=JSON.parse(body); }catch{ body={}; } } body = body || {};
    const action = body.action || ((/[?&]action=([^&]*)/.exec(req.url||"")||[])[1]) || (req.method==="GET"?"me":"");
    const base = (process.env.PUBLIC_URL || `https://${req.headers.host}`).replace(/\/$/, "");

    if (action === "me"){ const u=await authUser(req); return json(res,200,{ user: u ? {email:u.email, admin:!!u.admin, modules:u.modules||[]} : null, gate: !!process.env.AUTH_SECRET }); }

    if (action === "login"){
      const email=String(body.email||"").trim().toLowerCase();
      const r=await sql`select * from portal_users where lower(email)=${email}`;
      const u=r[0];
      if(!u || !checkPw(body.password, u.pass_hash)) return json(res,401,{error:"Invalid email or password"});
      const modules = u.is_admin ? PORTAL_MODULES : (u.modules||[]);
      const tok=await authSign({uid:u.id, email:u.email, admin:!!u.is_admin, modules, exp:Date.now()+AUTH_DAYS*864e5});
      if(!tok) return json(res,500,{error:"AUTH_SECRET not set on server"});
      res.setHeader("Set-Cookie", authCookie(tok, AUTH_DAYS*86400));
      return json(res,200,{ ok:true, user:{email:u.email, admin:!!u.is_admin, modules} });
    }
    if (action === "logout"){ res.setHeader("Set-Cookie", authCookie("", 0)); return json(res,200,{ok:true}); }

    if (action === "forgot"){
      const email=String(body.email||"").trim().toLowerCase();
      const r=await sql`select id from portal_users where lower(email)=${email}`;
      if(r[0]){ const token=crypto.randomBytes(24).toString("hex");
        await sql`update portal_users set reset_token=${token}, reset_expires=${new Date(Date.now()+3600000).toISOString()} where id=${r[0].id}`;
        const link=base+"/reset.html?token="+token;
        try{ await sendWheelsMail([email],[],`<p>A password reset was requested for your Bharat Steel MIS portal account.</p><p><a href="${link}">Reset your password</a> (link valid for 1 hour).</p><p>If you did not request this, you can ignore this email.</p>`,"Bharat Steel MIS - Password Reset",null); }catch(e){}
      }
      return json(res,200,{ok:true}); // never reveal whether the email exists
    }
    if (action === "reset"){
      const token=String(body.token||""); const pw=String(body.password||"");
      if(pw.length<6) return json(res,400,{error:"Password must be at least 6 characters"});
      const r=await sql`select id from portal_users where reset_token=${token} and reset_expires > now()`;
      if(!r[0]) return json(res,400,{error:"Invalid or expired reset link"});
      await sql`update portal_users set pass_hash=${hashPw(pw)}, reset_token=null, reset_expires=null, updated_at=now() where id=${r[0].id}`;
      return json(res,200,{ok:true});
    }

    // ---- admin-only: user management ----
    const me = await authUser(req);
    if (action === "users_list"){
      if(!me||!me.admin) return json(res,403,{error:"admin only"});
      const r=await sql`select id,email,is_admin,modules,created_at from portal_users order by email`;
      return json(res,200,{ users:r, all_modules:PORTAL_MODULES });
    }
    if (action === "user_save"){
      if(!me||!me.admin) return json(res,403,{error:"admin only"});
      const email=String(body.email||"").trim().toLowerCase(); if(!email) return json(res,400,{error:"Email is required"});
      const admin=!!body.admin;
      const modules=Array.isArray(body.modules)?body.modules.filter(m=>PORTAL_MODULES.includes(m)):[];
      try{
        if(body.id){
          if(body.password) await sql`update portal_users set email=${email}, is_admin=${admin}, modules=${modules}, pass_hash=${hashPw(String(body.password))}, updated_at=now() where id=${body.id}`;
          else              await sql`update portal_users set email=${email}, is_admin=${admin}, modules=${modules}, updated_at=now() where id=${body.id}`;
        } else {
          if(!body.password) return json(res,400,{error:"Password is required for a new user"});
          await sql`insert into portal_users (email, pass_hash, is_admin, modules) values (${email}, ${hashPw(String(body.password))}, ${admin}, ${modules})`;
        }
      }catch(e){ if(String(e.message||e).includes("duplicate")) return json(res,400,{error:"That email already exists"}); throw e; }
      return json(res,200,{ok:true});
    }
    if (action === "user_delete"){
      if(!me||!me.admin) return json(res,403,{error:"admin only"});
      if(+body.id === +me.uid) return json(res,400,{error:"You can't delete your own account"});
      const admins=await sql`select count(*)::int n from portal_users where is_admin=true`;
      const target=await sql`select is_admin from portal_users where id=${body.id}`;
      if(target[0]?.is_admin && (admins[0]?.n||0)<=1) return json(res,400,{error:"Can't delete the last admin"});
      await sql`delete from portal_users where id=${body.id}`;
      return json(res,200,{ok:true});
    }
    return json(res,400,{error:"unknown action"});
  }catch(e){ return json(res,500,{error:String(e.message||e)}); }
}

// _router.js
async function handler5(req, res) {
  const url = req.url || "";
  let route = decodeURIComponent((/[?&]route=([^&]*)/.exec(url) || [])[1] || req.query && req.query.route || "");
  if (!route || route === "index") route = url.split("?")[0].replace(/\/+$/, "").replace(/^\/api\/?/, "");
  route = route.replace(/^\/+|\/+$/g, "");
  if (route === "dispatch") return handler(req, res);
  if (route === "sync") return handler2(req, res);
  if (route === "wheels") return handlerWheels(req, res);
  if (route === "auth") return handlerAuth(req, res);
  res.setHeader("Cache-Control", "no-store");
  res.status(404).json({ error: "unknown route: " + route });
}
export {
  handler5 as default
};
