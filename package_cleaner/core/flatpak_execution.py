# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Explicit libflatpak execution. No sudo, CLI, GTK, or output protocol involved.

The same transaction is inspected at both authorization boundaries. State is
independently read after run(), even on failure; API success alone is insufficient.
"""
import gzip
import copy
import os
import signal
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from .operations import CleanerError, OperationCancelled, checkpoint, current_cancellation
from .result_protocol import encoded, digest
from .execution import preference_state, private_write
from .preferences import xdg_directory


def canonical(target, ref, commit, *, requires_authentication=False):
    if not isinstance(commit, str) or not commit:
        raise CleanerError('Flatpak installed commit is unavailable; execution cannot be verified.')
    pieces = ref.split('/')
    if len(pieces) != 4 or pieces[0] not in ('app', 'runtime') or not all(pieces):
        raise CleanerError('Invalid complete Flatpak ref.')
    return dict(provider='flatpak', installation=target, operation='uninstall',
                ref=ref, kind=pieces[0], architecture=pieces[2], branch=pieces[3],
                commit=commit, requires_authentication=bool(requires_authentication))


def state(packages):
    # Policy flags such as orphan status can legitimately change after uninstall.
    # Compare installed content and dependency identity, not those derived flags.
    return {n: dict(installation=p.installation, ref=p.ref, commit=p.commit,
                    runtime=p.runtime, version=p.version) for n,p in sorted(packages.items())}


def operation_records(tx, target, packages, F):
    by_ref = {p.ref:p for p in packages.values() if p.installation == target}
    records = []
    for op in tx.get_operations():
        if op.get_operation_type() != F.TransactionOperationType.UNINSTALL:
            raise CleanerError('Flatpak proposed an unsupported install, update or other operation.')
        p = by_ref.get(op.get_ref())
        if p is None:
            raise CleanerError('Flatpak proposed an unknown removal.')
        records.append(canonical(target, p.ref, p.commit,
                       requires_authentication=op.get_requires_authentication()))
    return sorted(records, key=lambda r:(r['installation'],r['ref']))


def approval_record(plan, packages, preferences):
    from .flatpak_provider import fingerprint
    from .providers import subset, effective_kept
    ps = subset(packages, 'flatpak')
    if plan.provider != 'flatpak' or plan.provider_plans or plan.execution_disabled or plan.blocked or not plan.removed:
        raise CleanerError('Only an individual validated Flatpak transaction can be authorized.')
    if sorted(plan.requested) != sorted(plan.removed) or len(set(plan.requested)) != len(plan.requested):
        raise CleanerError('Flatpak removals must be individually selected.')
    if fingerprint(ps) != plan.state_token or not plan.operations:
        raise CleanerError('Flatpak preview is stale or missing complete operations.')
    prefs = preference_state(preferences.path)
    kept = effective_kept(ps,prefs['kept'])
    for n in plan.removed:
        p=ps[n]
        if p.protected or n in kept or p.running:
            raise CleanerError('A selected Flatpak is protected, kept or running. Review again.')
    expected = [canonical(plan.installation,ps[n].ref,ps[n].commit) for n in sorted(plan.removed)]
    # Authentication is informational and must have been captured from libflatpak.
    def identities(rows): return [{k:v for k,v in r.items() if k!='requires_authentication'} for r in rows]
    if identities(sorted(expected,key=lambda r:r['ref'])) != identities(plan.operations):
        raise CleanerError('Preview operation identities do not match the exact selection.')
    return dict(version=1, provider='flatpak', uid=os.getuid(), installation=plan.installation,
                selected=sorted(plan.removed), operations=copy.deepcopy(plan.operations),
                state_token=plan.state_token, inventory=state(ps),
                preferences_path=str(preferences.path.absolute()), preferences=prefs,
                approved_at=datetime.now(timezone.utc).isoformat(), data_retained=True)


def reconcile(before, after, selected, events, api_success, boundaries, errors, cancelled):
    expected=set(selected); removed=set(before)-set(after)
    unexpected=sorted(removed-expected)
    changed=sorted(n for n in after if n not in before or after[n]!=before[n])
    observed=sorted(removed & expected); remaining=sorted(expected & set(after))
    done={e['package'] for e in events if e['event']=='completed'}
    started={e['package'] for e in events if e['event']=='started'}
    consistent=not unexpected and not changed and done<=set(observed) and set(observed)<=started
    verified=consistent
    if not consistent:
        status='unknown'
    elif not remaining and api_success is True and boundaries==['pre-auth','ready'] and done==expected and not errors:
        status='completed'
    elif observed:
        status='partial'
    elif cancelled:
        status='cancelled'
    else:
        status='failed'
    return dict(status=status, helper_verified=verified, final_state_verified=verified,
                removed=observed, remaining=remaining, unexpected_removed=unexpected,
                unexpected_changes=changed, unknown=sorted(expected) if not verified else [],
                data_retained=True)


def execute(record, *, provider=None, log_directory=None):
    from .flatpak_provider import FlatpakProvider, fingerprint, execution_supported
    from .providers import effective_kept
    provider = provider or FlatpakProvider()
    if not isinstance(record,dict) or record.get('provider')!='flatpak' or record.get('uid')!=os.getuid():
        raise CleanerError('Invalid Flatpak execution authorization.')
    if record.get('data_retained') is not True or not record.get('selected') or not record.get('operations'):
        raise CleanerError('Incomplete Flatpak approval.')
    from .flatpak_provider import identity
    rows = record['operations']
    if (len(rows) != len(set(record['selected'])) or
        sorted(identity(r['installation'],r['ref']) for r in rows) != sorted(record['selected']) or
        any(r != canonical(record['installation'],r['ref'],r['commit'], requires_authentication=r['requires_authentication']) for r in rows)):
        raise CleanerError('Flatpak approval does not describe exactly the selected removal operations.')
    directory=Path(log_directory) if log_directory else xdg_directory('XDG_STATE_HOME','.local/state')/'package-cleaner'
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    if directory.is_symlink() or directory.stat().st_uid!=os.getuid():
        raise CleanerError('Execution log directory must belong to you.')
    attempt=Path(tempfile.mkdtemp(prefix='flatpak-',dir=directory))
    private_write(attempt/'approval.json.gz',gzip.compress(encoded(record),mtime=0))
    result=dict(status='unknown',helper_verified=False,removed=[],remaining=record['selected'],unknown=record['selected'],data_retained=True)
    private_write(attempt/'result.json',encoded(result)+b'\n')
    private_write(attempt/'execution.log',(record['approved_at']+'\nFlatpak removal: '+record['installation']+'\nApproved: '+', '.join(record['selected'])+'\n').encode())
    def journal(message):
        with (attempt/'execution.log').open('a') as stream:
            stream.write(message+'\n'); stream.flush(); os.fsync(stream.fileno())
    events=[]; errors=[]; boundaries=[]; validated=[]; before=None; after=None
    api_success=False; was_cancelled=False; cancel=None; stopped=threading.Event(); watcher=None; old_handler=None
    token=current_cancellation()
    def interrupted(*_):
        nonlocal was_cancelled
        was_cancelled=True
        if cancel: cancel.cancel()
    def check():
        if was_cancelled: raise OperationCancelled('Flatpak execution cancellation requested.')
        checkpoint('flatpak_execution')
    def fresh():
        ps,warnings=provider.discover()
        if warnings: raise CleanerError('Flatpak inventory is incomplete: '+'; '.join(warnings))
        return ps
    try:
        check()
        ps=fresh(); before=state(ps)
        if before!=record['inventory'] or fingerprint(ps)!=record['state_token']:
            raise CleanerError('Flatpak state changed after approval. A new preview is required.')
        if preference_state(record['preferences_path'])!=record['preferences']:
            raise CleanerError('Keep preferences changed. A new preview is required.')
        kept=effective_kept(ps,record['preferences']['kept'])
        for n in record['selected']:
            if n not in ps or ps[n].protected or n in kept or ps[n].running or ps[n].installation!=record['installation']:
                raise CleanerError('A selected Flatpak is unavailable, protected, kept, running or in a different installation.')
        target=record['installation']; inst=provider.installations[target]
        F,Gio,GLib,GObject=provider.api
        if not execution_supported(provider.api):
            raise CleanerError('Flatpak API lacks required execution validation signals.')
        cancel=Gio.Cancellable()
        tx=F.Transaction.new_for_installation(inst,cancel)
        tx.set_no_interaction(False)  # Supported Flatpak/polkit session authorization.
        tx.set_no_pull(True); tx.set_disable_dependencies(True); tx.set_disable_related(True)
        tx.set_include_unused_uninstall_ops(False); tx.set_force_uninstall(False)
        # No application-data deletion API is called. No global unused pruning.
        def gate(transaction, phase):
            try:
                check()
                current=fresh()
                if fingerprint(current)!=record['state_token'] or state(current)!=record['inventory']:
                    raise CleanerError('Flatpak state changed during authorization; review again.')
                if any(current[n].running for n in record['selected']):
                    raise CleanerError('A selected application is running. Close it and review again.')
                if preference_state(record['preferences_path'])!=record['preferences']:
                    raise CleanerError('Keep preferences changed during authorization.')
                rows=operation_records(transaction,target,current,F)
                if rows!=record['operations']:
                    raise CleanerError('Resolved operations changed after approval; a new preview is required.')
                if phase=='ready' and boundaries!=['pre-auth']:
                    raise CleanerError('Missing pre-authorization validation.')
                validated.append(dict(phase=phase,operations=rows)); boundaries.append(phase)
                journal('Validation passed: '+phase+'; operation digest '+digest(rows))
                return True
            except BaseException as exc:
                if isinstance(exc,(OperationCancelled,KeyboardInterrupt)): interrupted()
                errors.append(str(exc)); cancel.cancel(); return False
        def event(kind,op,error=None):
            try:
                from .flatpak_provider import identity
                n=identity(target,op.get_ref())
                if n not in record['selected'] or op.get_operation_type()!=F.TransactionOperationType.UNINSTALL or boundaries!=['pre-auth','ready']:
                    raise CleanerError('Unexpected Flatpak execution notification.')
                events.append(dict(event=kind,package=n,error=str(error) if error else None))
                journal(kind+': '+n+(' — '+str(error)[:500] if error else ''))
                if error: errors.append(str(error))
                check()
            except BaseException as exc:
                if isinstance(exc,(OperationCancelled,KeyboardInterrupt)): interrupted()
                errors.append(str(exc)); cancel.cancel()
        tx.connect('ready-pre-auth',lambda t:gate(t,'pre-auth'))
        tx.connect('ready',lambda t:gate(t,'ready'))
        tx.connect('new-operation',lambda t,o,p:event('started',o))
        tx.connect('operation-done',lambda t,o,c,r:event('completed',o))
        def failed(t,o,e,d): event('failed',o,e); return False
        tx.connect('operation-error',failed)
        tx.connect('add-new-remote',lambda *_:False)
        tx.connect('choose-remote-for-ref',lambda *_:-1)
        tx.connect('end-of-lifed-with-rebase',lambda *_:False)
        tx.connect('install-authenticator',lambda *_:cancel.cancel())
        for n in record['selected']: tx.add_uninstall(ps[n].ref)
        if threading.current_thread() is threading.main_thread():
            old_handler=signal.signal(signal.SIGINT,interrupted)
        if token:
            def watch():
                while not stopped.wait(.1):
                    try: token.check()
                    except OperationCancelled: interrupted(); return
            watcher=threading.Thread(target=watch,daemon=True);watcher.start()
        api_success=tx.run(cancel)
    except BaseException as exc:
        if isinstance(exc,(KeyboardInterrupt,OperationCancelled)): was_cancelled=True
        elif provider.api and isinstance(exc,provider.api[2].Error):
            Gio=provider.api[1]
            if hasattr(exc,'matches') and hasattr(Gio,'io_error_quark'):
                was_cancelled=was_cancelled or exc.matches(Gio.io_error_quark(),Gio.IOErrorEnum.CANCELLED)
        errors.append(str(exc) or type(exc).__name__)
    finally:
        stopped.set()
        if watcher: watcher.join()
        if old_handler is not None: signal.signal(signal.SIGINT,old_handler)
    # Cancellation must not suppress reconciliation. A fresh context clears the
    # cancelled token while reading state, without resuming any transaction.
    from .operations import operation
    try:
        with operation(): after=state(fresh())
        if before is None: before=record['inventory']
        result=reconcile(before,after,record['selected'],events,api_success,boundaries,errors,was_cancelled)
    except BaseException as exc:
        errors.append('Final-state verification failed: '+str(exc))
    result.update(provider='flatpak',log_directory=str(attempt),api_success=api_success is True,
                  boundaries=boundaries,events=events,cancelled=was_cancelled,
                  approval_digest=digest(record),error='; '.join(errors)[:4000] if errors else None)
    evidence=dict(before_digest=digest(before),after=after,validated=validated,errors=errors)
    private_write(attempt/'evidence.json.gz',gzip.compress(encoded(evidence),mtime=0))
    result['evidence_digest']=digest(evidence)
    journal('Result: '+result['status'])
    journal('Application data retained; actual recovered space not measured.')
    if errors: journal('Error: '+result['error'])
    private_write(attempt/'result.complete.json',encoded(result)+b'\n')
    os.replace(attempt/'result.complete.json',attempt/'result.json')
    return result
