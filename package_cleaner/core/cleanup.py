# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Frontend-independent, single-use cleanup approvals and queue reconciliation."""
import copy
import gzip
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from . import authorization
from .operations import CleanerError, OperationCancelled, checkpoint, operation
from .result_protocol import digest, encoded
from .execution import dpkg_state, private_write
from .flatpak_execution import state as flatpak_state
from .providers import subset


@dataclass(frozen=True)
class Proposal:
    token: str
    plan: object
    packages: dict
    index: int
    total: int
    changed: bool = False


@dataclass(frozen=True)
class CleanupOutcome:
    result: authorization.ExecutionResult
    plan: object
    packages: dict
    remaining_queue: tuple[str, ...]
    more: bool


def signature(plan):
    from dataclasses import asdict
    value=asdict(plan)
    # Diagnostic prose and resolver output are not transaction identities.
    for key in ('output','retained','observations'):
        value.pop(key,None)
    return digest(value)


def audit_state(packages):
    path=Path('/var/lib/apt/extended_states')
    return dict(apt=dpkg_state(),apt_flags=digest(path.read_bytes().hex() if path.exists() else ''),
                flatpak=flatpak_state(subset(packages,'flatpak')))


class CleanupSession:
    """No UI, no implicit execution. One explicit approval per independent part."""
    def __init__(self, service, *, graphical=False):
        self.service=service
        self.graphical=graphical
        self.parts=()
        self.index=0
        self.pending=None
        self.pending_signature=None
        self.stopped=False
        self.lock=threading.Lock()

    def begin(self):
        if self.lock.locked():
            raise CleanerError('A removal is already in progress.')
        preview=self.service.validate()
        if preview.warnings and any(w not in preview.plan.observations for w in preview.warnings):
            raise CleanerError('Inventory is incomplete. Review the scan details before removal.')
        self.parts=tuple(copy.deepcopy(p) for p in authorization.execution_parts(preview.plan))
        self.index=0;self.stopped=False;self.pending=None
        return self.propose()

    def propose(self):
        if self.lock.locked():
            raise CleanerError('A removal is already in progress.')
        if self.stopped or self.index>=len(self.parts):
            raise CleanerError('This cleanup session is finished. Start a fresh review.')
        checkpoint('validation')
        part,packages=authorization.refresh_execution_part(self.parts[self.index],self.service.preferences)
        self.service.packages=packages
        proposal=Proposal(secrets.token_hex(24),copy.deepcopy(part),packages,self.index+1,len(self.parts))
        self.pending=proposal;self.pending_signature=signature(part)
        return proposal

    def cancel(self):
        self.pending=None;self.stopped=True;self.service.preview=None

    def confirm(self, token, *, accepted=False):
        if not accepted:
            self.cancel();return None
        if not self.lock.acquire(blocking=False):
            raise CleanerError('A removal is already in progress.')
        try:
            proposal=self.pending
            if self.stopped or proposal is None or proposal.token!=token or signature(proposal.plan)!=self.pending_signature:
                raise CleanerError('This approval is stale or already used. Review again.')
            self.pending=None  # Consume before any blocking call; cannot execute twice.
            if not set(proposal.plan.requested)<=self.service.selected:
                raise CleanerError('The removal queue changed. Review it again.')
            checkpoint('validation')
            fresh,packages=authorization.refresh_execution_part(proposal.plan,self.service.preferences)
            if signature(fresh)!=self.pending_signature:
                updated=Proposal(secrets.token_hex(24),copy.deepcopy(fresh),packages,proposal.index,proposal.total,True)
                self.pending=updated;self.pending_signature=signature(fresh)
                return updated
            record=authorization.prepare_authorization(fresh,packages,self.service.preferences)
            before=audit_state(packages)
            checkpoint('executing_'+fresh.provider)
            try:
                result=authorization.execute_confirmed(record,'REMOVE',enabled=True,graphical=self.graphical)
                if (not isinstance(result,authorization.ExecutionResult) or
                    result.get('status') not in ('completed','partial','cancelled','failed','rejected','unknown') or
                    not isinstance(result.get('removed'),list) or
                    not set(result.get('removed',[])) <= set(fresh.removed) or
                    (result.get('status')=='completed' and (not result.completed or set(result['removed'])!=set(fresh.removed)))):
                    raise CleanerError('Missing execution result; reconciliation required.')
            except Exception as exc:
                result=authorization.ExecutionResult(dict(status='unknown',helper_verified=False,removed=[],
                    remaining=fresh.requested,error=str(exc),log_directory='unavailable'))
            # Never abandon independent verification or queue bookkeeping when
            # a cancellation token has been set during an actual transaction.
            with operation():
                data=dict(result);after=None;warnings=[]
                try:
                    self.service.packages,warnings=self.service.inventory_provider()
                    after=audit_state(self.service.packages)
                    if warnings: raise CleanerError('Final inventory is incomplete: '+'; '.join(warnings))
                    unaffected=('apt','apt_flags') if fresh.provider=='flatpak' else ('flatpak',)
                    if any(before[k]!=after[k] for k in unaffected):
                        raise CleanerError('The other package provider changed during removal; reconciliation required.')
                    if data.get('helper_verified') and any(n in self.service.packages for n in data.get('removed',[])):
                        raise CleanerError('A reported removal is still installed; reconciliation required.')
                except Exception as exc:
                    data.update(status='unknown',helper_verified=False,error=str(exc),reconciliation_required=True)
                directory=Path(data.get('log_directory','unavailable'))
                if directory.is_dir() and data.get('log_directory')!='unavailable':
                    try:
                        evidence=dict(before=before,after=after,provider_result=dict(result),warnings=warnings)
                        private_write(directory/'cross-provider.json.gz',gzip.compress(encoded(evidence),mtime=0))
                        data['cross_provider_digest']=digest(evidence)
                        private_write(directory/'cleanup-result.json',encoded(data)+b'\n')
                    except OSError as exc:
                        data.update(status='unknown',helper_verified=False,error='Could not save final audit evidence: '+str(exc))
                result=authorization.ExecutionResult(data)
                if data.get('helper_verified') is True:
                    self.service.reconcile_removed(data.get('removed',[]))
                self.service.preview=None
            self.index+=1
            self.stopped=self.stopped or not result.completed or self.index>=len(self.parts)
            return CleanupOutcome(result,fresh,packages,tuple(sorted(self.service.selected)),not self.stopped)
        except BaseException:
            if self.pending is None:self.stopped=True
            self.service.preview=None
            raise
        finally:
            self.lock.release()
