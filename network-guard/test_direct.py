"""Direct mounts with dummy files and fake proxies; no model requests."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import guard
import singularity_guard as sg
import test_hardening
import test_singularity
from project_copy import ProjectCopy
from project_direct import DirectProject


class DirectProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.direct-unit-', dir=str(guard.ROOT.parent))
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.origin = self.base / 'project'
        self.origin.mkdir()
        self.storage = self.base / 'storage'
        self.storage.mkdir()

    def test_no_copy_or_content_reads_even_for_large_file(self):
        with open(self.origin / 'large', 'wb') as file:
            file.truncate(2 ** 30)
        with mock.patch('project_copy.os.read', side_effect=AssertionError('file content read')):
            session = DirectProject.create(self.origin, self.storage)
        self.assertEqual(session.workspace, self.origin)
        self.assertFalse((session.folder / 'workspace').exists())
        self.assertLess(sum(p.stat().st_size for p in session.folder.iterdir()), 4096)
        self.assertEqual(session.metadata['mode'], 'direct')
        with self.assertRaisesRegex(RuntimeError, '直接挂载'):
            ProjectCopy(session.folder)
        with self.assertRaisesRegex(RuntimeError, '直接挂载'):
            session.publish()

    def test_existing_hardlink_refused(self):
        outside = self.base / 'outside'
        outside.write_text('dummy')
        os.link(outside, self.origin / 'alias')
        with self.assertRaisesRegex(RuntimeError, '硬链接'):
            DirectProject.create(self.origin, self.storage)
        self.assertFalse((self.storage / 'projects').exists())

    def test_escaping_symlink_chain_refused(self):
        (self.origin / 'inside').symlink_to('.')
        (self.origin / 'escape').symlink_to('inside/../outside')
        with self.assertRaises(RuntimeError):
            DirectProject.create(self.origin, self.storage)

    def test_special_file_refused_without_opening(self):
        os.mkfifo(self.origin / 'fifo')
        with self.assertRaisesRegex(RuntimeError, 'FIFO'):
            DirectProject.create(self.origin, self.storage)

    def test_all_shared_projects_checked_before_creating_session(self):
        second = self.base / 'second'
        second.mkdir()
        outside = self.base / 'outside'
        outside.write_text('dummy')
        os.link(outside, second / 'alias')
        with self.assertRaisesRegex(RuntimeError, '硬链接'):
            DirectProject.create(self.origin, self.storage, additional=[second])
        self.assertFalse((self.storage / 'projects').exists())


class DirectNetworkTests(test_singularity.SingularityTests):
    direct = True

    def test_multiple_projects_share_one_container_without_copies(self):
        with tempfile.TemporaryDirectory(prefix='.multi-project-', dir=str(guard.ROOT.parent)) as directory:
            base = Path(directory)
            first, second = base / 'a/shared', base / 'b/shared'
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            (base / 'unselected').write_text('DUMMY_PRIVATE')
            shutil.rmtree(self.build.copy.folder)
            self.build = sg.builder(self.project, direct=True, extra_projects=[first, second, first])
            self.assertEqual(self.build.project_mounts, [
                (self.project, '/workspace'), (first, '/projects/shared'), (second, '/projects/shared-2')])
            script = '''
import errno,pathlib,socket
pathlib.Path('/workspace/main.txt').write_text('main')
pathlib.Path('/projects/shared/first.txt').write_text('first')
pathlib.Path('/projects/shared-2/second.txt').write_text('second')
assert not pathlib.Path('/projects/unselected').exists()
assert not pathlib.Path(%r).exists()
with socket.socket() as connection:
    assert connection.connect_ex(('198.51.100.1',443))==errno.ENETUNREACH
print('MULTI_PROJECT_OK')
''' % str(base / 'unselected')
            with guard.bridge(self.runtime, self.proxy.server_address[1], 'test:secret'):
                result = self.run_isolated(script)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'MULTI_PROJECT_OK')
            self.assertEqual((self.project / 'main.txt').read_text(), 'main')
            self.assertEqual((first / 'first.txt').read_text(), 'first')
            self.assertEqual((second / 'second.txt').read_text(), 'second')
            self.assertEqual(self.build.copy.projects, [self.project, first, second])
            self.assertFalse((self.build.copy.folder / 'workspace').exists())

    def test_filesystem_and_namespace_boundaries(self):
        (self.project / 'existing').write_text('before')
        (self.project / 'delete-me').write_text('dummy')
        expected_inode = (self.project / 'existing').stat().st_ino
        script = '''
import json,pathlib
root=pathlib.Path('/workspace')
assert (root/'existing').read_text()=='before'
inode=(root/'existing').stat().st_ino
(root/'existing').write_text('after')
(root/'new').write_text('created')
(root/'delete-me').unlink()
assert not pathlib.Path(%r).exists()
assert not pathlib.Path(%r).exists()
print(json.dumps({'inode':inode}))
''' % (str(sg.STORAGE / 'images'), str(sg.STORAGE / 'runtime/current'))
        with guard.bridge(self.runtime, self.proxy.server_address[1], 'test:secret'):
            result = self.run_isolated(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['inode'], expected_inode)
        self.assertEqual((self.project / 'existing').read_text(), 'after')
        self.assertEqual((self.project / 'new').read_text(), 'created')
        self.assertFalse((self.project / 'delete-me').exists())
        self.assertFalse((self.build.copy.folder / 'workspace').exists())


class DirectLifecycleTests(test_singularity.SingularityLifecycleTests):
    direct = True


class DirectMonitorTests(test_hardening.MonitoredLifecycleTests):
    direct = True


if __name__ == '__main__':
    unittest.main(verbosity=2)
