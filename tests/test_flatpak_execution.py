# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""All execution is fake. Real libflatpak transactions only run in aborting previews."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from types import SimpleNamespace as NS
from unittest.mock import patch
from test_providers import Installation, Ref, Transaction, api, Error
from package_cleaner.core.flatpak_provider import FlatpakProvider, identity, installation_id
from package_cleaner.core.flatpak_execution import approval_record, execute, state
from package_cleaner.core.authorization import execute_confirmed, ExecutionResult
from package_cleaner.core.preferences import Preferences
from package_cleaner.core.operations import CleanerError, CancellationToken, operation


class ExecutingTransaction(Transaction):
    def __init__(self,inst,options):
        super().__init__(); self.inst=inst; self.options=options
    def run(self,cancel):
        if not self.signals['ready-pre-auth'](self): raise Error('Aborted at pre-auth')
        if self.options.get('auth'): raise Error(self.options['auth'])
        if self.options.get('change_ready'):
            self.refs.append('runtime/org.example.Unapproved/x86_64/stable')
        if not self.signals['ready'](self): raise Error('Aborted at ready')
        for i,op in enumerate(self.get_operations()):
            if cancel.cancelled: raise Error('Cancelled')
            self.signals['new-operation'](self,op,None)
            if cancel.cancelled: raise Error('Cancelled')
            if self.options.get('fail_at')==i:
                self.signals['operation-error'](self,op,Error('operation failed'),0)
                raise Error('Failed')
            if not self.options.get('no_change'):
                self.inst.refs[:]=[r for r in self.inst.refs if r.format_ref()!=op.get_ref()]
            self.signals['operation-done'](self,op,None,0)
            if self.options.get('cancel_after'):
                self.options['cancel_after'].cancel()
        if self.options.get('unexpected'):
            self.inst.refs.append(Ref('app/org.example.Unapproved/x86_64/stable'))
        if self.options.get('missing_result'): return None
        return True


class FlatpakExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.prefs=Preferences(self.root/'prefs.json')
        self.running=patch('package_cleaner.core.flatpak_provider.running_applications',return_value=set())
        self.running.start();self.addCleanup(self.running.stop)
        Transaction.extra=[];Transaction.unused_extra=[];Transaction.failure=None;Transaction.before_ready=None
        self.user=Installation('user',[Ref(),Ref('app/org.example.Second/x86_64/stable')])
        self.system=Installation('default',[Ref()]); self.named=Installation('games',[Ref()])
        self.api=api([self.user,self.system,self.named]); self.provider=FlatpakProvider(self.api)
        self.ps,_=self.provider.discover();self.options={}
    def approve(self,inst=None,refs=None):
        inst=inst or self.user
        selected=[identity(installation_id(inst),r.format_ref()) for r in (refs or inst.refs[:1])]
        self.ps,_=self.provider.discover()
        plan=self.provider.preview(selected,self.ps)[0]
        return approval_record(plan,self.ps,self.prefs)
    def run_fake(self,record):
        self.api[0].Transaction.new_for_installation=lambda inst,_:ExecutingTransaction(inst,self.options)
        return execute(record,provider=self.provider,log_directory=self.root/'logs')
    def test_exact_user_system_and_named_installation(self):
        for inst in (self.user,self.system,self.named):
            with self.subTest(installation=inst.key):
                record=self.approve(inst);before=state(self.ps)
                result=self.run_fake(record)
                self.assertTrue(ExecutionResult(result).completed,result)
                self.assertEqual(result['removed'],record['selected'])
                self.assertFalse(Transaction.last.settings['set_force_uninstall'])
                self.assertFalse(Transaction.last.settings['set_include_unused_uninstall_ops'])
                after,_=self.provider.discover()
                self.assertEqual(set(before)-set(after),set(record['selected']))
                self.api[0].Transaction.new_for_installation=lambda *_:Transaction()
    def test_branches_architectures_not_conflated(self):
        self.user.refs.extend([Ref('app/org.example.Paint/aarch64/stable'),Ref('app/org.example.Paint/x86_64/beta')])
        result=self.run_fake(self.approve())
        self.assertEqual(result['status'],'completed')
        self.assertEqual(len(self.user.refs),3)
        self.assertEqual(len(self.system.refs),1)
    def test_changed_inventory_requires_new_approval(self):
        record=self.approve();self.user.refs.append(Ref('app/org.example.New/x86_64/stable'))
        result=self.run_fake(record)
        self.assertNotEqual(result['status'],'completed');self.assertFalse(result['events'])
    def test_changed_operations_after_authorization(self):
        record=self.approve();self.options['change_ready']=True
        result=self.run_fake(record)
        self.assertEqual(result['status'],'failed');self.assertFalse(result['removed'])
    def test_extra_removal_install_update_fail_closed(self):
        for kind in (1,2,3):
            with self.subTest(kind=kind):
                Transaction.extra=[];record=self.approve()
                Transaction.extra=[(kind,self.user.refs[1].format_ref())]
                result=self.run_fake(record)
                self.assertEqual(result['status'],'failed');self.assertFalse(result['removed'])
                self.api[0].Transaction.new_for_installation=lambda *_:Transaction()
    def test_auth_failures_not_retried(self):
        record=self.approve()
        for error in ('Authorization denied','Authentication cancelled','Authorization unavailable','Authorization timed out'):
            self.options['auth']=error;result=self.run_fake(record)
            self.assertEqual(result['status'],'failed');self.assertIn(error,result['error']);self.assertFalse(result['events'])
    def test_cancel_before_execution(self):
        record=self.approve();token=CancellationToken()
        with operation(cancellation=token):
            token.cancel();result=self.run_fake(record)
        self.assertEqual(result['status'],'cancelled');self.assertFalse(result['removed'])
    def test_partial_failure_keeps_completed_results(self):
        record=self.approve(refs=list(self.user.refs));self.options['fail_at']=1
        result=self.run_fake(record)
        self.assertEqual(result['status'],'partial');self.assertEqual(len(result['removed']),1);self.assertEqual(len(result['remaining']),1)
    def test_cancellation_during_execution_reconciles(self):
        record=self.approve(refs=list(self.user.refs));token=CancellationToken();self.options['cancel_after']=token
        with operation(cancellation=token): result=self.run_fake(record)
        self.assertEqual(result['status'],'partial');self.assertEqual(len(result['removed']),1)
    def test_unexpected_final_state_unknown(self):
        record=self.approve();self.options['unexpected']=True
        result=self.run_fake(record)
        self.assertEqual(result['status'],'unknown');self.assertFalse(ExecutionResult(result).completed)
    def test_api_success_without_actual_removal_not_success(self):
        record=self.approve();self.options['no_change']=True
        result=self.run_fake(record)
        self.assertEqual(result['status'],'unknown')
    def test_missing_api_result_cannot_report_success(self):
        record=self.approve();self.options['missing_result']=True
        result=self.run_fake(record)
        self.assertEqual(result['status'],'partial');self.assertFalse(ExecutionResult(result).completed)
    def test_failed_final_inventory_unknown(self):
        record=self.approve();original=self.provider.discover;calls=[]
        def discover():
            calls.append(1)
            if len(calls)==4: raise CleanerError('Unreadable inventory')
            return original()
        with patch.object(self.provider,'discover',side_effect=discover):result=self.run_fake(record)
        self.assertEqual(result['status'],'unknown');self.assertIn('verification failed',result['error'])
    def test_running_application_blocks_at_approval_and_again_at_ready(self):
        record=self.approve()
        with patch('package_cleaner.core.flatpak_provider.running_applications',return_value={'org.example.Paint'}):
            result=self.run_fake(record)
        self.assertEqual(result['status'],'failed');self.assertIn('running',result['error'])
    def test_keep_changes_block(self):
        record=self.approve();self.prefs.keep(record['selected'][0])
        result=self.run_fake(record)
        self.assertEqual(result['status'],'failed');self.assertIn('Keep preferences changed',result['error'])
    def test_data_preserved_and_logs_compact(self):
        data=self.root/'.var/app/org.example.Paint/data';data.mkdir(parents=True);(data/'important').write_text('keep')
        self.user.refs.extend(Ref(f'runtime/org.example.Lib{i}/x86_64/stable',metadata='[Runtime]\n') for i in range(1000))
        record=self.approve();result=self.run_fake(record)
        self.assertEqual((data/'important').read_text(),'keep')
        directory=Path(result['log_directory'])
        self.assertLess((directory/'execution.log').stat().st_size,4096)
        self.assertLess((directory/'result.json').stat().st_size,4096)
        self.assertNotIn('inventory',json.loads((directory/'result.json').read_text()))
        self.assertTrue(result['data_retained'])
    def test_opt_in_and_confirmation_required(self):
        record=self.approve()
        with patch.object(FlatpakProvider,'execute') as execute_mock:
            with self.assertRaises(CleanerError):execute_confirmed(record,'REMOVE')
            self.assertIsNone(execute_confirmed(record,'no',enabled=True))
            execute_mock.assert_not_called()
    def test_missing_or_malformed_result_unknown(self):
        record=self.approve()
        for value in (None,{},'completed'):
            with patch.object(FlatpakProvider,'execute',return_value=value):
                result=execute_confirmed(record,'REMOVE',enabled=True)
            self.assertFalse(result.completed);self.assertEqual(result['status'],'unknown')
    def test_cli_final_confirmation_cancel_never_executes(self):
        from package_cleaner.presentation import execute_selection
        plan=self.provider.preview([next(iter(self.ps))],self.ps)[0]
        with patch('builtins.input',return_value='cancel'),patch('package_cleaner.core.authorization.execute_confirmed') as run,redirect_stdout(io.StringIO()) as output:
            self.assertIsNone(execute_selection(plan,self.ps,self.prefs))
        run.assert_not_called();self.assertIn('Type REMOVE',output.getvalue())
    def test_no_second_provider_after_partial_or_cancel(self):
        from package_cleaner.presentation import execute_selection
        from package_cleaner.core.removal import Plan,Baseline
        part=self.provider.preview([next(iter(self.ps))],self.ps)[0]
        aggregate=Plan([],[],[],[],[],[],[],{},[],'',Baseline([],''),provider_plans=[part,part])
        for outcome in ('partial','unknown','failed'):
            with patch('package_cleaner.core.authorization.refresh_execution_part',return_value=(part,self.ps)) as refresh,patch('builtins.input',return_value='REMOVE'),patch('package_cleaner.core.authorization.execute_confirmed',return_value=ExecutionResult(dict(status=outcome,helper_verified=True,removed=[],log_directory='/fake'))) as run,redirect_stdout(io.StringIO()):
                self.assertEqual(execute_selection(aggregate,self.ps,self.prefs),2)
            self.assertEqual(run.call_count,1);self.assertEqual(refresh.call_count,1)
    def test_authentication_requirement_changed_requires_new_preview(self):
        record=self.approve()
        original=ExecutingTransaction.get_operations
        def ops(tx):
            rows=original(tx)
            for row in rows: row.get_requires_authentication=lambda:True
            return rows
        with patch.object(ExecutingTransaction,'get_operations',ops): result=self.run_fake(record)
        self.assertEqual(result['status'],'failed');self.assertIn('changed after approval',result['error'])
    def test_missing_required_signal_prevents_execution(self):
        record=self.approve()
        self.api[3].signal_lookup=lambda signal,_: signal!='ready'
        result=self.run_fake(record)
        self.assertEqual(result['status'],'failed');self.assertFalse(result['events'])
    def test_selected_runtime_must_be_individually_approved(self):
        spare=Ref('runtime/org.example.Spare/x86_64/stable',metadata='[Runtime]\n')
        self.user.refs.append(spare);self.user.unused.append(spare)
        result=self.run_fake(self.approve(refs=[spare]))
        self.assertEqual(result['status'],'completed');self.assertEqual(len(self.user.refs),2)
    def test_missing_completion_notification_is_not_success(self):
        record=self.approve()
        original=ExecutingTransaction.connect
        def connect(tx,name,callback):
            return original(tx,name,(lambda *_:None) if name=='operation-done' else callback)
        with patch.object(ExecutingTransaction,'connect',connect): result=self.run_fake(record)
        self.assertEqual(result['status'],'partial');self.assertFalse(ExecutionResult(result).completed)
    def test_unapproved_removal_in_final_state_unknown(self):
        record=self.approve();original=ExecutingTransaction.run
        def run(tx,cancel):
            value=original(tx,cancel);tx.inst.refs.clear();return value
        with patch.object(ExecutingTransaction,'run',run): result=self.run_fake(record)
        self.assertEqual(result['status'],'unknown');self.assertEqual(len(result['unexpected_removed']),1)
    def test_commit_upgrade_detected(self):
        record=self.approve();original=ExecutingTransaction.run
        def run(tx,cancel):
            value=original(tx,cancel);tx.inst.refs[0].get_commit=lambda:'new-commit';return value
        with patch.object(ExecutingTransaction,'run',run): result=self.run_fake(record)
        self.assertEqual(result['status'],'unknown');self.assertEqual(len(result['unexpected_changes']),1)
    def test_pending_result_exists_before_operations_begin(self):
        record=self.approve();original=ExecutingTransaction.run
        def run(tx,cancel):
            files=list((self.root/'logs').glob('*/result.json'))
            self.assertEqual(len(files),1)
            self.assertEqual(json.loads(files[0].read_text())['status'],'unknown')
            return original(tx,cancel)
        with patch.object(ExecutingTransaction,'run',run): self.assertEqual(self.run_fake(record)['status'],'completed')
    def test_malformed_success_record_is_unknown(self):
        record=self.approve()
        with patch.object(FlatpakProvider,'execute',return_value=dict(status='completed',provider='flatpak',removed=[],helper_verified=True)):
            result=execute_confirmed(record,'REMOVE',enabled=True)
        self.assertEqual(result['status'],'unknown')
    def test_changed_installation_approval_rejected(self):
        record=self.approve();record['installation']=installation_id(self.system)
        with self.assertRaises(CleanerError):self.run_fake(record)
    def test_approval_operations_do_not_alias_preview(self):
        plan=self.provider.preview([next(iter(self.ps))],self.ps)[0]
        record=approval_record(plan,self.ps,self.prefs)
        plan.operations[0]['branch']='changed'
        self.assertEqual(record['operations'][0]['branch'],'stable')
    def test_unsupported_execution_api_keeps_preview_readonly(self):
        self.api[3].signal_lookup=lambda name,_:name!='operation-done'
        plan=self.provider.preview([next(iter(self.ps))],self.ps)[0]
        self.assertTrue(plan.execution_disabled);self.assertTrue(plan.removed)
        with self.assertRaises(CleanerError):approval_record(plan,self.ps,self.prefs)
    def test_omitted_operation_rejected(self):
        record=self.approve(refs=list(self.user.refs))
        original=ExecutingTransaction.get_operations
        with patch.object(ExecutingTransaction,'get_operations',lambda tx:original(tx)[:1]):
            result=self.run_fake(record)
        self.assertEqual(result['status'],'failed');self.assertFalse(result['removed'])
    def test_no_second_transaction_after_confirmation_cancel(self):
        from package_cleaner.presentation import execute_selection
        from package_cleaner.core.removal import Plan,Baseline
        part=self.provider.preview([next(iter(self.ps))],self.ps)[0]
        aggregate=Plan([],[],[],[],[],[],[],{},[],'',Baseline([],''),provider_plans=[part,part])
        with patch('package_cleaner.core.authorization.refresh_execution_part',return_value=(part,self.ps)) as refresh,patch('builtins.input',return_value='cancel'),patch('package_cleaner.core.authorization.execute_confirmed') as run,redirect_stdout(io.StringIO()):
            self.assertIsNone(execute_selection(aggregate,self.ps,self.prefs))
        run.assert_not_called();self.assertEqual(refresh.call_count,1)
