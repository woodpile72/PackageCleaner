# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Explicit authorization boundary shared by any frontend; never prompts or prints."""
from dataclasses import dataclass
from collections.abc import Mapping
from . import execution
from .operations import AuthorizationError, checkpoint, domain_operation


@dataclass(frozen=True)
class ExecutionResult(Mapping):
    data: dict

    def __getitem__(self, key): return self.data[key]
    def __iter__(self): return iter(self.data)
    def __len__(self): return len(self.data)

    @property
    def completed(self):
        return (self.data.get('status') == 'completed' and self.data.get('helper_verified') is True
                and (self.data.get('provider') != 'flatpak' or
                     (self.data.get('final_state_verified') is True and self.data.get('api_success') is True
                      and self.data.get('boundaries') == ['pre-auth','ready']
                      and not self.data.get('remaining') and not self.data.get('error'))))


@domain_operation
def prepare_authorization(plan, packages, preferences):
    checkpoint('authorization')
    if plan.provider_plans:
        raise AuthorizationError('Approve each provider/installation transaction separately.')
    if plan.provider == 'flatpak':
        from .flatpak_execution import approval_record
        return approval_record(plan, packages, preferences)
    return execution.approval_record(plan, packages, preferences)


def is_confirmation(value):
    return value == 'REMOVE'


@domain_operation
def execute_confirmed(record, confirmation, *, enabled=False, graphical=False):
    if not enabled:
        raise AuthorizationError('Execution is not enabled for this session.')
    if not is_confirmation(confirmation):
        return None
    checkpoint('execution')
    # From this boundary forward cancellation cannot abandon/kill the privileged
    # helper. Its result must be awaited and reconciled, including after SIGINT.
    if record.get('provider') == 'flatpak':
        from .flatpak_provider import FlatpakProvider
        result = FlatpakProvider().execute(record, enabled=enabled, confirmation=confirmation)
        if (not isinstance(result, dict) or result.get('status') not in ('completed','partial','cancelled','failed','unknown')
            or not isinstance(result.get('removed'), list) or result.get('provider') != 'flatpak'
            or (result.get('status') == 'completed' and not ExecutionResult(result).completed)):
            result = dict(status='unknown',helper_verified=False,removed=[],remaining=[],unknown=record['selected'],error='Missing or malformed execution result; reconciliation required.',log_directory='unavailable')
        return ExecutionResult(result)
    return ExecutionResult(execution.launch(record, graphical=True) if graphical else execution.launch(record))


def execution_parts(plan):
    """Explicit independently approved steps. Never launch a second provider here."""
    if plan.blocked:
        raise AuthorizationError('Removal blocked: ' + '; '.join(plan.blocked))
    if plan.execution_disabled:
        raise AuthorizationError('Execution is unavailable for this transaction. ' + ' '.join(plan.observations))
    return tuple(plan.provider_plans or [plan])


@domain_operation
def refresh_execution_part(plan, preferences):
    """Prepare a separate fresh preview before its own visible confirmation."""
    from .providers import inventory, preview
    packages, warnings = inventory()
    if warnings:
        raise AuthorizationError('Inventory is incomplete; refresh the review before execution.')
    preferences.reload()
    fresh = preview(plan.requested, packages, preferences.kept)
    parts = execution_parts(fresh)
    if len(parts) != 1 or parts[0].provider != plan.provider or parts[0].installation != plan.installation:
        raise AuthorizationError('Transaction target changed. Return to review.')
    return parts[0], packages
