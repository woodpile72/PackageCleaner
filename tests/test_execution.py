# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from unittest.mock import patch
from package_cleaner.execution import (run_validated, inventory_record, plan_record, preference_state,
                                       launch, RESULT_PREFIX)
from package_cleaner.inventory import Package
from package_cleaner.removal import simulate
from package_cleaner.presentation import execute_selection, review
from package_cleaner.preferences import Preferences
from package_cleaner.recommendations import Recommendation, Evidence


class FakeBackend:
    def __init__(self, packages, plan):
        self.packages, self.plan = packages, plan
        self.executions = 0
        self.config = 'normal config'
        self.token = 'stable'
        self.states = ['installed']
        self.prepare_error = None
        self.execute_error = None
        self.exit_code = 0
        self.absent = set(plan.removed)
        self.prepared = None
        self.locked = False

    def lock(self):
        backend = self
        class Lock:
            def __enter__(self): backend.locked = True
            def __exit__(self, *args): backend.locked = False
        return Lock()
    def configuration(self): return self.config
    def state_token(self): return self.token
    def status(self): return self.states
    def inventory(self): return self.packages
    def simulate(self, *args): return self.plan
    def prepare(self, targets):
        assert self.locked
        if self.prepare_error: raise self.prepare_error
        self.prepared = targets
    def execute(self):
        assert self.locked
        self.executions += 1
        if self.execute_error: raise self.execute_error
        return self.exit_code
    def observed(self, names):
        return {n: 'not-installed' if self.executions and n in self.absent else 'installed' for n in names}


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.prefs = Preferences(Path(self.tmp.name)/'prefs.json')
        app = Package('app:amd64', 'app', '1', False, 100, 'Application', 'graphics', application=True)
        dep = Package('dep:amd64', 'dep', '1', True, 20, 'Support', 'libs')
        orphan = Package('system:i386', 'system', '1', True, 30, 'System', 'libs', protected=['system'])
        self.packages = {p.name:p for p in (app,dep,orphan)}
        with patch('package_cleaner.removal.package_state', return_value='stable'), patch(
                'package_cleaner.removal.apt_simulation', side_effect=[([orphan.name],''),([app.name],''),
                  ([app.name,dep.name,orphan.name],''),([app.name,dep.name],'')]):
            self.plan = simulate([app.name], self.packages)
        self.record = {'version':1,'preferences_path':str(self.prefs.path),
                       'preferences':preference_state(self.prefs.path),'state_token':'stable',
                       'inventory':inventory_record(self.packages),'dpkg_state':['installed'],
                       'apt_configuration':'normal config','plan':copy.deepcopy(plan_record(self.plan))}
        self.backend = FakeBackend(self.packages,self.plan)

    def run_engine(self): return run_validated(self.record,self.backend)
    def rejected(self):
        result=self.run_engine()
        self.assertEqual(result['status'],'rejected',result)
        self.assertEqual(self.backend.executions,0)
        return result

    def test_success_exact_transaction_leaves_protected_baseline(self):
        result=self.run_engine()
        self.assertEqual(result['status'],'completed')
        self.assertEqual(self.backend.prepared,['app:amd64','dep:amd64'])
        self.assertNotIn('system:i386',result['removed'])
        self.assertFalse(self.backend.locked)

    def test_changed_version(self):
        self.packages['app:amd64'].version='2';self.rejected()
    def test_changed_manual_flag(self):
        self.packages['dep:amd64'].automatic=False;self.rejected()
    def test_changed_keep(self):
        self.prefs.keep('app:amd64');self.rejected()
    def test_changed_fingerprint(self):
        self.backend.token='changed';self.rejected()
    def test_changed_installation_state(self):
        self.backend.states=['half-configured'];self.rejected()
    def test_changed_privileged_config(self):
        self.backend.config='other';self.rejected()
    def test_changed_resolution(self):
        self.plan.newly_orphaned=[];self.rejected()
    def test_unexpected_additional_removal(self):
        self.plan.removed.append('system:i386');self.rejected()
    def test_unexpected_install_or_upgrade(self):
        for action in ('install','upgrade','purge'):
            self.backend.prepare_error=ValueError('Unexpected '+action)
            self.rejected()
    def test_protection_rechecked(self):
        self.packages['app:amd64'].protected=['desktop'];self.rejected()
    def test_independent_protection_even_if_saved_plan_claims_safe(self):
        self.packages['app:amd64'].protected=['desktop']
        self.record['inventory']=inventory_record(self.packages)
        self.rejected()
    def test_unselected_manual_even_if_saved_plan_claims_safe(self):
        self.packages['dep:amd64'].automatic=False
        self.record['inventory']=inventory_record(self.packages)
        self.rejected()
    def test_permission_failure(self):
        self.backend.lock=lambda: (_ for _ in ()).throw(PermissionError('lock denied'))
        self.rejected()
    def test_partial_failure_records_observed_outcome(self):
        self.backend.exit_code=1;self.backend.absent={'app:amd64'}
        result=self.run_engine()
        self.assertEqual(result['status'],'incomplete')
        self.assertEqual(result['removed'],['app:amd64'])
        self.assertEqual(result['remaining'],['dep:amd64'])
        self.assertEqual(self.backend.executions,1)
    def test_interruption_never_retries(self):
        self.backend.execute_error=KeyboardInterrupt();self.backend.absent={'app:amd64'}
        result=self.run_engine()
        self.assertEqual(result['status'],'incomplete')
        self.assertEqual(self.backend.executions,1)
    def test_unknown_observation_never_success(self):
        self.backend.observed=lambda _: (_ for _ in ()).throw(OSError('unreadable'))
        self.assertEqual(self.run_engine()['status'],'unknown')
    def test_inputs_rechecked_after_preparation(self):
        original=self.backend.prepare
        def prepare(targets):
            original(targets);self.backend.token='changed'
        self.backend.prepare=prepare
        self.rejected()

    def test_cancel_confirmation_does_not_launch_or_request_privileges(self):
        for text in ('','remove','REMOVE ','yes','q'):
            with patch('package_cleaner.execution.approval_record',return_value=self.record), patch(
                    'package_cleaner.execution.launch') as run, patch('builtins.input',return_value=text),redirect_stdout(io.StringIO()):
                self.assertIsNone(execute_selection(self.plan,self.packages,self.prefs))
            run.assert_not_called()

    def test_exact_confirmation_launches_fake_only(self):
        with patch('package_cleaner.execution.approval_record',return_value=self.record), patch(
                'package_cleaner.execution.launch',return_value={'status':'completed','helper_verified':True,'removed':self.plan.removed,'log_directory':'test'}) as run,patch(
                'builtins.input',return_value='REMOVE'),redirect_stdout(io.StringIO()) as output:
            self.assertEqual(execute_selection(self.plan,self.packages,self.prefs),0)
        run.assert_called_once_with(self.record)
        self.assertIn('Actual disk space recovery was not measured',' '.join(output.getvalue().split()))

    def test_logging_restrictive_and_preserves_output(self):
        def fake(command,output,record):
            self.assertIsInstance(command,list)
            output.write('raw APT failure\n')
            from package_cleaner.result_protocol import send_result
            send_result(record, {'status':'incomplete','error':'failed','removed':[], 'remaining':record['plan']['removed'],'unknown':[]})
            return 1
        result=launch(self.record,Path(self.tmp.name)/'logs',runner=fake)
        directory=Path(result['log_directory'])
        self.assertEqual(directory.stat().st_mode & 0o777,0o700)
        for name in ('approval.json.gz','execution.log','result.json'):
            self.assertEqual((directory/name).stat().st_mode & 0o777,0o600)
        self.assertIn('raw APT failure',(directory/'execution.log').read_text())
        self.assertEqual(result['status'],'incomplete')
        self.assertEqual(result['process_exit_status'],1)

    def test_sudo_failure_never_claims_success(self):
        result=launch(self.record,Path(self.tmp.name)/'logs',runner=lambda cmd,out,record:1)
        self.assertEqual(result['status'],'unknown')

    def test_read_only_default_hides_execution(self):
        item=Recommendation('app:amd64','App','Purpose','',Evidence(),0,[],plan=self.plan)
        with patch('builtins.input',side_effect=['r','e','q']),patch('package_cleaner.presentation.inventory',return_value=(self.packages,[])),patch(
                'package_cleaner.presentation.simulate',return_value=self.plan),patch('package_cleaner.presentation.execute_selection') as execute,redirect_stdout(io.StringIO()) as output:
            review(self.packages,candidates=[item],preferences=self.prefs)
        execute.assert_not_called();self.assertNotIn('[E]',output.getvalue())

    def test_cli_opt_in_both_positions(self):
        from package_cleaner.__main__ import main
        for args in (['--enable-removal','review'],['review','--enable-removal'],['--enable-removal']):
            with patch('sys.argv',['package-cleaner',*args]),patch('package_cleaner.__main__.choose_category',return_value='large'),patch(
                    'package_cleaner.__main__.review',return_value=0) as run:
                self.assertEqual(main(),0)
            self.assertTrue(run.call_args.kwargs['enable_removal'])

    def test_changed_preferences_after_preparation(self):
        self.backend.prepare=lambda targets:self.prefs.keep('app:amd64')
        self.rejected()

    def test_existing_orphan_cannot_be_hidden_in_support(self):
        self.packages['system:i386'].protected=[]
        self.record['inventory']=inventory_record(self.packages)
        self.plan.removed.append('system:i386')
        self.plan.newly_orphaned.append('system:i386')
        self.record['plan']=copy.deepcopy(plan_record(self.plan))
        self.rejected()

    def test_execute_hidden_for_blocked_plan_even_when_enabled(self):
        self.plan.blocked=['protected']
        item=Recommendation('app:amd64','App','Purpose','',Evidence(),0,[],plan=self.plan)
        with patch('builtins.input',side_effect=['r','e','q']),patch('package_cleaner.presentation.inventory',return_value=(self.packages,[])),patch(
                'package_cleaner.presentation.simulate',return_value=self.plan),patch('package_cleaner.presentation.execute_selection') as execute,redirect_stdout(io.StringIO()) as output:
            review(self.packages,candidates=[item],preferences=self.prefs,enable_removal=True)
        execute.assert_not_called();self.assertNotIn('[E]',output.getvalue())

    def test_back_preserves_selection_without_executing(self):
        item=Recommendation('app:amd64','App','Purpose','',Evidence(),0,[],plan=self.plan)
        with patch('builtins.input',side_effect=['r','b','q']),patch('package_cleaner.presentation.inventory',return_value=(self.packages,[])),patch(
                'package_cleaner.presentation.simulate',return_value=self.plan),patch('package_cleaner.presentation.execute_selection') as execute,redirect_stdout(io.StringIO()) as output:
            review(self.packages,candidates=[item],preferences=self.prefs,enable_removal=True)
        execute.assert_not_called()
        self.assertIn('[E] Execute removal',output.getvalue())
        self.assertIn('Currently included in your cleanup preview',output.getvalue())

    def test_unknown_installation_outcome_is_not_counted_as_removed(self):
        self.backend.observed=lambda names:{n:'incomplete' for n in names}
        result=self.run_engine()
        self.assertEqual(result['status'],'incomplete')
        self.assertFalse(result['removed'])
        self.assertEqual(set(result['unknown']),set(self.plan.removed))

    def test_xdg_state_directory(self):
        with patch.dict(os.environ,{'XDG_STATE_HOME':self.tmp.name}):
            result=launch(self.record,runner=lambda command,output,record:1)
        self.assertEqual(Path(result['log_directory']).parent,Path(self.tmp.name)/'package-cleaner')

    def test_locked_apt_actions_are_independently_checked(self):
        from types import SimpleNamespace
        from package_cleaner.privileged import validate_changes
        p=SimpleNamespace(fullname='app:amd64',marked_delete=True,marked_install=False,
                          marked_upgrade=False,marked_downgrade=False,marked_reinstall=False)
        cache=SimpleNamespace(broken_count=0,get_changes=lambda:[p])
        validate_changes(cache,['app:amd64'])
        for action in ('marked_install','marked_upgrade','marked_downgrade','marked_reinstall'):
            setattr(p,action,True)
            with self.assertRaises(ValueError):validate_changes(cache,['app:amd64'])
            setattr(p,action,False)
        with self.assertRaises(ValueError):validate_changes(cache,['different:amd64'])
        p.marked_delete=False
        with self.assertRaises(ValueError):validate_changes(cache,['app:amd64'])

    def test_backend_will_not_execute_without_system_lock(self):
        from package_cleaner.privileged import AptBackend
        from types import SimpleNamespace
        backend=AptBackend.__new__(AptBackend)
        backend.apt_pkg=SimpleNamespace(pkgsystem_is_locked=lambda:False)
        with self.assertRaisesRegex(ValueError,'lock'):
            backend.execute()

    def test_configuration_changes_after_preparation(self):
        self.backend.prepare=lambda targets:setattr(self.backend,'config','changed')
        self.rejected()

    def test_sudo_os_error_is_logged_without_claiming_success(self):
        def denied(command,output,record):raise PermissionError('permission denied')
        result=launch(self.record,Path(self.tmp.name)/'logs',runner=denied)
        self.assertNotEqual(result['status'],'completed')
        self.assertIn('permission denied',result['error'])
        self.assertTrue((Path(result['log_directory'])/'result.json').exists())

    def test_approval_rejects_stale_review_before_privilege_request(self):
        from package_cleaner.execution import approval_record
        with patch('package_cleaner.execution.package_state',return_value='changed'):
            with self.assertRaisesRegex(ValueError,'changed'):
                approval_record(self.plan,self.packages,self.prefs)

    def test_final_observation_keeps_system_lock(self):
        original = self.backend.observed
        def observed(names):
            self.assertTrue(self.backend.locked)
            return original(names)
        self.backend.observed = observed
        self.assertEqual(self.run_engine()['status'], 'completed')
        self.assertFalse(self.backend.locked)

    def test_extra_action_detected_immediately_before_execution(self):
        from package_cleaner.privileged import AptBackend
        from types import SimpleNamespace
        backend = AptBackend.__new__(AptBackend)
        backend.apt_pkg = SimpleNamespace(pkgsystem_is_locked=lambda: True)
        backend.targets = ['app:amd64']
        backend.cache = SimpleNamespace(broken_count=0, get_changes=lambda: [SimpleNamespace(fullname='unapproved:amd64')])
        with self.assertRaisesRegex(ValueError, 'differs'):
            backend.execute()

    def test_unverified_completion_never_displays_success(self):
        with patch('package_cleaner.execution.approval_record', return_value=self.record), patch(
                'package_cleaner.execution.launch', return_value={'status':'completed', 'helper_verified':False,
                'removed':[], 'log_directory':'test'}), patch('builtins.input', return_value='REMOVE'), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(execute_selection(self.plan,self.packages,self.prefs), 2)
        self.assertNotIn('Cleanup complete.', output.getvalue())
        self.assertIn('reconciliation required', output.getvalue())
