"""Download verified Debian tools and unpack into the user-owned image.

Uses an isolated APT state/cache on /mnt, never the host package database.
No maintainer scripts run, no sudo, and no host installation takes place.
"""
import json
import os
from pathlib import Path
import subprocess

STORAGE = Path('/mnt/example/runtime-data')
IMAGE = STORAGE / 'images/python-rootfs'


def main():
    apt = STORAGE / 'build/apt'
    for name in ('lists/partial', 'archives/partial', 'log'):
        (apt / name).mkdir(parents=True, exist_ok=True)
    keyring = IMAGE / 'usr/share/keyrings/debian-archive-keyring.gpg'
    sources = apt / 'sources.list'
    sources.write_text('deb [signed-by=%s] https://deb.debian.org/debian bookworm main\n'
                       'deb [signed-by=%s] https://deb.debian.org/debian-security bookworm-security main\n'
                       % (keyring, keyring))
    base = ['apt-get', '-o', 'Dir::Etc::sourcelist=' + str(sources),
            '-o', 'Dir::Etc::sourceparts=-', '-o', 'Dir::Etc::main=-',
            '-o', 'Dir::Etc::parts=-', '-o', 'Dir::State=' + str(apt),
            '-o', 'Dir::State::status=' + str(IMAGE / 'var/lib/dpkg/status'),
            '-o', 'Dir::State::lists=' + str(apt / 'lists'),
            '-o', 'Dir::Cache=' + str(apt),
            '-o', 'Dir::Cache::archives=' + str(apt / 'archives'),
            '-o', 'Dir::Log=' + str(apt / 'log'), '-o', 'Debug::NoLocking=true',
            '-o', 'APT::Sandbox::User=' + os.environ['USER'],
            '-o', 'APT::Get::List-Cleanup=false']
    env = dict(os.environ, TMPDIR=str(STORAGE / 'tmp'))
    # APT reads config before command options; isolate its initial config too.
    config = apt / 'isolated.conf'
    config.write_text('Dir::Etc::parts "-"; Dir::Etc::main "-";\n')
    env['APT_CONFIG'] = str(config)
    subprocess.run(base + ['update'], env=env, check=True)
    subprocess.run(base + ['--download-only', '--assume-yes', '--no-install-recommends',
                           'install', 'git', 'curl', 'iproute2'], env=env, check=True)
    packages = []
    for path in sorted((apt / 'archives').glob('*.deb')):
        packages.append(subprocess.check_output(['dpkg-deb', '-f', str(path), 'Package', 'Version'], text=True).strip())
        # Preserve merged-/usr symlinks (/bin -> usr/bin, /lib -> usr/lib).
        # Plain dpkg-deb -x would replace these links with legacy directories.
        unpack = subprocess.Popen(['dpkg-deb', '--fsys-tarfile', str(path)], stdout=subprocess.PIPE, env=env)
        try:
            subprocess.run(['tar', '--extract', '--keep-directory-symlink',
                            '--no-same-owner', '--no-same-permissions', '-C', str(IMAGE)],
                           stdin=unpack.stdout, env=env, check=True)
        finally:
            unpack.stdout.close()
        if unpack.wait() != 0:
            raise RuntimeError('Cannot unpack verified package')
    (STORAGE / 'build/tools.json').write_text(json.dumps(packages, indent=2) + '\n')
    print('Extracted %d verified packages into user image' % len(packages))


if __name__ == '__main__':
    main()
