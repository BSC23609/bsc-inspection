# BSC Sticker Print Agent - install (Windows, head-less, SYSTEM account)

Prints run near-instantly: the server holds the agent's request open and hands it
a job the moment one is created (long-polling). The agent needs no console and no
logged-in user.

## 1. Server setting (once)
QMS server (Vercel -> Settings -> Environment Variables):

    AGENT_TOKEN        = <a long random secret>     (required)
    PRINT_POLL_SECONDS = 9                           (optional; max safe value for the plan)

Vercel Hobby functions time out at ~10 s, so keep PRINT_POLL_SECONDS <= 9.
On Pro you can raise it to ~25. Redeploy. Put the SAME secret in config.json -> agent_token.

## 2. Files
Copy `agent.py` and `config.json` into `C:\bsc-agent`. Edit `config.json`:
server_url, the matching agent_token, and the exact Windows printer name
(Settings -> Printers; default `H30C-lite (Sticker)`).
Python + requests + pywin32 are already installed on this PC.

## 3. Scheduled tasks (run as SYSTEM, head-less)
Open an **Administrator** Command Prompt and run (each is one line):

Main task - starts at boot, restarts if it ever stops:

    schtasks /Create /TN "BSC Sticker Agent" /RU SYSTEM /RL HIGHEST /SC ONSTART /F ^
      /TR "pythonw C:\bsc-agent\agent.py"

Watchdog - every 5 minutes makes sure it is running (the single-instance lock means
this is a no-op when it already is):

    schtasks /Create /TN "BSC Sticker Agent Watchdog" /RU SYSTEM /RL HIGHEST ^
      /SC MINUTE /MO 5 /F /TR "schtasks /Run /TN \"BSC Sticker Agent\""

Start it now without rebooting:

    schtasks /Run /TN "BSC Sticker Agent"

Check it: open `C:\bsc-agent\agent.log` - you should see `BSC print agent started`.
(`pythonw` has no window; if `pythonw` is not on SYSTEM's PATH, use its full path,
e.g. `C:\Python311\pythonw.exe C:\bsc-agent\agent.py`.)

## 4. Stop / uninstall

    schtasks /End    /TN "BSC Sticker Agent"
    schtasks /Delete /TN "BSC Sticker Agent" /F
    schtasks /Delete /TN "BSC Sticker Agent Watchdog" /F

## Notes
- Long-poll HTTP read timeout is 30 s (config `http_timeout_seconds`), comfortably
  longer than the server's wait; on timeout the agent just asks again.
- Never exits: network errors, bad tokens and crashes are logged and retried after 3 s.
- Single instance only (loopback-port lock). Logs rotate (5 x 1 MB).
- Job status: queued -> printing (agent claimed) -> printed (agent reported done).
  A job is NEVER marked printed on a timeout.
