# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from package_cleaner.inventory import Package
from package_cleaner.preferences import Preferences
from package_cleaner.recommendations import (Evidence, Recommendation, collect_evidence,
                                           evidence_summary, priority, audit_recommendations as recommend)
from package_cleaner.presentation import review, plain_plan
from package_cleaner.removal import Baseline, classify


class GuidedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.prefs = Preferences(Path(self.temp.name) / 'preferences.json')
        self.ps = {}
        for name, auto in [('one', False), ('two', False), ('unused', True), ('shared', True), ('old', True)]:
            p = Package(name + ':amd64', name, '1', auto, 1024**2, 'An installed test application', 'misc', application=not auto)
            self.ps[p.name] = p
        self.ps['one:amd64'].dependencies = ['unused:amd64', 'shared:amd64']
        self.ps['shared:amd64'].reverse_dependencies = ['one:amd64', 'two:amd64']
        self.plan = classify(['one:amd64'], ['one:amd64'], ['one:amd64', 'unused:amd64', 'old:amd64'],
                             Baseline(['old:amd64'], ''), self.ps, [], '')
        self.items = [Recommendation(n + ':amd64', title, 'Helps with a task.', 'Keep if you use it.', Evidence(), 1, [], self.plan)
                      for n, title in [('one', 'First application'), ('two', 'Second application')]]

    def run_review(self, choices, items=None, plan=None, fresh=None):
        output = io.StringIO()
        with patch('builtins.input', side_effect=choices), patch('package_cleaner.presentation.simulate', return_value=plan or self.plan) as simulate, patch('package_cleaner.presentation.inventory', return_value=(fresh or self.ps, [])) as inventory, redirect_stdout(output):
            result = review(self.ps, candidates=items or self.items, preferences=self.prefs)
        return result, output.getvalue(), simulate, inventory

    def test_missing_history_is_unknown(self):
        text = evidence_summary(Evidence())
        self.assertIn('not enough reliable history', text)
        self.assertIn('does not mean it is unused', text)
        with patch('package_cleaner.recommendations.command', return_value=(1, '', 'permission denied')):
            evidence = collect_evidence(self.ps['one:amd64'], set(), paths=['/usr/lib/systemd/system/test.service'])
        self.assertFalse(evidence.activity)
        self.assertIsNone(evidence.services[0]['active'])
        self.assertIn('not enough reliable history', evidence_summary(evidence))

    def test_stopped_disabled_is_only_limited_current_evidence(self):
        evidence = Evidence(services=[{'active': 'inactive', 'enabled': 'disabled', 'journal_entry': False}])
        self.assertIn('past use is unknown', evidence_summary(evidence))
        self.assertGreater(priority(self.ps['one:amd64'], evidence)[0], priority(self.ps['one:amd64'], Evidence())[0])

    def test_activity_and_configuration_lower_priority(self):
        base = priority(self.ps['one:amd64'], Evidence())[0]
        self.assertLess(priority(self.ps['one:amd64'], Evidence(running=True))[0], base)
        self.assertLess(priority(self.ps['one:amd64'], Evidence(modified_configuration=1))[0], base)
        self.assertIn('accessed', evidence_summary(Evidence(recent_accesses=1)))
        self.assertNotIn('last used', evidence_summary(Evidence(recent_accesses=1)))

    def test_config_compare_never_outputs_contents(self):
        conf = Path(self.temp.name) / 'test.conf'
        conf.write_text('private setting value')
        p = self.ps['one:amd64']
        p.conffiles = [(str(conf), '0' * 32)]
        evidence = collect_evidence(p, set(), paths=[])
        self.assertEqual(evidence.modified_configuration, 1)
        self.assertNotIn('private setting', str(evidence))

    def test_persistent_keep_and_reset(self):
        self.prefs.keep('one:amd64')
        again = Preferences(self.prefs.path)
        self.assertIn('one:amd64', again.kept)
        again.reset(['one:amd64'])
        self.assertFalse(Preferences(self.prefs.path).kept)

    def test_malformed_preferences_fail_closed(self):
        self.prefs.path.write_text('{')
        with self.assertRaises(ValueError):
            Preferences(self.prefs.path)

    def test_keep_excluded_but_running_alone_not_a_usage_veto(self):
        self.prefs.keep('one:amd64')
        with patch('package_cleaner.recommendations.running_executables', return_value=set()), patch('package_cleaner.recommendations.collect_evidence', return_value=Evidence(running=True)) as evidence, patch('package_cleaner.recommendations.simulate', return_value=self.plan):
            result = recommend(self.ps, self.prefs.kept)
        self.assertEqual([item.package for item in result], ['two:amd64'])
        self.assertEqual(result[0].assessment['level'], 'unknown')
        self.assertTrue(result[0].assessment['cautions'])
        self.assertTrue(all(call.args[0].name == 'two:amd64' for call in evidence.call_args_list))

    def test_blocked_not_recommended(self):
        self.plan.blocked = ['protected']
        with patch('package_cleaner.recommendations.running_executables', return_value=set()), patch('package_cleaner.recommendations.collect_evidence', return_value=Evidence()), patch('package_cleaner.recommendations.simulate', return_value=self.plan):
            self.assertFalse(recommend(self.ps))

    def test_navigation_keep_previous_remove_and_final_refresh(self):
        result, output, simulate, inventory = self.run_review(['k', 'p', 'r', 'f', 'f'])
        self.assertEqual(result, 0)
        self.assertNotIn('one:amd64', self.prefs.kept)
        self.assertEqual(inventory.call_count, 2)  # choosing Queue removal and final revalidation
        self.assertEqual(simulate.call_args.args[0], ['one:amd64'])
        self.assertIn('Reviewing suggestion 2 of 2', output)
        self.assertIn('Your cleanup summary', output)
        self.assertNotIn('one:amd64', output)

    def test_keep_removes_previous_selection_and_persists(self):
        result, output, simulate, inventory = self.run_review(['r', 'p', 'k', 'f'])
        self.assertIn('apt:system/one:amd64', Preferences(self.prefs.path).kept)
        self.assertIn('no applications selected', output)
        self.assertEqual(simulate.call_count, 1)

    def test_skip_and_previous_do_not_save_decisions(self):
        result, output, simulate, inventory = self.run_review(['s', 'p', 'f'])
        self.assertFalse(self.prefs.path.exists())
        self.assertFalse(simulate.called)
        self.assertEqual(output.count('Reviewing suggestion 1 of 2'), 2)

    def test_final_summary_shared_support_and_separate_baseline(self):
        output = io.StringIO()
        with redirect_stdout(output):
            plain_plan(self.plan, self.ps, final=True)
        text = output.getvalue()
        self.assertIn('Applications selected for removal: 1', text)
        self.assertIn('Additional unused supporting items: 1', text)
        self.assertIn('will remain installed: 1', text)
        self.assertIn('Already-unneeded items: 1', text)
        self.assertIn('2.0 MB', text)
        self.assertIn('3.0 MB', text)
        self.assertNotIn('shared:amd64', text)

    def test_final_blocked_preview_never_claims_safe(self):
        self.plan.blocked = ['newly protected']
        result, output, _, _ = self.run_review(['r', 'f'], items=self.items[:1])
        self.assertEqual(result, 2)
        self.assertIn('BLOCKED', output)
        self.assertNotIn('can be removed safely', output)

    def test_final_revalidation_uses_fresh_inventory(self):
        fresh = dict(self.ps)
        result, _, simulate, _ = self.run_review(['r', 'f'], items=self.items[:1], fresh=fresh)
        self.assertIs(simulate.call_args.args[1], fresh)
        self.assertEqual(simulate.call_count, 2)

    def test_final_changed_protection_blocks_previously_valid_choice(self):
        from dataclasses import replace
        fresh = dict(self.ps)
        fresh['one:amd64'] = replace(self.ps['one:amd64'], protected=['new protection'])
        def check(names, packages, kept):
            return classify(names, names, names, Baseline([], ''), packages, kept, '')
        output = io.StringIO()
        with patch('builtins.input', side_effect=['r', 'f']), patch('package_cleaner.presentation.inventory', side_effect=[(self.ps, []), (fresh, [])]), patch('package_cleaner.presentation.simulate', side_effect=check), redirect_stdout(output):
            result = review(self.ps, candidates=self.items[:1], preferences=self.prefs)
        self.assertEqual(result, 2)
        self.assertIn('BLOCKED', output.getvalue())

    def test_affected_manual_application_requires_its_own_review_removal_choice(self):
        removed = ['one:amd64', 'two:amd64']
        blocked = classify(['one:amd64'], removed, removed, Baseline([], ''), self.ps, [], '')
        approved = classify(removed, removed, removed, Baseline([], ''), self.ps, [], '')
        with patch('builtins.input', side_effect=['r', 'f', 'a', 'r', 'f']), patch('package_cleaner.presentation.inventory', return_value=(self.ps, [])), patch('package_cleaner.presentation.simulate', side_effect=[blocked, blocked, approved]) as run, redirect_stdout(io.StringIO()):
            result = review(self.ps, candidates=self.items, preferences=self.prefs)
        self.assertEqual(result, 0)
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         [['one:amd64'], ['one:amd64'], ['one:amd64', 'two:amd64']])

    def test_baseline_savings_do_not_inflate_ranking(self):
        first = priority(self.ps['one:amd64'], Evidence(), self.plan)[0]
        self.plan.savings['already_orphaned'] = 10**12
        self.assertEqual(priority(self.ps['one:amd64'], Evidence(), self.plan)[0], first)

    def test_saved_keep_applies_to_advanced_simulation(self):
        from package_cleaner.__main__ import main
        self.prefs.keep('one:amd64')
        self.plan.blocked = ['one:amd64 kept']
        with patch('sys.argv', ['package-cleaner', '--preferences', str(self.prefs.path), 'simulate', 'one']), patch('package_cleaner.__main__.inventory', return_value=(self.ps, [])), patch('package_cleaner.__main__.simulate', return_value=self.plan) as run, redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 2)
        self.assertEqual(run.call_args.args[2], {'one:amd64'})

    def test_quit_discards_staged_selection_without_final_revalidation(self):
        result, output, simulate, inventory = self.run_review(['r', 'q'])
        self.assertEqual(result, 0)
        self.assertEqual(simulate.call_count, 1)  # Only the earlier Queue removal preview.
        self.assertEqual(inventory.call_count, 1)
        self.assertNotIn('Your cleanup summary', output)
        self.assertIn('Queued removal choices discarded', output)

    def test_finish_revalidates_and_shows_summary(self):
        result, output, simulate, inventory = self.run_review(['r', 'f', 'q'])
        self.assertEqual(simulate.call_count, 2)
        self.assertEqual(inventory.call_count, 2)
        self.assertIn('Your cleanup summary', output)

    def test_quit_preserves_keep_preferences(self):
        result, output, simulate, inventory = self.run_review(['k', 'q'])
        self.assertIn('apt:system/one:amd64', Preferences(self.prefs.path).kept)
        self.assertNotIn('Your cleanup summary', output)
        self.assertFalse(simulate.called)

    def test_eof_quits_without_finishing(self):
        with patch('builtins.input', side_effect=EOFError), patch('package_cleaner.presentation.inventory') as inventory, redirect_stdout(io.StringIO()) as output:
            review(self.ps, candidates=self.items, preferences=self.prefs)
        inventory.assert_not_called()
        self.assertTrue(output.getvalue().endswith('Goodbye.\n'))
        self.assertNotIn('discarded', output.getvalue())
        self.assertNotIn('Your cleanup summary', output.getvalue())

    def test_next_set_keeps_later_matches_accessible(self):
        output = io.StringIO()
        with patch('builtins.input', side_effect=['s', 'n', 'q']), redirect_stdout(output):
            review(self.ps, candidates=self.items, preferences=self.prefs, limit=1)
        self.assertIn('1 more in this category', output.getvalue())
        self.assertIn('Reviewing suggestion 2 of 2', output.getvalue())

    def test_legacy_yes_no_shortcuts_do_not_save_or_simulate(self):
        result, output, simulate, inventory = self.run_review(['y', 'n', 'q'])
        self.assertFalse(self.prefs.path.exists())
        simulate.assert_not_called()
        inventory.assert_not_called()
        self.assertEqual(output.count('Reviewing suggestion 1 of 2'), 3)
        for label in ('[K] Keep permanently', '[R] Queue removal', '[S] Skip',
                      '[P] Previous', '[D] Details', '[F] Finish', '[Q] Quit'):
            self.assertIn(label, output)
        self.assertNotIn('[Y]', output)
        self.assertNotIn('[N]', output)

    def test_skip_key_does_not_advance_at_set_boundary(self):
        output = io.StringIO()
        with patch('builtins.input', side_effect=['s', 's', 'invalid', 'n', 'q']), redirect_stdout(output):
            review(self.ps, candidates=self.items, preferences=self.prefs, limit=1)
        self.assertIn('Reviewing suggestion 2 of 2', output.getvalue())
        self.assertNotIn('Your cleanup summary', output.getvalue())

    def test_details_stays_short_with_thousands_of_relationships(self):
        from package_cleaner.presentation import details
        package = self.ps['one:amd64']
        package.dependencies = ['support-' + str(n) for n in range(2000)]
        package.reverse_dependencies = ['consumer-' + str(n) for n in range(2000)]
        output = io.StringIO()
        with redirect_stdout(output):
            details(package, self.ps, Evidence(latest_file_access='2020-01-01'))
        text = output.getvalue()
        self.assertLessEqual(len(text.splitlines()), 25)
        self.assertIn('Jan 1, 2020', text)
        for forbidden in ('{', 'support-1999', 'consumer-1999', 'score', 'timings'):
            self.assertNotIn(forbidden, text)

    def test_staging_is_concise_and_not_a_combined_report(self):
        from package_cleaner.presentation import staged_preview
        output = io.StringIO()
        with redirect_stdout(output):
            staged_preview(self.items[0], self.plan, self.ps)
        text = output.getvalue()
        self.assertIn('Queued for removal:', text)
        self.assertIn('Estimated recoverable space', text)
        self.assertLessEqual(len(text.splitlines()), 8)
        self.assertNotIn('Already-unneeded', text)
        self.assertNotIn('Shared supporting', text)

    def test_advanced_lists_stop_at_page_boundary(self):
        from package_cleaner.presentation import paged_lines
        output = io.StringIO()
        with patch('builtins.input', return_value='q') as prompt, redirect_stdout(output):
            paged_lines(['package-' + str(n) for n in range(1000)])
        self.assertEqual(prompt.call_count, 1)
        self.assertIn('package-17', output.getvalue())
        self.assertNotIn('package-18', output.getvalue())

    def test_advanced_details_navigation_returns_to_current_application(self):
        from package_cleaner.presentation import technical_details
        self.ps['one:amd64'].dependencies = ['support-' + str(n) for n in range(80)]
        with patch('package_cleaner.presentation.technical_details', wraps=technical_details) as technical:
            result, output, simulate, inventory = self.run_review(['d', 't', 'n', 'q', 'q'])
        technical.assert_called_once_with(self.ps['one:amd64'], self.ps)
        self.assertIn('[T] Advanced package information', output)
        self.assertIn('[K/R/S/P/D/T/F/Q]', output)
        self.assertIn('support-20', output)
        self.assertNotIn('Reviewing suggestion 2', output)
        self.assertNotIn('Keep it if you recognize', output)
        simulate.assert_not_called()
        inventory.assert_not_called()

    def test_n_and_next_are_not_application_skip_aliases(self):
        result, output, simulate, inventory = self.run_review(['n', 'next', 's', 'q'])
        self.assertEqual(output.count('Reviewing suggestion 1 of 2'), 3)
        self.assertEqual(output.count('Reviewing suggestion 2 of 2'), 1)
        simulate.assert_not_called()
        inventory.assert_not_called()

    def test_s_at_boundary_reprompts_without_advancing(self):
        output = io.StringIO()
        with patch('builtins.input', side_effect=['s', 's', 'q']), redirect_stdout(output):
            review(self.ps, candidates=self.items, preferences=self.prefs, limit=1)
        self.assertIn('[N] Next set', output.getvalue())
        self.assertNotIn('Reviewing suggestion 2', output.getvalue())
        self.assertNotIn('Your cleanup summary', output.getvalue())
        self.assertNotIn('[S] Next set', output.getvalue())

    def test_details_is_optional(self):
        with patch('package_cleaner.presentation.details') as details:
            result, output, simulate, inventory = self.run_review(['d', 'f'])
        self.assertEqual(details.call_count, 1)
        self.assertFalse(simulate.called)
        self.assertNotIn('one:amd64', output)


if __name__ == '__main__':
    unittest.main()
