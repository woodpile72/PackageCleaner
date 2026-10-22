# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Opt-in execution protocol. Only the privileged backend may change packages."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import gzip
import secrets
import shutil
from contextlib import ExitStack
from .result_protocol import ResultReceiver, digest, encoded, verify_result
from datetime import datetime, timezone
from .preferences import Preferences, xdg_directory
from .state import package_state

RESULT_PREFIX = 'PACKAGE_CLEANER_RESULT '


def preference_state(path):
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        data = b''
    if data:
        obj = json.loads(data)
        if obj.get('version') != 1 or not isinstance(obj.get('kept'), list) or not all(isinstance(n, str) for n in obj['kept']):
            raise ValueError('Saved Keep preferences could not be verified.')
        kept = sorted({n.removeprefix('apt:system/') for n in obj['kept']})
    else:
        kept = []
    return {'digest': hashlib.sha256(data).hexdigest(), 'kept': kept}


def inventory_record(packages):
    return {n: {'version': p.version, 'automatic': p.automatic,
                'protected': sorted(p.protected), 'dependencies': sorted(p.dependencies)}
            for n, p in sorted(packages.items())}


def dpkg_state():
    result = subprocess.run(['/usr/bin/dpkg-query', '-W', '-f=${Package}:${Architecture}\t${Version}\t${db:Status-Abbrev}\n'],
                            capture_output=True, text=True, check=True, env={**os.environ, 'LC_ALL': 'C'})
    return sorted(result.stdout.splitlines())


def apt_configuration():
    result = subprocess.run(['/usr/bin/apt-config', 'dump'], capture_output=True, text=True,
                            check=True, env={**os.environ, 'LC_ALL': 'C'})
    return result.stdout


def plan_record(plan):
    return {key: getattr(plan, key) for key in
            ('requested', 'removed', 'newly_orphaned', 'existing_orphans', 'dependent_removals',
             'transaction_arguments', 'savings', 'baseline_selected_bytes')}


def approval_record(plan, packages, preferences):
    if getattr(plan, 'execution_disabled', False) or any(packages[n].provider != 'apt' for n in plan.removed):
        raise ValueError('This provider transaction is not authorized for execution.')
    if plan.blocked or not plan.removed:
        raise ValueError('Only a complete, unblocked selection can be executed.')
    from .providers import subset
    packages = subset(packages, 'apt')
    token = package_state()
    if token != plan.state_token:
        raise ValueError('Package information changed. Review the selection again.')
    prefs = preference_state(preferences.path)
    if set(plan.removed) & set(prefs['kept']):
        raise ValueError('The selection includes software saved with Keep.')
    record = {'version': 1, 'uid': os.getuid(), 'approved_at': datetime.now(timezone.utc).isoformat(),
              'preferences_path': str(preferences.path.absolute()), 'preferences': prefs,
              'state_token': token, 'inventory': inventory_record(packages),
              'dpkg_state': dpkg_state(), 'apt_configuration': apt_configuration(), 'plan': plan_record(plan)}
    if package_state() != token:
        raise ValueError('Package information changed. Review the selection again.')
    return record


