#!/usr/bin/env python3
"""Launch only Claude Code through a fixed proxy in an unprivileged netns."""
import argparse
import contextlib
import ctypes
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from inside import Relay
from proxy_peer import ProxyPeer


def private_json(path, data):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(data, file, ensure_ascii=False)


@contextlib.contextmanager
def proxy_config_fd(config):
    """Keep upstream credentials in memory even during an early SIGKILL."""
    if hasattr(os, 'memfd_create'):
        fd = os.memfd_create('claude-safe-proxy-config', os.MFD_CLOEXEC)
    else:
        # This host's Conda Python omits the wrapper despite kernel/libc
        # support. Use libc rather than falling back to a credential file.
        libc = ctypes.CDLL(None, use_errno=True)
        create = libc.memfd_create
        create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
        create.restype = ctypes.c_int
        fd = create(b'claude-safe-proxy-config', 1)  # MFD_CLOEXEC
        if fd < 0:
            raise OSError(ctypes.get_errno(), 'cannot create in-memory proxy config')
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(os.dup(fd), 'w') as file:
            json.dump(config, file, ensure_ascii=False)
        os.lseek(fd, 0, os.SEEK_SET)
        yield fd
    finally:
        os.close(fd)


def current_node():
    """Resolve the currently selected default node; never change shared config."""
    import yaml
    filename = os.environ.get('CLAUDE_PROXY_CONFIG')
    if not filename:
        raise RuntimeError('请指定宿主代理配置文件')
    config_path = Path(filename).expanduser()
    config = yaml.safe_load(config_path.read_text())
    proxy = next((os.environ[k] for k in
                  ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY")
                  if os.environ.get(k)), None)
    if not proxy:
        raise RuntimeError("未找到当前默认 HTTP/HTTPS 代理；拒绝启动")
    parsed = urllib.parse.urlsplit(proxy)
    if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost")
            or parsed.port != config.get("mixed-port")):
        raise RuntimeError("默认代理与本机 Mihomo 不匹配；拒绝猜测出口")
    controller = config.get("external-controller", "")
    endpoint = urllib.parse.urlsplit("http://" + controller)
    if endpoint.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError("只允许读取本机回环地址上的代理控制接口")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path):
        request = urllib.request.Request("http://" + controller + path)
        if config.get("secret"):
            request.add_header("Authorization", "Bearer " + str(config["secret"]))
        with opener.open(request, timeout=4) as response:
            return json.load(response)

    mode = get("/configs").get("mode")
    proxies = get("/proxies")["proxies"]
    if mode == "global":
        selected = "GLOBAL"
    elif mode == "rule":
        rules = get("/rules")["rules"]
        if not rules or rules[-1].get("type", "").lower() != "match":
            raise RuntimeError("无法确认当前默认 MATCH 出口")
        selected = rules[-1]["proxy"]
    else:
        raise RuntimeError("当前默认代理处于直连/未知模式；拒绝启动")
    visited = set()
    while True:
        if selected in visited or selected.upper() in ("DIRECT", "REJECT", "PASS", "COMPATIBLE"):
            raise RuntimeError("当前出口不是明确的代理节点；拒绝启动")
        visited.add(selected)
        item = proxies.get(selected, {})
        if "now" not in item:
            break
        selected = item["now"]
    matches = [p for p in config.get("proxies", []) if p.get("name") == selected]
    if len(matches) != 1 or matches[0].get("type") != "socks5":
        raise RuntimeError("此版本只支持当前配置内的固定 SOCKS5 节点；拒绝放宽策略")
    node = matches[0]
    allowed = {"name", "type", "server", "port", "username", "password", "udp"}
    if set(node) - allowed:
        raise RuntimeError("节点含尚未验证的链式/传输配置；拒绝启动")
    # Only the upstream proxy's hostname is resolved on the host. Application
    # destination names go through SOCKS5 at the upstream, not local DNS.
    addresses = socket.getaddrinfo(node["server"], int(node["port"]),
                                   socket.AF_INET, socket.SOCK_STREAM)
    if not addresses:
        raise RuntimeError("无法解析代理节点；拒绝启动")
    address = addresses[0][4][0]
    if not ipaddress.ip_address(address).is_global:
        raise RuntimeError("当前节点不是公网代理地址；拒绝启动")
    pinned = {key: node[key] for key in allowed if key in node}
    pinned.update(name="CLAUDE_ONLY_PROXY", server=address, udp=False)
    return selected, pinned


