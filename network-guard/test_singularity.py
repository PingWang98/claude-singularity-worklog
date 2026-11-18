"""Real unprivileged Singularity tests; fake upstreams, no model requests."""
import json
import os
from pathlib import Path
import selectors
import select
import pty
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import guard
import singularity_guard as sg
import test_guard as original
import test_lifecycle as lifecycle


class SingularityTests(original.GuardTests):
    direct = False

    def setUp(self):
        self.proxy = original.FakeProxy(('127.0.0.1', 0), original.FakeHandler)
        self.proxy.requests = []
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        self.temp = tempfile.TemporaryDirectory(prefix='integration-', dir=str(sg.STORAGE / 'work'))
        self.fixture = Path(self.temp.name)
        self.runtime = self.fixture / 'runtime'
        self.runtime.mkdir()
        self.project = self.fixture / 'project'
        self.project.mkdir()
        # Test fixtures deliberately live in private storage; normal CLI must
        # reject such projects, so move only the project into the workspace.
        self.project_temp = tempfile.TemporaryDirectory(prefix='.singularity-test-', dir=str(guard.ROOT.parent))
        self.project = Path(self.project_temp.name)
        self.addCleanup(self.project_temp.cleanup)
        self.build = sg.builder(self.project, direct=self.direct)

    def isolated_launch(self, script):
        args = self.build(self.runtime, ['/usr/bin/python3', '-I', '-c', script], str(self.project))
        return args, sg.environment(), sg.STORAGE / 'work/launch'

    def test_filesystem_and_namespace_boundaries(self):
        (self.fixture / 'outside-secret').write_text('MUST_NOT_BE_VISIBLE')
        (self.project / 'escape').symlink_to(self.fixture / 'outside-secret')
        script = '''
import ctypes,errno,json,os,pathlib,socket,subprocess
p=pathlib.Path('/workspace/writable');p.write_text('ok')
results={'project_write':p.read_text()=='ok', 'outside_hidden':not pathlib.Path('/workspace/escape').exists(),
         'host_image_hidden':not pathlib.Path(%r).exists(),
         'home':os.environ['HOME'],'sys_empty':not list(pathlib.Path('/sys').iterdir())}
for target in ('/etc/test-write','/opt/claude-safe/entry.py','/run/claude-safe/client.json'):
 try:
  with open(target,'a'): pass
  results[target]=False
 except OSError: results[target]=True
libc=ctypes.CDLL(None,use_errno=True)
results['new_userns_blocked']=libc.unshare(0x10000000)==-1 and ctypes.get_errno()==errno.EPERM
results['mount_change_blocked']=libc.umount2(b'/sys',2)==-1 and ctypes.get_errno()==errno.EPERM
pathlib.Path('/home/claude/test-home').write_text('ok');pathlib.Path('/home/claude/test-home').unlink()
results['node_threads']=subprocess.run(['/opt/node/bin/node','-e',"require('node:crypto').randomBytes(16,()=>console.log('ok'))"],capture_output=True,text=True).stdout.strip()=='ok'
print(json.dumps(results))
''' % str(sg.STORAGE / 'images')
        with guard.bridge(self.runtime, self.proxy.server_address[1], 'test:secret'):
            result = self.run_isolated(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data.pop('home'), '/home/claude')
        self.assertTrue(all(data.values()), data)
        self.assertFalse((self.project / 'writable').exists(), 'origin was directly writable')

    def test_unexpected_mount_refuses_application(self):
        with guard.bridge(self.runtime, self.proxy.server_address[1], 'test:secret'):
            args = self.build(self.runtime, ['/bin/echo', 'APP_MUST_NOT_RUN'], str(self.project))
            index = args.index(str(sg.IMAGE))
            args[index:index] = ['--bind', str(self.fixture) + ':/unexpected:ro']
            result = subprocess.run(args, env=sg.environment(), cwd=sg.STORAGE / 'work/launch',
                                    capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('APP_MUST_NOT_RUN', result.stdout)
        self.assertIn('isolation preflight failed', result.stderr)

    def test_inherited_bind_and_python_hooks_are_removed(self):
        with mock.patch.dict(os.environ, {'SINGULARITY_BIND': '/:/host',
                                         'SINGULARITYENV_PYTHONPATH': '/workspace',
                                         'LD_PRELOAD': '/workspace/inject.so'}):
            env = sg.environment()
        for key in ('SINGULARITY_BIND', 'SINGULARITYENV_PYTHONPATH', 'LD_PRELOAD'):
            self.assertNotIn(key, env)
        for path in (Path.home(), Path('/'), sg.STORAGE, sg.IMAGE):
            with self.assertRaises(RuntimeError):
                sg.validate_project(path)

    def test_terminal_input_and_ctrl_c(self):
        script = "import time; print('TTY_READY',flush=True); print('ECHO:'+input(),flush=True); time.sleep(120)"
        with guard.bridge(self.runtime, self.proxy.server_address[1], 'test:secret'):
            args, env, cwd = self.isolated_launch(script)
            fd, slave = pty.openpty()
            attach = "import fcntl,termios,os,sys; fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execv(sys.argv[1],sys.argv[1:])"
            process = subprocess.Popen(['/usr/bin/python3', '-I', '-c', attach] + args,
                                       env=env, cwd=cwd, stdin=slave, stdout=slave,
                                       stderr=slave, start_new_session=True)
            os.close(slave)
            output = b''
            def read_until(marker):
                nonlocal output
                until = time.monotonic() + 8
                while marker not in output and time.monotonic() < until:
                    if select.select([fd], [], [], .2)[0]:
                        try:
                            output += os.read(fd, 4096)
                        except OSError:
                            self.fail('PTY closed before readiness: ' + repr(output))
                self.assertIn(marker, output)
            try:
                read_until(b'TTY_READY')
                os.write(fd, b'hello\n')
                read_until(b'ECHO:hello')
                os.write(fd, b'\x03')
                process.wait(timeout=8)
            finally:
                guard.stop(process)
                os.close(fd)

    def tearDown(self):
        shutil.rmtree(self.build.copy.folder, ignore_errors=True)
        super().tearDown()
        import hashlib
        state = sg.STORAGE / 'state' / hashlib.sha256(os.fsencode(self.project)).hexdigest()[:24]
        shutil.rmtree(state, ignore_errors=True)


class SingularityLifecycleTests(lifecycle.LifecycleTests):
    direct = False

    def assert_stopped(self):
        try:
            super().assert_stopped()
        except AssertionError:
            for pid in self.pids:
                if lifecycle.running(pid):
                    print('REMAINING TEST PROCESS', pid, Path('/proc/%d/comm' % pid).read_text().strip(),
                          Path('/proc/%d/status' % pid).read_text().split('PPid:')[1].splitlines()[0], flush=True)
            raise

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.singularity-lifecycle-', dir=str(guard.ROOT.parent))
        self.fixture = Path(self.temp.name)
        driver = self.fixture / 'driver.py'
        payload = "import subprocess,time; subprocess.Popen(['/usr/bin/python3','-c','import time; time.sleep(120)'],start_new_session=True); print('FAKE_CLAUDE_READY',flush=True); time.sleep(120)"
        driver.write_text(
            'import sys\nsys.path.insert(0,%r)\nimport guard, singularity_guard as sg\n' % str(guard.ROOT) +
            'node={"name":"CLAUDE_ONLY_PROXY","type":"socks5","server":"192.0.2.1","port":1080,"udp":False}\n'
            'build=sg.builder(%r,direct=%r)\nfrom pathlib import Path\nPath("copy-id").write_text(build.copy.id)\n'
            'sys.exit(guard.run_session(node,["/usr/bin/python3","-I","-c",%r],%r,builder=build,runtime_parent=str(sg.STORAGE/"work"),environment=sg.environment(),launch_cwd=str(sg.STORAGE/"work/launch")))\n'
            % (str(self.fixture), self.direct, payload, str(Path.home() / '.local/bin/mihomo')))
        self.process = subprocess.Popen([sys.executable, '-I', str(driver)], cwd=str(self.fixture),
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, start_new_session=True)
        self.addCleanup(self.cleanup)
        with selectors.DefaultSelector() as poll:
            poll.register(self.process.stdout, selectors.EVENT_READ)
            self.assertTrue(poll.select(12), 'fake Claude did not start')
        line = self.process.stdout.readline().strip()
        if line != 'FAKE_CLAUDE_READY':
            guard.stop(self.process)
            self.fail('fake Claude failed: ' + self.process.stderr.read())
        self.pids = lifecycle.children(self.process.pid)
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
        self.assertIsNotNone(self.proxy_pid)

    def cleanup(self):
        marker = self.fixture / 'copy-id'
        if marker.exists():
            shutil.rmtree(sg.STORAGE / 'projects' / marker.read_text(), ignore_errors=True)
        super().cleanup()
        runtime = getattr(self, 'runtime', None)
        if runtime is not None and runtime.parent == sg.STORAGE / 'work':
            shutil.rmtree(runtime, ignore_errors=True)
        import hashlib
        shutil.rmtree(sg.STORAGE / 'state' / hashlib.sha256(os.fsencode(self.fixture)).hexdigest()[:24], ignore_errors=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
