#!/usr/bin/python3
"""Private network supervisor with strict agent and Chromium sandbox profiles."""
import ctypes
import errno
import json
import os
import platform
import selectors
import signal
import socket
import socketserver
import subprocess
import sys
import threading


def pipe_sockets(left, right):
    """Bounded duplex relay, preserving TCP half-close and backpressure."""
    limit = 256 * 1024
    peers = {left: right, right: left}
    pending = {left: bytearray(), right: bytearray()}
    eof = set()
    write_closed = set()
    for conn in peers:
        conn.setblocking(False)
    with selectors.DefaultSelector() as selector:
        while True:
            for conn, peer in peers.items():
                # A read EOF closes only the peer's write side, and only once
                # already-buffered bytes have been delivered to that peer.
                if peer in eof and not pending[conn] and conn not in write_closed:
                    try:
                        conn.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    write_closed.add(conn)
                events = 0
                if conn not in eof and len(pending[peer]) < limit:
                    events |= selectors.EVENT_READ
                if pending[conn]:
                    events |= selectors.EVENT_WRITE
                try:
                    selector.get_key(conn)
                    registered = True
                except KeyError:
                    registered = False
                if events and registered:
                    selector.modify(conn, events)
                elif events:
                    selector.register(conn, events)
                elif registered:
                    selector.unregister(conn)
            if len(eof) == 2 and not any(pending.values()):
                return
            for key, mask in selector.select(timeout=1):
                conn = key.fileobj
                peer = peers[conn]
                # Nonblocking operations are safe even if readiness changed
                # between select and recv/send. Buffers are bounded per peer.
                if mask & selectors.EVENT_READ and conn not in eof:
                    capacity = limit - len(pending[peer])
                    if capacity:
                        try:
                            data = conn.recv(min(65536, capacity))
                        except BlockingIOError:
                            pass
                        else:
                            if data:
                                pending[peer].extend(data)
                            else:
                                eof.add(conn)
                if mask & selectors.EVENT_WRITE and pending[conn]:
                    try:
                        sent = conn.send(pending[conn])
                    except BlockingIOError:
                        pass
                    else:
                        if sent == 0:
                            raise ConnectionError('relay write closed')
                        del pending[conn][:sent]


