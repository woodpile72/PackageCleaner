# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Provider tests use in-memory installations; no real package operations."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from package_cleaner.core.flatpak_provider import FlatpakProvider, identity, fingerprint
from package_cleaner.core.inventory import Package
from package_cleaner.core.providers import preview, eligible_support, effective_kept, filter_sources
from package_cleaner.core.recommendations import recommend
from package_cleaner.core.sorting import sort_applications
from package_cleaner.core.preferences import Preferences
from package_cleaner.core.review_service import ReviewService
from package_cleaner.core.operations import CleanerError, CancellationToken, operation, OperationCancelled
from package_cleaner.core.footprint import MB


class Error(Exception): pass

class Ref:
    def __init__(self, ref='app/org.example.Paint/x86_64/stable', size=100*MB, metadata='[Application]\nname=org.example.Paint\nruntime=org.example.Runtime/x86_64/stable\n'):
        self.ref,self.size,self.metadata=ref,size,metadata
    def format_ref(self): return self.ref
    def get_kind(self): return 0 if self.ref.startswith('app/') else 1
    def get_name(self): return self.ref.split('/')[1]
    def get_installed_size(self): return self.size
    def get_appdata_name(self): return self.get_name().split('.')[-1]
    def get_appdata_summary(self): return 'Paint pictures'
    def get_appdata_version(self): return '1'
    def get_commit(self): return 'commit'
    def load_metadata(self, _): return NS(get_data=lambda:self.metadata.encode())

class Installation:
    def __init__(self, key, refs=(), unused=()): self.key,self.refs,self.unused=key,list(refs),list(unused)
    def get_is_user(self): return self.key=='user'
    def get_id(self): return self.key
    def get_path(self): return NS(get_path=lambda:'/fake/'+self.key)
    def list_installed_refs(self,_): return self.refs
    def list_unused_refs(self,*_): return self.unused

class Cancellable:
    def __init__(self): self.cancelled=False
    def cancel(self): self.cancelled=True

class Transaction:
    extra=[]
    unused_extra=[]
    failure=None
    before_ready=None
    executed=0
    last=None
    def __init__(self): self.signals={};self.refs=[];self.settings={};Transaction.last=self
    def __getattr__(self,name):
        if name.startswith('set_'): return lambda value:self.settings.update({name:value})
        raise AttributeError(name)
    def connect(self,key,callback): self.signals[key]=callback
    def add_uninstall(self,ref): self.refs.append(ref)
    def get_operations(self):
        return [NS(get_operation_type=lambda kind=kind:kind,get_ref=lambda ref=ref:ref,get_requires_authentication=lambda:False)
                for kind,ref in [(1,r) for r in self.refs]+self.extra+(self.unused_extra if self.settings.get('set_include_unused_uninstall_ops') else [])]
    def run(self,cancel):
        if self.failure: raise Error(self.failure)
        if self.before_ready: self.before_ready()
        if not self.signals['ready-pre-auth'](self): raise Error('Aborted')
        if not self.signals['ready'](self): raise Error('Aborted')
        Transaction.executed += 1


def api(installations):
    return (NS(Installation=NS(new_user=lambda _:installations[0]),
               get_system_installations=lambda _:[*installations[1:]],
               RefKind=NS(APP=0), Transaction=NS(new_for_installation=lambda *_:Transaction()),
               TransactionOperationType=NS(UNINSTALL=1)),
            NS(Cancellable=Cancellable), NS(Error=Error), NS(signal_lookup=lambda *_:1))


