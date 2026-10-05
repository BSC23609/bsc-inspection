#!/usr/bin/env python3
"""BSC Sticker print agent (long-polling).
Waits on the QMS server for queued TSPL jobs and prints them RAW to the Windows
printer. Runs head-less under the SYSTEM account as a scheduled task."""
import time, json, base64, logging, os, sys, socket
from logging.handlers import RotatingFileHandler
import requests, win32print

HERE   = os.path.dirname(os.path.abspath(__file__))
CFG    = json.load(open(os.path.join(HERE, 'config.json'), 'r', encoding='utf-8'))
BASE   = CFG['server_url'].rstrip('/')
AGENT  = CFG.get('agent_id', 'office-pc')
TOKEN  = CFG['agent_token']
PRINTERS = CFG.get('printers', {})
HTTP_TIMEOUT = float(CFG.get('http_timeout_seconds', 30))   # > server long-poll wait
ERR_BACKOFF  = float(CFG.get('error_backoff_seconds', 3))
HDRS = {'X-Agent-Token': TOKEN}

log = logging.getLogger('bsc-agent'); log.setLevel(logging.INFO)
_h = RotatingFileHandler(os.path.join(HERE, 'agent.log'), maxBytes=1_000_000, backupCount=5, encoding='utf-8')
_h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
log.addHandler(_h)
# only add a console handler when a console actually exists (not under pythonw/SYSTEM)
if sys.stderr is not None:
    try: log.addHandler(logging.StreamHandler())
    except Exception: pass

_lock = None
def single_instance():
    """Hold a loopback socket for the process lifetime; a 2nd copy exits."""
    global _lock
    try:
        _lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _lock.bind(('127.0.0.1', 48917))
    except OSError:
        log.info('Another agent instance is already running - exiting this one.')
        sys.exit(0)

def raw_print(printer_name, data_bytes, doc_name='BSC Sticker'):
    h = win32print.OpenPrinter(printer_name)
    try:
        win32print.StartDocPrinter(h, 1, (doc_name, None, 'RAW'))
        win32print.StartPagePrinter(h)
        win32print.WritePrinter(h, data_bytes)
        win32print.EndPagePrinter(h); win32print.EndDocPrinter(h)
    finally:
        win32print.ClosePrinter(h)

def handle_job(j):
    jid = j['id']; pk = j.get('printer_key', 'default')
    printer = PRINTERS.get(pk) or PRINTERS.get('default') or 'H30C-lite (Sticker)'
    data = base64.b64decode(j['tspl_base64'])
    log.info('job %s -> "%s" (%d bytes, copies=%s)', jid, printer, len(data), j.get('copies'))
    try:
        raw_print(printer, data, 'BSC Sticker %s' % jid)
        requests.post(BASE + '/api/print-jobs/%s/done' % jid, headers=HDRS, timeout=20)
        log.info('job %s printed', jid)
    except Exception as e:
        log.exception('job %s print FAILED', jid)
        try: requests.post(BASE + '/api/print-jobs/%s/failed' % jid, headers=HDRS,
                           json={'error': str(e)[:400]}, timeout=20)
        except Exception: pass

def main():
    single_instance()
    log.info('BSC print agent started. server=%s agent=%s', BASE, AGENT)
    while True:                              # never exits
        try:
            r = requests.get(BASE + '/api/print-jobs/next',
                             params={'agent': AGENT}, headers=HDRS, timeout=HTTP_TIMEOUT)
            if r.status_code == 401:
                log.error('401 bad agent token - config.json agent_token must equal server AGENT_TOKEN')
                time.sleep(30); continue
            r.raise_for_status()
            j = r.json()
            if j.get('none'):
                continue                     # long-poll returned empty -> immediately ask again
            handle_job(j)
        except requests.exceptions.ReadTimeout:
            continue                         # our HTTP read timed out before the server replied; just re-ask
        except requests.RequestException as e:
            log.warning('network error: %s', e); time.sleep(ERR_BACKOFF)
        except Exception as e:
            log.exception('loop error: %s', e); time.sleep(ERR_BACKOFF)

if __name__ == '__main__':
    while True:                              # outer guard: survive even a crash in main()
        try: main()
        except SystemExit: raise
        except Exception:
            log.exception('fatal - restarting loop'); time.sleep(ERR_BACKOFF)
