# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""UI-independent API: review, selections, authorization and operation control.

No GTK, terminal I/O, ANSI or frontend modules are imported here.
"""
from .review_service import ReviewService, Discovery, Preview
from .operations import (CleanerError, AuthorizationError, OperationCancelled,
                         CancellationToken, ProgressEvent, operation)
from .authorization import ExecutionResult, prepare_authorization, execute_confirmed
from .cleanup import CleanupSession, Proposal, CleanupOutcome
from .inventory import Package
from .providers import inventory as load_inventory, preview as simulate, filter_sources
from .recommendations import Recommendation, Evidence, CATEGORIES
from .removal import Plan
from .preferences import Preferences

__all__ = ['CleanupSession', 'Proposal', 'CleanupOutcome', 'ReviewService', 'Discovery', 'Preview', 'CleanerError', 'AuthorizationError',
           'OperationCancelled', 'CancellationToken', 'ProgressEvent', 'operation',
           'ExecutionResult', 'prepare_authorization', 'execute_confirmed', 'Package',
           'load_inventory', 'Recommendation', 'Evidence', 'CATEGORIES', 'Plan', 'simulate', 'Preferences']
