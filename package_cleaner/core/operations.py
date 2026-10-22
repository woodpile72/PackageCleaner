# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Structured operation context. Cancellation never kills a privileged transaction."""
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Event
from typing import Callable


class CleanerError(ValueError):
    """Domain operation could not be completed safely."""


class OperationCancelled(CleanerError):
    pass


class AuthorizationError(CleanerError):
    pass


@dataclass(frozen=True)
class ProgressEvent:
    phase: str
    completed: int = 0
    total: int | None = None


class CancellationToken:
    def __init__(self):
        self._event = Event()

    def cancel(self):
        self._event.set()

    def check(self):
        if self._event.is_set():
            raise OperationCancelled('Operation cancelled. No package changes were requested.')


_current = ContextVar('package_cleaner_operation', default=None)


@contextmanager
def operation(progress: Callable[[ProgressEvent], None] | None = None, cancellation: CancellationToken | None = None):
    token = _current.set((progress, cancellation))
    try:
        checkpoint()
        yield
    finally:
        _current.reset(token)


def checkpoint(phase=None, completed=0, total=None):
    current = _current.get()
    if current:
        progress, cancellation = current
        if cancellation:
            cancellation.check()
        if phase and progress:
            progress(ProgressEvent(phase, completed, total))


def cancellable():
    return _current.get() is not None


def domain_operation(function):
    """Uniform typed errors and opt-in progress/cancellation on public operations."""
    from functools import wraps
    @wraps(function)
    def wrapped(*args, **kwargs):
        progress = kwargs.pop('progress', None)
        cancellation = kwargs.pop('cancellation', None)
        from contextlib import nullcontext
        scope = operation(progress, cancellation) if progress is not None or cancellation is not None else nullcontext()
        try:
            with scope:
                checkpoint()
                return function(*args, **kwargs)
        except CleanerError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            raise CleanerError(str(exc)) from exc
    return wrapped


def current_cancellation():
    """Capture a token for a cooperating backend cancellation monitor."""
    current = _current.get()
    return current[1] if current else None
