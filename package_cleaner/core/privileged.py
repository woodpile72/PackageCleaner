# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Privileged, non-setuid entry point. Invoked only after explicit UI approval.

SystemLock holds the frontend lock throughout; python-apt temporarily hands the
inner dpkg lock to dpkg in install_archives. A second cooperating APT frontend
cannot resolve a different transaction during that handoff. This is not a claim
of isolation from hostile root processes, maintainer scripts, or filesystem edits.
"""
import logging
import json
import gzip
import os
from pathlib import Path
import sys

# -I ignores user site/PYTHONPATH. This explicitly approved source checkout is
# trusted code, like any program executed through sudo; this is NOT setuid.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from package_cleaner.core.execution import (run_validated, apt_configuration, dpkg_state)
from package_cleaner.core.result_protocol import send_result

logger = logging.getLogger(__name__)


def validate_changes(cache, targets):
    changes = cache.get_changes()
    if cache.broken_count or {p.fullname for p in changes} != set(targets):
        raise ValueError('Locked APT transaction differs from the approved removals.')
    for p in changes:
        if not p.marked_delete or p.marked_install or p.marked_upgrade or p.marked_downgrade or p.marked_reinstall:
            raise ValueError('Unexpected non-removal action: ' + p.fullname)


class AptBackend:
    def __init__(self):
        import apt_pkg
        apt_pkg.init_config()
        apt_pkg.init_system()
        self.apt_pkg = apt_pkg
        self.cache = None

    def lock(self):
        return self.apt_pkg.SystemLock()

    def configuration(self):
        return apt_configuration()

    def state_token(self):
        from package_cleaner.core.state import package_state
        return package_state()

    def status(self):
        return dpkg_state()

    def inventory(self):
        from package_cleaner.core.inventory import inventory
        return inventory()[0]

    def simulate(self, requested, packages, kept):
        from package_cleaner.core.removal import simulate
        commands = [['autoremove'],
                    ['-o', 'APT::Get::AutomaticRemove=false', 'remove', '--', *requested],
                    ['--autoremove', 'remove', '--', *requested]]
        logger.info('Read-only validation commands: ' + json.dumps([
            ['apt-get', '--simulate', '-o', 'Debug::NoLocking=1', *args] for args in commands]))
        plan = simulate(requested, packages, kept)
        logger.info('Final validation command: ' + json.dumps(['apt-get', '--simulate', '-o',
              'Debug::NoLocking=1', *plan.transaction_arguments]))
        return plan

    def prepare(self, targets):
        import apt
        pkg = self.apt_pkg
        config = pkg.config
        # Unsupported configuration cannot be silently normalized at execution.
        for key in ('Debug::NoLocking', 'APT::Get::Assume-Yes', 'APT::Get::Force-Yes', 'APT::Get::Purge'):
            if config.find_b(key, False):
                raise ValueError('Unsafe or unsupported APT setting: ' + key)
        if (config.find_file('Dir::State::status') != '/var/lib/dpkg/status'
                or config.find_file('Dir::Bin::dpkg') != '/usr/bin/dpkg'
                or config.find('Dir') != '/'):
            raise ValueError('Custom package roots or dpkg executables are not supported.')
        for key in config.keys():
            if key.lower().startswith('binary::apt-get::') and config.find(key):
                raise ValueError('apt-get-specific configuration requires manual administrator review.')
            if key.lower().startswith('dpkg::options') and config.find(key):
                raise ValueError('Custom dpkg options are not supported for validated removal.')
        config.set('APT::Get::AutomaticRemove', 'false')
        self.cache = apt.Cache()
        if self.cache.broken_count:
            raise ValueError('Existing broken dependencies require administrator review first.')
        for p in self.cache:
            if p._pkg.current_state not in (pkg.CURSTATE_NOT_INSTALLED, pkg.CURSTATE_CONFIG_FILES, pkg.CURSTATE_INSTALLED):
                raise ValueError('Pending or incomplete package operations must be resolved first: ' + p.fullname)
        for name in targets:
            p = self.cache[name]
            if not p.is_installed or p._pkg.selected_state == pkg.SELSTATE_HOLD:
                raise ValueError('Selected package is missing or held: ' + name)
            p.mark_delete(auto_fix=False, purge=False)
        validate_changes(self.cache, targets)
        self.manager = pkg.PackageManager(self.cache._depcache)
        # Initialize the package manager's archive/order state, but never fetch.
        self.fetcher = pkg.Acquire()
        sources = pkg.SourceList()
        sources.read_main_list()
        if not self.manager.get_archives(self.fetcher, sources, pkg.PackageRecords(self.cache._cache)):
            raise ValueError('Could not prepare the exact APT transaction.')
        if self.fetcher.items:
            raise ValueError('Unexpected archive/download action in removal-only transaction.')
        validate_changes(self.cache, targets)
        self.targets = list(targets)

    def validated(self, plan):
        logger.info("Locked validation passed. Exact ordinary removals: " + json.dumps(plan.removed))
        logger.info("Execution API: apt.Cache.install_archives / apt_pkg.PackageManager; purge=False, auto_fix=False, global autoremove disabled; no downloads or automatic retry.")
        logger.info("Validated APT simulation arguments: " + json.dumps(plan.transaction_arguments))

    def execute(self):
        if not self.apt_pkg.pkgsystem_is_locked():
            raise ValueError("APT system lock is not held; refusing execution.")
        validate_changes(self.cache, self.targets)
        import apt.progress.base
        # One attempt only: unlike Cache.commit(), do not retry incomplete results.
        # install_archives preserves the frontend lock while dpkg takes its inner lock.
        import signal
        class WaitingProgress(apt.progress.base.InstallProgress):
            def wait_child(self):
                status = super().wait_child()
                return (1 << 8) if os.WIFSIGNALED(status) else status

            def fork(self):
                pid = super().fork()
                if pid == 0:
                    signal.signal(signal.SIGINT, signal.SIG_DFL)
                return pid
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            status = self.cache.install_archives(self.manager, WaitingProgress())
        finally:
            signal.signal(signal.SIGINT, previous)
        return 0 if status == self.manager.RESULT_COMPLETED else int(status) or 1

    def observed(self, names):
        import apt
        cache = apt.Cache()
        states = {self.apt_pkg.CURSTATE_NOT_INSTALLED: 'not-installed',
                  self.apt_pkg.CURSTATE_CONFIG_FILES: 'config-files',
                  self.apt_pkg.CURSTATE_INSTALLED: 'installed'}
        return {n: states.get(cache[n]._pkg.current_state, 'incomplete') if n in cache else 'not-installed'
                for n in names}


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    if os.geteuid() != 0:
        raise PermissionError('Administrator privileges are required for execution.')
    if len(sys.argv) != 2:
        raise ValueError('An approved transaction file is required.')
    fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        stat = os.fstat(stream.fileno())
        if stat.st_mode & 0o077:
            raise ValueError('Approval file permissions are too broad.')
        with gzip.GzipFile(fileobj=stream) as packed:
            record = json.load(packed)
        if record.get('version') != 1 or stat.st_uid != record.get('uid') or int(os.environ.get('SUDO_UID', record['uid'])) != record['uid']:
            raise ValueError('Invalid approval file ownership or format.')
    result = run_validated(record, AptBackend())
    send_result(record, result)
    return 0 if result['status'] == 'completed' else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (Exception, KeyboardInterrupt) as exc:
        logger.error('Helper stopped: ' + (str(exc) or 'Interrupted; reconciliation required.'))
        sys.exit(1)
