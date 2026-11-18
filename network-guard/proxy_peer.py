"""Verify the child-owned loopback listener and each accepted TCP stream (Linux)."""
import http.client
import os
from pathlib import Path
import socket
import ssl
import threading
import time


class ProxyPeer:
    def __init__(self, process, port):
        self.process = process
        self.port = port
        self.proc = Path('/proc') / str(process.pid)
        self.listener = None
        self.failed = threading.Event()
        self.on_failure = None

    def _owned_socket(self, remote_port):
        # Match the SERVER side of the TCP tuple. The connecting side belongs
        # to this launcher. Linux x86_64 prints loopback as 0100007F.
        local = '0100007F:%04X' % self.port
        remote = '0100007F:%04X' % remote_port if remote_port else '00000000:0000'
        state = '01' if remote_port else '0A'  # ESTABLISHED or LISTEN
        with (self.proc / 'net/tcp').open() as table:
            for line in table:
                fields = line.split()
                if fields[1:4] == [local, remote, state] and fields[9] != '0':
                    target = 'socket:[%s]' % fields[9]
                    break
            else:
                return None
        for fd in (self.proc / 'fd').iterdir():
            try:
                if os.readlink(fd) == target:
                    return fd, target
            except FileNotFoundError:
                continue  # unrelated fd closed during enumeration
        return None

    def wait_ready(self):
        deadline = time.monotonic() + 5
        while self.process.poll() is None and time.monotonic() < deadline:
            self.listener = self._owned_socket(0)
            if self.listener:
                with self.connect():
                    return
            time.sleep(.05)
        self.reject()

    def listener_ok(self):
        if self.failed.is_set() or not self.listener or self.process.poll() is not None:
            return False
        fd, target = self.listener
        try:
            return os.readlink(fd) == target
        except OSError:
            return False

    def invalidate(self):
        self.failed.set()
        if self.on_failure is not None:
            self.on_failure()

    def reject(self):
        self.invalidate()
        raise RuntimeError('专用代理连接身份未通过核验')

    def connect(self, timeout=None):
        if not self.listener_ok():
            self.reject()
        conn = None
        try:
            conn = socket.create_connection(('127.0.0.1', self.port), timeout=4)
            client_port = conn.getsockname()[1]
            # accept() can complete just after connect(). No application bytes
            # are sent while waiting for the accepted fd to appear.
            deadline = time.monotonic() + 1
            while self.listener_ok():
                if self._owned_socket(client_port) and self.listener_ok():
                    conn.settimeout(timeout)
                    return conn
                if time.monotonic() >= deadline:
                    break
                time.sleep(.01)
            raise RuntimeError('专用代理连接身份未通过核验')
        except BaseException:
            self.invalidate()
            if conn is not None:
                conn.close()
            raise

    def https(self, timeout):
        connection = http.client.HTTPSConnection('127.0.0.1', self.port, timeout=timeout,
                                                 context=ssl.create_default_context())
        # http.client obtains the socket here before writing CONNECT headers.
        connection._create_connection = lambda *_: self.connect(timeout)
        return connection
