"""Default host Conda reuse and temporary installs, without external downloads."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
import zipfile

import guard
import singularity_guard as sg
from test_guard import FakeProxy, FakeHandler


class CondaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.conda-test-', dir=str(guard.ROOT.parent))
        self.project = Path(self.temp.name)
        self.work = tempfile.TemporaryDirectory(prefix='conda-test-', dir=str(sg.STORAGE / 'work'))
        self.runtime = Path(self.work.name)
        self.build = sg.builder(self.project, direct=True)
        self.proxy = FakeProxy(('127.0.0.1', 0), FakeHandler)
        self.proxy.requests = []
        self.serial = 0
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.proxy.shutdown();self.proxy.server_close()
        shutil.rmtree(self.build.copy.folder, ignore_errors=True)
        shutil.rmtree(sg.STORAGE / 'state' / hashlib.sha256(os.fsencode(self.project)).hexdigest()[:24], ignore_errors=True)
        self.work.cleanup();self.temp.cleanup()

    def run_container(self, command, timeout=30):
        self.serial += 1
        runtime = self.runtime / ('attempt-%d' % self.serial)
        runtime.mkdir()
        with guard.bridge(runtime, self.proxy.server_address[1], 'test:secret'):
            args = self.build(runtime, command, str(self.project))
            env = {key:value for key,value in sg.environment().items()
                   if not key.startswith('SINGULARITYENV_ANTHROPIC')}
            return subprocess.run(args, env=env, cwd=sg.STORAGE / 'work/launch',
                                  text=True, capture_output=True, timeout=timeout)

    def test_default_python_base_packages_readonly_and_activate_existing_env(self):
        script = r'''
import errno,json,os,pathlib,shutil,socket,subprocess
assert shutil.which('python')=='/mnt/example/conda/bin/python'
assert shutil.which('python3')=='/mnt/example/conda/bin/python3'
assert os.environ['CONDA_DEFAULT_ENV']=='base'
p=subprocess.run(['python','-c','import sys,numpy,requests;print(sys.prefix)'],capture_output=True,text=True,check=True)
assert p.stdout.strip()=='/mnt/example/conda'
for target in ('/mnt/example/conda/conda-meta/history',str(pathlib.Path(os.environ['CONDA_PREFIX']).resolve() / 'conda-meta/history')):
 try:
  fd=os.open(target,os.O_WRONLY);os.close(fd);raise AssertionError('Host base writable')
 except OSError as error:assert error.errno in (errno.EROFS,errno.EACCES),error
p=subprocess.run(['/bin/bash','-c','conda activate example-env && python -c "import sys; print(sys.prefix)"'],capture_output=True,text=True,check=True)
assert p.stdout.strip()=='/mnt/example/conda/envs/example-env',p.stdout
with socket.socket() as s:assert s.connect_ex(('1.1.1.1',443))==errno.ENETUNREACH
with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:
 try:s.sendto(b'bounded-test',('1.1.1.1',53));raise AssertionError('DNS direct route exists')
 except OSError as error:assert error.errno==errno.ENETUNREACH
try:socket.socket(socket.AF_UNIX);raise AssertionError('Unix restriction missing')
except OSError as error:assert error.errno==errno.EPERM
print('BASE_OK')
'''
        result = self.run_container(['/usr/bin/python3','-I','-c',script])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('BASE_OK', result.stdout)

    def test_pip_install_defaults_to_temporary_user_site_and_does_not_persist(self):
        wheel = self.project / 'claude_temp_probe-0.0.1-py3-none-any.whl'
        metadata = 'claude_temp_probe-0.0.1.dist-info/'
        with zipfile.ZipFile(wheel, 'w') as archive:
            archive.writestr('claude_temp_probe.py', 'def main():\n print("TEMP_INSTALL_OK")\n')
            archive.writestr(metadata + 'METADATA', 'Metadata-Version: 2.1\nName: claude-temp-probe\nVersion: 0.0.1\n')
            archive.writestr(metadata + 'WHEEL', 'Wheel-Version: 1.0\nGenerator: local-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
            archive.writestr(metadata + 'entry_points.txt', '[console_scripts]\nclaude-temp-probe = claude_temp_probe:main\n')
            archive.writestr(metadata + 'RECORD', '')
        script = r'''
import os,pathlib,subprocess
p=subprocess.run(['python','-m','pip','install','--no-index','--no-deps','/workspace/claude_temp_probe-0.0.1-py3-none-any.whl'],capture_output=True,text=True)
assert p.returncode==0,p.stdout+p.stderr
p=subprocess.run(['python','-c','import claude_temp_probe;print(claude_temp_probe.__file__)'],capture_output=True,text=True,check=True)
assert p.stdout.strip().startswith('/tmp/claude-user/'),p.stdout
p=subprocess.run(['claude-temp-probe'],capture_output=True,text=True,check=True)
assert p.stdout.strip()=='TEMP_INSTALL_OK'
print('TEMP_PIP_OK')
'''
        result = self.run_container(['/usr/bin/python3','-I','-c',script])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('TEMP_PIP_OK', result.stdout)
        second = self.run_container(['python','-c',
            'import importlib.util;assert importlib.util.find_spec("claude_temp_probe") is None;print("CLEAN_OK")'])
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn('CLEAN_OK', second.stdout)

    def test_conda_can_create_offline_temporary_environment(self):
        result = self.run_container(['/bin/bash','-c',
            'conda create --offline --yes --name claude-temp-empty && '
            'test -f /tmp/claude-conda-envs/claude-temp-empty/conda-meta/history && '
            'conda activate claude-temp-empty && test "$CONDA_PREFIX" = /tmp/claude-conda-envs/claude-temp-empty && echo TEMP_CONDA_OK'], timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('TEMP_CONDA_OK', result.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
