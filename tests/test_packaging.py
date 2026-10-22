# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Installer tests only build/extract archives; never install packages."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import shutil
from package_cleaner import __version__

ROOT=Path(__file__).resolve().parents[1]

class DebianPackageTests(unittest.TestCase):
    def test_package_contents_and_reproducibility(self):
        with tempfile.TemporaryDirectory() as temporary:
            out=Path(temporary)
            env={**os.environ,'SOURCE_DATE_EPOCH':'1792600000'}
            cmd=['/usr/bin/python3',str(ROOT/'packaging/build_deb.py'),'--output',str(out)]
            subprocess.run(cmd,check=True,capture_output=True,env=env)
            deb=out/f'package-cleaner_{__version__}_all.deb'
            first=hashlib.sha256(deb.read_bytes()).hexdigest()
            subprocess.run(cmd,check=True,capture_output=True,env=env)
            self.assertEqual(first,hashlib.sha256(deb.read_bytes()).hexdigest())
            control=out/'control';root=out/'extract'
            subprocess.run(['dpkg-deb','-e',str(deb),str(control)],check=True)
            subprocess.run(['dpkg-deb','-x',str(deb),str(root)],check=True)
            self.assertEqual({p.name for p in control.iterdir()},{'control'})
            metadata=(control/'control').read_text()
            self.assertIn('Version: '+__version__,metadata)
            depends=next(line for line in metadata.splitlines() if line.startswith('Depends:'))
            self.assertNotIn('flatpak',depends)
            self.assertIn('Recommends: flatpak, gir1.2-flatpak-1.0',metadata)
            helper=root/'usr/share/package-cleaner/package_cleaner/core/privileged.py'
            self.assertEqual(helper.stat().st_mode & 0o7777,0o644)
            self.assertIn('parents[2]',helper.read_text())
            execution=(helper.parent/'execution.py').read_text()
            self.assertIn("'/usr/bin/python3', '-I', str(helper)",execution)
            self.assertIn("'/usr/bin/sudo'",execution)
            self.assertTrue((root/'usr/share/metainfo/org.packagecleaner.Review.metainfo.xml').exists())
            listing=subprocess.check_output(['dpkg-deb','-c',str(deb)],text=True)
            self.assertTrue(all('root/root' in line for line in listing.splitlines()))
            self.assertNotIn('__pycache__',listing)
            self.assertNotIn('/tests/',listing)
            self.assertNotIn('/work/',listing)
            desktop=(root/'usr/share/applications/org.packagecleaner.Review.desktop').read_text()
            self.assertIn('Exec=package-cleaner-gui',desktop)
            self.assertNotIn('/home/',desktop)
            self.assertTrue((root/'usr/share/icons/hicolor/scalable/apps/package-cleaner.svg').exists())
            self.assertTrue((root/'usr/share/package-cleaner/data/package-cleaner.svg').exists())
            for name in ('package-cleaner','package-cleaner-gui'):
                script=root/'usr/bin'/name
                self.assertEqual(script.stat().st_mode & 0o7777,0o755)
                self.assertIn('/usr/share/package-cleaner',script.read_text())
            code="import sys;sys.path.insert(0,sys.argv[1]);from package_cleaner import __version__;from package_cleaner.gui_icons import APPLICATION_ICON;assert __version__=='0.1.3';assert APPLICATION_ICON.exists();from package_cleaner.__main__ import main;sys.argv=['package-cleaner','--help'];main()"
            result=subprocess.run(['/usr/bin/python3','-I','-c',code,str(root/'usr/share/package-cleaner')],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('usage:',result.stdout)

    def test_build_from_source_archive_without_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'source';root.mkdir()
            for name in ('package_cleaner','packaging','data','docs'):
                shutil.copytree(ROOT/name,root/name,ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copyfile(ROOT/'README.md',root/'README.md')
            env={k:v for k,v in os.environ.items() if k!='SOURCE_DATE_EPOCH'}
            result=subprocess.run(['/usr/bin/python3',str(root/'packaging/build_deb.py')],capture_output=True,text=True,env=env)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue((root/'dist'/f'package-cleaner_{__version__}_all.deb').exists())
