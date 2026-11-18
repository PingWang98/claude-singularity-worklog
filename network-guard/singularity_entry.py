"""Image-side preflight: no application runs until isolation is verified."""
import json
import hashlib
import locale
import os
from pathlib import Path
import re
import socket
import stat
import sys
import time
import datetime
from zoneinfo import ZoneInfo


def unescape(value):
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)


def check():
    policy = json.loads(Path('/run/claude-safe/policy.json').read_text())
    if os.getuid() != policy['uid'] or os.geteuid() == 0:
        raise RuntimeError('unexpected identity')
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    if any(int(status[key].strip(), 16) for key in ('CapEff', 'CapPrm', 'CapBnd', 'CapInh', 'CapAmb')):
        raise RuntimeError('capabilities remain')
    if status['NoNewPrivs'].strip() != '1':
        raise RuntimeError('privilege escalation is not blocked')
    if [name for _, name in socket.if_nameindex()] != ['lo']:
        raise RuntimeError('unexpected network interface')
    for kind in ('net', 'pid'):
        if os.readlink('/proc/self/ns/' + kind) == policy['host_' + kind + 'ns']:
            raise RuntimeError('host namespace inherited')
    if os.readlink('/proc/1/ns/pid') != os.readlink('/proc/self/ns/pid'):
        raise RuntimeError('host procfs visible')
    if list(Path('/sys').iterdir()):
        raise RuntimeError('host sysfs visible')
    expected = policy['binds']
    image = policy['image']
    seen = set()
    # Known runtime pseudo mounts. Any other source/destination fails closed.
    pseudo = {'/': {'tmpfs'}, '/dev': {'tmpfs'}, '/dev/shm': {'tmpfs'},
              '/dev/mqueue': {'mqueue'}, '/dev/pts': {'devpts'}, '/proc': {'proc'},
              '/.singularity.d/actions': {'tmpfs'},
              '/etc/resolv.conf': {'tmpfs'}, '/etc/passwd': {'tmpfs'}, '/etc/group': {'tmpfs'}}
    for name in ('null', 'zero', 'random', 'urandom', 'tty'):
        pseudo['/dev/' + name] = {'devtmpfs'}
    # With a terminal attached, Singularity additionally binds its character
    # device as /dev/console. Never accept an entire device directory here.
    if Path('/dev/console').exists():
        if not stat.S_ISCHR(os.stat('/dev/console').st_mode):
            raise RuntimeError('unexpected console type')
        pseudo['/dev/console'] = {'devpts', 'devtmpfs'}
    rows = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        left, right = line.split(' - ', 1)
        fields = left.split()
        rows.append((unescape(fields[4]), unescape(fields[3]), fields[2],
                     set(fields[5].split(',')), right.split()[0]))
    for dst, root, device, options, fs in rows:
        # Singularity 3.5 creates sysfs first, then our empty read-only bind.
        # These lower mounts are inaccessible; the agent cannot unmount or
        # create a new user/mount namespace after its seccomp policy installs.
        if dst.startswith('/sys/') or (dst == '/sys' and fs == 'sysfs'):
            continue
        if device == image['device'] and root.startswith(image['root'] + '/'):
            relative = root[len(image['root']):]
            # Known read-only lower image mounts remain visible in mountinfo.
            # A timezone source mounted elsewhere must match its explicit bind.
            if relative == dst and 'ro' in options:
                continue
            if dst not in expected:
                raise RuntimeError('unexpected image mount')
        if dst in expected:
            want = expected[dst]
            if (device != want['device'] or root != want['root'] or want['mode'] not in options):
                raise RuntimeError('bind source or mode mismatch')
            seen.add(dst)
        elif fs not in pseudo.get(dst, set()):
            raise RuntimeError('unexpected mount at ' + dst)
        if dst == '/' and 'ro' not in options:
            raise RuntimeError('writable image root')
    if seen != set(expected):
        raise RuntimeError('required bind missing')
    if os.getcwd() != '/workspace':
        raise RuntimeError('unexpected working directory')
    region = policy.get('region')
    if region is not None:
        if json.loads(Path('/run/claude-safe/region.json').read_text()) != region:
            raise RuntimeError('region policy mismatch')
        if Path('/etc/timezone').read_text().strip() != region['timezone']:
            raise RuntimeError('system timezone mismatch')
        system_locale = dict(line.split('=', 1) for line in Path('/etc/default/locale').read_text().splitlines())
        if system_locale != {'LANG': region['locale'], 'LC_ALL': region['locale'], 'LANGUAGE': region['language']}:
            raise RuntimeError('system locale mismatch')
        if hashlib.sha256(Path('/etc/localtime').read_bytes()).hexdigest() != policy['zone_sha256']:
            raise RuntimeError('system timezone data mismatch')
        os.environ['TZ'] = region['timezone']
        time.tzset()
        locale.setlocale(locale.LC_ALL, region['locale'])
        now = time.time()
        with open('/etc/localtime', 'rb') as stream:
            system_zone = ZoneInfo.from_file(stream)
        offsets = [datetime.datetime.fromtimestamp(now, zone).utcoffset()
                   for zone in (system_zone, ZoneInfo(region['timezone']))]
        offsets.append(datetime.datetime.fromtimestamp(now).astimezone().utcoffset())
        if len(set(offsets)) != 1:
            raise RuntimeError('process timezone mismatch')
    return region


