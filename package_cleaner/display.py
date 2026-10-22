# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Plain display formatting shared by frontends, independent of terminal I/O."""
def local_date(value, now=None):
    """Human-readable observation date; naive historical dates are local time."""
    from datetime import datetime
    if not value:
        return 'unknown'
    try:
        observed = datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone()
    except (ValueError, TypeError, OverflowError):
        return 'unknown'
    today = (now or datetime.now().astimezone()).astimezone().date()
    days = (today - observed.date()).days
    date = f'{observed:%b} {observed.day}, {observed.year}'
    if days < 0:
        age = 'future date; check the system clock'
    elif days == 0:
        age = 'today'
    elif days == 1:
        age = 'yesterday'
    elif days < 30:
        age = f'{days} days ago'
    elif days < 730:
        months = max(1, (today.year - observed.year) * 12 + today.month - observed.month
                     - (today.day < observed.day))
        age = f'about {months} month' + ('s' if months != 1 else '') + ' ago'
    else:
        years = round(days / 365.25)
        age = f'about {years} years ago'
    return f'{date} ({age})'


def installation_label(identifier):
    """Presentation only: never use this label as a transaction identity."""
    from urllib.parse import unquote
    scope, separator, path = identifier.partition('@')
    if separator and scope == 'user':
        return 'User installation'
    if separator and scope.startswith('system/'):
        name = unquote(scope[len('system/'):]) or 'default'
        name = ''.join(c for c in name if c.isprintable())
        return 'System installation (' + name + ')'
    return 'APT system installation' if identifier == 'system' else 'Unknown installation'