def proxy_config(node, port, auth):
    return {
        "port": port, "mixed-port": 0, "socks-port": 0,
        "allow-lan": False, "bind-address": "127.0.0.1",
        "mode": "rule", "ipv6": False, "log-level": "silent",
        "external-controller": "", "authentication": [auth],
        "dns": {"enable": False}, "tun": {"enable": False},
        "profile": {"store-selected": False, "store-fake-ip": False},
        "proxies": [node], "rules": ["MATCH,CLAUDE_ONLY_PROXY"],
    }


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def connect_tcp(port):
    conn = socket.create_connection(("127.0.0.1", port), timeout=4)
    conn.settimeout(None)
    return conn


@contextlib.contextmanager
def bridge(runtime, port, auth, *, connector=None):
    path = runtime / "bridge.sock"
    private_json(runtime / "client.json", {"auth": auth})
    server = Relay(str(path), connector or (lambda: connect_tcp(port)))
    os.chmod(str(path), 0o600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.close_connections()
        server.server_close()
        thread.join(timeout=2)


def sandbox_command(runtime, command, cwd):
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("bubblewrap 未安装；拒绝不受保护地启动 Claude")
    # Preserve ordinary filesystem access/tools/configuration. This is a
    # network guard, not a project/filesystem security sandbox. Private proc,
    # dev/run/tmp and the child seccomp policy close host IPC escape routes.
    args = [bwrap, "--unshare-user", "--unshare-net", "--unshare-pid",
            "--unshare-ipc", "--unshare-uts", "--disable-userns",
            "--assert-userns-disabled", "--die-with-parent", "--cap-drop", "ALL",
            "--bind", "/", "/", "--proc", "/proc", "--dev", "/dev",
            "--tmpfs", "/run", "--tmpfs", "/tmp",
            "--ro-bind", "/sys", "/sys",
            "--dir", "/run/claude-safe",
            "--ro-bind", str(runtime / "bridge.sock"), "/run/claude-safe/bridge.sock",
            "--ro-bind", str(runtime / "client.json"), "/run/claude-safe/client.json",
            "--ro-bind", str(ROOT), str(ROOT)]
    # /var/run usually points into /run; explicitly handle uncommon layouts.
    if Path("/var/run").resolve() != Path("/run"):
        args += ["--tmpfs", "/var/run"]
    if Path("/var/tmp").exists():
        args += ["--tmpfs", "/var/tmp"]
    if str(Path(cwd).resolve()).startswith("/tmp/"):
        args += ["--bind", cwd, cwd]
    args += ["--chdir", cwd, "--", "/usr/bin/python3", "-I",
             str(ROOT / "inside.py"), "supervise"] + command
    return args


def child_env():
    env = dict(os.environ)
    # Do not run injected code in the trusted supervisor. The user's actual
    # Claude preferences remain in the existing home/config files.
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "LD_PRELOAD",
                "LD_LIBRARY_PATH", "BASH_ENV", "ENV"):
        env.pop(key, None)
    return env


def stop(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=4)


CHECK = r'''
import errno,json,os,socket,subprocess,urllib.request
result={"interfaces":[name for _,name in socket.if_nameindex()]}
for label,family,addr in (("ipv4",socket.AF_INET,("1.1.1.1",443)),
                         ("ipv6",socket.AF_INET6,("2606:4700:4700::1111",443))):
    s=socket.socket(family);s.settimeout(2)
    result[label+"_blocked"]=s.connect_ex(addr)!=0;s.close()
try:
    socket.socket(socket.AF_UNIX)
    result["host_unix_socket_blocked"]=False
except PermissionError:
    result["host_unix_socket_blocked"]=True
p=subprocess.run(["/usr/bin/python3","-c","import socket; s=socket.socket(); s.settimeout(1); raise SystemExit(0 if s.connect_ex(('1.1.1.1',443)) else 1)"],check=False)
result["child_direct_blocked"]=p.returncode==0
req=urllib.request.Request("https://example.com/",headers={"User-Agent":"claude-safe-connectivity-check"})
try:
    with urllib.request.urlopen(req,timeout=25) as response:
        result["proxy_https_status"]=response.status
except Exception as error:
    result["proxy_error"]=type(error).__name__
print(json.dumps(result))
raise SystemExit(0 if result["interfaces"]==["lo"] and all(result[k] for k in ("ipv4_blocked","ipv6_blocked","host_unix_socket_blocked","child_direct_blocked")) and result.get("proxy_https_status")==200 else 1)
'''


