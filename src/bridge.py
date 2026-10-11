#!/usr/bin/env python3
"""mcrb: single-worker Recovery HTTP bridge. No automatic redispatch."""
import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit, parse_qs
from urllib.request import urlopen, Request
import uuid

ROOT = Path(__file__).resolve().parent
LOCK = threading.RLock()
STALE = 20


def stamp():
    return datetime.now(timezone.utc).isoformat()


def atomic(path, payload):
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('wb') as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.jobs = self.path / 'jobs'
        self.jobs.mkdir(parents=True, exist_ok=True)
        self.worker = {'connected': False, 'last_seen': 0, 'state': 'none'}
        self.stopping = False
        # A restart cannot prove whether a dispatched command executed.
        for j in self.all():
            if j['status'] == 'running':
                j.update(status='unknown', reason='server restarted before completion')
                self.save(j)

    def all(self):
        return sorted((json.loads(p.read_text()) for p in self.jobs.glob('*/job.json')),
                      key=lambda j: j['submitted_at'])

    def directory(self, ident):
        if not re.fullmatch('[0-9a-f]{32}', ident):
            raise ValueError('invalid job id')
        return self.jobs / ident

    def get(self, ident):
        return json.loads((self.directory(ident) / 'job.json').read_text())

    def save(self, job):
        atomic(self.directory(job['id']) / 'job.json', json.dumps(job, indent=2).encode())

    def refresh(self):
        self.worker['connected'] = time.time() - self.worker['last_seen'] < STALE
        for j in self.all():
            if j['status'] == 'running':
                if not self.worker['connected']:
                    j.update(status='unknown', reason='heartbeat expired; execution not confirmed')
                    self.save(j)
                elif not j['start_at'] and time.time() - datetime.fromisoformat(
                        j['dispatched_at']).timestamp() > STALE:
                    j.update(status='unknown', reason='dispatch not acknowledged; never reissued')
                    self.save(j)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, data, code=200, mime='text/plain'):
        if isinstance(data, (dict, list)):
            data, mime = json.dumps(data).encode(), 'application/json'
        elif isinstance(data, str):
            data = data.encode()
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.handle_request(False)

    def do_POST(self):
        self.handle_request(True)

    def handle_request(self, post):
        self.connection.settimeout(15)
        try:
            url = urlsplit(self.path)
            parts = url.path.strip('/').split('/')
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            size = int(self.headers.get('Content-Length', 0))
            if size < 0 or size > 256 * 1024 * 1024:
                return self.reply('body too large', 413)
            body = self.rfile.read(size) if post else b''
            if len(body) != size and post:
                return self.reply('incomplete body', 400)
            with LOCK:
                store = self.server.store
                store.refresh()
                if not post and parts == ['b']:
                    # Download fully before executing; no partial curl pipe execution.
                    base = self.server.public_url
                    return self.reply((ROOT / 'worker.sh').read_text().replace(
                        'BASE=${1:?server URL required}', f"BASE=${{1:-{base}}}"))
                if not post and parts == ['status']:
                    return self.reply({'worker': store.worker, 'stopping': store.stopping,
                                       'jobs': store.all(), 'results': str(store.jobs),
                                       'preflight_report': str(store.path / 'preflight.txt')
                                       if (store.path / 'preflight.txt').exists() else None})
                if post and parts == ['preflight', 'select']:
                    path = json.loads(body)['path']
                    if path not in ('/tmp', '/private/tmp', '/private/var/tmp',
                                    '/System/Volumes/Data/private/tmp',
                                    '/System/Volumes/Data/private/var/tmp'):
                        raise ValueError('path is not a supported temporary location')
                    if not (store.path / 'preflight.txt').exists():
                        raise ValueError('worker preflight report not received; inspect it first')
                    atomic(store.path / 'temp-root', (path + '\n').encode())
                    return self.reply({'path': path})
                if parts == ['preflight']:
                    report = store.path / 'preflight.txt'
                    choice = store.path / 'temp-root'
                    if post:
                        # A new startup needs fresh verification; retrying the same
                        # report after a lost response does not revoke a selection.
                        if not report.exists() or report.read_bytes() != body:
                            choice.unlink(missing_ok=True)
                        atomic(report, body)
                        atomic(store.path / 'preflight.json', json.dumps(dict(
                            received_at=stamp(), source=self.client_address[0])).encode())
                        return self.reply('OK\n')
                    if store.stopping:
                        return self.reply('STOP\n')
                    if not choice.exists():
                        return self.reply('WAIT\n')
                    return self.reply(choice.read_text().strip() + '\n')
                if post and parts == ['submit']:
                    if store.stopping:
                        return self.reply('worker stopping', 409)
                    req = json.loads(body)
                    timeout = int(req.get('timeout', 60))
                    if not 1 <= timeout <= 3600:
                        raise ValueError('timeout must be 1..3600 seconds')
                    ident = uuid.uuid4().hex
                    directory = store.directory(ident)
                    directory.mkdir()
                    atomic(directory / 'script.sh', req['script'].encode())
                    job = dict(id=ident, submitted_at=stamp(), timeout=timeout,
                               status='queued', start_at=None, end_at=None, exit_code=None)
                    store.save(job)  # Published only after the entire script is durable.
                    return self.reply(job)
                if parts == ['stop'] and post:
                    store.stopping = True
                    return self.reply({'stopping': True})
                if parts == ['heartbeat'] and post:
                    wid = q['worker']
                    if store.worker['connected'] and store.worker.get('id') != wid:
                        return self.reply('another worker connected', 409)
                    store.worker = dict(id=wid, last_seen=time.time(), at=stamp(),
                                        state=q.get('state', 'idle'), connected=True)
                    return self.reply('STOP\n' if store.stopping else 'OK\n')
                if parts == ['next'] and not post:
                    if store.stopping:
                        return self.reply('STOP\n')
                    wid = q['worker']
                    if store.worker['connected'] and store.worker.get('id') != wid:
                        return self.reply('another worker connected', 409)
                    jobs = store.all()
                    if any(j['status'] in ('running', 'unknown') for j in jobs):
                        return self.reply('WAIT\n')
                    for j in jobs:
                        if j['status'] == 'queued':
                            j.update(status='running', worker=wid, dispatched_at=stamp())
                            store.save(j)  # Never redispatch even if this response is lost.
                            return self.reply(f"{j['id']} {j['timeout']}\n")
                    return self.reply('WAIT\n')
                if parts[0] == 'job' and len(parts) >= 2:
                    j = store.get(parts[1])
                    directory = store.directory(j['id'])
                    if len(parts) == 2 and not post:
                        return self.reply(j)
                    action = parts[2]
                    if not post and action in ('script.sh', 'stdout', 'stderr'):
                        return self.reply((directory / action).read_bytes())
                    if j.get('worker') != q.get('worker'):
                        return self.reply('worker mismatch', 409)
                    if post and action == 'start':
                        # Retried acknowledgement is safe, execution starts after this succeeds.
                        if j['status'] not in ('running', 'unknown'):
                            return self.reply('already finalized', 409)
                        if not j['start_at']:
                            j['start_at'] = body.decode().strip()
                            store.save(j)
                        return self.reply('OK\n')
                    if post and action in ('stdout', 'stderr'):
                        if j['status'] in ('running', 'unknown'):
                            atomic(directory / action, body)
                        return self.reply('OK\n')
                    if post and action == 'artifact' and len(parts) == 4:
                        name = parts[3]
                        if not re.fullmatch('[A-Za-z0-9_.-]+', name) or name in ('.', '..'):
                            raise ValueError('invalid artifact filename')
                        (directory / 'artifacts').mkdir(exist_ok=True)
                        atomic(directory / 'artifacts' / name, body)
                        return self.reply('OK\n')
                    if post and action == 'finish':
                        if j['status'] in ('completed', 'failed'):
                            return self.reply(j)  # Lost response: idempotent commit.
                        if not all((directory / name).exists() for name in ('stdout', 'stderr')):
                            return self.reply('outputs not uploaded', 409)
                        lines = body.decode().splitlines()
                        if len(lines) != 3 or lines[2] not in ('normal', 'timeout', 'interrupted'):
                            raise ValueError('invalid completion record')
                        code = int(lines[1])
                        j.update(end_at=lines[0], exit_code=code, outcome=lines[2],
                                 received_at=stamp(), status='completed' if code == 0 and
                                 lines[2] == 'normal' else 'failed')
                        j.pop('reason', None)
                        store.save(j)
                        return self.reply(j)
                self.reply('not found', 404)
        except (ValueError, KeyError, IndexError) as e:
            self.reply(str(e), 400)
        except FileNotFoundError:
            self.reply('not found', 404)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass


