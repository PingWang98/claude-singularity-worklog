"""Non-billable regression checks for helper bootstrap and stream semantics."""
import concurrent.futures
import json
import os
from pathlib import Path
import socket
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import guard
from inside import Relay


class RegressionTests(unittest.TestCase):
    def test_untrusted_or_direct_proxy_selections_fail_closed(self):
        import io
        config = {'mixed-port': 18080, 'external-controller': '127.0.0.1:18081',
                  'proxies': [{'name': 'NODE', 'type': 'socks5', 'server': 'node.invalid', 'port': 1080}]}
        normal = {'/configs': {'mode': 'rule'}, '/rules': {'rules': [{'type': 'Match', 'proxy': 'OUT'}]},
                  '/proxies': {'proxies': {'OUT': {'now': 'NODE'}, 'NODE': {'type': 'Socks5'}}}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'proxy-fixture/config.yaml'
            path.parent.mkdir(parents=True)
            cases = ('direct_mode', 'direct_group', 'cycle', 'private_node_ip', 'unverified_dialer')
            for case in cases:
                with self.subTest(case=case):
                    data = json.loads(json.dumps(config))
                    replies = json.loads(json.dumps(normal))
                    address = '8.8.8.8'
                    if case == 'direct_mode':
                        replies['/configs']['mode'] = 'direct'
                    elif case == 'direct_group':
                        replies['/proxies']['proxies']['OUT']['now'] = 'DIRECT'
                    elif case == 'cycle':
                        replies['/proxies']['proxies']['OUT']['now'] = 'OUT'
                    elif case == 'private_node_ip':
                        address = '127.0.0.1'
                    elif case == 'unverified_dialer':
                        data['proxies'][0]['dialer-proxy'] = 'DIRECT'
                    path.write_text(json.dumps(data))
                    opener = mock.Mock()
                    opener.open.side_effect = lambda req, **kw: io.BytesIO(json.dumps(
                        replies[guard.urllib.parse.urlsplit(req.full_url).path]).encode())
                    with mock.patch.dict(os.environ, {'HOME': directory, 'CLAUDE_PROXY_CONFIG': str(path), 'https_proxy': 'http://127.0.0.1:18080'}, clear=True), \
                         mock.patch.object(guard.urllib.request, 'build_opener', return_value=opener), \
                         mock.patch.object(guard.socket, 'getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 1080))]):
                        with self.assertRaises(RuntimeError):
                            guard.current_node()

    def test_early_sigkill_never_writes_proxy_credentials_to_disk(self):
        from test_lifecycle import children, running
        with tempfile.TemporaryDirectory(prefix='.early-stop-test-', dir=str(guard.ROOT.parent)) as directory:
            fixture = Path(directory)
            marker = fixture / 'proxy-started.json'
            proxy = fixture / 'mihomo'
            proxy.write_text(
                '#!/usr/bin/python3 -I\nimport sys,os,json,time\nfrom pathlib import Path\n'
                'args=sys.argv\nconfig=args[args.index("-f")+1]\nruntime=args[args.index("-d")+1]\n'
                'with open(config) as f: data=json.load(f)\n'
                'Path(%r).write_text(json.dumps({"pid":os.getpid(),"runtime":runtime,'
                '"memory_config":config.startswith("/proc/self/fd/"),'
                '"fixture_credential_loaded":data["proxies"][0]["password"]=="FAKE_ONLY"}))\n'
                'time.sleep(120)\n' % str(marker))
            proxy.chmod(0o700)
            claude = fixture / 'claude'
            claude.write_text('#!/bin/sh\nexit 99\n')
            claude.chmod(0o700)
            driver = fixture / 'driver.py'
            driver.write_text(
                'import sys\nsys.path.insert(0,%r)\nimport guard\n' % str(guard.ROOT) +
                'guard.current_node=lambda:("EARLY-STOP-TEST",{"name":"CLAUDE_ONLY_PROXY",'
                '"type":"socks5","server":"192.0.2.1","port":1080,"password":"FAKE_ONLY"})\n'
                'sys.exit(guard.main())\n')
            env = guard.child_env()
            env['PATH'] = str(fixture) + os.pathsep + env['PATH']
            process = subprocess.Popen([sys.executable, '-I', str(driver)], env=env,
                                       cwd=str(fixture), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True)
            runtime = None
            pids = []
            try:
                until = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < until and process.poll() is None:
                    time.sleep(.02)
                self.assertTrue(marker.exists(), 'test proxy did not load its configuration')
                status = json.loads(marker.read_text())
                runtime = Path(status['runtime'])
                pids = children(process.pid)
                self.assertTrue(status['memory_config'])
                self.assertTrue(status['fixture_credential_loaded'])
                self.assertFalse((runtime / 'proxy.json').exists())
                process.kill()  # before the proxy opens a port / readiness succeeds
                process.wait(timeout=5)
                until = time.monotonic() + 5
                while running(status['pid']) and time.monotonic() < until:
                    time.sleep(.02)
                self.assertFalse(running(status['pid']))
                self.assertFalse((runtime / 'proxy.json').exists())
            finally:
                if process.poll() is None:
                    pids = children(process.pid)
                guard.stop(process)
                for pid in pids:
                    if running(pid):
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                process.communicate(timeout=5)
                if runtime and runtime.parent == Path(tempfile.gettempdir()) and runtime.name.startswith('claude-safe-'):
                    shutil.rmtree(runtime, ignore_errors=True)

    def test_user_python_startup_hook_cannot_run_in_supervisor(self):
        with tempfile.TemporaryDirectory(prefix='.bootstrap-test-', dir=str(guard.ROOT.parent)) as directory:
            fixture = Path(directory)
            version = subprocess.check_output(
                ['/usr/bin/python3', '-I', '-c', 'import sys;print("%d.%d"%sys.version_info[:2])'],
                text=True).strip()
            packages = fixture / 'lib' / ('python' + version) / 'site-packages'
            packages.mkdir(parents=True)
            marker = fixture / 'injection-ran'
            (packages / 'usercustomize.py').write_text(
                'from pathlib import Path\nPath(%r).write_text("unexpected user startup hook")\n' % str(marker))
            env = guard.child_env()
            env['PYTHONUSERBASE'] = str(fixture)
            with tempfile.TemporaryDirectory(prefix='claude-safe-bootstrap-') as temporary:
                runtime = Path(temporary)
                with socket.socket() as unused:
                    unused.bind(('127.0.0.1', 0))
                    with guard.bridge(runtime, unused.getsockname()[1], 'test:secret'):
                        args = guard.sandbox_command(runtime, ['/usr/bin/python3', '-I', '-c', 'print("OK")'], str(fixture))
                        result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists(), 'usercustomize ran before the agent seccomp filter')

    def test_request_half_close_keeps_response_readable(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(5)
        errors = []

        def origin():
            try:
                conn, _ = listener.accept()
                with conn:
                    conn.settimeout(5)
                    request = bytearray()
                    while True:
                        block = conn.recv(65536)
                        if not block:
                            break
                        request.extend(block)
                    conn.sendall(b'AFTER_EOF:' + request)
            except Exception as error:
                errors.append(type(error).__name__)

        origin_thread = threading.Thread(target=origin, daemon=True)
        origin_thread.start()
        with tempfile.TemporaryDirectory(prefix='claude-safe-half-close-') as temporary:
            with guard.bridge(Path(temporary), listener.getsockname()[1], 'test:secret'):
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(5)
                    client.connect(str(Path(temporary) / 'bridge.sock'))
                    client.sendall(b'request')
                    client.shutdown(socket.SHUT_WR)
                    received = bytearray()
                    while True:
                        block = client.recv(65536)
                        if not block:
                            break
                        received.extend(block)
        origin_thread.join(timeout=6)
        listener.close()
        self.assertEqual(errors, [])
        self.assertEqual(bytes(received), b'AFTER_EOF:request')

    def test_simultaneous_large_streams_are_not_truncated(self):
        from inside import pipe_sockets
        left_client, left_relay = socket.socketpair()
        right_client, right_relay = socket.socketpair()
        for conn in (left_client, left_relay, right_client, right_relay):
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
            conn.settimeout(8)
        errors = []

        def relay():
            try:
                pipe_sockets(left_relay, right_relay)
            except Exception as error:
                errors.append(type(error).__name__)

        thread = threading.Thread(target=relay, daemon=True)
        thread.start()
        a, b = b'A' * (1024 * 1024), b'B' * (1024 * 1024)

        def write(conn, data):
            conn.sendall(data)
            conn.shutdown(socket.SHUT_WR)

        def read(conn):
            data = bytearray()
            while True:
                block = conn.recv(65536)
                if not block:
                    return bytes(data)
                data.extend(block)

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                sent_a = pool.submit(write, left_client, a)
                sent_b = pool.submit(write, right_client, b)
                got_b = pool.submit(read, left_client)
                got_a = pool.submit(read, right_client)
                self.assertEqual(got_a.result(timeout=10), a)
                self.assertEqual(got_b.result(timeout=10), b)
                sent_a.result(timeout=10)
                sent_b.result(timeout=10)
        finally:
            for conn in (left_client, right_client, left_relay, right_relay):
                conn.close()
            thread.join(timeout=2)
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
