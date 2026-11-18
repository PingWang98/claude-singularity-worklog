"""File-boundary and monitor fault tests: dummy data, no paid APIs."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import guard
import singularity_guard as sg
from project_copy import ProjectCopy, scan
from network_monitor import ExitMonitor, probe_exit
from launch_protection import Protection
from test_lifecycle import children, running


class CopyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.copy-security-', dir=str(guard.ROOT.parent))
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.origin = self.base / 'project'; self.origin.mkdir()
        (self.origin / 'file').write_text('original')

    def copy(self):
        copy = ProjectCopy.create(self.origin, sg.STORAGE)
        self.addCleanup(shutil.rmtree, copy.folder, True)
        return copy

    def test_existing_external_hardlink_refused(self):
        outside = self.base/'outside'; outside.write_text('DUMMY')
        os.link(outside,self.origin/'link')
        with self.assertRaisesRegex(RuntimeError,'硬链接'):
            self.copy()
        self.assertEqual(outside.read_text(),'DUMMY')

    def test_late_hardlink_cannot_change_private_copy_and_publish_refuses(self):
        copy = self.copy()
        os.link(self.origin/'file',self.base/'outside')
        (copy.workspace/'file').write_text('agent-change')
        self.assertEqual((self.base/'outside').read_text(),'original')
        with self.assertRaisesRegex(RuntimeError,'硬链接'):
            copy.publish()
        self.assertEqual((self.base/'outside').read_text(),'original')

    def test_workspace_hardlink_refused_on_publish(self):
        copy=self.copy();os.link(copy.workspace/'file',copy.workspace/'alias')
        with self.assertRaisesRegex(RuntimeError,'硬链接'):
            copy.publish()
        self.assertEqual((self.origin/'file').read_text(),'original')

    def test_symbolic_link_escape_and_chain_refused(self):
        (self.origin/'escape').symlink_to('../outside')
        with self.assertRaises(RuntimeError):self.copy()
        (self.origin/'escape').unlink()
        (self.origin/'a').symlink_to('.')
        (self.origin/'b').symlink_to('a/../outside')
        with self.assertRaises(RuntimeError):self.copy()

    def test_fifo_refused_without_blocking(self):
        os.mkfifo(self.origin/'fifo')
        with self.assertRaises(RuntimeError):self.copy()

    def test_safe_changes_only_publish_on_explicit_request(self):
        copy=self.copy()
        (copy.workspace/'file').write_text('changed')
        (copy.workspace/'newdir').mkdir();(copy.workspace/'newdir/new').write_text('new')
        self.assertEqual((self.origin/'file').read_text(),'original')
        self.assertEqual(copy.publish(),3)
        self.assertEqual((self.origin/'file').read_text(),'changed')
        self.assertEqual((self.origin/'newdir/new').read_text(),'new')
        self.assertEqual(copy.publish(),0)
        (copy.workspace/'file').unlink();copy.publish()
        self.assertFalse((self.origin/'file').exists())

    def test_host_changes_conflict_and_no_other_file_is_published(self):
        copy=self.copy();(copy.workspace/'file').write_text('agent')
        (copy.workspace/'new').write_text('new');(self.origin/'file').write_text('human')
        with self.assertRaisesRegex(RuntimeError,'冲突'):copy.publish()
        self.assertEqual((self.origin/'file').read_text(),'human')
        self.assertFalse((self.origin/'new').exists())

    def test_host_parent_symlink_cannot_redirect_publish(self):
        (self.origin/'dir').mkdir();(self.origin/'dir/a').write_text('before')
        copy=self.copy();(copy.workspace/'dir/a').write_text('after')
        shutil.rmtree(self.origin/'dir');(self.base/'outside').mkdir()
        (self.origin/'dir').symlink_to(self.base/'outside')
        with self.assertRaises(RuntimeError):copy.publish()
        self.assertEqual(list((self.base/'outside').iterdir()),[])

    def test_apply_while_session_active_refused(self):
        copy=self.copy();(copy.workspace/'file').write_text('agent')
        with copy.lock():
            with self.assertRaisesRegex(RuntimeError,'会话仍在运行'):copy.publish()
        self.assertEqual((self.origin/'file').read_text(),'original')

    def test_directory_deletion_with_new_host_file_conflicts_before_writes(self):
        (self.origin/'dir').mkdir();(self.origin/'dir/a').write_text('a')
        copy=self.copy();shutil.rmtree(copy.workspace/'dir')
        (copy.workspace/'file').write_text('agent')
        (self.origin/'dir/human').write_text('keep')
        with self.assertRaisesRegex(RuntimeError,'冲突'):copy.publish()
        self.assertEqual((self.origin/'file').read_text(),'original')
        self.assertEqual((self.origin/'dir/a').read_text(),'a')

    def test_host_added_files_preserved(self):
        copy=self.copy();(copy.workspace/'file').write_text('agent')
        (self.origin/'human').write_text('keep');copy.publish()
        self.assertEqual((self.origin/'human').read_text(),'keep')


class MonitorTests(unittest.TestCase):
    def monitor(self, probe, **kwargs):
        m=ExitMonitor(probe,interval=.02,lease=.15,**kwargs)
        self.addCleanup(m.close);m.start();self.assertTrue(m.ready.wait(1));return m

    def wait_failure(self,m):
        until=time.monotonic()+1
        while time.monotonic()<until:
            reason=m.failure()
            if reason:return reason
            time.sleep(.01)
        self.fail('monitor did not fail closed')

    def test_exit_change_revokes(self):
        values=iter(['8.8.8.8','1.1.1.1'])
        m=self.monitor(lambda:next(values))
        self.assertEqual(self.wait_failure(m),'exit_address_changed')

    def test_probe_failure_revokes(self):
        count=0
        def probe():
            nonlocal count
            count+=1
            if count>1:raise TimeoutError()
            return '8.8.8.8'
        m=self.monitor(probe)
        self.assertEqual(self.wait_failure(m),'exit_probe_failed')

    def test_stalled_monitor_lease_expires(self):
        release=threading.Event();self.addCleanup(release.set);count=0
        def probe():
            nonlocal count
            count+=1
            if count>1:release.wait(2)
            return '8.8.8.8'
        m=self.monitor(probe)
        self.assertEqual(self.wait_failure(m),'monitor_lease_expired')

    def test_expected_exit_mismatch_refuses_start(self):
        m=self.monitor(lambda:'1.1.1.1',expected='8.8.8.8')
        self.assertEqual(m.failure(),'exit_address_changed')

    def test_probe_ignores_no_proxy_and_never_direct_connects(self):
        with mock.patch.dict(os.environ,{'NO_PROXY':'*','HTTPS_PROXY':'http://evil.invalid:1'}), mock.patch('network_monitor.http.client.HTTPSConnection') as factory:
            conn=factory.return_value;conn.getresponse.return_value.status=200
            conn.getresponse.return_value.read.return_value=b'8.8.8.8'
            self.assertEqual(probe_exit(1234,'dummy:auth'),'8.8.8.8')
            self.assertEqual(factory.call_args.args,('127.0.0.1',1234))
            self.assertEqual(conn.set_tunnel.call_args.args,('api.ipify.org',443))

    def test_failed_startup_check_never_starts_application(self):
        class Reject:
            def prepare(self,*args):raise RuntimeError('fake failed network check')
            def close(self):pass
        with tempfile.TemporaryDirectory(dir=str(sg.STORAGE/'work')) as directory:
            marker=Path(directory)/'application-started'
            def build(*args):return ['/usr/bin/touch',str(marker)]
            node={'name':'CLAUDE_ONLY_PROXY','type':'socks5','server':'192.0.2.1','port':1080}
            with self.assertRaisesRegex(RuntimeError,'fake failed'):
                guard.run_session(node,[],str(Path.home()/'.local/bin/mihomo'),builder=build,
                                  runtime_parent=directory,protection=Reject())
            self.assertFalse(marker.exists())



class MonitoredLifecycleTests(unittest.TestCase):
    direct = False

    def exercise(self, mode):
        with tempfile.TemporaryDirectory(prefix='.monitor-fault-',dir=str(guard.ROOT.parent)) as directory:
            fixture=Path(directory)
            driver=fixture/'driver.py'
            driver.write_text('''import sys,time,threading
from pathlib import Path
sys.path.insert(0,%r)
import guard,singularity_guard as sg
from network_monitor import ExitMonitor
class Controller:
 def prepare(self,*args):
  self.count=0
  def probe():
   self.count+=1
   if self.count>1:
    if %r=='change':return '1.1.1.1'
    if %r=='fail':raise TimeoutError()
    time.sleep(10)
   return '8.8.8.8'
  self.monitor=ExitMonitor(probe,interval=1,lease=1.6)
  self.monitor.start();self.monitor.ready.wait(1)
 def failure(self):return self.monitor.failure()
 def close(self):self.monitor.close()
build=sg.builder(%r,direct=%r)
Path(%r).write_text(build.copy.id)
node={'name':'CLAUDE_ONLY_PROXY','type':'socks5','server':'192.0.2.1','port':1080}
payload="import subprocess,time; subprocess.Popen(['/usr/bin/python3','-c','import time; time.sleep(120)'],start_new_session=True); print('READY',flush=True); time.sleep(120)"
try:
 result=guard.run_session(node,['/usr/bin/python3','-I','-c',payload],%r,builder=build,runtime_parent=str(sg.STORAGE/'work'),environment=sg.environment(),launch_cwd=str(sg.STORAGE/'work/launch'),protection=Controller())
except RuntimeError as e:
 print(str(e),file=sys.stderr);result=125
sys.exit(result)
''' % (str(guard.ROOT),mode,mode,str(fixture),self.direct,str(fixture/'copy-id'),str(Path.home()/'.local/bin/mihomo')))
            process=subprocess.Popen([sys.executable,'-I',str(driver)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
            pids=[]
            try:
                import selectors
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout,selectors.EVENT_READ)
                    self.assertTrue(selector.select(10),'payload not ready')
                self.assertEqual(process.stdout.readline().strip(),'READY')
                pids=children(process.pid)
                out,err=process.communicate(timeout=8)
                self.assertEqual(process.returncode,125,err)
                reason={'change':'exit_address_changed','fail':'exit_probe_failed','stall':'monitor_lease_expired'}[mode]
                self.assertIn(reason,err)
                until=time.monotonic()+3
                while any(running(pid) for pid in pids) and time.monotonic()<until:time.sleep(.1)
                self.assertFalse([pid for pid in pids if running(pid)])
            finally:
                guard.stop(process)
                for pid in pids:
                    if running(pid):
                        try:os.kill(pid,9)
                        except ProcessLookupError:pass
                process.communicate(timeout=5)
                marker=fixture/'copy-id'
                if marker.exists():shutil.rmtree(sg.STORAGE/'projects'/marker.read_text(),ignore_errors=True)
                import hashlib
                shutil.rmtree(sg.STORAGE/'state'/hashlib.sha256(os.fsencode(fixture)).hexdigest()[:24],ignore_errors=True)

    def test_changed_exit_kills_real_container_tree(self):self.exercise('change')
    def test_failed_probe_kills_real_container_tree(self):self.exercise('fail')
    def test_expired_lease_kills_real_container_tree(self):self.exercise('stall')


class RevocationTests(unittest.TestCase):
    def test_revocation_refuses_new_connections(self):
        from test_guard import FakeProxy,FakeHandler
        proxy=FakeProxy(('127.0.0.1',0),FakeHandler);proxy.requests=[]
        threading.Thread(target=proxy.serve_forever,daemon=True).start()
        try:
            with tempfile.TemporaryDirectory(dir=str(sg.STORAGE/'work')) as directory:
                runtime=Path(directory)
                with guard.bridge(runtime,proxy.server_address[1],'dummy:auth') as relay:
                    relay.close_connections(permanent=True)
                    with socket.socket(socket.AF_UNIX) as client:
                        client.settimeout(2);client.connect(str(runtime/'bridge.sock'))
                        try:
                            client.sendall(b'GET http://fake.invalid/ HTTP/1.1\r\nHost: fake.invalid\r\n\r\n')
                            self.assertEqual(client.recv(100),b'')
                        except (ConnectionResetError,BrokenPipeError):pass
                    self.assertEqual(proxy.requests,[])
        finally:proxy.shutdown();proxy.server_close()

if __name__=='__main__':unittest.main(verbosity=2)
