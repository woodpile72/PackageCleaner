# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from package_cleaner.preferences import Preferences
from package_cleaner.__main__ import main


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = {'XDG_CONFIG_HOME': str(self.root / 'config'), 'XDG_STATE_HOME': str(self.root / 'state')}
        self.env = patch.dict(os.environ, self.environment)
        self.env.start()
        self.addCleanup(self.env.stop)

    def cli(self, args, answer='n'):
        out = io.StringIO()
        with patch('sys.argv', ['package-cleaner', *args]), patch('builtins.input', return_value=answer), patch('package_cleaner.__main__.inventory') as inventory, patch('package_cleaner.__main__.simulate') as simulate, redirect_stdout(out):
            result = main()
        inventory.assert_not_called()
        simulate.assert_not_called()
        return result, out.getvalue()

    def test_xdg_storage_and_persistence(self):
        p = Preferences()
        self.assertEqual(p.path, self.root / 'config/package-cleaner/preferences.json')
        p.keep('apache2:amd64')
        self.assertEqual(Preferences().kept, {'apache2:amd64'})
        self.assertEqual(json.loads(p.path.read_text())['kept'], ['apache2:amd64'])

    def test_individual_remove_does_not_clear_others(self):
        p = Preferences()
        p.keep('apache2:amd64'); p.keep('other:amd64')
        self.assertEqual(self.cli(['ignored', 'remove', 'apache2'])[0], 0)
        self.assertEqual(Preferences().kept, {'other:amd64'})

    def test_list_is_identifiers_not_labels(self):
        Preferences().keep('apache2:amd64')
        result, text = self.cli(['ignored', 'list'])
        self.assertIn('apache2:amd64', text)
        self.assertEqual(result, 0)

    def test_clear_requires_confirmation(self):
        Preferences().keep('apache2:amd64')
        for answer in ['', 'n', 'anything']:
            self.cli(['ignored', 'clear'], answer)
            self.assertEqual(Preferences().kept, {'apache2:amd64'})
        self.cli(['ignored', 'clear'], 'yes')
        self.assertFalse(Preferences().kept)

    def test_old_reset_alias_also_confirms(self):
        Preferences().keep('apache2:amd64')
        self.cli(['preferences', 'reset', '--all'], 'n')
        self.assertEqual(Preferences().kept, {'apache2:amd64'})

    def test_eof_does_not_clear(self):
        Preferences().keep('apache2:amd64')
        with patch('sys.argv', ['package-cleaner', 'ignored', 'clear']), patch('builtins.input', side_effect=EOFError), redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
        self.assertTrue(Preferences().kept)

    def test_legacy_migration_and_empty_config_precedence(self):
        old = self.root / 'state/package-cleaner/preferences.json'
        old.parent.mkdir(parents=True)
        old.write_text(json.dumps({'version': 1, 'kept': ['apache2:amd64']}))
        p = Preferences()
        self.assertEqual(p.kept, {'apache2:amd64'})
        self.assertTrue(p.path.exists())
        self.assertTrue(old.exists())
        p.reset()
        self.assertFalse(Preferences().kept)  # Old backup cannot resurrect cleared choices.

    def test_custom_path_does_not_import_default_choices(self):
        Preferences().keep('apache2:amd64')
        self.assertFalse(Preferences(self.root / 'custom.json').kept)

    def test_failed_atomic_replace_preserves_old_file(self):
        p = Preferences()
        p.keep('apache2:amd64')
        with patch('package_cleaner.preferences.os.replace', side_effect=OSError('test failure')):
            with self.assertRaises(OSError):
                p.keep('other:amd64')
        self.assertEqual(Preferences().kept, {'apache2:amd64'})
        self.assertEqual(list(p.path.parent.iterdir()), [p.path])

    def test_ambiguous_identifier_does_not_remove_anything(self):
        p = Preferences()
        p.keep('app:amd64'); p.keep('app:i386')
        with self.assertRaises(ValueError):
            p.remove('app')
        self.assertEqual(Preferences().kept, {'app:amd64', 'app:i386'})

    def test_sessions_use_same_saved_choices(self):
        Preferences().keep('apache2:amd64')
        command = [sys.executable, '-m', 'package_cleaner', 'ignored']
        first = subprocess.run([*command, 'list'], capture_output=True, text=True, check=True)
        self.assertIn('apache2:amd64', first.stdout)
        subprocess.run([*command, 'remove', 'apache2:amd64'], capture_output=True, text=True, check=True)
        second = subprocess.run([*command, 'list'], capture_output=True, text=True, check=True)
        self.assertIn('(none)', second.stdout)

    def test_group_rename_does_not_expand_keep_to_shared_software(self):
        from package_cleaner.inventory import Package
        from package_cleaner.recommendations import recommend, Evidence
        from package_cleaner.removal import Baseline, classify
        a = Package('apache2:amd64', 'apache2', '1', False, 1, 'Web server', 'web', application=True)
        b = Package('browser:amd64', 'browser', '1', False, 100 * 1024 ** 2, 'Browser', 'web', application=True)
        b.desktop_application = True
        b.display_name = 'Browser'
        # Both share support; grouping or display-name changes must not propagate Keep.
        a.stack = 'Renamed web software'; b.stack = a.stack
        a.dependencies = ['shared:amd64']; b.dependencies = ['shared:amd64']
        shared = Package('shared:amd64', 'shared', '1', True, 1, 'Support', 'libs')
        ps = {p.name: p for p in [a, b, shared]}
        Preferences().keep(a.name)
        plan = classify([b.name], [b.name], [b.name], Baseline([], ''), ps, [], '')
        with patch('package_cleaner.recommendations.collect_evidence', return_value=Evidence()), patch('package_cleaner.recommendations.running_executables', return_value=set()), patch('package_cleaner.recommendations.simulate', return_value=plan):
            items = recommend(ps, Preferences().kept)
        self.assertEqual([r.package for r in items], [b.name])


if __name__ == '__main__':
    unittest.main()
