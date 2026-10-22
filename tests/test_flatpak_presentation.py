# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Presentation regressions and a signal-only upstream GI diagnostic; no run()."""
import copy
import io
import unittest
import warnings
from contextlib import redirect_stdout
from unittest.mock import patch
from package_cleaner.display import installation_label
from package_cleaner.presentation import cleanup_summary, execute_selection, show_plan
from package_cleaner.core.removal import Plan, Baseline


class FlatpakPresentationTests(unittest.TestCase):
    def setUp(self):
        from test_flatpak_execution import FlatpakExecutionTests
        self.fixture=FlatpakExecutionTests()
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        f=self.fixture
        self.ps=f.ps;self.prefs=f.prefs
        selected=[n for n,p in self.ps.items() if p.installation.startswith('system/default')]
        self.part=f.provider.preview(selected,self.ps)[0]
        self.aggregate=Plan(self.part.requested, self.part.removed,[],[],[],[],[],self.part.savings,[],'',Baseline([],''),provider='flatpak',provider_plans=[self.part])
    def test_friendly_labels(self):
        self.assertEqual(installation_label('system/default@%2Fvar%2Flib%2Fflatpak'),'System installation (default)')
        self.assertEqual(installation_label('user@%2Fhome%2Fexample'),'User installation')
        self.assertEqual(installation_label('system/games@%2Fgames'),'System installation (games)')
        self.assertEqual(installation_label('bad'),'Unknown installation')
    def test_single_summary_and_confirmation_no_encoded_identifiers(self):
        with redirect_stdout(io.StringIO()) as output,patch('builtins.input',return_value='cancel'),patch('package_cleaner.core.authorization.refresh_execution_part',return_value=(self.part,self.ps)),patch('package_cleaner.core.authorization.execute_confirmed') as execute:
            cleanup_summary(self.aggregate,self.ps)
            execute_selection(self.aggregate,self.ps,self.prefs,summary_shown=True)
        text=output.getvalue()
        self.assertEqual(text.count('Your cleanup summary'),1)
        self.assertEqual(text.count('System installation (default)'),1)
        self.assertEqual(text.count('Type REMOVE'),1)
        self.assertNotIn('%2F',text);self.assertNotIn('independent transactions',text)
        self.assertIn('Potential recovery upper bound',text)
        self.assertIn('supporting packages: 0',text)
        execute.assert_not_called()
    def test_changed_proposal_is_displayed_again(self):
        changed=copy.deepcopy(self.part);changed.savings['selected']+=1024
        with redirect_stdout(io.StringIO()) as output,patch('builtins.input',return_value='cancel'),patch('package_cleaner.core.authorization.refresh_execution_part',return_value=(changed,self.ps)):
            cleanup_summary(self.aggregate,self.ps)
            execute_selection(self.aggregate,self.ps,self.prefs,summary_shown=True)
        self.assertEqual(output.getvalue().count('Your cleanup summary'),2)
        self.assertIn('proposal changed',output.getvalue())
    def test_advanced_details_preserve_exact_identity(self):
        with redirect_stdout(io.StringIO()) as output:show_plan(self.part)
        self.assertIn(self.part.installation,output.getvalue())
        self.assertIn(self.part.operations[0]['ref'],output.getvalue())
    def test_direct_confirmation_still_shows_proposal(self):
        with redirect_stdout(io.StringIO()) as output,patch('builtins.input',return_value='cancel'):
            execute_selection(self.part,self.ps,self.prefs)
        self.assertEqual(output.getvalue().count('Your cleanup summary'),1)


class FlatpakSignalDiagnosticTests(unittest.TestCase):
    def test_completion_signature_without_running_transaction(self):
        from package_cleaner.core.flatpak_provider import load_api
        api=load_api()
        if not api:self.skipTest('System libflatpak GI bindings unavailable')
        F,Gio,GLib,GObject=api
        tx=F.Transaction.new_for_installation(F.Installation.new_user(None),None)
        seen=[]
        # Exact arity used by PackageCleaner; the fourth argument is not used to
        # declare success. No operation is queued and run() is never called.
        tx.connect('operation-done',lambda transaction,op,commit,result:seen.append((op,commit)))
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter('always')
            tx.emit('operation-done',None,None,0)
        self.assertEqual(seen,[(None,None)])
        self.assertTrue(all('g_value_get_flags' in str(w.message) for w in captured))
        # Some future library versions may fix the annotation/runtime mismatch.
        # Do not require the upstream warning to persist, and never suppress it
        # in application execution code.
        self.assertEqual(len(GObject.signal_query('operation-done',F.Transaction).param_types),3)
    def test_cold_process_initializes_signals_before_capability_check(self):
        import subprocess,sys
        from package_cleaner.core.flatpak_provider import load_api
        if not load_api():self.skipTest('Flatpak bindings unavailable')
        result=subprocess.run([sys.executable,'-c',
            'from package_cleaner.core.flatpak_provider import load_api,execution_supported; '
            'assert execution_supported(load_api()), "Cold-process capability detection failed"'],
            capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
