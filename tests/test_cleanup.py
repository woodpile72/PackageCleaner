# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Shared graphical approval workflow; every inventory/execution backend is fake."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from package_cleaner.core.cleanup import CleanupSession, Proposal
from package_cleaner.core.authorization import ExecutionResult
from package_cleaner.core.operations import CleanerError, OperationCancelled
from package_cleaner.core.review_service import ReviewService
from package_cleaner.core.inventory import Package
from package_cleaner.core.preferences import Preferences
from package_cleaner.core.removal import Plan,Baseline


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)
        self.packages={n:Package(n,n,'1',False,100,'A tool','utils') for n in ('one','two','other')}
        self.plan=Plan(['one'],['one'],[],[],[],[],[],{'selected':100,'newly_orphaned':0,'already_orphaned':0},[],'',Baseline([],''),state_token='stable')
        self.service=ReviewService(Preferences(self.path/'preferences'),self.path/'queue',
            inventory_provider=lambda:(copy.deepcopy(self.packages),[]),simulation_provider=lambda *args:copy.deepcopy(self.plan))
        self.service.packages=copy.deepcopy(self.packages);self.service.select('one')
        self.session=CleanupSession(self.service,graphical=True)
        self.refresh=patch('package_cleaner.core.cleanup.authorization.refresh_execution_part',side_effect=lambda p,prefs:(copy.deepcopy(p),copy.deepcopy(self.packages)))
        self.refresh_mock=self.refresh.start();self.addCleanup(self.refresh.stop)
        self.prepare=patch('package_cleaner.core.cleanup.authorization.prepare_authorization',return_value={'approved':True})
        self.prepare_mock=self.prepare.start();self.addCleanup(self.prepare.stop)
        self.execute=patch('package_cleaner.core.cleanup.authorization.execute_confirmed',side_effect=self.success)
        self.execute_mock=self.execute.start();self.addCleanup(self.execute.stop)
        self.audit=patch('package_cleaner.core.cleanup.audit_state',return_value=dict(apt=['state'],apt_flags='same',flatpak={}))
        self.audit_mock=self.audit.start();self.addCleanup(self.audit.stop)
    def result(self,status='completed',removed=('one',),**extra):
        return ExecutionResult(dict(status=status,removed=list(removed),remaining=[],helper_verified=True,log_directory='unavailable',**extra))
    def success(self,*args,**kwargs):
        self.packages.pop('one',None)
        return self.result()
    def test_verified_success_consumes_approval_and_updates_saved_queue(self):
        self.service.select('other');self.service.preferences.keep('apt:system/kept')
        proposal=self.session.begin();out=self.session.confirm(proposal.token,accepted=True)
        self.assertTrue(out.result.completed);self.assertEqual(self.service.selected,{'other'})
        self.assertEqual(ReviewService(self.service.preferences,self.path/'queue').selected,{'other'})
        self.assertIn('apt:system/kept',Preferences(self.path/'preferences').kept)
        self.assertIsNone(self.service.preview)
        self.assertTrue(self.execute_mock.call_args.kwargs['graphical'])
        with self.assertRaises(CleanerError):self.session.confirm(proposal.token,accepted=True)
        self.assertEqual(self.execute_mock.call_count,1)
    def test_empty_queue(self):
        self.service.unstage()
        with self.assertRaises(CleanerError):self.session.begin()
        self.execute_mock.assert_not_called()
    def test_protected_plan_blocked(self):
        self.plan.blocked=['Protected desktop component']
        with self.assertRaises(CleanerError):self.session.begin()
        self.execute_mock.assert_not_called()
    def test_validation_error_never_executes(self):
        self.refresh_mock.side_effect=CleanerError('Resolver failed')
        with self.assertRaises(CleanerError):self.session.begin()
        self.execute_mock.assert_not_called()
    def test_cancel_before_confirmation_preserves_queue(self):
        proposal=self.session.begin()
        self.assertIsNone(self.session.confirm(proposal.token,accepted=False))
        self.execute_mock.assert_not_called();self.assertEqual(self.service.selected,{'one'})
    def test_changed_transaction_requires_new_approval(self):
        proposal=self.session.begin();new=copy.deepcopy(self.plan);new.savings['selected']=200
        self.refresh_mock.side_effect=lambda p,prefs:(new,copy.deepcopy(self.packages))
        changed=self.session.confirm(proposal.token,accepted=True)
        self.assertIsInstance(changed,Proposal);self.assertTrue(changed.changed)
        self.execute_mock.assert_not_called()
        with self.assertRaises(CleanerError):self.session.confirm(proposal.token,accepted=True)
    def test_updated_proposal_can_be_approved(self):
        proposal=self.session.begin();new=copy.deepcopy(self.plan);new.state_token='new'
        self.refresh_mock.side_effect=lambda p,prefs:(new,copy.deepcopy(self.packages))
        changed=self.session.confirm(proposal.token,accepted=True)
        self.assertTrue(self.session.confirm(changed.token,accepted=True).result.completed)
    def test_mutated_preview_rejected(self):
        proposal=self.session.begin();proposal.plan.removed.append('other')
        with self.assertRaises(CleanerError):self.session.confirm(proposal.token,accepted=True)
        self.execute_mock.assert_not_called()
    def test_removed_queue_intention_invalidates_confirmation(self):
        proposal=self.session.begin();self.service.unstage('one')
        with self.assertRaises(CleanerError):self.session.confirm(proposal.token,accepted=True)
        self.execute_mock.assert_not_called()
    def test_auth_denied_and_cancelled_preserve_remaining(self):
        for status in ('failed','cancelled','rejected'):
            with self.subTest(status=status):
                self.execute_mock.side_effect=lambda *a,**k:self.result(status,removed=(),error='Authorization not granted')
                proposal=self.session.begin();out=self.session.confirm(proposal.token,accepted=True)
                self.assertFalse(out.more);self.assertEqual(self.service.selected,{'one'})
    def test_partial_completion_only_removes_verified_absent_entries(self):
        self.service.select('two');self.plan.requested=['one','two'];self.plan.removed=['one','two']
        def partial(*a,**k):self.packages.pop('one');return self.result('partial')
        self.execute_mock.side_effect=partial
        proposal=self.session.begin();out=self.session.confirm(proposal.token,accepted=True)
        self.assertEqual(out.result['status'],'partial');self.assertEqual(self.service.selected,{'two'});self.assertFalse(out.more)
    def test_missing_result_or_helper_exception_unknown(self):
        for value in (None,{},ExecutionResult({'status':'completed','helper_verified':True,'removed':[]})):
            self.execute_mock.side_effect=None;self.execute_mock.return_value=value
            proposal=self.session.begin();out=self.session.confirm(proposal.token,accepted=True)
            self.assertEqual(out.result['status'],'unknown');self.assertEqual(self.service.selected,{'one'})
    def test_wrong_final_state_never_success(self):
        self.execute_mock.side_effect=lambda *a,**k:self.result()
        proposal=self.session.begin();out=self.session.confirm(proposal.token,accepted=True)
        self.assertEqual(out.result['status'],'unknown');self.assertEqual(self.service.selected,{'one'})
    def test_unaffected_provider_change_is_uncertain(self):
        self.audit_mock.side_effect=[dict(apt=[],apt_flags='',flatpak={}),dict(apt=[],apt_flags='',flatpak={'unexpected':1})]
        proposal=self.session.begin();out=self.session.confirm(proposal.token,accepted=True)
        self.assertEqual(out.result['status'],'unknown');self.assertEqual(self.service.selected,{'one'})
    def test_final_inventory_error_unknown(self):
        proposal=self.session.begin();self.service.inventory_provider=lambda:(_ for _ in ()).throw(CleanerError('Unreadable'))
        out=self.session.confirm(proposal.token,accepted=True)
        self.assertEqual(out.result['status'],'unknown')
    def test_concurrent_duplicate_execution_prevented(self):
        proposal=self.session.begin();self.session.lock.acquire()
        try:
            with self.assertRaises(CleanerError):self.session.confirm(proposal.token,accepted=True)
        finally:self.session.lock.release()
        self.execute_mock.assert_not_called()
    def test_external_unrelated_saved_selection_preserved(self):
        proposal=self.session.begin()
        other=ReviewService(self.service.preferences,self.path/'queue');other.select('other')
        self.session.confirm(proposal.token,accepted=True)
        self.assertEqual(self.service.selected,{'other'})
    def test_mixed_providers_and_installations_require_separate_confirmations(self):
        second=copy.deepcopy(self.plan);second.requested=['two'];second.removed=['two'];second.provider='flatpak';second.installation='user@path'
        third=copy.deepcopy(second);third.requested=['other'];third.removed=['other'];third.installation='system/named@path'
        self.plan.provider_plans=[copy.deepcopy(self.plan),second,third]
        self.service.select('two');self.service.select('other')
        proposal=self.session.begin();self.assertEqual(proposal.total,3)
        out=self.session.confirm(proposal.token,accepted=True);self.assertTrue(out.more)
        self.assertEqual(self.execute_mock.call_count,1)
        proposal2=self.session.propose();self.assertEqual(proposal2.plan.provider,'flatpak')
        self.assertEqual(proposal2.index,2);self.execute_mock.assert_called_once()
        self.session.cancel()
        with self.assertRaises(CleanerError):self.session.propose()
    def test_partial_stops_mixed_queue(self):
        second=copy.deepcopy(self.plan);second.provider='flatpak'
        self.plan.provider_plans=[copy.deepcopy(self.plan),second]
        def partial(*a,**k):self.packages.pop('one');return self.result('partial')
        self.execute_mock.side_effect=partial
        proposal=self.session.begin();out=self.session.confirm(proposal.token,accepted=True)
        self.assertFalse(out.more)
        with self.assertRaises(CleanerError):self.session.propose()
    def test_cancellation_after_partial_removal_does_not_skip_reconciliation(self):
        from package_cleaner.core.operations import CancellationToken, operation
        token=CancellationToken()
        def partial(*a,**k):
            self.packages.pop('one');token.cancel();return self.result('partial')
        self.execute_mock.side_effect=partial
        proposal=self.session.begin()
        with operation(cancellation=token):out=self.session.confirm(proposal.token,accepted=True)
        self.assertEqual(out.result['status'],'partial');self.assertFalse(self.service.selected)
    def test_cross_provider_evidence_is_compressed_and_small_result_separate(self):
        directory=self.path/'execution';directory.mkdir()
        self.execute_mock.side_effect=lambda *a,**k:(self.packages.pop('one'),ExecutionResult(dict(self.result(),log_directory=str(directory))))[1]
        proposal=self.session.begin();out=self.session.confirm(proposal.token,accepted=True)
        self.assertTrue(out.result.completed)
        self.assertTrue((directory/'cross-provider.json.gz').exists())
        self.assertLess((directory/'cleanup-result.json').stat().st_size,2048)
    def test_graphical_dispatch_uses_existing_apt_launcher(self):
        self.execute.stop()
        from package_cleaner.core.authorization import execute_confirmed
        with patch('package_cleaner.core.execution.launch',return_value=dict(self.result())) as launch:
            execute_confirmed({'plan':{}},'REMOVE',enabled=True,graphical=True)
        launch.assert_called_once_with({'plan':{}},graphical=True)
    def test_graphical_askpass_is_root_owned_and_not_writable(self):
        from package_cleaner.core.execution import graphical_askpass
        from types import SimpleNamespace
        with patch('package_cleaner.core.execution.os.stat',return_value=SimpleNamespace(st_uid=0,st_mode=0o100755)),patch('package_cleaner.core.execution.os.access',return_value=True):
            self.assertEqual(graphical_askpass(),'/usr/bin/ssh-askpass')
        for uid,mode in ((1001,0o100755),(0,0o100777)):
            with patch('package_cleaner.core.execution.os.stat',return_value=SimpleNamespace(st_uid=uid,st_mode=mode)),patch('package_cleaner.core.execution.os.access',return_value=True):
                with self.assertRaisesRegex(ValueError,'authentication is unavailable'):graphical_askpass()
    def test_flatpak_only_success_for_each_installation_scope(self):
        for installation in ('user@home','system/default@var','system/games@games'):
            with self.subTest(installation=installation):
                self.packages['one']=Package('one','one','1',False,100,'Tool','utils',provider='flatpak')
                self.service.select('one');self.plan.provider='flatpak';self.plan.installation=installation
                def success(*a,**k):
                    self.packages.pop('one')
                    return self.result(provider='flatpak',api_success=True,final_state_verified=True,boundaries=['pre-auth','ready'])
                self.execute_mock.side_effect=success
                proposal=self.session.begin();out=self.session.confirm(proposal.token,accepted=True)
                self.assertTrue(out.result.completed);self.assertFalse(self.service.selected)
    def test_cannot_begin_or_propose_during_execution(self):
        self.session.lock.acquire()
        try:
            with self.assertRaises(CleanerError):self.session.begin()
            with self.assertRaises(CleanerError):self.session.propose()
        finally:self.session.lock.release()
