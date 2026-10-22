# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
import copy
import unittest
from package_cleaner.core.sorting import sort_applications, observed_activity, sort_options, DEFAULTS
from package_cleaner.core.recommendations import Recommendation, Evidence


def item(package, title, size, date=None):
    return Recommendation(package,title,'Purpose','',Evidence(latest_file_access=date),0,[],installed_bytes=size)


class SortingTests(unittest.TestCase):
    def setUp(self):
        self.items = [item('z','beta',10,'2020-01-01T00:00:00+00:00'),
                      item('b','Alpha',100,'2025-01-01T00:00:00+00:00'),
                      item('a','alpha',100), item('d','Delta',2,'invalid')]
    def test_all_orders_and_stable_ties(self):
        expected = {'name_asc':['a','b','z','d'], 'name_desc':['d','z','a','b'],
                    'size_desc':['a','b','z','d'], 'size_asc':['d','z','a','b'],
                    'activity_asc':['z','b','a','d'], 'activity_desc':['b','z','a','d']}
        before = copy.deepcopy(self.items)
        for order,names in expected.items():
            self.assertEqual([i.package for i in sort_applications(self.items,order)], names)
        self.assertEqual(before, self.items)
    def test_entire_result_not_first_page(self):
        rows = [item(str(i),f'App {i}',i) for i in range(250)]
        result = sort_applications(rows,'size_desc')
        self.assertEqual([i.installed_bytes for i in result],list(reversed(range(250))))
    def test_filtered_input_stays_filtered(self):
        matches = [i for i in self.items if 'alpha' in i.title.casefold()]
        self.assertEqual([i.package for i in sort_applications(matches,'name_desc')],['a','b'])
    def test_no_dates_no_activity_options(self):
        self.assertEqual(len(sort_options([self.items[2],self.items[3]])),4)
        self.assertEqual(len(sort_options(self.items)),6)
    def test_request_activity_preferred_over_weak_access(self):
        row=self.items[0]
        row.evidence.server_observations={'application_activity':{'last_observed_request':'2026-02-01T12:00:00Z'}}
        observation=observed_activity(row)
        self.assertFalse(observation.weak)
        self.assertEqual(observation.source,'Application request log')
        row.evidence.server_observations=None
        self.assertTrue(observed_activity(row).weak)
    def test_equal_instants_and_unknown_last_both_directions(self):
        rows=[item('b','B',1,'2020-01-01T01:00:00+01:00'),item('a','A',1,'2020-01-01T00:00:00Z'),item('c','C',1)]
        for order in ('activity_asc','activity_desc'):
            self.assertEqual([i.package for i in sort_applications(rows,order)],['a','b','c'])
    def test_defaults(self):
        self.assertEqual(DEFAULTS,dict(large='size_desc',potential='size_desc',support='size_desc',servers='name_asc',advanced='review_priority',search='name_asc'))

    def test_last_use_label_does_not_promote_weak_evidence(self):
        from package_cleaner.gui import last_use_label
        self.assertEqual(last_use_label(self.items[0]), 'Last use: Unknown')
        self.assertEqual(last_use_label(self.items[2]), 'Last use: Unknown')
        self.items[0].evidence.server_observations = {'application_activity': {'last_observed_request': '2026-02-01T12:00:00Z'}}
        from datetime import datetime
        expected = datetime.fromisoformat('2026-02-01T12:00:00+00:00').astimezone().strftime('%b %d, %Y at %I:%M %p %Z')
        self.assertEqual(last_use_label(self.items[0]), 'Last use: ' + expected)