def main():
    region = check()
    env = {key: value for key, value in os.environ.items()
           if key in ('ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY',
                      'ANTHROPIC_MODEL', 'CLAUDE_CODE_MAX_OUTPUT_TOKENS', 'TERM', 'LANG')}
    env.update(HOME='/home/claude', PATH='/opt/claude:/opt/node/bin:/usr/local/bin:/usr/bin:/bin',
               TMPDIR='/tmp', XDG_CACHE_HOME='/home/claude/.cache',
               XDG_CONFIG_HOME='/home/claude/.config', XDG_DATA_HOME='/home/claude/.local/share',
               CLAUDE_CONFIG_DIR='/home/claude/.claude', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
               DISABLE_AUTOUPDATER='1', LANG='C.UTF-8')
    if region is not None:
        env.update(TZ=region['timezone'], LANG=region['locale'], LC_ALL=region['locale'],
                   LANGUAGE=region['language'])
    policy = json.loads(Path('/run/claude-safe/policy.json').read_text())
    profile = policy.get('application_profile', 'agent')
    if profile not in ('agent', 'browser'):
        raise RuntimeError('unknown application profile')
    if profile == 'agent':
        prefix = policy.get('conda_prefix')
        if prefix != '/mnt/example/conda':
            raise RuntimeError('unexpected default Conda prefix')
        # /tmp is a session-specific directory backed entirely by /mnt on host.
        # Standard pip user installs are version-separated and disappear on exit.
        userbase = '/tmp/claude-user'
        package_cache = '/tmp/claude-conda-pkgs'
        environments = '/tmp/claude-conda-envs'
        for directory in (userbase, package_cache, environments):
            Path(directory).mkdir(mode=0o700, exist_ok=True)
        env.update(PATH='/opt/claude:/opt/node/bin:' + userbase + '/bin:' + prefix + '/bin:/usr/local/bin:/usr/bin:/bin',
                   CONDA_PREFIX=prefix, CONDA_DEFAULT_ENV='base', CONDA_SHLVL='1',
                   CONDA_EXE=prefix + '/bin/conda', CONDA_PYTHON_EXE=prefix + '/bin/python',
                   BASH_ENV=prefix + '/etc/profile.d/conda.sh', SHELL='/bin/bash',
                   PYTHONUSERBASE=userbase, PIP_CACHE_DIR='/tmp/claude-pip-cache',
                   PIP_DISABLE_PIP_VERSION_CHECK='1',
                   CONDA_PKGS_DIRS=package_cache, CONDA_ENVS_PATH=environments + ':' + prefix + '/envs')
    if profile == 'browser':
        env = {key: value for key, value in env.items() if not key.startswith('ANTHROPIC_')}
    os.execve('/usr/local/bin/python3', ['/usr/local/bin/python3', '-I',
              '/opt/claude-safe/inside.py', 'supervise-browser' if profile == 'browser' else 'supervise-singularity'] + sys.argv[1:], env)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('claude-singularity: isolation preflight failed (%s); no fallback' %
              (str(error) if isinstance(error, RuntimeError) else type(error).__name__), file=sys.stderr)
        sys.exit(125)
