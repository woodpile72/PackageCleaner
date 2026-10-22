# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
import io
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from package_cleaner.inventory import Package
from package_cleaner.recommendations import Evidence, recommend, Recommendation
from package_cleaner.presentation import print_recommendation, review
from package_cleaner.preferences import Preferences
from package_cleaner.state import PackageStateChanged


class LazyDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.app=Package('app:amd64','app','1',False,100_000_000,'Photo editor','graphics',application=True,
                         desktop_application=True,display_name='Photo editor')
        self.other=Package('other:amd64','other','1',False,80_000_000,'Other app','graphics',application=True,
                           desktop_application=True,display_name='Other app')
        self.ps={p.name:p for p in (self.app,self.other)}
        self.evidence=Evidence(executable_count=1,old_accesses=1)

    def discover(self, **kwargs):
        with patch('package_cleaner.recommendations.collect_evidence',return_value=self.evidence) as collect, patch(
                'package_cleaner.recommendations.package_paths',return_value=[]), patch(
                'package_cleaner.recommendations.running_executables',return_value=set()), patch(
                'package_cleaner.recommendations.simulate') as sim, patch('package_cleaner.removal.apt_simulation') as apt:
            result=recommend(self.ps,category=kwargs.pop('category','potential'),**kwargs)
        sim.assert_not_called();apt.assert_not_called()
        return result,collect

    def test_discovery_never_simulates_and_evidence_is_collected_once(self):
        d={};items,collect=self.discover(diagnostics=d)
        self.assertEqual(len(items),2)
        self.assertEqual(collect.call_count,2)
        self.assertTrue(all(x.plan is None for x in items))
        self.assertEqual(d['timings']['apt_simulations'],0)
        self.assertEqual(d['validated_count'],0)
        self.assertNotIn('actionable_count',d)

    def test_metadata_size_is_not_presented_as_recoverable_space(self):
        items,_=self.discover();out=io.StringIO()
        with redirect_stdout(out):print_recommendation(items[0],1,2,'potential')
        self.assertIn('Installed application size:',out.getvalue())
        self.assertIn('not a removal estimate',out.getvalue())
        self.assertNotIn('Potential space',out.getvalue())

    def test_settings_and_tiny_tools_remain_in_advanced_and_search(self):
        self.other.desktop_categories=['Settings','System']
        items,_=self.discover();self.assertEqual([x.package for x in items],[self.app.name])
        items,_=self.discover(category='advanced');self.assertEqual(items,[])
        items,_=self.discover(category='advanced',component_filter='uncertain');self.assertEqual([x.package for x in items],[self.other.name])
        items,_=self.discover(category='search',query='other');self.assertEqual([x.package for x in items],[self.other.name])

    def test_small_unnamed_components_are_deferred(self):
        self.other.display_name=''
        self.other.size=13 * 1024 * 1024
        items,_=self.discover()
        self.assertEqual([x.package for x in items],[self.app.name])
        items,_=self.discover(category='search',query='other:amd64')
        self.assertEqual([x.package for x in items],[self.other.name])

    def test_desktop_dependencies_are_deferred(self):
        meta=Package('mint-meta-cinnamon:amd64','mint-meta-cinnamon','1',False,1,'Desktop','misc',protected=['desktop'])
        meta.dependencies=[self.other.name];self.ps[meta.name]=meta
        items,_=self.discover();self.assertEqual([x.package for x in items],[self.app.name])

    def test_missing_usage_does_not_qualify_for_potential(self):
        self.evidence=Evidence();items,_=self.discover();self.assertFalse(items)

    def test_real_activity_overrides_old_timestamp_clues(self):
        self.evidence.server_observations={'application_activity':{'recent_request_records':2}}
        items,_=self.discover();self.assertFalse(items)

    def test_keep_skip_and_quit_require_no_simulations(self):
        items,_=self.discover()
        with tempfile.TemporaryDirectory() as tmp,patch('builtins.input',side_effect=['k','q']),patch(
                'package_cleaner.presentation.simulate') as sim,redirect_stdout(io.StringIO()):
            review(self.ps,candidates=items,preferences=Preferences(Path(tmp)/'prefs'))
        sim.assert_not_called()

    def test_review_removal_runs_all_four_apt_stages_with_original_selection(self):
        support=Package('support:amd64','support','1',True,100,'Support','libs')
        old=Package('old:i386','old','1',True,100,'Protected orphan','libs',protected=['system'])
        self.ps.update({support.name:support,old.name:old})
        items,_=self.discover()
        with tempfile.TemporaryDirectory() as tmp,patch('builtins.input',side_effect=['r','q']),patch(
                'package_cleaner.presentation.inventory',return_value=(self.ps,[])),patch(
                'package_cleaner.removal.apt_simulation',side_effect=[([old.name],''),([self.app.name],''),
                 ([self.app.name,support.name,old.name],''),([self.app.name,support.name],'')]) as apt,redirect_stdout(io.StringIO()):
            review(self.ps,candidates=items,preferences=Preferences(Path(tmp)/'prefs'))
        self.assertEqual(apt.call_count,4)
        args=apt.call_args.args[0]
        self.assertIn('APT::Get::AutomaticRemove=false',args)
        self.assertNotIn(old.name,args)
        self.assertIn(support.name,args)

    def test_one_state_change_aborts_discovery_without_error_fanout(self):
        d={}
        with patch('package_cleaner.state.package_state',side_effect=['old','new']),patch(
                'package_cleaner.recommendations.collect_evidence',return_value=self.evidence),patch(
                'package_cleaner.recommendations.package_paths',return_value=[]),patch(
                'package_cleaner.recommendations.simulate') as sim:
            with self.assertRaises(PackageStateChanged):recommend(self.ps,category='potential',diagnostics=d)
        sim.assert_not_called();self.assertFalse(d.get('failed'))

    def test_review_refreshes_once_on_common_state_change(self):
        items,_=self.discover();fresh=dict(self.ps)
        with tempfile.TemporaryDirectory() as tmp,patch('builtins.input',return_value='q'),patch(
                'package_cleaner.presentation.inventory',side_effect=[(self.ps,[]),(fresh,[])]) as inv,patch(
                'package_cleaner.recommendations.recommend',side_effect=[PackageStateChanged('changed'),items]) as discover,redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            self.assertEqual(review(None,category='potential',preferences=Preferences(Path(tmp)/'prefs')),0)
        self.assertEqual(inv.call_count,2)
        self.assertIs(discover.call_args.args[0],fresh)

    def test_continued_state_changes_report_one_failure_not_zero_results(self):
        out=io.StringIO()
        with tempfile.TemporaryDirectory() as tmp,patch('builtins.input',return_value='q'),patch(
                'package_cleaner.presentation.inventory',return_value=(self.ps,[])) as inv,patch(
                'package_cleaner.recommendations.recommend',side_effect=PackageStateChanged('Package state changed during discovery')),redirect_stdout(out),redirect_stderr(out):
            self.assertEqual(review(None,category='potential',preferences=Preferences(Path(tmp)/'prefs')),1)
        self.assertEqual(inv.call_count,2)
        self.assertIn('Analysis failed',out.getvalue())
        self.assertNotIn('Found 0',out.getvalue())
        self.assertNotIn('No reviewable applications',out.getvalue())
