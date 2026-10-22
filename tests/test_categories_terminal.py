# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
import io
import json
import os
import re
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from package_cleaner.inventory import Package
from package_cleaner.recommendations import Evidence, audit_recommendations as recommend, collect_evidence, application_savings
from package_cleaner.removal import Baseline, classify
from package_cleaner.progress import Progress
from package_cleaner.terminal import style, write


class TTY(io.StringIO):
    def isatty(self):
        return True


class CategoryTests(unittest.TestCase):
    def setUp(self):
        self.ps = {}
        for i in range(15):
            p = Package(f'app{i}:amd64', f'app{i}', '1', False, 10**9, 'Application', 'misc', application=True)
            self.ps[p.name] = p
        self.small = Package('apache2:amd64', 'apache2', '1', False, 100, 'Hosts websites', 'web', stack='Apache')
        self.ps[self.small.name] = self.small

    def simulate(self, names, packages, kept):
        return classify(names, names, names, Baseline([], ''), packages, kept, '')

    def recommendations(self, category, evidence=None, **kwargs):
        with patch('package_cleaner.recommendations.running_executables', return_value=set()), patch('package_cleaner.recommendations.collect_evidence', side_effect=evidence or (lambda *a, **k: Evidence())), patch('package_cleaner.recommendations.simulate', side_effect=self.simulate) as sim:
            rows = recommend(self.ps, category=category, **kwargs)
        return rows, sim

    def test_unknown_usage_never_fills_potential_category(self):
        rows, sim = self.recommendations('potential')
        self.assertFalse(rows)
        sim.assert_not_called()

    def test_small_potential_candidate_not_hidden_by_large_unknowns(self):
        def evidence(p, *args, **kwargs):
            return Evidence(executable_count=1, old_accesses=1, recorded_age_days=500) if p.name == self.small.name else Evidence()
        rows, sim = self.recommendations('potential', evidence)
        self.assertEqual([r.package for r in rows], [self.small.name])

    def test_potential_inspects_all_beyond_twelve_and_keeps_all_results(self):
        rows, sim = self.recommendations('potential', lambda *a, **k: Evidence(services=[{'active': 'inactive', 'enabled': 'disabled'}]), limit=5)
        self.assertEqual(len(rows), 16)
        self.assertEqual(sim.call_count, 16)

    def test_servers_includes_small_active_apache_and_php(self):
        php = Package('php8.3-cli:amd64', 'php8.3-cli', '1', False, 100, 'PHP interpreter', 'php', stack='PHP')
        self.ps[php.name] = php
        rows, sim = self.recommendations('servers', lambda *a, **k: Evidence(running=True))
        self.assertEqual([r.package for r in rows], [self.small.name, php.name])

    def test_search_can_find_automatic_application_directly(self):
        self.small.automatic = True
        rows, sim = self.recommendations('search', query='websites')
        self.assertEqual([r.package for r in rows], [self.small.name])

    def test_large_category_still_uses_explicit_size_shortlist(self):
        rows, sim = self.recommendations('large', limit=5)
        self.assertEqual(sim.call_count, 12)
        self.assertNotIn(self.small.name, [r.package for r in rows])

    def test_preexisting_orphans_cannot_inflate_application_savings(self):
        self.small.automatic = True
        baseline = Baseline([self.small.name], '')
        plan = classify([self.small.name], [self.small.name], [self.small.name], baseline, self.ps, [], '')
        self.assertEqual(application_savings(plan), 0)
        self.assertEqual(plan.savings['selected'], 100)
        other = self.ps['app0:amd64']
        other.automatic = True
        plan = classify([self.small.name], [self.small.name], [self.small.name, other.name],
                        Baseline([other.name], ''), self.ps, [], '')
        self.assertEqual(application_savings(plan), 100)
        self.assertEqual(plan.savings['already_orphaned'], 10**9)

    def test_historical_service_event_is_not_reported_as_recent(self):
        old = datetime(2025, 1, 1, tzinfo=timezone.utc)
        responses = [(0, 'LoadState=loaded\nActiveState=inactive\nUnitFileState=disabled', ''),
                     (0, json.dumps({'__REALTIME_TIMESTAMP': str(int(old.timestamp() * 1_000_000))}), '')]
        with patch('package_cleaner.recommendations.command', side_effect=responses), patch('package_cleaner.recommendations.inspect_server', return_value=None):
            result = collect_evidence(self.small, set(), paths=['/usr/lib/systemd/system/apache2.service'], now=datetime(2026, 10, 20, tzinfo=timezone.utc))
        self.assertFalse(result.services[0]['journal_entry'])
        self.assertEqual(result.services[0]['last_journal_event'], old.isoformat())


class TerminalTests(unittest.TestCase):
    def test_redirected_output_plain(self):
        with patch.dict(os.environ, {'TERM': 'xterm'}):
            self.assertEqual(style('BLOCKED', 'blocked', io.StringIO()), 'BLOCKED')

    def test_no_color_and_dumb_terminal(self):
        with patch.dict(os.environ, {'TERM': 'xterm', 'NO_COLOR': ''}):
            self.assertEqual(style('Question', 'question', TTY()), 'Question')
        with patch.dict(os.environ, {'TERM': 'dumb'}):
            self.assertEqual(style('Question', 'question', TTY()), 'Question')

    def test_styles_wrap_plain_text_and_highlight_shortcuts(self):
        with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True):
            stream = TTY()
            write('[K] Keep it permanently [R] Queue removal [D] Details', 'actions', stream, width=30)
        text = stream.getvalue()
        self.assertIn('\033[1;36m[K]', text)
        plain = re.sub(r'\x1b\[[0-9;]*m', '', text)
        self.assertTrue(all(len(line) <= 30 for line in plain.splitlines()))
        self.assertIn('[D] Details', plain)

    def test_progress_overwrites_one_line(self):
        stream = TTY()
        progress = Progress(stream)
        progress('Analyzing applications... 1 of 12')
        progress('Analyzing applications... 6 of 12')
        progress.finish(5)
        self.assertEqual(stream.getvalue().count('\n'), 1)
        self.assertEqual(stream.getvalue().count('\r'), 3)
        self.assertIn('Found 5 cleanup opportunities.', stream.getvalue())

    def test_redirected_progress_only_final_count(self):
        stream = io.StringIO()
        progress = Progress(stream)
        progress('Analyzing applications... 1 of 12')
        progress.diagnostic('application diagnostic')
        progress.finish(5)
        self.assertEqual(stream.getvalue(), 'Found 5 cleanup opportunities.\n')

    def test_verbose_diagnostics_are_opt_in(self):
        stream = io.StringIO()
        progress = Progress(stream, verbose=True)
        progress.diagnostic('Inspecting apache2:amd64')
        self.assertIn('Inspecting apache2:amd64', stream.getvalue())


if __name__ == '__main__':
    unittest.main()
