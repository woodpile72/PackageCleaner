#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Disposable dpkg-root lifecycle, never the host package database.

Dependency resolution is separately checked with APT simulation. This smoke test
uses --force-depends because the temporary dpkg root starts empty. Runtime checks
mount the resulting application into a read-only view of host libraries/tools.
"""
import argparse
import hashlib
from pathlib import Path
import subprocess
import tempfile


def run(args, **kwargs):
    result=subprocess.run(args,capture_output=True,text=True,**kwargs)
    if result.returncode:
        raise RuntimeError(result.stdout+result.stderr)
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
        saved=preferences.read_bytes()
        if args.upgrade_from:
            run(namespace+['dpkg','--root='+str(root),'--force-depends','--install',str(args.upgrade_from.resolve())])
            old_version=run(['dpkg-deb','-f',str(args.upgrade_from.resolve()),'Version']).strip()
            assert 'Version: '+old_version in (root/'var/lib/dpkg/status').read_text()
        run(namespace+['dpkg','--root='+str(root),'--force-depends','--install',str(deb)])
        new_version=run(['dpkg-deb','-f',str(deb),'Version']).strip()
        assert 'Version: '+new_version in (root/'var/lib/dpkg/status').read_text()
        assert preferences.read_bytes()==saved
        expected=Path(tmp)/'expected'
        run(['dpkg-deb','-x',str(deb),str(expected)])
        for file in expected.rglob('*'):
            if file.is_file():
                assert (root/file.relative_to(expected)).read_bytes()==file.read_bytes(),file
        if args.upgrade_from:print('Upgrade:',old_version,'->',new_version,'; all archive files and saved preferences verified')
        status=(root/'var/lib/dpkg/status').read_text()
        assert 'Status: install ok installed' in status
        app=root/'usr/share/package-cleaner'
        sandbox=['bwrap','--unshare-user','--uid','1000','--gid','1000','--unshare-pid','--unshare-net',
                 '--die-with-parent','--setenv','GSETTINGS_BACKEND','memory','--ro-bind','/','/','--tmpfs','/tmp','--setenv','HOME','/tmp',
                 '--ro-bind',str(app),'/usr/share/package-cleaner']
        for name in ('package-cleaner','package-cleaner-gui'):
            sandbox+=['--ro-bind',str(root/'usr/bin'/name),'/usr/bin/'+name]
        assert 'APT and Flatpak' in run(sandbox+['/usr/bin/package-cleaner','--help'])
        # Actual inventory and a read-only, application resolver preview.
        # The entire host filesystem remains read-only and no execution API is called.
        code="import sys;sys.path.insert(0,'/usr/share/package-cleaner');from package_cleaner.core.providers import inventory,preview;ps,w=inventory();n=next(n for n,p in ps.items() if p.provider=='apt' and p.base=='package-cleaner');p=preview([n],ps);assert not p.blocked;assert n in p.removed;assert not any(ps[x].protected for x in p.removed);print('Discovery:',len(ps),'packages; explicit preview validated with protected orphans excluded')"
        print(run(sandbox+['/usr/bin/python3','-I','-c',code]).strip())
        run(namespace+['dpkg','--root='+str(root),'--remove','package-cleaner'])
        assert preferences.read_bytes()==saved
        assert not (root/'usr/bin/package-cleaner').exists()
        assert not (root/'usr/share/package-cleaner/package_cleaner/gui.py').exists()
    assert Path('/var/lib/dpkg/status').read_bytes()==before
    print('Isolated install, packaged launch/import, discovery, preview and removal passed; host dpkg state unchanged.')

if __name__=='__main__':main()