class Relay(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False

    def __init__(self, address, connector, tcp=False):
        if tcp:
            self.address_family = socket.AF_INET
        self.connector = connector
        self.connections = set()
        self.lock = threading.Lock()
        self.disabled = False
        self.slots = threading.BoundedSemaphore(64)
        super().__init__(address, Handler)

    def close_connections(self, permanent=False):
        with self.lock:
            if permanent:
                self.disabled = True
            for conn in tuple(self.connections):
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                conn.close()

    def handle_error(self, request, client_address):
        # Never log payload, proxy credentials, or request URLs.
        pass


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        if not self.server.slots.acquire(blocking=False):
            return
        upstream = None
        try:
            with self.server.lock:
                if self.server.disabled:
                    return
                self.server.connections.add(self.request)
            upstream = self.server.connector()
            with self.server.lock:
                if self.server.disabled:
                    return
                self.server.connections.add(upstream)
            pipe_sockets(self.request, upstream)
        except (OSError, ValueError):
            # The only response to upstream failure is connection closure.
            pass
        finally:
            with self.server.lock:
                self.server.connections.discard(self.request)
                self.server.connections.discard(upstream)
            if upstream is not None:
                upstream.close()
            self.server.slots.release()


def restrict_agent(container=False, browser=False):
    """x86_64 seccomp: prevent host IPC/namespace bypass of the network boundary.

    AF_UNIX socketpairs remain available for child-process IPC. Creating a new
    socket is limited to IPv4/IPv6 and netlink for namespace-local interface
    queries. The browser profile additionally permits local Unix IPC and nested
    namespaces for Chromium's own sandbox. Outer capabilities are already zero.
    """
    if platform.machine() != "x86_64":
        raise RuntimeError("seccomp policy currently supports x86_64 only")
    instructions = []

    def emit(code, jt=0, jf=0, k=0):
        instructions.append((code, jt, jf, k))

    emit(0x20, k=4)  # seccomp_data.arch
    emit(0x15, jt=1, k=0xC000003E)  # AUDIT_ARCH_X86_64
    emit(0x06, k=0x80000000)  # kill unexpected ABI
    emit(0x20, k=0)  # syscall number
    emit(0x45, jf=1, k=0x40000000)  # reject x32 ABI
    emit(0x06, k=0x80000000)
    for number in (101, 308, 310, 311, 425, 438):
        # ptrace, setns, process_vm_readv/writev, io_uring_setup, pidfd_getfd
        emit(0x15, jf=1, k=number)
        emit(0x06, k=0x00050000 | errno.EPERM)
    if container:
        # Keep the audited mount/net/PID namespaces fixed. clone3 callers can
        # fall back to clone for normal threads, which remain permitted.
        blocked = (428, 429, 430, 431, 432, 433, 442) if browser else (165, 166, 272, 155, 428, 429, 430, 431, 432, 433, 442)
        for number in blocked:
            emit(0x15, jf=1, k=number)
            emit(0x06, k=0x00050000 | errno.EPERM)
        emit(0x15, jf=1, k=435)
        emit(0x06, k=0x00050000 | errno.ENOSYS)
        if not browser:
            emit(0x15, jf=4, k=56)  # clone: reject new namespaces, permit threads
            emit(0x20, k=16)
            emit(0x45, jf=1, k=0x7e020000)
            emit(0x06, k=0x00050000 | errno.EPERM)
            emit(0x20, k=0)
    families = [socket.AF_INET, socket.AF_INET6, socket.AF_NETLINK]
    if browser:
        families.append(socket.AF_UNIX)  # Chromium private-profile IPC and its namespace sandbox.
    emit(0x15, jf=len(families) + 3, k=41)  # socket(domain, ...)
    emit(0x20, k=16)  # args[0]
    for index, family in enumerate(families):
        emit(0x15, jt=len(families) - index, k=family)
    emit(0x06, k=0x00050000 | errno.EPERM)
    emit(0x06, k=0x7FFF0000)
    emit(0x15, jf=4, k=16)  # ioctl: forbid terminal input injection
    emit(0x20, k=24)  # args[1]
    emit(0x15, jt=1, k=0x5412)  # TIOCSTI
    emit(0x15, jf=1, k=0x541C)  # TIOCLINUX
    emit(0x06, k=0x00050000 | errno.EPERM)
    emit(0x06, k=0x7FFF0000)

    class Filter(ctypes.Structure):
        _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                    ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]

    class Program(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ushort), ("filter", ctypes.POINTER(Filter))]

    filters = (Filter * len(instructions))(*(Filter(*i) for i in instructions))
    program = Program(len(instructions), filters)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "cannot set no_new_privs")
    if libc.prctl(22, 2, ctypes.byref(program), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot install seccomp")


def main():
    if sys.argv[1] in ("exec", "exec-singularity", "exec-browser"):
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        restrict_agent(container=sys.argv[1] != 'exec', browser=sys.argv[1] == 'exec-browser')
        os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
    # Keep the relay supervisor inaccessible to ptrace/proc-mem manipulation.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
        raise OSError(ctypes.get_errno(), "cannot protect relay supervisor")

    def connect_bridge():
        conn = socket.socket(socket.AF_UNIX)
        try:
            conn.settimeout(5)
            conn.connect("/run/claude-safe/bridge.sock")
            conn.settimeout(None)
            return conn
        except BaseException:
            conn.close()
            raise

    server = Relay(("127.0.0.1", 0), connect_bridge, tcp=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with open("/run/claude-safe/client.json") as file:
        client = json.load(file)
    auth = client["auth"]
    proxy = "http://%s@127.0.0.1:%d" % (auth, server.server_address[1])
    env = dict(os.environ)
    for key in ("http_proxy", "https_proxy", "all_proxy"):
        env[key] = env[key.upper()] = proxy
    env["NO_PROXY"] = env["no_proxy"] = ""
    for key in ("SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS", "DISPLAY",
                "WAYLAND_DISPLAY", "DOCKER_HOST", "DOCKER_CONTEXT"):
        env.pop(key, None)
    # Only the launched Claude tree receives the restriction. Siblings on the
    # host are unaffected. close_fds prevents inherited host network handles.
    mode = 'exec-browser' if sys.argv[1] == 'supervise-browser' else 'exec-singularity' if sys.argv[1] == 'supervise-singularity' else 'exec'
    command = ["/usr/bin/python3", "-I", os.path.realpath(__file__), mode] + sys.argv[2:]
    child = None
    old_int = signal.signal(signal.SIGINT, lambda *_: None)
    try:
        child = subprocess.Popen(command, env=env, close_fds=True)
        return child.wait()
    finally:
        signal.signal(signal.SIGINT, old_int)
        if child is not None and child.poll() is None:
            child.terminate()
        server.shutdown()
        server.close_connections()
        server.server_close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print("claude-safe: isolated launcher failed (%s); no direct fallback" %
              type(error).__name__, file=sys.stderr)
        sys.exit(125)