class ProviderTests(unittest.TestCase):
    def setUp(self):
        Transaction.extra=[];Transaction.unused_extra=[];Transaction.failure=None;Transaction.before_ready=None;Transaction.executed=0
        self.running=patch('package_cleaner.core.flatpak_provider.running_applications',return_value=set())
        self.running.start();self.addCleanup(self.running.stop)
        self.app=Ref();self.runtime=Ref('runtime/org.example.Runtime/x86_64/stable',500*MB,'[Runtime]\nname=org.example.Runtime\n')
        self.user=Installation('user',[self.app,self.runtime])
        self.system=Installation('default')
        self.provider=FlatpakProvider(api([self.user,self.system]))

    def test_absent_optional_bindings(self):
        with patch('package_cleaner.core.flatpak_provider.load_api',return_value=None),patch('shutil.which',return_value=None):
            self.assertEqual(FlatpakProvider().discover(),({},[]))

    def test_empty_installations(self):
        self.user.refs=[]
        self.assertEqual(self.provider.discover(),({},[]))

    def test_ids_include_installation_branch_architecture_and_provider(self):
        self.system.refs=[self.app,Ref('app/org.example.Paint/aarch64/stable'),Ref('app/org.example.Paint/x86_64/beta')]
        extra=Installation('external',[self.app])
        p=FlatpakProvider(api([self.user,self.system,extra]));ps,_=p.discover()
        apps=[x for x in ps.values() if x.application]
        self.assertEqual(len({x.identity for x in apps}),5)
        self.assertTrue(all(x.identity.startswith('flatpak:') for x in apps))
        apt=Package('org.example.Paint:amd64','org.example.Paint','1',False,1,'','misc')
        self.assertTrue(apt.identity.startswith('apt:system/'))
        self.assertNotIn(apt.identity,ps)

    def test_shared_runtime_is_never_an_application_or_an_unused_suggestion(self):
        self.system.refs=[self.runtime];self.system.unused=[self.runtime]
        ps,_=self.provider.discover()
        self.assertEqual(eligible_support(ps),[])
        self.assertTrue(all(p.protected for p in ps.values() if not p.application))
        self.assertEqual(len(recommend(ps,category='large')),1)
        self.assertEqual(recommend(ps,category='potential'),[])

    def test_unused_runtime_and_extension_are_support_only(self):
        self.user.refs=[self.runtime,Ref('runtime/org.example.Extension/x86_64/stable',1,'[Runtime]\nname=Extension\n')]
        self.user.unused=list(self.user.refs)
        ps,_=self.provider.discover()
        self.assertEqual(len(eligible_support(ps)),2)
        self.assertEqual(len(recommend(ps,category='support')),2)
        self.assertEqual(recommend(ps,category='large'),[])

    def test_search_threshold_sort_and_source_filter(self):
        self.user.refs += [Ref('app/org.example.Big/x86_64/stable',200*MB),Ref('app/org.example.Small/x86_64/stable',100*MB-1)]
        ps,_=self.provider.discover()
        items=recommend(ps,category='large')
        self.assertEqual([i.installed_bytes for i in items],[200*MB,100*MB])
        self.assertEqual(len(recommend(ps,category='search',query='org.example.Small')),1)
        self.assertEqual([i.title for i in sort_applications(items,'name_asc')],['Big','Paint'])
        self.assertEqual(filter_sources(items,'apt'),[])
        self.assertEqual(filter_sources(items,'flatpak'),items)

    def test_namespaced_keep_queue_and_legacy_apt_keep(self):
        ps,_=self.provider.discover();name=next(n for n,p in ps.items() if p.application)
        with tempfile.TemporaryDirectory() as tmp:
            prefs=Preferences(Path(tmp)/'prefs');path=Path(tmp)/'queue'
            service=ReviewService(prefs,path,inventory_provider=lambda:(ps,[]))
            service.packages=ps;service.select(name)
            restored=ReviewService(prefs,path)
            self.assertEqual(restored.selected,{name});self.assertIsNone(restored.preview)
            service.keep(name)
            self.assertIn(name,Preferences(prefs.path).kept)
            prefs.reload()
            self.assertEqual(recommend(ps,prefs.kept),[])
            self.assertIn('a:amd64',effective_kept({}, {'apt:system/a:amd64'}))

    def test_preview_aborts_before_execution_and_preserves_data(self):
        ps,_=self.provider.discover();name=next(n for n,p in ps.items() if p.application)
        plan=self.provider.preview([name],ps)[0]
        self.assertEqual(plan.removed,[name]);self.assertFalse(plan.blocked)
        self.assertTrue(plan.data_retained);self.assertFalse(plan.execution_disabled)
        self.assertEqual(Transaction.executed,0)
        self.assertTrue(Transaction.last.settings['set_include_unused_uninstall_ops'])
        # Both discovery and exact previews must abort; neither is executed.
        self.assertTrue(Transaction.last.settings['set_disable_related'])
        self.assertEqual(len(plan.retained),1)

    def test_unexpected_operations_block(self):
        ps,_=self.provider.discover();name=next(n for n,p in ps.items() if p.application)
        for kind in (1,2,3,4):
            with self.subTest(kind=kind):
                Transaction.extra=[(kind,'runtime/unapproved/x86_64/stable')]
                plan=self.provider.preview([name],ps)[0]
                self.assertTrue(plan.blocked);self.assertFalse(plan.removed)
                self.assertEqual(Transaction.executed,0)

    def test_changed_inventory_rejected_before_resolution(self):
        ps,_=self.provider.discover();name=next(iter(ps));ps[name].commit='old'
        with self.assertRaisesRegex(CleanerError,'state changed'): self.provider.preview([name],ps)
        self.assertEqual(Transaction.executed,0)

    def test_change_during_preview_invalidates_result(self):
        ps,_=self.provider.discover();name=next(iter(ps))
        original=self.provider.discover
        after,_=original();after[name].commit='changed'
        with patch.object(self.provider,'discover',side_effect=[(ps,[]),(after,[])]):
            with self.assertRaisesRegex(CleanerError,'during preview'):self.provider.preview([name],ps)

    def test_cancellation_aborts_in_ready_callback(self):
        ps,_=self.provider.discover();name=next(iter(ps));token=CancellationToken()
        Transaction.before_ready=staticmethod(token.cancel)
        with operation(cancellation=token),self.assertRaises(OperationCancelled):
            self.provider.preview([name],ps)
        self.assertEqual(Transaction.executed,0)

    def test_failed_authorization_or_interruption_is_not_success(self):
        ps,_=self.provider.discover();name=next(iter(ps))
        for failure in ('Authorization denied','Interrupted'):
            Transaction.failure=failure
            with self.assertRaisesRegex(CleanerError,failure):self.provider.preview([name],ps)
        self.assertEqual(Transaction.executed,0)

    def test_execution_disabled_including_partial_failure_scenario(self):
        for status in ('approved','interrupted','partially completed'):
            with self.assertRaisesRegex(CleanerError,'disabled'):self.provider.execute(status)
        self.assertEqual(Transaction.executed,0)

    def test_protected_runtime_cannot_be_previewed_for_removal(self):
        ps,_=self.provider.discover();name=next(n for n,p in ps.items() if not p.application)
        plan=self.provider.preview([name],ps)[0]
        self.assertTrue(plan.blocked);self.assertEqual(plan.savings['selected'],0)
        self.assertEqual(Transaction.executed,0)

    def test_protected_apt_orphans_excluded_from_both_frontends_and_totals(self):
        from package_cleaner.core.reports import supporting_report
        from package_cleaner.core.removal import Baseline, classify
        from package_cleaner.core.inventory import protection
        ps={}
        for base in ('libsystemd0','libudev1'):
            pkg=NS(name=base,essential=False,installed=NS(record={},priority='optional'))
            reasons=protection(pkg)
            self.assertTrue(reasons)
            p=Package(base+':i386',base,'1',True,200,'system library','libs',protected=reasons,orphan=True)
            ps[p.name]=p
        self.assertEqual(recommend(ps,category='support'),[])
        with patch('package_cleaner.core.reports.establish_baseline',return_value=Baseline(list(ps),'')):
            report=supporting_report(ps)
        self.assertEqual(report['installed_bytes'],0);self.assertEqual(report['already_orphaned'],[])
        self.assertEqual(len(report['excluded']),2)
        for n in ps:
            plan=classify([n],[n],[n],Baseline(list(ps),''),ps,[], '')
            self.assertTrue(plan.blocked)
        with tempfile.TemporaryDirectory() as tmp:
            s=ReviewService(Preferences(Path(tmp)/'prefs'),False,inventory_provider=lambda:(ps,[]))
            s.packages=ps
            with self.assertRaisesRegex(CleanerError,'protected'):s.select(next(iter(ps)))
            self.assertEqual(s.discover('support').applications,[])
            found=s.discover('search','libudev1:i386').applications
            self.assertTrue(found[0].protected)

    def test_apt_helper_rejects_flatpak_plan_even_if_unblocked(self):
        from package_cleaner.core.execution import approval_record
        ps,_=self.provider.discover();name=next(iter(ps))
        plan=self.provider.preview([name],ps)[0]
        with self.assertRaisesRegex(ValueError,'not authorized'): approval_record(plan,ps,None)

    def test_cli_and_gui_share_provider_inventory_and_preview(self):
        from package_cleaner import presentation, __main__
        from package_cleaner.core import providers, review_service
        self.assertIs(presentation.inventory,providers.inventory)
        self.assertIs(__main__.inventory,providers.inventory)
        self.assertIs(review_service.inventory,providers.inventory)
        self.assertIs(presentation.simulate,review_service.simulate)

    def test_existing_flatpak_orphans_not_attributed_to_application_savings(self):
        from package_cleaner.core.recommendations import application_savings
        self.user.refs=[self.runtime];self.user.unused=[self.runtime]
        ps,_=self.provider.discover();name=next(iter(ps))
        plan=self.provider.preview([name],ps)[0]
        self.assertEqual(plan.baseline_selected_bytes,self.runtime.size)
        self.assertEqual(application_savings(plan),0)
        self.assertEqual(sum(plan.savings.values()),self.runtime.size)

    def test_multiple_providers_remain_separate_and_require_independent_approval(self):
        from package_cleaner.core.removal import Plan,Baseline
        ps,_=self.provider.discover();name=next(iter(ps))
        apt=Package('aptapp:amd64','aptapp','1',False,10,'Apt app','misc')
        ps[apt.name]=apt
        ap=Plan([apt.name],[apt.name],[],[],[],[],[],{'selected':10,'newly_orphaned':0},[],'',Baseline([],''))
        with patch('package_cleaner.core.flatpak_provider.FlatpakProvider',return_value=self.provider),patch('package_cleaner.core.providers.AptProvider.preview',return_value=ap) as aptpreview:
            plan=preview([apt.name,name],ps)
        self.assertEqual(len(plan.provider_plans),2)
        self.assertEqual(aptpreview.call_args.args[0],[apt.name])
        self.assertTrue(all(p.provider=='apt' for p in aptpreview.call_args.args[1].values()))
        self.assertFalse(plan.execution_disabled)
        self.assertFalse(plan.blocked)
        self.assertFalse(plan.transaction_arguments)

    def test_running_application_warned_without_inventing_date(self):
        with patch('package_cleaner.core.flatpak_provider.running_applications',return_value={'org.example.Paint'}):
            ps,_=self.provider.discover();name=next(iter(ps))
            plan=self.provider.preview([name],ps)[0]
        self.assertTrue(any('currently running' in text for text in plan.blocked))
        item=recommend(ps)[0]
        self.assertIsNone(item.evidence.latest_file_access)
        self.assertEqual(recommend(ps,category='potential'),[])

    def test_newly_unused_components_are_discovered_but_not_approved(self):
        ps,_=self.provider.discover();name=next(n for n,p in ps.items() if p.application)
        runtime=next(n for n,p in ps.items() if not p.application)
        Transaction.unused_extra=[(1,self.runtime.ref)]
        plan=self.provider.preview([name],ps)[0]
        self.assertEqual(plan.newly_unneeded_retained,[runtime])
        self.assertEqual(plan.removed,[name])
        self.assertEqual(plan.newly_orphaned,[])
        self.assertEqual(plan.savings['selected'],self.app.size)
        self.assertEqual(Transaction.executed,0)

    def test_previously_unused_components_not_attributed_as_new(self):
        unused=Ref('runtime/org.example.Old/x86_64/stable',1,'[Runtime]\nname=Old\n')
        self.user.refs.append(unused);self.user.unused=[unused]
        ps,_=self.provider.discover();name=next(n for n,p in ps.items() if p.application)
        Transaction.unused_extra=[(1,unused.ref)]
        plan=self.provider.preview([name],ps)[0]
        self.assertEqual(plan.newly_unneeded_retained,[])
        self.assertEqual(plan.removed,[name])
        self.assertEqual(plan.baseline_selected_bytes,0)

    def test_no_ready_signal_never_yields_a_preview(self):
        ps,_=self.provider.discover();name=next(iter(ps))
        with patch.object(Transaction,'run',return_value=True):
            with self.assertRaisesRegex(CleanerError,'did not provide'): self.provider.preview([name],ps)
        self.assertEqual(Transaction.executed,0)
