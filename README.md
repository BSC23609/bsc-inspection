# Bharat Steel Group — Apps (app.thebharatsteelgroup.com)

One sign-in for every group application. People sign in here once; clicking an app tile opens that app already signed in as them.

## How it works

1. The person signs in to the hub with their **work email or employee code** and password.
2. They click an app tile. The hub creates a **one-minute token** for that app only, signed with a secret that only the hub and that app know, and sends the browser to the app's `/auth/sso` address.
3. The app checks the token, finds the employee (by their ID in that app, then by email) and starts its own normal session.

Each app keeps its own login as a fallback, and its own login page gets a **"Sign in with Bharat Steel Group"** button.

Apps can be in one of two modes (Admin → Apps):

| Mode | What happens on click |
|---|---|
| **Single sign-on** | Lands inside the app, already signed in. Needs the small receiver added to that app. |
| **Link only** | Just opens the app, which still shows its own login. Use until the app is wired up. |

## Deploy (Vercel + Neon, same as the Metfraa Portal)

1. **GitHub** — create the repo `BSC23609/bsg-hub` and push this folder to it.
2. **Neon** — create a project `bsg-hub` in Singapore. Copy the **pooled** connection string (host contains `-pooler`).
3. **Vercel** — import the repo (Framework: Other, Root Directory: empty). Add these environment variables, then deploy:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | the Neon pooled connection string |
   | `SESSION_SECRET` | a long random string (64+ characters). PowerShell: `[Convert]::ToBase64String((1..48 \| % {Get-Random -Max 256}))` |
   | `BOOTSTRAP_ADMIN_EMAIL` | the first admin's email, e.g. `ai@bharatsteels.in` |
   | `BOOTSTRAP_ADMIN_PASSWORD` | a temporary password for that admin |
   | `BOOTSTRAP_ADMIN_NAME` | optional, shown on the launcher |

   Tables are created automatically on first start. The bootstrap admin is only created while the people table is empty; remove `BOOTSTRAP_ADMIN_PASSWORD` from Vercel after your first sign-in.
4. **Domain** — Vercel project → Settings → Domains → add `app.thebharatsteelgroup.com`. In the DNS for `thebharatsteelgroup.com`, add the record Vercel shows (normally `CNAME  app  →  cname.vercel-dns.com`).
5. Open `https://app.thebharatsteelgroup.com`, sign in as the bootstrap admin and set your own password.

## Switch an app to single sign-on

Do this for the Metfraa Portal (`metfraa-portal`) and the BSC Portal (`bsc-portal`) first.

1. Hub → **Admin → Apps** → open the app → **Show the secret** → copy it.
2. In that app's repo, apply its patch and push:
   ```
   git apply metfraa-portal-group-sso.patch      (or bsc-tickets-group-sso.patch)
   git add -A && git commit -m "Group single sign-on" && git push
   ```
3. In that app's **Vercel** project add `GROUP_SSO_SECRET` = the secret you copied, and redeploy.
   Optional: `GROUP_HUB_URL` (defaults to `https://app.thebharatsteelgroup.com`) and `GROUP_SSO_APP_KEY` (defaults to `metfraa-portal` / `bsc-portal`).
4. Back in the hub, set the app to **Single sign-on**, check the sign-on address (`https://app.metfraa.com/auth/sso`, `https://app.bharatsteels.in/auth/sso`) and save.

Rotating a secret in the hub stops sign-on into that app until the new secret is pasted into its Vercel settings.

## Add people

- **One at a time:** Admin → People → Add person. A temporary password is shown once; they choose their own at first sign-in.
- **In bulk:** Admin → People → Import from CSV (download the template there). The results file contains the temporary passwords; share each privately, then delete the file.
- **"ID in that app":** each app finds the person by this value. Leave it blank when the app uses the same employee code as the hub. Fill it in when it differs, e.g. a person whose hub code is `MET66` but whose BSC Portal number is `BSC/017`. If no match by ID is found, the app tries the person's email.

The first time someone arrives in an app through the hub, that app's "must change password" flag is cleared and its default password (`Metfraa@123`, `Bsc@1234`) is replaced with a random one, so the shared default can no longer be used for that account. They can still set their own app password with that app's "Forgot password".

## Fill "ID in that app" automatically

Add each app's Neon connection string to the **bsg-hub** Vercel project (then redeploy):

| Variable | Value |
|---|---|
| `APPDB_BSC_PORTAL` | the BSC Portal (bsc-tickets) `DATABASE_URL` |
| `APPDB_METFRAA_PORTAL` | the Metfraa Portal `DATABASE_URL` |

The name is `APPDB_` + the app's key in capitals. The hub only reads these databases (every query runs read-only).
Then **Find in apps** on the Add person page ticks every app where the person has an account and fills their ID,
blank IDs are looked up on save, and **Match everyone with apps** on the People page does it for all people at once.
People are matched by email, then employee code (and by exact name, only when you press Find in apps).
Admin → Apps → (app) shows whether the lookup is connected.

## Good to know

- **Signing out** of the hub doesn't sign people out of apps they already opened; each app keeps its own session (Metfraa 180 days, BSC Portal 1 year).
- **Disabling** someone in the hub stops them opening apps from the hub at once. Also disable them inside each app to end sessions already open there.
- Five wrong passwords lock an account for 15 minutes. Every sign-in, app opening and admin change is in Admin → Activity.
- Forgotten hub password: an admin uses **Reset password** on that person.

## Run locally / tests

```
pip install -r requirements.txt pytest httpx
COOKIE_SECURE=false BOOTSTRAP_ADMIN_EMAIL=you@bharatsteels.in BOOTSTRAP_ADMIN_PASSWORD=Start1234 uvicorn app.main:app --reload
pytest -q
```
Locally it uses SQLite (`hub.db`) unless `DATABASE_URL` is set.

## Starting password
New and reset accounts get the common starting password `Bsg@1234` (change it with `COMMON_PASSWORD`
in Vercel → Environment Variables, then redeploy). Everyone must choose their own password at first
sign-in, and nobody can keep the common one.

## Forgot password (self-service)
"Forgot password?" on the sign-in page sends a 6-digit code (valid 10 minutes, 5 tries) to the person's
WhatsApp (mobile on their hub account) and/or email. Add to Vercel → bsg-hub → Environment Variables,
copied from the other group apps:
- WhatsApp: `WATI_BASE_URL`, `WATI_TOKEN` (template `otp_password2`, change with `WATI_OTP_TEMPLATE`)
- Email (optional): `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM`

Wrong passwords never lock an account. To turn a lock back on, set `MAX_FAILED_LOGINS` (e.g. 10).
