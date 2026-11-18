"""Local owner checks and session shutdown, with no upstream or model requests."""
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

import guard
from proxy_peer import ProxyPeer


class ProxyPeerTests(unittest.TestCase):
    def test_unverified_stream_sends_no_request_and_latches_failure(self):
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            server.listen()
            peer = ProxyPeer(mock.Mock(pid=os.getpid(), poll=lambda: None),
                             server.getsockname()[1])
            peer.listener = peer._owned_socket(0)
            self.assertTrue(peer.listener_ok())
            received = []
            def receive():
                conn, _ = server.accept()
                with conn:
                    received.append(conn.recv(128))
            observer = threading.Thread(target=receive, daemon=True)
            observer.start()
            with mock.patch.object(peer, '_owned_socket', return_value=None):
                client = peer.https(2)
                client.set_tunnel('example.invalid', 443, {'Proxy-Authorization': 'test-only'})
                with self.assertRaisesRegex(RuntimeError, '身份'):
                    client.request('GET', '/')
                client.close()
            observer.join(3)
            self.assertEqual(received, [b''])
            with self.assertRaisesRegex(RuntimeError, '身份'):
                peer.connect()

    def test_unverified_listener_prevents_application_start(self):
        node = {'name': 'CLAUDE_ONLY_PROXY', 'type': 'socks5', 'server': '192.0.2.1', 'port': 1080}
        build = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ProxyPeer, '_owned_socket', return_value=None):
            with self.assertRaisesRegex(RuntimeError, '身份'):
                guard.run_session(node, [], str(Path.home() / '.local/bin/mihomo'),
                                  builder=build, runtime_parent=tmp)
            build.assert_not_called()
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_owner_check_failure_stops_running_session(self):
        node = {'name': 'CLAUDE_ONLY_PROXY', 'type': 'socks5', 'server': '192.0.2.1', 'port': 1080}
        original = ProxyPeer._owned_socket
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'started'
            def owned(peer, remote_port):
                if remote_port and marker.exists():
                    return None  # inject an unverifiable accepted stream after startup
                return original(peer, remote_port)
            def build(runtime, command, cwd):
                script = ('import os,pathlib,socket,sys,time\n'
                          'pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n'
                          's=socket.socket(socket.AF_UNIX); s.connect(sys.argv[2]); time.sleep(6)')
                return [sys.executable, '-I', '-c', script, str(marker), str(runtime / 'bridge.sock')]
            with mock.patch.object(ProxyPeer, '_owned_socket', owned):
                with self.assertRaisesRegex(RuntimeError, 'proxy_identity_lost'):
                    guard.run_session(node, [], str(Path.home() / '.local/bin/mihomo'),
                                      builder=build, runtime_parent=tmp)
            self.assertFalse(Path('/proc', marker.read_text()).exists())
            self.assertEqual(list(Path(tmp).iterdir()), [marker])
