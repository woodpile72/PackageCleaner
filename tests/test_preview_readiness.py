# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
import unittest
from package_cleaner.core.review_service import Preview
from package_cleaner.core.removal import Plan, Baseline

class ReadinessTests(unittest.TestCase):
    def preview(self):
        return Preview(Plan(['app'],['app'],[],[],[],[],[],{},[],'',Baseline([],'')),['App'],0,0,[])
    def test_executable(self):
        preview=self.preview();self.assertTrue(preview.ready);self.assertEqual(preview.unavailable_reasons,[])
    def test_blocked(self):
        preview=self.preview();preview.plan.blocked=['Protected'];self.assertFalse(preview.ready)
        self.assertEqual(preview.unavailable_reasons,['Protected'])
    def test_disabled_with_reason(self):
        preview=self.preview();preview.plan.execution_disabled=True
        preview.plan.observations=['Required validation signal unavailable']
        self.assertFalse(preview.ready);self.assertIn('Required validation signal unavailable',preview.unavailable_reasons)
    def test_combined_plan_preserves_child_reason(self):
        preview=self.preview();child=self.preview().plan;child.execution_disabled=True
        child.observations=['Flatpak ready signal unavailable'];preview.plan.provider_plans=[child]
        self.assertFalse(preview.ready);self.assertEqual(preview.unavailable_reasons,child.observations)
    def test_empty_or_disabled_without_reason(self):
        preview=self.preview();preview.plan.removed=[];self.assertFalse(preview.ready)
        preview.plan.execution_disabled=True;self.assertTrue(preview.unavailable_reasons)
    def test_incomplete_inventory_is_not_ready(self):
        preview=self.preview();preview.warnings=['Inventory could not be inspected']
        self.assertFalse(preview.ready);self.assertIn(preview.warnings[0],preview.unavailable_reasons)
    def test_informational_observation_is_not_a_block(self):
        preview=self.preview();preview.plan.observations=['Application data retained']
        preview.warnings=list(preview.plan.observations);self.assertTrue(preview.ready)

class OptionalFlatpakTests(unittest.TestCase):
    def test_apt_inventory_survives_absent_flatpak(self):
        from unittest.mock import patch
        from package_cleaner.core.providers import inventory
        with patch('package_cleaner.core.providers.AptProvider.discover',return_value=({'apt-example':object()},[])), patch('package_cleaner.core.flatpak_provider.load_api',return_value=None),patch('shutil.which',return_value=None):
            packages,warnings=inventory()
        self.assertIn('apt-example',packages);self.assertEqual(warnings,[])
    def test_missing_bindings_for_installed_flatpak_remain_visible(self):
        from unittest.mock import patch
        from package_cleaner.core.providers import inventory
        with patch('package_cleaner.core.providers.AptProvider.discover',return_value=({'apt-example':object()},[])),patch('package_cleaner.core.flatpak_provider.load_api',return_value=None),patch('shutil.which',return_value='/usr/bin/flatpak'):
            packages,warnings=inventory()
        self.assertIn('apt-example',packages);self.assertTrue(warnings)
