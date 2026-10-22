# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""One updating terminal line; redirected output contains only the final count."""
import sys


class Progress:
    def __init__(self, stream=None, verbose=False):
        self.stream = stream if stream is not None else sys.stderr
        self.verbose = verbose
        self.interactive = self.stream.isatty()
        self.width = 0

    def __call__(self, message):
        if self.verbose:
            print(message, file=self.stream, flush=True)
        elif self.interactive:
            self.width = max(self.width, len(message))
            print('\r' + message.ljust(self.width), end='', file=self.stream, flush=True)

    def diagnostic(self, message):
        if self.verbose:
            print(message, file=self.stream, flush=True)

    def finish(self, count, failed=0, attempted=0, discovered=False):
        message = f'Found {count} cleanup opportunit' + ('y.' if count == 1 else 'ies.')
        if discovered:
            message = f'Found {count} application' + ('' if count == 1 else 's') + ' to review.'
        if discovered and failed:
            message = 'Analysis failed: applications could not be analyzed.'
        elif failed and failed == attempted:
            message = f'Analysis failed: {failed} previews could not be verified.'
        elif failed:
            message += f' Analysis incomplete: {failed} previews could not be verified.'
        if self.interactive and not self.verbose:
            print('\r' + message.ljust(self.width), file=self.stream, flush=True)
        else:
            print(message, file=self.stream, flush=True)
        self.width = 0