def main():
    parser = argparse.ArgumentParser(description="只为本次 Claude Code 及子进程强制代理，无直连回退")
    parser.add_argument("--check", action="store_true", help="测试隔离和代理 HTTPS；不使用 Claude 账号")
    known, arguments = parser.parse_known_args()
    if arguments[:1] == ["--"]:
        arguments.pop(0)
    claude = shutil.which("claude")
    mihomo = shutil.which("mihomo")
    if not mihomo:
        mihomo = str(Path.home() / ".local/bin/mihomo")
    if not claude or not Path(mihomo).is_file():
        raise RuntimeError("未找到现有 Claude Code 或 Mihomo；拒绝启动")
    name, node = current_node()
    print("claude-safe: 仅本次 Claude Code 进程树受保护；固定节点 %s；禁止直连" % name,
          file=sys.stderr)
    command = ["/usr/bin/python3", "-c", CHECK] if known.check else [claude] + arguments
    return run_session(node, command, mihomo)


def run_session(node, command, mihomo, builder=sandbox_command, runtime_parent=None,
                environment=None, launch_cwd=None, protection=None):
    """Share the fixed upstream and fail-closed lifecycle across backends."""
    port = available_port()
    auth = "claude-safe:" + secrets.token_hex(24)
    proxy_process = None
    child = None
    with tempfile.TemporaryDirectory(prefix="claude-safe-%d-" % os.getuid(),
                                     dir=runtime_parent) as directory:
        runtime = Path(directory)
        env = child_env() if environment is None else environment
        # Dedicated instance: it never edits/reloads the user's shared proxy.
        with proxy_config_fd(proxy_config(node, port, auth)) as config_fd:
            proxy_process = subprocess.Popen(
                ["/usr/bin/python3", "-I", str(ROOT / "parent_exec.py"), str(os.getpid()),
                 mihomo, "-d", directory, "-f", '/proc/self/fd/%d' % config_fd],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=env, start_new_session=True,
                pass_fds=(config_fd,))
        old_int = signal.signal(signal.SIGINT, lambda *_: None)
        old_term = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        try:
            peer = ProxyPeer(proxy_process, port)
            peer.wait_ready()
            with bridge(runtime, port, auth, connector=peer.connect) as relay:
                peer.on_failure = lambda: relay.close_connections(permanent=True)
                if protection is not None:
                    protection.prepare(runtime, peer, auth, builder, env, launch_cwd)
                if not peer.listener_ok():
                    peer.reject()
                child = subprocess.Popen(builder(runtime, command, os.getcwd()),
                                         env=env, close_fds=True, cwd=launch_cwd)
                while child.poll() is None:
                    failure = protection.failure() if protection is not None else None
                    if not peer.listener_ok():
                        failure = 'proxy_identity_lost'
                    if failure:
                        # Shut down the upstream before waiting for application
                        # cleanup; all old and newly queued tunnels lose egress.
                        relay.close_connections(permanent=True)
                        stop(proxy_process)
                        stop(child)
                        raise RuntimeError('网络检查/出口监测失败，已断网并停止 Claude: ' + failure)
                    time.sleep(.1)
                return child.returncode
        finally:
            if protection is not None:
                protection.close()
            stop(child)
            stop(proxy_process)
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as error:
        # Some library exceptions contain URLs/secrets, so only our explicit
        # RuntimeError messages are user-visible.
        detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print("claude-safe: " + detail, file=sys.stderr)
        sys.exit(125)
