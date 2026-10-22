# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Installed bytes attributable to an application, not removable bytes.

Only exclusive, installed, same-source dependency components are added. Shared
libraries/components and other application entry points are never attributed.
"""
from dataclasses import dataclass
from .inventory import application_root

MB = 1024 ** 2  # Existing UI's binary-scaled MB; all boundaries use this unit.
LARGE_MIN = 100 * MB
LARGE_DESCRIPTION = 'Applications using at least 100 MB'


def size_class(installed_bytes: int) -> str:
    if installed_bytes < 25 * MB:
        return 'Small'
    if installed_bytes < LARGE_MIN:
        return 'Medium'
    if installed_bytes < 500 * MB:
        return 'Large'
    return 'Very large'


@dataclass(frozen=True)
class Footprint:
    installed_bytes: int
    components: tuple[str, ...]
    shared_components: tuple[str, ...]


def application_footprint(name, packages) -> Footprint:
    root = packages[name]
    members = {name}
    pending = [name]
    while pending and root.source:
        for dependency in packages[pending.pop()].dependencies:
            if dependency in members or dependency not in packages:
                continue
            p = packages[dependency]
            if (p.source != root.source or p.protected
                    or p.section.split('/')[-1] in ('libs', 'libdevel', 'debug')
                    or p.desktop_application
                    or (not p.automatic and application_root(p))):
                continue
            members.add(dependency)
            pending.append(dependency)
    # Use the installed dependency graph, including conservative alternatives.
    # Fixed point also removes support beneath a component excluded as shared.
    shared = set()
    while True:
        outsiders = {dep for p in packages.values() if p.name not in members
                     for dep in p.dependencies if dep in members and dep != name}
        if not outsiders:
            break
        members -= outsiders
        shared |= outsiders
    return Footprint(sum(packages[n].size for n in members), tuple(sorted(members)), tuple(sorted(shared)))
