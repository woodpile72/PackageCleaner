# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from package_cleaner.terminal import ask
from package_cleaner.presentation import choose_category, quit_review
from package_cleaner.__main__ import confirm_clear


class PromptTests(unittest.TestCase):
    def test_category_answer_follows_question_without_choice_line(self):
        output = io.StringIO()
        def enter():
            self.assertTrue(output.getvalue().endswith('Choose a category: '))
            self.assertNotIn('Choice:', output.getvalue())
            print('q')  # Simulate terminal echo.
            return 'q'
        with redirect_stdout(output), patch('builtins.input', side_effect=enter):
            self.assertIsNone(choose_category())
        self.assertIn('Choose a category: q\n', output.getvalue())

    def test_confirmation_uses_same_line_and_normalizes_answer(self):
        output = io.StringIO()
        def enter():
            self.assertTrue(output.getvalue().endswith('[y/N]: '))
            return ' YES '
        with redirect_stdout(output), patch('builtins.input', side_effect=enter):
            self.assertTrue(confirm_clear())

    def test_wrapped_navigation_leaves_final_line_open(self):
        output = io.StringIO()
        with redirect_stdout(output), patch('builtins.input', return_value=' D '):
            self.assertEqual(ask('[N] Next set  ' * 10 + '[D] Details:'), 'd')
        self.assertIn('\n', output.getvalue())
        self.assertTrue(output.getvalue().endswith('[D] Details: '))

    def test_eof_cancels_confirmation(self):
        with redirect_stdout(io.StringIO()), patch('builtins.input', side_effect=EOFError):
            self.assertFalse(confirm_clear())

    def test_quit_messages_reflect_staged_selections(self):
        for selected, expected in [((), 'Goodbye.\n'),
                                   ({'app:amd64'}, 'Queued removal choices discarded.\nGoodbye.\n')]:
            output = io.StringIO()
            with redirect_stdout(output):
                quit_review(selected)
            self.assertEqual(output.getvalue(), expected)
