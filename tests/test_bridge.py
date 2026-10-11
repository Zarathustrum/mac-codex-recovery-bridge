import json
import os
from pathlib import Path
import socket
import subprocess
import shutil
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1] / 'src'


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            self.port = s.getsockname()[1]
        self.url = f'http://127.0.0.1:{self.port}'
        self.server = self.launch_server()
        self.worker = None
        self.until(lambda: self.request('/status'))

    def launch_server(self):
        return subprocess.Popen(['python3', str(ROOT / 'bridge.py'), '--state', self.tmp.name,
                                 'serve', '--host', '127.0.0.1', '--port', str(self.port)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def request(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        with urllib.request.urlopen(self.url + path, data=data, timeout=2) as r:
            return json.loads(r.read())

    def until(self, fn, seconds=15):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                value = fn()
                if value:
                    return value
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(.15)
        self.fail('timed out waiting for condition')

    def start_worker(self):
        # Local macOS /tmp lives on installed Data; Recovery must reject that mount.
        env = dict(os.environ, BRIDGE_LOCAL_TEST='1')
        with (Path(self.tmp.name) / 'worker.log').open('w') as log:
            self.worker = subprocess.Popen(['/bin/sh', str(ROOT / 'worker.sh'), self.url],
                                           stdout=log, stderr=log, env=env)
        self.until(lambda: self.request('/status')['worker']['connected'])

    def submit(self, script, timeout=10):
        return self.request('/submit', {'script': script, 'timeout': timeout})['id']

    def result(self, job):
        return self.until(lambda: (r if (r := self.request('/job/' + job))['status']
                                   in ('completed', 'failed') else None))

    def output(self, job, name):
        return (Path(self.tmp.name) / 'jobs' / job / name).read_text()

    def tearDown(self):
        if self.worker:
            try:
                self.request('/stop', {})
                self.worker.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                self.worker.terminate()
                self.worker.wait(timeout=10)
        self.server.terminate()
        self.server.wait(timeout=5)
        # The worker deliberately retains its spool for Recovery inspection.
        # Tests remove only the spool named in their own worker's shutdown log.
        log = Path(self.tmp.name) / 'worker.log'
        if log.exists():
            for line in log.read_text().splitlines():
                if line.startswith('Worker stopped. Temporary spool: '):
                    spool = Path(line.split(': ', 1)[1])
                    if (spool.parent == Path('/tmp').resolve() and
                            spool.name.startswith('recovery-bridge.') and not spool.is_symlink()):
                        shutil.rmtree(spool)
        self.tmp.cleanup()

    def test_select_temp_cli_requires_report_and_selects_verified_path(self):
        cmd = ['python3', str(ROOT / 'bridge.py'), '--url', self.url,
               'select-temp', '/private/tmp']
        missing = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(missing.returncode, 0)
        self.assertFalse((Path(self.tmp.name) / 'temp-root').exists())
        with urllib.request.urlopen(self.url + '/preflight', data=b'mount inventory\n') as r:
            self.assertEqual(r.read(), b'OK\n')
        selected = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertEqual(json.loads(selected.stdout)['path'], '/private/tmp')
        with urllib.request.urlopen(self.url + '/preflight') as r:
            self.assertEqual(r.read(), b'/private/tmp\n')

    def test_select_temp_rejects_other_paths(self):
        with urllib.request.urlopen(self.url + '/preflight', data=b'mount inventory\n') as r:
            self.assertEqual(r.read(), b'OK\n')
        for path in ['/Users/example', '/Volumes/Data/private/tmp', '/private/tmp/../../Users', 'tmp']:
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as caught:
                self.request('/preflight/select', {'path': path})
            self.assertEqual(caught.exception.code, 400)
        self.assertFalse((Path(self.tmp.name) / 'temp-root').exists())

    def test_delivery_failure_artifact_and_clean_stop(self):
        self.start_worker()
        job = self.submit('printf hello; printf problem >&2; printf artifact > "$BRIDGE_ARTIFACT_DIR/sample"; exit 7\n')
        result = self.result(job)
        self.assertEqual(result['exit_code'], 7)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.output(job, 'stdout'), 'hello')
        self.assertEqual(self.output(job, 'stderr'), 'problem')
        self.assertEqual(self.output(job, 'artifacts/sample'), 'artifact')
        self.assertTrue(result['start_at'] and result['end_at'])
        self.request('/stop', {})
        self.worker.wait(timeout=10)
        self.assertEqual(self.worker.returncode, 0)

    def test_timeout_descendants_heartbeat_and_next_job(self):
        self.start_worker()
        job = self.submit('sleep 100 &\necho "$!"\nwait\n', timeout=4)
        self.until(lambda: self.request('/status')['worker'].get('state') == job)
        first = self.request('/status')['worker']['last_seen']
        time.sleep(2.5)
        self.assertGreater(self.request('/status')['worker']['last_seen'], first)
        r = self.result(job)
        self.assertEqual(r['outcome'], 'timeout')
        self.assertEqual(r['exit_code'], 124)
        child = int(self.output(job, 'stdout').strip())
        check = subprocess.run(['ps', '-o', 'stat=', '-p', str(child)], capture_output=True, text=True)
        self.assertTrue(check.returncode != 0 or check.stdout.strip().startswith('Z'))
        self.assertEqual(self.result(self.submit('printf next\n'))['status'], 'completed')

    def test_cli_roundtrip_and_stop_during_execution(self):
        self.start_worker()
        script = Path(self.tmp.name) / 'probe.sh'
        script.write_text('uname -a\n')
        probe = subprocess.run(['python3', str(ROOT / 'bridge.py'), '--url', self.url,
                                'submit-and-wait', str(script)], capture_output=True, text=True, timeout=15)
        self.assertEqual(probe.returncode, 0, probe.stderr + probe.stdout)
        self.assertIn('Darwin', probe.stdout)
        job = self.submit('sleep 100\n', timeout=120)
        self.until(lambda: self.request('/job/' + job).get('start_at'))
        self.request('/stop', {})
        self.worker.wait(timeout=12)
        self.assertEqual(self.result(job)['outcome'], 'interrupted')

    def test_disconnect_upload_retry_no_reexecution(self):
        self.start_worker()
        count = Path(self.tmp.name) / 'count'
        job = self.submit(f"printf once >> '{count}'\nsleep 4\nprintf finished\n")
        self.until(lambda: self.request('/job/' + job).get('start_at'))
        self.server.terminate()
        self.server.wait(timeout=5)
        time.sleep(5)
        self.server = self.launch_server()
        self.until(lambda: self.request('/status'))
        self.assertEqual(self.result(job)['status'], 'completed')
        self.assertEqual(count.read_text(), 'once')

    def test_ambiguous_delivery_unknown_never_requeued(self):
        job = self.submit('printf should-not-run\n')
        with urllib.request.urlopen(self.url + '/next?worker=lost') as r:
            self.assertIn(job, r.read().decode())
        time.sleep(1.2)
        # Restart deliberately makes a dispatched job unknown immediately.
        self.server.terminate()
        self.server.wait(timeout=5)
        self.server = self.launch_server()
        self.until(lambda: self.request('/status'))
        self.assertEqual(self.request('/job/' + job)['status'], 'unknown')
        with urllib.request.urlopen(self.url + '/next?worker=other') as r:
            self.assertEqual(r.read(), b'WAIT\n')

    def test_lost_dispatch_with_live_idle_heartbeat_becomes_unknown(self):
        with urllib.request.urlopen(self.url + '/heartbeat?worker=lost&state=idle', data=b'') as r:
            self.assertEqual(r.read(), b'OK\n')
        job = self.submit('printf should-not-run\n')
        with urllib.request.urlopen(self.url + '/next?worker=lost') as r:
            self.assertIn(job, r.read().decode())
        # Advance the dispatch age without a 20-second test sleep.
        path = Path(self.tmp.name) / 'jobs' / job / 'job.json'
        meta = json.loads(path.read_text())
        meta['dispatched_at'] = '2000-01-01T00:00:00+00:00'
        path.write_text(json.dumps(meta))
        self.assertEqual(self.request('/job/' + job)['status'], 'unknown')
        with urllib.request.urlopen(self.url + '/next?worker=lost') as r:
            self.assertEqual(r.read(), b'WAIT\n')

    def test_recovery_temp_preflight_waits_for_verified_path(self):
        report = b'Recovery mount inventory\n'
        with urllib.request.urlopen(self.url + '/preflight', data=report) as r:
            self.assertEqual(r.read(), b'OK\n')
        self.assertEqual((Path(self.tmp.name) / 'preflight.txt').read_bytes(), report)
        with urllib.request.urlopen(self.url + '/preflight') as r:
            self.assertEqual(r.read(), b'WAIT\n')
        (Path(self.tmp.name) / 'temp-root').write_text('/System/Volumes/Data/private/tmp\n')
        with urllib.request.urlopen(self.url + '/preflight') as r:
            self.assertEqual(r.read(), b'/System/Volumes/Data/private/tmp\n')

    def test_actual_worker_preflight_then_execution(self):
        # Exercise startup without the local simulation bypass. Writes remain
        # in this working Mac's /tmp after the test explicitly selects it.
        env = dict(os.environ)
        env.pop('BRIDGE_LOCAL_TEST', None)
        bootstrap = Path(self.tmp.name) / 'mcrb.sh'
        shutil.copyfile(ROOT / 'worker.sh', bootstrap)
        with (Path(self.tmp.name) / 'worker.log').open('w') as log:
            self.worker = subprocess.Popen(['/bin/sh', str(bootstrap), self.url],
                                           stdout=log, stderr=log, env=env)
        job = self.submit('uname -a\n')
        report = Path(self.tmp.name) / 'preflight.txt'
        self.until(lambda: report.exists(), seconds=30)
        self.assertIn('--- mount table ---', report.read_text())
        self.assertEqual(self.request('/job/' + job)['status'], 'queued')
        self.assertFalse(self.request('/status')['worker']['connected'])
        chosen = subprocess.check_output(['/bin/sh', '-c', 'cd /tmp && pwd -P'], text=True)
        (Path(self.tmp.name) / 'temp-root').write_text(chosen)
        self.until(lambda: self.request('/status')['worker']['connected'])
        self.assertEqual(self.result(job)['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
