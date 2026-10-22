# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Read-only change detection for installed state and resolver inputs."""
import hashlib
import os
from pathlib import Path

_records_by_token = {}


def package_state():
    # Track metadata, not cache files APT may rebuild while reading. ctime also
    # detects replacements/edits that preserve a file's modification timestamp.
    paths = {Path(p) for p in ('/var/lib/dpkg/status', '/var/lib/apt/extended_states',
                              '/var/lib/dpkg/arch', '/etc/apt/apt.conf', '/etc/apt/preferences',
                              '/etc/apt/sources.list')}
    for directory in ('/var/lib/dpkg/updates', '/etc/apt/apt.conf.d',
                      '/etc/apt/preferences.d', '/etc/apt/sources.list.d', '/var/lib/apt/lists'):
        root = Path(directory)
        paths.add(root)
        paths.update(p for p in root.glob('*') if p.name not in ('lock', 'partial', 'auxfiles'))
    if os.environ.get('APT_CONFIG'):
        paths.add(Path(os.environ['APT_CONFIG']))
    records = []
    for path in sorted(paths):
        try:
            s = path.stat()
            records.append((str(path), s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns))
        except FileNotFoundError:
            records.append((str(path), None))
    token = hashlib.sha256(repr(records).encode()).hexdigest()
    _records_by_token[token] = {row[0]: row[1:] for row in records}
    while len(_records_by_token) > 8:
        del _records_by_token[next(iter(_records_by_token))]
    return token


class PackageSnapshot(dict):
    """Inventory together with the resolver-input state it was read from."""
    state_token = None


def state_change_message(expected, actual, phase):
    message = f'Package state changed {phase}; scan again.'
    before, after = _records_by_token.get(expected), _records_by_token.get(actual)
    if before is not None and after is not None:
        changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
        message += '\nChanged resolver inputs: ' + ', '.join(changed)
    else:
        message += f'\nExpected fingerprint: {expected}\nCurrent fingerprint: {actual}'
    return message


from .operations import CleanerError


class PackageStateChanged(CleanerError):
    """One global invalidation, never a per-application usage/validation failure."""
