# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Domain summaries with no presentation or I/O dependencies."""
from .removal import establish_baseline
from .providers import subset, eligible_support, effective_kept


def supporting_report(packages, kept=()):
    kept = effective_kept(packages, kept)
    baseline = establish_baseline(subset(packages, 'apt'))
    candidates = set(baseline.removed) | {p.name for p in packages.values() if p.provider == 'flatpak' and p.orphan}
    eligible = [n for n in sorted(candidates) if not packages[n].protected and n not in kept and packages[n].automatic]
    return {'already_orphaned': eligible,
            'installed_bytes': sum(packages[n].size for n in eligible),
            'blocked': [],
            'excluded': {n: packages[n].protected or ['Kept or manually installed supporting package.']
                         for n in sorted(candidates - set(eligible))}}
