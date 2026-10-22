# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
import unittest
from unittest.mock import patch
from package_cleaner.inventory import Package
from package_cleaner.removal import simulate
from package_cleaner.state import PackageSnapshot


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.ps = {n: Package(n, n.split(':')[0], '1', auto, 100, n, 'misc', protected=protected)
                   for n, auto, protected in [('app:amd64', False, []), ('other:amd64', False, []),
                                              ('support:amd64', True, []), ('shared:amd64', True, []),
                                              ('old:i386', True, ['system']), ('core:amd64', True, ['system'])]}
        self.ps['app:amd64'].dependencies = ['support:amd64', 'shared:amd64']
        self.ps['shared:amd64'].reverse_dependencies = ['app:amd64', 'other:amd64']

    def run_plan(self, baseline, direct, discovered, final, selected=('app:amd64',), kept=()):
        with patch('package_cleaner.removal.package_state', return_value='same'), patch(
                'package_cleaner.removal.apt_simulation', side_effect=[(x, '') for x in (baseline,direct,discovered,final)]) as run:
            plan = simulate(selected, self.ps, kept)
        self.assertEqual(run.call_args_list[-1].args[0], plan.transaction_arguments)
        self.assertIn('APT::Get::AutomaticRemove=false', plan.transaction_arguments)
        self.assertNotIn('--autoremove', plan.transaction_arguments)
        return plan

    def test_protected_baseline_retained_and_new_support_removed(self):
        p = self.run_plan(['old:i386'], ['app:amd64'], ['app:amd64','support:amd64','old:i386'], ['app:amd64','support:amd64'])
        self.assertFalse(p.blocked)
        self.assertNotIn('old:i386', p.transaction_arguments)
        self.assertEqual(p.newly_orphaned, ['support:amd64'])
        self.assertEqual(p.savings['already_orphaned'], 0)
        self.assertEqual(p.baseline.removed, ['old:i386'])
        self.assertEqual(p.requested, ['app:amd64'])

    def test_empty_baseline(self):
        p = self.run_plan([], ['app:amd64'], ['app:amd64','support:amd64'], ['app:amd64','support:amd64'])
        self.assertFalse(p.blocked)
        self.assertEqual(p.state_token, 'same')

    def test_shared_dependency_retained(self):
        p = self.run_plan([], ['app:amd64'], ['app:amd64','support:amd64'], ['app:amd64','support:amd64'])
        self.assertEqual(p.retained[0]['name'], 'shared:amd64')
        self.assertEqual(p.retained[0]['remaining_reverse_dependencies'], ['other:amd64'])

    def test_unselected_manual_remains_blocked_and_not_promoted(self):
        names = ['app:amd64','other:amd64']
        p = self.run_plan([], names, names, names)
        self.assertTrue(p.blocked)
        self.assertEqual(p.unselected_manual[0]['name'], 'other:amd64')
        self.assertNotIn('other:amd64', p.transaction_arguments)
        self.assertEqual(p.requested, ['app:amd64'])

    def test_multiple_selected_applications(self):
        names = ['app:amd64','other:amd64']
        p = self.run_plan([], names, names+['support:amd64'], names+['support:amd64'], selected=names)
        self.assertFalse(p.blocked)
        self.assertEqual(p.requested, names)

    def test_protected_resolver_removal_still_blocked(self):
        names = ['app:amd64','core:amd64']
        p = self.run_plan([], names, names, names)
        self.assertTrue(any('system' in x for x in p.blocked))

    def test_protected_explicit_selection_never_overrides_protection(self):
        p = self.run_plan(['old:i386'], ['old:i386'], ['old:i386'], ['old:i386'], selected=['old:i386'])
        self.assertTrue(p.blocked)

    def test_protected_and_kept_new_support_not_targeted(self):
        p = self.run_plan([], ['app:amd64'], ['app:amd64','core:amd64','support:amd64'], ['app:amd64'], kept=['support:amd64'])
        self.assertFalse(p.blocked)
        self.assertEqual(p.retained_ineligible_support, ['core:amd64','support:amd64'])

    def test_additional_automatic_removal_is_blocked(self):
        p = self.run_plan([], ['app:amd64'], ['app:amd64'], ['app:amd64','shared:amd64'])
        self.assertTrue(any('additional resolver removal' in x for x in p.blocked))
        self.assertEqual(p.newly_orphaned, [])

    def test_baseline_resolver_removal_needs_explicit_approval(self):
        self.ps['old:i386'].protected = []
        p = self.run_plan(['old:i386'], ['app:amd64'], ['app:amd64','old:i386'], ['app:amd64','old:i386'])
        self.assertTrue(any('existing orphan not separately approved' in x for x in p.blocked))
        p = self.run_plan(['old:i386'], ['app:amd64','old:i386'], ['app:amd64','old:i386'], ['app:amd64','old:i386'], selected=['app:amd64','old:i386'])
        self.assertFalse(p.blocked)
        self.assertEqual(p.baseline_selected_bytes, 100)

    def test_package_state_changes_at_any_simulation_boundary_fail_closed(self):
        # Initial snapshot plus before/after each of four simulations.
        for changed_at in range(1, 9):
            with self.subTest(changed_at=changed_at), patch('package_cleaner.removal.package_state',
                    side_effect=['same']*changed_at+['changed']*9), patch('package_cleaner.removal.apt_simulation',
                    side_effect=[([], ''), (['app:amd64'], ''), (['app:amd64'], ''), (['app:amd64'], '')]):
                with self.assertRaisesRegex(ValueError, 'Package state changed'):
                    simulate(['app:amd64'], self.ps)

    def test_stale_inventory_rejected_before_simulations(self):
        ps = PackageSnapshot(self.ps); ps.state_token = 'old'
        with patch('package_cleaner.removal.package_state', return_value='new'), patch('package_cleaner.removal.apt_simulation') as run:
            with self.assertRaisesRegex(ValueError, 'since inventory'):
                simulate(['app:amd64'], ps)
            run.assert_not_called()

    def test_missing_final_target_rejected(self):
        with self.assertRaisesRegex(ValueError, 'exact targets'):
            self.run_plan([], ['app:amd64'], ['app:amd64','support:amd64'], ['app:amd64'])

    def test_actual_command_wrapper_simulates_exact_explicit_arguments(self):
        def output(names):
            return (0, f'0 upgraded, 0 newly installed, {len(names)} to remove and 0 not upgraded.\n'
                    + ''.join(f'Remv {n} [1]\n' for n in names), '')
        with patch('package_cleaner.removal.package_state', return_value='same'), patch(
                'package_cleaner.removal.command', side_effect=[output(['old:i386']), output(['app:amd64']),
                output(['app:amd64','support:amd64','old:i386']), output(['app:amd64','support:amd64'])]) as run:
            plan = simulate(['app:amd64'], self.ps)
        self.assertEqual(run.call_args.args[0], ['apt-get', '--simulate', '-o', 'Debug::NoLocking=1',
                                               *plan.transaction_arguments])
        self.assertTrue(all('--simulate' in call.args[0] for call in run.call_args_list))
        self.assertFalse(plan.blocked)
