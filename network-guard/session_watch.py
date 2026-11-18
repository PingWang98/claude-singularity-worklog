"""Unprivileged host watchdog: kill a container process group on parent death."""
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys


def main():
    child = None

    def stop(*_):
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        # Never call Popen.wait inside a signal handler: it can interrupt a
        # wait holding Python's non-reentrant waitpid lock and deadlock.
        raise SystemExit(130)

    signal.signal(signal.SIGTERM, stop)
    def interrupt(*_):
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGINT)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGINT, interrupt)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) or os.getppid() != int(sys.argv[1]):
        return 125
    # Block death delivery during spawn so the handler always knows the group.
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    try:
        command = ['/usr/bin/python3', '-I', str(Path(__file__).with_name('parent_exec.py')),
                   str(os.getpid())] + sys.argv[2:]
        child = subprocess.Popen(command, close_fds=True, start_new_session=True)
    finally:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
    try:
        return child.wait()
    finally:
        if child.poll() is None:
            stop()


if __name__ == '__main__':
    sys.exit(main())
