"""Real browser-profile boundary and sandbox checks; loopback proxy only."""
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

import guard
import regional
import singularity_guard as sg
from test_guard import FakeProxy, FakeHandler


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.browser-test-', dir=str(guard.ROOT.parent))
        self.project = Path(self.temp.name)
        self.work = tempfile.TemporaryDirectory(prefix='browser-test-', dir=str(sg.STORAGE / 'work'))
        self.runtime = Path(self.work.name)
        self.build = sg.builder(self.project, direct=True, application_profile='browser')
        self.build.set_region(regional.validate_region({'ip': '8.8.8.8', 'country_code': 'US',
                                                       'timezone': 'America/New_York'}, '8.8.8.8'))
        self.proxy = FakeProxy(('127.0.0.1', 0), FakeHandler)
        self.proxy.requests = []
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.proxy.shutdown()
        self.proxy.server_close()
        shutil.rmtree(self.build.copy.folder, ignore_errors=True)
        shutil.rmtree(sg.STORAGE / 'browser-state' / hashlib.sha256(os.fsencode(self.project)).hexdigest()[:24], ignore_errors=True)
        self.work.cleanup()
        self.temp.cleanup()

    def run_container(self, command, timeout=25):
        with guard.bridge(self.runtime, self.proxy.server_address[1], 'test:secret'):
            args = self.build(self.runtime, command, str(self.project))
            env = sg.environment()
            env['SINGULARITYENV_ANTHROPIC_API_KEY'] = 'DUMMY_MUST_NOT_REACH_BROWSER'
            return subprocess.run(args, env=env, cwd=sg.STORAGE / 'work/launch',
                                  text=True, capture_output=True, timeout=timeout)

    def test_browser_local_ipc_does_not_open_direct_network_or_host_files(self):
        script = r'''
import errno,json,os,pathlib,socket,subprocess,urllib.request
assert not any(k.startswith('ANTHROPIC_') for k in os.environ)
assert [name for _,name in socket.if_nameindex()]==['lo']
with socket.socket(socket.AF_UNIX):pass
for family,host in ((socket.AF_INET,'1.1.1.1'),(socket.AF_INET6,'2606:4700:4700::1111')):
 for kind,port in ((socket.SOCK_STREAM,443),(socket.SOCK_DGRAM,53),(socket.SOCK_DGRAM,3478)):
  with socket.socket(family,kind) as s:
   try:
    if kind==socket.SOCK_STREAM:s.connect((host,port))
    else:s.sendto(b'bounded-test',(host,port))
    raise AssertionError('Direct route exists')
   except OSError as e:assert e.errno==errno.ENETUNREACH,e
assert not pathlib.Path(%r).exists()
assert not pathlib.Path('/var/run/docker.sock').exists()
for target in ('/opt/browser/manifest.json','/opt/claude-safe/entry.py','/etc/localtime'):
 try:
  with open(target,'a'):pass
  raise AssertionError('Protected file writable')
 except OSError:pass
with urllib.request.urlopen('http://fake.invalid/') as r:assert r.read()==b'proxy-only'
child="import socket; s=socket.socket(); assert s.connect_ex(('1.1.1.1',443))!=0"
subprocess.run(['/usr/bin/python3','-I','-c',child],env={'PATH':'/usr/bin:/bin'},check=True)
print('BOUNDARIES_OK')
''' % str(sg.STORAGE / 'images')
        result = self.run_container(['/usr/bin/python3', '-I', '-c', script])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('BOUNDARIES_OK', result.stdout)
        policy = json.loads((self.runtime / 'launch-1/policy.json').read_text())
        self.assertEqual(policy['binds']['/opt/browser']['mode'], 'ro')
        expected_state = sg.STORAGE / 'browser-state' / hashlib.sha256(os.fsencode(self.project)).hexdigest()[:24]
        self.assertEqual(policy['binds']['/home/claude']['root'], sg.mount_source(expected_state)['root'])

    def test_real_chromium_native_sandbox_region_and_rtc(self):
        result = self.run_container(['/opt/node/bin/node', '/opt/claude-safe/browser.js',
                                     'http://fake.invalid/'], timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        reports = list((self.project / 'browser-results').glob('*/report.json'))
        self.assertEqual(len(reports), 1)
        report = json.loads(reports[0].read_text())
        self.assertTrue(report['completed'])
        self.assertEqual(report['http_status'], 200)
        self.assertEqual(report['device']['timezone'], 'America/New_York')
        self.assertEqual(report['device']['language'], 'en-US')
        self.assertEqual(report['device']['languages'], ['en-US'])
        self.assertTrue(report['rtc']['api_available'])
        self.assertTrue(report['rtc']['offer_created'])
        self.assertEqual(report['rtc']['candidates'], [])
        self.assertFalse(report['rtc'].get('error'))
        self.assertTrue(any(request.startswith(b'GET http://fake.invalid/') for request in self.proxy.requests))
        self.assertTrue((reports[0].parent / 'page.png').is_file())


if __name__ == '__main__':
    unittest.main(verbosity=2)
