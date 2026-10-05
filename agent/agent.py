#!/usr/bin/env python3
"""BSC Sticker print agent. Polls the QMS server for queued TSPL jobs and
prints them RAW to the Windows printer. Runs on the office PC (C:\\bsc-agent)."""
import time, json, base64, logging, os, sys, socket
from logging.handlers import RotatingFileHandler
import requests, win32print

HERE = os.path.dirname(os.path.abspath(__file__))
CFG  = json.load(open(os.path.join(HERE, 'config.json'), 'r', encoding='utf-8'))
BASE = CFG['server_url'].rstrip('/')
AGENT= CFG.get('agent_id', 'office-pc')
TOKEN= CFG['agent_token']
PRINTERS = CFG.get('printers', {})
POLL = float(CFG.get('poll_seconds', 2))
HDRS = {'X-Agent-Token': TOKEN}

log = logging.getLogger('bsc-agent'); log.setLevel(logging.INFO)
_h = RotatingFileHandler(os.path.join(HERE, 'agent.log'), maxBytes=1_000_000, backupCount=5, encoding='utf-8')
_h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
log.addHandler(_h); log.addHandler(logging.StreamHandler())

_lock = None
def single_instance():
    global _lock
    try:
        _lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _lock.bind(('127.0.0.1', 48917))   # held for process lifetime
    except OSError:
        log.error('Another agent instance is already running. Exiting.'); sys.exit(1)

def raw_print(printer_name, data_bytes, doc_name='BSC Sticker'):
    h = win32print.OpenPrinter(printer_name)
    try:
        win32print.StartDocPrinter(h, 1, (doc_name, None, 'RAW'))
        win32print.StartPagePrinter(h)
        win32print.WritePrinter(h, data_bytes)
        win32print.EndPagePrinter(h); win32print.EndDocPrinter(h)
    finally:
        win32print.ClosePrinter(h)

def main():
    single_instance()
    log.info('BSC print agent started. server=%s agent=%s', BASE, AGENT)
    idle = 0
    while True:
        try:
            r = requests.get(BASE + '/api/print-jobs/next', params={'agent': AGENT}, headers=HDRS, timeout=20)
            if r.status_code == 401:
                log.error('401 bad agent token - config.json agent_token must equal the server AGENT_TOKEN env var.'); time.sleep(30); continue
            r.raise_for_status()
            j = r.json()
            if j.get('none'):
                idle = min(idle + 1, 10); time.sleep(POLL if idle < 5 else POLL * 3); continue
            idle = 0
            jid = j['id']; pk = j.get('printer_key', 'default'); 
            printer = PRINTERS.get(pk) or PRINTERS.get('default') or 'H30C-lite (Sticker)'
            data = base64.b64decode(j['tspl_base64'])
            log.info('job %s -> "%s" (%d bytes)', jid, printer, len(data))
            try:
                raw_print(printer, data, 'BSC Sticker %s' % jid)
                requests.post(BASE + '/api/print-jobs/%s/done' % jid, headers=HDRS, timeout=20)
                log.info('job %s done', jid)
            except Exception as e:
                log.exception('job %s failed', jid)
                try: requests.post(BASE + '/api/print-jobs/%s/failed' % jid, headers=HDRS, json={'error': str(e)[:400]}, timeout=20)
                except Exception: pass
            time.sleep(0.3)
        except requests.RequestException as e:
            log.warning('network error: %s', e); time.sleep(5)
        except Exception as e:
            log.exception('loop error: %s', e); time.sleep(5)

if __name__ == '__main__':
    main()
