# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Installed metadata and history; never update the APT cache or package flags."""
from dataclasses import dataclass, field
from datetime import datetime
import glob
import configparser
import gzip
import io
from contextlib import ExitStack
import re
from pathlib import Path
from .operations import checkpoint
from .inspection import open_inspection, read_inspection_text


@dataclass
class Package:
    name: str
    base: str
    version: str
    automatic: bool
    size: int
    summary: str
    section: str
    protected: list[str] = field(default_factory=list)
    installed_at: str | None = None
    dependencies: list[str] = field(default_factory=list)
    reverse_dependencies: list[str] = field(default_factory=list)
    stack: str = 'Other software'
    orphan: bool = False
    application: bool = False
    desktop_application: bool = False
    display_name: str = ""
    conffiles: list[tuple[str, str]] = field(default_factory=list)
    desktop_categories: list[str] = field(default_factory=list)
    source: str = ""
    provider: str = 'apt'
    installation: str = 'system'
    ref: str = ''
    runtime: str = ''
    commit: str = ''
    running: bool = False
    public_commands: list[str] = field(default_factory=list)
    service_units: list[str] = field(default_factory=list)
    recommends: list[str] = field(default_factory=list)
    file_metadata_available: bool = False
    integration_roles: list[str] = field(default_factory=list)
    package_priority: str = ''

    @property
    def identity(self):
        return self.name if self.provider == 'flatpak' else 'apt:system/' + self.name



# Deliberately conservative: protection is a guardrail, not a complete OS model.
CORE = re.compile(r'^(mint|cinnamon|muffin|nemo|cjs|libcinnamon|libmuffin|'
                  r'linux-|grub|shim|systemd|libsystemd|udev|libudev|'
                  r'network-manager|networkmanager|netplan|wpasupplicant|'
                  r'dbus|polkit|policykit|accountsservice|gnome-session|gnome-settings-daemon|xapp|libxapp|lightdm|slick-greeter|xorg|xserver-|xwayland|'
                  r'pipewire|wireplumber|pulseaudio|alsa-|'
                  r'mesa-|lib.*mesa|libgl|libegl|libdrm|nvidia|'
                  r'firmware|intel-microcode|amd64-microcode|'
                  r'cryptsetup|lvm2|mdadm|initramfs|ubuntu-desktop)')
# Family membership describes context only; it NEVER grants removal approval.
STACKS = [
    ('Apache', r'^(apache2|libapache2-)'),
    ('PHP', r'^(php([0-9.-]|$)|libphp)'),
    ('MariaDB / MySQL', r'^(mariadb|mysql|default-mysql|galera|libmariadb|libmysql)'),
    ('Development tools', r'^(build-essential$|gcc([0-9-]|$)|g\+\+|clang|llvm|cmake|make$|gdb$|meson$|ninja-build$|rustc$|cargo$)'),
]
ROOTS = {
    'Apache': r'apache2',
    'PHP': r'php(?:[0-9]+(?:\.[0-9]+)*)?(?:-(?:cli|fpm|cgi))?',
    'MariaDB / MySQL': r'(?:mariadb|mysql|default-mysql)-(?:server|client)(?:-[0-9]+(?:\.[0-9]+)*)?',
    'Development tools': r'(?:build-essential|gcc|g\+\+|clang|clangd|llvm|cmake|make|gdb|meson|ninja-build|rustc|cargo)(?:-[0-9]+(?:\.[0-9]+)*)?',
}


def stack_family(name):
    return next((label for label, pattern in STACKS if re.match(pattern, name)), 'Other software')


def application_root(package):
    """Conservative entry points. Manual support is still support, never implicit approval."""
    if package.stack in ROOTS:
        return re.fullmatch(ROOTS[package.stack], package.base) is not None
    return package.application


