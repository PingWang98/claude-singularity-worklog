#!/usr/bin/python3
"""Exec a helper that the kernel kills when its launcher dies (including KILL)."""
import ctypes
import os
import signal
import sys


def main():
    signal.pthread_sigmask(signal.SIG_SETMASK, [])
    expected_parent = int(sys.argv[1])
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        return 125
    # The parent may have exited before the prctl call. Never start an orphan.
    if os.getppid() != expected_parent:
        return 125
    os.execvpe(sys.argv[2], sys.argv[2:], os.environ)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        # Do not print command arguments or potentially sensitive exceptions.
        sys.exit(125)
