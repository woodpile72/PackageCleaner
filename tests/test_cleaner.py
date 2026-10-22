# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
import gzip
import io
from contextlib import redirect_stdout, redirect_stderr
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from package_cleaner.inventory import Package, history, resolve, stack_family, stack_catalog, stack_selection
from package_cleaner.analysis import review_reason, review_units
from package_cleaner.removal import Baseline, classify, parse_simulation, simulate


def pkg(name, auto=False, size=100, **kwargs):
    return Package(name + ':amd64', name, '1', auto, size, name, 'misc', **kwargs)


class CleanerTests(unittest.TestCase):
    def setUp(self):
        self.rows = [pkg('app', application=True), pkg('new', True), pkg('old', True),
                     pkg('consumer'), pkg('shared', True), pkg('desktop', protected=['desktop'])]
        self.ps = {p.name: p for p in self.rows}
        self.ps['app:amd64'].dependencies = ['new:amd64', 'shared:amd64']
        self.ps['shared:amd64'].reverse_dependencies = ['app:amd64', 'consumer:amd64']

    def test_distinct_savings_and_retained_shared(self):
        plan = classify(['app:amd64'], ['app:amd64', 'consumer:amd64'],
                        ['app:amd64', 'consumer:amd64', 'new:amd64', 'old:amd64'],
                        Baseline(['old:amd64'], ''), self.ps, [], '')
        self.assertEqual(plan.savings, dict(selected=100, newly_orphaned=100,
                                         already_orphaned=100, dependent_removals=100))
        self.assertEqual(plan.newly_orphaned, ['new:amd64'])
        self.assertEqual(plan.retained[0]['name'], 'shared:amd64')
        plan = classify(['app:amd64'], ['app:amd64'], ['app:amd64', 'new:amd64', 'old:amd64'],
                        Baseline(['old:amd64'], ''), self.ps, [], '')
        self.assertEqual(plan.retained[0]['remaining_reverse_dependencies'], ['consumer:amd64'])

    def test_selected_baseline_not_double_counted(self):
        plan = classify(['old:amd64'], ['old:amd64'], ['old:amd64'], Baseline(['old:amd64'], ''), self.ps, [], '')
        self.assertEqual(plan.savings['already_orphaned'], 0)
        self.assertEqual(plan.savings['selected'], 100)

    def test_keep_and_protected_transitive_removals_rejected(self):
        plan = classify(['app:amd64'], ['app:amd64', 'desktop:amd64'],
                        ['app:amd64', 'desktop:amd64', 'new:amd64'], Baseline([], ''), self.ps, ['new:amd64'], '')
        self.assertEqual(len(plan.blocked), 3)

    def test_manual_dependent_blocks_until_explicitly_selected(self):
        self.ps['consumer:amd64'].dependencies = ['app:amd64']
        plan = classify(['app:amd64'], ['app:amd64', 'consumer:amd64'],
                        ['app:amd64', 'consumer:amd64'], Baseline([], ''), self.ps, [], '')
        self.assertTrue(plan.blocked)
        self.assertEqual(plan.unselected_manual[0]['removed_dependencies'], ['app:amd64'])
        plan = classify(['app:amd64', 'consumer:amd64'], ['app:amd64', 'consumer:amd64'],
                        ['app:amd64', 'consumer:amd64'], Baseline([], ''), self.ps, [], '')
        self.assertFalse(plan.blocked)

    def test_explicit_selection_cannot_override_protection(self):
        plan = classify(['desktop:amd64'], ['desktop:amd64'], ['desktop:amd64'],
                        Baseline([], ''), self.ps, [], '')
        self.assertTrue(plan.blocked)

    def test_strict_simulation_parse(self):
        output = '0 upgraded, 0 newly installed, 1 to remove and 0 not upgraded.\nRemv app [1]\n'
        self.assertEqual(parse_simulation(output, self.ps), ['app:amd64'])
        for invalid in [output.replace('1 to remove', '2 to remove'), output + 'Inst consumer [1]\n',
                        'Remv app [1]', output.replace('Remv app', 'Remv unknown')]:
            with self.assertRaises(ValueError):
                parse_simulation(invalid, self.ps)

    def test_baseline_precedes_target_simulations(self):
        with patch('package_cleaner.removal.apt_simulation', side_effect=[
            (['old:amd64'], 'baseline'), (['app:amd64'], 'direct'),
            (['app:amd64', 'old:amd64', 'new:amd64'], 'combined'),
            (['app:amd64', 'new:amd64'], 'final')]) as run:
            plan = simulate(['app'], self.ps)
        self.assertEqual(run.call_args_list[0].args[0], ['autoremove'])
        self.assertIn('APT::Get::AutomaticRemove=false', run.call_args_list[1].args[0])
        self.assertEqual(plan.newly_orphaned, ['new:amd64'])

    def test_unknown_not_inactivity_and_library_not_card(self):
        self.assertIn('unknown', review_reason(self.ps['app:amd64']))
        self.assertIsNone(review_reason(self.ps['new:amd64']))
        self.assertEqual(review_units(self.ps), [('app', ['app:amd64'])])

    def stack_fixture(self):
        rows = [pkg('apache2'), pkg('apache2-bin', application=True),
                pkg('apache2-utils', True, application=True), pkg('apache2-data', True),
                pkg('libapr', True), pkg('other', application=True)]
        ps = {p.name: p for p in rows}
        for p in rows:
            p.stack = stack_family(p.base)
        ps['apache2:amd64'].dependencies = ['apache2-bin:amd64', 'apache2-data:amd64', 'libapr:amd64']
        ps['apache2-bin:amd64'].reverse_dependencies = ['apache2:amd64']
        ps['libapr:amd64'].reverse_dependencies = ['apache2:amd64', 'other:amd64']
        return ps

    def test_grouping_never_selects_manual_or_automatic_support(self):
        ps = self.stack_fixture()
        self.assertEqual(stack_selection('Apache', ps), ['apache2:amd64'])
        cards = review_units(ps)
        self.assertEqual([names for label, names in cards], [['apache2:amd64'], ['other:amd64']])
        group = stack_catalog(ps)['Apache']
        self.assertEqual(group['manual_supporting_packages'], ['apache2-bin:amd64'])
        self.assertNotIn('apache2-bin:amd64', group['candidates'])

    def test_grouped_manual_support_still_blocks_resolver_removal(self):
        ps = self.stack_fixture()
        selected = stack_selection('Apache', ps)
        removed = ['apache2:amd64', 'apache2-bin:amd64', 'apache2-data:amd64']
        plan = classify(selected, removed[:2], removed, Baseline([], ''), ps, [], '')
        self.assertEqual(plan.requested, ['apache2:amd64'])
        self.assertEqual([row['name'] for row in plan.unselected_manual], ['apache2-bin:amd64'])
        self.assertTrue(plan.blocked)
        self.assertEqual(plan.newly_orphaned, ['apache2-data:amd64'])
        self.assertEqual(plan.retained[0]['name'], 'libapr:amd64')
        self.assertEqual(plan.retained[0]['remaining_reverse_dependencies'], ['other:amd64'])
        # Only a separate explicit selection can unblock the manual support package.
        plan = classify(selected + ['apache2-bin:amd64'], removed[:2], removed, Baseline([], ''), ps, [], '')
        self.assertFalse(plan.blocked)

    def test_interactive_remove_stages_only_single_application(self):
        from package_cleaner.presentation import review
        from package_cleaner.preferences import Preferences
        from package_cleaner.recommendations import Recommendation, Evidence
        ps = self.stack_fixture()
        plan = classify(['apache2:amd64'], ['apache2:amd64'], ['apache2:amd64'], Baseline([], ''), ps, [], '')
        item = Recommendation('apache2:amd64', 'Apache', 'Hosts websites.', 'Keep if used.', Evidence(), 1, [], plan)
        with tempfile.TemporaryDirectory() as directory, patch('builtins.input', side_effect=['r', 'f']), patch('package_cleaner.presentation.inventory', return_value=(ps, [])), patch('package_cleaner.presentation.simulate', return_value=plan) as run, redirect_stdout(io.StringIO()):
            review(ps, candidates=[item], preferences=Preferences(Path(directory) / 'preferences.json'))
        self.assertEqual(run.call_args.args[0], ['apache2:amd64'])

    def test_cli_stack_cannot_expand_to_support(self):
        from package_cleaner.__main__ import main
        ps = self.stack_fixture()
        plan = classify(['apache2:amd64'], ['apache2:amd64'], ['apache2:amd64'], Baseline([], ''), ps, [], '')
        with patch('sys.argv', ['package-cleaner', 'simulate', '--stack', 'Apache']), patch('package_cleaner.__main__.inventory', return_value=(ps, [])), patch('package_cleaner.__main__.simulate', return_value=plan) as run, redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
        self.assertEqual(run.call_args.args[0], ['apache2:amd64'])

    def test_multiple_entry_points_require_individual_selection(self):
        rows = [pkg('php'), pkg('php8.3-cli'), pkg('php8.3-common')]
        for p in rows:
            p.stack = stack_family(p.base)
        ps = {p.name: p for p in rows}
        with self.assertRaisesRegex(ValueError, 'explicitly name'):
            stack_selection('PHP', ps)
        self.assertEqual(len(review_units(ps)), 2)
        self.assertTrue(all(len(names) == 1 for _, names in review_units(ps)))

    def test_database_library_and_client_are_not_server_installation(self):
        library = pkg('libmariadb3')
        common = pkg('mariadb-common')
        client = pkg('mariadb-client')
        server = pkg('mariadb-server', True)
        ps = {}
        for p in [library, common, client, server]:
            p.stack = stack_family(p.base)
        ps.update({p.name: p for p in [library, common]})
        self.assertEqual(review_units(ps), [])
        self.assertIn('no server entry point', stack_catalog(ps)['MariaDB / MySQL']['status'])
        with self.assertRaises(ValueError):
            stack_selection('MariaDB / MySQL', ps)
        ps[client.name] = client
        self.assertTrue(stack_catalog(ps)['MariaDB / MySQL']['status'].startswith('Client tools only'))
        ps[server.name] = server
        self.assertTrue(stack_catalog(ps)['MariaDB / MySQL']['status'].startswith('Server entry point installed'))
        self.assertEqual(stack_selection('MariaDB / MySQL', ps), [client.name])

    def test_protected_root_not_selectable_by_group(self):
        ps = self.stack_fixture()
        ps['apache2:amd64'].protected = ['test policy']
        with self.assertRaises(ValueError):
            stack_selection('Apache', ps)
        self.assertFalse(any('apache2:amd64' in names for _, names in review_units(ps)))

    def test_rotated_history_latest_install(self):
        with tempfile.TemporaryDirectory() as directory:
            plain = Path(directory) / 'dpkg.log'
            archive = Path(directory) / 'dpkg.log.1.gz'
            plain.write_text('2025-01-01 10:00:00 install app:amd64 <none> 1\n'
                             '2026-01-01 10:00:00 upgrade app:amd64 1 2\n')
            with gzip.open(archive, 'wt') as stream:
                stream.write('2024-01-01 10:00:00 install app:amd64 <none> 0\n')
            dates, warnings = history([str(plain), str(archive)])
        self.assertEqual(dates['app:amd64'], '2025-01-01T10:00:00')
        self.assertFalse(warnings)

    def test_multiarch_ambiguity_rejected(self):
        alt = pkg('app'); alt.name = 'app:i386'; self.ps[alt.name] = alt
        with self.assertRaises(ValueError):
            resolve(['app'], self.ps)


if __name__ == '__main__':
    unittest.main()
