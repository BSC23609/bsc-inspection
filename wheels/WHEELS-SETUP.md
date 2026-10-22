# Wheels India module — setup

## Portal (auto-deploys on push)
Files: wheels/index.html (statement), wheels/settings.html, wheels/BSC.png,
api/index.js (adds /api/wheels route), _portal.js (adds nav tab),
wheels/wheels_stock.csv + wheels_meta.json (written by refresh_stock.ps1).

Neon table `wheels_settings` self-creates on first call to /api/wheels — no manual SQL.

## refresh_stock.ps1
Already updated: queries WH45 for BSV01373 and writes wheels/wheels_stock.csv every 15 min.
Just replace C:\scripts\refresh_stock.ps1 with the new one and copy to the repo too.

## Portal test-send (Settings page)
The Settings page has a "Send test now" button. It renders the current statement from the
latest wheels_stock.csv (server-side) and emails it via Graph as PDQC to a test address.
For this to work, set these env vars on the Vercel project (the app already has the first three
for NMDC sync — only WHEELS_SENDER is new):
    TENANT_ID, CLIENT_ID, CLIENT_SECRET  (existing)
    WHEELS_SENDER = pdqc@bharatsteels.in
And the Azure app needs Graph Mail.Send (below).

## Daily 10:00 email — send_wheels_report.ps1
1. Copy send_wheels_report.ps1 and BSC.png to C:\scripts.
2. Add a [wheels] section to C:\scripts\config.ini:

    [wheels]
    portal        = https://mis.bharatsteels.in
    tenant_id     = f3f819ba-724b-4c0b-a9c0-2aa8d12bcbcc
    client_id     = 627c4231-5cdf-40b8-8af8-fef565f62bb7
    client_secret = <the NEW Azure secret>
    sender        = pdqc@bharatsteels.in
    logo          = C:\scripts\BSC.png

3. Azure: the app (627c4231…) needs Graph APPLICATION permission **Mail.Send**,
   admin-consented. Restrict it to the **pdqc@bharatsteels.in** mailbox with an application access policy
   (same pattern as the info@ enquiry pipeline) so it can only send as pdqc@bharatsteels.in.
4. Test by hand:  powershell -ExecutionPolicy Bypass -File C:\scripts\send_wheels_report.ps1
   (set your own address as the only To in Settings first, confirm it arrives, then put the
   real Wheels India recipients back.)
5. Task Scheduler: new task, weekdays 10:00, run whether logged on or not (S4U), highest,
   action = powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\scripts\send_wheels_report.ps1
   Check C:\scripts\wheels_mail.log after the first run.

Recipients + WIP exclusions are managed on the Settings page in the portal — no code edits.
