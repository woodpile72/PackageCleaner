# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Application preferences only; never touch APT's installation flags."""
import json
import os
from pathlib import Path
import tempfile


def xdg_directory(variable, fallback):
    value = os.environ.get(variable)
    return Path(value) if value and Path(value).is_absolute() else Path.home() / fallback


class Preferences:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else xdg_directory('XDG_CONFIG_HOME', '.config') / 'package-cleaner/preferences.json'
        source = self.path
        if path is None and not source.exists():
            legacy = xdg_directory('XDG_STATE_HOME', '.local/state') / 'package-cleaner/preferences.json'
            if legacy.exists():
                source = legacy
        self.kept = set()
        if source.exists():
            try:
                data = json.loads(source.read_text())
                if data.get('version') != 1 or not isinstance(data.get('kept'), list) or not all(isinstance(n, str) for n in data['kept']):
                    raise ValueError('unsupported preference format')
                self.kept = set(data['kept'])
            except (ValueError, AttributeError) as exc:
                raise ValueError(f'Cannot safely read Keep preferences at {source}: {exc}. Restore the file or use a different --preferences path.') from exc

        if source != self.path:
            # Keep the old file as a backup. Even an empty config file takes precedence.
            self.save()

    def remove(self, identifier):
        if identifier in self.kept:
            name = identifier
        else:
            matches = [n for n in self.kept if n.removeprefix('apt:system/').split(':')[0] == identifier or n.removeprefix('apt:system/') == identifier]
            if len(matches) != 1:
                raise ValueError('Unknown or ambiguous ignored entry; use ignored list for exact identifiers.')
            name = matches[0]
        self.reset([name])

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=self.path.parent, delete=False) as stream:
                temporary = stream.name
                json.dump({'version': 1, 'kept': sorted(self.kept)}, stream, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def reload(self):
        """Pick up decisions made by another open interface."""
        self.kept = Preferences(self.path).kept

    def keep(self, name):
        self.reload()
        self.kept.add(name)
        self.save()

    def reset(self, names=None):
        self.reload()
        self.kept = self.kept - set(names) if names is not None else set()
        self.save()
