# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Conservative metadata classification, not a dependency resolver or usage verdict.

All installed Depends/PreDepends alternatives count as possible requirements.
APT alone decides the actual consequences when a transaction is requested.
"""
from dataclasses import dataclass
from enum import Enum
from .operations import checkpoint


class Classification(str, Enum):
    REVIEWABLE = 'REVIEWABLE'
    REQUIRED = 'REQUIRED'
    PROTECTED = 'PROTECTED'
    UNCERTAIN = 'UNCERTAIN'


FILTERS = {'reviewable':'Reviewable', 'uncertain':'Uncertain', 'required':'Required', 'all':'All components',
           'investigate':'Manual support to investigate'}
SUPPORT_SECTIONS = {'libs','libdevel','debug','oldlibs','introspection','doc','fonts','localization',
                    'metapackages','kernel','translations'}


@dataclass(frozen=True)
class ComponentAssessment:
    classification: Classification
    reasons: tuple[str, ...]
    required_by: tuple[str, ...] = ()
    desktop_root: str | None = None
    manual_investigation: bool = False
    orphan_kind: str = 'none'
    priority: float = 0


def file_roles(paths):
    """Installed integration points identify roles without package-name heuristics."""
    prefixes = {
        'hardware/device integration': ('/usr/lib/udev/','/lib/udev/','/etc/udev/','/etc/acpi/events/'),
        'boot/kernel integration': ('/usr/share/initramfs-tools/','/etc/initramfs-tools/','/etc/kernel/',
                                   '/usr/lib/modules/','/lib/modules/','/usr/lib/systemd/system-generators/',
                                   '/lib/systemd/system-generators/','/usr/lib/systemd/system-sleep/','/usr/lib/os-probes/','/usr/lib/linux-boot-probes/','/usr/lib/grub/','/etc/default/grub.d/'),
        'system-bus integration': ('/usr/share/dbus-1/system-services/','/usr/share/dbus-1/system.d/','/etc/dbus-1/system.d/'),
        'desktop-session integration': ('/etc/xdg/autostart/','/usr/share/dbus-1/services/', '/usr/lib/systemd/user/'),
        'security-policy integration': ('/etc/apparmor/','/etc/apparmor.d/','/usr/lib/apparmor/','/usr/lib/security/','/lib/security/'),
        'authentication integration': ('/etc/pam.d/','/usr/share/pam-configs/'),
        'printer/scanner integration': ('/usr/lib/cups/','/usr/share/cups/drv/','/usr/share/cups/model/','/usr/share/ppd/','/etc/sane.d/'),
        'network integration': ('/etc/dhcp/','/etc/NetworkManager/dispatcher.d/','/etc/network/if-up.d/','/etc/network/if-pre-up.d/'),
    }
    return sorted(role for role, locations in prefixes.items() if any(path.startswith(locations) for path in paths))


def system_component(p):
    """Keep ordinary desktop apps in their existing categories; inspect infrastructure here."""
    return p.provider == 'apt' and (not p.desktop_application or bool(p.service_units) or
        bool(set(p.desktop_categories) & {'System','Settings','Accessibility','DesktopSettings'}))


def classify_components(packages):
    apt = {n:p for n,p in packages.items() if p.provider == 'apt'}
    reverse = {n:set() for n in apt}
    recommended = {n:set() for n in apt}
    for n,p in apt.items():
        checkpoint('classification')
        for dep in p.dependencies:
            if dep in apt and dep != n:
                reverse[dep].add(n)
        for dep in p.recommends:
            if dep in apt and dep != n:
                recommended[dep].add(n)
    # Installed desktop/metapackage relationships, not a second name blacklist.
    roots = {n for n,p in apt.items() if p.protected or
             (p.section.split('/')[-1] == 'metapackages' and not p.automatic) or
             (p.desktop_application and set(p.desktop_categories) & {'DesktopSettings','Accessibility'})}
    reached = {n:n for n in sorted(roots)}
    pending = list(reached)
    while pending:
        n = pending.pop()
        for dep in apt[n].dependencies + apt[n].recommends:
            if dep in apt and dep not in reached:
                reached[dep] = reached[n]
                pending.append(dep)
    library_sources = {p.source for n,p in apt.items() if p.source and
                       p.section.split('/')[-1] in {'libs','libdevel'} and (p.protected or n in reached or reverse[n])}
    result = {}
    for n,p in apt.items():
        section = p.section.split('/')[-1]
        users = tuple(sorted(reverse[n]))
        soft_users = tuple(sorted(recommended[n]))
        reasons = []
        investigate = False
        orphan_kind = 'existing_autoremove' if p.orphan and p.automatic else 'none'
        priority = 0
        if p.protected:
            status = Classification.PROTECTED
            reasons.extend(p.protected)
        elif n in reached:
            status = Classification.REQUIRED
            reasons.append('Part of an installed protected system, desktop, or metapackage dependency set (including recommendations).')
            reasons.append('Dependency anchor: '+reached[n]+'.')
        elif users:
            status = Classification.REQUIRED
            reasons.append(f'{len(users)} installed package(s) have a hard dependency relationship with this component.')
            reasons.append('Installed alternatives are counted conservatively; this does not prove every alternative is necessary.')
        elif soft_users:
            status = Classification.UNCERTAIN
            reasons.append('Recommended by installed software; the absence of a hard dependency is insufficient to recommend removal.')
        elif p.integration_roles:
            status = Classification.UNCERTAIN
            reasons.append('Installed files provide '+', '.join(p.integration_roles)+'.')
            reasons.append('These integrations may be used without APT reverse dependencies. Their optionality is not established.')
        elif p.source and p.source in library_sources and not p.desktop_application:
            status = Classification.UNCERTAIN
            reasons.append('Built from the same source as an installed library required by other software; this may be an internal supporting tool.')
        elif p.package_priority == 'standard':
            status = Classification.UNCERTAIN
            reasons.append('APT metadata identifies this as a standard system package; independent optionality is not established.')
        elif p.automatic:
            status = Classification.UNCERTAIN
            reasons.append('Automatically installed supporting software; it is not an independently selected application.')
            if p.orphan:
                reasons.append('APT already reports this as an autoremove candidate; inspect it in Unneeded supporting packages.')
        elif section in SUPPORT_SECTIONS or not (p.public_commands or p.service_units or p.desktop_application):
            status = Classification.UNCERTAIN
            investigate = (p.file_metadata_available and section not in {'fonts','localization','translations','doc','metapackages','kernel'})
            orphan_kind = 'manual_support_investigation' if investigate else 'none'
            if investigate:
                priority = 10 if section in {'libs','libdevel','oldlibs'} else 5
            reasons.extend(['Manually installed support with no recorded installed reverse dependency or recommendation.',
                            'This is an investigation clue, not proof it is unnecessary. Scripts, plugins, dynamically loaded libraries and local software may still require it.'])
        elif set(p.desktop_categories) & {'Settings','DesktopSettings','Accessibility'}:
            status = Classification.UNCERTAIN
            reasons.append('Desktop configuration or accessibility role; optionality is not established by the installed dependency graph.')
        elif p.service_units and not p.public_commands and not p.desktop_application:
            status = Classification.UNCERTAIN
            reasons.append('A background service is present, but no independent public tool or launcher was identified. Its purpose requires investigation.')
        elif not p.summary.strip() or not p.file_metadata_available:
            status = Classification.UNCERTAIN
            reasons.append('Insufficient package role or file-list metadata to establish an independent tool.')
        else:
            status = Classification.REVIEWABLE
            role = 'background service' if p.service_units else 'desktop administration tool' if p.desktop_application else 'standalone command-line tool'
            reasons.extend([f'Installed file metadata exposes a {role} entry point.',
                            'Manually installed, outside the protected/desktop dependency set, with no recorded installed reverse dependencies or recommendations.',
                            'Reviewable means worth asking about, not unused or validated for removal. Local scripts and software outside APT may rely on it.'])
            priority = 80 if p.service_units or section in {'devel','web','httpd','php','net','database'} else 70 if section == 'admin' else 50
            # A continuous tiebreaking preference, never a size eligibility cutoff.
            import math
            priority += min(15, math.log2(max(1,p.size))) / 10
        if p.integration_roles and not any('Installed files provide' in reason for reason in reasons):
            reasons.append('Integration roles: '+', '.join(p.integration_roles)+'.')
        if p.service_units:
            reasons.append('Service units are present. Running/enabled state alone is not evidence of actual use.')
        result[n] = ComponentAssessment(status,tuple(reasons),users or soft_users,
                         reached.get(n),investigate,orphan_kind,priority)
    return result


def filter_components(items, mode='reviewable'):
    if mode not in FILTERS:
        raise ValueError('Unknown component filter.')
    if mode == 'all':
        return list(items)
    if mode == 'investigate':
        return [i for i in items if i.component_assessment and i.component_assessment.manual_investigation]
    return [i for i in items if i.component_assessment and i.component_assessment.classification.value == mode.upper()]


def component_recommendations(packages, kept=(), mode='reviewable', assessments=None):
    from .recommendations import Recommendation, Evidence, describe
    from .providers import effective_kept
    assessments = assessments if assessments is not None else classify_components(packages)
    kept = effective_kept(packages,kept)
    items = []
    for n,a in assessments.items():
        p = packages[n]
        if not system_component(p) or (n in kept and a.classification == Classification.REVIEWABLE):
            continue
        title,purpose,_ = describe(p)
        items.append(Recommendation(n,title,purpose,'',Evidence(),a.priority,[],installed_bytes=p.size,
                     identity=p.identity,protected=list(p.protected),component_assessment=a))
    items.sort(key=lambda i:(-i.score,i.title.casefold(),i.package))
    return filter_components(items,mode)
