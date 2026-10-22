# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Read inspection data without turning executable reads into usage evidence."""
import errno
import fcntl
import os
import stat
from contextlib import contextmanager


@contextmanager
def open_inspection(path, mode='rb', **kwargs):
    # Opening alone does not read contents. Check the actual opened inode, also
    # covering configuration/desktop/log symlinks and hard links to executables.
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, 'Inspection requires a regular file', str(path))
        if info.st_mode & 0o111:
            if not hasattr(os, 'O_NOATIME'):
                raise OSError(errno.ENOTSUP, 'Cannot inspect executable contents without changing access time', str(path))
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            # EPERM means we lack ownership/CAP_FOWNER. Never fall back to a
            # normal read and never restore timestamps after reading.
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NOATIME)
        stream = os.fdopen(fd, mode, **kwargs)
        fd = None
        with stream:
            yield stream
    finally:
        if fd is not None:
            os.close(fd)


def read_inspection_text(path, **kwargs):
    with open_inspection(path, 'r', **kwargs) as stream:
        return stream.read()
