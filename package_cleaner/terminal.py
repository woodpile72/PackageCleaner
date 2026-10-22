# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Dependency-free terminal presentation. Wrap plain text before adding ANSI codes."""
import os
import re
import shutil
import sys
import textwrap


CODES = {'title': '1;97', 'question': '1', 'success': '32', 'warning': '33',
         'blocked': '1;31', 'secondary': '2', 'actions': '36'}


def enabled(stream=None):
    stream = sys.stdout if stream is None else stream
    return stream.isatty() and 'NO_COLOR' not in os.environ and os.environ.get('TERM', '') not in ('', 'dumb')


def style(text, role='normal', stream=None):
    if not enabled(stream) or role not in CODES:
        return text
    code = '\033[' + CODES[role] + 'm'
    if role == 'actions':
        text = re.sub(r'\[([A-Z0-9])\]', lambda m: '\033[1;36m' + m[0] + '\033[0m' + code, text)
    return code + text + '\033[0m'


def write(text='', role='normal', stream=None, width=None, end='\n'):
    stream = sys.stdout if stream is None else stream
    width = width or max(30, min(100, shutil.get_terminal_size((88, 24)).columns))
    # Never measure escape sequences. Styling happens only after plain-text wrapping.
    rendered = []
    for line in str(text).split('\n'):
        indent = len(line) - len(line.lstrip())
        if role == 'actions' and re.search(r'\[[A-Z0-9]\]', line):
            # Keep each shortcut attached to its action description when possible.
            lines, current = [], ''
            for action in re.split(r'(?=\[[A-Z0-9]\])', line):
                action = action.strip()
                if not action:
                    continue
                if current and len(current) + 2 + len(action) > width:
                    lines.append(current)
                    current = ''
                if len(action) > width:
                    parts = textwrap.wrap(action, width=width, break_long_words=False, break_on_hyphens=False)
                    lines.extend(parts[:-1])
                    action = parts[-1]
                current = current + '  ' + action if current else action
            lines.append(current)
        else:
            lines = textwrap.wrap(line, width=width, subsequent_indent=' ' * indent,
                                  replace_whitespace=False, break_long_words=False, break_on_hyphens=False) or ['']
        for wrapped in lines:
            rendered.append(style(wrapped, role, stream))
    print('\n'.join(rendered), file=stream, end=end, flush=end != '\n')


def ask(prompt, role='actions'):
    """Wrap and style the prompt, leaving input on its final line. EOF quits."""
    write(prompt.rstrip(), role, end=' ')
    try:
        return input().strip().lower()
    except EOFError:
        return 'q'


from .display import local_date