def run_validated(record, backend, preferences_reader=preference_state):
    """Testable fail-closed engine. Backend lock spans validation and installation."""
    result = {'status': 'rejected', 'validation': 'not completed', 'execution_started': False,
              'exit_status': None, 'removed': [], 'remaining': [], 'unknown': []}
    expected = record['plan']['removed']
    lock = ExitStack()
    try:
        lock.enter_context(backend.lock())
        def check_inputs():
            if preferences_reader(record['preferences_path']) != record['preferences']:
                raise ValueError('Keep preferences changed. A new review is required.')
            if backend.configuration() != record['apt_configuration']:
                raise ValueError('APT configuration differs under administrator privileges. A new review is required.')
            if backend.state_token() != record['state_token']:
                raise ValueError('Package environment changed. A new review is required.')
            if backend.status() != record['dpkg_state']:
                raise ValueError('Package installation states changed. A new review is required.')
        check_inputs()
        packages = backend.inventory()
        if inventory_record(packages) != record['inventory']:
            raise ValueError('Installed versions, dependencies, protection or installation flags changed.')
        plan = backend.simulate(record['plan']['requested'], packages, record['preferences']['kept'])
        if plan.blocked or plan_record(plan) != record['plan']:
            raise ValueError('The removal proposal changed or is blocked. A new review is required.')
        # Independent checks do not trust an allegedly unblocked saved plan.
        removed = set(plan.removed)
        requested = set(plan.requested)
        if removed != requested | set(plan.newly_orphaned) or plan.dependent_removals:
            raise ValueError('Unexpected additional removals in the proposal.')
        for name in removed:
            p = packages[name]
            if p.protected or name in record['preferences']['kept'] or (not p.automatic and name not in requested):
                raise ValueError('Protected, kept or unapproved manual software is affected.')
            if name in plan.baseline.removed and name not in requested:
                raise ValueError('Unrelated existing orphan would be removed.')
        backend.prepare(sorted(removed))
        check_inputs()
        result['validation'] = 'passed: locked APT plan exactly matches approval'
        if hasattr(backend, 'validated'):
            backend.validated(plan)
        result['execution_started'] = True
        result['exit_status'] = backend.execute()
        result['status'] = 'completed' if result['exit_status'] == 0 else 'incomplete'
    except (Exception, KeyboardInterrupt) as exc:
        result['error'] = str(exc) or 'Execution interrupted.'
        result['status'] = 'incomplete' if result['execution_started'] else 'rejected'
    finally:
        try:
            final = backend.observed(expected)
            result['final_state'] = final
            result['final_dpkg_state'] = backend.status()
            before_others = [line for line in record['dpkg_state'] if line.split('\t')[0] not in expected]
            after_others = [line for line in result['final_dpkg_state'] if line.split('\t')[0] not in expected]
            if result['execution_started'] and before_others != after_others:
                result['unexpected_state_change'] = True
                result['status'] = 'incomplete'
            absent = [n for n, state in final.items() if state in ('not-installed', 'config-files')]
            result['removed'] = absent if result['execution_started'] else []
            result['already_absent'] = [] if result['execution_started'] else absent
            result['remaining'] = [n for n, state in final.items() if state == 'installed']
            result['unknown'] = sorted(set(expected) - set(absent) - set(result['remaining']))
            if result['status'] == 'completed' and set(result['removed']) != set(expected):
                result['status'] = 'incomplete'
        except Exception as exc:
            result['observation_error'] = str(exc)
            result['unknown'] = list(expected)
            if result['execution_started']:
                result['status'] = 'unknown'
        finally:
            lock.close()
    return result


def private_write(path, data):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
        stream.write(data)
        stream.flush(); os.fsync(stream.fileno())


