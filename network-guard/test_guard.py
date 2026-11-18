"""Integration tests use only loopback fake proxies and no user credentials."""
import errno
import json
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import unittest

import guard


class FakeProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class FakeHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data = b""
        self.request.settimeout(5)
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            data += chunk
        self.server.requests.append(data.split(b"\r\n", 1)[0])
        if data.startswith(b"CONNECT "):
            self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            while True:
                data = self.request.recv(4096)
                if not data:
                    return
                self.request.sendall(data)
        else:
            self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\nConnection: close\r\n\r\nproxy-only")


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.proxy = FakeProxy(("127.0.0.1", 0), FakeHandler)
        self.proxy.requests = []
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        self.temp = tempfile.TemporaryDirectory(prefix="claude-safe-test-")
        self.runtime = Path(self.temp.name)

    def tearDown(self):
        self.proxy.shutdown()
        self.proxy.server_close()
        self.temp.cleanup()

    def run_isolated(self, script, timeout=20):
        args, env, cwd = self.isolated_launch(script)
        return subprocess.run(args, env=env, cwd=cwd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, timeout=timeout)

    def isolated_launch(self, script):
        return (guard.sandbox_command(self.runtime, ["/usr/bin/python3", "-c", script], str(guard.ROOT.parent)),
                guard.child_env(), None)

    def test_proxy_only_and_child_cannot_bypass(self):
        script = r'''
import errno,json,os,socket,subprocess,urllib.request
result={"interfaces":socket.if_nameindex()}
with urllib.request.urlopen("http://fake.invalid/",timeout=5) as response:
    result["via_proxy"]=response.read().decode()
for key,family,address in (("v4",socket.AF_INET,("1.1.1.1",443)),("v6",socket.AF_INET6,("2606:4700:4700::1111",443))):
    s=socket.socket(family);s.settimeout(1);result[key]=s.connect_ex(address);s.close()
try:
    socket.socket(socket.AF_UNIX)
    result["unix"]=0
except OSError as error:
    result["unix"]=error.errno
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
try:
    s.sendto(b"test",("1.1.1.1",53));result["dns"]=0
except OSError as error:
    result["dns"]=error.errno
s.close()
env=dict(os.environ)
for key in list(env):
    if key.lower().endswith("proxy"): env.pop(key)
p=subprocess.run(["/usr/bin/python3","-c","import socket; s=socket.socket(); s.settimeout(1); print(s.connect_ex(('1.1.1.1',443)))"],env=env,text=True,stdout=subprocess.PIPE,check=True)
result["child"]=int(p.stdout)
result["docker_socket_visible"]=os.path.exists("/var/run/docker.sock")
result["host_runtime_visible"]=os.path.exists("/run/user/1006/bus")
print(json.dumps(result))
'''
        with guard.bridge(self.runtime, self.proxy.server_address[1], "test:secret"):
            result = self.run_isolated(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["via_proxy"], "proxy-only")
        self.assertEqual([p[1] for p in data["interfaces"]], ["lo"])
        for key in ("v4", "v6", "dns", "child"):
            self.assertNotEqual(data[key], 0, data)
        self.assertEqual(data["unix"], errno.EPERM)
        self.assertFalse(data["docker_socket_visible"])
        self.assertFalse(data["host_runtime_visible"])
        self.assertEqual(self.proxy.requests, [b"GET http://fake.invalid/ HTTP/1.1"])

    def test_no_upstream_stays_disconnected(self):
        script = r'''
import socket,urllib.request,json
try:
    urllib.request.urlopen("http://fake.invalid/",timeout=3)
    failed=False
except Exception:
    failed=True
s=socket.socket();s.settimeout(1)
print(json.dumps({"proxy_failed":failed,"direct_failed":s.connect_ex(("1.1.1.1",443))!=0}))
'''
        # Reserve a port without listening so it cannot be taken by another server.
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            with guard.bridge(self.runtime, unused.getsockname()[1], "test:secret"):
                result = self.run_isolated(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"proxy_failed": True, "direct_failed": True})

    def test_bridge_closure_terminates_established_tunnel(self):
        script = r'''
import os,socket,urllib.parse
u=urllib.parse.urlsplit(os.environ["HTTPS_PROXY"])
s=socket.create_connection((u.hostname,u.port),timeout=5)
s.sendall(b"CONNECT fake.invalid:443 HTTP/1.1\r\nHost: fake.invalid\r\n\r\n")
data=b""
while b"\r\n\r\n" not in data: data+=s.recv(4096)
s.sendall(b"ping");assert s.recv(4)==b"ping"
print("TUNNEL_READY",flush=True)
try:
    closed=s.recv(1)==b""
except (ConnectionResetError,BrokenPipeError):
    closed=True
print("CLOSED" if closed else "UNEXPECTED",flush=True)
'''
        with guard.bridge(self.runtime, self.proxy.server_address[1], "test:secret") as relay:
            args, env, cwd = self.isolated_launch(script)
            child = subprocess.Popen(args, env=env, cwd=cwd, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
            try:
                # Bound the readiness wait without blocking indefinitely on readline.
                import selectors
                with selectors.DefaultSelector() as sel:
                    sel.register(child.stdout, selectors.EVENT_READ)
                    self.assertTrue(sel.select(10), "tunnel did not start")
                self.assertEqual(child.stdout.readline().strip(), "TUNNEL_READY")
                relay.close_connections()
                out, err = child.communicate(timeout=10)
                self.assertEqual(child.returncode, 0, err)
                self.assertEqual(out.strip(), "CLOSED")
            finally:
                guard.stop(child)

    def test_generated_policy_has_only_pinned_proxy(self):
        config = guard.proxy_config({"name": "CLAUDE_ONLY_PROXY", "type": "socks5",
                                     "server": "192.0.2.1", "port": 1080}, 12345, "a:b")
        self.assertEqual(config["rules"], ["MATCH,CLAUDE_ONLY_PROXY"])
        self.assertNotIn("proxy-groups", config)
        self.assertEqual(config["external-controller"], "")
        self.assertFalse(config["tun"]["enable"])

    def test_webrtc_stun_turn_and_interface_boundary(self):
        # A real STUN Binding Request header, but only documentation addresses:
        # verify the syscall fails before transmission, without public probes.
        script = r'''
import errno,json,os,socket,struct,subprocess
for key in list(os.environ):
    if key.lower().endswith("proxy"): os.environ.pop(key)
request=struct.pack("!HHI12s",1,0,0x2112A442,os.urandom(12))
results=[]
for family,host,label in ((socket.AF_INET,"198.51.100.1","ipv4"),
                          (socket.AF_INET6,"2001:db8::1","ipv6")):
    for port in (3478,19302,5349,443):
        with socket.socket(family,socket.SOCK_DGRAM) as s:
            s.settimeout(1)
            try:
                sent=s.sendto(request,(host,port));error=0
            except OSError as e:
                sent=0;error=e.errno
            results.append({"family":label,"protocol":"udp","port":port,"sent":sent,"errno":error})
    for port in (3478,5349,443):
        with socket.socket(family,socket.SOCK_STREAM) as s:
            s.settimeout(1)
            results.append({"family":label,"protocol":"tcp","port":port,"errno":s.connect_ex((host,port))})
interfaces=json.loads(subprocess.check_output(["/usr/sbin/ip","-j","address","show"],text=True))
print(json.dumps({"probes":results,"interfaces":interfaces}))
'''
        with guard.bridge(self.runtime, self.proxy.server_address[1], "test:secret"):
            result = self.run_isolated(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(len(data["probes"]), 14)
        for probe in data["probes"]:
            self.assertEqual(probe["errno"], errno.ENETUNREACH, probe)
            if probe["protocol"] == "udp":
                self.assertEqual(probe["sent"], 0, probe)
        self.assertEqual([i["ifname"] for i in data["interfaces"]], ["lo"])
        import ipaddress
        for interface in data["interfaces"]:
            for address in interface.get("addr_info", []):
                self.assertTrue(ipaddress.ip_address(address["local"]).is_loopback)
        self.assertEqual(self.proxy.requests, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
