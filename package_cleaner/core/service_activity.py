# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Read-only, bounded server evidence. Never equate lifecycle events with usage."""
from datetime import datetime, timezone, timedelta
import glob
import gzip
from pathlib import Path
import re
import shlex
from contextlib import ExitStack
from .inspection import open_inspection, read_inspection_text
import stat

from .analysis import command

REQUEST = re.compile(r'\[([^\]]+)\]\s+"((?:\\.|[^"\\])*)"\s+(\d{3})\s')
MONTHS = {m: i + 1 for i, m in enumerate('Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split())}


def log_time(value):
    match = re.fullmatch(r'(\d{2})/([A-Za-z]{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})', value)
    if not match:
        raise ValueError('Unsupported access-log timestamp')
    day, month, year, hour, minute, second, sign, zh, zm = match.groups()
    if int(zh) > 23 or int(zm) > 59:
        raise ValueError('Invalid timezone offset')
    offset = timedelta(hours=int(zh), minutes=int(zm)) * (1 if sign == '+' else -1)
    return datetime(int(year), MONTHS[month], int(day), int(hour), int(minute), int(second),
                    tzinfo=timezone(offset)).astimezone(timezone.utc)


def access_logs(paths, now=None, max_bytes=32 * 1024 * 1024, max_files=128):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.astimezone()
    result = {'source': 'HTTP access logs', 'request_records': 0, 'recent_request_records': 0,
              'recent_successful_responses': 0, 'recent_status_counts': {},
              'first_observed_request': None, 'last_observed_request': None,
              'last_successful_response': None, 'readable_files': [], 'unreadable_files': [],
              'unparsed_records': 0, 'future_records': 0, 'truncated': False,
              'coverage_complete': False,
              'limitations': ['Retained logs are not complete history; rotation, permissions, conditional logging and alternate destinations limit coverage.',
                              'Requests may be automated probes or monitoring. Responses, including 2xx, do not prove human or meaningful application use.']}
    seen = set()
    consumed = 0
    for index, raw in enumerate(sorted(set(map(str, paths)))):
        if index >= max_files or consumed >= max_bytes:
            result['truncated'] = True
            break
        path = Path(raw)
        try:
            st = path.stat()
            if not stat.S_ISREG(st.st_mode):
                result['unreadable_files'].append(raw)
                continue
            identity = (st.st_dev, st.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
            with ExitStack() as stack:
                stream = stack.enter_context(open_inspection(path, 'rb'))
                if path.suffix == '.gz':
                    stream = stack.enter_context(gzip.GzipFile(fileobj=stream))
                while consumed < max_bytes:
                    line = stream.readline(min(1024 * 1024, max_bytes - consumed))
                    if not line:
                        break
                    consumed += len(line)
                    if not line.endswith(b'\n'):
                        result['truncated'] = True
                        break
                    match = REQUEST.search(line.decode('utf-8', errors='replace'))
                    if not match or not re.fullmatch(r'[A-Z]+ \S+ HTTP/\d+(?:\.\d+)?', match[2]):
                        result['unparsed_records'] += 1
                        continue
                    try:
                        stamp = log_time(match[1])
                        status = int(match[3])
                        if not 100 <= status <= 599:
                            raise ValueError('Invalid status')
                    except (ValueError, KeyError, OverflowError):
                        result['unparsed_records'] += 1
                        continue
                    if stamp > now:
                        result['future_records'] += 1
                        continue
                    iso = stamp.isoformat()
                    result['request_records'] += 1
                    result['first_observed_request'] = min(result['first_observed_request'] or iso, iso)
                    result['last_observed_request'] = max(result['last_observed_request'] or iso, iso)
                    if 200 <= status < 300:
                        result['last_successful_response'] = max(result['last_successful_response'] or iso, iso)
                    if stamp >= now - timedelta(days=90):
                        result['recent_request_records'] += 1
                        counts = result['recent_status_counts']
                        counts[str(status)] = counts.get(str(status), 0) + 1
                        result['recent_successful_responses'] += 200 <= status < 300
                if consumed >= max_bytes:
                    result['truncated'] = True
            result['readable_files'].append(raw)
        except (OSError, EOFError):
            result['unreadable_files'].append(raw)
    # No client addresses, request paths, query strings or log messages are returned.
    return result


def discover_apache_logs(config=Path('/etc/apache2/apache2.conf'), log_dir=Path('/var/log/apache2')):
    """Inspect static CustomLog/TransferLog and Include paths; never execute logging pipes."""
    config, log_dir = Path(config), Path(log_dir)
    queue, seen = [config], set()
    destinations = set(log_dir.glob('*access*.log'))
    warnings = []
    while queue and len(seen) < 128:
        path = queue.pop()
        if str(path) in seen:
            continue
        seen.add(str(path))
        try:
            if path.stat().st_size > 256 * 1024 or not path.is_file():
                warnings.append('A configuration file was not inspected because of its type or size.')
                continue
            lines = read_inspection_text(path, errors='replace').splitlines()
        except OSError:
            warnings.append('A configuration file could not be read: ' + str(path))
            continue
        for line in lines:
            try:
                fields = shlex.split(line, comments=True)
            except ValueError:
                continue
            if len(fields) < 2:
                continue
            directive = fields[0].lower()
            if directive not in ('customlog', 'transferlog', 'include', 'includeoptional'):
                continue
            value = fields[1].replace('${APACHE_LOG_DIR}', str(log_dir))
            if '$' in value or value.startswith('|'):
                warnings.append('An indirect or piped log/include destination was not inspected.')
                continue
            target = Path(value) if Path(value).is_absolute() else config.parent / value
            if directive.startswith('include'):
                queue.extend(Path(p) for p in glob.glob(str(target)))
            else:
                destinations.add(target)
    if queue:
        warnings.append('Configuration include traversal was limited to 128 files.')
    paths = set(log_dir.glob('*access*.log*'))
    for destination in destinations:
        paths.add(destination)
        paths.update(destination.parent.glob(destination.name + '.*'))
        paths.update(destination.parent.glob(destination.name + '-*'))
    warnings.append('Static configuration discovery may include conditional directives and miss custom instances, environment overrides or remote/piped logging.')
    return paths, warnings


def listening(process_names):
    """Only attribute sockets with visible process ownership; never infer from port numbers."""
    found = []
    for transport, options in [('TCP', '-ltnp'), ('Unix', '-lxnp')]:
        code, out, err = command(['ss', '-H', options], timeout=3)
        if code == 0:
            for line in out.splitlines():
                if any(re.search(r'"' + re.escape(name) + r'"', line) for name in process_names):
                    found.append(transport)
    return {'state': 'listening' if found else 'unknown', 'observed_socket_count': len(found),
            'transports': sorted(set(found)),
            'limitation': 'Only sockets with visible matching process ownership are attributed. Missing ownership, namespaces or permissions leave the result unknown; listening is not usage.'}


def inspect_server(package, now=None):
    name = package.base
    if name == 'apache2':
        paths, warnings = discover_apache_logs()
        requests = access_logs(paths, now=now)
        requests['limitations'].extend(warnings)
        return {'listening': listening(['apache2', 'httpd']), 'application_activity': requests}
    if name == 'nginx' or name.startswith('nginx-'):
        requests = access_logs(Path('/var/log/nginx').glob('*access*.log*'), now=now)
        requests['limitations'].append('Only conventional Nginx access-log paths were inspected; custom destinations are not resolved.')
        return {'listening': listening(['nginx']), 'application_activity': requests}
    if ('-server' in name and package.stack == 'MariaDB / MySQL') or (package.stack == 'PHP' and name.endswith('-fpm')):
        processes = ['mariadbd', 'mysqld'] if package.stack == 'MariaDB / MySQL' else ['php-fpm', re.sub(r'^php(.*)-fpm$', r'php-fpm\1', name)]
        return {'listening': listening(processes), 'application_activity': {
            'source': 'No verified application-activity reader for this service',
            'recent_request_records': 0, 'last_observed_request': None, 'coverage_complete': False,
            'limitations': ['Service startup/error logs are not query or request history. No new logging is enabled and no live queries or test requests are sent.']}}
    return None
