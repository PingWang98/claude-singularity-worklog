"""Direct project mounts: metadata preflight, no project copy or content hash.

This is a startup check, not a mediated filesystem. Host-side link changes
during a session are outside this check's protection.
"""
import json
from pathlib import Path
import secrets
import shutil
import time

from project_copy import ProjectCopy, scan


class DirectProject(ProjectCopy):
    def __init__(self, folder):
        self.folder = Path(folder)
        self.metadata = json.loads((self.folder / 'origin.json').read_text())
        self.origin = Path(self.metadata['origin'])
        self.projects = [Path(p) for p in self.metadata.get('projects', [str(self.origin)])]
        self.workspace = self.origin
        self.id = self.folder.name

    @classmethod
    def create(cls, origin, storage, *, additional=()):
        origin = Path(origin).resolve(strict=True)
        projects = list(dict.fromkeys([origin] + [Path(p).resolve(strict=True) for p in additional]))
        # Inspect names, types and link counts, never read or hash file bytes.
        for project in projects:
            scan(project, metadata_only=True)
        base = Path(storage) / 'projects'
        base.mkdir(mode=0o700, exist_ok=True)
        folder = base / (time.strftime('%Y%m%dT%H%M%S') + '-' + secrets.token_hex(5))
        folder.mkdir(mode=0o700)
        try:
            info = origin.stat()
            metadata = {'mode': 'direct', 'origin': str(origin),
                        'projects': [str(p) for p in projects],
                        'device': info.st_dev, 'inode': info.st_ino,
                        'created': time.time()}
            (folder / 'origin.json').write_text(json.dumps(metadata, ensure_ascii=False))
            return cls(folder)
        except BaseException:
            shutil.rmtree(folder)
            raise

    def changes(self):
        raise RuntimeError('直接挂载会话已实时修改原项目；不使用 --changes 或 --apply')

    def publish(self):
        raise RuntimeError('直接挂载会话已实时修改原项目；不使用 --changes 或 --apply')