def stack_catalog(packages):
    catalog = {}
    for package in packages.values():
        if package.stack == 'Other software':
            continue
        group = catalog.setdefault(package.stack, {'members': [], 'candidates': [], 'supporting_packages': [],
                                                   'installed_entry_points': []})
        group['members'].append(package.name)
        if application_root(package):
            group['installed_entry_points'].append(package.name)
            if not package.automatic and not package.protected:
                group['candidates'].append(package.name)
        else:
            group['supporting_packages'].append(package.name)
    for label, group in catalog.items():
        for names in group.values():
            names.sort()
        group['manual_supporting_packages'] = [n for n in group['supporting_packages'] if not packages[n].automatic]
        group['status'] = 'Application entry points installed' if group['installed_entry_points'] else 'Supporting packages only'
        if label == 'MariaDB / MySQL':
            servers = [n for n in group['installed_entry_points'] if '-server' in packages[n].base]
            clients = [n for n in group['installed_entry_points'] if '-client' in packages[n].base]
            group['status'] = ('Server entry point installed' if servers else
                               'Client tools only; no server entry point installed' if clients else
                               'Client libraries / supporting packages only; no server entry point installed')
    return catalog


def stack_selection(label, packages):
    """A stack shorthand may select ONE manual application, never a package group."""
    group = stack_catalog(packages).get(label)
    if not group or not group['candidates']:
        raise ValueError(f'No unprotected manual application entry point for {label!r}. Use groups to inspect support separately.')
    if len(group['candidates']) != 1:
        raise ValueError(f'Multiple application entry points for {label!r}; explicitly name the desired packages: '
                         + ', '.join(group['candidates']))
    return list(group['candidates'])


def history(paths=None):
    """Latest recorded install, including reinstalls after removal; never infer from mtime."""
    dates, warnings = {}, []
    for path in paths if paths is not None else glob.glob('/var/log/dpkg.log*'):
        try:
            with ExitStack() as stack:
                raw = stack.enter_context(open_inspection(path, 'rb'))
                if path.endswith('.gz'):
                    raw = stack.enter_context(gzip.GzipFile(fileobj=raw))
                stream = stack.enter_context(io.TextIOWrapper(raw, errors='replace'))
                for line in stream:
                    parts = line.split()
                    if len(parts) >= 6 and parts[2] == 'install':
                        stamp = parts[0] + 'T' + parts[1]
                        try:
                            datetime.fromisoformat(stamp)
                        except ValueError:
                            continue
                        dates[parts[3]] = max(stamp, dates.get(parts[3], ''))
        except (OSError, EOFError) as exc:
            warnings.append(f'Cannot read history {path}: {exc}')
    return dates, warnings


def protection(pkg):
    reasons = []
    record = pkg.installed.record
    if pkg.essential or record.get('Essential', '') == 'yes':
        reasons.append('essential')
    if record.get('Protected', '') == 'yes':
        reasons.append('APT protected')
    if pkg.installed.priority in ('required', 'important'):
        reasons.append('required/important priority')
    if CORE.match(pkg.name):
        reasons.append('system/desktop protection policy')
    return reasons


