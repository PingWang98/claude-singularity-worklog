"""Bounded proxy-only exit probes and a fail-closed monitoring lease."""
import base64
import ipaddress
import json
import threading
import time

EXIT_HOST = 'api.ipify.org'
INTERVAL = 30.0
LEASE = 45.0
PROBE_TIMEOUT = 10.0


def probe_exit(peer, auth):
    # Explicit CONNECT to a loopback proxy. No environment proxy discovery,
    # NO_PROXY bypass, target-host local DNS, redirects, or direct fallback.
    connection = peer.https(PROBE_TIMEOUT)
    authorization = base64.b64encode(auth.encode()).decode()
    connection.set_tunnel(EXIT_HOST, 443, headers={'Proxy-Authorization': 'Basic ' + authorization})
    try:
        connection.request('GET', '/', headers={'Host': EXIT_HOST, 'User-Agent': 'claude-singularity-exit-check'})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError('exit_probe_http_error')
        data = response.read(129)
        if len(data) > 128:
            raise RuntimeError('exit_probe_invalid_response')
        address = ipaddress.ip_address(data.decode('ascii').strip())
        if address.version != 4 or not address.is_global:
            raise RuntimeError('exit_probe_invalid_address')
        return str(address)
    finally:
        connection.close()


class ExitMonitor:
    def __init__(self, probe, expected=None, interval=INTERVAL, lease=LEASE, audit=None):
        self.probe = probe
        self.expected = expected
        self.interval = interval
        self.lease = lease
        self.audit = audit
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.last_success = None
        self.reason = None
        self.thread = None

    def record(self, event):
        if self.audit:
            with open(self.audit, 'a') as file:
                file.write(json.dumps({'time': time.time(), 'event': event}) + '\n')

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name='exit-monitor')
        self.thread.start()

    def run(self):
        try:
            while not self.stop_event.is_set():
                address = self.probe()
                with self.lock:
                    if self.expected is None:
                        self.expected = address
                    if address != self.expected:
                        self.reason = 'exit_address_changed'
                        self.ready.set()
                    else:
                        self.last_success = time.monotonic()
                        self.ready.set()
                if self.reason:
                    self.record(self.reason)
                    return
                self.record('exit_verified')
                if self.stop_event.wait(self.interval):
                    return
        except Exception:
            with self.lock:
                self.reason = 'exit_probe_failed'
                self.ready.set()
            self.record(self.reason)

    def failure(self):
        with self.lock:
            if self.reason:
                return self.reason
            if self.last_success is None:
                return 'exit_not_verified'
            if time.monotonic() - self.last_success > self.lease:
                return 'monitor_lease_expired'
            if self.thread is None or not self.thread.is_alive():
                return 'monitor_stopped'
        return None

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=.2)
