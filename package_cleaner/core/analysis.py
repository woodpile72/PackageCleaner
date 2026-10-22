# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Review hints and on-demand, explicitly limited usage evidence."""
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
from .inventory import application_root
from .operations import checkpoint, cancellable
import time


def command(args, timeout=15):
    checkpoint()
    if cancellable():
        try:
            with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  env={**os.environ, 'LC_ALL': 'C'}) as process:
                deadline = time.monotonic() + timeout
                try:
                    while True:
                        checkpoint()
                        if time.monotonic() >= deadline:
                            raise subprocess.TimeoutExpired(args, timeout)
                        try:
                            out, err = process.communicate(timeout=0.1)
                            return process.returncode, out, err
                        except subprocess.TimeoutExpired:
                            continue
                except BaseException:
                    process.kill()
                    process.communicate()
                    raise
        except (OSError, subprocess.TimeoutExpired) as exc:
            return -1, '', str(exc)
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                   env={**os.environ, 'LC_ALL': 'C'})
        return completed.returncode, completed.stdout, completed.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, '', str(exc)


def review_reason(package, days=365, now=None):
    if package.protected:
        return None
    if package.automatic or not application_root(package):
        return None
    if package.installed_at:
        age = ((now or datetime.now()) - datetime.fromisoformat(package.installed_at)).days
        if age >= days:
            return f'Manual package with a recorded installation {age} days ago; age is not inactivity.'
        return None
    return 'Manual package with no retained installation date; usage and age are unknown.'


def usage(package, days=90):
    evidence = []
    code, out, err = command(['dpkg-query', '-L', package.name])
    if code:
        return [{'source': 'file inventory', 'confidence': 'unknown', 'detail': err.strip()}]
    executables, units = [], set()
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    for value in out.splitlines():
        path = Path(value)
        if path.suffix == '.service' and ('/systemd/system/' in value or '/systemd/user/' in value):
            units.add((path.name, '/systemd/user/' in value))
        if any(value.startswith(prefix) for prefix in ('/usr/bin/', '/usr/sbin/', '/bin/', '/sbin/', '/usr/libexec/')):
            try:
                st = path.stat()
                if path.is_file() and st.st_mode & 0o111:
                    executables.append((value, st.st_atime))
            except OSError:
                pass
    recent = [(path, datetime.fromtimestamp(stamp, timezone.utc).isoformat())
              for path, stamp in executables if stamp >= cutoff]
    evidence.append({'source': 'executable access times', 'confidence': 'low',
                     'detail': f'{len(recent)} of {len(executables)} executable paths accessed within {days} days. '
                     'Reads, backups and scanners can update atime; relatime/noatime and restored files limit reliability. '
                     'Access does not prove execution; old/missing atime does not prove inactivity.',
                     'recent_paths': recent})
    for unit, user in sorted(units):
        scope = ['--user'] if user else []
        code, out, err = command(['systemctl', *scope, 'show', '--no-pager',
                                 '--property=LoadState,ActiveState,SubState,ActiveEnterTimestamp', unit])
        evidence.append({'source': f'systemd {"user" if user else "system"} {unit}',
                         'confidence': 'high' if code == 0 and 'LoadState=loaded' in out else 'unknown',
                         'detail': out.strip() if code == 0 else err.strip()})
        code, out, err = command(['journalctl', *scope, '--no-pager', '--quiet',
                                 '--user-unit' if user else '--unit', unit,
                                 f'--since={days} days ago', '-n', '1', '-o', 'short-iso'])
        evidence.append({'source': f'journal {unit}',
                         'confidence': 'medium' if code == 0 and out.strip() else 'unknown',
                         'detail': (out.strip() if code == 0 and out.strip() else 'No accessible retained entry.') +
                         ' Journal entries are lifecycle/diagnostic records, not application-usage evidence. Startup and reload messages are never counted as served requests. '
                         'Permissions and retention limit coverage.' + (f' {err.strip()}' if err.strip() else '')})
    if not units:
        evidence.append({'source': 'services', 'confidence': 'unknown',
                         'detail': 'No packaged service unit found; generated units and other launch methods may exist.'})
    from .service_activity import inspect_server
    server = inspect_server(package)
    if server:
        evidence.append({'source': 'Server application activity (separate from service lifecycle)', 'evidence': server})
    return evidence


def review_units(packages):
    """One approval per application; stack membership only supplies context."""
    apps = [p for p in packages.values() if application_root(p)
            and not p.automatic and not p.protected]
    apps.sort(key=lambda p: (p.stack == 'Other software', not p.desktop_application,
                            review_reason(p) is None, p.installed_at or '9999', -p.size, p.name))
    return [(f'{p.base} [{p.stack}]' if p.stack != 'Other software' else p.base, [p.name]) for p in apps]
