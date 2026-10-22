# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
import unittest
from unittest.mock import patch
from package_cleaner.core.inventory import Package
from package_cleaner.core.footprint import MB, application_footprint, size_class
from package_cleaner.core.recommendations import Evidence, recommend


def app(name, size, **kw):
    return Package(name, name, '1', False, size, name, 'graphics',
                   application=True, desktop_application=True, display_name=name, source=name, **kw)


def support(name, size, source, dependencies=()):
    return Package(name, name, '1', True, size, name, 'misc', source=source,
                   dependencies=list(dependencies))


class FootprintTests(unittest.TestCase):
    def test_classification_boundaries(self):
        for value, expected in [(0,'Small'), (25*MB-1,'Small'), (25*MB,'Medium'),
                                (100*MB-1,'Medium'), (100*MB,'Large'),
                                (500*MB-1,'Large'), (500*MB,'Very large')]:
            self.assertEqual(size_class(value), expected)

    def discover(self, ps, **kwargs):
        with patch('package_cleaner.core.recommendations.collect_evidence',return_value=Evidence()), patch(
                'package_cleaner.core.recommendations.package_paths',return_value=[]), patch(
                'package_cleaner.core.recommendations.running_executables',return_value=set()), patch(
                'package_cleaner.core.recommendations.simulate') as sim:
            items = recommend(ps, **kwargs)
        sim.assert_not_called()
        return items

    def test_threshold_numeric_order_and_search(self):
        ps = {p.name:p for p in [app('tiny', 9*MB), app('below',100*MB-1),
              app('Zulu',100*MB), app('Alpha',100*MB), app('huge',500*MB), app('middle',120*MB)]}
        items = self.discover(ps)
        self.assertEqual([i.package for i in items], ['huge','middle','Alpha','Zulu'])
        self.assertEqual([i.package for i in self.discover(ps,category='search',query='tiny')], ['tiny'])

    def test_exclusive_components_count_once_without_expanding_selection(self):
        root=app('editor',60*MB);root.dependencies=['data','plugin']
        data=support('data',30*MB,'editor')
        plugin=support('plugin',10*MB,'editor',['data'])
        ps={p.name:p for p in [root,data,plugin]}
        f=application_footprint(root.name,ps)
        self.assertEqual(f.installed_bytes,100*MB)
        self.assertEqual(len(f.components),3)
        items=self.discover(ps)
        self.assertEqual([i.package for i in items],['editor'])
        self.assertIsNone(items[0].plan)

    def test_shared_components_and_libraries_not_double_counted(self):
        a=app('a',60*MB);b=app('b',70*MB);b.source='a'
        a.dependencies=['data','lib'];b.dependencies=['data']
        data=support('data',40*MB,'a',['nested'])
        nested=support('nested',10*MB,'a')
        lib=support('lib',80*MB,'a');lib.section='libs'
        ps={p.name:p for p in [a,b,data,nested,lib]}
        for root in (a,b):
            f=application_footprint(root.name,ps)
            self.assertEqual(f.installed_bytes,root.size)
            self.assertIn('data',f.shared_components)
        self.assertEqual(self.discover(ps),[])

    def test_unrelated_protected_and_independent_app_components_excluded(self):
        a=app('a',100*MB);b=app('b',50*MB);b.source='a'
        protected=support('protected',50*MB,'a');protected.protected=['system']
        unrelated=support('unrelated',50*MB,'a')
        a.dependencies=['b','protected']
        ps={p.name:p for p in [a,b,protected,unrelated]}
        self.assertEqual(application_footprint('a',ps).components,('a',))
        a.source='';a.dependencies=['unrelated']
        self.assertEqual(application_footprint('a',ps).installed_bytes,a.size)