def request(base, path, body=None):
    if body is not None:
        body = json.dumps(body).encode()
    with urlopen(Request(base + path, data=body), timeout=10) as response:
        return json.loads(response.read())


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url', default='http://127.0.0.1:8765')
    p.add_argument('--state', default=str(ROOT.parent / 'sessions' / 'default'))
    sub = p.add_subparsers(dest='command', required=True)
    serve = sub.add_parser('serve')
    serve.add_argument('--host', default='0.0.0.0')
    serve.add_argument('--port', default=8765, type=int)
    serve.add_argument('--public-url')
    for name in ('submit', 'submit-and-wait'):
        cmd = sub.add_parser(name)
        cmd.add_argument('script', help='script file, or - for stdin')
        cmd.add_argument('--timeout', default=60, type=int)
        cmd.add_argument('--wait-seconds', default=120, type=int)
    wait = sub.add_parser('wait')
    wait.add_argument('id')
    wait.add_argument('--wait-seconds', default=120, type=int)
    sub.add_parser('status')
    sub.add_parser('stop')
    select = sub.add_parser('select-temp', help='select worker storage after inspecting preflight')
    select.add_argument('path')
    args = p.parse_args()
    if args.command == 'serve':
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        server.store = Store(args.state)
        server.public_url = args.public_url or f'http://127.0.0.1:{args.port}'
        print(f'Listening on {args.host}:{args.port}; results: {server.store.jobs}', flush=True)
        server.serve_forever()
        return
    if args.command == 'select-temp':
        print(json.dumps(request(args.url, '/preflight/select', {'path': args.path}), indent=2))
        return
    if args.command in ('status', 'stop'):
        print(json.dumps(request(args.url, '/' + args.command,
                                 {} if args.command == 'stop' else None), indent=2))
        return
    if args.command in ('submit', 'submit-and-wait'):
        import sys
        script = sys.stdin.read() if args.script == '-' else Path(args.script).read_text()
        job = request(args.url, '/submit', {'script': script, 'timeout': args.timeout})
        print(job['id'], flush=True)
        if args.command == 'submit':
            return
        ident = job['id']
    else:
        ident = args.id
    deadline = time.monotonic() + args.wait_seconds
    while True:
        j = request(args.url, '/job/' + ident)
        if j['status'] in ('completed', 'failed', 'unknown'):
            print(json.dumps(j, indent=2))
            for name in ('stdout', 'stderr'):
                try:
                    with urlopen(args.url + f'/job/{ident}/{name}', timeout=10) as r:
                        print(f'--- {name} ---\n' + r.read().decode(errors='replace'))
                except OSError:
                    pass
            raise SystemExit(0 if j['status'] == 'completed' else 1)
        if time.monotonic() >= deadline:
            print(f'Wait ended; job remains {j["status"]}. Use wait {ident}.')
            raise SystemExit(2)
        time.sleep(.5)


if __name__ == '__main__':
    main()
