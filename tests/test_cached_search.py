# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from package_cleaner.core.inventory import Package
from package_cleaner.core.preferences import Preferences
from package_cleaner.core.review_service import ReviewService
from package_cleaner.core.providers import filter_sources


class CachedSearchTests(unittest.TestCase):
    def test_global_metadata_search_keeps_and_exact_components(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs=Preferences(Path(tmp)/'prefs')
            service=ReviewService(prefs,False)
            apt=Package('paint:amd64','paint','1',False,5,'Edit photographs','graphics',application=True,display_name='Picture Studio')
            flat=Package('flatpak:user/app/org.example.Art/x86_64/stable','org.example.Art','1',False,6,'Edit photographs','flatpak-app',provider='flatpak',application=True,display_name='Art Studio')
            protected=Package('libudev1:i386','libudev1','1',True,1,'Device helper','libs',protected=['system'],orphan=True)
            component=Package('support:amd64','support','1',True,1,'Studio support','libs')
            service.packages={p.name:p for p in [apt,flat,protected,component]}
            service.selected={flat.name}
            prefs.keep(apt.identity)
            before=prefs.path.read_bytes()
            with patch.object(service,'inventory_provider',side_effect=AssertionError('No rescan')),patch.object(service,'simulation_provider',side_effect=AssertionError('No simulation')),patch('package_cleaner.core.recommendations.collect_evidence',side_effect=AssertionError('No inspection')):
                for query in ('studio','photographs'):
                    items=service.search_cached(query).applications
                    self.assertEqual({i.package for i in items},{apt.name,flat.name})
                self.assertEqual(service.search_cached('org.example.Art').applications[0].package,flat.name)
                found=service.search_cached('libudev1:i386').applications
                self.assertTrue(found[0].protected)
                self.assertEqual(service.search_cached('support:amd64').applications[0].package,component.name)
                self.assertEqual(service.search_cached('Device').applications,[])
                self.assertEqual(service.search_cached(' ').applications,[])
                self.assertEqual(len(filter_sources(items,'apt')),1)
                self.assertEqual(len(filter_sources(items,'flatpak')),1)
            self.assertEqual(service.selected,{flat.name})
            self.assertEqual(before,prefs.path.read_bytes())
