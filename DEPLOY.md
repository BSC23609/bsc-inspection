# BSC QMS — FastAPI rebuild (Phase 1)

New portal (FastAPI + Jinja2, Metfraa-style UI). **Phase 1 = Foundation + Diesel Generator Log.**
It runs *alongside* the current Node app and shares the same database, auth cookie
and OneDrive — so staff log in with the same employee code/password, and DG Log
data is the same records in both.

## What's in this phase
- Login (shared `bsc_qms_token` cookie — same `JWT_SECRET`, bcrypt passwords, `employees` table)
- Metfraa-style shell: dark top bar, section header, module tiles
- Diesel Generator Log: Excel-style grid (no horizontal scroll), add/edit/delete,
  auto-calc running hours + units, monthly totals, month PDF, OneDrive CSV log
- Other modules shown as "migrating" tiles (ported in later phases)

## Project layout
```
api/index.py        ← Vercel entrypoint (exports `app`)
app/                ← config, db (asyncpg), auth, graph, pdfgen, routers/
templates/          ← base, login, home, dg_log (Jinja2)
static/             ← app.css, bsc-logo.png
vercel.json         ← Python build + routing
requirements.txt
```

## Deploy on Vercel (new project, separate from the current app)
1. Put this folder in its own Git repo (e.g. `bsc-qms-fastapi`) and push to GitHub.
2. In Vercel: **New Project → import that repo**. Framework preset: *Other*. No build
   command needed (the Python runtime reads `requirements.txt`).
3. Add **Environment Variables** (copy the exact same values the current QMS uses):
   - `DATABASE_URL`   (same Neon connection string)
   - `JWT_SECRET`     (**must match** the current app, or existing logins won't verify)
   - `TENANT_ID`, `CLIENT_ID`, `CLIENT_SECRET`, `USER_ID`  (same Microsoft Graph app)
4. Deploy. Open the Vercel URL → you'll get the login screen.
5. Sign in with any existing employee code (e.g. your BSC/… code) — same password as today.

## Cutover (later, when you're happy)
Point `qms.bharatsteels.in` at this project (or a subdomain like `qms2.` for parallel
testing first). Until then the old app keeps serving; nothing is removed.

## Local run (optional)
```
pip install -r requirements.txt
export DATABASE_URL=... JWT_SECRET=... TENANT_ID=... CLIENT_ID=... CLIENT_SECRET=... USER_ID=...
uvicorn api.index:app --reload
```

## Next phases (suggested order)
Inward → CTL Quality → Shearing → PDI → Gate Pass → Complaints/8D → Stock → Dashboard.
Each reuses the same DB tables and OneDrive folders; the old app stays live until its
module is ported and verified here.
