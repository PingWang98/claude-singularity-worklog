"""Project-only Singularity backend for the existing fixed-proxy guard."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import guard
from project_copy import ProjectCopy
from project_direct import DirectProject
from launch_protection import Protection
from relay_credentials import DEFAULT_MODEL, relay_values
from regional import public_settings

STORAGE = Path('/mnt/example/runtime-data')
IMAGE = STORAGE / 'images/python-rootfs'
SINGULARITY = Path('/usr/local/bin/singularity')
MIHOMO = Path.home() / '.local/bin/mihomo'
CONDA_ROOT = Path('/mnt/example/conda')


def unescape(value):
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)


def mount_source(path):
    path = str(Path(path).resolve())
    matches = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields = line.split()
        point = unescape(fields[4])
        if path == point or path.startswith(point.rstrip('/') + '/'):
            matches.append((len(point), fields, point))
    _, fields, point = max(matches, key=lambda x: x[0])
    tail = os.path.relpath(path, point)
    root = os.path.normpath(os.path.join(unescape(fields[3]), tail))
    return {'device': fields[2], 'root': root}


def validate_project(value):
    project = Path(value).resolve(strict=True)
    if not project.is_dir() or project == Path.home().resolve():
        raise RuntimeError('必须指定独立的项目目录，不能挂载真实 HOME')
    if project == STORAGE or project in STORAGE.parents or STORAGE in project.parents:
        raise RuntimeError('项目不能包含或位于容器镜像、缓存和私有运行目录')
    protected = [Path(sys.prefix).resolve(), (Path.home() / '.local/bin').resolve(), ROOT.resolve()]
    # Development source can be copied, but installed code and interpreters
    # must never become a project's origin or publication target.
    if ROOT.is_relative_to(STORAGE):
        protected.append(ROOT)
    else:
        protected.remove(ROOT.resolve())
    for path in protected:
        if path == project or project in path.parents or path in project.parents:
            raise RuntimeError('项目与受信运行程序重叠；拒绝启动')
    if not project.is_relative_to(Path('/mnt')):
        raise RuntimeError('项目必须位于 /mnt')
    if any(c in str(project) for c in ':,\n\r'):
        raise RuntimeError('此版本不支持目录名中的冒号、逗号或换行')
    return project


def environment():
    # Do not inherit SINGULARITY_BIND/PATH/ENVIRONMENT or shell injection hooks.
    env = {k: os.environ[k] for k in ('HOME', 'USER', 'LOGNAME', 'TERM', 'LANG')
           if k in os.environ}
    env.update(PATH='/usr/local/bin:/usr/bin:/bin',
               SINGULARITY_CACHEDIR=str(STORAGE / 'cache'),
               SINGULARITY_TMPDIR=str(STORAGE / 'tmp'), TMPDIR=str(STORAGE / 'tmp'))
    for key in ('ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY',
                'ANTHROPIC_MODEL', 'CLAUDE_CODE_MAX_OUTPUT_TOKENS'):
        if os.environ.get(key):
            env['SINGULARITYENV_' + key] = os.environ[key]
    env['SINGULARITYENV_ANTHROPIC_MODEL'] = os.environ.get('ANTHROPIC_MODEL', '').strip() or DEFAULT_MODEL
    return env


def builder(project, copy=None, *, direct=False, extra_projects=(), application_profile='agent'):
    if application_profile not in ('agent', 'browser'):
        raise ValueError('unknown application profile')
    project = validate_project(project)
    projects = list(dict.fromkeys([project] + [validate_project(p) for p in extra_projects]))
    if len(projects) > 1 and not direct:
        raise RuntimeError('副本模式仅支持一个项目；多项目请使用默认直接挂载模式')
    singularity = str(SINGULARITY)
    if not SINGULARITY.is_file() or not (IMAGE / 'opt/claude-safe/entry.py').is_file():
        raise RuntimeError('Singularity 或已准备的隔离镜像缺失；拒绝启动')
    if copy is not None and direct:
        raise ValueError('direct mode cannot reuse a working copy')
    copy = copy or (DirectProject.create(project, STORAGE, additional=projects[1:])
                    if direct else ProjectCopy.create(project, STORAGE))
    project_mounts = [(copy.workspace, '/workspace')]
    used_names = set()
    for additional in projects[1:]:
        name = additional.name
        suffix = 2
        while name in used_names:
            name = additional.name + '-' + str(suffix)
            suffix += 1
        used_names.add(name)
        project_mounts.append((additional, '/projects/' + name))
    state = STORAGE / ('browser-state' if application_profile == 'browser' else 'state') / hashlib.sha256(os.fsencode(project)).hexdigest()[:24]
    state.mkdir(mode=0o700, parents=True, exist_ok=True)

    serial = 0
    def build(runtime, command, cwd):
        nonlocal serial
        serial += 1
        launch = runtime / ('launch-%d' % serial)
        launch.mkdir(mode=0o700)
        work = launch / 'work'
        work.mkdir(mode=0o700)
        empty_sys = launch / 'empty-sys'
        empty_sys.mkdir(mode=0o700)
        policy = launch / 'policy.json'
        binds = [(src, dst, 'rw') for src, dst in project_mounts] + [(state, '/home/claude', 'rw'),
                 (empty_sys, '/sys', 'ro'),
                 (runtime / 'bridge.sock', '/run/claude-safe/bridge.sock', 'ro'),
                 (runtime / 'client.json', '/run/claude-safe/client.json', 'ro'),
                 (policy, '/run/claude-safe/policy.json', 'ro')]
        regional = build.region
        if application_profile == 'agent':
            if not (CONDA_ROOT / 'bin/python').is_file() or not (CONDA_ROOT / 'etc/profile.d/conda.sh').is_file():
                raise RuntimeError('默认 Conda base 缺失；拒绝静默切换 Python 环境')
            # Preserve both existing absolute prefixes used by installed scripts.
            for destination in dict.fromkeys((str(CONDA_ROOT), str(CONDA_ROOT.resolve()))):
                binds.append((CONDA_ROOT.resolve(), destination, 'ro'))
        if application_profile == 'browser':
            browser_root = STORAGE / 'browser/current'
            if not (browser_root / 'usr/lib/chromium/chromium').is_file():
                raise RuntimeError('浏览器尚未准备')
            binds.append((browser_root.resolve(), '/opt/browser', 'ro'))
        if regional is not None:
            timezone = launch / 'timezone'
            timezone.write_text(regional['timezone'] + '\n')
            region_file = launch / 'region.json'
            region_file.write_text(json.dumps(public_settings(regional)))
            locale_file = launch / 'default-locale'
            locale_file.write_text('LANG=%s\nLC_ALL=%s\nLANGUAGE=%s\n' %
                                   (regional['locale'], regional['locale'], regional['language']))
            binds += [(Path(regional['zonefile']), '/etc/localtime', 'ro'),
                      (timezone, '/etc/timezone', 'ro'),
                      (locale_file, '/etc/default/locale', 'ro'),
                      (Path(regional['locale_root']), '/usr/lib/locale', 'ro'),
                      (region_file, '/run/claude-safe/region.json', 'ro')]
        # Resolve expected sources before entering the namespace, including
        # /mnt's filesystem-relative mount roots (not guessed string prefixes).
        policy.touch(mode=0o600)
        expected = {dst: dict(mount_source(src), mode=mode) for src, dst, mode in binds}
        for name, dst in (('tmp', '/tmp'), ('var_tmp', '/var/tmp')):
            path = work / name
            path.mkdir(mode=0o700)
            expected[dst] = dict(mount_source(path), mode='rw')
        policy_data = {'image': mount_source(IMAGE), 'binds': expected,
                                     'application_profile': application_profile,
                                     'uid': os.getuid(),
                                     'host_netns': os.readlink('/proc/self/ns/net'),
                                     'host_pidns': os.readlink('/proc/self/ns/pid')}
        if application_profile == 'agent':
            policy_data['conda_prefix'] = str(CONDA_ROOT)
        if regional is not None:
            policy_data['region'] = public_settings(regional)
            policy_data['zone_sha256'] = regional['zone_sha256']
        policy.write_text(json.dumps(policy_data))
        args = [singularity, 'exec', '--userns', '--net', '--network', 'none',
                '--containall', '--cleanenv', '--no-home', '--drop-caps', 'ALL',
                '--workdir', str(work), '--pwd', '/workspace']
        for src, dst, mode in binds:
            args += ['--bind', '%s:%s:%s' % (src, dst, mode)]
        args += [str(IMAGE), '/usr/local/bin/python3', '-I',
                 '/opt/claude-safe/entry.py'] + command
        return ['/usr/bin/python3', '-I', str(ROOT / 'session_watch.py'),
                str(os.getpid())] + args
    build.copy = copy
    build.project_mounts = project_mounts
    build.region = None
    build.application_profile = application_profile
    def set_region(settings):
        build.region = settings
        (copy.folder / 'region.json').write_text(json.dumps(public_settings(settings), ensure_ascii=False, indent=2) + '\n')
    build.set_region = set_region
    return build


def main():
    parser = argparse.ArgumentParser(description='只挂载指定项目的无管理员 Singularity Claude Code')
    parser.add_argument('--project', action='append',
                        help='项目目录，可重复指定；默认当前目录，首个是主项目')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--browser', metavar='URL', help='在独立受保护的 Chromium 会话打开网页并保存评测/截图')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--direct', dest='mount_mode', action='store_const', const='direct',
                       help='直接读写原项目（默认），不复制；修改和删除立即生效')
    modes.add_argument('--copy', dest='mount_mode', action='store_const', const='copy',
                       help='显式使用工作副本，退出后通过 --apply 回写')
    parser.add_argument('--expected-exit-ip')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--changes', metavar='SESSION')
    actions.add_argument('--apply', metavar='SESSION')
    known, arguments = parser.parse_known_args()
    if arguments[:1] == ['--']:
        arguments.pop(0)
    if known.changes or known.apply:
        if known.browser:
            raise RuntimeError('--browser 不与副本同步操作同用')
        if known.mount_mode is not None:
            raise RuntimeError('--direct / --copy 不与 --changes 或 --apply 同用')
        identity = known.changes or known.apply
        if not re.fullmatch(r'[0-9]{8}T[0-9]{6}-[0-9a-f]{10}', identity):
            raise RuntimeError('无效工作副本编号')
        copy = ProjectCopy(STORAGE / 'projects' / identity)
        validate_project(copy.origin)
        if known.changes:
            print(json.dumps({'origin':str(copy.origin), 'workspace':str(copy.workspace),
                              'changes':copy.changes()[1]}, ensure_ascii=False, indent=2))
        else:
            print('已同步 %d 项变化' % copy.publish())
        return 0
    if known.expected_exit_ip:
        import ipaddress
        ip = ipaddress.ip_address(known.expected_exit_ip)
        if ip.version != 4 or not ip.is_global:
            raise RuntimeError('预期出口必须是公网 IPv4')
    projects = known.project or [os.getcwd()]
    direct = known.mount_mode != 'copy'
    launch_env = environment()
    if known.browser:
        from urllib.parse import urlsplit
        url = urlsplit(known.browser)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or any(ord(c) < 32 for c in known.browser):
            raise RuntimeError('浏览器只接受不含凭据的 HTTP/HTTPS 地址')
        if arguments:
            raise RuntimeError('浏览器模式不接受额外命令或 Chromium 参数')
        launch_env = {key: value for key, value in launch_env.items()
                      if not key.startswith('SINGULARITYENV_ANTHROPIC') and key != 'SINGULARITYENV_CLAUDE_CODE_MAX_OUTPUT_TOKENS'}
    elif not known.check:
        launch_env.update({'SINGULARITYENV_' + key: value for key, value in relay_values().items()})
    build = builder(projects[0], direct=direct, extra_projects=projects[1:],
                    application_profile='browser' if known.browser else 'agent')
    mihomo = str(MIHOMO)
    if not Path(mihomo).is_file():
        raise RuntimeError('找不到现有 Mihomo')
    name, node = guard.current_node()
    print('claude-singularity: %s %s → /workspace；固定代理 %s；禁止直连' %
          ('直接挂载' if direct else '项目副本', build.copy.origin, name), file=sys.stderr)
    for src, dst in build.project_mounts[1:]:
        print('claude-singularity: 共享项目 %s → %s' % (src, dst), file=sys.stderr)
    if direct:
        print('claude-singularity: 会话 %s；原项目实时读写，无需同步' % build.copy.id, file=sys.stderr)
    else:
        print('claude-singularity: 工作副本 %s；原项目不挂载，退出后用 --changes / --apply 查看和同步' % build.copy.id, file=sys.stderr)
    command = ['/bin/echo', 'Automatic network checks passed'] if known.check else (
        ['/opt/claude/claude'] +
        [item for _, dst in build.project_mounts[1:] for item in ('--add-dir', dst)] + arguments)
    if known.browser and not known.check:
        command = ['/opt/node/bin/node', '/opt/claude-safe/browser.js', known.browser]
    with build.copy.lock():
        return guard.run_session(node, command, mihomo, builder=build,
                                 runtime_parent=str(STORAGE / 'work'),
                                 environment=launch_env, launch_cwd=str(STORAGE / 'work/launch'),
                                 protection=Protection(expected=known.expected_exit_ip,
                                                       audit=build.copy.folder / 'network-audit.jsonl'))


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as error:
        detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print('claude-singularity: ' + detail, file=sys.stderr)
        sys.exit(125)
