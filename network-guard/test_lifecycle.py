"""Fault injection with a fake Claude process; no account or model requests."""
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import guard


def children(pid):
    found = []
    try:
        direct = Path('/proc/%d/task/%d/children' % (pid, pid)).read_text().split()
    except OSError:
        return found
    for value in direct:
        child = int(value)
        found.append(child)
        found.extend(children(child))
    return found


def running(pid):
    try:
        return Path('/proc/%d/stat' % pid).read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except OSError:
        return False


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.lifecycle-', dir=str(guard.ROOT.parent))
        self.fixture = Path(self.temp.name)
        binary = self.fixture / 'claude'
        binary.write_text('#!/usr/bin/python3\nimport time\nprint("FAKE_CLAUDE_READY",flush=True)\ntime.sleep(120)\n')
        binary.chmod(0o700)
        driver = self.fixture / 'driver.py'
        driver.write_text(
            'import sys\nsys.path.insert(0,%r)\nimport guard\n' % str(guard.ROOT) +
            'guard.current_node=lambda:("LOCAL-FAULT-TEST", {"name":"CLAUDE_ONLY_PROXY",'
            '"type":"socks5","server":"192.0.2.1","port":1080,"udp":False})\n'
            'sys.exit(guard.main())\n')
        env = guard.child_env()
        env['PATH'] = str(self.fixture) + os.pathsep + env['PATH']
        self.process = subprocess.Popen([sys.executable, str(driver)], env=env,
                                        cwd=str(self.fixture), stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.addCleanup(self.cleanup)
        with selectors.DefaultSelector() as poll:
            poll.register(self.process.stdout, selectors.EVENT_READ)
            self.assertTrue(poll.select(12), 'fake Claude did not start')
        self.assertEqual(self.process.stdout.readline().strip(), 'FAKE_CLAUDE_READY')
        self.pids = children(self.process.pid)
        self.proxy_pid = None
        self.runtime = None
        for pid in self.pids:
            try:
                args = Path('/proc/%d/cmdline' % pid).read_bytes().split(b'\0')
                if b'-d' in args and b'-f' in args and any(b'mihomo' in a for a in args):
                    self.proxy_pid = pid
                    self.runtime = Path(os.fsdecode(args[args.index(b'-d') + 1]))
            except OSError:
                pass
        self.assertIsNotNone(self.proxy_pid, 'test-owned proxy not found')

    def cleanup(self):
        guard.stop(self.process)
        for pid in getattr(self, 'pids', []):
            if running(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.process.communicate(timeout=5)
        runtime = getattr(self, 'runtime', None)
        if runtime is not None and runtime.parent == Path(tempfile.gettempdir()) and runtime.name.startswith('claude-safe-'):
            shutil.rmtree(runtime, ignore_errors=True)
        self.temp.cleanup()

    def assert_stopped(self):
        until = time.monotonic() + 7
        alive = self.pids
        while alive and time.monotonic() < until:
            alive = [pid for pid in self.pids if running(pid)]
            if alive:
                time.sleep(.1)
        self.assertEqual(alive, [], 'test-owned descendants remained after failure')

    def test_dedicated_proxy_crash_stops_claude(self):
        os.kill(self.proxy_pid, signal.SIGKILL)
        self.process.wait(timeout=10)
        self.assertNotEqual(self.process.returncode, 0)
        self.assert_stopped()

    def test_launcher_sigterm_cleans_children(self):
        self.process.terminate()
        self.process.wait(timeout=10)
        self.assert_stopped()
        self.assertFalse(self.runtime.exists())

    def test_launcher_sigkill_leaves_no_network_process(self):
        self.process.kill()
        self.process.wait(timeout=10)
        self.assert_stopped()
        self.assertFalse((self.runtime / 'proxy.json').exists(), 'node credential config remained')


if __name__ == '__main__':
    unittest.main(verbosity=2)
