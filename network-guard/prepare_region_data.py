"""Download signed Debian locale data into /mnt, without host installation."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

STORAGE = Path('/mnt/example/runtime-data')
IMAGE = STORAGE / 'images/python-rootfs'


def main():
    apt = STORAGE / 'build/apt'
    for name in ('lists/partial', 'archives/partial', 'log'):
        (apt / name).mkdir(parents=True, exist_ok=True)
    config = apt / 'isolated.conf'
    config.write_text('Dir::Etc::parts "-"; Dir::Etc::main "-";\n')
    env = dict(os.environ, APT_CONFIG=str(config), TMPDIR=str(STORAGE / 'tmp'))
    options = ['apt-get', '-o', 'Dir::Etc::sourcelist=' + str(apt / 'sources.list'),
               '-o', 'Dir::Etc::sourceparts=-', '-o', 'Dir::Etc::main=-',
               '-o', 'Dir::Etc::parts=-', '-o', 'Dir::State=' + str(apt),
               '-o', 'Dir::State::status=' + str(IMAGE / 'var/lib/dpkg/status'),
               '-o', 'Dir::State::lists=' + str(apt / 'lists'),
               '-o', 'Dir::Cache=' + str(apt), '-o', 'Dir::Cache::archives=' + str(apt / 'archives'),
               '-o', 'Dir::Log=' + str(apt / 'log'), '-o', 'Debug::NoLocking=true',
               '-o', 'APT::Sandbox::User=' + os.environ['USER']]
    subprocess.run(options + ['download', 'locales-all'], cwd=apt / 'archives', env=env, check=True)
    packages = sorted((apt / 'archives').glob('locales-all_*.deb'), key=lambda p: p.stat().st_mtime)
    if not packages:
        raise RuntimeError('未下载语言数据包')
    package = packages[-1]
    identity = subprocess.check_output(['dpkg-deb', '-f', str(package), 'Package', 'Version'], text=True).strip()
    version = hashlib.sha256(package.read_bytes()).hexdigest()[:16]
    base = STORAGE / 'region-data'
    base.mkdir(mode=0o700, exist_ok=True)
    destination = base / version
    if not destination.exists():
        destination.mkdir(mode=0o700)
        subprocess.run(['dpkg-deb', '-x', str(package), str(destination)], env=env, check=True)
        # C.UTF-8 belongs to libc-bin, not necessarily to locales-all.
        c_locale = destination / 'usr/lib/locale/C.utf8'
        if not c_locale.exists():
            shutil.copytree(IMAGE / 'usr/lib/locale/C.utf8', c_locale)
        (destination / 'manifest.json').write_text(json.dumps({'package': identity, 'sha256': hashlib.sha256(package.read_bytes()).hexdigest()}, indent=2) + '\n')
    temporary = base / 'current.new'
    if temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(destination)
    os.replace(temporary, base / 'current')
    print(json.dumps({'locale_data': str(destination), 'package': identity, 'uid': os.getuid()}))


if __name__ == '__main__':
    main()