def launch(record, log_directory=None, runner=None, *, graphical=False):
    """Called only after REMOVE. A dedicated socket carries verified results."""
    directory = Path(log_directory) if log_directory else xdg_directory('XDG_STATE_HOME', '.local/state') / 'package-cleaner'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise ValueError('Execution log directory must belong to you and not be a symlink.')
    directory.chmod(0o700)
    attempt = Path(tempfile.mkdtemp(prefix='attempt-', dir=directory))
    record = dict(record)
    record['transport'] = {'attempt': secrets.token_hex(24), 'approval_digest': digest(record),
                           'address': '\0package-cleaner-' + secrets.token_hex(20)}
    approval = attempt / 'approval.json.gz'
    private_write(approval, gzip.compress(encoded(record), mtime=0))
    helper = Path(__file__).with_name('privileged.py')
    command = ['/usr/bin/sudo', '--', '/usr/bin/env', '-i', 'PATH=/usr/sbin:/usr/bin:/sbin:/bin',
               'LC_ALL=C', '/usr/bin/python3', '-I', str(helper), str(approval)]
    environment = None
    if graphical:
        askpass = graphical_askpass()
        command.insert(1, '-A')
        environment = {**os.environ, 'SUDO_ASKPASS': askpass}
    receiver = ResultReceiver(record['transport']['address'], os.getuid() if runner else 0)
    output_path = attempt / 'output.pending'
    process_status, launch_error = None, None
    try:
        with os.fdopen(os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
            try:
                if runner:
                    process_status = runner(command, output, record)
                else:
                    child = subprocess.Popen(command, stdout=output, stderr=output, **({"env":environment,"stdin":subprocess.DEVNULL} if graphical else {}))
                    while True:
                        try:
                            process_status = child.wait()
                            break
                        except KeyboardInterrupt:
                            # SIGINT reaches sudo/installer too. Never abandon or retry.
                            continue
            except (OSError, KeyboardInterrupt) as exc:
                launch_error = str(exc) or 'Interrupted while awaiting helper completion.'
    finally:
        receiver.finish()
    try:
        if launch_error or receiver.error:
            raise ValueError(launch_error or receiver.error)
        result = verify_result(receiver.payload, receiver.snapshot, record, process_status)
    except ValueError as exc:
        result = {'status': 'unknown', 'helper_verified': False, 'error': str(exc),
                  'removed': [], 'unknown': record['plan']['removed'],
                  'reconciliation_required': True}
    if receiver.packed is not None:
        private_write(attempt / 'after-dpkg.json.gz', receiver.packed)
    result.update(process_exit_status=process_status, command=command, log_directory=str(attempt),
                  finished_at=datetime.now(timezone.utc).isoformat())
    # The socket address/nonce are protocol data, not useful user diagnostics.
    for key in ('attempt', 'approval_digest', 'protocol'):
        result.pop(key, None)
    private_write(attempt / 'result.json', encoded(result) + b'\n')
    with output_path.open('rb') as stream:
        output_size = os.fstat(stream.fileno()).st_size
        if output_size <= 32768:
            output_text = stream.read().decode(errors='replace')
        else:
            head = stream.read(8192)
            stream.seek(-8192, os.SEEK_END)
            tail = stream.read()
            output_text = (head.decode(errors='replace') + '\n[Full package-manager output: package-manager.log.gz]\n' + tail.decode(errors='replace'))
            stream.seek(0)
            fd = os.open(attempt / 'package-manager.log.gz', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', mtime=0) as packed:
                shutil.copyfileobj(stream, packed)
    lines = ['Started: ' + record.get('approved_at', 'not recorded'),
             'Selected applications: ' + ', '.join(record['plan']['requested']),
             'Approved removals: ' + ', '.join(record['plan']['removed']),
             'Validation: ' + result.get('validation', 'not verified'),
             'Package-manager output:', output_text,
             'Result: ' + result['status'], 'Helper verified: ' + str(result['helper_verified']),
             'Helper process exit: ' + str(process_status),
             'Package-manager exit: ' + str(result.get('exit_status')),
             'Finished: ' + result['finished_at']]
    if result.get('error'):
        lines.append('Error: ' + result['error'])
    private_write(attempt / 'execution.log', ('\n'.join(lines) + '\n').encode())
    output_path.unlink()
    return result


def graphical_askpass():
    """Use sudo's supported graphical prompt, never collect passwords ourselves."""
    import stat
    for path in ('/usr/bin/ssh-askpass','/usr/lib/openssh/ssh-askpass',
                 '/usr/lib/ssh/ssh-askpass','/usr/bin/ksshaskpass','/usr/bin/kdesvnaskpass'):
        try:
            info=os.stat(path)
            if info.st_uid==0 and stat.S_ISREG(info.st_mode) and not info.st_mode & 0o022 and os.access(path,os.X_OK):
                return path
        except OSError:
            continue
    raise ValueError('Graphical administrator authentication is unavailable. Use the terminal interface; no packages were changed.')
