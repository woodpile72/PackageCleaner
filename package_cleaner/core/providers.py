# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Provider boundary. APT internals keep their native identifiers and safeguards."""
from typing import Protocol
from .operations import CleanerError, checkpoint


class PackageProvider(Protocol):
    provider: str
    def discover(self): ...
    def preview(self, names, packages, kept=()): ...


class AptProvider:
    provider = 'apt'

    def discover(self):
        from .inventory import inventory
        return inventory()

    def preview(self, names, packages, kept=()):
        from .removal import simulate
        return simulate(names, packages, kept)


def subset(packages, provider):
    from .state import PackageSnapshot
    cls = PackageSnapshot if hasattr(packages, 'state_token') else dict
    result = cls({n:p for n,p in packages.items() if p.provider == provider})
    if hasattr(packages, 'state_token'):
        result.state_token = packages.state_token
    return result


def inventory():
    from .flatpak_provider import FlatpakProvider
    packages, warnings = AptProvider().discover()
    extra, observations = FlatpakProvider().discover()
    packages.update(extra)
    return packages, [*warnings, *observations]


def filter_sources(items, source='all'):
    if source not in ('all', 'apt', 'flatpak'):
        raise CleanerError('Unknown software source.')
    return [i for i in items if source == 'all' or i.provider == source]


def eligible_support(packages, kept=()):
    return [p for p in packages.values() if p.orphan and p.automatic
            and not p.protected and p.name not in kept and p.identity not in kept]


def effective_kept(packages, kept):
    return set(kept) | {n.removeprefix('apt:system/') for n in kept if n.startswith('apt:system/')} | {n for n,p in packages.items() if p.identity in kept}


def preview(names, packages, kept=()):
    """Separate provider plans; Flatpak plans are never APT execution approvals."""
    from .removal import Plan, Baseline
    from .flatpak_provider import FlatpakProvider
    from .inventory import resolve
    requested = resolve(names, packages)
    kept = effective_kept(packages, kept)
    apt_names = [n for n in requested if packages[n].provider == 'apt']
    fp_names = [n for n in requested if packages[n].provider == 'flatpak']
    if not fp_names:
        return AptProvider().preview(apt_names, subset(packages, 'apt'), kept)
    plans = []
    if apt_names:
        plans.append(AptProvider().preview(apt_names, subset(packages,'apt'), kept))
    plans.extend(FlatpakProvider().preview(fp_names, subset(packages,'flatpak'), kept))
    # An aggregate is a display model only, deliberately unexecutable by the APT helper.
    result = Plan(requested, [], [], [], [], [], [], {}, [], '', Baseline([], ''))
    result.provider = 'mixed' if apt_names else 'flatpak'
    result.installation = 'separate transactions'
    result.provider_plans = plans
    result.execution_disabled = any(p.execution_disabled for p in plans)
    for p in plans:
        for key in ('removed','newly_orphaned','existing_orphans','dependent_removals','retained','unselected_manual','blocked'):
            getattr(result,key).extend(getattr(p,key))
        for key,value in p.savings.items():
            result.savings[key] = result.savings.get(key,0) + value
        result.baseline_selected_bytes += p.baseline_selected_bytes
        result.observations.extend(text for text in p.observations if 'currently running' in text)
    result.observations.append('Provider and installation transactions are separate, each requiring its own approval; no atomic execution or rollback is available.')
    return result
