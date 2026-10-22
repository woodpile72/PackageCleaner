# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Optional libflatpak provider. Discovery is local; execution requires core authorization.

Preview aborts unconditionally at BOTH ready-pre-auth and ready. Callbacks never
return true, even when inspection/cancellation fails. No UI or CLI imports.
"""
import configparser
import hashlib
from urllib.parse import quote
from .inventory import Package
from .operations import CleanerError, OperationCancelled, checkpoint
from .removal import Plan, Baseline


def load_api():
    try:
        import gi
        gi.require_version('Flatpak', '1.0')
        from gi.repository import Flatpak, Gio, GLib, GObject
        return Flatpak, Gio, GLib, GObject
    except (ImportError, ValueError):
        return None


def execution_supported(api):
    """Register lazy GObject class signals before checking their availability."""
    F, Gio, GLib, GObject = api
    initialize = getattr(GObject, 'type_class_ref', None)
    if initialize is not None:
        initialize(F.Transaction)
    return all(GObject.signal_lookup(name, F.Transaction) for name in
               ('ready-pre-auth', 'ready', 'new-operation', 'operation-done', 'operation-error'))


def installation_id(inst):
    # A configured installation moving to a different path is a new target.
    prefix = 'user' if inst.get_is_user() else ('system/' + (inst.get_id() or 'default'))
    return prefix + '@' + quote(inst.get_path().get_path(), safe='')


def identity(installation, ref):
    return 'flatpak:' + installation + '/' + ref


def fingerprint(packages):
    records = sorted((n,p.commit,p.runtime,p.size,p.orphan,tuple(p.protected),tuple(p.dependencies)) for n,p in packages.items())
    return hashlib.sha256(repr(records).encode()).hexdigest()


def running_applications():
    # libflatpak 1.14's Instance.get_all GI ownership annotations are unsafe on
    # this system. Read the documented instance metadata instead; do not execute apps.
    import os
    from pathlib import Path
    from .inspection import read_inspection_text
    result = set()
    for path in (Path('/run/user') / str(os.getuid()) / '.flatpak').glob('[0-9]*/info'):
        try:
            pid = int(read_inspection_text(path.with_name('pid')).strip())
            if not Path(f'/proc/{pid}').exists():
                continue
            metadata = configparser.ConfigParser(interpolation=None, strict=False)
            metadata.read_string(read_inspection_text(path))
            result.add(metadata.get('Application','name'))
        except (OSError, ValueError, configparser.Error):
            continue
    return result


class FlatpakProvider:
    provider = 'flatpak'

    def __init__(self, api=None):
        self.api = api if api is not None else load_api()
        self.installations = {}

    def discover(self):
        if not self.api:
            import shutil
            return {}, (['Flatpak is installed but its Python API is unavailable; Flatpak discovery is unavailable.'] if shutil.which('flatpak') else [])
        F, Gio, GLib, GObject = self.api
        packages, warnings, unused, required, parents = {}, [], set(), set(), {}
        try:
            installations = [F.Installation.new_user(None), *F.get_system_installations(None)]
        except GLib.Error as exc:
            return {}, [f'Flatpak installations could not be inspected: {exc}']
        running = running_applications()
        for inst in installations:
            checkpoint('flatpak_inventory')
            target = installation_id(inst)
            self.installations[target] = inst
            try:
                refs = inst.list_installed_refs(None)
                unused_refs = {r.format_ref() for r in inst.list_unused_refs(None,None)}
                for r in refs:
                    checkpoint('flatpak_metadata')
                    ref = r.format_ref()
                    is_app = r.get_kind() == F.RefKind.APP
                    metadata = configparser.ConfigParser(interpolation=None, strict=False)
                    try:
                        metadata.read_string(r.load_metadata(None).get_data().decode('utf-8'))
                    except (GLib.Error, UnicodeError, configparser.Error) as exc:
                        warnings.append(f'Flatpak dependency information unavailable for {ref}: {exc}')
                    group = 'Application' if is_app else 'Runtime'
                    runtime = metadata.get(group,'runtime',fallback='')
                    sdk = metadata.get(group,'sdk',fallback='')
                    name = identity(target, ref)
                    p = Package(name,r.get_name(),r.get_appdata_version() or '',not is_app,
                                r.get_installed_size(),r.get_appdata_summary() or '',
                                'flatpak-app' if is_app else 'flatpak-component',
                                application=is_app, desktop_application=is_app,
                                display_name=r.get_appdata_name() or r.get_name(),provider='flatpak',
                                installation=target,ref=ref,runtime=runtime,commit=r.get_commit() or '',
                                running=r.get_name() in running)
                    packages[name] = p
                    if ref in unused_refs and not is_app:
                        unused.add(name)
                    else:
                        required.update('runtime/'+x for x in (runtime,sdk) if x)
                    parent = metadata.get('ExtensionOf','ref',fallback='')
                    if parent:
                        parents[name] = parent
            except GLib.Error as exc:
                warnings.append(f'Cannot inspect Flatpak installation {target}: {exc}')
        # Cross-installation dependencies can be satisfied from system installs.
        # Conservative identity-independent matching protects every matching ref.
        changed = True
        while changed:
            old = set(required)
            for n,p in packages.items():
                if p.ref in required or n not in unused or parents.get(n) in required:
                    required.add(p.ref)
                    if p.runtime:
                        required.add('runtime/'+p.runtime)
            changed = required != old
        incomplete = bool(warnings)
        for n,p in packages.items():
            if not p.application:
                p.orphan = n in unused and p.ref not in required and not incomplete
                if not p.orphan:
                    p.protected = ['Flatpak component is required, pinned, or its unused status could not be verified.']
            if p.runtime:
                p.dependencies = [q.name for q in packages.values() if q.ref == 'runtime/'+p.runtime]
        return packages, warnings

    def preview(self, names, packages, kept=()):
        current, warnings = self.discover()
        if warnings or fingerprint(current) != fingerprint(packages):
            raise CleanerError('Flatpak state changed or is incomplete. Refresh discovery before reviewing removal.')
        F, Gio, GLib, GObject = self.api
        supported = execution_supported(self.api)
        plans = []
        for target in sorted({packages[n].installation for n in names}):
            checkpoint('flatpak_preview')
            selected = sorted(n for n in names if packages[n].installation == target)
            blocked = [f'{n}: '+(', '.join(packages[n].protected) or 'saved Keep preference')
                       for n in selected if packages[n].protected or n in kept]
            plan = Plan(selected, [], [], [], [], [], [],
                        {'selected':0,'newly_orphaned':0,'already_orphaned':0,'dependent_removals':0},
                        blocked,'',Baseline(sorted(n for n,p in current.items() if p.installation == target and p.orphan),''),provider='flatpak',installation=target,
                        execution_disabled=not supported,state_token=fingerprint(current))
            if not supported:
                plan.observations.append('Execution unavailable: this libflatpak lacks required validation signals.')
            if not blocked:
                operations = self._resolve(self.installations[target], [packages[n].ref for n in selected], packages=current)
                plan.operations = self.resolved_records
                expected = {('uninstall',packages[n].ref) for n in selected}
                if len(operations) != len(expected) or set(operations) != expected:
                    plan.blocked.append('Flatpak proposed operations outside the explicitly selected removal set.')
                else:
                    plan.removed = selected
                    plan.baseline_selected_bytes = sum(packages[n].size for n in selected if packages[n].orphan)
                    plan.savings['selected'] = sum(packages[n].size for n in selected)
                plan.output = repr(operations)
                if not plan.blocked:
                    discovered = self._resolve(self.installations[target], [packages[n].ref for n in selected], include_unused=True)
                    by_ref = {p.ref:n for n,p in current.items() if p.installation == target}
                    extras = [(kind,ref) for kind,ref in discovered if (kind,ref) not in expected]
                    for kind,ref in extras:
                        n = by_ref.get(ref)
                        if kind != 'uninstall' or not n or current[n].application:
                            plan.blocked.append('Flatpak unused-component discovery proposed an unexpected operation.')
                        elif not current[n].orphan:
                            plan.newly_unneeded_retained.append(n)
                    if plan.newly_unneeded_retained:
                        plan.observations.append(f'{len(plan.newly_unneeded_retained)} supporting components may become unused; they remain installed in this explicit preview.')
            if any(packages[n].running for n in selected):
                plan.blocked.append('A selected Flatpak application is currently running. Close it before reviewing removal.')
            plan.observations.append('System authorization may be required.' if not self.installations[target].get_is_user() else 'User installation; no administrator elevation is requested.')
            plan.observations.extend(['Application data will be retained.',
                'Installed bytes are an upper bound, not exact freed space: Flatpak shares storage objects.',
                'Existing unused components and runtimes are retained unless separately selected.'])
            # No runtime pruning inferred from package sizes or our dependency graph.
            plan.retained = [{'name':n,'reason':'Retained; not explicitly selected.'} for n,p in current.items()
                             if not p.application and n not in selected]
            plans.append(plan)
        after, warnings = self.discover()
        if warnings or fingerprint(after) != fingerprint(current):
            raise CleanerError('Flatpak state changed during preview. Refresh and validate again.')
        return plans

    def _resolve(self, installation, refs, *, include_unused=False, packages=None):
        F, Gio, GLib, GObject = self.api
        cancel = Gio.Cancellable()
        transaction = F.Transaction.new_for_installation(installation, cancel)
        if not GObject.signal_lookup('ready-pre-auth', F.Transaction):
            raise CleanerError('This Flatpak API lacks a verified pre-execution preview boundary. Removal is disabled.')
        self.resolved_records = []
        transaction.set_force_uninstall(False)
        transaction.set_no_interaction(True)
        transaction.set_no_pull(True)
        transaction.set_disable_dependencies(True)
        transaction.set_disable_related(True)
        transaction.set_include_unused_uninstall_ops(include_unused)
        operations, errors, reached = [], [], []
        def stop(tx):
            reached.append(True)
            try:
                checkpoint('flatpak_preview_resolved')
                from .flatpak_execution import operation_records
                # Capture complete canonical records for the exact preview.
                if not include_unused:
                    current = packages if packages is not None else self.discover()[0]
                    try:
                        self.resolved_records = operation_records(tx, installation_id(installation), current, F)
                    except CleanerError:
                        self.resolved_records = []
                for op in tx.get_operations():
                    kind = 'uninstall' if op.get_operation_type() == F.TransactionOperationType.UNINSTALL else 'unexpected'
                    operations.append((kind,op.get_ref()))
            except BaseException as exc:
                errors.append(exc)
            finally:
                cancel.cancel()
            return False  # Never authorize execution, including callback errors.
        transaction.connect('ready-pre-auth', stop)
        transaction.connect('ready', lambda *_: False)
        transaction.connect('add-new-remote', lambda *_: False)
        transaction.connect('choose-remote-for-ref', lambda *_: -1)
        for ref in refs:
            checkpoint('flatpak_preview')
            transaction.add_uninstall(ref)
        try:
            transaction.run(cancel)
        except GLib.Error as exc:
            if not reached:
                raise CleanerError(f'Flatpak preview could not be resolved: {exc}') from exc
        if errors:
            raise errors[0]
        if not reached:
            raise CleanerError('Flatpak did not provide a verified pre-execution operation list.')
        return operations

    def execute(self, record, *, enabled=False, confirmation='', **kwargs):
        if not enabled or confirmation != 'REMOVE':
            raise CleanerError('Flatpak execution is disabled without opt-in and exact confirmation.')
        from .flatpak_execution import execute
        return execute(record, provider=self, **kwargs)
