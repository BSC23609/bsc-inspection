/* _portal.js — shared shell for the BSC MIS portal.
   Injects the top module nav + common CSS + tiny helpers on every page.
   Each page sets  window.PORTAL = { module:'stock', screen:'daily' }  before including this. */
(function(){
  const P = window.PORTAL || {};
  const BASE = location.pathname.includes('/stock/')||location.pathname.includes('/dispatch/')||location.pathname.includes('/nmdc/')||location.pathname.includes('/wheels/') ? '..' : '.';

  // ---- client-side auth gate (opt-in: the server returns gate:true only when AUTH_SECRET is set) ----
  // Fail-open: on any network/parse error the page is shown, so auth can never hard-break the portal.
  const GATED_MODS=['stock','wheels','dispatch','nmdc'];
  (function authGate(){
    const G=id=>document.getElementById(id);
    try{ document.documentElement.style.visibility='hidden'; }catch(e){}
    const show=()=>{ try{ document.documentElement.style.visibility=''; }catch(e){} };
    const modOf=p=>{ for(const k of GATED_MODS){ if(p.indexOf('/'+k+'/')>=0 || p.endsWith('/'+k)) return k; } return null; };
    fetch(BASE+'/api/auth?action=me',{credentials:'same-origin'}).then(r=>r.json()).then(d=>{
      const path=location.pathname;
      if(d && d.gate && !d.user){ location.replace(BASE+'/login.html?next='+encodeURIComponent(path)); return; }
      if(d && d.gate && d.user && !d.user.admin){
        if(path.indexOf('/users')>=0){ location.replace(BASE+'/login.html?denied=admin'); return; }
        const mod=modOf(path); if(mod && !((d.user.modules||[]).includes(mod))){ location.replace(BASE+'/login.html?denied='+mod); return; }
      }
      show();
      const u=d&&d.user; if(u){
        const ue=G('pUser'); if(ue) ue.textContent=u.email;
        const lo=G('pLogout'); if(lo){ lo.style.display=''; lo.onclick=()=>{ fetch(BASE+'/api/auth',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify({action:'logout'})}).then(()=>location.href=BASE+'/login.html'); }; }
        if(u.admin){ const us=G('pUsers'); if(us) us.style.display=''; }
        else document.querySelectorAll('.pmods a[data-mod]').forEach(a=>{ const m=a.getAttribute('data-mod'); if(GATED_MODS.includes(m)&&!((u.modules||[]).includes(m))) a.style.display='none'; });
      }
    }).catch(()=>show());
  })();

  const MODULES = [
    { id:'stock', label:'Stock', href:BASE+'/stock/daily.html', screens:[
        {id:'daily',  label:'Daily Stock Report', href:BASE+'/stock/daily.html'},
        {id:'pivot',  label:'Coil Stock (T×W×Grade)', href:BASE+'/stock/coil-pivot.html'},
        {id:'sheets', label:'Sheet Stock List', href:BASE+'/stock/sheets.html'},
        {id:'coils',  label:'Coil Stock List', href:BASE+'/stock/coils.html'},
    ]},
    { id:'dispatch', label:'Dispatch', href:BASE+'/dispatch/index.html', screens:[] },
    { id:'nmdc', label:'NMDC', href:BASE+'/nmdc/index.html', screens:[] },
    { id:'wheels', label:'Wheels India', href:BASE+'/wheels/index.html', screens:[
        {id:'report',   label:'Stock Statement', href:BASE+'/wheels/index.html'},
        {id:'settings', label:'Settings', href:BASE+'/wheels/settings.html'},
        {id:'sendlog',  label:'Send Log', href:BASE+'/wheels/log.html'},
    ]},
    { id:'worktracker', label:'Work Tracker', href:'https://tasks.bharatsteels.in', external:true, screens:[] },
  ];


  const isEmbedded = (P.module==='dispatch'||P.module==='nmdc');
  if(isEmbedded){
    // self-contained bar, all styles inline so nothing leaks into the host page's CSS
    const bar=document.createElement('div');
    bar.setAttribute('style','position:sticky;top:0;z-index:99999;display:flex;align-items:center;gap:14px;'+
      'background:linear-gradient(180deg,#1367a6,#0e4d7d);color:#fff;padding:8px 16px;'+
      'font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;font-size:14px;box-shadow:0 1px 4px rgba(0,0,0,.15)');
    const link=(m)=>`<a href="${m.href}"${m.external?' target="_blank" rel="noopener"':''} style="color:#fff;text-decoration:none;font-weight:600;font-size:13px;padding:6px 13px;border-radius:7px;${m.id===P.module?'background:rgba(255,255,255,.18)':'opacity:.75'}">${m.label}</a>`;
    bar.innerHTML='<span style="font-weight:700;font-size:15px">Bharat Steel MIS</span>'+
      '<nav style="display:flex;gap:2px">'+MODULES.map(link).join('')+'</nav>';
    document.body.insertBefore(bar, document.body.firstChild);
    return; // do NOT add global css / portalskin / helpers to embedded pages
  }

  const css = `
  :root{--blue:#1367a6;--blue-dark:#0e4d7d;--ink:#14202b;--gray:#5b6b78;--line:#e3e8ec;--bg:#f5f7f9;--head:#e6eef5;--good:#1a7f4b;--amber:#c77a10;--red:#b3261e;--yellow:#fff1c2;--mono:"SFMono-Regular",ui-monospace,Menlo,Consolas,monospace;}
  *{box-sizing:border-box}
  body.portalskin{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:var(--ink);background:var(--bg);font-size:14px;line-height:1.4}
  .pw{max-width:1280px;margin:0 auto;padding:0 16px}
  .ptop{background:linear-gradient(180deg,var(--blue),var(--blue-dark));color:#fff}
  .ptop .row{display:flex;align-items:center;gap:16px;max-width:1280px;margin:0 auto;padding:10px 16px;flex-wrap:wrap}
  .ptop img{height:30px;background:#fff;padding:3px 6px;border-radius:6px}
  .pbrand{font-weight:700;font-size:15px;line-height:1}
  .pbrand small{display:block;font-size:10px;opacity:.8;text-transform:uppercase;letter-spacing:.12em;font-weight:600;margin-top:2px}
  .pmods{display:flex;gap:2px;margin-left:8px}
  .pmods a{color:#fff;text-decoration:none;font-weight:600;font-size:13px;padding:7px 14px;border-radius:7px;opacity:.75}
  .pmods a:hover{opacity:1;background:rgba(255,255,255,.12)}
  .pmods a.on{opacity:1;background:rgba(255,255,255,.18)}
  .pright{margin-left:auto;display:flex;align-items:center;gap:10px;font-size:12px;flex-wrap:wrap}
  .pstamp{font-family:var(--mono);opacity:.9}
  .pstale{background:#fff3cd;color:#7a5200;font-size:11px;padding:2px 8px;border-radius:6px;font-weight:700}
  .pbtn{background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.3);color:#fff;text-decoration:none;padding:6px 12px;border-radius:7px;font-weight:600;font-size:12px;cursor:pointer;font-family:inherit}
  .pbtn:hover{background:rgba(255,255,255,.25)}
  .psub{background:#fff;border-bottom:1px solid var(--line)}
  .psub .row{display:flex;gap:2px;max-width:1280px;margin:0 auto;padding:0 16px;overflow-x:auto}
  .psub a{padding:10px 14px;font-weight:600;font-size:13px;color:var(--gray);text-decoration:none;white-space:nowrap;border-bottom:2px solid transparent}
  .psub a:hover{color:var(--ink)} .psub a.on{color:var(--blue);border-bottom-color:var(--blue)}
  .card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:14px 0;min-width:0}
  .card h3{margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:.1em;color:var(--gray);display:flex;align-items:center;gap:8px}
  .card h3 .r{margin-left:auto;font-family:var(--mono);text-transform:none;letter-spacing:0;font-weight:600;color:var(--ink);font-size:11px}
  .filters{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:14px 0}
  .filters select,.filters input{border:1px solid var(--line);border-radius:8px;padding:7px 9px;font-size:13px;background:#fff;color:var(--ink);font-family:inherit}
  .filters input[type=search]{min-width:200px;flex:1}
  .filters label{font-size:12px;color:var(--gray);display:flex;align-items:center;gap:5px}
  .clr{color:var(--blue);font-weight:600;font-size:12px;cursor:pointer;padding:6px 4px}
  .fcount{font-size:12px;color:var(--gray);margin-left:auto;font-family:var(--mono)}
  .tblwrap{overflow-x:auto}.scroll{max-height:600px;overflow:auto}.scroll thead th{position:sticky;top:0;background:#fff;z-index:1}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--gray);font-weight:700;padding:8px 10px;border-bottom:2px solid var(--line);white-space:nowrap}
  th.num,td.num{text-align:right;font-family:var(--mono);white-space:nowrap}
  td{padding:7px 10px;border-bottom:1px solid var(--line)}
  tbody tr:hover{background:#f8fafb}
  tr.tot td{font-weight:700;border-top:2px solid var(--ink);border-bottom:none;font-family:var(--mono);background:var(--head)}
  tr.tot td:first-child{font-family:inherit}
  tr.grp>td{background:#fbfcfd;font-weight:700;cursor:pointer}
  tr.itm>td:first-child{padding-left:28px}
  .tw{display:inline-block;width:14px;color:var(--blue);font-family:var(--mono);font-size:11px}
  td.cell{cursor:pointer}td.cell:hover{background:#e8f1fa}td.cell.sel{background:#dbe9f7;font-weight:700}
  .pivot td.num.hot{background:#eef6ef}
  .tag{display:inline-block;font-size:10px;font-weight:700;padding:2px 6px;border-radius:5px;background:#eef3f7;color:var(--blue-dark);white-space:nowrap}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:16px 0}
  .kpi{background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px 14px}
  .kpi.click{cursor:pointer}.kpi.on{border-color:var(--blue);box-shadow:0 0 0 1px var(--blue)}
  .kpi .lab{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--gray);font-weight:700}
  .kpi .val{font-family:var(--mono);font-size:22px;font-weight:700;margin-top:3px;white-space:nowrap}
  .kpi .val small{font-size:11px;color:var(--gray);font-weight:600}
  .kpi .sub{font-size:11px;color:var(--gray);margin-top:2px}
  .bar{display:inline-block;height:4px;border-radius:3px;background:var(--blue);vertical-align:middle}
  .empty{text-align:center;color:var(--gray);padding:30px 0;font-size:13px}
  .err{background:#fdecea;border:1px solid #f5c6c0;color:#a3271b;padding:12px 14px;border-radius:9px;margin:14px 0;font-size:13px}
  .muted{color:var(--gray);font-size:11px}
  footer{color:var(--gray);font-size:11px;text-align:center;padding:24px 0}
  @media print{.ptop .pbtn,.psub,.filters{display:none}}
  `;
  document.head.insertAdjacentHTML('beforeend','<style>'+css+'</style>');
  if(!(P.module==='dispatch'||P.module==='nmdc')) document.body.classList.add('portalskin');
  const mod = MODULES.find(m=>m.id===P.module) || MODULES[0];
  const modLinks = MODULES.map(m=>`<a data-mod="${m.id}" href="${m.href}"${m.external?' target="_blank" rel="noopener"':''} class="${m.id===P.module?'on':''}">${m.label}</a>`).join('');
  const subLinks = (mod.screens||[]).map(s=>`<a href="${s.href}" class="${s.id===P.screen?'on':''}">${s.label}</a>`).join('');
  document.body.insertAdjacentHTML('afterbegin',
    `<div class="ptop"><div class="row">
       <img src="${BASE}/stock/BSC.png" alt="BSC" onerror="this.style.display='none'"/>
       <div class="pbrand">Bharat Steel MIS<small>Chennai · live from SAP</small></div>
       <nav class="pmods">${modLinks}</nav>
       <div class="pright">
         <span class="pstale" id="pStale" style="display:none">STALE</span>
         <span class="pstamp" id="pStamp">—</span>
         <span class="pstamp" id="pUser"></span>
         <a class="pbtn" id="pUsers" href="${BASE}/users.html" style="display:none">Users</a>
         <a class="pbtn" id="pLogout" style="display:none">Logout</a>
       </div>
     </div></div>
     ${subLinks?`<div class="psub"><div class="row">${subLinks}</div></div>`:''}`);

  // shared helpers
  window.$=id=>document.getElementById(id);
  window.num=v=>{const n=parseFloat(v);return isNaN(n)?0:n;};
  window.f0=n=>n.toLocaleString('en-IN',{maximumFractionDigits:0});
  window.f1=n=>n.toLocaleString('en-IN',{minimumFractionDigits:1,maximumFractionDigits:1});
  window.f3=n=>n.toLocaleString('en-IN',{minimumFractionDigits:3,maximumFractionDigits:3});
  window.nz=v=>{const n=num(v);return n?(Number.isInteger(n)?n:(+n.toFixed(2))):'';};
  window.esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  window.parseCSV=function(text){
    if(!text) return [];
    if(text.charCodeAt(0)===0xFEFF)text=text.slice(1);
    const rows=[];let row=[],cur="",q=false;
    for(let i=0;i<text.length;i++){const c=text[i];
      if(q){if(c==='"'){if(text[i+1]==='"'){cur+='"';i++;}else q=false;}else cur+=c;}
      else{if(c==='"')q=true;else if(c===',')row.push(cur),cur="";else if(c==='\n')row.push(cur),rows.push(row),row=[],cur="";else if(c==='\r'){}else cur+=c;}}
    if(cur!==""||row.length){row.push(cur);rows.push(row);}
    if(!rows.length)return[];const head=rows.shift().map(h=>h.trim());
    return rows.filter(r=>r.length>1).map(r=>{const o={};head.forEach((h,i)=>o[h]=(r[i]??"").trim());return o;});
  };
  window.setStamp=function(gen){
    const d=gen?new Date(gen):null;
    $('pStamp').textContent='as of '+(d||new Date()).toLocaleString('en-IN',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'});
    $('pStale').style.display=(d&&Date.now()-d.getTime()>3*3600e3)?'':'none';
  };
  window.BASE=BASE;
  // data CSVs live in a SEPARATE repo Vercel does not watch (avoids a deploy per refresh)
  window.DATA_BASE="https://raw.githubusercontent.com/BSC23609/bsc-stock-data/main";
})();
