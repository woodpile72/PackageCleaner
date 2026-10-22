# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
import io
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch
from package_cleaner.inventory import Package
from package_cleaner.recommendations import collect_evidence, recommend
from package_cleaner.inspection import open_inspection
from package_cleaner.presentation import review
from package_cleaner.preferences import Preferences


class StabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'example'
        self.path.write_text('synthetic executable configuration')
        self.path.chmod(0o755)
        # Only a test-owned file, never an installed package file.
        os.utime(self.path,(1_000_000_000,1_100_000_000))
        self.app=Package('example:amd64','example','1',False,100_000_000,'Example editor','graphics',
                         application=True,desktop_application=True,display_name='Example')
        self.ps={self.app.name:self.app}
        self.virtual='/usr/bin/package-cleaner-test-fixture'

    def path_factory(self,value):
        return self.path if str(value)==self.virtual else Path(value)

    def evidence(self):
        with patch('package_cleaner.recommendations.Path',side_effect=self.path_factory):
            return collect_evidence(self.app,set(),paths=[self.virtual])

    def test_metadata_inspection_does_not_open_executable_contents(self):
        before=self.path.stat().st_atime_ns
        with patch('package_cleaner.recommendations.open_inspection',side_effect=AssertionError('content read')):
            self.assertEqual(self.evidence().old_accesses,1)
        self.assertEqual(self.path.stat().st_atime_ns,before)

    def test_executable_conffile_read_cannot_contaminate_next_pass(self):
        self.app.conffiles=[(str(self.path),'0'*32)]
        before=self.path.stat().st_atime_ns
        first,second=self.evidence(),self.evidence()
        self.assertEqual(first.old_accesses,1);self.assertEqual(second.old_accesses,1)
        self.assertEqual(second.recent_accesses,0)
        self.assertEqual(self.path.stat().st_atime_ns,before)

    def test_noatime_permission_failure_does_not_fall_back_to_read(self):
        self.app.conffiles=[(str(self.path),'0'*32)]
        before=self.path.stat().st_atime_ns
        with patch('package_cleaner.inspection.fcntl.fcntl',side_effect=PermissionError('no CAP_FOWNER')):
            evidence=self.evidence()
        self.assertEqual(evidence.unreadable_configuration,1)
        self.assertEqual(self.path.stat().st_atime_ns,before)

    def test_symlink_inspection_protects_executable_target(self):
        alias=Path(self.temp.name)/'config';alias.symlink_to(self.path)
        before=self.path.stat().st_atime_ns
        with open_inspection(alias) as stream:self.assertTrue(stream.read())
        self.assertEqual(self.path.stat().st_atime_ns,before)

    def test_repeated_discovery_same_evidence_same_membership(self):
        runs=[]
        with patch('package_cleaner.recommendations.Path',side_effect=self.path_factory),patch(
                'package_cleaner.recommendations.package_paths',return_value=[self.virtual]),patch(
                'package_cleaner.recommendations.running_executables',return_value=set()),patch(
                'package_cleaner.removal.apt_simulation',side_effect=AssertionError('simulation during discovery')):
            for _ in range(3):
                diagnostic={};items=recommend(self.ps,category='potential',diagnostics=diagnostic)
                runs.append(([i.package for i in items],diagnostic['exclusions'],diagnostic['classifications']))
        self.assertEqual(runs[0],runs[1]);self.assertEqual(runs[1],runs[2])
        self.assertEqual(runs[0][0],[self.app.name])

    def test_real_evidence_changes_can_change_membership(self):
        with patch('package_cleaner.recommendations.Path',side_effect=self.path_factory),patch(
                'package_cleaner.recommendations.package_paths',return_value=[self.virtual]),patch(
                'package_cleaner.recommendations.running_executables',return_value=set()):
            self.assertEqual(len(recommend(self.ps,category='potential')),1)
            # Simulate an external read on a disposable fixture, not a package.
            self.path.read_bytes()
            diagnostic={};self.assertFalse(recommend(self.ps,category='potential',diagnostics=diagnostic))
        self.assertIn('Recent file reads',diagnostic['exclusions'][self.app.name][0])

    def test_keep_exclusion_has_explicit_diagnostic_reason(self):
        diagnostic={};self.assertFalse(recommend(self.ps,kept=[self.app.name],category='potential',diagnostics=diagnostic))
        self.assertEqual(diagnostic['exclusions'][self.app.name],['Saved Keep preference.'])

    def test_execution_flag_cannot_change_review_discovery(self):
        outputs=[]
        prefs=Preferences(Path(self.temp.name)/'preferences')
        with patch('package_cleaner.recommendations.Path',side_effect=self.path_factory),patch(
                'package_cleaner.recommendations.package_paths',return_value=[self.virtual]),patch(
                'package_cleaner.recommendations.running_executables',return_value=set()),patch(
                'package_cleaner.presentation.inventory',return_value=(self.ps,[])),patch(
                'package_cleaner.presentation.execute_selection',side_effect=AssertionError('Execution forbidden')) as execute:
            for enabled in (False,True):
                out=io.StringIO()
                with patch('builtins.input',return_value='q'),redirect_stdout(out),redirect_stderr(io.StringIO()):
                    review(None,preferences=prefs,category='potential',enable_removal=enabled)
                outputs.append(out.getvalue())
        self.assertEqual(outputs[0],outputs[1]);execute.assert_not_called()
