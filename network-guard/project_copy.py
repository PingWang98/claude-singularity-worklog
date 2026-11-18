"""Private working copies with descriptor-based, conflict-checked publishing.

The agent never receives an origin mount. Only regular files with one link and
contained relative symlinks are admitted. Publishing replaces inodes, never
writes through an existing file/link; host-side changes are conflicts.
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import time
import re

DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC


def digest_fd(fd):
    h = hashlib.sha256()
    while True:
        data = os.read(fd, 1024 * 1024)
        if not data:
            break
        h.update(data)
    return h.hexdigest()


def symlink_target(relative, target):
    if os.path.isabs(target):
        raise RuntimeError('拒绝项目中的绝对路径符号链接: ' + relative)
    resolved = os.path.normpath(os.path.join(os.path.dirname(relative), target))
    if resolved == '..' or resolved.startswith('../'):
        raise RuntimeError('拒绝指向项目之外的符号链接: ' + relative)
    return target


@contextlib.contextmanager
def parent_fd(root_fd, relative, create=False):
    parts = relative.split('/')
    if not parts or any(p in ('', '.', '..') for p in parts):
        raise RuntimeError('非法同步路径')
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            following = os.open(part, DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = following
        yield fd, parts[-1]
    finally:
        os.close(fd)


def scan(root, destination=None, *, metadata_only=False):
    """No-follow traversal; metadata checks can skip all file-content reads."""
    if metadata_only and destination is not None:
        raise ValueError('metadata-only scans cannot copy files')
    records = {}
    canonical = Path(root).resolve(strict=True)
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        point = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), line.split()[4])
        if Path(point) != canonical and Path(point).is_relative_to(canonical):
            raise RuntimeError('拒绝项目中的嵌套挂载')
    root_fd = os.open(root, DIRECTORY)
    target_fd = os.open(destination, DIRECTORY) if destination is not None else None
    device = os.fstat(root_fd).st_dev

    def walk(fd, output, prefix):
        for name in sorted(os.listdir(fd)):
            relative = prefix + name
            before = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if before.st_dev != device:
                raise RuntimeError('拒绝项目中的嵌套文件系统: ' + relative)
            mode = stat.S_IMODE(before.st_mode) & 0o777
            if stat.S_ISDIR(before.st_mode):
                child = os.open(name, DIRECTORY, dir_fd=fd)
                out = None
                try:
                    if output is not None:
                        os.mkdir(name, 0o700, dir_fd=output)
                        out = os.open(name, DIRECTORY, dir_fd=output)
                    records[relative] = {'kind': 'directory'}
                    walk(child, out, relative + '/')
                finally:
                    os.close(child)
                    if out is not None:
                        os.close(out)
            elif stat.S_ISREG(before.st_mode):
                if before.st_nlink != 1:
                    raise RuntimeError('拒绝硬链接或异常文件: ' + relative)
                if metadata_only:
                    records[relative] = {'kind': 'file'}
                    continue
                source = os.open(name, READ, dir_fd=fd)
                try:
                    first = os.fstat(source)
                    if first.st_nlink != 1 or not stat.S_ISREG(first.st_mode):
                        raise RuntimeError('拒绝硬链接或异常文件: ' + relative)
                    h = hashlib.sha256()
                    sink = None
                    try:
                        if output is not None:
                            sink = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode | 0o600, dir_fd=output)
                        while True:
                            data = os.read(source, 1024 * 1024)
                            if not data:
                                break
                            h.update(data)
                            if sink is not None:
                                view = memoryview(data)
                                while view:
                                    view = view[os.write(sink, view):]
                        last = os.fstat(source)
                        if (first.st_size, first.st_mtime_ns, first.st_ctime_ns, first.st_nlink) != (last.st_size, last.st_mtime_ns, last.st_ctime_ns, last.st_nlink):
                            raise RuntimeError('文件在读取期间发生变化: ' + relative)
                    finally:
                        if sink is not None:
                            os.close(sink)
                    records[relative] = {'kind': 'file', 'sha256': h.hexdigest(), 'mode': mode | 0o600}
                finally:
                    os.close(source)
            elif stat.S_ISLNK(before.st_mode):
                target = symlink_target(relative, os.readlink(name, dir_fd=fd))
                resolved = (Path(root) / relative).resolve()
                if not resolved.is_relative_to(Path(root).resolve()):
                    raise RuntimeError('拒绝符号链接链越界: ' + relative)
                records[relative] = {'kind': 'symlink', 'target': target}
                if output is not None:
                    os.symlink(target, name, dir_fd=output)
            else:
                raise RuntimeError('拒绝设备、socket 或 FIFO: ' + relative)
    try:
        walk(root_fd, target_fd, '')
        return records
    finally:
        os.close(root_fd)
        if target_fd is not None:
            os.close(target_fd)


class ProjectCopy:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.workspace = self.folder / 'workspace'
        self.metadata = json.loads((self.folder / 'origin.json').read_text())
        if self.metadata.get('mode') == 'direct':
            raise RuntimeError('直接挂载会话已实时修改原项目；不使用 --changes 或 --apply')
        self.origin = Path(self.metadata['origin'])
        self.id = self.folder.name

    @classmethod
    def create(cls, origin, storage):
        origin = Path(origin).resolve(strict=True)
        base = Path(storage) / 'projects'
        base.mkdir(mode=0o700, exist_ok=True)
        folder = base / (time.strftime('%Y%m%dT%H%M%S') + '-' + secrets.token_hex(5))
        folder.mkdir(mode=0o700)
        workspace = folder / 'workspace'
        workspace.mkdir(mode=0o700)
        try:
            baseline = scan(origin, workspace)
            info = origin.stat()
            metadata = {'origin': str(origin), 'device': info.st_dev, 'inode': info.st_ino,
                        'baseline': baseline, 'created': time.time()}
            (folder / 'origin.json').write_text(json.dumps(metadata, ensure_ascii=False))
            return cls(folder)
        except BaseException:
            shutil.rmtree(folder)
            raise

    def changes(self):
        current = scan(self.workspace)
        baseline = self.metadata['baseline']
        return current, {p: ('create' if p not in baseline else 'delete' if p not in current else 'modify')
                         for p in sorted(set(current) | set(baseline))
                         if current.get(p) != baseline.get(p)}

    @contextlib.contextmanager
    def lock(self):
        fd = os.open(self.folder / 'session.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('会话仍在运行或正在同步；请先停止再同步') from None
            yield
        finally:
            os.close(fd)

    def publish(self):
        """Explicit host command only. No automatic publishing on agent exit."""
        with self.lock():
            return self._publish()

    def _publish(self):
        current, changes = self.changes()
        if not changes:
            return 0
        # First take a second, private, no-hardlink copy. The agent cannot race
        # source reads during publication or replace files after validation.
        stage = self.folder / ('publish-' + secrets.token_hex(6))
        stage.mkdir(mode=0o700)
        root_fd = None
        try:
            staged = scan(self.workspace, stage)
            if staged != current:
                raise RuntimeError('工作副本仍在变化；请先停止会话')
            root_fd = os.open(self.origin, DIRECTORY)
            identity = os.fstat(root_fd)
            if (identity.st_dev, identity.st_ino) != (self.metadata['device'], self.metadata['inode']):
                raise RuntimeError('原项目目录已被替换；拒绝同步')
            host = scan(self.origin)
            baseline = self.metadata['baseline']
            conflicts = [p for p in changes if host.get(p) != baseline.get(p)]
            for path in changes:
                if path not in current and baseline.get(path, {}).get('kind') == 'directory':
                    if any(p.startswith(path + '/') and p not in baseline for p in host):
                        conflicts.append(path)
            # Refuse structural replacement; a directory must remain a directory.
            conflicts += [p for p in changes if ((baseline.get(p, {}).get('kind') == 'directory') !=
                          (current.get(p, {}).get('kind') == 'directory')) and p in baseline and p in current]
            if conflicts:
                raise RuntimeError('原项目发生冲突，未同步: ' + ', '.join(sorted(set(conflicts))[:8]))
            for path in sorted(changes, key=lambda p: (p.count('/'), p)):
                item = current.get(path)
                if item is None:
                    continue
                with parent_fd(root_fd, path, create=True) as (parent, name):
                    if item['kind'] == 'directory':
                        try:
                            os.mkdir(name, 0o700, dir_fd=parent)
                        except FileExistsError:
                            check = os.open(name, DIRECTORY, dir_fd=parent)
                            os.close(check)
                        continue
                    temporary = '.claude-publish-' + secrets.token_hex(12)
                    try:
                        if item['kind'] == 'symlink':
                            os.symlink(item['target'], temporary, dir_fd=parent)
                        else:
                            source = os.open(stage / path, READ)
                            sink = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                                           item['mode'], dir_fd=parent)
                            try:
                                with os.fdopen(source, 'rb') as inp, os.fdopen(sink, 'wb') as out:
                                    shutil.copyfileobj(inp, out)
                                    out.flush()
                                    os.fsync(out.fileno())
                            except BaseException:
                                raise
                        # Atomic replacement never follows a target symlink or
                        # modifies the inode behind an outside hard link.
                        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                    finally:
                        try:
                            os.unlink(temporary, dir_fd=parent)
                        except FileNotFoundError:
                            pass
            for path in sorted((p for p in changes if p not in current), key=lambda p: (-p.count('/'), p)):
                with parent_fd(root_fd, path) as (parent, name):
                    if baseline[path]['kind'] == 'directory':
                        os.rmdir(name, dir_fd=parent)  # Never recursively remove host additions.
                    else:
                        os.unlink(name, dir_fd=parent)
            # Retain the new workspace baseline; unrelated host edits are not
            # imported into this session and remain untouched.
            self.metadata['baseline'] = current
            (self.folder / 'origin.json').write_text(json.dumps(self.metadata, ensure_ascii=False))
            return len(changes)
        finally:
            if root_fd is not None:
                os.close(root_fd)
            shutil.rmtree(stage)
