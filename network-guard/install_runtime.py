"""Install a versioned user-only trusted runtime outside all project mounts."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import sys

SOURCE = Path(__file__).resolve().parent
STORAGE = Path('/mnt/example/runtime-data')
FILES = ('guard.py','inside.py','parent_exec.py','session_watch.py','singularity_guard.py',
         'singularity_entry.py','project_copy.py','project_direct.py','network_monitor.py','launch_protection.py','proxy_peer.py',
         'relay_credentials.py','regional.py','browser_driver.js')


def main():
    hashes = {name: hashlib.sha256((SOURCE/name).read_bytes()).hexdigest() for name in FILES}
    version = hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()[:16]
    releases = STORAGE / 'runtime/releases'
    releases.mkdir(mode=0o700,parents=True,exist_ok=True)
    release = releases / version
    if not release.exists():
        release.mkdir(mode=0o700)
        for name in FILES:
            shutil.copyfile(SOURCE/name,release/name)
            (release/name).chmod(0o400)
        launcher = '#!/bin/bash\nset -euo pipefail\nexec ' + shlex.quote(sys.executable) + ' -I "' + str(release/'singularity_guard.py') + '" "$@"\n'
        (release/'claude-singularity').write_text(launcher)
        (release/'claude-singularity').chmod(0o500)
        (release/'claude-browser').write_text('#!/bin/bash\nset -euo pipefail\nexec "' + str(release/'claude-singularity') + '" --browser "$@"\n')
        (release/'claude-browser').chmod(0o500)
        (release/'manifest.json').write_text(json.dumps(hashes,indent=2)+'\n')
        (release/'manifest.json').chmod(0o400)
        release.chmod(0o500)
    current = STORAGE / 'runtime/current'
    temporary = STORAGE / 'runtime/current.new'
    if temporary.is_symlink():temporary.unlink()
    temporary.symlink_to(release)
    os.replace(temporary,current)
    # Image-side helpers are independent, read-only inside the container.
    image = STORAGE / 'images/python-rootfs/opt/claude-safe'
    # A regular image-side target lets each session bind its own timezone file,
    # without following /etc/localtime's old UTC symlink during mount setup.
    localtime = STORAGE / 'images/python-rootfs/etc/localtime'
    if localtime.is_symlink():
        temporary = localtime.with_name('localtime.new')
        shutil.copyfile(STORAGE / 'images/python-rootfs/usr/share/zoneinfo/Etc/UTC', temporary)
        temporary.chmod(0o444)
        os.replace(temporary, localtime)
    for source,target in (('inside.py','inside.py'),('singularity_entry.py','entry.py'),('browser_driver.js','browser.js')):
        temporary = image/(target+'.new')
        shutil.copyfile(SOURCE/source,temporary)
        temporary.chmod(0o444)
        os.replace(temporary,image/target)
    link = Path.home() / '.local/bin/claude-singularity'
    if link.exists() and not link.is_symlink():
        raise RuntimeError('Existing non-symlink command preserved')
    temporary = link.with_name('claude-singularity.new')
    if temporary.is_symlink():temporary.unlink()
    temporary.symlink_to(current/'claude-singularity')
    os.replace(temporary,link)
    browser_link = link.with_name('claude-browser')
    if browser_link.exists() and not browser_link.is_symlink():
        raise RuntimeError('Existing non-symlink browser command preserved')
    temporary = browser_link.with_name('claude-browser.new')
    if temporary.is_symlink():temporary.unlink()
    temporary.symlink_to(current/'claude-browser')
    os.replace(temporary,browser_link)
    print(json.dumps({'runtime':str(release),'command':str(link),'uid':os.getuid()}))


if __name__ == '__main__':main()
