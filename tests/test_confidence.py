# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
import unittest
from unittest.mock import patch

from package_cleaner.inventory import Package
from package_cleaner.recommendations import Evidence, inactivity_assessment, potential_clue, audit_recommendations as recommend
from package_cleaner.removal import Baseline, classify


class ConfidenceTests(unittest.TestCase):
    def setUp(self):
        p = Package('app:amd64', 'app', '1', False, 100, 'App', 'misc', application=True)
        other = Package('other:amd64', 'other', '1', False, 100, 'Other', 'misc')
        self.ps = {p.name:p, other.name:other}

    def run_scan(self, evidence, plan):
        diagnostics = {}
        with patch('package_cleaner.recommendations.running_executables', return_value=set()), patch('package_cleaner.recommendations.collect_evidence', return_value=evidence), patch('package_cleaner.recommendations.simulate', return_value=plan):
            result = recommend(self.ps, category='potential', diagnostics=diagnostics)
        return result, diagnostics

    def test_old_access_does_not_require_install_date(self):
        e = Evidence(executable_count=2, old_accesses=2, recorded_age_days=None)
        self.assertEqual(inactivity_assessment(e)['level'], 'weak_inactivity')
        self.assertIsNotNone(potential_clue(e))
        self.assertIn('not a reliable usage history', potential_clue(e))

    def test_recent_install_is_context_not_a_veto_for_old_access(self):
        e = Evidence(executable_count=1, old_accesses=1, recorded_age_days=10)
        self.assertIsNotNone(potential_clue(e))

    def test_running_and_enabled_are_not_activity(self):
        e = Evidence(executable_count=1, old_accesses=1, running=True,
                     services=[{'active':'active', 'enabled':'enabled', 'journal_entry':True}])
        self.assertFalse(e.activity)
        self.assertEqual(inactivity_assessment(e)['level'], 'weak_inactivity')
        self.assertIn('Removal would stop it', inactivity_assessment(e)['cautions'][0])
        plan = classify(['app:amd64'], ['app:amd64'], ['app:amd64'], Baseline([], ''), self.ps, [], '')
        result, diagnostics = self.run_scan(e, plan)
        self.assertEqual(len(result), 1)
        self.assertEqual(diagnostics['actionable_count'], 1)

    def test_missing_everything_stays_unknown(self):
        self.assertEqual(inactivity_assessment(Evidence())['level'], 'unknown')
        self.assertIsNone(potential_clue(Evidence()))
        self.assertEqual(inactivity_assessment(Evidence(running=True))['level'], 'unknown')

    def test_request_records_take_precedence_over_old_timestamps(self):
        e = Evidence(executable_count=1, old_accesses=1,
                     server_observations={'application_activity': {'recent_request_records':8}})
        self.assertEqual(inactivity_assessment(e)['level'], 'recent_activity')
        self.assertIsNone(potential_clue(e))

    def test_weak_clues_never_promoted_to_strong(self):
        e = Evidence(executable_count=1, old_accesses=1, recorded_age_days=2000,
                     services=[{'active':'inactive', 'enabled':'disabled'}])
        self.assertEqual(inactivity_assessment(e)['level'], 'weak_inactivity')

    def test_config_uncertainty_preserves_clue_but_adds_caution(self):
        e = Evidence(executable_count=1, old_accesses=1, modified_configuration=1, unreadable_configuration=1)
        self.assertEqual(inactivity_assessment(e)['level'], 'weak_inactivity')
        self.assertEqual(len(inactivity_assessment(e)['cautions']), 2)

    def test_unsafe_removal_not_actionable_but_diagnostic_is_preserved(self):
        names = ['app:amd64','other:amd64']
        plan = classify(['app:amd64'], names, names, Baseline([], ''), self.ps, [], '')
        result, diagnostics = self.run_scan(Evidence(executable_count=1,old_accesses=1), plan)
        self.assertFalse(result)
        self.assertEqual(len(diagnostics['blocked']), 1)
        self.assertEqual(diagnostics['blocked'][0]['package'], 'app:amd64')
        self.assertIn('not explicitly selected', diagnostics['blocked'][0]['reasons'][0])

    def test_protected_and_kept_removal_still_blocked(self):
        self.ps['other:amd64'].protected = ['desktop']
        names = ['app:amd64','other:amd64']
        plan = classify(['app:amd64'], names, names, Baseline([], ''), self.ps, ['app:amd64'], '')
        result, diagnostics = self.run_scan(Evidence(executable_count=1,old_accesses=1), plan)
        self.assertFalse(result)
        self.assertTrue(any('desktop' in reason for reason in diagnostics['blocked'][0]['reasons']))
        self.assertTrue(any('Keep' in reason for reason in diagnostics['blocked'][0]['reasons']))

    def test_recent_read_conflict_is_unknown_not_proven_activity(self):
        e = Evidence(recent_accesses=1, services=[{'active':'inactive','enabled':'disabled'}])
        self.assertEqual(inactivity_assessment(e)['level'], 'unknown')


if __name__ == '__main__':
    unittest.main()
