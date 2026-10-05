# BSC Sticker Print Agent — install (Windows, office PC)

The agent lets any phone/browser send a sticker to the USB printer: the app queues
the job on the QMS server, this agent (on the PC the printer is plugged into) picks
it up every ~2 s and prints it RAW.

## 1. One-time server setting
On the QMS server (Vercel → Project → Settings → Environment Variables), add:

    AGENT_TOKEN = <a long random secret>

Redeploy. Put the **same** value in `config.json` → `agent_token`.

## 2. Put the files on the PC
Create `C:\bsc-agent` and copy `agent.py` and `config.json` into it.
Edit `config.json`:
- `server_url` → `https://qms.bharatsteels.in`
- `agent_token` → the same secret as AGENT_TOKEN above
- `printers` → the exact Windows printer name (check Settings → Printers; default `H30C-lite (Sticker)`)

Python + requests + pywin32 are already installed on this PC. If ever needed:

    pip install requests pywin32

## 3. Run it at logon (background, no window)
Open Command Prompt **as Administrator** and run (one line):

    schtasks /Create /TN "BSC Sticker Agent" /TR "pythonw C:\bsc-agent\agent.py" /SC ONLOGON /RL HIGHEST /F

Start it now without logging off:

    schtasks /Run /TN "BSC Sticker Agent"

Check it's printing: open `C:\bsc-agent\agent.log` — you should see
`BSC print agent started`.

## 4. Stop / uninstall

    schtasks /End    /TN "BSC Sticker Agent"      (stop now)
    schtasks /Delete /TN "BSC Sticker Agent" /F   (remove the task)

## Notes
- The job carries its own `SIZE`, so the printer's paper setting doesn't matter.
- Single instance only (a second copy exits immediately).
- Survives network drops and 401s (logs and keeps retrying).
- Logs rotate (5 × 1 MB) in `C:\bsc-agent\agent.log`.