def inventory():
    from .state import PackageSnapshot, package_state, state_change_message, PackageStateChanged
    initial_state = package_state()
    try:
        import apt
    except ImportError as exc:
        raise RuntimeError('python3-apt unavailable. Try /usr/bin/python3; nothing will be installed automatically.') from exc
    cache = apt.Cache()
    dates, warnings = history()
    result = PackageSnapshot()
    for p in cache:
        checkpoint("inventory")
        if not p.is_installed:
            continue
        v = p.installed
        row = Package(p.fullname, p.name, v.version, p.is_auto_installed,
                      v.installed_size, v.summary, v.section, protection(p),
                      dates.get(p.fullname, dates.get(p.name)), orphan=p.is_auto_removable)
        row.source = (v.record.get('Source') or p.name).split()[0]
        row.stack = stack_family(p.name)
        row.package_priority = v.priority
        for line in v.record.get('Conffiles', '').splitlines():
            fields = line.split()
            if len(fields) == 2 and re.fullmatch(r'[0-9a-f]{32}', fields[1]):
                row.conffiles.append((fields[0], fields[1]))
        # Desktop launchers and executable-bearing non-library manual packages.
        for key in (p.fullname, p.name):
            try:
                paths = read_inspection_text(Path(f'/var/lib/dpkg/info/{key}.list')).splitlines()
            except OSError:
                continue
            row.file_metadata_available = True
            from .classification import file_roles
            row.integration_roles = file_roles(paths)
            row.public_commands = [x for x in paths if x.startswith(('/usr/bin/','/usr/sbin/','/bin/','/sbin/'))]
            row.service_units = [x for x in paths if x.endswith('.service') and '/systemd/' in x]
            desktop = any(x.startswith('/usr/share/applications/') and x.endswith('.desktop') for x in paths)
            cli = any(x.startswith(('/usr/bin/', '/usr/sbin/', '/bin/', '/sbin/')) for x in paths)
            library = p.name.startswith('lib') or v.section.split('/')[-1] in ('libs', 'libdevel', 'debug')
            if desktop:
                for entry in paths:
                    if entry.startswith('/usr/share/applications/') and entry.endswith('.desktop'):
                        try:
                            config = configparser.ConfigParser(interpolation=None, strict=False)
                            with open_inspection(entry, 'r') as stream:
                                config.read_file(stream)
                            if config.get('Desktop Entry', 'NoDisplay', fallback='false') != 'true':
                                row.display_name = config.get('Desktop Entry', 'Name', fallback='')
                                row.desktop_categories = config.get('Desktop Entry', 'Categories', fallback='').strip(';').split(';')
                            if row.display_name:
                                break
                        except (OSError, configparser.Error, UnicodeError):
                            pass
            row.desktop_application = desktop
            row.application = desktop or (cli and not library and not p.name.endswith(('-dev', '-dbg', '-dbgsym')))
            break
        result[p.fullname] = row
    for p in cache:
        checkpoint("inventory")
        if not p.is_installed:
            continue
        targets = set()
        recommended = set()
        # Version.dependencies exposes hard dependencies only. Recommendations
        # require the explicit API call; do not silently omit desktop coverage.
        for group in [*p.installed.dependencies, *p.installed.get_dependencies('Recommends')]:
            if group.rawtype not in ('Depends', 'PreDepends', 'Recommends'):
                continue
            for alternative in group.or_dependencies:
                (recommended if group.rawtype == 'Recommends' else targets).update(v.package.fullname for v in alternative.installed_target_versions)
        result[p.fullname].recommends = sorted(recommended & result.keys())
        result[p.fullname].dependencies = sorted(targets & result.keys())
        for target in result[p.fullname].dependencies:
            result[target].reverse_dependencies.append(p.fullname)
    current_state = package_state()
    if current_state != initial_state:
        raise PackageStateChanged(state_change_message(initial_state, current_state, 'while reading inventory'))
    result.state_token = initial_state
    return result, warnings


def resolve(names, packages):
    resolved = []
    for name in names:
        matches = [p.name for p in packages.values() if name in (p.name, p.base, p.identity)]
        if len(matches) != 1:
            raise ValueError(f'Unknown or ambiguous installed package: {name}; use name:architecture.')
        if matches[0] not in resolved:
            resolved.append(matches[0])
    return resolved


def support_packages(roots, packages, automatic_only=True):
    """Conservative installed Depends/PreDepends closure, NOT a removal plan."""
    seen, pending = set(roots), list(roots)
    while pending:
        for dep in packages[pending.pop()].dependencies:
            if dep not in seen:
                seen.add(dep)
                pending.append(dep)
    return [{'name': name,
             'outside_users': sorted(set(packages[name].reverse_dependencies) - seen)}
            for name in sorted(seen - set(roots)) if not automatic_only or packages[name].automatic]
