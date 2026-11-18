"""Stage Debian Chromium and dependencies in /mnt, without host installation."""
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
    archives = apt / 'browser-archives'
    archives.mkdir(parents=True, exist_ok=True)
    (archives / 'partial').mkdir(exist_ok=True)
    env = dict(os.environ, APT_CONFIG=str(apt / 'isolated.conf'), TMPDIR=str(STORAGE / 'tmp'))
    options = ['apt-get', '-o', 'Dir::Etc::sourcelist=' + str(apt / 'sources.list'),
               '-o', 'Dir::Etc::sourceparts=-', '-o', 'Dir::Etc::main=-', '-o', 'Dir::Etc::parts=-',
               '-o', 'Dir::State=' + str(apt), '-o', 'Dir::State::status=' + str(IMAGE / 'var/lib/dpkg/status'),
               '-o', 'Dir::State::lists=' + str(apt / 'lists'), '-o', 'Dir::Cache=' + str(apt),
               '-o', 'Dir::Cache::archives=' + str(archives), '-o', 'Dir::Log=' + str(apt / 'log'),
               '-o', 'Debug::NoLocking=true', '-o', 'APT::Sandbox::User=' + os.environ['USER']]
    subprocess.run(options + ['--download-only', '--assume-yes', '--no-install-recommends',
                              'install', 'chromium', 'chromium-l10n', 'fonts-noto-cjk', 'fonts-liberation'], env=env, check=True)
    packages = []
    for package in sorted(archives.glob('*.deb')):
        name = subprocess.check_output(['dpkg-deb', '-f', str(package), 'Package'], text=True).strip()
        version = subprocess.check_output(['dpkg-deb', '-f', str(package), 'Version'], text=True).strip()
        packages.append({'name': name, 'version': version, 'file': package.name,
                         'sha256': hashlib.sha256(package.read_bytes()).hexdigest()})
    digest = hashlib.sha256(json.dumps(packages, sort_keys=True).encode()).hexdigest()[:16]
    base = STORAGE / 'browser'
    base.mkdir(mode=0o700, exist_ok=True)
    destination = base / digest
    if not destination.exists():
        destination.mkdir(mode=0o700)
        for item in packages:
            # Use the existing image's compatible glibc/loader, never shadow it.
            if item['name'] in ('libc6', 'libc-bin', 'libc-l10n', 'locales', 'locales-all'):
                continue
            subprocess.run(['dpkg-deb', '-x', str(archives / item['file']), str(destination)], env=env, check=True)
        playwright = Path('/mnt/example/playwright/package')
        if not (playwright / 'index.js').is_file():
            raise RuntimeError('Missing existing Playwright runtime')
        shutil.copytree(playwright, destination / 'playwright', symlinks=True)
        fonts = destination / 'fonts.conf'
        fonts.write_text('<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig>'
                         '<dir>/opt/browser/usr/share/fonts</dir>'
                         '<cachedir>/home/claude/.cache/fontconfig</cachedir>'
                         '<include ignore_missing="yes">/opt/browser/etc/fonts/conf.d</include></fontconfig>\n')
        (destination / 'manifest.json').write_text(json.dumps(packages, indent=2) + '\n')
    link = base / 'current.new'
    if link.is_symlink():
        link.unlink()
    link.symlink_to(destination)
    os.replace(link, base / 'current')
    print(json.dumps({'browser_bundle': str(destination), 'packages': len(packages), 'uid': os.getuid()}))


if __name__ == '__main__':
    main()
