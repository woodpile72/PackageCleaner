# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Library contracts tested without GTK, terminal input or package execution."""
import ast
import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from package_cleaner.core import (ReviewService, Preferences, CancellationToken,
                                 OperationCancelled, ProgressEvent, operation,
                                 execute_confirmed, AuthorizationError)
from package_cleaner.core.analysis import command
from package_cleaner.core.operations import checkpoint

ROOT = Path(__file__).resolve().parents[1]


class CoreBoundaryTests(unittest.TestCase):
    def test_core_has_no_frontend_imports_or_terminal_io(self):
        for path in (ROOT/'package_cleaner/core').glob('*.py'):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [node.module or ''] if isinstance(node, ast.ImportFrom) else [n.name for n in node.names]
                    for name in names:
                        if path.name == 'flatpak_provider.py' and name in ('gi','gi.repository'):
                            self.assertFalse(any(alias.name == 'Gtk' for alias in node.names))
                            continue  # Optional libflatpak bindings, not GTK.
                        if name.startswith("apt.progress"):
                            continue  # python-apt installer API, not a frontend.
                        self.assertFalse(any(part in name.split('.') for part in ('gi','gui','terminal','presentation','progress','__main__')), (path, name))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, ('input','print'), path)

    def test_library_and_gui_import_without_cli_files_or_gtk(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)/'package_cleaner'
            package.mkdir()
            for file in ('__init__.py','gui.py','gui_icons.py','gui_dialogs.py','gui_formatting.py','display.py'):
                shutil.copy(ROOT/'package_cleaner'/file, package/file)
            shutil.copytree(ROOT/'package_cleaner/core', package/'core', ignore=shutil.ignore_patterns('__pycache__'))
            script = '''
import importlib.abc, sys
class NoUI(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname == 'gi' or fullname.startswith('gi.') or any(x in fullname.split('.') for x in ('terminal','presentation','__main__','progress')):
            raise AssertionError('Forbidden UI dependency: ' + fullname)
sys.meta_path.insert(0, NoUI())
from package_cleaner.core import ReviewService, Preferences, Plan, execute_confirmed
from package_cleaner.gui import Window
assert 'gi' not in sys.modules
'''
            result = subprocess.run([sys.executable,'-c',script],cwd=directory,env={**os.environ,'PYTHONPATH':directory},capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout,'')

    def test_core_public_service_is_silent_and_structured(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as out, patch('package_cleaner.core.review_service.inventory',return_value=({},[])), patch('package_cleaner.core.review_service.recommend',return_value=[]):
            service=ReviewService(Preferences(Path(directory)/'prefs'),selection_path=False)
            discovered=service.discover('large')
            self.assertEqual(discovered.applications,[])
            self.assertEqual(discovered.warnings,[])
            self.assertEqual(out.getvalue(),'')

    def test_pre_cancelled_scan_does_not_collect_inventory(self):
        with tempfile.TemporaryDirectory() as directory, patch('package_cleaner.core.review_service.inventory') as inventory:
            service=ReviewService(Preferences(Path(directory)/'prefs'),selection_path=False)
            token=CancellationToken(); token.cancel()
            with self.assertRaises(OperationCancelled):
                service.discover('large',cancellation=token)
            inventory.assert_not_called()

    def test_progress_is_a_typed_event(self):
        events=[]
        with operation(events.append): checkpoint('discovery',2,9)
        self.assertEqual(events,[ProgressEvent('discovery',2,9)])

    def test_read_only_subprocess_cancellation_is_prompt(self):
        token=CancellationToken()
        timer=threading.Timer(0.15,token.cancel)
        timer.start()
        started=time.monotonic()
        try:
            with operation(cancellation=token), self.assertRaises(OperationCancelled):
                command([sys.executable,'-c','import time; time.sleep(10)'])
        finally:
            timer.cancel()
        self.assertLess(time.monotonic()-started,2)

    def test_confirmation_and_opt_in_are_core_owned(self):
        with patch('package_cleaner.core.execution.launch') as launch:
            with self.assertRaises(AuthorizationError): execute_confirmed({},'REMOVE')
            for text in ('remove','REMOVE ','yes',''):
                self.assertIsNone(execute_confirmed({},text,enabled=True))
            launch.assert_not_called()
            launch.return_value={'status':'completed','helper_verified':True}
            result=execute_confirmed({'approved':'record'},'REMOVE',enabled=True)
            self.assertTrue(result.completed)
            launch.assert_called_once_with({'approved':'record'})

    def test_cancel_after_execution_start_does_not_drop_result(self):
        token=CancellationToken()
        def fake_launch(record):
            token.cancel()
            return {'status':'unknown','helper_verified':False}
        with operation(cancellation=token), patch('package_cleaner.core.execution.launch',side_effect=fake_launch):
            result=execute_confirmed({},'REMOVE',enabled=True)
        self.assertFalse(result.completed)
        self.assertEqual(result['status'],'unknown')
