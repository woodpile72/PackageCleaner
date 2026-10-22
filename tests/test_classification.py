# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
import unittest
from copy import deepcopy
from unittest.mock import patch
from package_cleaner.core.inventory import Package
from package_cleaner.core.classification import Classification as C, classify_components, component_recommendations, filter_components, file_roles
from package_cleaner.core.recommendations import recommend


def package(name, **kw):
    fields=dict(name=name,base=name,version='1',automatic=False,size=100,
                summary='Independent tool',section='utils',public_commands=['/usr/bin/tool'],file_metadata_available=True)
    fields.update(kw)
    return Package(**fields)


class ClassificationTests(unittest.TestCase):
    def classify(self,*packages):
        ps={p.name:p for p in packages}
        before=deepcopy(ps)
        with patch('package_cleaner.core.removal.simulate',side_effect=AssertionError('No simulation')),patch('package_cleaner.core.recommendations.collect_evidence',side_effect=AssertionError('No usage inspection')):
            result=classify_components(ps)
        self.assertEqual(ps,before)
        return result

    def test_manual_flag_and_missing_reverse_dependencies_are_not_sufficient(self):
        p=package('arbitrary',public_commands=[],section='libs')
        a=self.classify(p)[p.name]
        self.assertEqual(a.classification,C.UNCERTAIN)
        self.assertTrue(a.manual_investigation)
        self.assertEqual(a.orphan_kind,'manual_support_investigation')
        self.assertIn('not proof',' '.join(a.reasons))

    def test_metadata_missing_not_reviewable(self):
        p=package('tool',file_metadata_available=False)
        self.assertEqual(self.classify(p)['tool'].classification,C.UNCERTAIN)

    def test_independent_tiny_tool_reviewable_without_size_cutoff(self):
        p=package('tiny',size=1)
        self.assertEqual(self.classify(p)['tiny'].classification,C.REVIEWABLE)
        self.assertEqual(len(recommend({'tiny':p},category='advanced')),1)

    def test_required_direct_transitive_and_recommended_desktop_components(self):
        desktop=package('arbitrary-desktop',protected=['existing policy'],dependencies=['middle'],recommends=['recommended'])
        middle=package('middle',dependencies=['leaf'])
        leaf=package('leaf');recommended=package('recommended')
        result=self.classify(desktop,middle,leaf,recommended)
        self.assertEqual(result[desktop.name].classification,C.PROTECTED)
        for n in ('middle','leaf','recommended'):
            self.assertEqual(result[n].classification,C.REQUIRED)
            self.assertEqual(result[n].desktop_root,desktop.name)

    def test_installed_application_requirement_blocks_reviewable_classification(self):
        app=package('consumer',dependencies=['helper'])
        helper=package('helper')
        a=self.classify(app,helper)['helper']
        self.assertEqual(a.classification,C.REQUIRED)
        self.assertEqual(a.required_by,('consumer',))

    def test_recommendation_is_uncertain_outside_desktop_closure(self):
        app=package('consumer',recommends=['helper']);helper=package('helper')
        self.assertEqual(self.classify(app,helper)['helper'].classification,C.UNCERTAIN)

    def test_metadata_metapackage_closure_and_cycles(self):
        meta=package('anything',section='metapackages',dependencies=['a'])
        a=package('a',dependencies=['b']);b=package('b',dependencies=['a'])
        result=self.classify(meta,a,b)
        self.assertTrue(all(x.classification==C.REQUIRED for x in result.values()))

    def test_protected_orphan_never_reviewable_or_investigation(self):
        p=package('system-library',automatic=True,orphan=True,protected=['policy'],section='libs')
        result=self.classify(p)[p.name]
        self.assertEqual(result.classification,C.PROTECTED)
        self.assertFalse(result.manual_investigation)
        self.assertEqual(component_recommendations({p.name:p}),[])

    def test_distinct_orphan_sources_and_flatpak_exclusion(self):
        auto=package('auto',automatic=True,orphan=True)
        manual=package('manual',public_commands=[],section='libdevel')
        fp=package('fp',provider='flatpak',orphan=True)
        result=self.classify(auto,manual,fp)
        self.assertEqual(result['auto'].orphan_kind,'existing_autoremove')
        self.assertEqual(result['manual'].orphan_kind,'manual_support_investigation')
        self.assertNotIn('fp',result)

    def test_running_service_is_not_use_or_automatic_required(self):
        p=package('server',section='httpd',running=True,service_units=['/usr/lib/systemd/system/web.service'])
        a=self.classify(p)['server']
        self.assertEqual(a.classification,C.REVIEWABLE)
        self.assertIn('not evidence of actual use',' '.join(a.reasons))
        item=recommend({'server':p},category='advanced')[0]
        self.assertIsNone(item.evidence.latest_file_access)
        self.assertFalse(item.evidence.activity)

    def test_integration_roles_do_not_depend_on_package_names(self):
        roles=file_roles(['/usr/lib/udev/rules.d/anything.rules','/etc/pam.d/anything'])
        self.assertIn('hardware/device integration',roles)
        for name in ('harmless-name','apache-lookalike'):
            p=package(name,integration_roles=roles)
            self.assertEqual(self.classify(p)[name].classification,C.UNCERTAIN)
        p=package('internal-service',service_units=['/usr/lib/systemd/system/x.service'],public_commands=[])
        self.assertEqual(self.classify(p)[p.name].classification,C.UNCERTAIN)

    def test_filters_and_keep_never_mutate_selection_or_protection(self):
        tool=package('tool');unknown=package('unknown',section='libs',public_commands=[])
        consumer=package('consumer',dependencies=['required']);required=package('required')
        protected=package('protected',protected=['critical'])
        ps={p.name:p for p in (tool,unknown,consumer,required,protected)}
        items=component_recommendations(ps,kept={'apt:system/consumer'},mode='all')
        self.assertEqual([i.package for i in filter_components(items)],['tool'])
        self.assertEqual([i.package for i in filter_components(items,'uncertain')],['unknown'])
        self.assertEqual([i.package for i in filter_components(items,'investigate')],['unknown'])
        self.assertEqual([i.package for i in filter_components(items,'required')],['required'])
        self.assertEqual(len(filter_components(items,'all')),4)
        self.assertEqual(next(i for i in items if i.package=='protected').protected,['critical'])
        with self.assertRaises(ValueError):filter_components(items,'invalid')

    def test_metadata_only_discovery_has_classification_diagnostics(self):
        ps={'tool':package('tool')};diagnostics={}
        with patch('package_cleaner.core.recommendations.package_paths',side_effect=AssertionError('No file inspection')),patch('package_cleaner.core.removal.apt_simulation',side_effect=AssertionError('No APT')):
            items=recommend(ps,category='advanced',diagnostics=diagnostics)
        self.assertEqual(len(items),1)
        self.assertEqual(diagnostics['component_counts']['REVIEWABLE'],1)
        self.assertEqual(diagnostics['timings']['apt_simulations'],0)

    def test_library_source_helpers_and_security_integrations_are_uncertain(self):
        lib=package('library',section='libs',source='common',protected=['policy'])
        helper=package('helper',source='common')
        self.assertEqual(self.classify(lib,helper)['helper'].classification,C.UNCERTAIN)
        security=package('unfamiliar-name',integration_roles=file_roles(['/etc/apparmor.d/profile']))
        self.assertEqual(self.classify(security)[security.name].classification,C.UNCERTAIN)

    def test_inventory_explicitly_collects_recommends_without_changing_hard_dependencies(self):
        from types import SimpleNamespace as NS
        from unittest.mock import Mock
        from package_cleaner.core.inventory import inventory
        def pkg(name):
            version=NS(version='1',installed_size=1,summary='Tool',section='utils',priority='optional',record={},dependencies=[],get_dependencies=Mock(return_value=[]))
            return NS(name=name,fullname=name+':amd64',is_installed=True,is_auto_installed=False,is_auto_removable=False,essential=False,installed=version)
        a,b=pkg('alpha'),pkg('beta')
        a.installed.get_dependencies.return_value=[NS(rawtype='Recommends',or_dependencies=[NS(installed_target_versions=[NS(package=b)])])]
        with patch.dict('sys.modules',{'apt':NS(Cache=lambda:[a,b])}),patch('package_cleaner.core.inventory.history',return_value=({},[])),patch('package_cleaner.core.inventory.read_inspection_text',side_effect=OSError()),patch('package_cleaner.core.state.package_state',return_value='stable'):
            ps,_=inventory()
        self.assertEqual(ps[a.fullname].recommends,[b.fullname])
        self.assertEqual(ps[a.fullname].dependencies,[])
        self.assertEqual(ps[b.fullname].reverse_dependencies,[])
        a.installed.get_dependencies.assert_called_once_with('Recommends')

    def test_stale_classification_snapshot_is_rejected(self):
        from package_cleaner.core.state import PackageSnapshot,PackageStateChanged
        ps=PackageSnapshot({'tool':package('tool')});ps.state_token='old'
        with patch('package_cleaner.core.state.package_state',return_value='new'):
            with self.assertRaises(PackageStateChanged):recommend(ps,category='advanced')
