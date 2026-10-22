# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
import io
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from package_cleaner.__main__ import main
from package_cleaner.inventory import Package
from package_cleaner.preferences import Preferences
from package_cleaner.presentation import review
from package_cleaner.progress import Progress
from package_cleaner.recommendations import Evidence, audit_recommendations as recommend


class AnalysisFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.prefs = Preferences(Path(self.temp.name)/'preferences.json')
        self.ps = {f'app{i}:amd64': Package(f'app{i}:amd64',f'app{i}','1',False,100,'App','misc',application=True)
                   for i in range(161)}

    def failure_scan(self, **kwargs):
        with patch('package_cleaner.recommendations.running_executables',return_value=set()), patch(
                'package_cleaner.recommendations.collect_evidence',return_value=Evidence(executable_count=1,old_accesses=1)), patch(
                'package_cleaner.recommendations.simulate',side_effect=ValueError('Package state changed since inventory; scan again.')):
            return recommend(self.ps, category='potential', **kwargs)

    def test_161_failures_are_analysis_failure_not_zero_opportunities(self):
        d={}; output=io.StringIO()
        self.assertEqual(self.failure_scan(diagnostics=d,progress=Progress(output)),[])
        self.assertEqual(d['status'],'failed')
        self.assertEqual(len(d['failed']),161)
        self.assertIn('Analysis failed: 161',output.getvalue())
        self.assertNotIn('Found 0',output.getvalue())
        self.assertTrue(all('Package state changed' in x['error'] for x in d['failed']))

    def test_review_refreshes_inventory_and_exposes_exact_errors(self):
        fresh = dict(self.ps)
        seen=[]
        def fail(packages,*args,**kwargs):
            seen.append(packages)
            kwargs['diagnostics'].update(status='failed',blocked=[],failed=[{'package':'app0','error':'APT simulation failed (100): exact reason'}])
            return []
        output=io.StringIO()
        with patch('package_cleaner.presentation.inventory',return_value=(fresh,[])), patch(
                'package_cleaner.recommendations.recommend',side_effect=fail), patch(
                'builtins.input',side_effect=['d','q']), redirect_stdout(output):
            result=review(self.ps,category='potential',preferences=self.prefs)
        self.assertIs(seen[0],fresh)
        self.assertEqual(result,1)
        self.assertIn('Analysis failed',output.getvalue())
        self.assertIn('exact reason',output.getvalue())
        self.assertNotIn('No reviewable',output.getvalue())

    def test_retry_loads_inventory_again(self):
        calls=[]
        def fail(packages,*args,**kwargs):
            calls.append(packages)
            kwargs['diagnostics'].update(status='failed',blocked=[],failed=[{'package':'app0','error':'failure'}])
            return []
        second=dict(self.ps)
        with patch('package_cleaner.presentation.inventory',side_effect=[(self.ps,[]),(second,[])]), patch(
                'package_cleaner.recommendations.recommend',side_effect=fail), patch(
                'builtins.input',side_effect=['r','q']),redirect_stdout(io.StringIO()):
            self.assertEqual(review(None,category='potential',preferences=self.prefs),1)
        self.assertIs(calls[0],self.ps);self.assertIs(calls[1],second)

    def test_partial_failure_not_reported_as_complete(self):
        stream=io.StringIO();Progress(stream).finish(4,failed=2,attempted=6)
        self.assertIn('Found 4',stream.getvalue())
        self.assertIn('Analysis incomplete',stream.getvalue())

    def test_empty_success_not_an_analysis_failure(self):
        with patch('package_cleaner.recommendations.running_executables',return_value=set()), patch(
                'package_cleaner.recommendations.collect_evidence',return_value=Evidence()):
            d={};stream=io.StringIO()
            self.assertFalse(recommend(self.ps,category='potential',diagnostics=d,progress=Progress(stream)))
        self.assertEqual(d['status'],'complete')
        self.assertIn('Found 0',stream.getvalue())

    def test_scan_returns_failure_status_and_keeps_details(self):
        def fail(*args,**kwargs):
            kwargs['diagnostics'].update(status='failed',blocked=[],failed=[{'package':'app0','error':'actual failure'}])
            return []
        output=io.StringIO()
        with patch('sys.argv',['package-cleaner','--preferences',str(self.prefs.path),'scan','--category','potential','--diagnostics']), patch(
                'package_cleaner.__main__.inventory',return_value=(self.ps,[])),patch(
                'package_cleaner.__main__.recommend',side_effect=fail),redirect_stdout(output):
            self.assertEqual(main(),1)
        self.assertIn('actual failure',output.getvalue())
        self.assertIn('Analysis failed',output.getvalue())
        self.assertNotIn('No reviewable',output.getvalue())

    def test_no_arguments_opens_menu_before_inventory(self):
        output=io.StringIO()
        with patch('sys.argv',['package-cleaner']),patch('builtins.input',return_value='q'),patch(
                'package_cleaner.__main__.inventory') as inv,redirect_stdout(output):
            self.assertEqual(main(),0)
        inv.assert_not_called()
        self.assertIn('Choose a category:',output.getvalue())
        self.assertTrue(output.getvalue().endswith('Goodbye.\n'))

    def test_help_does_not_start_analysis(self):
        with patch('sys.argv',['package-cleaner','--help']),patch('package_cleaner.__main__.inventory') as inv,redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as result: main()
        self.assertEqual(result.exception.code,0)
        inv.assert_not_called();self.assertIn('usage:',output.getvalue())

    def test_inventory_failure_has_details_and_retry_in_review(self):
        output=io.StringIO()
        with patch('package_cleaner.presentation.inventory',side_effect=OSError('inventory permission denied')),patch(
                'builtins.input',side_effect=['d','q']),redirect_stdout(output),redirect_stderr(io.StringIO()):
            self.assertEqual(review(None,category='potential',preferences=self.prefs),1)
        self.assertIn('inventory permission denied',output.getvalue())
        self.assertNotIn('No reviewable',output.getvalue())

    def test_fingerprint_failure_identifies_changed_input_and_rejects_stale_snapshot(self):
        import os
        from package_cleaner.state import package_state, PackageSnapshot
        from package_cleaner.removal import simulate
        config = Path(self.temp.name)/'apt.conf'
        config.write_text('// first revision\n')
        with patch.dict(os.environ, {'APT_CONFIG':str(config)}):
            packages = PackageSnapshot(self.ps)
            packages.state_token = package_state()
            config.write_text('// a different revision\n')
            with patch('package_cleaner.removal.apt_simulation') as apt:
                with self.assertRaisesRegex(ValueError, 'since inventory') as error:
                    simulate(['app0:amd64'], packages)
            apt.assert_not_called()
        self.assertIn(str(config),str(error.exception))

    def test_unsupported_apt_operation_preserves_actual_record(self):
        from package_cleaner.removal import parse_simulation
        with self.assertRaises(ValueError) as error:
            parse_simulation('Inst replacement [1] (2 Ubuntu)\n', self.ps)
        self.assertIn('Inst replacement [1] (2 Ubuntu)', str(error.exception))

    def test_tty_and_redirected_analysis_failure_are_consistent(self):
        class TTY(io.StringIO):
            def isatty(self): return True
        for stream in (io.StringIO(), TTY()):
            self.failure_scan(progress=Progress(stream))
            self.assertIn('Analysis failed: 161',stream.getvalue())
            self.assertNotIn('Found 0',stream.getvalue())

    def test_final_selection_failure_keeps_exact_reason_accessible(self):
        from package_cleaner.recommendations import Recommendation
        from package_cleaner.removal import classify, Baseline
        name='app0:amd64'
        plan=classify([name],[name],[name],Baseline([],''),self.ps,[], '')
        item=Recommendation(name,'App','Purpose','Keep reason',Evidence(),1,[],plan)
        output=io.StringIO()
        with patch('package_cleaner.presentation.inventory',return_value=(self.ps,[])),patch(
                'package_cleaner.presentation.simulate',side_effect=ValueError('exact selection failure')),patch(
                'builtins.input',side_effect=['r','d','q']),redirect_stdout(output):
            self.assertEqual(review(self.ps,candidates=[item],preferences=self.prefs),2)
        self.assertIn('exact selection failure',output.getvalue())
        self.assertIn('Analysis failed',output.getvalue())

    def test_supporting_category_failure_offers_details(self):
        output=io.StringIO()
        with patch('package_cleaner.presentation.inventory',return_value=(self.ps,[])),patch(
                'package_cleaner.removal.establish_baseline',side_effect=ValueError('exact baseline failure')),patch(
                'builtins.input',side_effect=['d','q']),redirect_stdout(output):
            self.assertEqual(review(None,category='support',preferences=self.prefs),1)
        self.assertIn('exact baseline failure',output.getvalue())
        self.assertNotIn('Found 0',output.getvalue())
