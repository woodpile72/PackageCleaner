#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Disposable dpkg-root lifecycle, never the host package database.

This smoke test uses --force-depends because the temporary dpkg root starts
empty. Runtime checks mount only the application, synthetic user files and
read-only runtime libraries/tools. No removal API or host inventory is used.
"""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile


def run(args, **kwargs):
    result=subprocess.run(args,capture_output=True,text=True,**kwargs)
    if result.returncode:
        raise RuntimeError(f'Command failed ({result.returncode}): {args!r}\n'+result.stdout+result.stderr)
    return result.stdout


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('deb',type=Path)
    parser.add_argument('--upgrade-from',type=Path,help='Install this previous archive first, then upgrade')
    args=parser.parse_args()
    deb=args.deb.resolve()
    before=Path('/var/lib/dpkg/status').read_bytes()
    with tempfile.TemporaryDirectory(prefix='package-cleaner-isolated-') as tmp:
        root=Path(tmp)/'root';root.mkdir()
        namespace=['unshare','--user','--map-root-user','--mount','--pid','--fork']
        preferences=root/'home/demo/.config/package-cleaner/preferences.json'
        preferences.parent.mkdir(parents=True)
        preferences.write_text('{"version": 1, "kept": ["apt:system/demo"]}\n')
        queue=root/'home/demo/.local/state/package-cleaner/cleanup-selection.json'
        queue.parent.mkdir(parents=True)
        queue.write_text(json.dumps(['apt:system/queued-demo:amd64'])+'\n')
        saved={preferences:preferences.read_bytes(), queue:queue.read_bytes()}
        if args.upgrade_from:
            run(namespace+['dpkg','--root='+str(root),'--force-depends','--install',str(args.upgrade_from.resolve())])
            old_version=run(['dpkg-deb','-f',str(args.upgrade_from.resolve()),'Version']).strip()
            assert 'Version: '+old_version in (root/'var/lib/dpkg/status').read_text()
        run(namespace+['dpkg','--root='+str(root),'--force-depends','--install',str(deb)])
        new_version=run(['dpkg-deb','-f',str(deb),'Version']).strip()
        assert 'Version: '+new_version in (root/'var/lib/dpkg/status').read_text()
        assert all(path.read_bytes()==content for path,content in saved.items()), 'Saved preferences or queue changed'
        expected=Path(tmp)/'expected'
        run(['dpkg-deb','-x',str(deb),str(expected)])
        for file in expected.rglob('*'):
            if file.is_file():
                assert (root/file.relative_to(expected)).read_bytes()==file.read_bytes(),file
                assert (root/file.relative_to(expected)).stat().st_mode & 0o7777 == file.stat().st_mode & 0o7777, file
        if args.upgrade_from:print('Upgrade:',old_version,'->',new_version,'; all archive files, Keep preferences and saved queue verified')
        status=(root/'var/lib/dpkg/status').read_text()
        assert 'Status: install ok installed' in status
        app=root/'usr/share/package-cleaner'
        # Build a minimal read-only runtime instead of relying on this application
        # being installed on the host. No host dpkg database or user home is mounted.
        sandbox=['bwrap','--unshare-user','--uid','1000','--gid','1000',
                 '--unshare-pid','--unshare-net','--die-with-parent',
                 '--clearenv','--setenv','PATH','/usr/bin:/bin',
                 '--setenv','HOME','/home/demo','--setenv','GSETTINGS_BACKEND','memory',
                 '--proc','/proc','--dev','/dev','--tmpfs','/tmp']
        for path in ('/usr/bin','/usr/lib','/lib','/lib64','/etc'):
            if Path(path).exists():
                sandbox+=['--ro-bind',path,path]
        sandbox+=['--ro-bind',str(app),'/usr/share/package-cleaner',
                  '--ro-bind',str(root/'usr/bin'),'/opt/launchers',
                  '--ro-bind',str(root/'home/demo'),'/home/demo']
        assert 'APT and Flatpak' in run(sandbox+['/usr/bin/python3','-I','/opt/launchers/package-cleaner','--help'])
        code=(
            "import sys;sys.path.insert(0,'/usr/share/package-cleaner');"
            "from package_cleaner import __version__;assert __version__==sys.argv[1];"
            "from package_cleaner.core import Preferences,ReviewService;"
            "p=Preferences();assert p.kept=={'apt:system/demo'};"
            "s=ReviewService(p);assert s.selected=={'queued-demo:amd64'};"
            "print('Packaged CLI, version, Keep preferences and queued intentions verified; no execution authorized')"
        )
        print(run(sandbox+['/usr/bin/python3','-I','-c',code,new_version]).strip())
        run(namespace+['dpkg','--root='+str(root),'--remove','package-cleaner'])
        assert all(path.read_bytes()==content for path,content in saved.items()), 'Saved preferences or queue changed'
        assert not (root/'usr/bin/package-cleaner').exists()
        assert not (root/'usr/share/package-cleaner/package_cleaner/gui.py').exists()
    assert Path('/var/lib/dpkg/status').read_bytes()==before
    print('Isolated install, packaged launch/import and uninstall passed; host dpkg state unchanged.')

if __name__=='__main__':main()
